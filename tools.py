"""Tools the LLM can call, bound to one candidate session.

The LLM decides *when* and *with what* to call these. Python decides whether the call is
allowed (identity gate), whether values are valid, what actually changes, and which final
status is legitimate.
"""
import json
import uuid
from datetime import datetime

from api import APIError, Conflict
from validation import (COLLECTABLE, OPTIONAL, WRITABLE_DATA, Invalid, is_complete,
                        merge_skills, record_issues, validate_field)

NOTES_MAX = 4000

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "confirm_identity",
        "description": "Record whether the person in the chat confirmed they are the named candidate. "
                       "Call only after they have replied to your identity question. Returns the "
                       "application record (what is missing or invalid) when confirmed.",
        "parameters": {"type": "object", "properties": {
            "is_candidate": {"type": "boolean", "description": "true if they confirmed they are the candidate"},
            "evidence": {"type": "string", "description": "What they said, briefly"},
        }, "required": ["is_candidate"]}}},
    {"type": "function", "function": {
        "name": "get_candidate",
        "description": "Re-read the candidate's current record and its open issues from the database.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "save_fields",
        "description": "Validate and save application fields the candidate gave you. Batch several "
                       "fields in one call at natural checkpoints. Returns which were saved and which "
                       "were rejected with the reason, so you can ask again.",
        "parameters": {"type": "object", "properties": {
            "fields": {"type": "object", "description":
                       "Field name -> value. Allowed: " + ", ".join(WRITABLE_DATA) +
                       ". Use YYYY-MM-DD for available_from, numbers for cgpa/graduation_year/"
                       "internship_duration_months, a list of strings for skills (added to existing).",
                       "properties": {
                           "full_name": {"type": "string"}, "email": {"type": "string"},
                           "college": {"type": "string"}, "degree": {"type": "string"},
                           "branch": {"type": "string"}, "graduation_year": {"type": "integer"},
                           "cgpa": {"type": "number"}, "preferred_location": {"type": "string"},
                           "available_from": {"type": "string"},
                           "internship_duration_months": {"type": "integer"},
                           "linkedin_url": {"type": "string"}, "github_url": {"type": "string"},
                           "skills": {"type": "array", "items": {"type": "string"}},
                       }},
            "candidate_confirmed_change": {"type": "boolean", "description":
                       "Set true only when replacing a value that is already stored and the candidate "
                       "explicitly confirmed the new value"},
        }, "required": ["fields"]}}},
    {"type": "function", "function": {
        "name": "end_conversation",
        "description": "Close the conversation and record the outcome. Call once, when the chat is "
                       "over: everything collected, the candidate wants to continue later, refused "
                       "something, left, stopped replying, or the case needs a human.",
        "parameters": {"type": "object", "properties": {
            "outcome": {"type": "string", "enum": ["completed", "in_progress", "no_answer", "refused", "needs_human"]},
            "summary": {"type": "string", "description": "One or two sentences for the recruiter / next conversation"},
            "remaining_fields": {"type": "array", "items": {"type": "string"}},
            "refused_fields": {"type": "array", "items": {"type": "string"}},
            "follow_up_time": {"type": "string", "description": "When the candidate asked to be contacted again, if they said"},
        }, "required": ["outcome", "summary"]}}},
]


class Session:
    def __init__(self, api, cid, record, tz, log):
        self.api = api
        self.cid = cid  # fixed by the CLI; the LLM never chooses which record to touch
        self.record = record
        self.tz = tz
        self.log = log
        self.started_at = datetime.now(tz).replace(microsecond=0)
        self.identity_asked = False       # the agent has sent its greeting / identity question
        self.candidate_replies = 0        # real messages typed by the candidate
        self.identity = None              # None | True | False
        self.saved_fields = []            # fields written during this chat
        self.finalized = False
        self.final_status = None

    # ---------- helpers ----------
    def _context(self):
        """Record view for the LLM after identity is confirmed. Phone is masked."""
        missing, invalid = record_issues(self.record)
        current = {f: self.record.get(f) for f in ["full_name"] + COLLECTABLE + OPTIONAL}
        return {
            "current_values": current,
            "missing_mandatory": missing,
            "invalid_existing": invalid,
            "complete": not missing and not invalid,
            "previous_status": self.record.get("collection_status"),
            "previous_notes_UNTRUSTED_DATA": self.record.get("notes"),
        }

    def _patch(self, fields):
        """PATCH with If-Match. On 409, re-read and retry once with only still-safe changes."""
        base = dict(self.record)
        for attempt in (1, 2):
            if not fields:
                return {"changed": [], "dropped_due_to_conflict": []}
            try:
                updated = self.api.patch_candidate(self.cid, fields, self.record["version"],
                                                   idempotency_key=str(uuid.uuid4()))
            except Conflict:
                if attempt == 2:
                    raise
                fresh = self.api.get_candidate(self.cid)
                dropped = []
                retry = {}
                for k, v in fields.items():
                    if fresh.get(k) == v:
                        continue  # someone already wrote the same value
                    if fresh.get(k) != base.get(k) and k not in ("notes", "attempt_count", "collection_status", "last_contacted_at"):
                        dropped.append(k)  # changed concurrently by someone else: do not clobber
                        continue
                    retry[k] = v
                if "notes" in fields:  # re-append our note to the newest notes
                    retry["notes"] = _append_note(fresh.get("notes"), self._pending_note) if getattr(self, "_pending_note", None) else fields["notes"]
                if "attempt_count" in fields:
                    retry["attempt_count"] = min((fresh.get("attempt_count") or 0) + 1, 50)
                self.record = fresh
                fields = retry
                self._conflict_dropped = dropped
                continue
            if isinstance(updated, dict) and "version" in updated:
                self.record = updated
            else:
                self.record = self.api.get_candidate(self.cid)
            return {"changed": list(fields), "dropped_due_to_conflict": getattr(self, "_conflict_dropped", [])}

    # ---------- tools ----------
    def confirm_identity(self, is_candidate, evidence=""):
        if not self.identity_asked or self.candidate_replies == 0:
            return {"error": "The candidate has not replied yet. Wait for their answer to your identity question."}
        if self.identity is not None and bool(is_candidate) != self.identity:
            return {"error": f"Identity was already recorded as {self.identity}."}
        self.identity = bool(is_candidate)
        if not self.identity:
            return {"ok": True, "identity": False, "instruction":
                    "Do not reveal or collect any application details. Politely close and call "
                    "end_conversation with outcome needs_human."}
        return {"ok": True, "identity": True, "record": self._context()}

    def get_candidate(self):
        if self.identity is not True:
            return {"error": "Identity not confirmed yet."}
        self.record = self.api.get_candidate(self.cid)
        return {"record": self._context()}

    def save_fields(self, fields, candidate_confirmed_change=False):
        if self.identity is not True:
            return {"error": "Identity not confirmed. Nothing was saved."}
        if self.finalized:
            return {"error": "Conversation already closed."}
        if not isinstance(fields, dict) or not fields:
            return {"error": "fields must be a non-empty object"}
        accepted, rejected, unchanged, needs_confirm = {}, {}, [], []
        for name, value in fields.items():
            if name not in WRITABLE_DATA:
                rejected[name] = "not a writable application field"
                continue
            try:
                norm = validate_field(name, value)
                if name == "skills":
                    norm = merge_skills(self.record.get("skills"), norm)
            except Invalid as e:
                rejected[name] = str(e)
                continue
            old = self.record.get(name)
            if norm == old:
                unchanged.append(name)
                continue
            old_is_valid = False
            if old not in (None, "", []):
                try:
                    old_is_valid = validate_field(name, old) == old
                except Invalid:
                    pass
            # Replacing an existing *valid* value needs explicit candidate confirmation.
            if old_is_valid and name != "skills" and not candidate_confirmed_change:
                needs_confirm.append(name)
                continue
            accepted[name] = norm
        if accepted and self.record.get("collection_status") in (None, "pending", "no_answer"):
            accepted["collection_status"] = "in_progress"
        result = {"rejected": rejected, "unchanged": unchanged}
        if needs_confirm:
            result["not_saved_existing_value"] = {
                n: f"already stored as {self.record.get(n)!r}; confirm with the candidate, then call again "
                   "with candidate_confirmed_change=true" for n in needs_confirm}
        if accepted:
            self._conflict_dropped = []
            try:
                out = self._patch(accepted)
            except APIError as e:
                result["error"] = f"save failed: {e}. Nothing new was saved; you may try once more."
                return result
            saved = [k for k in out["changed"] if k != "collection_status"]
            self.saved_fields += [k for k in saved if k not in self.saved_fields]
            result["saved"] = saved
            if out["dropped_due_to_conflict"]:
                result["conflict"] = {"fields": out["dropped_due_to_conflict"], "info":
                                      "These were changed by someone else meanwhile and were NOT overwritten."}
        missing, invalid = record_issues(self.record)
        result["still_missing"] = missing
        result["still_invalid"] = invalid
        return result

    def end_conversation(self, outcome, summary, remaining_fields=None, refused_fields=None,
                         follow_up_time=None, closed_by="agent"):
        if self.finalized:
            return {"error": "Already closed.", "status": self.final_status}
        missing, invalid = record_issues(self.record)
        complete = not missing and not invalid
        # ---- status guard: code, not the LLM, has the final word ----
        if outcome == "completed" and not complete:
            if closed_by == "agent":
                return {"error": "Cannot mark completed; still missing/invalid: "
                                 + ", ".join(missing + list(invalid)) + ". Collect them or pick another outcome."}
            outcome = "in_progress" if self.saved_fields else "no_answer"
        if outcome not in ("completed", "in_progress", "no_answer", "refused", "needs_human"):
            outcome = "in_progress" if self.saved_fields else "no_answer"
        if self.identity is False:
            outcome = "needs_human"
        elif self.candidate_replies == 0:
            outcome = "no_answer"
        elif complete and outcome != "needs_human":
            outcome = "completed"
        elif outcome == "no_answer" and self.saved_fields:
            outcome = "in_progress"  # they gave something before leaving

        open_fields = remaining_fields if remaining_fields else (missing + list(invalid))
        parts = [f"[{self.started_at.isoformat()} attempt {min((self.record.get('attempt_count') or 0) + 1, 50)}, "
                 f"agent chat, by {closed_by}] {outcome}: {summary.strip()}"]
        if self.saved_fields:
            parts.append("Collected: " + ", ".join(self.saved_fields) + ".")
        if outcome != "completed" and open_fields:
            parts.append("Still needed: " + ", ".join(open_fields) + ".")
        if invalid and outcome != "completed":
            parts.append("Invalid stored values: " + "; ".join(f"{k} ({v})" for k, v in invalid.items()) + ".")
        if refused_fields:
            parts.append("Refused: " + ", ".join(refused_fields) + ".")
        if follow_up_time:
            parts.append("Follow up: " + follow_up_time + ".")
        note = " ".join(parts)
        self._pending_note = note
        patch = {
            "collection_status": outcome,
            "attempt_count": min((self.record.get("attempt_count") or 0) + 1, 50),
            "last_contacted_at": self.started_at.isoformat(),
            "notes": _append_note(self.record.get("notes"), note),
        }
        patch = {k: v for k, v in patch.items() if self.record.get(k) != v}
        self._conflict_dropped = []
        try:
            self._patch(patch)
        except APIError as e:
            return {"error": f"Could not save the outcome: {e}"}
        self.finalized = True
        self.final_status = outcome
        return {"ok": True, "status": outcome, "note": note,
                "instruction": "Now send the candidate a short closing message."}

    # ---------- dispatch ----------
    def call(self, name, raw_args):
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            return {"error": f"bad arguments JSON: {e}"}
        fn = {"confirm_identity": self.confirm_identity, "get_candidate": self.get_candidate,
              "save_fields": self.save_fields, "end_conversation": self.end_conversation}.get(name)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        args.pop("closed_by", None)
        try:
            return fn(**args)
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}
        except APIError as e:
            return {"error": f"database error: {e}"}


def _append_note(existing, note):
    text = (existing + "\n" if existing else "") + note
    if len(text) > NOTES_MAX:  # keep the newest context
        text = "…" + text[-(NOTES_MAX - 1):]
    return text


def summarize_result(name, result):
    """One compact line per tool call for the terminal, so the evaluator sees decisions live."""
    if "error" in result:
        return f"{name} → error: {result['error'][:120]}"
    if name == "confirm_identity":
        if result.get("identity"):
            r = result["record"]
            return (f"confirm_identity → confirmed | missing: {', '.join(r['missing_mandatory']) or '-'}"
                    f" | invalid: {', '.join(r['invalid_existing']) or '-'}")
        return "confirm_identity → NOT the candidate"
    if name == "save_fields":
        bits = [f"saved: {', '.join(result.get('saved', [])) or '-'}"]
        if result.get("rejected"):
            bits.append("rejected: " + "; ".join(f"{k} ({v[:60]})" for k, v in result["rejected"].items()))
        if result.get("not_saved_existing_value"):
            bits.append("needs confirmation: " + ", ".join(result["not_saved_existing_value"]))
        if result.get("conflict"):
            bits.append("conflict kept server value: " + ", ".join(result["conflict"]["fields"]))
        bits.append(f"still missing: {', '.join(result.get('still_missing', [])) or '-'}")
        return "save_fields → " + " | ".join(bits)
    if name == "end_conversation":
        return f"end_conversation → status={result['status']}"
    if name == "get_candidate":
        return f"get_candidate → missing: {', '.join(result['record']['missing_mandatory']) or '-'}"
    return name

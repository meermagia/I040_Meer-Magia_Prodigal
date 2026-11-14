"""ACME candidate-collection agent.

    python agent.py --candidate-id c001 [--timeout 120]

Fetch the candidate, decide whether a chat is needed, then let the LLM run the conversation and
call tools (tools.py) until it records an outcome. The candidate types in this terminal.
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI

from api import APIError, CandidateAPI
from prompts import build_system_prompt
from tools import TOOL_SPECS, Session, summarize_result
from validation import record_issues

ROOT = Path(__file__).resolve().parent
MAX_TOOL_STEPS = 8   # per candidate turn
MAX_TURNS = 40       # hard stop for runaway chats

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    DIM = BOLD = RESET = ""


# ---------------------------------------------------------------- run log
class RunLog:
    """Structured JSONL trace + readable transcript in runs/. Secrets never logged; phones masked."""

    def __init__(self, cid):
        (ROOT / "runs").mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.base = ROOT / "runs" / f"{stamp}_{cid}"
        self.f = open(f"{self.base}.jsonl", "w")
        self.transcript = []
        self.t0 = time.time()

    def __call__(self, event, **data):
        rec = {"t": round(time.time() - self.t0, 2), "event": event, **data}
        self.f.write(json.dumps(_mask(rec), default=str) + "\n")
        self.f.flush()

    def say(self, who, text):
        self.transcript.append((who, text))
        self("chat", who=who, text=text)

    def close(self, header):
        lines = [header, ""]
        for who, text in self.transcript:
            lines.append(f"**{who}:** {text}" if who in ("Agent", "Candidate") else f"> _{text}_")
            lines.append("")
        Path(f"{self.base}.md").write_text(_mask("\n".join(lines)))
        self.f.close()


def _mask(obj):
    if isinstance(obj, dict):
        return {k: ("+91******" + str(v)[-4:] if k == "phone" and v else _mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(x) for x in obj]
    if isinstance(obj, str):
        return re.sub(r"\+\d{6,}(\d{4})", r"+**********\1", obj)
    return obj


# ---------------------------------------------------------------- LLM
class LLMDown(Exception):
    pass


class LLM:
    def __init__(self, log):
        self.client = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"],
                             timeout=45, max_retries=0)
        self.models = [m for m in [os.environ.get("LLM_MODEL"), os.environ.get("LLM_FALLBACK_MODEL")] if m]
        self.log = log

    def complete(self, messages):
        last = None
        for model in self.models:
            for attempt in range(3):
                t0 = time.time()
                try:
                    r = self.client.chat.completions.create(model=model, messages=messages, tools=TOOL_SPECS)
                    self.log("llm", model=model, ms=int((time.time() - t0) * 1000))
                    return r.choices[0].message
                except Exception as e:  # 429 / 5xx / timeouts: back off, then try the fallback model
                    last = e
                    self.log("llm_error", model=model, attempt=attempt + 1, error=f"{type(e).__name__}: {str(e)[:300]}")
                    status = getattr(e, "status_code", None)
                    if status in (400, 401, 403, 404):
                        break
                    time.sleep(2 ** attempt)
        raise LLMDown(f"{type(last).__name__}: {str(last)[:200]}")


# ---------------------------------------------------------------- terminal input
def start_reader():
    """Read stdin on a daemon thread so the main loop can time out on silence."""
    q = queue.Queue()

    def run():
        while True:
            line = sys.stdin.readline()
            if not line:
                q.put(None)  # EOF
                return
            q.put(line.rstrip("\n"))

    threading.Thread(target=run, daemon=True).start()
    return q


# ---------------------------------------------------------------- agent turn
def agent_turn(llm, messages, session, log):
    """Let the LLM act until it produces a message for the candidate (or hits the step cap)."""
    for _ in range(MAX_TOOL_STEPS):
        msg = llm.complete(messages)
        messages.append(msg.model_dump(exclude_none=True))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                result = session.call(tc.function.name, tc.function.arguments)
                log("tool", name=tc.function.name, args=tc.function.arguments, result=result)
                line = summarize_result(tc.function.name, result)
                log.say("tool", line)
                print(f"{DIM}  · {line}{RESET}")
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, default=str)})
            continue
        text = (msg.content or "").strip()
        if text:
            print(f"\n{BOLD}Riya:{RESET} {text}\n")
            log.say("Agent", text)
            session.identity_asked = True
        return
    log("warning", message="tool step cap reached")


def event(messages, text, log):
    log.say("event", text)
    messages.append({"role": "user", "content": f"[EVENT] {text}"})


def reconcile_complete(api, record, log):
    """No chat needed: only fix a stale status, never touch attempts or data."""
    status = record.get("collection_status")
    if status == "completed":
        print("Record is complete and already marked completed. Nothing to do; no chat started.")
        return
    if status == "needs_human":
        print("Record data is complete but flagged needs_human; leaving it for a human. No chat started.")
        return
    api.patch_candidate(record["id"], {"collection_status": "completed"}, record["version"])
    print(f"Record is complete; status corrected {status} → completed. No chat started.")


def main():
    ap = argparse.ArgumentParser(description="ACME candidate information collection agent")
    ap.add_argument("--candidate-id", required=True)
    ap.add_argument("--timeout", type=float, default=120,
                    help="seconds of candidate silence before a nudge; a second silence ends the chat")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    for var in ("CANDIDATE_API_BASE_URL", "CANDIDATE_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        if not os.environ.get(var):
            sys.exit(f"Missing {var} in .env (see .env.example)")
    tz = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "Asia/Kolkata"))
    cid = args.candidate_id.strip()
    log = RunLog(cid)
    api = CandidateAPI(os.environ["CANDIDATE_API_BASE_URL"], os.environ["CANDIDATE_API_KEY"],
                       timeout=float(os.environ.get("CANDIDATE_API_TIMEOUT_SECONDS", 20)), log=log)

    print(f"{DIM}Fetching candidate {cid}…{RESET}")
    try:
        record = api.get_candidate(cid)
    except APIError as e:
        log("abort", error=str(e))
        log.close(f"# Run {cid}: aborted ({e})")
        sys.exit({404: f"No candidate with id {cid}.", 401: "API key rejected (401)."}.get(e.status, f"API error: {e}"))

    missing, invalid = record_issues(record)
    log("plan", missing=missing, invalid=invalid, status=record.get("collection_status"))
    if not missing and not invalid:
        try:
            reconcile_complete(api, record, log)
        except APIError as e:
            print(f"Could not update status: {e}")
        log.close(f"# Run {cid}: complete record, no chat")
        return
    print(f"{DIM}Needs attention → missing: {', '.join(missing) or '-'} | invalid: {', '.join(invalid) or '-'}{RESET}")
    print(f"{DIM}Chat started. You are the candidate. Type /quit to leave; silence for {int(args.timeout)}s "
          f"gets one nudge, then the chat closes.{RESET}")

    session = Session(api, cid, record, tz, log)
    llm = LLM(log)
    today = datetime.now(tz).strftime("%A, %d %B %Y")
    messages = [{"role": "system", "content": build_system_prompt(record["full_name"], today)}]
    event(messages, "Chat opened. Send your first message to the applicant.", log)
    reason = None
    q = start_reader()
    try:
        agent_turn(llm, messages, session, log)
        silences = turns = 0
        while not session.finalized and turns < MAX_TURNS:
            turns += 1
            print("You: ", end="", flush=True)
            try:
                line = q.get(timeout=args.timeout)
            except queue.Empty:
                print()
                silences += 1
                if silences == 1:
                    event(messages, f"No reply for {int(args.timeout)} seconds. Send one short, friendly nudge.", log)
                    agent_turn(llm, messages, session, log)
                    continue
                event(messages, "Still no reply after the nudge. The chat is over: call end_conversation now "
                                "(no_answer if they never engaged, in_progress if they gave some info).", log)
                agent_turn(llm, messages, session, log)
                reason = "candidate stopped replying"
                break
            if line is None or line.strip().lower() in ("/quit", "/exit"):
                if line is None:
                    print()
                event(messages, "The candidate left the chat (closed it). They cannot read further messages. "
                                "Call end_conversation now with the right outcome.", log)
                agent_turn(llm, messages, session, log)
                reason = "candidate left the chat"
                break
            if not line.strip():
                continue
            silences = 0
            session.candidate_replies += 1
            log.say("Candidate", line)
            messages.append({"role": "user", "content": line})
            agent_turn(llm, messages, session, log)
        else:
            if not session.finalized:
                reason = "turn limit reached"
    except KeyboardInterrupt:
        print()
        reason = "interrupted (Ctrl+C)"
    except LLMDown as e:
        print(f"\nThe language model is unavailable ({e}).")
        reason = f"LLM unavailable: {e}"
    finally:
        if not session.finalized:
            fallback_close(session, reason or "chat ended without a recorded outcome", log)
        status = session.final_status or "not saved"
        print(f"{DIM}Outcome recorded: {status}. Log: {log.base.relative_to(ROOT)}.jsonl{RESET}")
        log("end", status=session.final_status, reason=reason)
        log.close(f"# Run {cid}: outcome {status}")


def fallback_close(session, reason, log):
    """Deterministic close when the LLM did not record an outcome (Ctrl+C, LLM down, it forgot)."""
    if session.identity is False:
        outcome = "needs_human"
    elif session.candidate_replies == 0:
        outcome = "no_answer"
    elif session.saved_fields:
        outcome = "in_progress"
    else:
        outcome = "no_answer"
    result = session.end_conversation(outcome, f"Chat closed by the system: {reason}.", closed_by="system")
    log("fallback_close", reason=reason, result=result)
    line = summarize_result("end_conversation", result)
    log.say("tool", f"fallback {line}")
    print(f"{DIM}  · fallback {line}{RESET}")


if __name__ == "__main__":
    main()

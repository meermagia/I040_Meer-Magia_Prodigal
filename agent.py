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
        chain = [os.environ.get("LLM_MODEL", "")] + os.environ.get("LLM_FALLBACK_MODEL", "").split(",")
        self.models = [m.strip() for m in chain if m.strip()]
        self.exhausted = set()  # models whose daily quota is used up: skip for the rest of the run
        self.log = log

    def complete(self, messages):
        last = None
        for model in self.models:
            if model in self.exhausted:
                continue
            for attempt in range(3):
                t0 = time.time()
                try:
                    r = self.client.chat.completions.create(model=model, messages=messages, tools=TOOL_SPECS)
                    self.log("llm", model=model, ms=int((time.time() - t0) * 1000))
                    return r.choices[0].message
                except Exception as e:  # 429 / 5xx / timeouts: back off, then try the next model
                    last = e
                    msg = str(e)
                    self.log("llm_error", model=model, attempt=attempt + 1, error=f"{type(e).__name__}: {msg[:600]}")
                    status = getattr(e, "status_code", None)
                    if status == 429 and ("per day" in msg.lower() or "PerDay" in msg):
                        self.exhausted.add(model)  # waiting won't help; move on now
                        break
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


# ---------------------------------------------------------------- shared conversation core
# Used by both the CLI (main below) and the web UI (web.py), so both behave identically.
class Conversation:
    def __init__(self, cid, interface, echo):
        self.cid = cid
        self.interface = interface
        self.echo = echo            # print to terminal (CLI) or stay quiet (web)
        self.log = RunLog(cid)
        self.log("start", interface=interface)
        self.api = CandidateAPI(os.environ["CANDIDATE_API_BASE_URL"], os.environ["CANDIDATE_API_KEY"],
                                timeout=float(os.environ.get("CANDIDATE_API_TIMEOUT_SECONDS", 20)), log=self.log)
        self.record = None
        self.session = None
        self.llm = None
        self.messages = []
        self.no_chat = None         # message when no conversation was needed
        self.first = None           # TurnResult of the greeting
        self.closed = False
        self.reason = None


class TurnResult:
    def __init__(self, texts, tool_events, conv, error=None):
        self.texts = texts
        self.tool_events = tool_events
        self.ended = bool(conv.session and conv.session.finalized)
        self.final_status = conv.session.final_status if conv.session else None
        self.error = error


def load_env():
    load_dotenv(ROOT / ".env")
    for var in ("CANDIDATE_API_BASE_URL", "CANDIDATE_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        if not os.environ.get(var):
            raise RuntimeError(f"Missing {var} in .env (see .env.example)")


def agent_turn(conv):
    """Let the LLM act until it produces a message for the candidate (or hits the step cap)."""
    llm, messages, session, log = conv.llm, conv.messages, conv.session, conv.log
    texts, events = [], []
    for _ in range(MAX_TOOL_STEPS):
        msg = llm.complete(messages)
        messages.append(msg.model_dump(exclude_none=True))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                result = session.call(tc.function.name, tc.function.arguments)
                log("tool", name=tc.function.name, args=tc.function.arguments, result=result)
                line = summarize_result(tc.function.name, result)
                log.say("tool", line)
                events.append({"name": tc.function.name, "args": tc.function.arguments, "result": result, "line": line})
                if conv.echo:
                    print(f"{DIM}  · {line}{RESET}")
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, default=str)})
            continue
        text = (msg.content or "").strip()
        if text:
            if conv.echo:
                print(f"\n{BOLD}Riya:{RESET} {text}\n")
            log.say("Agent", text)
            session.chat_log.append(("assistant", text))
            texts.append(text)
            session.identity_asked = True
        return texts, events
    log("warning", message="tool step cap reached")
    return texts, events


def _safe_turn(conv):
    try:
        texts, events = agent_turn(conv)
        return TurnResult(texts, events, conv)
    except LLMDown as e:
        conv.reason = f"LLM unavailable: {e}"
        if conv.echo:
            print(f"\nThe language model is unavailable ({e}).")
        finish_session(conv, conv.reason, left=False)
        return TurnResult([], [], conv, error=f"The assistant is unavailable right now ({e}). The chat was closed safely.")


def event(messages, text, log):
    log.say("event", text)
    messages.append({"role": "user", "content": f"[EVENT] {text}"})


def reconcile_complete(api, record, log):
    """No chat needed: only fix a stale status, never touch attempts or data. Returns a message."""
    status = record.get("collection_status")
    if status == "completed":
        return "Record is complete and already marked completed. Nothing to do; no chat started."
    if status == "needs_human":
        return "Record data is complete but flagged needs_human; leaving it for a human. No chat started."
    api.patch_candidate(record["id"], {"collection_status": "completed"}, record["version"])
    return f"Record is complete; status corrected {status} → completed. No chat started."


def start_session(cid, interface="cli", echo=False):
    """Fetch the candidate; either finish with no chat, or open the chat and get the greeting.
    Raises APIError if the candidate can't be fetched."""
    conv = Conversation(cid, interface, echo)
    try:
        conv.record = conv.api.get_candidate(cid)
    except APIError as e:
        conv.log("abort", error=str(e))
        conv.log.close(f"# Run {cid}: aborted ({e})")
        conv.closed = True
        raise
    missing, invalid = record_issues(conv.record)
    conv.log("plan", missing=missing, invalid=invalid, status=conv.record.get("collection_status"))
    if not missing and not invalid:
        try:
            conv.no_chat = reconcile_complete(conv.api, conv.record, conv.log)
        except APIError as e:
            conv.no_chat = f"Record is complete, but the status could not be updated: {e}"
        conv.log.close(f"# Run {cid}: complete record, no chat")
        conv.closed = True
        return conv
    tz = ZoneInfo(os.environ.get("AGENT_TIMEZONE", "Asia/Kolkata"))
    conv.session = Session(conv.api, cid, conv.record, tz, conv.log)
    conv.llm = LLM(conv.log)
    today = datetime.now(tz).strftime("%A, %d %B %Y")
    conv.messages = [{"role": "system", "content": build_system_prompt(conv.record["full_name"], today)}]
    if echo:
        print(f"{DIM}Needs attention → missing: {', '.join(missing) or '-'} | invalid: {', '.join(invalid) or '-'}{RESET}")
    event(conv.messages, "Chat opened. Send your first message to the applicant.", conv.log)
    conv.first = _safe_turn(conv)
    return conv


def run_turn(conv, user_text):
    """One candidate message → the agent's response (with any tool calls it made)."""
    conv.session.candidate_replies += 1
    conv.session.chat_log.append(("user", user_text))
    conv.log.say("Candidate", user_text)
    conv.messages.append({"role": "user", "content": user_text})
    return _safe_turn(conv)


def run_event(conv, text):
    """A chat-system event (silence, etc.) → the agent's response."""
    event(conv.messages, text, conv.log)
    return _safe_turn(conv)


def finish_session(conv, reason, left=True):
    """Close the conversation exactly once. If the candidate left, the LLM gets one step to call
    end_conversation; anything still unrecorded goes through the deterministic fallback guard."""
    if conv.closed:
        return conv.session.final_status if conv.session else None
    conv.reason = conv.reason or reason
    result = None
    if left and not conv.session.finalized:
        event(conv.messages, "The candidate left the chat (closed it). They cannot read further messages. "
                             "Call end_conversation now with the right outcome.", conv.log)
        try:
            texts, events = agent_turn(conv)
            result = TurnResult(texts, events, conv)
        except LLMDown as e:
            conv.reason = f"{reason}; LLM unavailable: {e}"
    if not conv.session.finalized:
        fallback_close(conv, conv.reason or "chat ended without a recorded outcome")
    conv.closed = True
    status = conv.session.final_status or "not saved"
    conv.log("end", status=conv.session.final_status, reason=conv.reason, interface=conv.interface)
    conv.log.close(f"# Run {conv.cid} ({conv.interface}): outcome {status}")
    if conv.echo:
        print(f"{DIM}Outcome recorded: {status}. Log: {conv.log.base.relative_to(ROOT)}.jsonl{RESET}")
    return result


def fallback_close(conv, reason):
    """Deterministic close when the LLM did not record an outcome (Ctrl+C, LLM down, it forgot)."""
    session, log = conv.session, conv.log
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
    if conv.echo:
        print(f"{DIM}  · fallback {line}{RESET}")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="ACME candidate information collection agent")
    ap.add_argument("--candidate-id", required=True)
    ap.add_argument("--timeout", type=float, default=120,
                    help="seconds of candidate silence before a nudge; a second silence ends the chat")
    args = ap.parse_args()
    try:
        load_env()
    except RuntimeError as e:
        sys.exit(str(e))
    cid = args.candidate_id.strip()

    print(f"{DIM}Fetching candidate {cid}…{RESET}")
    try:
        conv = start_session(cid, interface="cli", echo=True)
    except APIError as e:
        sys.exit({404: f"No candidate with id {cid}.", 401: "API key rejected (401)."}.get(e.status, f"API error: {e}"))
    if conv.no_chat:
        print(conv.no_chat)
        return
    print(f"{DIM}Chat started. You are the candidate. Type /quit to leave; silence for {int(args.timeout)}s "
          f"gets one nudge, then the chat closes.{RESET}")
    session = conv.session
    q = start_reader()
    try:
        silences = turns = 0
        while not session.finalized and not conv.closed and turns < MAX_TURNS:
            turns += 1
            print("You: ", end="", flush=True)
            try:
                line = q.get(timeout=args.timeout)
            except queue.Empty:
                print()
                silences += 1
                if silences == 1:
                    run_event(conv, f"No reply for {int(args.timeout)} seconds. Send one short, friendly nudge.")
                    continue
                run_event(conv, "Still no reply after the nudge. The chat is over: call end_conversation now "
                                "(no_answer if they never engaged, in_progress if they gave some info).")
                conv.reason = "candidate stopped replying"
                break
            if line is None or line.strip().lower() in ("/quit", "/exit"):
                if line is None:
                    print()
                finish_session(conv, "candidate left the chat")
                break
            if not line.strip():
                continue
            silences = 0
            run_turn(conv, line)
        else:
            if not session.finalized:
                conv.reason = "turn limit reached"
    except KeyboardInterrupt:
        print()
        conv.reason = "interrupted (Ctrl+C)"
    finally:
        finish_session(conv, conv.reason or "chat ended without a recorded outcome", left=False)


if __name__ == "__main__":
    main()

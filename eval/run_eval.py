"""Scenario evaluator: runs the REAL agent against scripted candidates and scores the DB afterwards.

    python -m eval.run_eval --list
    python -m eval.run_eval                      # dry: validate scenarios vs current records, no LLM calls
    python -m eval.run_eval --live --scenario already_complete

Live mode resets the workspace (POST /v1/reset) before each scenario and once at the end.
Only this harness resets; the agent never does.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import agent
from api import APIError, CandidateAPI
from validation import WRITABLE_DATA, Invalid, is_grounded, record_issues, validate_field

from eval.scenarios import BY_NAME, SCENARIOS

RESULTS = Path(__file__).resolve().parent / "results"
SILENCE = "<SILENCE>"
QUIT = "/quit"
SILENCE_SECONDS = 120
NUDGE = f"No reply for {SILENCE_SECONDS} seconds. Send one short, friendly nudge."
CLOSE = ("Still no reply after the nudge. The chat is over: call end_conversation now "
         "(no_answer if they never engaged, in_progress if they gave some info).")
CHECKS = ["status", "fields", "unchanged", "grounded", "attempts", "turns"]


def api_client():
    return CandidateAPI(os.environ["CANDIDATE_API_BASE_URL"], os.environ["CANDIDATE_API_KEY"],
                        timeout=float(os.environ.get("CANDIDATE_API_TIMEOUT_SECONDS", 20)))


def same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


# ---------------------------------------------------------------- dry mode
def dry_check(sc, records):
    """Return a list of problems with the scenario definition (empty = OK)."""
    problems = []
    rec = records.get(sc["candidate_id"])
    if rec is None:
        return [f"candidate {sc['candidate_id']} not found"]
    for f, v in sc["expect_fields"].items():
        if f not in WRITABLE_DATA:
            problems.append(f"{f} is not a writable data field")
            continue
        try:
            norm = validate_field(f, v)
        except Invalid as e:
            problems.append(f"expected {f}={v!r} fails validation: {e}")
            continue
        if not same(norm, v):
            problems.append(f"expected {f}={v!r} normalises to {norm!r}; use the normalised value")
    for f in sc["must_not_change"]:
        if f not in rec:
            problems.append(f"must_not_change field {f} not in record")
        if f in sc["expect_fields"]:
            problems.append(f"{f} is both expected to change and must_not_change")
    missing, invalid = record_issues(rec)
    complete = not missing and not invalid
    if sc.get("no_chat") and not complete:
        problems.append(f"no_chat scenario but record has issues: {missing + list(invalid)}")
    if not sc.get("no_chat") and complete:
        problems.append("record is already complete, so no chat will start")
    if not sc.get("no_chat") and not sc["replies"]:
        problems.append("chat scenario with no replies")
    if sc["expect_status"] == "completed" and not sc.get("no_chat"):
        uncovered = [f for f in missing + list(invalid) if f not in sc["expect_fields"]]
        if uncovered:
            problems.append(f"expects completed but no expected value for {uncovered}")
    fed = len(sc["replies"]) - sc["replies"].count(QUIT)
    if fed > sc["max_turns"]:
        problems.append(f"{fed} scripted turns exceed max_turns={sc['max_turns']}")
    return problems


def describe(sc, rec):
    missing, invalid = record_issues(rec) if rec else ([], {})
    lines = [f"- **{sc['name']}** ({sc['candidate_id']}): {sc['description']}",
             f"  record now: status={rec.get('collection_status') if rec else '?'}, "
             f"missing={missing or '-'}, invalid={list(invalid) or '-'}",
             f"  replies: {len(sc['replies'])} | expect status={sc['expect_status']}, "
             f"fields={sc['expect_fields'] or '-'} | max_turns={sc['max_turns']}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- live mode
def count_llm_calls(conv):
    path = Path(f"{conv.log.base}.jsonl")
    if not path.exists():
        return None
    n = 0
    for line in path.read_text().splitlines():
        try:
            n += json.loads(line).get("event") == "llm"
        except ValueError:
            pass
    return n


def drive(conv, sc):
    """Feed scripted replies until the session is finalized. Returns turns fed."""
    turns = silences = 0
    left = False
    for reply in sc["replies"]:
        if conv.session.finalized or conv.closed:
            break
        if reply == QUIT:
            agent.finish_session(conv, "candidate left the chat")
            left = True
            break
        turns += 1
        if reply == SILENCE:
            silences += 1
            if silences == 1:
                agent.run_event(conv, NUDGE)
            else:
                agent.run_event(conv, CLOSE)
                conv.reason = "candidate stopped replying"
                break
            continue
        silences = 0
        agent.run_turn(conv, reply)
    if not left:
        reason = conv.reason or ("scripted replies ran out" if not conv.session.finalized else "chat ended")
        agent.finish_session(conv, reason, left=not conv.session.finalized)
    return turns


def score(sc, before, after, conv, turns):
    checks, notes = {}, []
    checks["status"] = after.get("collection_status") == sc["expect_status"]
    if not checks["status"]:
        notes.append(f"status {after.get('collection_status')} != {sc['expect_status']}")
    bad = [f for f, v in sc["expect_fields"].items() if not same(after.get(f), v)]
    checks["fields"] = not bad
    if bad:
        notes.append("fields wrong: " + ", ".join(f"{f}={after.get(f)!r}" for f in bad))
    moved = [f for f in sc["must_not_change"] if not same(after.get(f), before.get(f))]
    checks["unchanged"] = not moved
    if moved:
        notes.append(f"changed but must not: {moved}")
    changed = [f for f in WRITABLE_DATA if not same(after.get(f), before.get(f))]
    ungrounded = []
    if changed:
        if conv.session is None:
            ungrounded = changed  # no chat happened, so no data write can be grounded
        else:
            texts, readbacks = conv.session._grounding_sources()
            for f in changed:
                val = after.get(f)
                if f == "skills":  # only newly added skills need grounding
                    val = [s for s in (val or []) if s not in (before.get(f) or [])]
                if val in (None, []) or not is_grounded(f, val, texts, readbacks):
                    ungrounded.append(f)
    checks["grounded"] = not ungrounded
    if ungrounded:
        notes.append(f"ungrounded writes: {ungrounded}")
    want = 0 if sc.get("no_chat") else 1
    delta = (after.get("attempt_count") or 0) - (before.get("attempt_count") or 0)
    checks["attempts"] = delta == want
    if not checks["attempts"]:
        notes.append(f"attempt_count +{delta}, expected +{want}")
    checks["turns"] = turns <= sc["max_turns"]
    if not checks["turns"]:
        notes.append(f"{turns} turns > {sc['max_turns']}")
    for s in sc.get("notes_contain", []):
        if s.lower() not in (after.get("notes") or "").lower():
            checks["status"] = False
            notes.append(f"notes lack {s!r}")
    if changed:
        notes.append(f"data changed: {changed}")
    return checks, notes


def run_live(sc, api):
    api._request("POST", "/v1/reset")
    before = api.get_candidate(sc["candidate_id"])
    t0 = time.time()
    conv = agent.start_session(sc["candidate_id"], interface="eval")
    turns = 0
    error = None
    try:
        if conv.no_chat:
            pass
        elif sc.get("no_chat"):
            error = "expected no chat, but a chat started"
        if not conv.closed:
            if conv.first and conv.first.error:
                error = conv.first.error
            turns = drive(conv, sc)
    except Exception as e:  # report, but always close the session
        error = f"{type(e).__name__}: {e}"
    finally:
        if not conv.closed:
            agent.finish_session(conv, "evaluator error", left=False)
    wall = round(time.time() - t0, 1)
    after = api.get_candidate(sc["candidate_id"])
    checks, notes = score(sc, before, after, conv, turns)
    if sc.get("no_chat") and not conv.no_chat:
        checks["status"] = False
    if conv.no_chat:
        notes.insert(0, conv.no_chat)
    if error:
        notes.insert(0, f"error: {error}")
    llm = count_llm_calls(conv)
    if sc.get("no_chat") and llm:
        checks["status"] = False
        notes.append(f"{llm} LLM calls for a no-chat record")
    return {
        "scenario": sc["name"], "candidate_id": sc["candidate_id"],
        "passed": all(checks.values()), "checks": checks, "notes": notes,
        "final_status": after.get("collection_status"), "turns": turns, "llm_calls": llm,
        "wall_seconds": wall, "run_log": str(Path(conv.log.base).name),
    }


# ---------------------------------------------------------------- report
def write_report(results, live):
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    mode = "live" if live else "dry"
    base = RESULTS / f"{stamp}_{mode}"
    lines = [f"# Eval {mode} run {stamp}", ""]
    if live:
        lines += ["| scenario | " + " | ".join(CHECKS) + " | LLM calls | turns used | wall s | result | notes |",
                  "|" + "---|" * (len(CHECKS) + 6)]
        for r in results:
            cells = ["✅" if r["checks"].get(c) else "❌" for c in CHECKS] if r.get("checks") else ["-"] * len(CHECKS)
            lines.append(f"| {r['scenario']} | " + " | ".join(cells) +
                         f" | {r.get('llm_calls')} | {r.get('turns')} | {r.get('wall_seconds')} | "
                         f"{'PASS' if r['passed'] else 'FAIL'} | {'; '.join(r['notes']).replace('|', '/')} |")
    else:
        lines += ["| scenario | candidate | definition | problems |", "|---|---|---|---|"]
        for r in results:
            lines.append(f"| {r['scenario']} | {r['candidate_id']} | {'OK' if r['passed'] else 'BAD'} | "
                         f"{'; '.join(r['notes']) or '-'} |")
    passed = sum(r["passed"] for r in results)
    lines += ["", f"**{passed}/{len(results)} passed.**", ""]
    Path(f"{base}.md").write_text("\n".join(lines))
    Path(f"{base}.json").write_text(json.dumps({"mode": mode, "stamp": stamp, "results": results}, indent=2))
    return base


def main():
    ap = argparse.ArgumentParser(description="Scenario evaluator for the candidate agent")
    ap.add_argument("--scenario", action="append", help="run only this scenario (repeatable)")
    ap.add_argument("--live", action="store_true", help="run the real agent (uses LLM quota, resets the workspace)")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args()

    if args.list:
        for sc in SCENARIOS:
            print(f"{sc['name']:24} {sc['candidate_id']}  expect={sc['expect_status']:12} {sc['description']}")
        return 0
    selected = SCENARIOS
    if args.scenario:
        unknown = [n for n in args.scenario if n not in BY_NAME]
        if unknown:
            sys.exit(f"Unknown scenario(s): {unknown}. Use --list.")
        selected = [BY_NAME[n] for n in args.scenario]

    agent.load_env()
    api = api_client()
    results = []
    if not args.live:
        body = api._request("GET", "/v1/candidates")
        records = {r["id"]: r for r in (body.get("candidates", []) if isinstance(body, dict) else body)}
        print("Dry run: checking scenario definitions against current records (no LLM calls).\n")
        for sc in selected:
            problems = dry_check(sc, records)
            print(describe(sc, records.get(sc["candidate_id"])))
            print(f"  definition: {'OK' if not problems else 'PROBLEMS: ' + '; '.join(problems)}\n")
            results.append({"scenario": sc["name"], "candidate_id": sc["candidate_id"],
                            "passed": not problems, "notes": problems})
    else:
        try:
            for sc in selected:
                print(f"▶ {sc['name']} ({sc['candidate_id']}) …", flush=True)
                try:
                    r = run_live(sc, api)
                except APIError as e:
                    r = {"scenario": sc["name"], "candidate_id": sc["candidate_id"], "passed": False,
                         "checks": {}, "notes": [f"API error: {e}"]}
                results.append(r)
                print(f"  {'PASS' if r['passed'] else 'FAIL'} {r.get('checks')} llm={r.get('llm_calls')} "
                      f"turns={r.get('turns')} {'; '.join(r['notes'])}", flush=True)
        finally:
            api._request("POST", "/v1/reset")
            print("Workspace reset.")
    base = write_report(results, args.live)
    ok = sum(r["passed"] for r in results)
    print(f"\n{ok}/{len(results)} passed. Report: {base.relative_to(Path.cwd()) if base.is_relative_to(Path.cwd()) else base}.md")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

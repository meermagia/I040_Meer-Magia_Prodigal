"""Show one candidate record the way the agent sees it: ✓ valid / ✗ missing / ⚠ invalid per
mandatory field, plus tracking fields and the latest note. Read-only (one GET).

    python scripts/show.py c005
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api import APIError, CandidateAPI  # noqa: E402
from validation import COLLECTABLE, OPTIONAL, record_issues  # noqa: E402

G, Y, R, D, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    G = Y = R = D = B = X = ""


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/show.py <candidate-id>")
    cid = sys.argv[1].strip()
    load_dotenv(ROOT / ".env")
    api = CandidateAPI(os.environ["CANDIDATE_API_BASE_URL"], os.environ["CANDIDATE_API_KEY"])
    try:
        rec = api.get_candidate(cid)
    except APIError as e:
        sys.exit(f"{cid}: {e}")
    missing, invalid = record_issues(rec)
    fields = ["full_name"] + COLLECTABLE
    ok = len(fields) - len(missing) - len(invalid)
    print(f"\n{B}{rec['id']} · {rec['full_name']}{X}   phone +**{str(rec.get('phone') or '')[-4:]} (read-only)")
    print(f"{D}{'-' * 64}{X}")
    for f in fields:
        v = rec.get(f)
        if f in missing:
            print(f"  {R}✗{X} {f:<28} {D}missing{X}")
        elif f in invalid:
            print(f"  {Y}⚠{X} {f:<28} {v!r}  {Y}{invalid[f][:60]}{X}")
        else:
            print(f"  {G}✓{X} {f:<28} {v}")
    for f in OPTIONAL:
        print(f"  {D}· {f:<28} {rec.get(f) if rec.get(f) not in (None, []) else '-'}{X}")
    print(f"{D}{'-' * 64}{X}")
    verdict = f"{G}complete{X}" if not missing and not invalid else f"{Y}{len(missing)} missing, {len(invalid)} invalid{X}"
    print(f"  mandatory: {ok}/{len(fields)} valid ({verdict})")
    print(f"  collection_status={B}{rec.get('collection_status')}{X}  attempt_count={rec.get('attempt_count')}  "
          f"version={rec.get('version')}")
    print(f"  last_contacted_at={rec.get('last_contacted_at')}")
    notes = (rec.get("notes") or "").strip().splitlines()
    print(f"  latest note: {notes[-1] if notes else '-'}\n")


if __name__ == "__main__":
    main()

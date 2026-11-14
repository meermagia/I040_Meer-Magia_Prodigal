"""Restore the candidate workspace to its original data (POST /v1/reset). Demo/test helper only;
the agent itself never resets.

    python scripts/reset.py --yes
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api import CandidateAPI  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Reset the candidate workspace to the original data")
    ap.add_argument("--yes", action="store_true", help="confirm: this wipes every change in the workspace")
    if not ap.parse_args().yes:
        sys.exit("This restores ALL candidates to the original data. Re-run with --yes to confirm.")
    load_dotenv(ROOT / ".env")
    api = CandidateAPI(os.environ["CANDIDATE_API_BASE_URL"], os.environ["CANDIDATE_API_KEY"])
    api._request("POST", "/v1/reset")
    for c in api._request("GET", "/v1/candidates")["candidates"]:
        print(f"{c['id']}  {c['full_name']:<16} {c['collection_status']:<12} v{c['version']}  attempts={c['attempt_count']}")
    print("Workspace reset.")


if __name__ == "__main__":
    main()

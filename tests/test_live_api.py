"""LIVE integration tests against the real candidate API. No LLM calls.

Skipped by default. Run deliberately with:  pytest -m live
Each test resets the workspace (POST /v1/reset) before and after; the agent itself never resets.
They drive the real CandidateAPI and the real Session.save_fields code path, with identity marked
confirmed and a transcript containing the values so the grounding check passes.
"""
import os
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api import CandidateAPI  # noqa: E402
from tools import Session  # noqa: E402

pytestmark = pytest.mark.live
load_dotenv(ROOT / ".env")


class Trace:
    def __init__(self):
        self.events = []

    def __call__(self, event, **kw):
        self.events.append({"event": event, **kw})

    def calls(self, method=None):
        return [e for e in self.events if e["event"] == "api" and (method is None or e["method"] == method)]


def client(trace=None):
    return CandidateAPI(os.environ["CANDIDATE_API_BASE_URL"], os.environ["CANDIDATE_API_KEY"],
                        timeout=float(os.environ.get("CANDIDATE_API_TIMEOUT_SECONDS", 20)),
                        log=trace or (lambda *a, **k: None))


def reset():
    client()._request("POST", "/v1/reset")


@pytest.fixture(autouse=True)
def clean_workspace():
    if not os.environ.get("CANDIDATE_API_KEY"):
        pytest.skip("no CANDIDATE_API_KEY")
    reset()
    yield
    reset()


def confirmed_session(cid, said):
    trace = Trace()
    api = client(trace)
    record = api.get_candidate(cid)
    s = Session(api, cid, record, ZoneInfo("Asia/Kolkata"), trace)
    s.identity_asked, s.candidate_replies, s.identity = True, 1, True
    s.chat_log.append(("user", said))
    trace.events.clear()  # only count calls made by save_fields
    return s, api, trace


def test_diff_only_write_increments_version_once():
    s, api, trace = confirmed_session("c001", "my email is aarav.live@example.com")
    v0 = s.record["version"]
    college = s.record["college"]
    out = s.save_fields({"email": "aarav.live@example.com", "college": college})
    patches = trace.calls("PATCH")
    assert len(patches) == 1 and patches[0]["status"] == 200
    # only the changed field (+ the pending -> in_progress status move) is sent; college is not
    assert set(patches[0]["request"]) == {"email", "collection_status"}
    assert out["saved"] == ["email"] and "college" in out["unchanged"]
    assert patches[0]["headers"]["If-Match"] == f'"{v0}"'
    assert api.get_candidate("c001")["version"] == v0 + 1


def test_real_409_keeps_other_writer_and_retries_with_new_key():
    s, api, trace = confirmed_session("c001", "my cgpa is 8.6")
    v0 = s.record["version"]
    # another writer changes a different field after our session read the record
    other = client()
    other.patch_candidate("c001", {"email": "hr.changed@example.com"}, v0)
    out = s.save_fields({"cgpa": 8.6})
    patches = trace.calls("PATCH")
    assert [p["status"] for p in patches] == [409, 200]
    assert any(c["method"] == "GET" for c in trace.calls())  # re-read after the 409
    k1, k2 = (p["headers"]["Idempotency-Key"] for p in patches)
    assert k1 != k2
    rec = api.get_candidate("c001")
    assert rec["email"] == "hr.changed@example.com" and rec["cgpa"] == 8.6
    assert out["saved"] == ["cgpa"]


def test_same_field_conflict_keeps_server_value():
    s, api, trace = confirmed_session("c004", "I study at VJTI Mumbai")
    assert s.record["college"] is None
    client().patch_candidate("c004", {"college": "IIT Bombay"}, s.record["version"])
    out = s.save_fields({"college": "VJTI Mumbai"})
    assert out["conflict"]["fields"] == ["college"]  # reported to the LLM, not overwritten
    assert "college" not in out.get("saved", [])
    assert api.get_candidate("c004")["college"] == "IIT Bombay"
    assert trace.calls("PATCH")[0]["status"] == 409


def test_idempotency_key_replays_without_applying_twice():
    api = client()
    rec = api.get_candidate("c001")
    key = str(uuid.uuid4())
    body = {"cgpa": 7.77}
    first = api.patch_candidate("c001", body, rec["version"], idempotency_key=key)
    second = api.patch_candidate("c001", body, rec["version"], idempotency_key=key)
    assert first == second
    assert api.get_candidate("c001")["version"] == rec["version"] + 1


def test_noop_save_makes_no_api_call():
    s, api, trace = confirmed_session("c003", "nothing new")
    out = s.save_fields({"college": s.record["college"], "degree": s.record["degree"]})
    assert trace.calls() == [] and set(out["unchanged"]) == {"college", "degree"}
    assert "saved" not in out

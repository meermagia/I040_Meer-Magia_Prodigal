"""MOCKED unit tests (no network, no LLM). Live evidence is in runs/."""
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api as api_mod  # noqa: E402
from api import CandidateAPI, Conflict  # noqa: E402
from tools import Session, _append_note  # noqa: E402
from validation import Invalid, record_issues, validate_field  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")


def rec(**kw):
    base = {"id": "c900", "full_name": "Test Person", "phone": "+910000000000", "email": None,
            "college": "VJTI, Mumbai", "degree": "B.Tech", "branch": "IT", "graduation_year": 2027,
            "cgpa": None, "preferred_location": "Remote", "available_from": "2027-01-04",
            "internship_duration_months": 6, "linkedin_url": None, "github_url": None, "skills": ["Go"],
            "collection_status": "pending", "attempt_count": 0, "last_contacted_at": None,
            "notes": None, "version": 1}
    base.update(kw)
    return base


class FakeAPI:
    """In-memory stand-in for the candidate API. `conflicts` = how many PATCHes return 409."""

    def __init__(self, record, conflicts=0, concurrent=None):
        self.record = dict(record)
        self.conflicts = conflicts
        self.concurrent = concurrent or {}
        self.patches = []

    def get_candidate(self, cid):
        return dict(self.record)

    def patch_candidate(self, cid, fields, version, idempotency_key=None):
        self.patches.append((dict(fields), version, idempotency_key))
        if self.conflicts:
            self.conflicts -= 1
            self.record.update(self.concurrent)  # another writer got there first
            self.record["version"] += 1
            raise Conflict(409, "version mismatch")
        if version != self.record["version"]:
            raise Conflict(409, "version mismatch")
        self.record.update(fields)
        self.record["version"] += 1
        return dict(self.record)


def session(record, **kw):
    api = FakeAPI(record, **kw)
    s = Session(api, record["id"], dict(record), TZ, lambda *a, **k: None)
    return s, api


def confirmed(record, **kw):
    s, api = session(record, **kw)
    s.identity_asked = True
    s.candidate_replies = 1
    assert s.confirm_identity(True)["identity"] is True
    return s, api


# ---------------- validation
@pytest.mark.parametrize("field,value,expected", [
    ("cgpa", "8.456", 8.46), ("degree", "btech", "B.Tech"), ("degree", "B.E", "B.E."),
    ("preferred_location", "Bangalore", "Bengaluru"), ("graduation_year", "2027", 2027),
    ("internship_duration_months", 3.0, 3), ("email", "A@Example.COM", "A@example.com"),
    ("github_url", "github.com/octocat", "https://github.com/octocat"),
])
def test_valid_values(field, value, expected):
    assert validate_field(field, value) == expected


@pytest.mark.parametrize("field,value", [
    ("cgpa", 85), ("cgpa", -1), ("cgpa", True), ("email", "kabir.shah@gmail"), ("degree", "PhD"),
    ("graduation_year", 2031), ("available_from", "2027-06"), ("available_from", "2028-01-01"),
    ("available_from", "2027-02-30"), ("internship_duration_months", 4),
    ("preferred_location", "Delhi"), ("github_url", "https://github.com/a/repo"),
    ("linkedin_url", "https://example.com/in/x"), ("college", "X"),
])
def test_invalid_values(field, value):
    with pytest.raises(Invalid):
        validate_field(field, value)


def test_existing_invalid_values_are_detected():
    missing, invalid = record_issues(rec(email="kabir.shah@gmail", graduation_year=2031))
    assert "cgpa" in missing and set(invalid) == {"email", "graduation_year"}


# ---------------- identity gate
def test_confirm_identity_requires_a_real_reply():
    s, _ = session(rec())
    s.identity_asked = True
    assert "error" in s.confirm_identity(True)


def test_save_blocked_before_identity():
    s, api = session(rec())
    assert "error" in s.save_fields({"email": "a@b.com"})
    assert api.patches == []


def test_wrong_person_forces_needs_human_and_no_data_write():
    s, api = session(rec())
    s.identity_asked, s.candidate_replies = True, 1
    s.confirm_identity(False)
    assert "error" in s.save_fields({"email": "a@b.com"})
    assert s.end_conversation("in_progress", "not them")["status"] == "needs_human"
    assert set(api.patches[0][0]) <= {"collection_status", "attempt_count", "last_contacted_at", "notes"}


def test_record_values_hidden_before_confirmation():
    s, _ = session(rec())
    assert "error" in s.get_candidate()


# ---------------- writes
def test_only_changed_valid_fields_are_sent():
    s, api = confirmed(rec())
    out = s.save_fields({"email": "t@x.com", "cgpa": 8.4, "college": "VJTI, Mumbai", "phone": "+911", "cgpa_bad": 1})
    sent = api.patches[0][0]
    assert sent == {"email": "t@x.com", "cgpa": 8.4, "collection_status": "in_progress"}
    assert "college" in out["unchanged"] and "phone" in out["rejected"]
    assert api.patches[0][1] == 1  # If-Match version


def test_invalid_value_rejected_not_written():
    s, api = confirmed(rec())
    out = s.save_fields({"cgpa": 85})
    assert "cgpa" in out["rejected"] and api.patches == []


def test_existing_valid_value_needs_confirmation_to_overwrite():
    s, api = confirmed(rec(email="old@x.com"))
    out = s.save_fields({"email": "new@x.com"})
    assert "email" in out["not_saved_existing_value"] and api.patches == []
    s.save_fields({"email": "new@x.com"}, candidate_confirmed_change=True)
    assert api.record["email"] == "new@x.com"


def test_existing_invalid_value_can_be_corrected():
    s, api = confirmed(rec(email="kabir.shah@gmail"))
    s.save_fields({"email": "kabir.shah@gmail.com"})
    assert api.record["email"] == "kabir.shah@gmail.com"


def test_skills_are_merged_not_replaced():
    s, api = confirmed(rec())
    s.save_fields({"skills": ["go", "Rust"]})
    assert api.record["skills"] == ["Go", "Rust"]


def test_409_keeps_other_writers_value_and_retries_safe_diff_with_new_key():
    s, api = confirmed(rec(), conflicts=1, concurrent={"email": "hr@x.com"})
    out = s.save_fields({"email": "me@x.com", "cgpa": 9.0})
    assert api.record["email"] == "hr@x.com"  # not clobbered
    assert api.record["cgpa"] == 9.0
    assert out["conflict"]["fields"] == ["email"]
    (first, _, k1), (second, v2, k2) = api.patches
    assert "email" not in second and v2 == 2 and k1 != k2


def test_second_409_is_reported_not_looped():
    s, api = confirmed(rec(), conflicts=5)
    out = s.save_fields({"cgpa": 9.0})
    assert "error" in out and len(api.patches) == 2


# ---------------- outcomes
def test_completed_rejected_while_fields_missing():
    s, _ = confirmed(rec())
    assert "error" in s.end_conversation("completed", "done")
    assert not s.finalized


def test_completed_when_all_valid_and_attempt_incremented_once():
    s, api = confirmed(rec(attempt_count=2))
    s.save_fields({"email": "t@x.com", "cgpa": 8.0})
    r = s.end_conversation("completed", "all done")
    assert r["status"] == "completed" and api.record["attempt_count"] == 3
    assert api.record["last_contacted_at"].endswith("+05:30")
    assert "error" in s.end_conversation("completed", "again")  # no duplicate final write
    assert api.record["attempt_count"] == 3


def test_no_reply_is_no_answer():
    s, api = session(rec())
    s.identity_asked = True
    assert s.end_conversation("in_progress", "silent", closed_by="system")["status"] == "no_answer"
    assert api.record["attempt_count"] == 1


def test_left_after_giving_data_is_in_progress():
    s, api = confirmed(rec())
    s.save_fields({"email": "t@x.com"})
    r = s.end_conversation("no_answer", "left", closed_by="system")
    assert r["status"] == "in_progress" and "cgpa" in api.record["notes"]


def test_refused_records_fields_in_notes():
    s, api = confirmed(rec())
    s.end_conversation("refused", "won't share", refused_fields=["cgpa"])
    assert api.record["collection_status"] == "refused" and "Refused: cgpa" in api.record["notes"]


def test_notes_capped_at_4000_and_keep_newest():
    out = _append_note("x" * 3990, "NEW NOTE")
    assert len(out) <= 4000 and out.endswith("NEW NOTE")


# ---------------- HTTP client
def test_network_retry_reuses_same_idempotency_key(monkeypatch):
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: None)
    seen = []

    def handler(request):
        seen.append((request.headers["Idempotency-Key"], request.headers["If-Match"], request.content))
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(seen) == 2:
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, json={"id": "c1", "version": 2})

    client = CandidateAPI("https://x", "k", transport=httpx.MockTransport(handler))
    assert client.patch_candidate("c1", {"cgpa": 8.0}, 1)["version"] == 2
    keys = {k for k, _, _ in seen}
    assert len(seen) == 3 and len(keys) == 1 and seen[0][1] == '"1"'
    assert len({body for _, _, body in seen}) == 1


def test_409_and_422_not_retried(monkeypatch):
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(409 if len(calls) == 1 else 422, json={"detail": "x"})

    client = CandidateAPI("https://x", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(Conflict):
        client.patch_candidate("c1", {"cgpa": 8.0}, 1)
    with pytest.raises(api_mod.APIError) as e:
        client.patch_candidate("c1", {"cgpa": 8.0}, 1)
    assert e.value.status == 422 and len(calls) == 2

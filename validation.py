"""Field validation for candidate records.

The API stores whatever it is sent, so every value is checked here before a PATCH.
Rules come from GET /v1/schema (snapshot in docs/schema.json).
"""
import re
from datetime import date
from urllib.parse import urlparse

MANDATORY = [
    "full_name", "phone", "email", "college", "degree", "branch", "graduation_year",
    "cgpa", "preferred_location", "available_from", "internship_duration_months",
]
# Mandatory fields the agent may collect. phone is read-only; full_name is always present.
COLLECTABLE = [f for f in MANDATORY if f not in ("phone", "full_name")]
OPTIONAL = ["linkedin_url", "github_url", "skills"]
# Data fields save_fields may write. Tracking fields are written only by end_conversation.
WRITABLE_DATA = ["full_name"] + COLLECTABLE + OPTIONAL

DEGREES = ["B.Tech", "B.E.", "B.Sc", "BCA", "MBA Tech", "MCA", "M.Tech", "Other"]
LOCATIONS = ["Mumbai", "Bengaluru", "Remote", "No preference"]
DURATIONS = [2, 3, 6]
STATUSES = ["pending", "in_progress", "completed", "no_answer", "refused", "needs_human"]

_DEGREE_ALIASES = {re.sub(r"[^a-z]", "", d.lower()): d for d in DEGREES}
_DEGREE_ALIASES.update({"bachelorofengineering": "B.E.", "bacheloroftechnology": "B.Tech",
                        "bachelorofscience": "B.Sc", "masteroftechnology": "M.Tech"})
_LOCATION_ALIASES = {
    "mumbai": "Mumbai", "bombay": "Mumbai",
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "blr": "Bengaluru",
    "remote": "Remote", "wfh": "Remote", "workfromhome": "Remote",
    "nopreference": "No preference", "anywhere": "No preference", "any": "No preference",
    "either": "No preference",
}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")


class Invalid(ValueError):
    pass


def _text(value, lo, hi, name):
    if not isinstance(value, str):
        raise Invalid(f"{name} must be text")
    v = " ".join(value.split())
    if not lo <= len(v) <= hi:
        raise Invalid(f"{name} must be {lo}-{hi} characters")
    return v


def _int(value, name):
    if isinstance(value, bool):
        raise Invalid(f"{name} must be a whole number")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise Invalid(f"{name} must be a whole number")


def _url(value, hosts, name):
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{name} must be a URL")
    v = value.strip()
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    p = urlparse(v)
    host = (p.hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise Invalid(f"{name} must be a {hosts[0]} URL")
    return v, p


def validate_field(name, value):
    """Return the normalised value, or raise Invalid with a message the LLM can act on."""
    if value is None:
        raise Invalid(f"{name} is empty")
    if name == "full_name":
        return _text(value, 2, 100, name)
    if name == "email":
        if not isinstance(value, str):
            raise Invalid("email must be text")
        v = value.strip()
        if len(v) > 254 or not EMAIL_RE.match(v):
            raise Invalid("not a valid email address (expected something like name@domain.com)")
        local, domain = v.rsplit("@", 1)
        return f"{local}@{domain.lower()}"
    if name == "college":
        return _text(value, 2, 150, name)
    if name == "branch":
        return _text(value, 2, 100, name)
    if name == "degree":
        key = re.sub(r"[^a-z]", "", str(value).lower())
        if key in _DEGREE_ALIASES:
            return _DEGREE_ALIASES[key]
        raise Invalid(f"degree must be one of {DEGREES}. If theirs is not listed, confirm with them and use 'Other'")
    if name == "graduation_year":
        v = _int(value, name)
        if not 2026 <= v <= 2028:
            raise Invalid("graduation_year must be between 2026 and 2028")
        return v
    if name == "cgpa":
        if isinstance(value, bool):
            raise Invalid("cgpa must be a number")
        try:
            v = float(str(value).strip())
        except ValueError:
            raise Invalid("cgpa must be a number on a 10-point scale")
        if not 0.0 <= v <= 10.0:
            raise Invalid("cgpa must be 0-10 on a 10-point scale. Do not convert percentages or other "
                          "scales yourself; ask the candidate for their 10-point CGPA")
        return round(v, 2)
    if name == "preferred_location":
        key = re.sub(r"[^a-z]", "", str(value).lower())
        if key in _LOCATION_ALIASES:
            return _LOCATION_ALIASES[key]
        raise Invalid(f"preferred_location must be one of {LOCATIONS}")
    if name == "available_from":
        s = str(value).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            raise Invalid("available_from must be an exact date YYYY-MM-DD (ask for the day if only a month was given)")
        try:
            d = date.fromisoformat(s)
        except ValueError:
            raise Invalid("available_from is not a real calendar date")
        if not date(2026, 1, 1) <= d <= date(2027, 12, 31):
            raise Invalid("available_from must be between 2026-01-01 and 2027-12-31")
        return s
    if name == "internship_duration_months":
        v = _int(value, name)
        if v not in DURATIONS:
            raise Invalid("internship_duration_months must be 2, 3 or 6")
        return v
    if name == "linkedin_url":
        v, _ = _url(value, ["linkedin.com"], name)
        return v
    if name == "github_url":
        v, p = _url(value, ["github.com"], name)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) != 1:
            raise Invalid("github_url must be a profile URL like https://github.com/username")
        return f"https://github.com/{parts[0]}"
    if name == "skills":
        if isinstance(value, str):
            value = [s for s in re.split(r"[,;]", value)]
        if not isinstance(value, list):
            raise Invalid("skills must be a list of strings")
        out = []
        for s in value:
            if not isinstance(s, str) or not s.strip():
                continue
            s = " ".join(s.split())
            if len(s) > 50:
                raise Invalid(f"skill '{s[:20]}...' is longer than 50 characters")
            out.append(s)
        if len(out) > 30:
            raise Invalid("at most 30 skills")
        return out
    raise Invalid(f"{name} is not a field the agent may write")


def merge_skills(existing, new):
    """Add new skills to existing ones without losing any, case-insensitive de-dup."""
    out = list(existing or [])
    seen = {s.lower() for s in out}
    for s in new:
        if s.lower() not in seen:
            out.append(s)
            seen.add(s.lower())
    if len(out) > 30:
        raise Invalid("adding these would exceed 30 skills in total")
    return out


def record_issues(record):
    """Check every mandatory field the agent can fix. Returns (missing, invalid{field: reason})."""
    missing, invalid = [], {}
    for f in COLLECTABLE + ["full_name"]:
        v = record.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f)
            continue
        try:
            norm = validate_field(f, v)
        except Invalid as e:
            invalid[f] = str(e)
            continue
        # A stored value that only validates after normalisation (e.g. 'bangalore', 8.456)
        # is still not schema-valid as stored.
        if norm != v:
            invalid[f] = f"stored value {v!r} is not in canonical form (would be {norm!r})"
    return missing, invalid


def is_complete(record):
    missing, invalid = record_issues(record)
    return not missing and not invalid


# ---------------------------------------------------------------- grounding
# A value may be written only if it was stated by the candidate, or read back to them by the
# agent and followed by a candidate reply (the LLM judges whether that reply was a "yes").
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_STRING_FIELDS = ("full_name", "college", "branch", "degree", "preferred_location", "email",
                  "linkedin_url", "github_url")
_NUMBER_FIELDS = ("cgpa", "graduation_year", "internship_duration_months")


def _norm(s, keep_dot=False):
    s = re.sub(r"[^a-z0-9@.]", "", str(s).lower())
    return s if keep_dot else s.replace(".", "")


def _words(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip() + " "


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def is_grounded(field, value, candidate_texts, confirmed_readbacks):
    """True if `value` for `field` appears in what the candidate said or confirmed."""
    raw = " ".join(list(candidate_texts) + list(confirmed_readbacks))
    if field in _STRING_FIELDS:
        v = str(value)
        if field.endswith("_url"):
            v = re.sub(r"^https?://(www\.)?", "", v)
        keep = field == "email"
        return bool(_norm(v, keep)) and _norm(v, keep) in _norm(raw, keep)
    if field in _NUMBER_FIELDS:
        x = float(value)
        tokens = {str(value), f"{x:.2f}", f"{x:.1f}"}
        if x.is_integer():
            tokens.add(str(int(x)))
        low = raw.lower()
        return any(re.search(rf"(?<![\d.]){re.escape(t)}(?![\d]|\.\d)", low) for t in tokens)
    if field == "available_from":
        d = date.fromisoformat(str(value))
        m, mon, y, o = _MONTHS[d.month - 1], _MONTHS[d.month - 1][:3], d.year, _ordinal(d.day)
        renderings = [d.isoformat(), f"{d.day} {m} {y}", f"{m} {d.day} {y}", f"{d.day} {mon} {y}",
                      f"{mon} {d.day} {y}", f"{d.day:02d}/{d.month:02d}/{y}", f"{m} {o} {y}",
                      f"{o} {m} {y}", f"{o} {mon} {y}", f"{mon} {o} {y}", f"{o} of {m} {y}"]
        hay = _words(raw)
        return any(_words(r) in hay for r in renderings)
    if field == "skills":
        return all(is_grounded("branch", s, candidate_texts, confirmed_readbacks) for s in value)
    return True

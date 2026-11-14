# ACME candidate-collection agent

An LLM chat agent that takes **one candidate ID** and works their incomplete internship
application to completion in a single terminal chat. It fetches the record, works out what is
missing **or wrong**, confirms it's talking to the right person, collects and validates the
answers, writes them back safely, and records the outcome.

## Setup and run

```bash
git clone <this repo> && cd I040_Meer-Magia_Prodigal
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your CANDIDATE_API_KEY and LLM_API_KEY in .env
python agent.py --candidate-id c001 --timeout 60
```

Python 3.9+. Tests (mocked, no network): `python -m pytest -q tests`.

### Web UI (optional)

Instead of the last command you can run `streamlit run web.py`. This is a chat page over the
**same agent core**: `agent.start_session` / `run_turn` / `finish_session` are what the CLI
calls too, so the LLM loop, tools, validation and status guard are identical. The sidebar
("Operator view") has a candidate picker, a live checklist of mandatory fields (only names and
✓/✗/⚠ until identity is confirmed; values after), status/version/attempts, and a **Candidate
leaves / End chat** button (the same path as `/quit`). Tool calls appear as coloured chips
under each agent message, with full args/results in "Agent actions". Runs are logged to `runs/`
with `"interface": "web"`. The silence timeout is CLI-only. **Closing the browser tab without
clicking "Candidate leaves" skips the end-of-session write**: the CLI's `finally` guard covers
that case, but the web UI can't detect a closed tab.

**Chatting:** the person at the keyboard plays the candidate. Type a reply and press Enter.
The chat ends when:

| How | What happens |
|---|---|
| The agent finishes | It calls `end_conversation` itself (completed, later, refused, wrong person…) |
| `/quit` (or `/exit`), or Ctrl+D (EOF) | Treated as "the candidate left". The LLM is told and records the right outcome |
| Silence for `--timeout` seconds (default 120) | One friendly nudge. A second silence closes the chat (`no_answer`, or `in_progress` if they had given data) |
| Ctrl+C, LLM outage, turn limit | Deterministic fallback close; the outcome is still written |

Each grey line `· save_fields → saved: email, cgpa | still missing: -` is a tool call the agent
just made, so you can watch its decisions live. Every run writes `runs/<time>_<id>.jsonl` (full
trace) and `.md` (transcript).

## Architecture

```
agent.py --candidate-id c005
  │
  ├─ GET /v1/candidates/c005 ──► validation.record_issues()   (missing + invalid existing values)
  │      └─ nothing to fix? → no chat. Only correct a stale status (pending → completed). Exit.
  │
  ├─ Session(cid fixed by CLI) + system prompt (contains ONLY the candidate's name)
  │
  └─ loop: candidate message ─► LLM ─┬─► text  → printed to candidate
                                     └─► tool calls → Python executes → result back to LLM → …
                                          (≤ 8 tool steps per turn)
       tools:  confirm_identity · get_candidate · save_fields · end_conversation
       exit:   end_conversation succeeded, or fallback_close() in `finally`
```

| File | Role |
|---|---|
| `agent.py` | CLI, the LLM tool loop, stdin reader thread (for silence timeouts), fallback close, run logs |
| `tools.py` | The 4 tools, bound to one `Session`. Identity gate, diff-only writes, 409 handling, status guard, notes |
| `validation.py` | Schema rules from `/v1/schema` (snapshot in `docs/schema.json`), normalisation, record issue detection |
| `api.py` | `httpx` client: quoted `If-Match`, `Idempotency-Key`, retries on timeout/429/5xx, `Conflict` on 409 |
| `prompts.py` | System prompt: role, today's date (Asia/Kolkata), conversation and outcome policy |

### Tools the LLM can call

| Tool | What the LLM decides | What Python enforces |
|---|---|---|
| `confirm_identity(is_candidate, evidence)` | Whether the reply confirms identity | Refused until the candidate has actually replied. Only then is the record (current values, missing, invalid) returned to the LLM. `false` → nothing is revealed, saves are blocked, outcome forced to `needs_human` |
| `get_candidate()` | When to refresh | Blocked before identity |
| `save_fields(fields, candidate_confirmed_change)` | What it extracted, when to save (batched), how to phrase rejections | Writable allowlist only (no phone/id/version/status). Every value validated. Only *changed* values sent. Replacing an existing **valid** value requires `candidate_confirmed_change=true`. Skills are merged, never replaced. First save moves status `pending → in_progress` |
| `end_conversation(outcome, summary, remaining_fields, refused_fields, follow_up_time)` | Which outcome, the summary, follow-up time | `completed` refused unless every mandatory field is valid. Wrong person → `needs_human`. No reply → `no_answer`. Left after giving data → `in_progress`. Increments `attempt_count` once, sets `last_contacted_at` (with +05:30 offset), appends a dated note (≤4000 chars, newest kept). Second call rejected, so no duplicate final write |

So the LLM owns the conversation: wording, order, grouping, clarifying, extraction, tool choice,
and when to close. Python owns everything that must be right in the database. No question script
exists; the prompt describes goals and rules, not a sequence.

### Where state lives

- **Database (source of truth):** field values, `collection_status`, `attempt_count`,
  `last_contacted_at`, `notes` (context for a human or the next chat).
- **`Session` (in memory, one run):** the latest record and version (taken from each PATCH
  response, which returns the full record), identity state, number of candidate replies, fields
  saved this chat, finalized flag.
- **Message history:** the LLM conversation, kept in memory only.
- **`runs/`:** the trace of each run.

## Decisions

**Deciding what's needed.** Every mandatory field is checked against the schema, not just for
null. In the seed data that finds c005's email `kabir.shah@gmail` (no TLD) and c008's
graduation year 2031 (outside 2026–2028). If nothing is missing or invalid, no chat starts. A
stale status is corrected to `completed` with no attempt counted; a `needs_human` flag is left
for the human. Invalid *optional* values alone don't trigger outreach.

**Identity.** The first message asks "am I speaking with <name>?". Until the candidate confirms,
the LLM's context holds only the name, so it can't leak the application. Wrong person or a
dispute → nothing collected, `needs_human`. Never asks for an ID. (Name confirmation is not
authentication. The phone is already OTP-verified.)

**Stops replying / leaves.** A reader thread lets the main loop time out. First silence →
one LLM-written nudge. Second → closed: `no_answer` if nothing was given, else `in_progress`
with what's left in notes. `/quit` or EOF → the LLM is told the candidate left and chooses the
outcome. Python overrides it if that contradicts the facts.

**Continue later.** `in_progress`. The suggested time is resolved against today's date (in the
prompt) and stored in notes (e.g. `Follow up: 2026-11-15 19:00`) along with the remaining fields.
The agent does not schedule anything (per the assignment).

**Refusals.** The agent explains once why a field is needed. If the candidate still declines, it
respects that, continues with the other fields, and closes `refused` with the fields in notes.
Declining optional fields is not a refusal.

**Invalid or ambiguous answers.** Python validation returns a specific reason (e.g. "cgpa must
be 0–10 on a 10-point scale; don't convert percentages") and the LLM asks again in its own
words. The agent never converts percentages or 4-point scales, never assumes a day for "June",
and asks when an answer has two options ("Mumbai or Bengaluru"). Safe aliases are normalised:
`btech → B.Tech`, `Bangalore → Bengaluru`, `8.456 → 8.46`.

**Existing data that looks wrong.** It is shown to the candidate ("the email on file looks
incomplete: …") and replaced only with their corrected value. A *valid* stored value is never
overwritten unless the candidate explicitly changes it (`candidate_confirmed_change`).

**Safe writes.** PATCH bodies contain only changed fields. Every PATCH sends `If-Match: "<version>"`
and a new `Idempotency-Key`. Network or 5xx retries reuse the same key and body, so a retry is
never applied twice. **409:** re-GET. Fields another writer changed meanwhile are kept (not
clobbered) and reported to the LLM. The rest is retried once with a new key. A second 409 is
reported, not looped. 401/403/404/422 are not retried. The agent never calls `/v1/reset`.

**Untrusted data.** Stored notes and values are passed to the LLM as data, and the prompt says so.
The tool layer means a prompt-injected LLM still cannot write invalid values, other candidates
(the ID is fixed by the CLI), tracking fields, or read-only fields.

### Assumptions (mine, not from the spec)

- One attempt = one started chat. `attempt_count` is incremented for every outcome except the
  no-chat case. (The spec only explicitly mandates it for `no_answer`.)
- `last_contacted_at` = when the chat started, in `AGENT_TIMEZONE` (default Asia/Kolkata).
- A valid record whose status is `needs_human` is left alone. A human flagged it.
- Previous notes are kept. Each chat appends one dated line, and the oldest text is trimmed past 4000 chars.
- `full_name` is writable through `save_fields` only as a correction confirmed by the candidate.

## Status (honest)

**Verified live** (real API + real LLM; see [`runs/README.md`](runs/README.md)): happy path to
`completed`; existing invalid value corrected; percentage CGPA refused and re-asked; `in_progress`
with follow-up time; `refused` with fields in notes; wrong person → `needs_human`; silence →
nudge → `no_answer`; EOF → `no_answer`; no-chat on a complete record with only the status fixed;
503s retried; quoted `If-Match` and PATCH response shape confirmed. I checked the database state
after the runs with a GET.

**Mock-tested only** (`tests/test_core.py`, 42 tests, no network): 409 conflict handling, same
Idempotency-Key on network retry, 422/409 not retried, identity gate, overwrite protection,
skills merge, status guard, notes cap, validators.

**Known limitations:**
- **LLM quota.** The Gemini key is free tier: **20 requests/day per model**, and a chat uses
  about 8–12. `gemini-3-flash-preview` (used for most sample runs) ran out during testing, so the
  default is now a chain of flash-lite models (`LLM_MODEL` + comma-separated
  `LLM_FALLBACK_MODEL`). A model that returns a daily-quota 429 is skipped instantly for the rest
  of the run. Lite models are less polished at conversation. Switching models *mid-chat* has not
  been tested (Gemini 3 "thought signatures" may not carry across models). A paid key or a
  higher-quota provider removes the problem with no code change.
- **The lite model can invent values.** In the one web-UI run
  ([`runs/20261114-152059_c007`](runs/20261114-152059_c007.md)) the candidate said "B.Tech at
  VJTI Mumbai" and `gemini-3.1-flash-lite` also saved `branch = "Computer Science"`, which the
  candidate never said. Validation can't catch a plausible-but-unstated value. Fix next: have
  `save_fields` require a short verbatim `source_quote` per field and check it against the
  candidate's actual messages, and/or use a stronger model.
- Most sample runs' candidate replies were piped in, not typed live (LLM and API side are
  real). The web-UI run was typed into the browser.
- Ctrl+C goes through the same fallback as LLM failure, but I haven't triggered it with a real
  keypress in a test.
- The 409 path is only mock-tested. I didn't force a real conflict against the live API.
- The agent asks for a 10-point CGPA but can't verify it is true, and the same goes for every
  answer. A suspicious-but-valid value (e.g. an email at a typo domain) isn't detected.
- Terminal only, one candidate per run, no streaming output.

**Next with more time:** stream LLM output; a small eval harness with an LLM-played
candidate for regression-testing conversations; a live forced-409 test; detect typo email
domains; handle "I'm her parent, she's busy" (proxy respondent) explicitly; a store per candidate
for multi-session context beyond notes; real WhatsApp transport and a scheduler for
`in_progress` follow-ups.

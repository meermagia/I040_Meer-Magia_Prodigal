# ACME Internship Application Agent

> An AI chat assistant that finishes incomplete internship applications by **talking to the
> candidate**, then saves the answers **safely and correctly** to the application database.

ACME's application form accidentally made some required fields optional, so many applications
arrived incomplete (and some contain wrong values). Every candidate gave a verified phone number,
so we can chat with them. This project is the agent that does that chat:

1. **Looks up one candidate** and works out what is missing *or wrong* in their application.
2. **Chats with them** (greets them, checks it's the right person, asks naturally, handles
   questions, typos, refusals and "can we do this later?").
3. **Validates every answer** and **writes it back** without overwriting good data.
4. **Records the outcome**: `completed`, `in_progress`, `refused`, `no_answer` or `needs_human`.

The core idea: **the AI model decides what to say and which action to take; plain Python code
decides what is allowed into the database.** So even if the model makes a mistake, invalid or
invented data can't be saved.

### What a chat looks like

This is a real run, abridged from [`runs/20261114-145810_c008.md`](runs/20261114-145810_c008.md).
Grey `·` lines are the actions the agent took:

```text
Riya:  Hi there! I'm Riya from ACME Corp. Am I speaking with Meera Joshi?
You:   yes that's me
  · confirm_identity → confirmed | missing: cgpa | invalid: graduation_year
Riya:  Your graduation year is listed as 2031, which seems off (we look for 2026–2028).
       Could you confirm it? We also need your CGPA on a 10-point scale.
You:   my cgpa is 85 percent
Riya:  Our system needs CGPA on a 10-point scale, and I can't convert percentages myself.
       What is it out of 10?
You:   oh sorry, on the 10 point scale it's 8.5
  · save_fields → saved: cgpa | still missing: -
You:   ah that's a typo, I graduate in 2027
  · save_fields → saved: graduation_year | still missing: -
  · end_conversation → status=completed
```

---

## Contents

- [Quick start](#quick-start)
- [Trying it out](#trying-it-out)
- [Repository tour](#repository-tour)
- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Status: what works, what doesn't, what's next](#status-what-works-what-doesnt-whats-next)
- [Glossary](#glossary)

---

## Quick start

**You need:** Python 3.9 or newer, an API key for the candidate database (supplied with the
assignment), and an LLM API key. The default is Google Gemini; a free key comes from
[aistudio.google.com](https://aistudio.google.com). Any OpenAI-compatible provider works.

```bash
git clone https://github.com/meermagia/I040_Meer-Magia_Prodigal.git && cd I040_Meer-Magia_Prodigal
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then open .env and paste CANDIDATE_API_KEY and LLM_API_KEY
python agent.py --candidate-id c001 --timeout 60
```

You are now the candidate: type replies and press Enter.

**Prefer a browser?** Replace the last command with `streamlit run web.py` and open
http://localhost:8501. Pick a candidate in the sidebar and press **Start conversation**. The
web page uses exactly the same agent code as the terminal.

**Settings** (all in `.env`, see [`.env.example`](.env.example)): `CANDIDATE_API_KEY`,
`LLM_API_KEY`, `LLM_MODEL` (default `gemini-3.5-flash`), `LLM_FALLBACK_MODEL` (comma-separated
backups, tried if the main model is rate-limited) and `AGENT_TIMEZONE` (default `Asia/Kolkata`).
Never commit `.env`; it is git-ignored.

---

## Trying it out

### The 8 test candidates

The database workspace holds 8 candidates. Each one tests something different:

| ID | Name | What's wrong with the record | Good for trying |
|---|---|---|---|
| `c001` | Aarav Mehta | email and CGPA missing | the happy path; give both in one message |
| `c002` | Priya Nair | graduation year, start date, duration missing | dates ("next June"), or say "wrong number" |
| `c003` | Rohan Kulkarni | nothing | **no chat starts**: the record is already complete |
| `c004` | Sneha Iyer | college, degree, branch missing | refusing to share something |
| `c005` | Kabir Shah | location missing, **email invalid** (`kabir.shah@gmail`) | fixing bad existing data, "Bangalore" |
| `c006` | Ananya Desai | nothing | no chat (like c003) |
| `c007` | Vikram Rao | everything except name and phone | a long chat, or staying silent |
| `c008` | Meera Joshi | CGPA missing, **graduation year 2031 is invalid** | "85%" as a CGPA, correcting typos |

See a record the way the agent sees it (✓ valid / ✗ missing / ⚠ invalid). This only reads:

```bash
python scripts/show.py c005
```

Put every record back to its original data (handy between tries; the agent itself never does this):

```bash
python scripts/reset.py --yes
```

### How a chat ends

| You do | What happens |
|---|---|
| Nothing special; the agent finishes | It records the outcome itself (completed, later, refused, wrong person…) |
| Type `/quit` (or press Ctrl+D) | Counts as "the candidate left". The agent records the right outcome |
| Stay silent for `--timeout` seconds | One friendly nudge; if you stay silent again, the chat closes |
| Ctrl+C, or the AI service is down | A safety net still writes a correct status |
| Web page: click **Candidate leaves** | Same as `/quit`. (Closing the browser tab does *not* save an outcome.) |

Every run is logged to `runs/`: a readable transcript (`.md`) and a full trace (`.jsonl`) of
every API call and AI action.

---

## Repository tour

```text
agent.py            ← start here: the chat loop, CLI, and shared core used by the web UI
tools.py            ← the 4 actions the AI can take, and the safety rules around them
validation.py       ← field rules (e.g. CGPA 0–10) and the "was this actually said?" check
api.py              ← talks to the candidate database (safe, versioned updates + retries)
prompts.py          ← the instructions given to the AI (goals and rules, not a script)
web.py              ← optional browser chat UI (Streamlit)
scripts/show.py     ← print one candidate record with ✓/✗/⚠
scripts/reset.py    ← restore the database workspace
tests/              ← automated tests (offline by default; `-m live` hits the real API)
eval/               ← scenario evaluator: scripted candidates, scored on what lands in the DB
runs/               ← real chat transcripts and traces (index: runs/README.md)
docs/ARCHITECTURE.md← diagram + walkthrough
docs/schema.json    ← snapshot of the database's field rules
```

---

## How it works

Full diagram: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

```text
agent.py --candidate-id c005
  ├─ fetch the record ──► find missing + invalid mandatory fields
  │     └─ nothing to fix? → no chat (only correct a stale status), exit
  ├─ greet: "Am I speaking with Kabir Shah?"   (the AI knows only the name until confirmed)
  └─ loop: candidate message ─► AI ─┬─► reply → shown to the candidate
                                    └─► action (tool call) → Python runs it → result back to the AI
     actions: confirm_identity · get_candidate · save_fields · end_conversation
     exit:    end_conversation, or the safety-net close on any other exit
```

### The four actions ("tools")

| Action | The AI decides… | The code guarantees… |
|---|---|---|
| `confirm_identity` | whether the person confirmed who they are | it only works after a real reply; the record is revealed only after confirmation; wrong person → nothing revealed, outcome `needs_human` |
| `get_candidate` | when to re-read the record | blocked before identity is confirmed |
| `save_fields` | what it understood, when to save | only allowed fields; every value validated; the value must have been **said or confirmed** by the candidate; only *changed* values sent; existing good values aren't overwritten without confirmation |
| `end_conversation` | the outcome and a summary | `completed` is refused unless every required field is valid; attempts counted once; a dated note appended; no double write |

The AI writes every message and chooses the order, grouping, clarifying questions, and when to
finish. There is no fixed question script.

### Where information lives

- **The database** (source of truth): field values, `collection_status`, `attempt_count`,
  `last_contacted_at`, and `notes` (context for a human or the next chat).
- **In memory, for one chat:** the latest record and version, whether identity is confirmed, and
  the conversation so far.
- **`runs/`:** a log of each chat.

---

## Design decisions

**Deciding what's needed.** Every required field is checked against the rules, not just for being
empty. That finds c005's email `kabir.shah@gmail` (no `.com`) and c008's graduation year 2031. If
nothing is missing or invalid, no chat starts; a stale status is corrected to `completed` with no
attempt counted. A record flagged `needs_human` is left for the human.

**Identity.** The first message asks "Am I speaking with <name>?". The agent never asks for an ID.
Until the person confirms, the AI holds only the name, so it can't leak the application. Wrong
person or a dispute → nothing collected, `needs_human`. (Confirming a name is not real
authentication; the phone number was already verified by OTP.)

**The candidate stops replying or leaves.** First silence → one nudge. Second → the chat closes:
`no_answer` if they gave nothing, `in_progress` (with what's left in notes) if they gave
something. `/quit` → the AI is told the candidate left and picks the outcome; code overrides it if
it contradicts the facts.

**"Can we continue later?"** → `in_progress`. The suggested time is worked out against today's
date and saved in notes (e.g. `Follow up: 2026-11-15 19:00`) with the remaining fields. The agent
doesn't schedule anything itself, as the assignment specifies.

**Refusals.** The agent explains once why a field is needed. If the candidate still declines, it
respects that, carries on with the rest, and closes `refused` with the field in notes. Declining
an *optional* field (LinkedIn, GitHub, skills) is fine.

**Invalid or ambiguous answers.** The code rejects with a specific reason (e.g. "CGPA must be
0–10; don't convert percentages") and the AI asks again in its own words. It never converts
percentages, never assumes a day for "June", and asks when an answer has two options. Safe
spellings are normalised: `btech → B.Tech`, `Bangalore → Bengaluru`, `8.456 → 8.46`.

**Existing data that looks wrong** is shown to the candidate ("the email on file looks
incomplete…") and replaced only with their correction. A *valid* stored value is never overwritten
unless the candidate explicitly changes it.

**Grounding check: no invented values.** `save_fields` only writes a value the candidate stated,
or confirmed after the agent read it back ("So that's 1 June 2027?" then "yes"). This is enforced
in code (`validation.is_grounded`). It came from a real bug found in testing, where the model
saved a branch the candidate never mentioned. Matching ignores case and punctuation (`BTech` =
`B.Tech`), matches numbers exactly (`8.4` ≠ `8.45`), and understands common date formats. A
question listing options ("Mumbai, Bengaluru or Remote?") doesn't count as a confirmation.

**Safe database writes.**
- Only changed fields are sent.
- Every update carries the record's version (`If-Match`). If someone else changed the record
  meanwhile (HTTP 409), the agent re-reads it, keeps their change, and retries only its own fields.
- Every update has a unique `Idempotency-Key`, so a network retry is never applied twice.
- Notes are appended, never overwritten.
- The agent never resets the database.

**Resuming from earlier chats.** After identity is confirmed, the AI sees the previous status and
notes (marked as data, not instructions), acknowledges the earlier chat and continues from there.

**Untrusted data.** Stored notes and values are treated as data. Even a manipulated AI can't write
invalid values, touch another candidate (the ID is fixed by the command line), or change
read-only or tracking fields.

### Assumptions (mine, not from the spec)

- One attempt = one started chat; `attempt_count` goes up for every outcome except "no chat needed".
- `last_contacted_at` = when the chat started, in `AGENT_TIMEZONE`.
- A complete record marked `needs_human` is left alone.
- Each chat appends one dated line to notes; past 4000 characters the oldest text is trimmed.

---

## Testing

| What | How to run | What it proves |
|---|---|---|
| **Offline tests** (55) | `python -m pytest -q` | field rules, identity gate, grounding check, diff-only writes, conflict handling, status guard, notes, retries (all with a fake API, no network) |
| **Live API tests** (5) | `python -m pytest -m live` (resets the workspace) | against the real database: diff-only writes, a real 409 conflict, same-field conflict, idempotent retry, no-op save. Output: [`runs/live_api_checks.txt`](runs/live_api_checks.txt) |
| **Scenario evaluator** (8 scenarios) | `python -m eval.run_eval --list`, then `--live --scenario <name>` | runs the real agent with a scripted candidate and scores what landed in the database. See [`eval/README.md`](eval/README.md) |
| **Real chat logs** | browse [`runs/`](runs/README.md) | transcripts + full traces of real runs for c001–c008 |

---

## Status: what works, what doesn't, what's next

**Verified with real chats** (real AI + real database, see [`runs/README.md`](runs/README.md)):
- happy path to `completed`
- an invalid existing value corrected
- a percentage CGPA refused and re-asked
- `in_progress` with a follow-up time
- `refused` with the field in notes
- wrong person → `needs_human`
- silence → nudge → `no_answer`
- leaving → `no_answer`
- no chat for a complete record
- service errors (503) retried
- the web UI end to end

**Verified against the real database without the AI:** conflict (409) recovery, idempotent
retries, diff-only writes (`pytest -m live`).

**Known limitations**
- **AI quota.** Free Gemini keys allow ~20 requests per model per day, and a chat uses ~8–12. The
  agent falls back to other models when one is exhausted. Lighter models write less naturally.
  Switching models in the middle of a chat is untested.
- **Read-back judgement.** Whether "yes" answered a read-back is judged by the AI, and the text
  match can't catch a wrong value that happens to appear elsewhere in the chat.
- **Refusing twice.** In one live check of resuming, the model asked for a previously refused field
  a second time and closed `in_progress` instead of `refused`. The prompt alone doesn't make lighter
  models follow the "ask once" rule.
- **Unverified fixes.** The "option list isn't a confirmation" fix is tested offline only. Conflict
  handling has been tested against the real database, but never inside a live AI chat.
- **Piped replies.** Most sample chats had their candidate replies piped in by a script (the AI and
  database side are real). The web-UI chats were typed by hand.
- **Not covered:** a real Ctrl+C keypress (only the same safety-net path, via other exits), and
  detecting plausible-but-false answers (a believable fake CGPA or a typo'd email domain).
- **Evaluator coverage.** Only 2 of the 8 evaluator scenarios have been run live (quota).

**Next steps with more time:** an AI-played candidate in the evaluator; a stronger model with
provider fallback; detecting typo'd email domains; handling a parent answering for the candidate;
real WhatsApp delivery with a scheduler for follow-ups; streaming replies.

---

## Glossary

| Term | Meaning |
|---|---|
| **Candidate record** | One application in the database, identified by an ID like `c001` |
| **Mandatory fields** | email, college, degree, branch, graduation year, CGPA, preferred location, start date, internship duration (plus name and phone) |
| **`collection_status`** | How the last chat ended: `pending` (never contacted), `in_progress`, `completed`, `no_answer`, `refused`, `needs_human` |
| **Tool call** | The AI asking the program to perform an action (e.g. save fields); the program runs it and replies with the result |
| **`If-Match` / version** | Every record has a version number; an update says "only apply if the version is still N", so two writers can't silently overwrite each other |
| **409 conflict** | The database's answer when the version changed meanwhile; the agent re-reads and retries safely |
| **Idempotency key** | A unique ID per update so that retrying the same request never applies it twice |
| **Grounding** | Only saving a value that the candidate actually said or confirmed |

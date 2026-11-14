# Sample runs (live)

Every file here comes from a real run of `agent.py` against the real candidate API and a real LLM
(Gemini, via its OpenAI-compatible endpoint). Each run produces two files:

- `*.md`: readable transcript (agent and candidate messages, plus one line per tool call)
- `*.jsonl`: full trace: every API request (method, path, body, `If-Match`, `Idempotency-Key`),
  status code, response body and latency; every LLM call (model, latency, errors); every tool call
  with its arguments and result; the final status.

**How the candidate side was typed:** the candidate replies in these runs were piped into
stdin (`printf '...' | python agent.py ...`) so they could run unattended. The LLM's replies,
tool choices and all API calls/responses are live and unedited. Because stdin was a pipe, the
terminal shows `You:` with the reply missing; the transcript has the actual text.

**Redaction:** API keys and the `Authorization` header are never logged. Phone numbers are masked
(`+91******1776`). Names and emails are the synthetic seed data from the assignment workspace.

| Run | Candidate | Scenario | Outcome |
|---|---|---|---|
| `20261114-145607_c001` | c001 | Happy path: email + CGPA given in one message, optional fields declined | `completed` |
| `20261114-145810_c008` | c008 | Stored graduation year 2031 is invalid; candidate gives CGPA as "85 percent" (agent refuses to convert, asks for the 10-point value) | `completed` |
| `20261114-145810_c005` | c005 | Stored email `kabir.shah@gmail` flagged; candidate asks "is this a scam?", then asks to continue tomorrow at 7 | `in_progress`, follow-up `2026-11-15 19:00` in notes |
| `20261114-145937_c004` | c004 | Gives degree ("BTech" → `B.Tech`), refuses college and branch | `refused` with refused fields in notes |
| `20261114-145810_c002` | c002 | Wrong person ("you have the wrong number"); nothing revealed or collected | `needs_human` |
| `20261114-145937_c007` | c007 | Silence with `--timeout 5`: one nudge, then closed | `no_answer`, attempt 1 |
| `20261114-145810_c006` | c006 | Record already complete: no chat, only the stale `pending` status corrected | `completed`, attempt_count unchanged |
| `20261114-150115_c007` | c007 | Confirms identity, then stdin closes (EOF = leaving). The LLM closed as `no_answer`; the follow-up LLM call hit a 429 daily quota, and the outcome was already saved | `no_answer` |
| `20261114-150502_c002` | c002 | Same wrong-person case on `gemini-3.1-flash-lite`; two 503s were retried | `needs_human` |

Runs before `150502` used `gemini-3-flash-preview`. That model's free-tier daily quota
(20 requests) ran out during testing (see the main README).

**Web UI run** (typed into the Streamlit page, `"interface": "web"` in the trace):

| Run | Candidate | Scenario | Outcome |
|---|---|---|---|
| `20261114-152059_c007` | c007 | Confirms identity, gives email + "B.Tech at VJTI Mumbai", then the operator clicks "Candidate leaves". **The model also saved `branch = Computer Science`, which the candidate never said** (see the main README's limitations) | `in_progress`, attempt 3 |

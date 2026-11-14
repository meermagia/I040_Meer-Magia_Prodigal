# Scenario evaluator

A small harness that runs the **real agent** (same `agent.py` core as the CLI and the web UI) against
scripted candidates, then scores what actually landed in the candidate database. It judges outcomes
at the DB level (status, stored values, attempt count, grounding of every write), not the wording of
the chat.

- `eval/scenarios.py`: the scenarios, as plain Python dicts.
- `eval/run_eval.py`: the runner.
- `eval/results/`: one `.md` table and one `.json` per run. They contain no secrets and no phone numbers.

## Scenarios

| name | candidate | what it exercises | expected status |
|---|---|---|---|
| `happy_multi_fact` | c001 | email and CGPA given together in one message | completed |
| `invalid_then_corrected` | c008 | CGPA "85%" is rejected, then "8.5"; the stored graduation_year 2031 (invalid) is fixed to 2027 | completed |
| `ambiguous_readback` | c005 | "Bangalore" → Bengaluru with a read-back; the invalid email `kabir.shah@gmail` is replaced | completed |
| `refuses_mandatory` | c004 | the candidate refuses college/degree/branch | refused |
| `continue_later` | c002 | gives one fact, then asks to continue "tomorrow at 6 pm" | in_progress, with the time in notes |
| `wrong_person` | c002 | someone else answers | needs_human, zero data writes |
| `silence` | c007 | two timeouts: nudge, then close | no_answer |
| `already_complete` | c003 | complete record: no chat, 0 LLM calls, status corrected to completed | completed |

In `replies`, `"<SILENCE>"` simulates a timeout. It uses the CLI's wording: the first sends a nudge
event and the second tells the agent to end the chat. `"/quit"` means the candidate leaves the chat.

## How to run

```bash
.venv/bin/python -m eval.run_eval --list
```

```bash
.venv/bin/python -m eval.run_eval
```

Dry mode (the default) makes **no LLM calls**. For every scenario it checks that the candidate
exists, that each expected value passes `validation.validate_field` and is already in normalised
form, and that the scenario fits the current record. For example, a `completed` scenario must give an
expected value for every missing or invalid field, and a no-chat scenario must point at a complete
record. It also checks that the script fits within `max_turns`. Then it prints the plan. Run it while
the workspace is reset, because it reads the records as they are now.

```bash
.venv/bin/python -m eval.run_eval --live --scenario already_complete
```

Live mode uses LLM quota: about 8–12 requests per chat scenario, against a Gemini free tier of
20 per day. Run one scenario at a time with `--scenario`, which can be repeated.
`already_complete` costs 0 LLM calls. For each scenario, live mode:

1. resets the workspace (`POST /v1/reset`) and reads the original record. Only this deliberate test
   harness resets; the agent never does.
2. runs `agent.start_session(cid, interface="eval")`, then feeds the replies through `run_turn` and
   `run_event`. It stops feeding as soon as the session is finalized, and always calls `finish_session`.
   If the script runs out before the agent records an outcome, it is treated as the candidate leaving,
   so the agent gets one step to call `end_conversation` and the code-level fallback guard covers the rest.
3. reads the final record and scores it.

A live run ends with `POST /v1/reset`, so the workspace is left clean.

## Checks

| check | passes when |
|---|---|
| status | `collection_status` equals `expect_status`. For a no-chat scenario, no chat started and 0 LLM calls were made. Any `notes_contain` substrings appear in `notes`. |
| fields | every `expect_fields` value is stored exactly (numbers are compared numerically) |
| unchanged | every `must_not_change` field still has its original value |
| grounded | every data field that changed passes `validation.is_grounded` against what the candidate said, or against agent read-backs the candidate replied to (`Session._grounding_sources()`). For skills, only newly added items are checked. Any data write with no chat counts as ungrounded. |
| attempts | `attempt_count` went up by exactly 1 (by 0 when no chat was needed) |
| turns | the candidate turns actually fed (replies plus silences) are ≤ `max_turns` |

The report also shows the LLM calls (the `"event": "llm"` lines in the run's `runs/<stamp>_<cid>.jsonl`),
the turns used, the wall time, the data fields that changed, and the run-log name for digging in.

## Limits

- **Scripted replies can't adapt.** If the agent asks something the script didn't expect (it confirms
  a value twice, or asks for optional fields), the next scripted line may answer the wrong question.
  So some failures are script mismatches, not agent bugs. Read the run's transcript in `runs/` before
  you blame the agent.
- LLM output varies between runs, so one pass or fail is a sample, not proof. Quota rules out repeat runs.
- Silence is simulated with events, not real waiting, so the timing of the CLI's input reader isn't tested.
- The checks are DB-level only. Tone, brevity and how clear the questions are aren't scored.
- **Next step:** an LLM-played candidate that has a persona and the "true" facts, so replies adapt to
  whatever the agent asks. Score it with the same DB-level checks.

## Needed core changes

None so far. The harness uses only the public core API (`load_env`, `start_session`, `run_turn`,
`run_event`, `finish_session`) and reads `Session._grounding_sources()`. If that private method is
renamed, the `grounded` check breaks, so a public alias would be cleaner.

## Results so far

| scenario | run live? | result | report |
|---|---|---|---|
| `already_complete` | yes | PASS (0 LLM calls) | [`results/20261114-154640_live.md`](results/20261114-154640_live.md) |
| `wrong_person` | yes | PASS (4 LLM calls, zero data writes) | [`results/20261114-155627_live.md`](results/20261114-155627_live.md) |
| the other 6 | not yet (LLM quota) | dry-run validated only | [`results/20261114-154624_dry.md`](results/20261114-154624_dry.md) |

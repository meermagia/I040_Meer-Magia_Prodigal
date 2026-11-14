"""Scripted candidate scenarios for the evaluator (plain dicts).

Keys:
  name            unique id, used with --scenario
  candidate_id    record to run against (the workspace is reset before each live scenario)
  replies         candidate lines in order. "<SILENCE>" = simulated timeout, "/quit" = candidate leaves
  expect_status   collection_status expected afterwards
  expect_fields   field -> stored value expected afterwards
  must_not_change fields that must keep their original value
  max_turns       upper bound on candidate turns (replies + silences actually fed)
  notes_contain   (optional) substrings expected in notes, case-insensitive
  description     what the scenario exercises
"""

SCENARIOS = [
    {
        "name": "happy_multi_fact",
        "description": "Identity confirmed, then two missing facts given in one message.",
        "candidate_id": "c001",
        "replies": [
            "Yes, this is Aarav.",
            "My email is aarav.mehta@example.com and my CGPA is 8.4",
            "Yes, that's all correct.",
            "No, nothing else. Thanks!",
        ],
        "expect_status": "completed",
        "expect_fields": {"email": "aarav.mehta@example.com", "cgpa": 8.4},
        "must_not_change": ["college", "degree", "branch", "graduation_year", "preferred_location",
                            "available_from", "internship_duration_months", "linkedin_url", "skills"],
        "max_turns": 6,
    },
    {
        "name": "invalid_then_corrected",
        "description": "CGPA given as a percentage (rejected), then corrected; the invalid stored "
                       "graduation_year 2031 is fixed to 2027.",
        "candidate_id": "c008",
        "replies": [
            "Yes, I'm Meera.",
            "My CGPA is 85%",
            "Sorry, it's 8.5 out of 10.",
            "Oh, my graduation year is actually 2027.",
            "Yes, correct.",
            "No, that's all. Thanks!",
        ],
        "expect_status": "completed",
        "expect_fields": {"cgpa": 8.5, "graduation_year": 2027},
        "must_not_change": ["email", "college", "degree", "branch", "preferred_location",
                            "available_from", "internship_duration_months"],
        "max_turns": 8,
    },
    {
        "name": "ambiguous_readback",
        "description": "'Bangalore' needs normalising/read-back to Bengaluru; invalid stored email is replaced.",
        "candidate_id": "c005",
        "replies": [
            "Yes, Kabir here.",
            "Bangalore",
            "Yes, Bengaluru.",
            "Oh right, it's kabir.shah@gmail.com",
            "Yes, that's right.",
            "No, all good. Thanks!",
        ],
        "expect_status": "completed",
        "expect_fields": {"preferred_location": "Bengaluru", "email": "kabir.shah@gmail.com"},
        "must_not_change": ["college", "degree", "branch", "graduation_year", "cgpa",
                            "available_from", "internship_duration_months"],
        "max_turns": 8,
    },
    {
        "name": "refuses_mandatory",
        "description": "Candidate refuses to share college details (mandatory) -> refused, nothing written.",
        "candidate_id": "c004",
        "replies": [
            "Yes, this is Sneha.",
            "I'd rather not share my college, degree or branch.",
            "No, I don't want to share that at all. Please don't ask again.",
            "No.",
        ],
        "expect_status": "refused",
        "expect_fields": {},
        "must_not_change": ["college", "degree", "branch", "email", "graduation_year", "cgpa",
                            "preferred_location", "available_from", "internship_duration_months"],
        "max_turns": 5,
    },
    {
        "name": "continue_later",
        "description": "Gives one fact, then asks to continue tomorrow at 6 pm -> in_progress, time in notes.",
        "candidate_id": "c002",
        "replies": [
            "Yes, I'm Priya.",
            "I graduate in 2027.",
            "I'm busy right now, can we continue tomorrow at 6 pm?",
            "Yes, tomorrow 6 pm works. Bye!",
        ],
        "expect_status": "in_progress",
        "expect_fields": {"graduation_year": 2027},
        "must_not_change": ["email", "college", "degree", "branch", "cgpa", "preferred_location"],
        "notes_contain": ["6"],
        "max_turns": 5,
    },
    {
        "name": "wrong_person",
        "description": "Someone else answers -> needs_human with zero data writes.",
        "candidate_id": "c002",
        "replies": [
            "No, this isn't Priya. You have the wrong number.",
            "I don't know any Priya, sorry.",
        ],
        "expect_status": "needs_human",
        "expect_fields": {},
        "must_not_change": ["email", "college", "degree", "branch", "graduation_year", "cgpa",
                            "preferred_location", "available_from", "internship_duration_months"],
        "max_turns": 3,
    },
    {
        "name": "silence",
        "description": "Candidate never replies: nudge, then close -> no_answer.",
        "candidate_id": "c007",
        "replies": ["<SILENCE>", "<SILENCE>"],
        "expect_status": "no_answer",
        "expect_fields": {},
        "must_not_change": ["email", "college", "degree", "branch", "graduation_year", "cgpa",
                            "preferred_location", "available_from", "internship_duration_months"],
        "max_turns": 2,
    },
    {
        "name": "already_complete",
        "description": "Complete record with status pending: no chat, 0 LLM calls, status corrected to completed, "
                       "attempt_count unchanged.",
        "candidate_id": "c003",
        "replies": [],
        "expect_status": "completed",
        "expect_fields": {},
        "must_not_change": ["email", "college", "degree", "branch", "graduation_year", "cgpa",
                            "preferred_location", "available_from", "internship_duration_months",
                            "github_url", "skills"],
        "max_turns": 0,
        "no_chat": True,
    },
]

BY_NAME = {s["name"]: s for s in SCENARIOS}

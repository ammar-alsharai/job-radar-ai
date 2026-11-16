"""Tests for job_radar/judge.py. No real AI calls: we only test the Python parts
(the verdict rule and the validation of a fake AI answer).
"""

import json

from job_radar import judge
from job_radar.judge import decide_verdict, validate_judgement


def make_checks(junior="yes", english="yes", location="yes", skills="yes"):
    return {"junior_ok": junior, "english_ok": english, "location_ok": location, "skills_ok": skills}


def fake_answer(**changes):
    """A valid AI answer as JSON text; keyword arguments change or add fields."""
    data = make_checks()
    data["reason"] = "Junior Python role in Den Haag."
    data.update(changes)
    return json.dumps(data)


# --- the verdict rule --------------------------------------------------------

def test_all_yes_is_match():
    assert decide_verdict(make_checks()) == "match"


def test_any_no_is_skip():
    assert decide_verdict(make_checks(english="no")) == "skip"
    assert decide_verdict(make_checks(skills="no")) == "skip"


def test_no_wins_over_unknown():
    assert decide_verdict(make_checks(junior="unknown", location="no")) == "skip"


def test_title_only_job_is_unclear():
    # The NN Group case: clear title and city, but no evidence for junior or English.
    checks = make_checks(junior="unknown", english="unknown", location="yes", skills="yes")
    assert decide_verdict(checks) == "unclear"


def test_yes_and_unknown_mixed_is_unclear():
    assert decide_verdict(make_checks(english="unknown")) == "unclear"
    assert decide_verdict(make_checks("unknown", "unknown", "unknown", "unknown")) == "unclear"


# --- validation of the AI answer ---------------------------------------------

def test_valid_answer_is_accepted():
    result = validate_judgement(fake_answer())
    assert result["junior_ok"] == "yes"
    assert result["reason"] == "Junior Python role in Den Haag."


def test_bad_check_value_is_rejected():
    assert validate_judgement(fake_answer(english_ok="maybe")) is None


def test_missing_check_is_rejected():
    data = json.loads(fake_answer())
    del data["skills_ok"]
    assert validate_judgement(json.dumps(data)) is None


def test_missing_reason_is_rejected():
    data = json.loads(fake_answer())
    del data["reason"]
    assert validate_judgement(json.dumps(data)) is None


def test_broken_json_is_rejected():
    assert validate_judgement("not json") is None
    assert validate_judgement(None) is None


def test_long_reason_is_cut():
    # One very long "word": no space to cut at, so it is cut at the limit.
    result = validate_judgement(fake_answer(reason="x" * 300))
    assert len(result["reason"]) == judge.MAX_REASON_CHARS + 1  # + the "…"


def test_long_reason_is_cut_at_a_word_boundary():
    reason = "Senior role that asks for many years of experience, " * 5
    result = validate_judgement(fake_answer(reason=reason))

    assert len(result["reason"]) <= judge.MAX_REASON_CHARS + 1
    assert result["reason"].endswith("…")
    shortened = result["reason"][:-1]                 # without the "…"
    assert reason.startswith(shortened)               # nothing rewritten
    assert reason[len(shortened)] in " ,"             # cut right before a space/comma


def test_short_reason_is_unchanged():
    assert judge.shorten("Good fit.", 100) == "Good fit."


# --- judge_job with a fake AI ------------------------------------------------

def test_judge_job_adds_verdict_and_sends_no_link(monkeypatch):
    sent = []

    def fake_ask_ai(user_text):
        sent.append(user_text)
        return fake_answer(location_ok="unknown")

    # monkeypatch temporarily replaces judge.ask_ai for this test only.
    monkeypatch.setattr(judge, "ask_ai", fake_ask_ai)
    job = {"id": 1, "title": "Junior Data Engineer", "company": "Eneco", "location": "Rotterdam",
           "language": "english", "seniority": "junior", "salary": "", "snippet": "",
           "link": "https://secret.example/job", "email_uid": "35", "source": "indeed.com"}

    result = judge.judge_job(job, "Target roles: data engineer")

    assert result["verdict"] == "unclear"
    assert "secret.example" not in sent[0]
    assert "indeed.com" not in sent[0]


def test_judge_job_returns_none_on_invalid_answer(monkeypatch):
    def broken_ask_ai(user_text):
        return "not json"

    monkeypatch.setattr(judge, "ask_ai", broken_ask_ai)
    assert judge.judge_job({"id": 1, "title": "x"}, "profile") is None

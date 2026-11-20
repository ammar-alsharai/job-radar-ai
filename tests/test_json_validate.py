"""Groq's intermittent HTTP 400 "json_validate_failed" must not stop the daily run.

Such a request is retried once; if it fails again, that email (or job) is
skipped and tried again next run. Any other HTTP 400 stays fatal and shows
only the error code. The error message and "failed_generation" can contain
email or job text, so they must never be printed (public Actions logs).
"""

import json
import logging
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime

import httpx2
import openai
import pytest

import main
from job_radar import config, email_reader, extractor, judge, notifier, storage

SECRET = "SECRET JOB TEXT from failed_generation"


def bad_request(code):
    """A BadRequestError like the openai package raises for Groq's HTTP 400."""
    request = httpx2.Request("POST", "https://ai.example/v1/chat/completions")
    response = httpx2.Response(400, request=request)
    body = {"message": f"Failed to generate JSON. {SECRET}", "type": "invalid_request_error",
            "code": code, "failed_generation": SECRET}
    return openai.BadRequestError(f"Error code: 400 - {body['message']}",
                                  response=response, body=body)


class FakeAI:
    """Fake extract/judge AI. `extract_errors` / `judge_errors` list, per call
    number (1, 2, ...), the error code to raise instead of answering."""

    def __init__(self):
        self.extract_calls = 0
        self.judge_calls = 0
        self.extract_errors = {}
        self.judge_errors = {}

    def extract(self, text):
        self.extract_calls += 1
        if self.extract_calls in self.extract_errors:
            raise bad_request(self.extract_errors[self.extract_calls])
        number = self.extract_calls
        return json.dumps({"listings_seen": 1, "jobs": [{
            "title": f"Job {number}", "company": "ACME", "location": "Den Haag",
            "language": "english", "seniority": "junior", "salary": "",
            "snippet": "", "posted": "", "link_code": ""}]})

    def judge(self, text):
        self.judge_calls += 1
        if self.judge_calls in self.judge_errors:
            raise bad_request(self.judge_errors[self.judge_calls])
        return json.dumps({"junior_ok": "yes", "english_ok": "yes", "location_ok": "yes",
                           "skills_ok": "yes", "reason": "Good fit."})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Two saved emails (UIDs 1 and 2), fake Gmail/AI/Telegram, temporary database."""
    monkeypatch.setattr(config, "RAW_EMAIL_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setenv("PROFILE_TEXT", "Target roles: data engineer")

    config.RAW_EMAIL_DIR.mkdir()
    for uid in ["1", "2"]:
        message = EmailMessage()
        message["From"] = "alerts@jobs.example"
        message["Subject"] = f"Alert {uid}"
        message["Date"] = format_datetime(datetime.now().astimezone())
        message.set_content(f"Jobs in email {uid}")
        (config.RAW_EMAIL_DIR / f"{uid}.eml").write_bytes(message.as_bytes())

    def fake_fetch(days_back):
        return [{"uid": uid, "from": "alerts@jobs.example", "subject": f"Alert {uid}",
                 "date": "", "is_new": True} for uid in ["1", "2"]]

    ai = FakeAI()
    sent = []

    def fake_send(text):
        sent.append(text)

    def fake_sleep(seconds):
        pass

    monkeypatch.setattr(email_reader, "fetch_recent_emails", fake_fetch)
    monkeypatch.setattr(extractor, "ask_ai", ai.extract)
    monkeypatch.setattr(judge, "ask_ai", ai.judge)
    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)

    yield ai, sent

    logging.getLogger("job_radar").setLevel(logging.NOTSET)


def run_main(monkeypatch, *options):
    monkeypatch.setattr(sys, "argv", ["main.py", "--days", "2", "--ci", *options])
    main.main()   # a sys.exit(1) would raise SystemExit


def assert_no_secret(capsys, caplog):
    output = capsys.readouterr()
    everything = output.out + output.err + caplog.text
    assert SECRET not in everything
    assert "Failed to generate JSON" not in everything
    return output.out


def test_json_validate_failed_once_is_retried(setup, monkeypatch, capsys, caplog):
    ai, sent = setup
    ai.extract_errors = {1: "json_validate_failed"}   # only the very first call fails

    run_main(monkeypatch)

    out = assert_no_secret(capsys, caplog)
    assert ai.extract_calls == 3                         # 1 retried + email 2
    assert storage.is_email_processed(config.DB_PATH, "1")
    assert storage.is_email_processed(config.DB_PATH, "2")
    assert "AI answers skipped (invalid JSON): 0" in out
    assert len(sent) == 1


def test_extraction_failing_twice_skips_that_email_only(setup, monkeypatch, capsys, caplog):
    ai, sent = setup
    ai.extract_errors = {1: "json_validate_failed", 2: "json_validate_failed"}

    run_main(monkeypatch)   # finishes normally (no SystemExit)

    out = assert_no_secret(capsys, caplog)
    assert not storage.is_email_processed(config.DB_PATH, "1")   # tried again next run
    assert storage.is_email_processed(config.DB_PATH, "2")       # the rest continued
    assert "AI answers skipped (invalid JSON): 1" in out
    assert len(sent) == 1 and "Job 3" in sent[0]                  # email 2's job was sent
    assert "json_validate_failed" in caplog.text                  # the code is logged


def test_judging_failing_twice_leaves_that_job_unjudged(setup, monkeypatch, capsys, caplog):
    ai, sent = setup
    ai.judge_errors = {1: "json_validate_failed", 2: "json_validate_failed"}

    run_main(monkeypatch)

    out = assert_no_secret(capsys, caplog)
    assert ai.judge_calls == 3                             # job 1: 2 tries, job 2: 1 try
    unjudged = storage.get_unjudged_jobs(config.DB_PATH)
    assert [job["title"] for job in unjudged] == ["Job 1"]
    assert "AI answers skipped (invalid JSON): 1" in out
    assert len(sent) == 1 and "Job 2" in sent[0] and "Job 1" not in sent[0]


def test_other_bad_request_is_fatal_and_shows_only_the_code(setup, monkeypatch, capsys, caplog):
    ai, sent = setup
    ai.extract_errors = {1: "model_not_found"}

    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch)

    out = assert_no_secret(capsys, caplog)
    assert exit_info.value.code == 1
    assert "The AI rejected the request (code: model_not_found)" in out
    assert ai.extract_calls == 1   # not retried
    assert sent == []

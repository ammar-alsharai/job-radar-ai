"""What happens when the AI rate limit is hit (after the client's own retries).

The daily run must not fail: it stops calling the AI, keeps the remaining work
for the next run, sends what it has, and exits normally (code 0).
"""

import json
import logging
import sys
from email.message import EmailMessage

import httpx2
import openai
import pytest

import main
from job_radar import config, email_reader, extractor, judge, notifier, storage


def rate_limit_error():
    """A RateLimitError like the openai package raises after HTTP 429."""
    request = httpx2.Request("POST", "https://ai.example/v1/chat/completions")
    response = httpx2.Response(429, request=request)
    return openai.RateLimitError("Rate limit reached", response=response, body=None)


class FakeAI:
    """Fake extract and judge AI. Raises RateLimitError while `limited` is True."""

    def __init__(self, jobs_per_email):
        self.jobs_per_email = jobs_per_email
        self.extract_calls = 0
        self.judge_calls = 0
        self.extract_limit_after = None   # raise after this many extract calls
        self.judge_limit_after = None     # raise after this many judge calls

    def extract(self, text):
        if self.extract_limit_after is not None and self.extract_calls >= self.extract_limit_after:
            raise rate_limit_error()
        self.extract_calls += 1
        number = self.extract_calls
        jobs = []
        for job_number in range(self.jobs_per_email):
            jobs.append({"title": f"Job {number}-{job_number}", "company": "ACME",
                         "location": "Den Haag", "language": "english", "seniority": "junior",
                         "salary": "", "snippet": "", "posted": "", "link_code": ""})
        return json.dumps({"listings_seen": len(jobs), "jobs": jobs})

    def judge(self, text):
        if self.judge_limit_after is not None and self.judge_calls >= self.judge_limit_after:
            raise rate_limit_error()
        self.judge_calls += 1
        return json.dumps({"junior_ok": "yes", "english_ok": "yes", "location_ok": "yes",
                           "skills_ok": "yes", "reason": "Good fit."})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Two saved emails with 3 jobs each, fake Gmail/AI/Telegram, temporary database."""
    monkeypatch.setattr(config, "RAW_EMAIL_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setenv("PROFILE_TEXT", "Target roles: data engineer")

    config.RAW_EMAIL_DIR.mkdir()
    for uid in ["1", "2"]:
        message = EmailMessage()
        message["From"] = "alerts@jobs.example"
        message["Subject"] = f"Alert {uid}"
        message["Date"] = "Mon, 16 Nov 2026 08:00:00 +0100"
        message.set_content(f"Jobs in email {uid}")
        (config.RAW_EMAIL_DIR / f"{uid}.eml").write_bytes(message.as_bytes())

    def fake_fetch(days_back):
        return [{"uid": uid, "from": "alerts@jobs.example", "subject": f"Alert {uid}",
                 "date": "", "is_new": True} for uid in ["1", "2"]]

    ai = FakeAI(jobs_per_email=3)
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
    """Run main.py with these options; returns normally if the exit code is 0."""
    monkeypatch.setattr(sys, "argv", ["main.py", "--days", "2", "--ci", *options])
    main.main()   # a sys.exit(1) would raise SystemExit and fail the test


# --- 1. --mark-all-sent does not judge -----------------------------------------

def test_mark_all_sent_does_not_judge(setup, monkeypatch):
    ai, sent = setup

    run_main(monkeypatch, "--mark-all-sent")

    assert ai.judge_calls == 0                                   # no AI judging at all
    assert sent == []
    assert storage.get_unnotified_jobs(config.DB_PATH) == []
    assert storage.get_unjudged_jobs(config.DB_PATH) == []       # sent jobs are never judged
    assert len(storage.get_unjudged_jobs(config.DB_PATH, include_notified=True)) == 6

    # The next normal run does not judge the old jobs either.
    run_main(monkeypatch, "--skip-if-sent-today")
    assert ai.judge_calls == 0
    assert "no new jobs today" in sent[0]


# --- 2. rate limit while judging -----------------------------------------------

def test_rate_limit_while_judging(setup, monkeypatch, capsys):
    ai, sent = setup
    ai.judge_limit_after = 2   # the 3rd judge call hits the rate limit

    run_main(monkeypatch, "--skip-if-sent-today")   # exits normally (code 0)

    output = capsys.readouterr().out
    assert "waiting (rate limit): 4" in output
    assert len(sent) == 1
    assert sent[0].startswith("⏳ 4 jobs are waiting for AI judgment (rate limit)")
    assert "Job 1-0" in sent[0] and "Job 1-1" in sent[0]          # the 2 judged jobs
    assert storage.get_meta(config.DB_PATH, "last_digest_date") is None

    # A fallback run that is still rate limited: nothing new, so nothing is sent.
    ai.judge_limit_after = ai.judge_calls
    run_main(monkeypatch, "--skip-if-sent-today")
    assert len(sent) == 1
    assert "Nothing new to send" in capsys.readouterr().out

    # Later the limit is gone: the 4 waiting jobs are judged and sent, the
    # 2 jobs that were already sent are not sent again.
    ai.judge_limit_after = None
    run_main(monkeypatch, "--skip-if-sent-today")
    assert len(sent) == 2
    assert "Job 1-0" not in sent[1]
    assert "Job 2-2" in sent[1]
    assert "⏳" not in sent[1]
    assert storage.get_meta(config.DB_PATH, "last_digest_date") is not None


# --- 3. rate limit while extracting --------------------------------------------

def test_rate_limit_while_extracting(setup, monkeypatch, capsys):
    ai, sent = setup
    ai.extract_limit_after = 1   # the 2nd email hits the rate limit

    run_main(monkeypatch, "--skip-if-sent-today")   # exits normally (code 0)

    output = capsys.readouterr().out
    assert "waiting (rate limit): 1" in output
    assert storage.is_email_processed(config.DB_PATH, "1")
    assert not storage.is_email_processed(config.DB_PATH, "2")      # kept for next run
    assert len(sent) == 1 and "Job 1-0" in sent[0]                  # first email's jobs sent
    assert storage.get_meta(config.DB_PATH, "last_digest_date") is None

    # Next run: the limit is gone, email 2 is extracted and its jobs are sent.
    ai.extract_limit_after = None
    run_main(monkeypatch, "--skip-if-sent-today")
    assert storage.is_email_processed(config.DB_PATH, "2")
    assert "Job 2-0" in sent[1]
    assert "Job 1-0" not in sent[1]

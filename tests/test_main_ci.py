"""End-to-end test of main.py in --ci mode with fake Gmail, AI and Telegram.

GitHub Actions logs of a public repo are public, so --ci must print only
counts: no senders, subjects, titles, companies, reasons or links.
"""

import json
import logging
import sys
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

import main
from job_radar import config, email_reader, extractor, judge, notifier

# Recognisable "private" strings that must never appear in --ci output.
SENDER = "Secret Recruiter <hr@private-sender.example>"
SUBJECT = "Confidential alert subject"
TITLE = "Secret Title Developer"
COMPANY = "Hidden Company"
REASON = "Private reason text"
LINK = "https://private-link.example/job/1"
PRIVATE_STRINGS = [SENDER, "private-sender.example", SUBJECT, TITLE, COMPANY, REASON,
                   LINK, "private-link.example"]


@pytest.fixture
def fake_run(tmp_path, monkeypatch):
    """Set up a temporary data folder and fake Gmail, AI and Telegram."""
    monkeypatch.setattr(config, "RAW_EMAIL_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setenv("PROFILE_TEXT", "Target roles: data engineer")

    # One saved email, as fetch_recent_emails() would have saved it.
    config.RAW_EMAIL_DIR.mkdir()
    message = EmailMessage()
    message["From"] = SENDER
    message["Subject"] = SUBJECT
    # "Now", not a fixed date, so the 3-day warning never appears here.
    message["Date"] = format_datetime(datetime.now().astimezone())
    message.set_content(f"{TITLE} at {COMPANY} in Den Haag: {LINK}")
    (config.RAW_EMAIL_DIR / "7.eml").write_bytes(message.as_bytes())

    def fake_fetch(days_back):
        return [{"uid": "7", "from": SENDER, "subject": SUBJECT, "date": "", "is_new": True}]

    calls = {"extract": 0, "judge": 0}
    sent = []

    def fake_extract_ai(text):
        calls["extract"] += 1
        return json.dumps({"listings_seen": 1, "jobs": [{
            "title": TITLE, "company": COMPANY, "location": "Den Haag",
            "language": "english", "seniority": "junior", "salary": "",
            "snippet": "", "posted": "", "link_code": "L1"}]})

    def fake_judge_ai(text):
        calls["judge"] += 1
        return json.dumps({"junior_ok": "yes", "english_ok": "yes", "location_ok": "yes",
                           "skills_ok": "yes", "reason": REASON})

    def fake_send(text):
        sent.append(text)

    def fake_sleep(seconds):
        pass

    monkeypatch.setattr(email_reader, "fetch_recent_emails", fake_fetch)
    monkeypatch.setattr(extractor, "ask_ai", fake_extract_ai)
    monkeypatch.setattr(judge, "ask_ai", fake_judge_ai)
    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)

    yield calls, sent

    # main.main() changes the "job_radar" log level; put it back for other tests.
    logging.getLogger("job_radar").setLevel(logging.NOTSET)


def run_main(monkeypatch, *options):
    monkeypatch.setattr(sys, "argv", ["main.py", *options])
    main.main()


def test_ci_output_contains_only_counts(fake_run, monkeypatch, capsys, caplog):
    calls, sent = fake_run
    caplog.set_level(logging.DEBUG)

    run_main(monkeypatch, "--days", "2", "--ci")

    output = capsys.readouterr()
    everything = output.out + output.err + caplog.text
    for private in PRIVATE_STRINGS:
        assert private not in everything

    assert "Emails read: 1 (new: 1)" in output.out
    assert "Jobs extracted: 1 (new: 1)" in output.out
    assert "match: 1" in output.out
    assert "Telegram messages sent: 1" in output.out
    # The digest itself (sent to Telegram, not printed) does contain the job.
    assert TITLE in sent[0]


def test_processed_email_is_not_extracted_again(fake_run, monkeypatch, capsys):
    calls, sent = fake_run

    run_main(monkeypatch, "--days", "2", "--ci")
    # Delete the local cache file, like on a fresh GitHub Actions runner.
    (config.EXTRACTED_DIR / "7.json").unlink()
    run_main(monkeypatch, "--days", "2", "--ci")

    assert calls["extract"] == 1   # the second run skipped the processed email
    assert "already done: 1" in capsys.readouterr().out


def test_skip_if_sent_today(fake_run, monkeypatch, capsys):
    calls, sent = fake_run

    run_main(monkeypatch, "--days", "2", "--ci", "--skip-if-sent-today")
    run_main(monkeypatch, "--days", "2", "--ci", "--skip-if-sent-today")

    assert len(sent) == 1   # only the first run sent a digest
    assert capsys.readouterr().out.strip().endswith("Digest already sent today.")


def test_mark_all_sent_sends_nothing_and_keeps_todays_digest(fake_run, monkeypatch, capsys):
    calls, sent = fake_run

    run_main(monkeypatch, "--days", "2", "--ci", "--mark-all-sent")
    assert sent == []
    assert "Marked 1 job(s) as sent without sending." in capsys.readouterr().out

    # --mark-all-sent does not set last_digest_date: today's digest still goes out.
    run_main(monkeypatch, "--days", "2", "--ci", "--skip-if-sent-today")
    assert len(sent) == 1
    assert "no new jobs today" in sent[0]

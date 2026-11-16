"""Tests for job_radar/storage.py. Run them with:  python -m pytest

pytest finds every function whose name starts with "test_" and runs it.
A test passes when all its "assert" lines are true.

tmp_path is a pytest "fixture": when a test function has a parameter with
that name, pytest gives it a fresh, empty temporary folder. So every test gets
its own database and never touches data/jobs.db.
"""

import sqlite3
from datetime import datetime

from job_radar.storage import (
    clear_verdicts,
    export_judgements,
    get_unnotified_jobs,
    mark_notified,
    restore_judgements,
    get_unjudged_jobs,
    make_dedup_key,
    normalize,
    normalize_company,
    save_jobs,
    save_verdict,
)


def make_job(email_uid, title="Data Engineer", company="Eneco", location="Rotterdam"):
    """A job dict like the extractor returns, with defaults for the tests."""
    return {
        "title": title,
        "company": company,
        "location": location,
        "language": "english",
        "seniority": "junior",
        "salary": "",
        "snippet": "",
        "posted": "",
        "link": f"https://example.com/job?from={email_uid}",
        "source": "example.com",
        "email_uid": email_uid,
    }


def count_rows(db_path, query):
    """Run a COUNT query, e.g. "SELECT COUNT(*) FROM jobs", and return the number."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query).fetchone()[0]
    finally:
        conn.close()


# --- normalize ---------------------------------------------------------------

def test_normalize_removes_accents():
    assert normalize("Café Ingénieur") == "cafe ingenieur"


def test_normalize_replaces_punctuation_with_spaces():
    assert normalize("Python/Django-Developer (m/v)") == "python django developer m v"


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize("  DATA \t  Engineer \n") == "data engineer"


def test_normalize_handles_empty_text():
    assert normalize("") == ""
    assert normalize(None) == ""


# --- company suffixes --------------------------------------------------------

def test_company_suffix_bv_is_removed():
    assert normalize_company("MA-IT B.V.") == normalize_company("MA-IT")


def test_other_company_suffixes_are_removed():
    assert normalize_company("Acme Ltd.") == "acme"
    assert normalize_company("Muster GmbH") == "muster"
    assert normalize_company("Philips N.V.") == "philips"


def test_suffix_inside_a_word_is_kept():
    # "inc" is only removed as a whole word, not inside "Incentro".
    assert normalize_company("Incentro") == "incentro"


# --- dedup key ---------------------------------------------------------------

def test_dedup_key_is_readable():
    assert make_dedup_key(make_job("1")) == "eneco|data engineer|rotterdam"


def test_job_without_company_uses_source_in_key():
    job = make_job("1", company="")
    job["source"] = "randstad.nl"
    assert make_dedup_key(job) == "|data engineer|rotterdam|randstad.nl"


def test_job_without_company_from_same_source_is_merged():
    # Same agency job in two emails, each with its own tracking link.
    job_a = make_job("1", company="")
    job_b = make_job("2", company="")
    assert job_a["link"] != job_b["link"]
    assert make_dedup_key(job_a) == make_dedup_key(job_b)


def test_jobs_without_company_from_different_sources_are_not_merged():
    job_a = make_job("1", company="")
    job_b = make_job("2", company="")
    job_b["source"] = "other-site.nl"
    assert make_dedup_key(job_a) != make_dedup_key(job_b)


# --- save_jobs ---------------------------------------------------------------

def test_same_job_from_two_emails_gives_one_job_with_two_sightings(tmp_path):
    db_path = tmp_path / "jobs.db"

    new_first = save_jobs([make_job("101")], db_path)
    new_second = save_jobs([make_job("102", company="Eneco B.V.")], db_path)

    assert len(new_first) == 1   # first time: new
    assert len(new_second) == 0  # second email: already known
    assert count_rows(db_path, "SELECT COUNT(*) FROM jobs") == 1
    assert count_rows(db_path, "SELECT COUNT(*) FROM sightings") == 2


def test_saving_the_same_email_twice_adds_nothing(tmp_path):
    db_path = tmp_path / "jobs.db"

    save_jobs([make_job("101")], db_path)
    new_again = save_jobs([make_job("101")], db_path)

    assert new_again == []
    assert count_rows(db_path, "SELECT COUNT(*) FROM jobs") == 1
    assert count_rows(db_path, "SELECT COUNT(*) FROM sightings") == 1


def test_two_locations_give_two_jobs(tmp_path):
    db_path = tmp_path / "jobs.db"

    new_jobs = save_jobs(
        [make_job("101", location="Rotterdam"), make_job("101", location="Den Haag")],
        db_path,
    )

    assert len(new_jobs) == 2
    assert count_rows(db_path, "SELECT COUNT(*) FROM jobs") == 2


def test_dates_cover_first_and_last_email(tmp_path):
    db_path = tmp_path / "jobs.db"

    # Saved out of order on purpose: the later email first.
    save_jobs([make_job("102")], db_path, seen_at=datetime(2026, 11, 15, 9, 0).astimezone())
    save_jobs([make_job("101")], db_path, seen_at=datetime(2026, 11, 13, 9, 0).astimezone())

    conn = sqlite3.connect(db_path)
    try:
        first_seen, last_seen = conn.execute("SELECT first_seen, last_seen FROM jobs").fetchone()
    finally:
        conn.close()
    assert first_seen == "2026-11-13"
    assert last_seen == "2026-11-15"


# --- verdicts (Phase 4) ------------------------------------------------------

def test_verdicts_are_saved_and_cleared(tmp_path):
    db_path = tmp_path / "jobs.db"
    save_jobs([make_job("101"), make_job("101", location="Den Haag")], db_path)

    unjudged = get_unjudged_jobs(db_path)
    assert len(unjudged) == 2
    assert "link" not in unjudged[0]  # the judge never gets the link

    result = {"junior_ok": "yes", "english_ok": "yes", "location_ok": "yes",
              "skills_ok": "yes", "reason": "Good fit.", "verdict": "match"}
    save_verdict(db_path, unjudged[0]["id"], result)
    assert len(get_unjudged_jobs(db_path)) == 1

    assert clear_verdicts(db_path) == 2
    assert len(get_unjudged_jobs(db_path)) == 2


# --- notifications and rebuild (Phase 5) --------------------------------------

def judge(db_path, job_id, verdict="match"):
    result = {"junior_ok": "yes", "english_ok": "yes", "location_ok": "yes",
              "skills_ok": "yes", "reason": "Good fit.", "verdict": verdict}
    save_verdict(db_path, job_id, result)


def test_only_judged_unnotified_jobs_are_returned(tmp_path):
    db_path = tmp_path / "jobs.db"
    save_jobs([make_job("101"), make_job("101", location="Den Haag")], db_path)
    first_id = get_unjudged_jobs(db_path)[0]["id"]
    judge(db_path, first_id)

    unnotified = get_unnotified_jobs(db_path)
    assert [job["id"] for job in unnotified] == [first_id]   # the unjudged job waits
    assert unnotified[0]["judge_checks"]["skills_ok"] == "yes"

    mark_notified(db_path, [first_id])
    assert get_unnotified_jobs(db_path) == []


def rebuild(db_path, emails):
    """Like main.rebuild_db(): export, delete, save the emails again, restore."""
    saved, notified_uids = export_judgements(db_path)
    db_path.unlink()
    for jobs in emails:
        save_jobs(jobs, db_path)
    return restore_judgements(db_path, saved, notified_uids)


def test_rebuild_keeps_verdicts_and_notified_at(tmp_path):
    db_path = tmp_path / "jobs.db"
    emails = [[make_job("101")], [make_job("102", title="Data Analyst")]]
    for jobs in emails:
        save_jobs(jobs, db_path)
    for job in get_unjudged_jobs(db_path):
        judge(db_path, job["id"], verdict="unclear")
    sent_id = get_unnotified_jobs(db_path)[0]["id"]
    mark_notified(db_path, [sent_id])

    restored, marked, new = rebuild(db_path, emails)

    assert (restored, marked, new) == (2, 0, 0)
    assert get_unjudged_jobs(db_path) == []                    # verdicts are back
    assert len(get_unnotified_jobs(db_path)) == 1              # one was sent, one not


def test_rebuild_marks_unmatched_jobs_only_from_fully_notified_emails(tmp_path):
    db_path = tmp_path / "jobs.db"
    # Email 101: one job, sent.  Email 102: two jobs, only one of them sent.
    save_jobs([make_job("101", title="Sent Job")], db_path)
    save_jobs([make_job("102", title="Also Sent"), make_job("102", title="Not Sent")], db_path)
    for job in get_unjudged_jobs(db_path):
        judge(db_path, job["id"])
    ids = {job["title"]: job["id"] for job in get_unnotified_jobs(db_path)}
    mark_notified(db_path, [ids["Sent Job"], ids["Also Sent"]])

    # New dedup rules (simulated by changed titles): no key matches any more.
    restored, marked, new = rebuild(db_path, [
        [make_job("101", title="Sent Job (new key)")],
        [make_job("102", title="Also Sent (new key)"), make_job("102", title="Not Sent (new key)")],
    ])

    assert restored == 0
    assert marked == 1   # only the job from email 101, which was fully notified
    assert new == 2      # email 102 had an unsent job, so its jobs are not marked
    conn = sqlite3.connect(db_path)
    try:
        notified_titles = [row[0] for row in conn.execute(
            "SELECT title FROM jobs WHERE notified_at IS NOT NULL")]
    finally:
        conn.close()
    assert notified_titles == ["Sent Job (new key)"]

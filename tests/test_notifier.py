"""Tests for job_radar/notifier.py. No real network calls: urlopen is replaced by a fake."""

import io
import json
import urllib.error

import pytest

from job_radar import notifier
from job_radar.notifier import (
    MAX_RAW_CHARS,
    MAX_VISIBLE_CHARS,
    NO_ALERTS_WARNING,
    NO_JOBS_TEXT,
    build_digest,
    visible_length,
)


def make_job(title="Data Engineer", verdict="match", checks=None, first_seen="2026-11-16", **extra):
    job = {
        "id": 1,
        "title": title,
        "company": "Eneco",
        "location": "Rotterdam",
        "link": "https://example.com/job/1",
        "first_seen": first_seen,
        "verdict": verdict,
        "verdict_reason": "Junior Python role.",
        "judge_checks": checks or {},
    }
    job.update(extra)
    return job


def unclear_checks(yes_count):
    """Checks with the given number of "yes" answers; the rest are "unknown"."""
    names = ["junior_ok", "english_ok", "location_ok", "skills_ok"]
    return {name: ("yes" if index < yes_count else "unknown") for index, name in enumerate(names)}


# --- HTML escaping -----------------------------------------------------------

def test_job_text_is_escaped():
    job = make_job(title="R&D <Data>", company="A & B", verdict_reason="Needs <b>Dutch</b>")
    text = build_digest([job], [], 0)[0]

    assert "R&amp;D &lt;Data&gt;" in text   # Telegram shows this as "R&D <Data>"
    assert "A &amp; B" in text
    assert "Needs &lt;b&gt;Dutch&lt;/b&gt;" in text
    assert "<Data>" not in text


def test_link_is_escaped_inside_href():
    job = make_job(link='https://example.com/job?a=1&b="2"')
    text = build_digest([job], [], 0)[0]
    assert '<a href="https://example.com/job?a=1&amp;b=&quot;2&quot;">' in text


def test_job_without_link_has_plain_title():
    text = build_digest([make_job(link="")], [], 0)[0]
    assert "<a " not in text
    assert "Data Engineer · Eneco · Rotterdam" in text


# --- sorting -----------------------------------------------------------------

def test_unclear_jobs_sorted_by_yes_count_then_newest():
    jobs = [
        make_job("One yes", "unclear", unclear_checks(1), "2026-11-16"),
        make_job("Three yes old", "unclear", unclear_checks(3), "2026-11-10"),
        make_job("Three yes new", "unclear", unclear_checks(3), "2026-11-15"),
    ]
    text = build_digest([], jobs, 0)[0]

    assert text.index("Three yes new") < text.index("Three yes old") < text.index("One yes")


# --- header, empty digest, warning ---------------------------------------------

def test_header_counts():
    text = build_digest([make_job()], [make_job(verdict="unclear")], 3)[0]
    assert text.startswith("💼 Job Radar: 5 new jobs (1 match, 1 unclear, 3 skipped)")


def test_empty_digest():
    assert build_digest([], [], 0) == [NO_JOBS_TEXT]


def test_warning_is_on_top():
    messages = build_digest([], [], 0, warning=NO_ALERTS_WARNING)
    assert len(messages) == 1
    assert messages[0].startswith(NO_ALERTS_WARNING)
    assert NO_JOBS_TEXT in messages[0]


# --- splitting ---------------------------------------------------------------

def test_visible_length_ignores_tags_and_urls():
    text = '<a href="https://example.com/very/long/tracking/url">R&amp;D</a> <b>x</b>'
    assert visible_length(text) == len("R&D x")


def test_many_jobs_are_split_without_cutting_a_job():
    jobs = []
    for number in range(60):
        jobs.append(make_job(title=f"Job number {number} " + "x" * 80, verdict="unclear",
                             link=f"https://example.com/job/{number}?" + "t" * 100))
    messages = build_digest([], jobs, 0)

    assert len(messages) > 1
    for message in messages:
        assert visible_length(message) <= MAX_VISIBLE_CHARS
        assert len(message) <= MAX_RAW_CHARS

    # Every job block appears complete in exactly one message.
    all_text = "\n\n".join(messages)
    for job in jobs:
        block = notifier.format_job(job)
        assert sum(1 for message in messages if block in message) == 1
        assert block in all_text


def test_long_tracking_urls_hit_the_raw_cap():
    # Short visible text, but 2,000-character links: the raw HTML cap splits them.
    jobs = [make_job(title=f"Job {number}", link="https://t.example/" + "u" * 2000)
            for number in range(20)]
    messages = build_digest(jobs, [], 0)

    assert len(messages) > 1
    for message in messages:
        assert len(message) <= MAX_RAW_CHARS
        assert visible_length(message) < MAX_VISIBLE_CHARS


def test_build_messages_lists_the_job_ids_per_message():
    jobs = [make_job(title=f"Job {number}", id=number, link="https://t.example/" + "u" * 2000)
            for number in range(20)]
    messages = notifier.build_messages(jobs, [], 0)

    all_ids = []
    for message in messages:
        for job_id in message["job_ids"]:
            assert f">Job {job_id}<" in message["text"]   # the job really is in this message
        all_ids += message["job_ids"]
    assert sorted(all_ids) == list(range(20))            # every job exactly once


# --- send_message without network --------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    """Replace time.sleep in notifier with a fake that only records the waits."""
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(notifier.time, "sleep", fake_sleep)
    return waits


def http_error(code, answer):
    return urllib.error.HTTPError(
        "https://api.telegram.org/bot<hidden>/sendMessage", code, "error", {},
        io.BytesIO(json.dumps(answer).encode("utf-8")),
    )


def fake_urlopen_with(answers):
    """A fake urlopen that raises or returns the prepared answers one by one."""

    def fake_urlopen(request, timeout):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fake_urlopen

class FakeResponse:
    """Pretends to be what urlopen returns."""

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_send_message_posts_json(monkeypatch):
    sent = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        return FakeResponse(b'{"ok": true, "result": {}}')

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    notifier.send_message("Hello <b>world</b>")

    body = json.loads(sent[0].data.decode("utf-8"))
    assert sent[0].get_method() == "POST"
    assert body["text"] == "Hello <b>world</b>"
    assert body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}


def test_send_error_contains_description_but_not_token(monkeypatch, no_sleep):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {},
            io.BytesIO(b'{"ok": false, "description": "Unauthorized"}'),
        )

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as error_info:
        notifier.send_message("hi")

    message = str(error_info.value)
    assert message == "Telegram error (HTTP 401): Unauthorized"
    assert "dummy-secret-token" not in message
    assert error_info.value.__cause__ is None  # the original error (with the URL) is dropped
    assert len(calls) == 1                     # 401 is not retried
    assert no_sleep == []


def test_network_error_does_not_show_token(monkeypatch, no_sleep):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no internet")

    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as error_info:
        notifier.send_message("hi")

    assert "Could not reach Telegram" in str(error_info.value)
    assert "dummy-secret-token" not in str(error_info.value)
    assert no_sleep == [2, 5, 10]   # 3 retries with increasing waits, then give up


def test_network_error_then_success_is_retried(monkeypatch, no_sleep):
    answers = [ConnectionResetError(10054, "connection forcibly closed"),
               FakeResponse(b'{"ok": true}')]
    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen_with(answers))

    notifier.send_message("hi")   # no error: the retry worked
    assert no_sleep == [2]


def test_429_uses_retry_after(monkeypatch, no_sleep):
    answers = [http_error(429, {"ok": False, "description": "Too Many Requests: retry after 7",
                                "parameters": {"retry_after": 7}}),
               FakeResponse(b'{"ok": true}')]
    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen_with(answers))

    notifier.send_message("hi")
    assert no_sleep == [7]


def test_400_is_not_retried(monkeypatch, no_sleep):
    answers = [http_error(400, {"ok": False, "description": "Bad Request: chat not found"})]
    monkeypatch.setattr(notifier.urllib.request, "urlopen", fake_urlopen_with(answers))

    with pytest.raises(RuntimeError, match="chat not found"):
        notifier.send_message("hi")
    assert no_sleep == []


# --- marking per message (main.send_digest) -----------------------------------

def test_failure_on_message_2_marks_only_jobs_of_message_1(tmp_path, monkeypatch):
    import main
    from job_radar import config, storage

    db_path = tmp_path / "jobs.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)

    # 6 match jobs and 1 skipped job, all judged.
    jobs = []
    for number in range(6):
        jobs.append({"title": f"Job {number}", "company": "ACME", "location": "Den Haag",
                     "link": f"https://example.com/{number}", "source": "example.com",
                     "email_uid": "1"})
    jobs.append({"title": "Skipped job", "company": "X", "location": "Y",
                 "link": "", "source": "example.com", "email_uid": "1"})
    storage.save_jobs(jobs, db_path)
    for job in storage.get_unjudged_jobs(db_path):
        verdict = "skip" if job["title"] == "Skipped job" else "match"
        storage.save_verdict(db_path, job["id"], {
            "junior_ok": "yes", "english_ok": "yes", "location_ok": "yes", "skills_ok": "yes",
            "reason": "Reason " + "r" * 100, "verdict": verdict})

    # A small limit, so the 6 listed jobs need several messages.
    monkeypatch.setattr(notifier, "MAX_VISIBLE_CHARS", 300)
    sent = []

    def fake_send(text):
        if len(sent) == 1:  # message 2 fails
            raise RuntimeError("Could not reach Telegram: connection forcibly closed")
        sent.append(text)

    def fake_sleep(seconds):
        pass

    monkeypatch.setattr(notifier, "send_message", fake_send)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError):
        main.send_digest()

    titles_in_message_1 = {f"Job {n}" for n in range(6) if f">Job {n}<" in sent[0]}
    still_unsent = {job["title"] for job in storage.get_unnotified_jobs(db_path)}

    assert len(sent) == 1
    assert titles_in_message_1                                   # message 1 had jobs ...
    assert titles_in_message_1.isdisjoint(still_unsent)          # ... and they are marked
    assert still_unsent == ({f"Job {n}" for n in range(6)} - titles_in_message_1) | {"Skipped job"}

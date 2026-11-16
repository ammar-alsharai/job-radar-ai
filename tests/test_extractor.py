"""Tests for the Python parts of job_radar/extractor.py (no AI calls)."""

import json
from email.message import EmailMessage

from job_radar import config, extractor
from job_radar.extractor import clean_company


def test_placeholder_companies_become_empty():
    for placeholder in ["-", "–", " n.v.t. ", "Onbekend", "UNKNOWN"]:
        assert clean_company(placeholder) == ""


def test_real_company_is_kept():
    assert clean_company("  Eneco ") == "Eneco"
    assert clean_company("Unknown Worlds") == "Unknown Worlds"  # only exact placeholders


# --- completeness check with a fake AI client -------------------------------


def make_raw_email():
    message = EmailMessage()
    message["From"] = "Jobs <alert@jobs.example>"
    message["Subject"] = "8 new jobs"
    message.set_content("Eight job listings would be here.")
    return message.as_bytes()


def make_answer(listings_seen, job_count):
    """An AI answer (JSON text) with job_count jobs and the given listings_seen."""
    jobs = []
    for number in range(1, job_count + 1):
        jobs.append({"title": f"Job {number}", "company": "ACME", "location": "Den Haag",
                     "language": "unknown", "seniority": "unknown", "salary": "",
                     "snippet": "", "posted": "", "link_code": ""})
    return json.dumps({"listings_seen": listings_seen, "jobs": jobs})


class FakeClient:
    """Pretends to be the openai client: returns the prepared answers one by one."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0
        self.chat = self          # so client.chat.completions.create(...) works
        self.completions = self

    def create(self, **request):
        answer = self.answers[self.calls]
        self.calls += 1

        class Message:
            content = answer

        class Choice:
            message = Message

        class Response:
            choices = [Choice]

        return Response


def test_short_answer_is_retried_and_complete_one_kept(tmp_path, monkeypatch):
    fake = FakeClient([make_answer(8, 5), make_answer(8, 8)])

    def fake_get_client():
        return fake

    # Use a temporary cache folder and the fake client, for this test only.
    monkeypatch.setattr(config, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(extractor, "get_client", fake_get_client)

    jobs = extractor.extract_jobs("1", make_raw_email())

    assert fake.calls == 2   # first answer was short, so one retry
    assert len(jobs) == 8    # the complete answer was kept


def test_shorter_retry_does_not_replace_first_answer(tmp_path, monkeypatch):
    fake = FakeClient([make_answer(8, 6), make_answer(8, 4)])

    def fake_get_client():
        return fake

    monkeypatch.setattr(config, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(extractor, "get_client", fake_get_client)

    jobs = extractor.extract_jobs("1", make_raw_email())

    assert fake.calls == 2
    assert len(jobs) == 6    # still short, but the better of the two answers


def test_complete_answer_is_not_retried(tmp_path, monkeypatch):
    fake = FakeClient([make_answer(3, 3)])

    def fake_get_client():
        return fake

    monkeypatch.setattr(config, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(extractor, "get_client", fake_get_client)

    assert len(extractor.extract_jobs("1", make_raw_email())) == 3
    assert fake.calls == 1


def test_missing_listings_seen_is_invalid():
    assert extractor.validate_jobs({"jobs": []}) is None
    assert extractor.validate_jobs({"listings_seen": True, "jobs": []}) is None
    assert extractor.validate_jobs({"listings_seen": 0, "jobs": []}) == []

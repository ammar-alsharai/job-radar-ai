"""Tests for the Python parts of job_radar/extractor.py (no AI calls)."""

from job_radar.extractor import clean_company


def test_placeholder_companies_become_empty():
    for placeholder in ["-", "–", " n.v.t. ", "Onbekend", "UNKNOWN"]:
        assert clean_company(placeholder) == ""


def test_real_company_is_kept():
    assert clean_company("  Eneco ") == "Eneco"
    assert clean_company("Unknown Worlds") == "Unknown Worlds"  # only exact placeholders

"""Load settings and secrets from the .env file and check that they are present.

The email settings are checked as soon as this module is imported, because
every command needs them. The AI settings are only checked when an AI call
is really about to happen (see require_ai_settings()), so commands that do
not use the AI keep working without AI keys.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# The project root is one folder above this file (job_radar/ -> project root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Read .env into the environment. Variables that are already set (for example
# GitHub Secrets later on) are NOT overwritten.
load_dotenv(PROJECT_ROOT / ".env")


def _require(key):
    """Return the value of a required setting, or stop with a clear error.

    The error message names the KEY only, never its value.
    """
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"Missing setting: {key} is not set or empty in .env")
    return value


# Settings for reading emails (Phase 1)
JOB_EMAIL_ADDRESS = _require("JOB_EMAIL_ADDRESS")
JOB_EMAIL_APP_PASSWORD = _require("JOB_EMAIL_APP_PASSWORD")
IMAP_HOST = os.getenv("IMAP_HOST", "").strip() or "imap.gmail.com"

def _optional(key):
    """Return the value of an optional setting, or "" if it is not set."""
    return os.getenv(key, "").strip()


# Optional: name variants to remove from email text, e.g. "First Last, First".
# Split on commas into a list and drop empty pieces.
MY_NAMES = [name.strip() for name in _optional("MY_NAME").split(",") if name.strip()]

# AI settings (Phase 2). These may be empty here; require_ai_settings() checks them.
AI_API_KEY = _optional("AI_API_KEY")
AI_BASE_URL = _optional("AI_BASE_URL")
AI_MODEL = _optional("AI_MODEL")
AI_REASONING_EFFORT = _optional("AI_REASONING_EFFORT")  # optional, "" = don't send


def require_ai_settings():
    """Stop with a clear error if a required AI setting is missing.

    Call this right before using the AI. The error names the KEY only.
    """
    for key in ["AI_API_KEY", "AI_BASE_URL", "AI_MODEL"]:
        if not _optional(key):
            raise RuntimeError(f"Missing setting: {key} is not set or empty in .env")


# Where data is saved (the data/ folder is gitignored)
RAW_EMAIL_DIR = PROJECT_ROOT / "data" / "raw"
EXTRACTED_DIR = PROJECT_ROOT / "data" / "extracted"
DB_PATH = PROJECT_ROOT / "data" / "jobs.db"

"""Load settings and secrets from the .env file and check that they are present.

Only the keys Phase 1 needs are checked here. Later phases will add their own.
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

# Where raw emails are saved (this folder is gitignored)
RAW_EMAIL_DIR = PROJECT_ROOT / "data" / "raw"

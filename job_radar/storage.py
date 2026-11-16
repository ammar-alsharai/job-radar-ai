"""Store jobs in SQLite and recognise duplicates (the same job from several emails or sites).

Database concepts used here
---------------------------
- Table: like a spreadsheet tab with fixed columns. We have two:
  "jobs" (one row per unique job) and "sightings" (one row per email a job
  appeared in). One job can have many sightings.
- Primary key (id INTEGER PRIMARY KEY): a unique number for each row that
  SQLite fills in by itself.
- Foreign key (sightings.job_id -> jobs.id): a column that points to a row in
  another table. With "PRAGMA foreign_keys = ON", SQLite refuses a sighting
  for a job that does not exist.
- UNIQUE: SQLite refuses a second row with the same value(s). We use it for
  dedup_key (one row per job) and for (job_id, email_uid) (one sighting per
  job per email).
- Upsert: "insert, or update if it already exists".
- Transaction: a group of changes that is saved all together or not at all.
  "with conn:" starts one; it commits at the end, or rolls everything back if
  an error happens, so a crash never leaves half-written data.

Parameterised queries
---------------------
We always write values as "?" in the SQL and pass them separately:
    conn.execute("SELECT id FROM jobs WHERE dedup_key = ?", (key,))
Never with an f-string like f"... WHERE dedup_key = '{key}'". Job titles come
from emails, and a title containing a quote (') would break the SQL or, in
the worst case, change what it does ("SQL injection"). With "?", SQLite
always treats the value as plain data, never as SQL.
"""

import re
import sqlite3
import unicodedata
from datetime import date, datetime

# Legal company suffixes, removed as whole words (after normalize(), so
# "B.V." has already become "b v").
COMPANY_SUFFIXES = ["b v", "n v", "bv", "nv", "ltd", "gmbh", "inc"]

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT UNIQUE NOT NULL,
    title TEXT,
    company TEXT,
    location TEXT,
    language TEXT,
    seniority TEXT,
    salary TEXT,
    snippet TEXT,
    posted TEXT,
    link TEXT,               -- from the first sighting
    first_seen TEXT NOT NULL, -- ISO date, e.g. 2026-11-16
    last_seen TEXT NOT NULL,
    verdict TEXT,            -- Phase 4
    verdict_reason TEXT,     -- Phase 4
    judged_at TEXT,          -- Phase 4
    notified_at TEXT         -- Phase 5
)
"""

CREATE_SIGHTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    email_uid TEXT NOT NULL,
    source TEXT,
    link TEXT,
    seen_at TEXT NOT NULL,   -- ISO date and time of the email
    FOREIGN KEY (job_id) REFERENCES jobs (id),
    UNIQUE (job_id, email_uid)
)
"""


def connect(db_path):
    """Open the database (created if missing) and make sure the tables exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite has this off by default
    with conn:
        conn.execute(CREATE_JOBS_TABLE)
        conn.execute(CREATE_SIGHTINGS_TABLE)
    return conn


def normalize(text):
    """Make text comparable: lowercase, no accents, no punctuation, single spaces.

    Example: "  Café-Medewerker (Den Haag) " -> "cafe medewerker den haag"
    """
    # NFKD splits "é" into "e" + a separate accent mark, so we can drop the mark.
    decomposed = unicodedata.normalize("NFKD", text or "")
    chars = []
    for char in decomposed:
        if unicodedata.combining(char):
            continue  # an accent mark: skip it
        if char.isalnum():
            chars.append(char.lower())
        else:
            chars.append(" ")  # punctuation and other symbols become spaces
    return " ".join("".join(chars).split())


def normalize_company(company):
    """normalize() plus removing legal suffixes: "MA-IT B.V." -> "ma it"."""
    text = normalize(company)
    for suffix in COMPANY_SUFFIXES:
        text = re.sub(r"\b" + re.escape(suffix) + r"\b", " ", text)
    return " ".join(text.split())


def make_dedup_key(job):
    """Build a readable key like "eneco|data engineer|rotterdam".

    If the company is unknown, the link (or the email UID) is added, so jobs
    without a company are never merged with each other.
    """
    company = normalize_company(job.get("company", ""))
    title = normalize(job.get("title", ""))
    location = normalize(job.get("location", ""))
    key = f"{company}|{title}|{location}"
    if not company:
        key += "|" + (job.get("link") or f"email {job.get('email_uid', '')}")
    return key


def save_jobs(jobs, db_path, seen_at=None):
    """Save the jobs from ONE email. Return the jobs that are new to the database.

    seen_at: when the email arrived (a datetime). Defaults to now.
    Saving the same email twice changes nothing.
    """
    if seen_at is None:
        seen_at = datetime.now().astimezone()
    seen_date = seen_at.date().isoformat()          # e.g. "2026-11-16"
    seen_time = seen_at.isoformat(timespec="seconds")  # e.g. "2026-11-16T08:12:03+01:00"

    new_jobs = []
    conn = connect(db_path)
    try:
        with conn:  # one transaction for the whole email
            for job in jobs:
                key = make_dedup_key(job)
                row = conn.execute("SELECT id FROM jobs WHERE dedup_key = ?", (key,)).fetchone()

                if row is None:
                    # Unknown job: insert it.
                    cursor = conn.execute(
                        """INSERT INTO jobs (dedup_key, title, company, location, language,
                               seniority, salary, snippet, posted, link, first_seen, last_seen)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (key, job.get("title", ""), job.get("company", ""),
                         job.get("location", ""), job.get("language", ""),
                         job.get("seniority", ""), job.get("salary", ""),
                         job.get("snippet", ""), job.get("posted", ""),
                         job.get("link", ""), seen_date, seen_date),
                    )
                    job_id = cursor.lastrowid  # the id SQLite just gave the new row
                    new_jobs.append(job)
                else:
                    # Known job: only widen the date range. MIN()/MAX() with two
                    # values pick the smaller/larger one (ISO dates sort correctly
                    # as text), so the order in which emails are saved does not matter.
                    job_id = row[0]
                    conn.execute(
                        """UPDATE jobs SET first_seen = MIN(first_seen, ?),
                                           last_seen = MAX(last_seen, ?)
                           WHERE id = ?""",
                        (seen_date, seen_date, job_id),
                    )

                # One sighting per job per email. "OR IGNORE" skips it silently
                # if UNIQUE(job_id, email_uid) says we already have it.
                conn.execute(
                    """INSERT OR IGNORE INTO sightings (job_id, email_uid, source, link, seen_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (job_id, job.get("email_uid", ""), job.get("source", ""),
                     job.get("link", ""), seen_time),
                )
    finally:
        conn.close()  # "with conn" commits, but does not close the connection
    return new_jobs


def get_stats(db_path):
    """Return a dict with total jobs, jobs per source and jobs first seen today."""
    conn = connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        # A job can come from several sites, so count it once per source.
        per_source = conn.execute(
            """SELECT source, COUNT(DISTINCT job_id) FROM sightings
               GROUP BY source ORDER BY COUNT(DISTINCT job_id) DESC"""
        ).fetchall()
        today = date.today().isoformat()
        first_seen_today = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE first_seen = ?", (today,)
        ).fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "per_source": per_source, "first_seen_today": first_seen_today}

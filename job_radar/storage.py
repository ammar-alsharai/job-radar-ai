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

import json
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
    judge_checks TEXT,       -- Phase 4: the four checks as JSON
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

    If the company is unknown (agencies like Randstad often leave it out),
    the source is used instead: "|data engineer|rotterdam|randstad.nl". So the
    same agency job from several emails is merged, but it is never merged with
    a job from another site that also has no company. (Not the link: agencies
    use a different tracking link in every email.)
    """
    company = normalize_company(job.get("company", ""))
    title = normalize(job.get("title", ""))
    location = normalize(job.get("location", ""))
    key = f"{company}|{title}|{location}"
    if not company:
        key += "|" + (job.get("source") or "").lower()
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


def get_unjudged_jobs(db_path):
    """Return every job without a verdict, as a list of dicts (oldest first)."""
    conn = connect(db_path)
    # sqlite3.Row lets us read columns by name (row["title"]) instead of by
    # position (row[1]), and dict(row) turns a row into a normal dict.
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, title, company, location, language, seniority, salary, snippet
               FROM jobs WHERE verdict IS NULL ORDER BY id"""
        ).fetchall()
    finally:
        conn.close()
    # "IS NULL" (not "= NULL"): in SQL, NULL means "no value", and nothing is
    # ever equal to "no value", so "= NULL" would never match.
    return [dict(row) for row in rows]


def save_verdict(db_path, job_id, result):
    """Store the judge's result for one job.

    result: dict with the four checks, "reason" and "verdict" (from judge.judge_job).
    """
    checks = {
        "junior_ok": result["junior_ok"],
        "english_ok": result["english_ok"],
        "location_ok": result["location_ok"],
        "skills_ok": result["skills_ok"],
    }
    judged_at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """UPDATE jobs SET verdict = ?, verdict_reason = ?, judge_checks = ?, judged_at = ?
                   WHERE id = ?""",
                (result["verdict"], result["reason"], json.dumps(checks), judged_at, job_id),
            )
    finally:
        conn.close()


def clear_verdicts(db_path):
    """Remove all verdicts, so every job is judged again. Returns the number of jobs."""
    conn = connect(db_path)
    try:
        with conn:
            cursor = conn.execute(
                """UPDATE jobs SET verdict = NULL, verdict_reason = NULL,
                                   judge_checks = NULL, judged_at = NULL"""
            )
            count = cursor.rowcount  # how many rows the UPDATE changed
    finally:
        conn.close()
    return count


def get_stats(db_path):
    """Return a dict with total jobs, jobs per source, jobs first seen today and verdict counts."""
    conn = connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        # NULL verdicts (not judged yet) are grouped together as one row.
        per_verdict = conn.execute(
            "SELECT verdict, COUNT(*) FROM jobs GROUP BY verdict ORDER BY COUNT(*) DESC"
        ).fetchall()
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
    return {
        "total": total,
        "per_source": per_source,
        "first_seen_today": first_seen_today,
        "per_verdict": per_verdict,
    }

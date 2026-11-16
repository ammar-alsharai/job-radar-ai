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


# Emails whose extraction succeeded (Phase 6). On GitHub Actions there is no
# data/extracted/ folder from earlier runs, so this table is how a run knows
# an email was already done.
CREATE_PROCESSED_EMAILS_TABLE = """
CREATE TABLE IF NOT EXISTS processed_emails (
    email_uid TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
)
"""

# Small key/value settings, e.g. last_digest_date = "2026-11-20".
CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
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
        conn.execute(CREATE_PROCESSED_EMAILS_TABLE)
        conn.execute(CREATE_META_TABLE)
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


def get_unjudged_jobs(db_path, include_notified=False):
    """Return jobs without a verdict, as a list of dicts (oldest first).

    Normally only jobs that were NOT sent yet: judging an already-sent job
    would only cost AI calls. include_notified=True (used by --rejudge) also
    returns sent jobs.
    """
    conn = connect(db_path)
    # sqlite3.Row lets us read columns by name (row["title"]) instead of by
    # position (row[1]), and dict(row) turns a row into a normal dict.
    conn.row_factory = sqlite3.Row
    try:
        if include_notified:
            rows = conn.execute(
                """SELECT id, title, company, location, language, seniority, salary, snippet
                   FROM jobs WHERE verdict IS NULL ORDER BY id"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, title, company, location, language, seniority, salary, snippet
                   FROM jobs WHERE verdict IS NULL AND notified_at IS NULL ORDER BY id"""
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


# --- Phase 5: notifications ----------------------------------------------------

def get_unnotified_jobs(db_path):
    """Return judged jobs that were not sent yet, as a list of dicts.

    Jobs without a verdict are left out: they wait until the next run judges them.
    judge_checks is turned back from JSON text into a dict.
    """
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, title, company, location, link, first_seen,
                      verdict, verdict_reason, judge_checks
               FROM jobs
               WHERE verdict IS NOT NULL AND notified_at IS NULL
               ORDER BY id"""
        ).fetchall()
    finally:
        conn.close()

    jobs = []
    for row in rows:
        job = dict(row)
        job["judge_checks"] = json.loads(job["judge_checks"] or "{}")
        jobs.append(job)
    return jobs


def mark_notified(db_path, job_ids):
    """Set notified_at to now for the given job ids."""
    notified_at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        with conn:
            for job_id in job_ids:
                conn.execute("UPDATE jobs SET notified_at = ? WHERE id = ?", (notified_at, job_id))
    finally:
        conn.close()


def days_since_last_sighting(db_path):
    """Days since the newest email that contained a job, or None if there are no jobs."""
    conn = connect(db_path)
    try:
        newest = conn.execute("SELECT MAX(last_seen) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    if newest is None:
        return None
    return (date.today() - date.fromisoformat(newest)).days


# --- keeping verdicts when the database is rebuilt -----------------------------

def export_judgements(db_path):
    """Remember verdicts and notifications before the database is deleted.

    Returns two things:
    - saved: {dedup_key: {"verdict": ..., "verdict_reason": ..., "judge_checks": ...,
              "judged_at": ..., "notified_at": ...}}
    - notified_email_uids: the set of email UIDs whose jobs were ALL notified.
    """
    if not db_path.exists():
        return {}, set()

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job_rows = conn.execute(
            """SELECT dedup_key, verdict, verdict_reason, judge_checks, judged_at, notified_at
               FROM jobs"""
        ).fetchall()
        sighting_rows = conn.execute(
            """SELECT sightings.email_uid, jobs.notified_at
               FROM sightings JOIN jobs ON jobs.id = sightings.job_id"""
        ).fetchall()
    finally:
        conn.close()

    saved = {}
    for row in job_rows:
        saved[row["dedup_key"]] = {
            "verdict": row["verdict"],
            "verdict_reason": row["verdict_reason"],
            "judge_checks": row["judge_checks"],
            "judged_at": row["judged_at"],
            "notified_at": row["notified_at"],
        }

    # An email counts as "fully notified" only if NONE of its jobs is unnotified.
    all_uids = set()
    uids_with_unnotified_job = set()
    for row in sighting_rows:
        all_uids.add(row["email_uid"])
        if row["notified_at"] is None:
            uids_with_unnotified_job.add(row["email_uid"])
    notified_email_uids = all_uids - uids_with_unnotified_job

    return saved, notified_email_uids


def restore_judgements(db_path, saved, notified_email_uids):
    """Put remembered verdicts back after a rebuild.

    For each job in the rebuilt database:
    - its dedup_key is in saved -> restore verdict, reason, checks, judged_at, notified_at;
    - otherwise (e.g. the dedup rules changed), if ALL its sightings come from
      fully-notified emails -> it was surely sent before under another key, so
      mark it as notified (it is never sent again);
    - otherwise leave it alone: it will be judged and sent as new.

    Returns (restored, marked_as_sent, will_be_sent_as_new).
    """
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    restored = 0
    marked_as_sent = 0
    will_be_sent = 0

    conn = connect(db_path)
    try:
        with conn:
            job_rows = conn.execute("SELECT id, dedup_key FROM jobs").fetchall()
            for job_id, dedup_key in job_rows:
                if dedup_key in saved:
                    old = saved[dedup_key]
                    conn.execute(
                        """UPDATE jobs SET verdict = ?, verdict_reason = ?, judge_checks = ?,
                                           judged_at = ?, notified_at = ?
                           WHERE id = ?""",
                        (old["verdict"], old["verdict_reason"], old["judge_checks"],
                         old["judged_at"], old["notified_at"], job_id),
                    )
                    restored += 1
                    continue

                email_uids = set()
                for (email_uid,) in conn.execute(
                    "SELECT email_uid FROM sightings WHERE job_id = ?", (job_id,)
                ):
                    email_uids.add(email_uid)

                # issubset: every UID in email_uids is also in notified_email_uids.
                if email_uids and email_uids.issubset(notified_email_uids):
                    conn.execute("UPDATE jobs SET notified_at = ? WHERE id = ?", (now, job_id))
                    marked_as_sent += 1
                else:
                    will_be_sent += 1
    finally:
        conn.close()

    return restored, marked_as_sent, will_be_sent


# --- Phase 6: state for runs on GitHub Actions --------------------------------

def is_email_processed(db_path, email_uid):
    """True if this email's extraction already succeeded in an earlier run."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE email_uid = ?", (email_uid,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def mark_email_processed(db_path, email_uid):
    """Remember that this email's extraction succeeded (also with 0 jobs)."""
    processed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        with conn:
            # "OR REPLACE": if the UID is already there, just update the time.
            conn.execute(
                "INSERT OR REPLACE INTO processed_emails (email_uid, processed_at) VALUES (?, ?)",
                (email_uid, processed_at),
            )
    finally:
        conn.close()


def get_meta(db_path, key):
    """Return a stored setting (text), or None if it was never set."""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row[0]


def set_meta(db_path, key, value):
    """Store a setting (text), replacing an older value."""
    conn = connect(db_path)
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    finally:
        conn.close()


def mark_all_notified(db_path):
    """Mark EVERY unsent job as sent (judged or not) WITHOUT sending it. Returns the number.

    Used once for the first cloud run, so old jobs are never sent and never
    judged (get_unjudged_jobs skips sent jobs): no AI calls for them.
    """
    notified_at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        with conn:
            cursor = conn.execute(
                "UPDATE jobs SET notified_at = ? WHERE notified_at IS NULL",
                (notified_at,),
            )
            count = cursor.rowcount
    finally:
        conn.close()
    return count

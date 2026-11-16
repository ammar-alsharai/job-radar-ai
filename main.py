"""Job Radar AI entry point.

Phase 1: read recent job-alert emails and save them to data/raw/.
Phase 2: let the AI extract the jobs from each email (saved to data/extracted/).
Phase 3: store the jobs in data/jobs.db and report which ones are new.
Phase 4: let the AI judge every job that has no verdict yet (profile.txt).

Usage:
    python main.py --days 3          read emails, extract, store and judge jobs
    python main.py --days 3 --no-ai  only read emails (Phase 1)
    python main.py --preview 35      show what would be sent to the AI for email 35
    python main.py --stats           show database statistics
    python main.py --rebuild-db      rebuild data/jobs.db from data/extracted/
    python main.py --rejudge         judge every job again (after changing profile.txt)
"""

import argparse
import json
import logging
import sqlite3
import sys

import openai


def parse_args():
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Read job-alert emails, extract and store the jobs.")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="how many days back to look for emails (default: 1)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="only read and save emails, skip the AI extraction and judging",
    )
    parser.add_argument(
        "--re-extract",
        action="store_true",
        help="ignore saved results in data/extracted/ and ask the AI again (overwrites them)",
    )
    parser.add_argument(
        "--preview",
        metavar="UID",
        help="show the cleaned text of one saved email (no AI call, no Gmail)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="show database statistics (no Gmail, no AI)",
    )
    parser.add_argument(
        "--rebuild-db",
        action="store_true",
        help="delete data/jobs.db and rebuild it from data/extracted/ (no Gmail, no AI). "
             "WARNING: this also erases verdicts and notified_at (Phase 4/5 data)",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="clear all verdicts and judge every job again (no Gmail, no extraction)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show DEBUG log messages, including the text sent to the AI",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be 1 or more")
    return args


def show_preview(uid):
    """Print exactly the text that would be sent to the AI, plus the link codes."""
    # Imported here so a missing .env key is caught by main()'s error handling.
    from job_radar import config
    from job_radar.email_cleaner import clean_email

    path = config.RAW_EMAIL_DIR / f"{uid}.eml"
    if not path.exists():
        raise RuntimeError(f"No saved email with UID {uid} (expected data/raw/{uid}.eml)")

    text, links = clean_email(path.read_bytes())
    print(text)
    print()
    print(f"--- {len(text)} characters, {len(links)} link codes: {', '.join(links)}")


def read_emails(days):
    """Phase 1: fetch emails, save new ones and print a summary. Returns the list."""
    from job_radar.email_reader import fetch_recent_emails

    emails = fetch_recent_emails(days_back=days)
    new_count = sum(1 for item in emails if item["is_new"])
    old_count = len(emails) - new_count

    print()
    print(f"Found {len(emails)} emails from the last {days} day(s): "
          f"{new_count} new, {old_count} already saved.")
    for item in emails:
        label = "new  " if item["is_new"] else "saved"
        print(f"  [{label}] {item['from']} | {item['subject']}")
    return emails


def extract_and_store(emails, use_cache=True):
    """Phase 2 + 3: extract the jobs from every email, store them and print a report."""
    from job_radar import config
    from job_radar.email_reader import get_email_date
    from job_radar.extractor import extract_jobs
    from job_radar.storage import save_jobs

    print()
    print("Extracted jobs:")
    total = 0
    new_jobs = []
    for item in emails:
        raw_bytes = (config.RAW_EMAIL_DIR / f"{item['uid']}.eml").read_bytes()
        jobs = extract_jobs(item["uid"], raw_bytes, use_cache=use_cache)
        total += len(jobs)

        print()
        print(f"{item['from']}: {len(jobs)} job(s)")
        for job in jobs:
            # Show "-" for empty fields so the columns stay readable.
            parts = [job["title"], job["company"], job["location"],
                     job["language"], job["seniority"]]
            print("    " + " | ".join(part or "-" for part in parts))

        # Save this email's jobs, dated by when the email was sent.
        new_jobs += save_jobs(jobs, config.DB_PATH, seen_at=get_email_date(raw_bytes))

    print()
    print(f"Extracted this run: {total} job(s) from {len(emails)} email(s): "
          f"{len(new_jobs)} new, {total - len(new_jobs)} already known.")
    if new_jobs:
        print()
        print("New jobs:")
        for job in new_jobs:
            parts = [job["title"], job["company"], job["location"]]
            print("    " + " | ".join(part or "-" for part in parts))


def uid_number(path):
    """Sort helper: data/extracted/35.json -> 35 (so 9 comes before 10)."""
    return int(path.stem)


def rebuild_db():
    """Delete the database and fill it again from the cached extractions."""
    from job_radar import config
    from job_radar.email_reader import get_email_date
    from job_radar.storage import save_jobs

    if config.DB_PATH.exists():
        print(f"Deleting {config.DB_PATH.name} ...")
        config.DB_PATH.unlink()

    files = sorted(config.EXTRACTED_DIR.glob("*.json"), key=uid_number)
    print(f"Rebuilding from {len(files)} file(s) in data/extracted/ (oldest email first) ...")
    for path in files:
        jobs = json.loads(path.read_text(encoding="utf-8"))
        raw_path = config.RAW_EMAIL_DIR / f"{path.stem}.eml"
        if raw_path.exists():
            seen_at = get_email_date(raw_path.read_bytes())
        else:
            seen_at = None  # save_jobs() then uses the current time
        save_jobs(jobs, config.DB_PATH, seen_at=seen_at)
    print("Done.")


VERDICT_LABELS = [("match", "🟢 match"), ("unclear", "🟡 unclear"), ("skip", "❌ skip")]


def judge_all():
    """Phase 4: judge every job without a verdict and print them grouped by verdict."""
    from job_radar import config
    from job_radar.judge import judge_job, load_profile
    from job_radar.storage import get_unjudged_jobs, save_verdict

    jobs = get_unjudged_jobs(config.DB_PATH)
    print()
    print(f"Judging {len(jobs)} job(s) without a verdict ...")
    if not jobs:
        return

    profile = load_profile()  # only needed when there is something to judge
    judged = {"match": [], "unclear": [], "skip": []}
    for job in jobs:
        result = judge_job(job, profile)
        if result is None:
            continue  # invalid answer: stays unjudged, tried again next run
        # Saved right away, so a stop halfway (e.g. rate limit) loses nothing.
        save_verdict(config.DB_PATH, job["id"], result)
        judged[result["verdict"]].append((job, result["reason"]))

    for verdict, label in VERDICT_LABELS:
        print()
        print(f"{label} ({len(judged[verdict])})")
        for job, reason in judged[verdict]:
            parts = [job["title"], job["company"], job["location"]]
            print("    " + " | ".join(part or "-" for part in parts) + f" — {reason}")

    not_judged = len(jobs) - sum(len(items) for items in judged.values())
    if not_judged:
        print()
        print(f"{not_judged} job(s) could not be judged (invalid AI answer); they are retried next run.")


def rejudge_all():
    """Clear every verdict, then judge all jobs again."""
    from job_radar import config
    from job_radar.judge import load_profile
    from job_radar.storage import clear_verdicts

    # Check profile.txt BEFORE clearing, so a missing profile never wipes the verdicts.
    load_profile()
    count = clear_verdicts(config.DB_PATH)
    print(f"Re-judging {count} job(s): all verdicts cleared.")
    judge_all()


def show_stats():
    """Print the database statistics."""
    from job_radar import config
    from job_radar.storage import get_stats

    stats = get_stats(config.DB_PATH)
    print()
    print(f"Jobs in database: {stats['total']}")
    print(f"First seen today: {stats['first_seen_today']}")
    print("Jobs per source:")
    for source, count in stats["per_source"]:
        print(f"    {source or '-'}: {count}")
    print("Jobs per verdict:")
    for verdict, count in stats["per_verdict"]:
        print(f"    {verdict or 'not judged yet'}: {count}")


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # The HTTP library and the openai package log every request and every
    # automatic retry at INFO level. Only show their warnings and errors, so
    # the output (public on GitHub Actions later) stays short. "httpx2" is the
    # name the HTTP library uses in our installed version; "httpx" covers the
    # original library name in case that is installed instead.
    for noisy_logger in ["httpx", "httpx2", "openai"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    if args.debug:
        # Only our own modules (job_radar.*) go to DEBUG. Other packages such as
        # openai stay at INFO: their DEBUG output is noisy and includes request details.
        logging.getLogger("job_radar").setLevel(logging.DEBUG)

    # Every known problem prints one clear line instead of a traceback.
    # The order matters: Python uses the first "except" that matches.
    try:
        if args.preview:
            show_preview(args.preview)
        elif args.stats:
            show_stats()
        elif args.rebuild_db:
            rebuild_db()
            show_stats()
        elif args.rejudge:
            rejudge_all()
        else:
            emails = read_emails(args.days)
            if not args.no_ai:
                # --re-extract turns the cache off for this run.
                extract_and_store(emails, use_cache=not args.re_extract)
                judge_all()
    except RuntimeError as error:
        # Our own messages: missing setting, Gmail login failed, ...
        print(f"Error: {error}")
        sys.exit(1)
    except sqlite3.Error as error:
        print(f"Database error: {error}")
        sys.exit(1)
    except openai.AuthenticationError:
        print("AI login failed: check AI_API_KEY (and AI_BASE_URL) in .env.")
        sys.exit(1)
    except openai.RateLimitError:
        print("AI rate limit reached, even after retries: wait a while and run again "
              "(emails that are already done are saved).")
        sys.exit(1)
    except openai.BadRequestError:
        print("The AI rejected the request: check that AI_MODEL supports strict JSON "
              "schema output and that AI_REASONING_EFFORT is valid for that model.")
        sys.exit(1)
    except openai.APIConnectionError:
        print("Could not connect to the AI provider: check your internet connection and AI_BASE_URL.")
        sys.exit(1)
    except openai.APIStatusError as error:
        # Any other error answer from the provider, e.g. 404 for an unknown model.
        print(f"The AI provider returned an error (HTTP {error.status_code}): "
              "check AI_MODEL and AI_BASE_URL.")
        sys.exit(1)
    except OSError:
        print("Could not connect to the mail server: check your internet connection and IMAP_HOST.")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Job Radar AI entry point.

Phase 1: read recent job-alert emails and save them to data/raw/.
Phase 2: let the AI extract the jobs from each email (saved to data/extracted/).
Phase 3: store the jobs in data/jobs.db and report which ones are new.
Phase 4: let the AI judge every job that has no verdict yet (profile).
Phase 5: send the judged, not yet sent jobs as a Telegram digest.
Phase 6: run daily on GitHub Actions (--ci, --skip-if-sent-today, encrypted database).

Usage:
    python main.py --days 3          read emails, extract, store, judge, send digest
    python main.py --days 3 --no-send  same, but print the digest instead (dry run)
    python main.py --days 3 --no-ai  only read emails (Phase 1)
    python main.py --days 2 --ci --skip-if-sent-today   the daily cloud run
    python main.py --days 2 --ci --mark-all-sent        the first cloud run
    python main.py --test-telegram   send a test message to check the bot and chat id
    python main.py --preview 35      show what would be sent to the AI for email 35
    python main.py --stats           show database statistics
    python main.py --rebuild-db      rebuild data/jobs.db from data/extracted/
    python main.py --rejudge         judge every job again (after changing the profile)
    python main.py --encrypt-db      data/jobs.db -> data/jobs.db.enc
    python main.py --decrypt-db      data/jobs.db.enc -> data/jobs.db
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import openai

# The database files, for --encrypt-db and --decrypt-db. These must match
# config.DB_PATH. They are defined here instead of importing config, because
# config requires the Gmail settings, and the encrypt/decrypt steps of the
# workflow only get DB_ENCRYPTION_KEY.
PROJECT_ROOT = Path(__file__).resolve().parent
DB_FILE = PROJECT_ROOT / "data" / "jobs.db"
ENCRYPTED_DB_FILE = PROJECT_ROOT / "data" / "jobs.db.enc"


def parse_args():
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Read job-alert emails, extract, judge and send the jobs.")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="how many days back to look for emails (default: 1)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="only read and save emails, skip the AI extraction, judging and digest",
    )
    parser.add_argument(
        "--re-extract",
        action="store_true",
        help="ignore saved results and already-processed emails, ask the AI again",
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
        help="delete data/jobs.db and rebuild it from data/extracted/ (no Gmail, no AI); "
             "verdicts and sent status are kept",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="clear all verdicts and judge every job again (no Gmail, no extraction)",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="print the Telegram digest instead of sending it; nothing is marked as sent",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send a test message to Telegram and stop",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="public-log mode: print only counts (no senders, subjects, titles, reasons or links)",
    )
    parser.add_argument(
        "--skip-if-sent-today",
        action="store_true",
        help="stop right away if today's digest was already sent",
    )
    parser.add_argument(
        "--mark-all-sent",
        action="store_true",
        help="read, extract, store and judge, then mark every judged job as sent WITHOUT sending",
    )
    parser.add_argument(
        "--encrypt-db",
        action="store_true",
        help="encrypt data/jobs.db to data/jobs.db.enc (needs DB_ENCRYPTION_KEY)",
    )
    parser.add_argument(
        "--decrypt-db",
        action="store_true",
        help="decrypt data/jobs.db.enc to data/jobs.db (needs DB_ENCRYPTION_KEY)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show DEBUG log messages, including the text sent to the AI (ignored with --ci)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be 1 or more")
    return args


# --- encryption (Phase 6) ------------------------------------------------------

def encrypt_db():
    """data/jobs.db -> data/jobs.db.enc with the key in DB_ENCRYPTION_KEY."""
    from dotenv import load_dotenv

    from job_radar.crypto_store import encrypt_file, get_key

    load_dotenv(PROJECT_ROOT / ".env")  # locally the key may be in .env
    if not DB_FILE.exists():
        raise RuntimeError("data/jobs.db not found: nothing to encrypt")
    encrypt_file(DB_FILE, ENCRYPTED_DB_FILE, get_key())
    print("Encrypted data/jobs.db to data/jobs.db.enc.")


def decrypt_db():
    """data/jobs.db.enc -> data/jobs.db with the key in DB_ENCRYPTION_KEY."""
    from dotenv import load_dotenv

    from job_radar.crypto_store import decrypt_file, get_key

    load_dotenv(PROJECT_ROOT / ".env")
    if not ENCRYPTED_DB_FILE.exists():
        raise RuntimeError("data/jobs.db.enc not found: nothing to decrypt")
    decrypt_file(ENCRYPTED_DB_FILE, DB_FILE, get_key())
    print("Decrypted data/jobs.db.enc to data/jobs.db.")


# --- the pipeline ----------------------------------------------------------------

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


def read_emails(days, ci=False):
    """Phase 1: fetch emails, save new ones and print a summary. Returns the list."""
    from job_radar.email_reader import fetch_recent_emails

    emails = fetch_recent_emails(days_back=days)
    new_count = sum(1 for item in emails if item["is_new"])
    old_count = len(emails) - new_count

    if ci:
        print(f"Emails read: {len(emails)} (new: {new_count})")
        return emails

    print()
    print(f"Found {len(emails)} emails from the last {days} day(s): "
          f"{new_count} new, {old_count} already saved.")
    for item in emails:
        label = "new  " if item["is_new"] else "saved"
        print(f"  [{label}] {item['from']} | {item['subject']}")
    return emails


def extract_and_store(emails, use_cache=True, ci=False):
    """Phase 2 + 3: extract the jobs from every email that is not done yet, store them."""
    from job_radar import config
    from job_radar.email_reader import get_email_date
    from job_radar.extractor import extract_jobs
    from job_radar.storage import is_email_processed, mark_email_processed, save_jobs

    if not ci:
        print()
        print("Extracted jobs:")
    total = 0
    new_jobs = []
    skipped = 0
    failed = 0
    for item in emails:
        # Already done in an earlier run (also on GitHub Actions, where the
        # data/extracted/ files of earlier runs do not exist): skip it.
        if use_cache and is_email_processed(config.DB_PATH, item["uid"]):
            skipped += 1
            continue

        raw_bytes = (config.RAW_EMAIL_DIR / f"{item['uid']}.eml").read_bytes()
        jobs = extract_jobs(item["uid"], raw_bytes, use_cache=use_cache)
        if jobs is None:
            failed += 1  # invalid AI answer: not marked, tried again next run
            continue
        total += len(jobs)

        if not ci:
            print()
            print(f"{item['from']}: {len(jobs)} job(s)")
            for job in jobs:
                # Show "-" for empty fields so the columns stay readable.
                parts = [job["title"], job["company"], job["location"],
                         job["language"], job["seniority"]]
                print("    " + " | ".join(part or "-" for part in parts))

        # Save this email's jobs, dated by when the email was sent, and only
        # then mark the email as done.
        new_jobs += save_jobs(jobs, config.DB_PATH, seen_at=get_email_date(raw_bytes))
        mark_email_processed(config.DB_PATH, item["uid"])

    if ci:
        print(f"Emails extracted: {len(emails) - skipped - failed} "
              f"(already done: {skipped}, failed: {failed})")
        print(f"Jobs extracted: {total} (new: {len(new_jobs)})")
        return

    print()
    print(f"Extracted this run: {total} job(s) from {len(emails) - skipped - failed} email(s): "
          f"{len(new_jobs)} new, {total - len(new_jobs)} already known.")
    if skipped:
        print(f"Skipped {skipped} email(s) that were already processed.")
    if failed:
        print(f"{failed} email(s) got an invalid AI answer; they are retried next run.")
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
    from job_radar.storage import (
        export_judgements,
        mark_email_processed,
        restore_judgements,
        save_jobs,
    )

    # Remember verdicts and sent status BEFORE deleting anything.
    saved, notified_email_uids = export_judgements(config.DB_PATH)
    if config.DB_PATH.exists():
        print(f"Saved verdicts of {len(saved)} job(s); deleting {config.DB_PATH.name} ...")
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
        mark_email_processed(config.DB_PATH, path.stem)

    restored, marked, new = restore_judgements(config.DB_PATH, saved, notified_email_uids)
    print(f"Restored {restored}, marked {marked} as already sent, {new} will be sent as new.")


VERDICT_LABELS = [("match", "🟢 match"), ("unclear", "🟡 unclear"), ("skip", "❌ skip")]


def judge_all(ci=False):
    """Phase 4: judge every job without a verdict and print them grouped by verdict."""
    from job_radar import config
    from job_radar.judge import judge_job, load_profile
    from job_radar.storage import get_unjudged_jobs, save_verdict

    jobs = get_unjudged_jobs(config.DB_PATH)
    if not ci:
        print()
        print(f"Judging {len(jobs)} job(s) without a verdict ...")
    judged = {"match": [], "unclear": [], "skip": []}

    if jobs:
        profile = load_profile()  # only needed when there is something to judge
        for job in jobs:
            result = judge_job(job, profile)
            if result is None:
                continue  # invalid answer: stays unjudged, tried again next run
            # Saved right away, so a stop halfway (e.g. rate limit) loses nothing.
            save_verdict(config.DB_PATH, job["id"], result)
            judged[result["verdict"]].append((job, result["reason"]))

    not_judged = len(jobs) - sum(len(items) for items in judged.values())

    if ci:
        print(f"Jobs judged: {len(jobs) - not_judged} (match: {len(judged['match'])}, "
              f"unclear: {len(judged['unclear'])}, skip: {len(judged['skip'])}, "
              f"failed: {not_judged})")
        return
    if not jobs:
        return

    for verdict, label in VERDICT_LABELS:
        print()
        print(f"{label} ({len(judged[verdict])})")
        for job, reason in judged[verdict]:
            parts = [job["title"], job["company"], job["location"]]
            print("    " + " | ".join(part or "-" for part in parts) + f" — {reason}")

    if not_judged:
        print()
        print(f"{not_judged} job(s) could not be judged (invalid AI answer); they are retried next run.")


def rejudge_all():
    """Clear every verdict, then judge all jobs again."""
    from job_radar import config
    from job_radar.judge import load_profile
    from job_radar.storage import clear_verdicts

    # Check the profile BEFORE clearing, so a missing profile never wipes the verdicts.
    load_profile()
    count = clear_verdicts(config.DB_PATH)
    print(f"Re-judging {count} job(s): all verdicts cleared.")
    judge_all()


def send_digest(dry_run=False, ci=False):
    """Phase 5: send all judged, not yet sent jobs to Telegram, then mark them as sent.

    dry_run=True prints the digest instead and marks nothing.
    """
    from job_radar import config
    from job_radar.notifier import (
        NO_ALERTS_WARNING,
        PAUSE_BETWEEN_MESSAGES,
        build_messages,
        send_message,
    )
    from job_radar.storage import (
        days_since_last_sighting,
        get_unnotified_jobs,
        mark_notified,
        set_meta,
    )

    jobs = get_unnotified_jobs(config.DB_PATH)
    match_jobs = [job for job in jobs if job["verdict"] == "match"]
    unclear_jobs = [job for job in jobs if job["verdict"] == "unclear"]
    skip_ids = [job["id"] for job in jobs if job["verdict"] == "skip"]
    skip_count = len(skip_ids)

    days = days_since_last_sighting(config.DB_PATH)
    warning = None
    if days is not None and days >= 3:
        warning = NO_ALERTS_WARNING

    messages = build_messages(match_jobs, unclear_jobs, skip_count, warning=warning)

    if dry_run:
        if ci:
            print(f"Dry run: {len(messages)} Telegram message(s) built, nothing sent")
            return
        print()
        for number, message in enumerate(messages, start=1):
            print(f"--- Telegram message {number} of {len(messages)} ---")
            print(message["text"])
        print("--- dry run: nothing sent, nothing marked as sent ---")
        return

    for number, message in enumerate(messages, start=1):
        if number > 1:
            time.sleep(PAUSE_BETWEEN_MESSAGES)  # be gentle with Telegram
        try:
            send_message(message["text"])
        except RuntimeError:
            print(f"Sent {number - 1} of {len(messages)} message(s); "
                  "the jobs that did not arrive are sent again next run.")
            raise  # main() prints the error and exits with code 1
        # This message arrived: mark its jobs now, so a failure later on
        # only resends the jobs that did not arrive.
        mark_notified(config.DB_PATH, message["job_ids"])

    # Skipped jobs are only counted in the header, so mark them once every
    # message has arrived. Then remember that today's digest is done.
    mark_notified(config.DB_PATH, skip_ids)
    set_meta(config.DB_PATH, "last_digest_date", date.today().isoformat())

    if ci:
        print(f"Telegram messages sent: {len(messages)}")
    else:
        print()
        print(f"Sent {len(messages)} Telegram message(s); {len(jobs)} job(s) marked as sent.")


def mark_all_sent():
    """First cloud run: mark every judged job as sent without sending anything."""
    from job_radar import config
    from job_radar.storage import mark_all_judged_notified

    count = mark_all_judged_notified(config.DB_PATH)
    print(f"Marked {count} job(s) as sent without sending.")


def run_pipeline(args):
    """The normal run: read -> extract -> store -> judge -> send (or mark all sent)."""
    from job_radar import config
    from job_radar.storage import get_meta

    if args.skip_if_sent_today:
        # date.today() follows the computer's time zone; the workflow sets
        # TZ=Europe/Amsterdam, so this is the Amsterdam date there too.
        if get_meta(config.DB_PATH, "last_digest_date") == date.today().isoformat():
            print("Digest already sent today.")
            return

    emails = read_emails(args.days, ci=args.ci)
    if args.no_ai:
        return
    # --re-extract turns the cache and the processed-emails check off for this run.
    extract_and_store(emails, use_cache=not args.re_extract, ci=args.ci)
    judge_all(ci=args.ci)
    if args.mark_all_sent:
        mark_all_sent()  # note: does NOT set last_digest_date
    else:
        send_digest(dry_run=args.no_send, ci=args.ci)


def test_telegram():
    """Send a short test message to check the bot token and chat id."""
    from job_radar.notifier import send_message

    send_message("✅ Job Radar test message")
    print("Test message sent: check Telegram.")


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


def setup_logging(args):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # The HTTP library and the openai package log every request and every
    # automatic retry at INFO level. Only show their warnings and errors, so
    # the output stays short. "httpx2" is the name the HTTP library uses in
    # our installed version; "httpx" covers the original library name.
    for noisy_logger in ["httpx", "httpx2", "openai"]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    if args.ci:
        # Public logs: our own modules only show warnings (which contain only
        # UIDs, job ids and numbers), never the INFO/DEBUG details.
        logging.getLogger("job_radar").setLevel(logging.WARNING)
    elif args.debug:
        # Only our own modules (job_radar.*) go to DEBUG. Other packages such as
        # openai stay quiet: their DEBUG output is noisy and includes request details.
        logging.getLogger("job_radar").setLevel(logging.DEBUG)


def main():
    args = parse_args()
    setup_logging(args)

    # Every known problem prints one clear line instead of a traceback.
    # The order matters: Python uses the first "except" that matches.
    try:
        if args.encrypt_db:
            encrypt_db()
        elif args.decrypt_db:
            decrypt_db()
        elif args.test_telegram:
            test_telegram()
        elif args.preview:
            show_preview(args.preview)
        elif args.stats:
            show_stats()
        elif args.rebuild_db:
            rebuild_db()
            show_stats()
        elif args.rejudge:
            rejudge_all()
        else:
            run_pipeline(args)
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

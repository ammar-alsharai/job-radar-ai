"""Job Radar AI entry point.

Phase 1: read recent job-alert emails and save them to data/raw/.
Phase 2: let the AI extract the jobs from each email (saved to data/extracted/).

Usage:
    python main.py --days 3          read emails + extract jobs
    python main.py --days 3 --no-ai  only read emails (Phase 1)
    python main.py --preview 35      show what would be sent to the AI for email 35
"""

import argparse
import logging
import sys

import openai


def parse_args():
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Read job-alert emails and extract the jobs.")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="how many days back to look for emails (default: 1)",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="only read and save emails, skip the AI extraction",
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


def extract_all(emails, use_cache=True):
    """Phase 2: extract the jobs from every email and print them."""
    from job_radar import config
    from job_radar.extractor import extract_jobs

    print()
    print("Extracted jobs:")
    total = 0
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

    print()
    print(f"Total: {total} job(s) from {len(emails)} email(s).")


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Every known problem prints one clear line instead of a traceback.
    # The order matters: Python uses the first "except" that matches.
    try:
        if args.preview:
            show_preview(args.preview)
            return
        emails = read_emails(args.days)
        if not args.no_ai:
            # --re-extract turns the cache off for this run.
            extract_all(emails, use_cache=not args.re_extract)
    except RuntimeError as error:
        # Our own messages: missing setting, Gmail login failed, ...
        print(f"Error: {error}")
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

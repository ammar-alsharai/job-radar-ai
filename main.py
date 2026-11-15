"""Job Radar AI entry point.

Phase 1: read recent job-alert emails, save them to data/raw/ and print a summary.
Usage:  python main.py --days 3
"""

import argparse
import logging
import sys


def parse_args():
    """Read the command-line options."""
    parser = argparse.ArgumentParser(description="Read recent job-alert emails.")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="how many days back to look for emails (default: 1)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be 1 or more")
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        # Imported here, not at the top: importing it also loads config.py,
        # which checks .env. This way a missing key is caught below too.
        from job_radar.email_reader import fetch_recent_emails

        emails = fetch_recent_emails(days_back=args.days)
    except RuntimeError as error:
        # Our own clear error messages (missing key, login failed, ...).
        print(f"Error: {error}")
        sys.exit(1)
    except OSError:
        # Network problems: no internet, wrong host name, timeout, SSL error.
        print("Could not connect to the mail server: "
              "check your internet connection and IMAP_HOST.")
        sys.exit(1)

    new_count = sum(1 for item in emails if item["is_new"])
    old_count = len(emails) - new_count

    print()
    print(f"Found {len(emails)} emails from the last {args.days} day(s): "
          f"{new_count} new, {old_count} already saved.")
    for item in emails:
        label = "new  " if item["is_new"] else "saved"
        print(f"  [{label}] {item['from']} | {item['subject']}")


if __name__ == "__main__":
    main()

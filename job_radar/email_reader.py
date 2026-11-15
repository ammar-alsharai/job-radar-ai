"""Read recent job-alert emails from Gmail via IMAP and save them raw to disk.

UIDs vs. sequence numbers
-------------------------
IMAP gives every email two numbers:
- A sequence number: its position in the mailbox right now (1, 2, 3, ...).
  These shift when an email is deleted or moved, so "email 5" today can be a
  different email tomorrow.
- A UID: a permanent ID for that email in this mailbox. It does not change,
  so we can safely use it as a file name (data/raw/<uid>.eml).
That is why we use imap.uid("search", ...) and imap.uid("fetch", ...).
(Strictly speaking a UID is only unique together with the mailbox's
UIDVALIDITY value, but Gmail practically never resets that for INBOX.)
"""

import email
import imaplib
import logging
from datetime import date, timedelta
from email.header import decode_header, make_header

from job_radar import config

logger = logging.getLogger(__name__)

IMAP_PORT = 993  # standard port for IMAP over SSL
TIMEOUT_SECONDS = 30  # give up instead of hanging forever on a bad connection

# IMAP needs English month names in dates (e.g. "12-Nov-2026").
# strftime("%b") would follow the computer's language settings, so we use a
# fixed list instead.
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(day):
    """Format a date the way IMAP expects it, e.g. date(2026, 11, 2) -> '02-Nov-2026'."""
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def decode_header_value(value):
    """Turn a raw email header into readable text.

    Headers with non-ASCII characters are encoded, for example
    '=?UTF-8?B?Sm9iIGFsZXJ0IOKAkyDwn5K8?='. decode_header() splits this into
    parts and make_header() glues them back together as normal text.
    """
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError):
        # Unknown or broken character set: fall back to the raw header text.
        return str(value)


def read_headers(raw_bytes):
    """Return the sender, subject and date of one raw email."""
    message = email.message_from_bytes(raw_bytes)
    return {
        "from": decode_header_value(message["From"]),
        "subject": decode_header_value(message["Subject"]),
        "date": decode_header_value(message["Date"]),
    }


def fetch_recent_emails(days_back=1):
    """Save emails from the last `days_back` days to data/raw/ and return a summary.

    Returns a list of dicts with: uid, from, subject, date, is_new.
    is_new is False if the email was already saved by an earlier run.
    Emails are opened read-only and fetched with BODY.PEEK[], so they stay unread.
    """
    config.RAW_EMAIL_DIR.mkdir(parents=True, exist_ok=True)

    # IMAP's SINCE only works with whole days (no times).
    since = imap_date(date.today() - timedelta(days=days_back))

    results = []

    # The "with" block always logs out at the end, even if an error happens.
    with imaplib.IMAP4_SSL(config.IMAP_HOST, IMAP_PORT, timeout=TIMEOUT_SECONDS) as imap:
        try:
            imap.login(config.JOB_EMAIL_ADDRESS, config.JOB_EMAIL_APP_PASSWORD)
        except imaplib.IMAP4.error:
            # "from None" hides the original error so nothing sensitive is shown.
            raise RuntimeError(
                "Gmail login failed. Check JOB_EMAIL_ADDRESS and "
                "JOB_EMAIL_APP_PASSWORD in .env (use an app password, "
                "not your normal Gmail password)."
            ) from None
        logger.info("Logged in to %s", config.IMAP_HOST)

        # readonly=True: we can look, but nothing in the mailbox can change.
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Could not open INBOX")

        status, data = imap.uid("search", None, "SINCE", since)
        if status != "OK":
            raise RuntimeError("IMAP search failed")

        # data[0] looks like b"101 102 105": split it into a list of UIDs.
        uids = [uid.decode() for uid in data[0].split()]
        logger.info("Found %d emails since %s", len(uids), since)

        for uid in uids:
            path = config.RAW_EMAIL_DIR / f"{uid}.eml"

            if path.exists():
                # Saved by an earlier run: read the headers from disk instead
                # of downloading the email again.
                raw_bytes = path.read_bytes()
                is_new = False
            else:
                # BODY.PEEK[] downloads the full email WITHOUT marking it as read
                # (plain BODY[] would mark it as read).
                status, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
                # On success, data[0] is a pair: (info line, raw email bytes).
                if status != "OK" or not isinstance(data[0], tuple):
                    logger.warning("Could not fetch email with UID %s, skipping", uid)
                    continue
                raw_bytes = data[0][1]
                path.write_bytes(raw_bytes)
                is_new = True
                logger.info("Saved email UID %s", uid)

            headers = read_headers(raw_bytes)
            results.append({
                "uid": uid,
                "from": headers["from"],
                "subject": headers["subject"],
                "date": headers["date"],
                "is_new": is_new,
            })

    return results

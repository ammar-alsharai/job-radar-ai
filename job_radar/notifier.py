"""Build the daily job digest and send it to Telegram (standard library only).

HTTP POST with urllib and a JSON body
-------------------------------------
To send a message we make an HTTP POST request to the Telegram Bot API: we
send data (the "body") to a web address. Our body is JSON, e.g.
{"chat_id": "...", "text": "..."}, so we turn a dict into JSON text with
json.dumps(), then into bytes with .encode("utf-8"), and tell the server
what it is with the header "Content-Type: application/json".

Security: the bot token is part of the request URL
(https://api.telegram.org/bot<TOKEN>/sendMessage). So we never log or print
the URL, and errors only contain the HTTP status and Telegram's description.

HTML escaping
-------------
We send the message with parse_mode "HTML", so Telegram reads tags like
<a href="...">. Job titles can contain characters such as & < > (e.g. "R&D"),
which would break the HTML or could add tags we never meant. html.escape()
turns them into safe codes (&amp; &lt; &gt;) that Telegram shows as the
normal characters.

Retries
-------
Sometimes the connection to Telegram drops, or Telegram says "too many
requests" (HTTP 429). Those problems are usually gone a few seconds later, so
we wait and try again (up to 3 times, waiting longer each time). Errors that
will not fix themselves, like a wrong token (401) or chat id (400), are not
retried.
"""

import html
import json
import logging
import re
import time
import urllib.error
import urllib.request

from job_radar import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_SECONDS = 30

# Waits before retry 1, 2 and 3 after a network error or HTTP 429.
RETRY_WAITS = [2, 5, 10]

# Pause between two messages of one digest (used by main.py).
PAUSE_BETWEEN_MESSAGES = 1

# Telegram allows 4,096 characters of VISIBLE text per message (after the HTML
# is parsed: tags and the URLs inside href do not count). We stay well below.
MAX_VISIBLE_CHARS = 3500
# Safety cap on the raw HTML (tags and long tracking URLs included).
MAX_RAW_CHARS = 12000

NO_ALERTS_WARNING = "⚠️ No new jobs from your alerts in 3 days: check your alert subscriptions."
NO_JOBS_TEXT = "💼 Job Radar: no new jobs today."

# Matches any HTML tag, e.g. <b>, </a> or <a href="...">.
TAG_PATTERN = re.compile(r"<[^>]+>")


class RetryableError(Exception):
    """A temporary problem (network error or HTTP 429): worth trying again.

    retry_after: seconds Telegram asked us to wait (HTTP 429), or None.
    We use our own exception class so the retry loop can tell temporary
    problems apart from permanent ones (plain RuntimeError).
    """

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def send_message(text):
    """Send one message, retrying temporary problems. Raises RuntimeError on failure."""
    config.require_telegram_settings()

    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            send_once(text)
            return
        except RetryableError as error:
            if attempt == len(RETRY_WAITS):
                # All retries used up: give up with a normal error.
                raise RuntimeError(f"{error} (gave up after {len(RETRY_WAITS)} retries)") from None
            wait = error.retry_after or RETRY_WAITS[attempt]
            logger.warning("%s: retrying in %d s (retry %d of %d)",
                           error, wait, attempt + 1, len(RETRY_WAITS))
            time.sleep(wait)


def send_once(text):
    """Make one sendMessage request.

    Raises RetryableError for temporary problems and RuntimeError for others.
    """
    # This URL contains the secret token: never log or print it.
    url = f"{TELEGRAM_API}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Telegram answered with an error status. "from None" drops the
        # original error, which contains the URL with the token.
        answer = read_error_answer(error)
        description = answer.get("description", "no description")
        message = f"Telegram error (HTTP {error.code}): {description}"
        if error.code == 429:
            retry_after = (answer.get("parameters") or {}).get("retry_after")
            raise RetryableError(message, retry_after=retry_after) from None
        raise RuntimeError(message) from None  # 400, 401, 403, ...: not retried
    except OSError as error:
        # No answer at all: no internet, connection dropped, timeout, ...
        # (URLError and TimeoutError are both kinds of OSError.)
        reason = getattr(error, "reason", error)
        raise RetryableError(f"Could not reach Telegram: {reason}") from None

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result.get('description', 'unknown error')}")


def read_error_answer(http_error):
    """Return Telegram's JSON error answer as a dict, or {} if it cannot be read."""
    try:
        answer = json.loads(http_error.read().decode("utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(answer, dict):
        return {}
    return answer


# --- building the digest ------------------------------------------------------

def visible_length(html_text):
    """Length of the text as Telegram shows it: without tags, entities decoded.

    Example: '<a href="https://...">R&amp;D</a>' -> "R&D" -> 3.
    """
    return len(html.unescape(TAG_PATTERN.sub("", html_text)))


def fits_in_one_message(html_text):
    return visible_length(html_text) <= MAX_VISIBLE_CHARS and len(html_text) <= MAX_RAW_CHARS


def format_job(job):
    """One job block:  <a href="link">Title</a> · Company · Location
                        ↳ reason
    All job text is escaped, including the link inside href.
    """
    title = html.escape(job.get("title") or "(no title)")
    if job.get("link"):
        title = f'<a href="{html.escape(job["link"])}">{title}</a>'

    parts = [title]
    for field in ["company", "location"]:
        if job.get(field):
            parts.append(html.escape(job[field]))
    block = " · ".join(parts)

    reason = job.get("verdict_reason") or ""
    if reason:
        block += "\n↳ " + html.escape(reason)
    return block


def count_yes(job):
    """How many of the four checks are "yes"."""
    checks = job.get("judge_checks") or {}
    return sum(1 for answer in checks.values() if answer == "yes")


def unclear_sort_key(job):
    # Python compares tuples item by item: first the yes-count, then the date.
    return (count_yes(job), job.get("first_seen") or "")


def newest_first_key(job):
    return job.get("first_seen") or ""


def pack_messages(blocks):
    """Join blocks into messages that fit Telegram's limits, never splitting a block.

    blocks: list of (text, job_ids) pairs; job_ids lists the jobs in that block.
    Returns a list of {"text": ..., "job_ids": [...]} dicts, one per message.
    """
    messages = []
    current_text = ""
    current_ids = []
    for text, job_ids in blocks:
        if not current_text:
            current_text = text
            current_ids = list(job_ids)
        elif fits_in_one_message(current_text + "\n\n" + text):
            current_text += "\n\n" + text
            current_ids += job_ids
        else:
            messages.append({"text": current_text, "job_ids": current_ids})
            current_text = text  # a block bigger than the limit gets its own message
            current_ids = list(job_ids)
    if current_text:
        messages.append({"text": current_text, "job_ids": current_ids})
    return messages


def waiting_line(waiting_count):
    """The line shown when the AI rate limit stopped judging."""
    return (f"⏳ {waiting_count} jobs are waiting for AI judgment (rate limit) "
            "and will follow in a later digest")


def build_messages(match_jobs, unclear_jobs, skip_count, warning=None, waiting_count=0):
    """Return the digest as a list of {"text": ..., "job_ids": [...]} dicts.

    job_ids are the listed (match and unclear) jobs in that message, so main.py
    can mark them as sent right after that message arrives.
    waiting_count: jobs not judged yet because of the AI rate limit (shown on top).
    """
    blocks = []
    if warning:
        blocks.append((warning, []))
    if waiting_count:
        blocks.append((waiting_line(waiting_count), []))

    total = len(match_jobs) + len(unclear_jobs) + skip_count
    if total == 0:
        blocks.append((NO_JOBS_TEXT, []))
        return pack_messages(blocks)

    blocks.append((
        f"💼 Job Radar: {total} new jobs "
        f"({len(match_jobs)} match, {len(unclear_jobs)} unclear, {skip_count} skipped)",
        [],
    ))

    # reverse=True: highest first (most "yes" checks, then newest date).
    sections = [
        ("🟢 <b>Match</b>", sorted(match_jobs, key=newest_first_key, reverse=True)),
        ("🟡 <b>Unclear</b>", sorted(unclear_jobs, key=unclear_sort_key, reverse=True)),
    ]
    for heading, jobs in sections:
        for index, job in enumerate(jobs):
            text = format_job(job)
            if index == 0:
                # Attach the heading to the first job, so a heading never ends
                # up alone at the bottom of a message.
                text = heading + "\n" + text
            blocks.append((text, [job.get("id")]))

    return pack_messages(blocks)


def build_digest(match_jobs, unclear_jobs, skip_count, warning=None, waiting_count=0):
    """Return the digest as a list of message strings (usually just one)."""
    messages = build_messages(match_jobs, unclear_jobs, skip_count, warning, waiting_count)
    return [message["text"] for message in messages]

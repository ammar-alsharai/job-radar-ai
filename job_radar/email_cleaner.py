"""Turn a raw email into short, clean text that is safe to send to the AI.

Privacy: the text this module returns contains no URLs, no HTML, no job-alerts
email address and no name from MY_NAME. Every link is replaced by a short code
like [L1]; the code -> URL mapping stays here on this computer.

MIME parts
----------
An email is often made of several "parts" (MIME = the standard for this):
for example a text/plain version, a text/html version of the same message,
and maybe images or attachments. We walk through all parts and pick the
HTML version if there is one (that is what people see), otherwise the
plain-text version.
"""

import email
import html
import logging
import re
from email import policy
from html.parser import HTMLParser

from job_radar import config

logger = logging.getLogger(__name__)

# Maximum length of the text we send to the AI. About 12,000 characters is
# roughly 3,000-4,000 tokens, well under Groq's free limit of 8,000 tokens/minute.
MAX_TEXT_CHARS = 12_000

# Finds web links in plain text: "http://" or "https://" followed by anything
# that is not a space, quote or bracket.
URL_PATTERN = re.compile(r"https?://[^\s<>\"'()\[\]]+")

# Invisible characters that marketing emails use for spacing tricks (often as
# long runs of "&zwnj;"). They carry no meaning and only waste tokens.
# Written as \u codes because they are invisible in the editor.
INVISIBLE_CHARS = [
    "​",  # zero-width space
    "‌",  # zero-width non-joiner (&zwnj;)
    "‍",  # zero-width joiner
    "‎",  # left-to-right mark
    "‏",  # right-to-left mark
    "⁠",  # word joiner
    "﻿",  # byte order mark (BOM)
    "­",  # soft hyphen
    "͏",  # combining grapheme joiner
]

# Finds leftover HTML tags in plain text, like <br> or </td>: a "<", maybe a
# "/", then a LETTER. "<[L1]>" or "a < b" are not tags and stay.
TAG_PATTERN = re.compile(r"</?[a-zA-Z][^<>]*>")

# HTML tags whose content is never visible text. (Not "head": some emails
# never close it, and then we would skip the whole email.)
SKIP_TAGS = {"script", "style", "title"}

# HTML tags that start a new line of text.
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}


class LinkCodes:
    """Hands out short codes for URLs: the first URL gets L1, the next L2, ...

    The same URL always gets the same code, so a job whose logo and title both
    link to the same page only gets one code.
    """

    def __init__(self):
        self.links = {}  # code -> URL, e.g. {"L1": "https://..."}
        self.codes = {}  # URL -> code, to find codes we already gave out

    def code_for(self, url):
        if url not in self.codes:
            code = f"L{len(self.links) + 1}"
            self.codes[url] = code
            self.links[code] = url
        return self.codes[url]


class HtmlToText(HTMLParser):
    """Collect the visible text of an HTML email, with [Ln] codes after links.

    html.parser works by "callbacks": while it reads the HTML, it calls
    handle_starttag() for every <tag>, handle_endtag() for every </tag> and
    handle_data() for the text in between. We subclass HTMLParser and fill in
    those methods to decide what to keep.
    """

    def __init__(self, link_codes):
        super().__init__()
        self.link_codes = link_codes
        self.pieces = []  # bits of text, joined together at the end
        self.skip_depth = 0  # > 0 while we are inside <script>, <style>, ...
        self.current_link_code = None  # code of the <a> we are inside, if any

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.pieces.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            # Only web links get a code; mailto: and other links are dropped.
            if href.startswith(("http://", "https://")):
                self.current_link_code = self.link_codes.code_for(href)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.pieces.append("\n")
        elif tag == "a" and self.current_link_code:
            self.pieces.append(f" [{self.current_link_code}] ")
            self.current_link_code = None

    def handle_data(self, data):
        if self.skip_depth == 0:
            self.pieces.append(data)

    def get_text(self):
        return "".join(self.pieces)


def get_body(message):
    """Return (body_text, is_html) for the best text part of the email."""
    plain_body = None
    html_body = None
    for part in message.walk():
        # Skip attachments: we only want the message itself.
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        # get_content() also undoes the transfer encoding (base64, quoted-
        # printable) and the character set, so we get a normal string.
        if content_type == "text/plain" and plain_body is None:
            plain_body = part.get_content()
        elif content_type == "text/html" and html_body is None:
            html_body = part.get_content()

    # HTML first: it is what people actually see. Some senders put the jobs
    # only in the HTML and send a stripped plain-text copy.
    if html_body and html_body.strip():
        return html_body, True
    if plain_body:
        return plain_body, False
    return "", False


def replace_urls(text, link_codes):
    """Replace every plain-text URL with its code, e.g. "https://..." -> "[L3]"."""

    def url_to_code(match):
        # re.sub() calls this for every URL it finds and uses the return value.
        url = match.group()
        return f"[{link_codes.code_for(url)}]"

    return URL_PATTERN.sub(url_to_code, text)


def remove_personal_info(text):
    """Remove the job-alerts email address and name variants (case-insensitive)."""
    # The email address anywhere, also inside other text.
    text = re.sub(re.escape(config.JOB_EMAIL_ADDRESS), "", text, flags=re.IGNORECASE)
    # Names only as whole words: \b is a "word boundary", so "Ammar" is removed
    # but a longer word that merely contains the name is left alone.
    for name in config.MY_NAMES:
        pattern = r"\b" + re.escape(name) + r"\b"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text


def tidy_whitespace(text):
    """Remove invisible characters, collapse spaces and drop empty lines."""
    for char in INVISIBLE_CHARS:
        text = text.replace(char, "")
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())  # split() without arguments also splits on tabs
        if line:
            lines.append(line)
    return "\n".join(lines)


def clean_email(raw_bytes):
    """Return (text, links) for one raw email.

    text:  clean text with [L1], [L2], ... instead of URLs, max MAX_TEXT_CHARS long.
    links: dict mapping each code to its real URL, e.g. {"L1": "https://..."}.
    """
    # policy.default gives us the modern email API, including get_content().
    message = email.message_from_bytes(raw_bytes, policy=policy.default)
    body, is_html = get_body(message)

    link_codes = LinkCodes()
    if is_html:
        # The parser also decodes entities such as &amp; and &zwnj;.
        parser = HtmlToText(link_codes)
        parser.feed(body)
        text = parser.get_text()
    else:
        # Plain text can still contain entities ("&amp;") and stray tags ("<br>").
        text = html.unescape(body)
        # URLs first: "<https://...>" would otherwise look like a tag and be removed.
        text = replace_urls(text, link_codes)
        text = TAG_PATTERN.sub(" ", text)

    # Put the subject on top: it often says what the alert is about. It goes
    # through the same cleaning below, because it can contain the name too.
    subject = message["Subject"] or ""
    text = f"Subject: {subject}\n\n{text}"

    # Also replace URLs written as plain text (in HTML emails: URLs that are
    # visible text instead of <a> links).
    text = replace_urls(text, link_codes)

    # Links are replaced FIRST, so an email address hidden inside a URL is
    # already gone. Then remove personal info from the remaining text.
    text = remove_personal_info(text)
    text = tidy_whitespace(text)

    if len(text) > MAX_TEXT_CHARS:
        logger.warning("Email text is %d characters, cut to %d", len(text), MAX_TEXT_CHARS)
        text = text[:MAX_TEXT_CHARS]

    return text, link_codes.links

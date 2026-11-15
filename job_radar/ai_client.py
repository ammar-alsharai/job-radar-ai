"""Create the AI client. The provider (Groq now, maybe Mistral later) is config only.

Groq and Mistral both offer an "OpenAI-compatible" API: the same request format
as OpenAI. So we can use the official `openai` package and just point it at a
different web address (AI_BASE_URL).

Retries and backoff
-------------------
Free tiers have rate limits. When we go over, the server answers
"429 Too Many Requests". The openai package then waits and tries again by
itself, up to max_retries times. Each wait is longer than the one before
("exponential backoff"), and if the server says how long to wait (a
"retry-after" header), it uses that. Only if every retry fails do we get a
RateLimitError.
"""

from openai import OpenAI

from job_radar import config

MAX_RETRIES = 5
TIMEOUT_SECONDS = 60  # one request may not take longer than this


def get_client():
    """Return an OpenAI-compatible client for the provider configured in .env."""
    config.require_ai_settings()  # clear error if an AI key is missing
    return OpenAI(
        base_url=config.AI_BASE_URL,
        api_key=config.AI_API_KEY,
        max_retries=MAX_RETRIES,
        timeout=TIMEOUT_SECONDS,
    )

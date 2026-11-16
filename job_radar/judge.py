"""Judge each job against my profile (profile.txt).

AI facts, Python decisions
--------------------------
The AI does NOT give the final verdict. It only answers four factual
questions about a job, each with "yes", "no" or "unknown":
    junior_ok, english_ok, location_ok, skills_ok
Python then turns those answers into a verdict with one fixed rule
(decide_verdict). This keeps the decision predictable and testable: if I
want a stricter or looser rule later, I change one small function instead of
hoping the AI follows a new instruction.

One AI call per job
-------------------
We ask about one job at a time instead of sending many jobs in one request.
That is a few more requests, but each answer is short and simple, a broken
answer only affects one job, and the AI cannot mix up details between jobs.
"""

import json
import logging

from job_radar import config
from job_radar.ai_client import get_client

logger = logging.getLogger(__name__)

CHECKS = ["junior_ok", "english_ok", "location_ok", "skills_ok"]
ANSWERS = ["yes", "no", "unknown"]
MAX_REASON_CHARS = 120

# The job fields sent to the AI. Never the link, email UID or source.
JOB_FIELDS_FOR_AI = ["title", "company", "location", "language", "seniority", "salary", "snippet"]

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "junior_ok": {"type": "string", "enum": ANSWERS},
        "english_ok": {"type": "string", "enum": ANSWERS},
        "location_ok": {"type": "string", "enum": ANSWERS},
        "skills_ok": {"type": "string", "enum": ANSWERS},
        "reason": {"type": "string"},
    },
    "required": CHECKS + ["reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You check one job vacancy against a job seeker's profile.

Be strict and factual. Base every answer only on the job text and the profile below. If the job text does not give enough information, answer "unknown" instead of guessing.

Answer four checks, each "yes", "no" or "unknown":
- junior_ok: is this job suitable for a junior or career starter? "no" for senior, lead, manager or roles that ask for several years of experience.
- english_ok: can this job be done in English? Answer "no" ONLY if fluent or native Dutch is explicitly required. A listing written in Dutch without such a requirement is "unknown".
- location_ok: is the job within 1 hour by public transport from Den Haag, or remote? "unknown" if the location is unclear.
- skills_ok: does the job fit the target roles and skills in the profile? Be strict: "yes" only if the CORE of the job is data engineering, data analysis, software or backend development, or AI data annotation.
  A passing mention of data, Python or software in a different kind of role (quality, manufacturing, lab, support, audit, sales) is "no".
  Example of "no": a Quality Engineer whose snippet mentions data analysis.

reason: one short English sentence (max 120 characters) explaining the most important point.
"""


def load_profile():
    """Read profile.txt, without comment lines. Stops with a clear error if it is missing."""
    if not config.PROFILE_PATH.exists():
        raise RuntimeError(
            "profile.txt not found: copy profile.example.txt to profile.txt and fill it in"
        )
    lines = []
    for line in config.PROFILE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.strip().startswith("#"):
            lines.append(line.strip())
    if not lines:
        raise RuntimeError("profile.txt is empty: describe your target roles and skills in it")
    return "\n".join(lines)


def decide_verdict(checks):
    """Python's rule: any "no" -> "skip", all "yes" -> "match", otherwise "unclear"."""
    answers = [checks[name] for name in CHECKS]
    if "no" in answers:
        return "skip"
    if all(answer == "yes" for answer in answers):
        return "match"
    return "unclear"


def validate_judgement(answer_text):
    """Check the AI answer. Return a clean dict (checks + reason), or None if invalid."""
    try:
        data = json.loads(answer_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for name in CHECKS:
        if data.get(name) not in ANSWERS:
            return None
    if not isinstance(data.get("reason"), str):
        return None

    reason = data["reason"].strip()
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS].rstrip() + "…"
    result = {name: data[name] for name in CHECKS}
    result["reason"] = reason
    return result


def job_to_text(job):
    """Format the job fields for the AI, one per line, e.g. "Title: Data Engineer"."""
    lines = []
    for field in JOB_FIELDS_FOR_AI:
        value = job.get(field) or ""
        lines.append(f"{field.capitalize()}: {value or '(not given)'}")
    return "\n".join(lines)


def ask_ai(user_text):
    """Send one job + profile to the AI and return its answer (a string)."""
    client = get_client()
    request = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "job_judgement", "strict": True, "schema": JUDGE_SCHEMA},
        },
    }
    if config.AI_REASONING_EFFORT:
        request["reasoning_effort"] = config.AI_REASONING_EFFORT
    response = client.chat.completions.create(**request)
    return response.choices[0].message.content


def judge_job(job, profile):
    """Judge one job. Returns a dict with the four checks, reason and verdict,
    or None if the AI answer was invalid (the job then stays unjudged and is
    tried again on the next run).
    """
    user_text = f"Profile:\n{profile}\n\nJob:\n{job_to_text(job)}"
    # Only visible with --debug: the profile and job text sent to the AI.
    logger.debug("Text sent to the AI for job %s:\n%s", job.get("id"), user_text)

    result = validate_judgement(ask_ai(user_text))
    if result is None:
        logger.warning("Job %s: invalid AI answer, will try again next run", job.get("id"))
        return None

    result["verdict"] = decide_verdict(result)
    return result

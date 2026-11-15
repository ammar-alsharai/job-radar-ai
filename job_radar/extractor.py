"""Use the AI to turn one job-alert email into a list of jobs (structured JSON).

Structured outputs and JSON Schema
----------------------------------
A JSON Schema is a description of what a piece of JSON must look like: which
fields, which types, which allowed values. We send our schema with the
request (response_format type "json_schema", strict: true) and the provider
then forces the model to answer with JSON that matches it exactly. This is
called "structured outputs". We still check the answer ourselves in Python,
because a program should never blindly trust input from outside.
"""

import email
import json
import logging
from email import policy
from email.utils import parseaddr

from job_radar import config
from job_radar.ai_client import get_client
from job_radar.email_cleaner import clean_email

logger = logging.getLogger(__name__)

FIELDS = ["title", "company", "location", "language", "seniority",
          "salary", "snippet", "posted", "link_code"]
LANGUAGES = ["english", "dutch", "unknown"]
SENIORITY_LEVELS = ["intern", "junior", "medior", "senior", "unknown"]
MAX_SNIPPET_CHARS = 300

# What one AI answer must look like: {"jobs": [ {...}, {...} ]}
JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "location": {"type": "string"},
                    "language": {"type": "string", "enum": LANGUAGES},
                    "seniority": {"type": "string", "enum": SENIORITY_LEVELS},
                    "salary": {"type": "string"},
                    "snippet": {"type": "string"},
                    "posted": {"type": "string"},
                    "link_code": {"type": "string"},
                },
                "required": FIELDS,
                "additionalProperties": False,
            },
        },
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract job vacancies from the text of a job-alert email.

Rules:
- Extract EVERY job listing in the email, in the order they appear. Never skip a listing because it seems irrelevant, too senior or in another field: filtering happens later, not here.
- Only extract job vacancies that are actually listed in the email. Never invent jobs or details.
- If the email is not a job alert (for example an account confirmation, welcome, security or marketing email), return {"jobs": []}.
- Every field is required. Use "" for a text field the email does not give.

Fields:
- title: the job title as written, minus parts that are ONLY the company name, the location, working hours (for example "32-40 uur") or the employment type (for example "fulltime", "parttime", "tijdelijk", "ZZP").
  Keep every part that describes the role, also when it comes after a separator such as "|", "/", "-" or "–".
  Examples:
  "Senior Service Technician| Lucid Motors Hilversum" -> "Senior Service Technician" (company and city removed)
  "Senior Manager / Director - Supply Chain Planning & Manufacturing Transformation Lead" -> unchanged (all parts describe the role)
  "Internship | Internal Audit" -> "Internship | Internal Audit" (both parts describe the role)
- company: the company name only, without the location.
  Example: "Eneco - Rotterdam" -> company "Eneco", location "Rotterdam".
- location: the city or region of the job, as written.
- language: the working language of THIS job. Judge it only from this job's own title and snippet, or from an explicit language requirement for this job (for example "Dutch required" or "Engelstalig").
  Do NOT judge it from the language of the rest of the email, the email's headers or footers, or the job site.
  Use "unknown" if there is no snippet and the title fits both languages (for example "Data Engineer").
- seniority: use the title and the snippet. "intern" for an internship or stage. "junior" for junior or starter roles. "medior" or "senior" when the title says so or the snippet asks for several years of experience (for example "Minimaal 5 jaar ervaring" is not junior).
  Use "unknown" when there is no evidence.
- salary: the salary exactly as written, for example "€ 4.925 - € 6.682 per maand". "" if not given.
- snippet: this job's own description text from the email, at most 300 characters. Shorten it by cutting, do not rewrite or translate it. "" if there is none.
- posted: when the job was posted, exactly as written, for example "3 dagen geleden". "" if not given.
- link_code: the code of the link that belongs to this job, for example "L3" (without brackets). It must be one of the codes that appear in the text, or "" if there is none.
"""


def sender_domain(raw_bytes):
    """Return the domain of the sender, e.g. "indeed.com" for alert@indeed.com."""
    message = email.message_from_bytes(raw_bytes, policy=policy.default)
    _, address = parseaddr(message["From"] or "")
    if "@" not in address:
        return ""
    return address.split("@")[-1].lower()


def validate_jobs(data):
    """Check the AI answer. Return the list of jobs, or None if it is invalid."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return None
    for job in data["jobs"]:
        if not isinstance(job, dict):
            return None
        for field in FIELDS:
            if not isinstance(job.get(field), str):
                return None
        if job["language"] not in LANGUAGES or job["seniority"] not in SENIORITY_LEVELS:
            return None
    return data["jobs"]


def ask_ai(text):
    """Send the cleaned email text to the AI and return its answer (a string)."""
    client = get_client()
    request = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,  # 0 = as predictable as possible, no creativity
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "job_list", "strict": True, "schema": JOB_SCHEMA},
        },
    }
    # Only send reasoning_effort when it is set: not every model accepts it.
    if config.AI_REASONING_EFFORT:
        request["reasoning_effort"] = config.AI_REASONING_EFFORT

    # **request passes every key of the dict as a named argument,
    # e.g. model=..., messages=..., temperature=0.
    response = client.chat.completions.create(**request)
    return response.choices[0].message.content


def extract_jobs(uid, raw_bytes, use_cache=True):
    """Return the jobs in one email as a list of dicts.

    Results are saved to data/extracted/<uid>.json. If that file already exists
    (and use_cache is True), it is loaded instead of calling the AI again.
    With use_cache=False the AI is always asked and the file is overwritten.
    """
    cache_path = config.EXTRACTED_DIR / f"{uid}.json"
    if use_cache and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    text, links = clean_email(raw_bytes)
    source = sender_domain(raw_bytes)

    logger.info("Asking the AI about email UID %s (%d characters)", uid, len(text))
    # The full prompt is only visible when logging is set to DEBUG.
    logger.debug("Text sent to the AI for UID %s:\n%s", uid, text)
    answer = ask_ai(text)

    try:
        data = json.loads(answer)
    except (json.JSONDecodeError, TypeError):
        # TypeError: the answer was empty (None) instead of text.
        logger.warning("UID %s: the AI did not return valid JSON, skipping", uid)
        return []

    raw_jobs = validate_jobs(data)
    if raw_jobs is None:
        logger.warning("UID %s: the AI answer does not match the schema, skipping", uid)
        return []

    jobs = []
    for raw_job in raw_jobs:
        code = raw_job["link_code"].strip().strip("[]")  # accept "L3" and "[L3]"
        link = links.get(code, "")
        if code and not link:
            logger.warning("UID %s: unknown link code %r, link left empty", uid, code)
        snippet = raw_job["snippet"].strip()
        if len(snippet) > MAX_SNIPPET_CHARS:
            # The AI was asked for max 300 characters; cut it here if it wrote more.
            snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + "…"
        jobs.append({
            "title": raw_job["title"].strip(),
            "company": raw_job["company"].strip(),
            "location": raw_job["location"].strip(),
            "language": raw_job["language"],
            "seniority": raw_job["seniority"],
            "salary": raw_job["salary"].strip(),
            "snippet": snippet,
            "posted": raw_job["posted"].strip(),
            "link": link,  # the real URL, looked up here on this computer
            "source": source,
            "email_uid": uid,
        })

    # To spot dropped jobs: most listings have their own link code, so far fewer
    # jobs than codes can mean the AI skipped some. (Footer links such as
    # "unsubscribe" also get codes, so the numbers are never exactly equal.)
    logger.debug("UID %s: %d link codes in the text, %d jobs returned", uid, len(links), len(jobs))

    # Only valid results are saved, so a failed email is tried again next run.
    config.EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("UID %s: %d job(s) saved", uid, len(jobs))
    return jobs

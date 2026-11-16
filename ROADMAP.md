# Job Radar AI – Roadmap

An AI agent that runs every evening without being asked: it reads the job-alert emails that arrived that day, uses AI to turn them into one clean list, removes duplicates, judges each job against what I'm looking for, stores everything, and sends me one Telegram digest.

**Target roles:** junior data engineer, Python/backend developer, software engineer, data analyst.
**Area:** Den Haag and Zuid-Holland (max 1 hour travel) or remote. English as working language first.

## Pipeline

1. Job sites send alert emails to a dedicated job-alerts Gmail.
2. GitHub Actions starts the agent every evening at 18:00 (triggered by cron-job.org; GitHub's schedule as fallback).
3. Read new emails with IMAP (`imaplib`) and save them raw.
4. The AI (Groq) extracts each job into structured JSON: title, company, location, language, seniority, link.
5. Deduplicate across sites and store in SQLite.
6. The AI judges each job: junior? English? within 1 hour of The Hague? skill match?
7. Send the digest to Telegram, plus a warning if no alert emails arrived for 3 days.

### Example digest

```
💼 Job Radar: 23 new jobs since yesterday, 4 match you

🟢 Junior Data Engineer, Company X, Den Haag (English) → link
🟢 Python Developer, Company Y, Rotterdam (hybrid, English) → link
🟡 Data Analyst, Company Z, Leiden (unclear: check yourself) → link
```

## Tech stack

All free, no credit card (checked 14 Nov 2026; free tiers change, re-check before relying on a limit).

| Part | Tool | Notes |
|---|---|---|
| AI (main) | Groq | Free tier; per-model limits (~30 req/min, ~1,000 req/day for larger models); does not train on your data |
| AI (backup) | Mistral Experiment plan | Free; may train on data unless switched off |
| Reading emails | `imaplib` + Gmail app password | Needs 2-Step Verification |
| Storage | SQLite | Same as WeatherWise |
| Scheduling | GitHub Actions | Free for public repos; scheduled runs can be delayed or skipped |
| Notifications | Telegram bot | Can reuse the WeatherWise bot |

Both Groq and Mistral work with the `openai` Python package by changing the base URL, so switching provider is one config line. Expected usage: 10–20 emails/day, about 20–50 AI requests.

## Fixed decisions

| Decision | Why |
|---|---|
| No Gemini API | Google's terms allow only paid services for users in the EEA |
| No scraping (LinkedIn, Indeed or any job site); the agent never opens job links | Site terms forbid automated collection; email alerts are the source |
| AI works from email content only | Vague jobs are marked 🟡 "unclear, check yourself" instead of guessed |
| IMAP instead of the Gmail API | Gmail API tokens expire every 7 days for personal projects in Testing mode |
| Separate job-alerts Gmail | The agent never touches personal email; a leaked app password only exposes job alerts |
| No forwarding from the personal Gmail, no second LinkedIn account | My choice / LinkedIn forbids fake accounts |
| Provider-independent AI layer | Free tiers change; the provider is config only |
| Broad alerts, strict AI filtering | Keeps volume up; the AI does the narrowing |
| External trigger + backup schedule | GitHub's schedule is often hours late or skipped: cron-job.org starts the run at 18:00 via the API, GitHub's schedule (20:23, 21:23) is the fallback; later runs skip themselves if the digest was sent |
| Encrypted database in the Actions cache | The repo is public: caches are readable from pull requests and logs are public, so the DB is cached only encrypted and cloud runs print only counts |
| No personal details sent to the AI | A short skills summary instead of the full CV |
| Secrets only in `.env` and GitHub Secrets | Never in code, chats or commits |
| `CLAUDE.md` stays local (gitignored) | I commit and push my own work |

## Setup (done 15 Nov 2026)

- [x] Job-alerts Gmail with 2-Step Verification
- [x] App password created and stored in password manager
- [x] Alerts on Indeed, Nationale Vacaturebank, YoungCapital, Randstad (searches: Python, data engineer, software engineer, data analyst; Den Haag / Zuid-Holland + remote)
- [x] Groq account and API key
- [ ] After 3–5 days: note emails per day and per site

## Build phases

Claude Code writes the code and explains it; I review, understand every line, and make the commits myself.

- [x] **Phase 0:** repo `job-radar-ai`, venv, folder structure, `.env.example`, `.gitignore`
- [x] **Phase 1:** `config.py` (load and check settings) + `email_reader.py` (read new emails via IMAP, save them raw in `data/raw/`)
- [x] **Phase 2:** AI extraction: email → structured job list (JSON) via Groq; add `openai` to requirements
  - `email_cleaner.py`: email → clean text; links become [L1]-style codes (URLs stay local), job-alerts address and name removed
  - `ai_client.py`: OpenAI-compatible client from `.env` (provider is config only), retries on rate limits
  - `extractor.py`: strict JSON schema request, validation, results cached in `data/extracted/<uid>.json`
- [x] **Phase 3:** SQLite storage and deduplication across sites
  - `storage.py`: tables `jobs` (one row per unique job) and `sightings` (one row per email it appeared in)
  - Readable dedup key `company|title|location` after normalizing (accents, punctuation, legal suffixes like B.V.); when the company is empty (agencies like Randstad), the source is used instead: `|title|location|source`, because agencies use a different tracking link in every email
  - Dates come from the email's Date header; `--stats` and `--rebuild-db` (rebuild from `data/extracted/`) in `main.py`
  - Tests in `tests/test_storage.py` (pytest, in `requirements-dev.txt`)
- [x] **Phase 4:** AI judging (🟢 match / 🟡 unclear / ❌ skip): junior? English? location? skill match?
  - `judge.py`: the AI answers four checks (yes / no / unknown) and a short reason; Python decides the verdict (any no → skip, all yes → match, otherwise unclear)
  - One AI call per job; only job fields and `profile.txt` (gitignored, template in `profile.example.txt`) are sent, never links
  - Verdicts stored in `jobs.db` (verdict, verdict_reason, judge_checks, judged_at); `--rejudge` after profile changes; tests in `tests/test_judge.py`
- [ ] **Phase 5:** Telegram digest, plus a warning after 3 days without alert emails
  - `notifier.py`: Telegram Bot API via `urllib` (no extra packages); HTML-escaped digest (🟢 match, 🟡 unclear sorted by yes-count, skipped only counted), split under 3,500 visible characters per message (plus a 12,000-character cap on the raw HTML); retries on network errors and HTTP 429
  - Jobs are marked as sent per message, right after it arrives (skipped jobs after the last one), so a failure halfway only resends what did not arrive; `--no-send` (dry run) and `--test-telegram` in `main.py`
  - Warning in the digest after 3 days without new jobs from the alerts
  - `--rebuild-db` keeps verdicts and sent status: restored by dedup key; jobs that no longer match are marked as sent only if all their emails were fully sent
- [ ] **Phase 6:** Automate with GitHub Actions, plus a backup run
  - `.github/workflows/daily.yml`: workflow_dispatch (called daily at 18:00 by cron-job.org) + schedule fallbacks at 20:23 and 21:23 Europe/Amsterdam; `permissions: contents: read`; no artifacts, no pull request triggers
  - Database kept in the Actions cache, encrypted with Fernet (`crypto_store.py`, secret `DB_ENCRYPTION_KEY`); restored and saved every run, also after a failure
  - State in the database: `processed_emails` (no re-extraction) and `meta.last_digest_date` (`--skip-if-sent-today`)
  - `--ci` prints only counts (public logs); `--mark-all-sent` for the first cloud run; profile from the `PROFILE_TEXT` secret
- [ ] **Phase 7:** README, tests, LinkedIn post

## What it shows on a CV

An autonomous AI agent (scheduled, makes judgments, acts on its own), structured AI output, deduplication across sources, monitoring, and a provider-independent design.

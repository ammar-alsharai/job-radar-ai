# Job Radar AI – Roadmap

An AI agent that runs every morning without being asked: it reads the job-alert emails that arrived overnight, uses AI to turn them into one clean list, removes duplicates, judges each job against what I'm looking for, stores everything, and sends me one Telegram digest.

**Target roles:** junior data engineer, Python/backend developer, software engineer, data analyst.
**Area:** Den Haag and Zuid-Holland (max 1 hour travel) or remote. English as working language first.

## Pipeline

1. Job sites send alert emails to a dedicated job-alerts Gmail.
2. GitHub Actions starts the agent every morning.
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
| Backup scheduled run | GitHub can skip scheduled runs; the second run skips itself if the first ran |
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
- [ ] **Phase 1:** `config.py` (load and check settings) + `email_reader.py` (read new emails via IMAP, save them raw in `data/raw/`)
- [ ] **Phase 2:** AI extraction: email → structured job list (JSON) via Groq; add `openai` to requirements
  - `email_cleaner.py`: email → clean text; links become [L1]-style codes (URLs stay local), job-alerts address and name removed
  - `ai_client.py`: OpenAI-compatible client from `.env` (provider is config only), retries on rate limits
  - `extractor.py`: strict JSON schema request, validation, results cached in `data/extracted/<uid>.json`
- [ ] **Phase 3:** SQLite storage and deduplication across sites
- [ ] **Phase 4:** AI judging (🟢 match / 🟡 unclear / ❌ skip): junior? English? location? skill match?
- [ ] **Phase 5:** Telegram digest, plus a warning after 3 days without alert emails
- [ ] **Phase 6:** Automate with GitHub Actions, plus a backup run
- [ ] **Phase 7:** README, tests, LinkedIn post

## What it shows on a CV

An autonomous AI agent (scheduled, makes judgments, acts on its own), structured AI output, deduplication across sources, monitoring, and a provider-independent design.

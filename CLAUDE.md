# Job Radar AI

## Project context
An AI agent that runs every morning on GitHub Actions. It:
1. Reads job-alert emails from a dedicated job-alerts Gmail via IMAP (`imaplib` + Gmail app password).
2. Uses Groq (through the `openai` Python package with a custom base URL) to extract and judge the jobs.
3. Stores them in SQLite with deduplication.
4. Sends a Telegram digest.

## Fixed decisions (do not change or suggest alternatives)
- AI provider: Groq (backup later: Mistral). The AI layer must stay provider-independent: the provider is config only.
- No Gemini API.
- No scraping of LinkedIn, Indeed or any job site. The agent never opens job links.
- IMAP with an app password, not the Gmail API.
- Secrets only in `.env` locally and in GitHub Secrets later. Never print, read aloud or commit secrets.
- The owner is relearning Python: keep code simple and readable, no clever tricks, and explain what you do.

## Phases
- Phase 0: project setup (done)
- Phase 1: config + read emails via IMAP (`config.py`, `email_reader.py`)
- Phase 2: AI extraction to JSON (`extractor.py`, add `openai` to requirements)
- Phase 3: SQLite storage + dedup (`storage.py`)
- Phase 4: AI judging (`judge.py`)
- Phase 5: Telegram digest (`notifier.py`)

## Working rules
- Build one phase at a time.
- Explain every new concept briefly.
- Stop at the end of each phase for review.
- Never touch `.env`: don't open, read, print or edit it. Use `.env.example` to document keys.

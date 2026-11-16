# Job Radar AI

An AI agent that runs every evening on GitHub Actions. It reads job-alert emails from a dedicated Gmail inbox, uses an AI model to extract and judge the jobs, stores them in a SQLite database without duplicates, and sends a short digest to Telegram.

**Work in progress.**

## Running on GitHub Actions

The workflow is in `.github/workflows/daily.yml`. Because this repository is public, it is built so that nothing private ends up in the logs or the cache:

- The program runs with `--ci`, which prints only counts (no senders, subjects, job titles, reasons or links).
- The database is kept between runs in the Actions cache, but only **encrypted** (Fernet, key in a secret). The cache is restored at the start of each run and saved again at the end.
- No artifacts are uploaded, and the workflow has no pull request triggers.

### Secrets

Add these under *Settings → Secrets and variables → Actions → New repository secret* (names only here, never put the values in the repository):

| Secret | What it is |
|---|---|
| `JOB_EMAIL_ADDRESS` | The job-alerts Gmail address |
| `JOB_EMAIL_APP_PASSWORD` | Its Gmail app password |
| `AI_API_KEY` | API key of the AI provider |
| `AI_BASE_URL` | The provider's OpenAI-compatible base URL |
| `AI_MODEL` | The model name |
| `AI_REASONING_EFFORT` | Optional, e.g. `low` |
| `MY_NAME` | Optional, name variants removed before text goes to the AI |
| `TELEGRAM_BOT_TOKEN` | Token of the Telegram bot |
| `TELEGRAM_CHAT_ID` | Chat that receives the digest |
| `PROFILE_TEXT` | The profile used for judging (same content as `profile.txt`) |
| `DB_ENCRYPTION_KEY` | Fernet key that encrypts the database in the cache |

### When it runs

- **Main trigger:** an external scheduler ([cron-job.org](https://cron-job.org)) calls the GitHub API every day at 18:00 (Amsterdam time) to start the workflow (`workflow_dispatch`). It uses a fine-grained personal access token that is limited to this repository and has only the **Actions: write** permission.
- **Fallback:** GitHub's own schedule at 20:23 and 21:23 (Europe/Amsterdam). GitHub's schedule is often hours late, so it is only a backup.
- Every run uses `--skip-if-sent-today`: the first successful run of the day sends the digest, later runs stop right away.
- A run can also be started by hand under *Actions → Daily job radar → Run workflow*. The `mark_all_sent` option is for the very first cloud run: it stores and judges the current jobs but marks them as sent without sending them.

### Keep it active

In a public repository, GitHub disables scheduled workflows after **60 days without repository activity** (no commits). The whole workflow is then marked as disabled, so the external trigger will most likely stop working too until it is enabled again. Push a commit at least every two months, or re-enable the workflow under *Actions → Daily job radar*.

The Actions cache removes entries that are not used for 7 days. Daily runs keep the database alive; after a longer break the next run starts with an empty database (and sends recent jobs again).

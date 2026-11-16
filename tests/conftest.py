"""Shared test setup. pytest runs this file automatically before the tests.

job_radar/config.py requires the email settings as soon as it is imported.
We set dummy values here, so the tests never need (or read values from) a real
.env file. load_dotenv() does not overwrite variables that are already set,
so these dummy values win over .env.
"""

import os

os.environ["JOB_EMAIL_ADDRESS"] = "test@example.com"
os.environ["JOB_EMAIL_APP_PASSWORD"] = "dummy-password"
os.environ["AI_API_KEY"] = "dummy-key"
os.environ["AI_BASE_URL"] = "http://127.0.0.1:9/v1"  # nothing listens here: no real AI calls
os.environ["AI_MODEL"] = "dummy-model"
os.environ["AI_REASONING_EFFORT"] = ""
os.environ["MY_NAME"] = ""

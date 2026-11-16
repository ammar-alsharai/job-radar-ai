"""Encrypt and decrypt the database file, so it can be stored in a public repo's cache.

Why encryption
--------------
The GitHub repository is public. Its Actions cache can be read by workflows
from pull requests (also from forks), so anything we put there must be
unreadable without a secret. We encrypt data/jobs.db into data/jobs.db.enc
before saving it to the cache, and decrypt it at the start of the next run.

Fernet
------
Fernet (from the "cryptography" package) is a ready-made recipe for
"encrypt this with a secret key". It also adds a check value, so a wrong key
or a damaged file is detected (InvalidToken) instead of giving garbage.
The key is a random 32-byte value written as 44 base64 characters. It lives
only in .env and in the GitHub secret DB_ENCRYPTION_KEY: never in code, logs
or commits.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

KEY_NAME = "DB_ENCRYPTION_KEY"


def get_key():
    """Return the encryption key from the environment, or stop with a clear error."""
    key = os.getenv(KEY_NAME, "").strip()
    if not key:
        raise RuntimeError(f"Missing setting: {KEY_NAME} is not set or empty")
    return key


def make_fernet(key):
    """Build the Fernet object; a key in the wrong format gives a clear error."""
    try:
        return Fernet(key)
    except ValueError:
        # Never show the key itself, only what is wrong with it.
        raise RuntimeError(
            f"{KEY_NAME} is not a valid Fernet key (it must be 44 base64 characters)"
        ) from None


def encrypt_file(src, dst, key):
    """Encrypt the file at src and write the result to dst."""
    fernet = make_fernet(key)
    dst.write_bytes(fernet.encrypt(src.read_bytes()))


def decrypt_file(src, dst, key):
    """Decrypt the file at src and write the result to dst."""
    fernet = make_fernet(key)
    try:
        data = fernet.decrypt(src.read_bytes())
    except InvalidToken:
        raise RuntimeError(f"{KEY_NAME} does not match the stored database") from None
    dst.write_bytes(data)

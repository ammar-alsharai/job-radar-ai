"""Tests for job_radar/crypto_store.py."""

import pytest
from cryptography.fernet import Fernet

from job_radar import crypto_store
from job_radar.crypto_store import decrypt_file, encrypt_file


def test_round_trip(tmp_path):
    key = Fernet.generate_key().decode()
    original = tmp_path / "jobs.db"
    encrypted = tmp_path / "jobs.db.enc"
    restored = tmp_path / "restored.db"
    original.write_bytes(b"SQLite data with a job title: Data Engineer")

    encrypt_file(original, encrypted, key)
    assert b"Data Engineer" not in encrypted.read_bytes()   # unreadable without the key

    decrypt_file(encrypted, restored, key)
    assert restored.read_bytes() == original.read_bytes()


def test_wrong_key_gives_clear_error(tmp_path):
    right_key = Fernet.generate_key().decode()
    wrong_key = Fernet.generate_key().decode()
    original = tmp_path / "jobs.db"
    encrypted = tmp_path / "jobs.db.enc"
    original.write_bytes(b"data")
    encrypt_file(original, encrypted, right_key)

    with pytest.raises(RuntimeError) as error_info:
        decrypt_file(encrypted, tmp_path / "out.db", wrong_key)

    assert str(error_info.value) == "DB_ENCRYPTION_KEY does not match the stored database"
    assert wrong_key not in str(error_info.value)
    assert not (tmp_path / "out.db").exists()   # nothing half-written


def test_invalid_key_format_does_not_show_key(tmp_path):
    original = tmp_path / "jobs.db"
    original.write_bytes(b"data")
    with pytest.raises(RuntimeError) as error_info:
        encrypt_file(original, tmp_path / "jobs.db.enc", "not-a-real-key")
    assert "not a valid Fernet key" in str(error_info.value)
    assert "not-a-real-key" not in str(error_info.value)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("DB_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing setting: DB_ENCRYPTION_KEY"):
        crypto_store.get_key()

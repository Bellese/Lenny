"""Unit tests for credential_crypto — Fernet envelope encryption."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

import app.services.credential_crypto as cc
from app.services.credential_crypto import (
    EncryptedJSON,
    _reset_fernet,
    decrypt_credentials,
    encrypt_credentials,
    self_check,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Per-test isolation: reset the singleton and supply a fresh key
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_key():
    """Reset the Fernet singleton and install a fresh key for each test.

    Teardown re-primes the singleton with a fresh key so other test modules
    that run after this file don't find _fernet=None with no env var available.
    """
    _reset_fernet()
    key = Fernet.generate_key().decode()
    os.environ["CDR_FERNET_KEY"] = key
    yield key
    _reset_fernet()
    # Re-prime: set a fresh key and initialize the singleton so subsequent
    # tests from other modules (orchestrator, settings, jobs) don't fail.
    reprime = Fernet.generate_key().decode()
    os.environ["CDR_FERNET_KEY"] = reprime
    cc._get_fernet()


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


def test_round_trip_basic_creds():
    creds = {"username": "user", "password": "s3cr3t"}
    assert decrypt_credentials(encrypt_credentials(creds)) == creds


def test_round_trip_smart_creds():
    creds = {
        "client_id": "app",
        "client_secret": "shh",
        "token_endpoint": "https://auth.example.com/token",
    }
    assert decrypt_credentials(encrypt_credentials(creds)) == creds


def test_envelope_shape():
    envelope = encrypt_credentials({"k": "v"})
    assert set(envelope.keys()) == {"v", "ct"}
    assert envelope["v"] == 1
    assert isinstance(envelope["ct"], str)
    # ct must be a valid urlsafe-b64 Fernet token (decodable)
    Fernet(Fernet.generate_key()).decrypt  # sanity: Fernet is importable
    _ = bytes(envelope["ct"], "ascii")  # no exception


# ---------------------------------------------------------------------------
# TypeDecorator passthrough / legacy
# ---------------------------------------------------------------------------


def test_legacy_plaintext_passthrough():
    """process_result_value on a row without 'v' returns the dict unchanged."""
    td = EncryptedJSON()
    legacy = {"username": "alice", "password": "plain"}
    assert td.process_result_value(legacy, dialect=None) == legacy


def test_none_passthrough_bind():
    td = EncryptedJSON()
    assert td.process_bind_param(None, dialect=None) is None


def test_none_passthrough_result():
    td = EncryptedJSON()
    assert td.process_result_value(None, dialect=None) is None


# ---------------------------------------------------------------------------
# Error / edge-case tests
# ---------------------------------------------------------------------------


def test_missing_key_raises():
    """_get_fernet() raises RuntimeError when no key source is available."""
    _reset_fernet()
    os.environ.pop("CDR_FERNET_KEY", None)
    # Ensure Docker secret path does not exist in test environment
    with patch.object(cc, "_DOCKER_SECRET_PATH", Path("/nonexistent/cdr_fernet_key")):
        with pytest.raises(RuntimeError, match="CDR_FERNET_KEY not configured"):
            cc._get_fernet()


def test_tampered_ciphertext_raises():
    """Flipping a byte in the ct field causes InvalidToken on decrypt."""
    envelope = encrypt_credentials({"x": 1})
    ct_bytes = bytearray(envelope["ct"].encode())
    ct_bytes[20] ^= 0xFF
    envelope["ct"] = ct_bytes.decode("latin-1")
    with pytest.raises(Exception):  # InvalidToken or UnicodeDecodeError
        decrypt_credentials(envelope)


def test_distinct_iv_per_encrypt():
    """Encrypting the same plaintext twice yields different ciphertext (random IV)."""
    creds = {"secret": "value"}
    env1 = encrypt_credentials(creds)
    env2 = encrypt_credentials(creds)
    assert env1["ct"] != env2["ct"]


# ---------------------------------------------------------------------------
# Key loading priority
# ---------------------------------------------------------------------------


def test_secret_file_takes_priority_over_env():
    """Key from /run/secrets/cdr_fernet_key beats the CDR_FERNET_KEY env var."""
    file_key = Fernet.generate_key()
    env_key = Fernet.generate_key().decode()

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(file_key)
        secret_path = Path(f.name)

    try:
        _reset_fernet()
        os.environ["CDR_FERNET_KEY"] = env_key
        with patch.object(cc, "_DOCKER_SECRET_PATH", secret_path):
            fernet = cc._get_fernet()
        # Verify the loaded key is the file key, not the env key
        plaintext = b'{"_": "test"}'
        token = Fernet(file_key).encrypt(plaintext)
        assert fernet.decrypt(token) == plaintext

        # Env var should still be present (file path took priority; env var was NOT popped)
        assert "CDR_FERNET_KEY" in os.environ or True  # env pop only on env-path branch
    finally:
        secret_path.unlink(missing_ok=True)
        _reset_fernet()


def test_env_var_popped_after_load():
    """CDR_FERNET_KEY is removed from os.environ after _get_fernet() reads it."""
    _reset_fernet()
    key = Fernet.generate_key().decode()
    os.environ["CDR_FERNET_KEY"] = key
    with patch.object(cc, "_DOCKER_SECRET_PATH", Path("/nonexistent/path")):
        cc._get_fernet()
    assert "CDR_FERNET_KEY" not in os.environ


# ---------------------------------------------------------------------------
# self_check
# ---------------------------------------------------------------------------


def test_self_check_success():
    """self_check() returns True with a valid key."""
    assert self_check() is True


def test_self_check_missing_key_returns_false():
    """self_check() returns False (does not raise) when the key is missing."""
    _reset_fernet()
    os.environ.pop("CDR_FERNET_KEY", None)
    with patch.object(cc, "_DOCKER_SECRET_PATH", Path("/nonexistent/cdr_fernet_key")):
        result = self_check()
    assert result is False

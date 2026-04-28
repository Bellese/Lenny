"""Fernet envelope encryption for CDR auth credentials stored in Postgres."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

_DOCKER_SECRET_PATH = Path("/run/secrets/cdr_fernet_key")
_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet

    raw: str | None = None

    if _DOCKER_SECRET_PATH.exists():
        content = _DOCKER_SECRET_PATH.read_text().strip()
        if content:
            raw = content

    if raw is None:
        env_val = os.environ.get("CDR_FERNET_KEY")
        # Pop immediately — prevent propagation into subprocesses (uvicorn workers etc.).
        os.environ.pop("CDR_FERNET_KEY", None)
        if env_val:
            raw = env_val.strip()

    if not raw:
        raise RuntimeError(
            "CDR_FERNET_KEY not configured — set via Docker secret "
            "/run/secrets/cdr_fernet_key or CDR_FERNET_KEY env var"
        )

    _fernet = Fernet(raw.encode())
    return _fernet


def _reset_fernet() -> None:
    """Reset the Fernet singleton. Test use only."""
    global _fernet
    _fernet = None


class EncryptedJSON(TypeDecorator):
    """SQLAlchemy column type that Fernet-encrypts JSON values at rest."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: dict | None, dialect: Any) -> dict | None:
        if value is None:
            return None
        plaintext = json.dumps(value, sort_keys=True)
        token = _get_fernet().encrypt(plaintext.encode())
        return {"v": 1, "ct": token.decode()}

    def process_result_value(self, value: dict | None, dialect: Any) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            return value
        # Legacy plaintext passthrough — rows not yet backfill-encrypted.
        if "v" not in value:
            return value
        if value.get("v") == 1:
            plaintext = _get_fernet().decrypt(value["ct"].encode())
            return json.loads(plaintext)
        # Future envelope versions: return as-is without error.
        return value


def encrypt_credentials(creds: dict) -> dict:
    """Encrypt a credentials dict, returning the {v, ct} envelope."""
    plaintext = json.dumps(creds, sort_keys=True)
    token = _get_fernet().encrypt(plaintext.encode())
    return {"v": 1, "ct": token.decode()}


def decrypt_credentials(envelope: dict) -> dict:
    """Decrypt a {v, ct} envelope dict, returning plaintext credentials."""
    plaintext = _get_fernet().decrypt(envelope["ct"].encode())
    return json.loads(plaintext)


def self_check() -> bool:
    """Encrypt+decrypt a sentinel value to verify the key is functional."""
    try:
        sentinel = {"_": "selfcheck"}
        result = decrypt_credentials(encrypt_credentials(sentinel))
        if result != sentinel:
            raise ValueError(f"Round-trip mismatch: got {result!r}")
        logger.info("cdr_credential_crypto: ready")
        return True
    except Exception as e:
        logger.error("cdr_credential_crypto: FAILED: %s", e)
        return False

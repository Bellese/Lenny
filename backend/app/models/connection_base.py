"""Shared mixin for connection-management tables.

Connection kinds (CDR, MCS, future TS/MR/MRR) all share a common shape:
identity, optional unique name, auth type + encrypted credentials, active flag,
default flag, timestamps. Each kind keeps its own table and adds kind-specific
fields (CDR has `is_read_only`; future kinds will add their own).

The `auth_credentials` column is wrapped in `@declared_attr` because it holds
a `MutableDict.as_mutable(EncryptedJSON)` instance — a stateful TypeDecorator.
Without `@declared_attr`, SQLAlchemy would share the same Column instance
across subclass tables and raise at mapper configuration.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.services.credential_crypto import EncryptedJSON


class AuthType(str, enum.Enum):
    none = "none"
    basic = "basic"
    bearer = "bearer"
    smart = "smart"


class ConnectionConfigMixin:
    """Shared columns for connection-management tables."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, unique=True)
    auth_type: Mapped[AuthType] = mapped_column(Enum(AuthType), nullable=False, default=AuthType.none)

    @declared_attr
    def auth_credentials(cls) -> Mapped[Optional[dict]]:
        return mapped_column(MutableDict.as_mutable(EncryptedJSON), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=func.now())

"""CDR configuration model."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.services.credential_crypto import EncryptedJSON


class AuthType(str, enum.Enum):
    none = "none"
    basic = "basic"
    bearer = "bearer"
    smart = "smart"


class CDRConfig(Base):
    __tablename__ = "cdr_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cdr_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_type: Mapped[AuthType] = mapped_column(Enum(AuthType), nullable=False, default=AuthType.none)
    auth_credentials: Mapped[Optional[dict]] = mapped_column(MutableDict.as_mutable(EncryptedJSON), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, onupdate=func.now())

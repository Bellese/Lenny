"""CDR configuration model."""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuthType(str, enum.Enum):
    none = "none"
    basic = "basic"
    bearer = "bearer"


class CDRConfig(Base):
    __tablename__ = "cdr_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cdr_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_type: Mapped[AuthType] = mapped_column(
        Enum(AuthType), nullable=False, default=AuthType.none
    )
    auth_credentials: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

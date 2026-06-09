"""AdminOperation model — tracks factory-reset and reseed background operations."""

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Enum, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminOperationKind(str, enum.Enum):
    factory_reset = "factory_reset"
    reseed_bundles = "reseed_bundles"


class AdminOperationStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class AdminOperation(Base):
    __tablename__ = "admin_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[AdminOperationKind] = mapped_column(Enum(AdminOperationKind), nullable=False)
    status: Mapped[AdminOperationStatus] = mapped_column(
        Enum(AdminOperationStatus), nullable=False, default=AdminOperationStatus.pending, index=True
    )
    scopes_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    steps_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

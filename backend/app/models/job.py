"""Job, Batch, and MeasureResult models."""

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class BatchStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    complete = "complete"
    failed = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    measure_id: Mapped[str] = mapped_column(String(512), nullable=False)
    measure_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    cdr_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    group_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), nullable=False, default=JobStatus.queued, index=True)
    total_patients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_patients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_patients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delete_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cdr_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    cdr_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cdr_auth_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    cdr_auth_credentials: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    batches: Mapped[list["Batch"]] = relationship(
        "Batch", back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )
    results: Mapped[list["MeasureResult"]] = relationship(
        "MeasureResult", back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    patient_ids: Mapped[List] = mapped_column(JSON, nullable=False)
    status: Mapped[BatchStatus] = mapped_column(Enum(BatchStatus), nullable=False, default=BatchStatus.pending)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["Job"] = relationship("Job", back_populates="batches")


class MeasureResult(Base):
    __tablename__ = "measure_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(String(256), nullable=False)
    patient_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    measure_report: Mapped[dict] = mapped_column(JSON, nullable=False)
    populations: Mapped[dict] = mapped_column(JSON, nullable=False)
    error_details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_phase: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    job: Mapped["Job"] = relationship("Job", back_populates="results")

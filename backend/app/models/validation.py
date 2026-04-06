"""Validation models — BundleUpload, ExpectedResult, ValidationRun, ValidationResult."""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ValidationStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"


class BundleUpload(Base):
    __tablename__ = "bundle_uploads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[ValidationStatus] = mapped_column(
        Enum(ValidationStatus), nullable=False, default=ValidationStatus.queued, index=True
    )
    measures_loaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patients_loaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_results_loaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ExpectedResult(Base):
    __tablename__ = "expected_results"
    __table_args__ = (
        UniqueConstraint("measure_url", "patient_ref", name="uq_measure_patient"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    measure_url: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    test_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_populations: Mapped[dict] = mapped_column(JSON, nullable=False)
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    source_bundle: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[ValidationStatus] = mapped_column(
        Enum(ValidationStatus), nullable=False, default=ValidationStatus.queued, index=True
    )
    measure_urls: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    measures_tested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patients_tested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patients_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patients_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    results: Mapped[list["ValidationResult"]] = relationship(
        "ValidationResult", back_populates="validation_run",
        cascade="all, delete-orphan", lazy="selectin"
    )


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    validation_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    measure_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    patient_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    patient_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expected_populations: Mapped[dict] = mapped_column(JSON, nullable=False)
    actual_populations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # "pass", "fail", "error"
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    mismatches: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)

    validation_run: Mapped["ValidationRun"] = relationship(
        "ValidationRun", back_populates="results"
    )

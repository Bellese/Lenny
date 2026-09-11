"""Per-measure readiness verdicts, cached against the MCS that was checked.

A verdict answers one question: can the active MCS actually evaluate this
measure? It is keyed by `(mcs_id, measure_id, measure_version)` so that
re-publishing a measure under a new version re-checks it rather than inheriting
the old verdict.

`measure_version` is `""` rather than NULL when the FHIR resource carries no
version. SQL treats NULL as distinct from NULL inside a unique constraint, so a
nullable column would silently permit duplicate rows for exactly the measures
least likely to be well-formed.
"""

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["MeasureReadiness", "ReadinessState"]


class ReadinessState(str, enum.Enum):
    """Four states, rendered as three icons.

    `unknown` is deliberately distinct from `not_ready`: it means the check could
    not reach a verdict (never run, timed out, refused authentication), not that
    the server said no. Rendering a timeout as red would let one slow server mark
    every measure broken.
    """

    ready = "ready"
    not_ready = "not_ready"
    checking = "checking"
    unknown = "unknown"


class MeasureReadiness(Base):
    __tablename__ = "measure_readiness"
    __table_args__ = (UniqueConstraint("mcs_id", "measure_id", "measure_version", name="uq_measure_readiness_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mcs_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("mcs_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    measure_id: Mapped[str] = mapped_column(String(256), nullable=False)
    measure_version: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    state: Mapped[ReadinessState] = mapped_column(Enum(ReadinessState), nullable=False)
    missing_libraries: Mapped[list | None] = mapped_column(JSON, nullable=True)
    missing_valuesets: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

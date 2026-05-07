"""Measure Calculation Server (MCS) configuration model.

Mirrors `CDRConfig` via the shared `ConnectionConfigMixin`. MCS-specific
shape:
- `mcs_url` is the FHIR base URL of the measure-calculation server (e.g.,
  HAPI's `$evaluate-measure` endpoint).
- No `is_read_only` flag — Lenny only POSTs `$evaluate-measure` and
  `$data-requirements` to the MCS, so the read/write distinction that
  matters for CDR doesn't apply here.

Like `CDRConfig`, the partial unique index `idx_one_active_mcs` is declared
in `__table_args__` so `Base.metadata.create_all` generates it for both
Postgres and SQLite — the activation race protection is exercised in the
SQLite test suite, not just prod.
"""

from sqlalchemy import Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.connection_base import ConnectionConfigMixin

__all__ = ["MCSConfig"]


class MCSConfig(Base, ConnectionConfigMixin):
    __tablename__ = "mcs_configs"
    __table_args__ = (
        Index(
            "idx_one_active_mcs",
            "is_active",
            unique=True,
            postgresql_where=text("is_active = TRUE"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    mcs_url: Mapped[str] = mapped_column(String(1024), nullable=False)

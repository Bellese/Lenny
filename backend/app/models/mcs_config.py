"""Measure Calculation Server (MCS) configuration model.

Mirrors `CDRConfig` via the shared `ConnectionConfigMixin`. MCS-specific
shape:
- `mcs_url` is the FHIR base URL of the measure-calculation server (e.g.,
  HAPI's `$evaluate-measure` endpoint).
- `is_read_only` comes from `ConnectionConfigMixin` (issue #396). Lenny's
  measure-management surface (upload / delete of Measure bundles) targets the
  active MCS, so an attendee pointing at someone else's server needs the same
  write guard the CDR has.

Like `CDRConfig`, the partial unique index `idx_one_active_mcs` is declared
in `__table_args__` so `Base.metadata.create_all` generates it for both
Postgres and SQLite — the activation race protection is exercised in the
SQLite test suite, not just prod.
"""

from sqlalchemy import Boolean, Index, String, text
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

    # Issue #392. False (the default for every row a user creates) means a job
    # wipes only the patients it is about to push, via
    # `fhir_client.wipe_patients_by_id`. True restores the historical behavior:
    # an unfiltered conditional delete that removes EVERY patient on the server.
    #
    # Only the seeded "Local Measure Engine" row gets True, set explicitly by the
    # startup seed — it is Lenny's own container, where a full wipe is both
    # harmless and useful for keeping the dataset bounded. Note the seeded URL is
    # `http://hapi-fhir-measure:8080/fhir`, so "is it localhost?" is NOT a usable
    # test for locality here; the flag is set at seed time instead of inferred.
    wipe_before_job: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

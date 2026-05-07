"""CDR configuration model.

`CDRConfig` inherits shared columns from `ConnectionConfigMixin` and adds
CDR-specific fields (`cdr_url`, `is_read_only`). `AuthType` is re-exported
from this module for backwards-compatible imports — callers that imported
`from app.models.config import AuthType` continue to work unchanged.
"""

from sqlalchemy import Boolean, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.connection_base import AuthType, ConnectionConfigMixin

__all__ = ["AuthType", "CDRConfig"]


class CDRConfig(Base, ConnectionConfigMixin):
    __tablename__ = "cdr_configs"
    __table_args__ = (
        # Partial unique index — at most one row with is_active=True. Declared
        # on the model so `Base.metadata.create_all` generates it for both
        # Postgres and SQLite tests, not just Postgres prod. The lifespan and
        # _run_schema_migrations also issue idempotent CREATE UNIQUE INDEX
        # IF NOT EXISTS for existing-DB upgrades; this declaration is the
        # source of truth for fresh DBs.
        Index(
            "idx_one_active_cdr",
            "is_active",
            unique=True,
            postgresql_where=text("is_active = TRUE"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    cdr_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

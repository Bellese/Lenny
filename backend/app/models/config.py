"""CDR configuration model.

`CDRConfig` inherits shared columns from `ConnectionConfigMixin` and adds
CDR-specific fields (`cdr_url`, `is_read_only`). `AuthType` is re-exported
from this module for backwards-compatible imports — callers that imported
`from app.models.config import AuthType` continue to work unchanged.
"""

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.connection_base import AuthType, ConnectionConfigMixin

__all__ = ["AuthType", "CDRConfig"]


class CDRConfig(Base, ConnectionConfigMixin):
    __tablename__ = "cdr_configs"

    cdr_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

"""Unit tests for app.dependencies — ConnectionContext, kind defaulting, and
the CDRContext back-compat alias. These lock in load-bearing semantics that
future PRs (alias removal, MCS kind addition, mixin reuse for MCSConfig)
would otherwise silently break.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.dependencies import CDRContext, ConnectionContext, get_active_cdr
from app.models.config import AuthType, CDRConfig
from app.models.connection_base import ConnectionConfigMixin, ConnectionKind


def test_cdrcontext_is_connectioncontext_alias():
    assert CDRContext is ConnectionContext


@pytest.mark.asyncio
async def test_get_active_cdr_fallback_returns_cdr_kind(test_session):
    ctx = await get_active_cdr(session=test_session)
    assert ctx.kind == ConnectionKind.cdr
    assert ctx.kind == "cdr"


@pytest.mark.asyncio
async def test_get_active_cdr_db_branch_returns_cdr_kind(test_session):
    cfg = CDRConfig(
        cdr_url="http://example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="Test CDR",
        is_default=False,
        is_read_only=False,
    )
    test_session.add(cfg)
    await test_session.commit()

    ctx = await get_active_cdr(session=test_session)
    assert ctx.kind == ConnectionKind.cdr
    assert ctx.name == "Test CDR"
    assert ctx.cdr_url == "http://example.com/fhir"


def test_mixin_auth_credentials_is_per_subclass():
    """The @declared_attr on auth_credentials must produce a fresh Column
    per subclass. Without it, SQLAlchemy would share the same Column across
    subclass tables and raise at mapper configuration. We verify by attaching
    two probe subclasses to a throwaway Base (so we don't pollute the
    production app.models.base.Base metadata) and asserting their
    auth_credentials Columns are distinct instances.
    """

    class _ProbeBase(DeclarativeBase):
        pass

    class _ProbeA(_ProbeBase, ConnectionConfigMixin):
        __tablename__ = "_probe_a"
        a_url: Mapped[str] = mapped_column(String(64), nullable=False)

    class _ProbeB(_ProbeBase, ConnectionConfigMixin):
        __tablename__ = "_probe_b"
        b_url: Mapped[str] = mapped_column(String(64), nullable=False)

    a_col = _ProbeA.__table__.c.auth_credentials
    b_col = _ProbeB.__table__.c.auth_credentials

    assert a_col is not b_col, (
        "@declared_attr must produce per-subclass Columns; shared Column would break MCSConfig in PR #1b."
    )
    assert a_col.table.name == "_probe_a"
    assert b_col.table.name == "_probe_b"

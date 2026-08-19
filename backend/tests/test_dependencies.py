"""Unit tests for app.dependencies — ConnectionContext, kind defaulting, and
the CDRContext back-compat alias. These lock in load-bearing semantics that
future PRs (alias removal, MCS kind addition, mixin reuse for MCSConfig)
would otherwise silently break.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String
from sqlalchemy.exc import IntegrityError
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


@pytest.mark.asyncio
async def test_get_active_cdr_populates_request_timeout_seconds(test_session):
    """The DB-backed branch carries the column value through to the context."""
    cfg = CDRConfig(
        cdr_url="http://example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="Slow CDR",
        is_default=False,
        is_read_only=False,
        request_timeout_seconds=120,
    )
    test_session.add(cfg)
    await test_session.commit()

    ctx = await get_active_cdr(session=test_session)
    assert ctx.request_timeout_seconds == 120


@pytest.mark.asyncio
async def test_get_active_cdr_fallback_uses_default_timeout(test_session):
    """Empty table → fallback context returns the dataclass default of 30s."""
    ctx = await get_active_cdr(session=test_session)
    assert ctx.request_timeout_seconds == 30


@pytest.mark.asyncio
async def test_activate_concurrent_raises_integrity_error(test_session):
    """Regression test (per IRON RULE) for the activation race.

    The partial unique index `idx_one_active_cdr` (declared via
    `CDRConfig.__table_args__`) must reject a second row with `is_active=True`.
    Without it, two concurrent `/activate` requests on different rows could
    both end up active. SQLite supports partial unique indexes since 3.8.0;
    this test runs on the in-memory SQLite test DB.
    """
    first = CDRConfig(
        cdr_url="http://example.com/first",
        auth_type=AuthType.none,
        is_active=True,
        name="First Active",
        is_default=False,
        is_read_only=False,
    )
    test_session.add(first)
    await test_session.commit()

    second = CDRConfig(
        cdr_url="http://example.com/second",
        auth_type=AuthType.none,
        is_active=True,
        name="Second Active",
        is_default=False,
        is_read_only=False,
    )
    test_session.add(second)

    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


# ---------------------------------------------------------------------------
# get_active_mcs: is_read_only (issue #396)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_mcs_fallback_is_writable(test_session):
    """No active MCS row → the built-in local engine, which IS writable.

    Defaulting this to True would break measure upload/delete on a stock
    install, where no mcs_configs row exists yet.
    """
    from app.dependencies import get_active_mcs

    ctx = await get_active_mcs(session=test_session)
    assert ctx.kind == ConnectionKind.mcs
    assert ctx.is_read_only is False


@pytest.mark.asyncio
async def test_get_active_mcs_populates_is_read_only(test_session):
    """The DB branch carries the row's is_read_only through to the context."""
    from app.dependencies import get_active_mcs
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        mcs_url="https://shared-mcs.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="Shared MCS",
        is_default=False,
        is_read_only=True,
    )
    test_session.add(cfg)
    await test_session.commit()

    ctx = await get_active_mcs(session=test_session)
    assert ctx.kind == ConnectionKind.mcs
    assert ctx.name == "Shared MCS"
    assert ctx.mcs_url == "https://shared-mcs.example.com/fhir"
    assert ctx.is_read_only is True


def test_is_read_only_is_shared_by_the_mixin():
    """is_read_only lives on the mixin, so every connection kind gets it."""
    from app.models.mcs_config import MCSConfig

    assert "is_read_only" in ConnectionConfigMixin.__dict__
    assert "is_read_only" in CDRConfig.__table__.columns
    assert "is_read_only" in MCSConfig.__table__.columns


# ---------------------------------------------------------------------------
# get_active_mcs: wipe_before_job (issue #392)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_mcs_fallback_keeps_full_wipe(test_session):
    """No active MCS row → the built-in local engine, which DOES full-wipe.

    The seeded local engine is Lenny's own container; wiping it between jobs is
    correct and keeps the dataset from growing without bound. Issue #392 only
    makes the full wipe unsafe for servers Lenny does not own.
    """
    from app.dependencies import get_active_mcs

    ctx = await get_active_mcs(session=test_session)
    assert ctx.wipe_before_job is True


@pytest.mark.asyncio
async def test_get_active_mcs_defaults_user_created_rows_to_scoped_wipe(test_session):
    """A connection a user created defaults to the safe, scoped wipe.

    This is the core of #392: pointing Lenny at a shared connectathon server must
    not delete other participants' patients on the first job.
    """
    from app.dependencies import get_active_mcs
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        mcs_url="https://shared-mcs.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="Shared MCS",
        is_default=False,
    )
    test_session.add(cfg)
    await test_session.commit()

    ctx = await get_active_mcs(session=test_session)
    assert ctx.wipe_before_job is False


@pytest.mark.asyncio
async def test_get_active_mcs_populates_wipe_before_job(test_session):
    """An explicit opt-in on the row carries through to the context."""
    from app.dependencies import get_active_mcs
    from app.models.mcs_config import MCSConfig

    cfg = MCSConfig(
        mcs_url="https://my-own-mcs.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="My Own MCS",
        is_default=False,
        wipe_before_job=True,
    )
    test_session.add(cfg)
    await test_session.commit()

    ctx = await get_active_mcs(session=test_session)
    assert ctx.wipe_before_job is True


def test_wipe_before_job_is_mcs_only():
    """wipe_before_job belongs to MCSConfig, NOT the shared mixin.

    Unlike is_read_only — which is meaningful for any connection Lenny can write
    to — only the measure engine is wiped before a job. The CDR is read from and
    wiped only by an explicit factory reset. Putting this on the mixin would give
    CDRConfig a column with no meaning.
    """
    from app.models.mcs_config import MCSConfig

    assert "wipe_before_job" in MCSConfig.__table__.columns
    assert "wipe_before_job" not in ConnectionConfigMixin.__dict__
    assert "wipe_before_job" not in CDRConfig.__table__.columns

"""Schema-migration tests.

Right now the only migration with non-trivial behavior worth testing in a
unit suite is the MCS snapshot backfill on the jobs table. Other ALTER
TABLE statements in `_run_schema_migrations()` are Postgres-only and
gated by the test runner (the SQLite test DB picks up the schema via
`Base.metadata.create_all`, not the raw-SQL ALTERs).

This file is the place to add focused unit-tests for any migration that:
  - Mutates existing data (UPDATE / backfill)
  - Has dialect-portable SQL
  - Has env-var-dependent behavior worth pinning
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.models.job import Job, JobStatus

pytestmark = pytest.mark.asyncio


async def _make_legacy_job(test_session, *, cdr_url: str = "http://cdr/fhir") -> int:
    """Insert a Job row that pre-dates the mcs_url snapshot column.

    On SQLite the Job table is created with all columns (including
    mcs_url), but we leave them NULL to simulate the legacy state the
    backfill needs to handle.
    """
    job = Job(
        measure_id="CMS122",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url=cdr_url,
        status=JobStatus.complete,
        # mcs_url, mcs_name, mcs_id all NULL — legacy row.
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)
    return job.id


async def test_backfill_populates_mcs_url_when_env_var_set(test_session):
    """The backfill UPDATE pattern: legacy NULL mcs_url is filled from env URL."""
    job_id = await _make_legacy_job(test_session)

    # Verify pre-state: mcs_url is NULL
    pre = await test_session.execute(select(Job).where(Job.id == job_id))
    job = pre.scalar_one()
    assert job.mcs_url is None
    assert job.mcs_name is None

    # Simulate the backfill UPDATE (mirrors the SQL in
    # `_run_schema_migrations`, dialect-portable via parameterized text()).
    env_url = "https://measure.example.com/fhir"
    await test_session.execute(
        text("UPDATE jobs SET   mcs_url = :url,   mcs_name = 'Local Measure Engine' WHERE mcs_url IS NULL"),
        {"url": env_url},
    )
    await test_session.commit()
    test_session.expire_all()  # Drop identity-map cache so post-query sees the UPDATE.

    # Post-state: mcs_url + mcs_name populated
    post = await test_session.execute(select(Job).where(Job.id == job_id))
    job = post.scalar_one()
    assert job.mcs_url == env_url
    assert job.mcs_name == "Local Measure Engine"


async def test_backfill_skips_jobs_with_existing_mcs_url(test_session):
    """The WHERE mcs_url IS NULL guard: jobs already populated aren't overwritten."""
    # Pre-populate one row to simulate a job created post-PR #4
    job_existing = Job(
        measure_id="CMS125",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
        mcs_url="https://attendees-own-mcs.example.com/fhir",
        mcs_name="Attendee MCS",
    )
    test_session.add(job_existing)
    legacy_id = await _make_legacy_job(test_session)
    await test_session.commit()
    await test_session.refresh(job_existing)
    existing_id = job_existing.id

    env_url = "https://default.example.com/fhir"
    await test_session.execute(
        text("UPDATE jobs SET   mcs_url = :url,   mcs_name = 'Local Measure Engine' WHERE mcs_url IS NULL"),
        {"url": env_url},
    )
    await test_session.commit()

    # Existing row unchanged
    existing = (await test_session.execute(select(Job).where(Job.id == existing_id))).scalar_one()
    assert existing.mcs_url == "https://attendees-own-mcs.example.com/fhir"
    assert existing.mcs_name == "Attendee MCS"

    # Legacy row backfilled
    legacy = (await test_session.execute(select(Job).where(Job.id == legacy_id))).scalar_one()
    assert legacy.mcs_url == env_url


async def test_get_mcs_url_falls_back_to_settings_when_job_mcs_url_null(test_session, monkeypatch):
    """Orchestrator's _get_mcs_url(): NULL on the Job → env-var fallback.

    Mirrors the legacy-job path where the migration backfill couldn't run
    (env var was unset at migration time) but the worker still needs SOME
    URL to call.
    """
    from app.config import settings as app_config
    from app.services.orchestrator import _get_mcs_url

    job_id = await _make_legacy_job(test_session)
    monkeypatch.setattr(app_config, "MEASURE_ENGINE_URL", "https://fallback.example.com/fhir")

    # Patch the orchestrator's session factory so it picks up the test DB.
    from contextlib import asynccontextmanager

    import app.services.orchestrator as orch

    @asynccontextmanager
    async def _fake_session():
        yield test_session

    monkeypatch.setattr(orch, "async_session", _fake_session)
    url = await _get_mcs_url(job_id)
    assert url == "https://fallback.example.com/fhir"


async def test_get_mcs_url_returns_job_snapshot_when_set(test_session, monkeypatch):
    """When Job.mcs_url IS populated, _get_mcs_url returns it (NOT the env var)."""
    from app.config import settings as app_config
    from app.services.orchestrator import _get_mcs_url

    # Job created post-PR #4 with mcs_url set.
    job = Job(
        measure_id="CMS122",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.queued,
        mcs_url="https://job-snapshot.example.com/fhir",
        mcs_name="Job Snapshot MCS",
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    # Different env var to verify we DON'T fall back.
    monkeypatch.setattr(app_config, "MEASURE_ENGINE_URL", "https://env-default.example.com/fhir")

    from contextlib import asynccontextmanager

    import app.services.orchestrator as orch

    @asynccontextmanager
    async def _fake_session():
        yield test_session

    monkeypatch.setattr(orch, "async_session", _fake_session)
    url = await _get_mcs_url(job.id)
    assert url == "https://job-snapshot.example.com/fhir"

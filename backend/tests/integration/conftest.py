"""Integration test fixtures — run against real HAPI FHIR instances via Docker.

Prerequisites:
    docker compose -f docker-compose.test.yml up -d
"""

import json
import pathlib
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base

# ---------------------------------------------------------------------------
# Test infrastructure URLs
# ---------------------------------------------------------------------------

TEST_CDR_URL = "http://localhost:8180/fhir"
TEST_MEASURE_URL = "http://localhost:8181/fhir"
TEST_DATABASE_URL = "postgresql+asyncpg://mct2:mct2@localhost:5433/mct2"

SEED_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed"

SKIP_MESSAGE = (
    "Integration test infrastructure not running. "
    "Start with: docker compose -f docker-compose.test.yml up -d"
)


# ---------------------------------------------------------------------------
# Session-scoped: verify infrastructure is reachable
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _require_infrastructure():
    """Skip the entire integration test session if HAPI FHIR instances are unreachable."""
    import httpx as _httpx  # sync for setup check

    for url in (
        f"{TEST_CDR_URL}/metadata",
        f"{TEST_MEASURE_URL}/metadata",
    ):
        try:
            resp = _httpx.get(url, timeout=10)
            resp.raise_for_status()
        except Exception:
            pytest.skip(SKIP_MESSAGE, allow_module_level=True)


# ---------------------------------------------------------------------------
# FHIR URL fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cdr_url() -> str:
    return TEST_CDR_URL


@pytest.fixture(scope="session")
def measure_url() -> str:
    return TEST_MEASURE_URL


# ---------------------------------------------------------------------------
# Seed data loading (once per session)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _load_seed_data(_require_infrastructure):
    """Load measure-bundle.json and patient-bundle.json into the test HAPI instances."""
    import httpx as _httpx

    measure_bundle_path = SEED_DIR / "measure-bundle.json"
    patient_bundle_path = SEED_DIR / "patient-bundle.json"

    headers = {"Content-Type": "application/fhir+json"}

    # Load measure bundle into measure engine
    if measure_bundle_path.exists():
        with open(measure_bundle_path) as f:
            bundle = json.load(f)
        resp = _httpx.post(TEST_MEASURE_URL, json=bundle, headers=headers, timeout=60)
        resp.raise_for_status()

    # Load patient bundle into CDR
    if patient_bundle_path.exists():
        with open(patient_bundle_path) as f:
            bundle = json.load(f)
        resp = _httpx.post(TEST_CDR_URL, json=bundle, headers=headers, timeout=60)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Database engine & session (session-scoped engine, function-scoped sessions)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def integration_engine():
    """Create an async engine pointing at the test PostgreSQL instance."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    # Drop all tables on teardown
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def integration_session_factory(integration_engine):
    """Return a session factory bound to the integration database."""
    return async_sessionmaker(
        integration_engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest_asyncio.fixture
async def db_session(integration_session_factory) -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session for a single test, with cleanup."""
    async with integration_session_factory() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(integration_session_factory):
    """Truncate job/batch/result tables between tests (keep HAPI data loaded)."""
    yield
    async with integration_session_factory() as session:
        for table in ("measure_results", "batches", "jobs", "cdr_configs"):
            await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        await session.commit()


# ---------------------------------------------------------------------------
# FastAPI test client wired to real HAPI + test PostgreSQL
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def integration_client(integration_session_factory):
    """Provide an httpx AsyncClient talking to the FastAPI app.

    The database dependency is overridden to use the test PostgreSQL.
    Environment variables are patched so fhir_client talks to test HAPI instances.
    """
    import os
    from unittest.mock import patch

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.db import get_session
    from app.routes import health, jobs, measures, results, settings

    test_app = FastAPI()
    test_app.include_router(health.router)
    test_app.include_router(jobs.router)
    test_app.include_router(measures.router)
    test_app.include_router(results.router)
    test_app.include_router(settings.router)

    async def _override_get_session():
        async with integration_session_factory() as session:
            yield session

    test_app.dependency_overrides[get_session] = _override_get_session

    # Patch settings so fhir_client uses the test HAPI instances
    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
    ):
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

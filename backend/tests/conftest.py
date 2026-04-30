"""Shared pytest fixtures for Lenny backend tests."""

import asyncio
import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base

# ---------------------------------------------------------------------------
# CDR Fernet key — must be present before any EncryptedJSON TypeDecorator call
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _cdr_fernet_key():
    """Set a test Fernet key for the full session and prime the singleton.

    EncryptedJSON.process_bind_param calls _get_fernet() on every flush of a
    CDRConfig row.  Without this fixture any test that touches auth_credentials
    would raise RuntimeError("CDR_FERNET_KEY not configured").

    The singleton is intentionally left live after the session ends — it will
    be torn down with the process.  Tests in test_services_credential_crypto.py
    that need to exercise key-loading edge cases call _reset_fernet() themselves
    and manage the env var locally.
    """
    from app.services.credential_crypto import _get_fernet, _reset_fernet

    key = Fernet.generate_key().decode()
    os.environ["CDR_FERNET_KEY"] = key
    _get_fernet()  # prime the singleton (also pops the env var)
    yield key
    _reset_fernet()


# ---------------------------------------------------------------------------
# Async SQLite engine (in-memory, one per test)
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Use a single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def test_engine():
    """Create a fresh in-memory SQLite engine and tables for each test."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session bound to the in-memory test database."""
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def session_factory(test_engine):
    """Return the session factory itself (used by override)."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# FastAPI TestClient with dependency overrides
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(session_factory) -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient talking to the FastAPI app.

    The database dependency is overridden so every request uses the
    in-memory SQLite test database. The lifespan is skipped to avoid
    starting the background worker and hitting the real Postgres.
    """
    from fastapi import FastAPI

    from app.db import get_session
    from app.routes import health, jobs, measures, results, settings, validation

    # Build a minimal app without the lifespan (no worker, no real DB init)
    test_app = FastAPI()
    test_app.include_router(health.router)
    test_app.include_router(jobs.router)
    test_app.include_router(measures.router)
    test_app.include_router(results.router)
    test_app.include_router(settings.router)
    test_app.include_router(validation.router)

    async def _override_get_session():
        async with session_factory() as session:
            yield session

    test_app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Mock FHIR responses
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_fhir_metadata():
    """Return a minimal FHIR CapabilityStatement for /metadata calls."""
    return {
        "resourceType": "CapabilityStatement",
        "fhirVersion": "4.0.1",
        "software": {"name": "HAPI FHIR Test"},
        "status": "active",
    }


@pytest.fixture
def mock_measure_bundle():
    """A minimal FHIR Bundle with a single Measure resource."""
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {
                "resource": {
                    "resourceType": "Measure",
                    "id": "measure-1",
                    "name": "TestMeasure",
                    "title": "Test Measure",
                    "version": "1.0",
                    "status": "active",
                    "url": "http://example.com/Measure/measure-1",
                    "description": "A test measure",
                }
            }
        ],
    }


@pytest.fixture
def mock_patient_bundle():
    """A minimal FHIR Bundle with patient entries."""
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "patient-1",
                    "name": [{"given": ["John"], "family": "Doe"}],
                }
            },
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "patient-2",
                    "name": [{"given": ["Jane"], "family": "Smith"}],
                }
            },
        ],
        "link": [],
    }


@pytest.fixture
def mock_measure_report():
    """A minimal MeasureReport resource."""
    return {
        "resourceType": "MeasureReport",
        "id": "report-1",
        "status": "complete",
        "type": "individual",
        "group": [
            {
                "population": [
                    {
                        "code": {"coding": [{"code": "initial-population"}]},
                        "count": 1,
                    },
                    {
                        "code": {"coding": [{"code": "denominator"}]},
                        "count": 1,
                    },
                    {
                        "code": {"coding": [{"code": "numerator"}]},
                        "count": 1,
                    },
                    {
                        "code": {"coding": [{"code": "denominator-exclusion"}]},
                        "count": 0,
                    },
                    {
                        "code": {"coding": [{"code": "numerator-exclusion"}]},
                        "count": 0,
                    },
                ]
            }
        ],
        "evaluatedResource": [
            {"reference": "Patient/patient-1"},
            {"reference": "Condition/cond-1"},
        ],
    }


@pytest.fixture
def mock_test_bundle_with_expected():
    """A test bundle with Measure, Library, Patient, and isTestCase MeasureReport."""
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": {
                    "resourceType": "Measure",
                    "id": "CMS124",
                    "url": "https://example.com/Measure/CMS124",
                    "name": "CMS124Test",
                    "status": "active",
                },
                "request": {"method": "PUT", "url": "Measure/CMS124"},
            },
            {
                "resource": {
                    "resourceType": "Library",
                    "id": "lib-1",
                    "url": "https://example.com/Library/lib-1",
                    "status": "active",
                },
                "request": {"method": "PUT", "url": "Library/lib-1"},
            },
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "test-patient-1",
                    "name": [{"given": ["Alice"], "family": "Test"}],
                },
                "request": {"method": "PUT", "url": "Patient/test-patient-1"},
            },
            {
                "resource": {
                    "resourceType": "Observation",
                    "id": "obs-1",
                    "subject": {"reference": "Patient/test-patient-1"},
                    "code": {"coding": [{"code": "test-obs"}]},
                },
                "request": {"method": "PUT", "url": "Observation/obs-1"},
            },
            {
                "resource": {
                    "resourceType": "MeasureReport",
                    "id": "expected-report-1",
                    "status": "complete",
                    "type": "individual",
                    "measure": "https://example.com/Measure/CMS124",
                    "contained": [
                        {
                            "resourceType": "Parameters",
                            "id": "params-1",
                            "parameter": [{"name": "subject", "valueString": "test-patient-1"}],
                        }
                    ],
                    "extension": [
                        {
                            "url": "http://hl7.org/fhir/StructureDefinition/cqf-inputParameters",
                            "valueReference": {"reference": "#params-1"},
                        },
                        {
                            "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-testCaseDescription",
                            "valueMarkdown": "Female 24yo, cervical cytology 2yrs prior",
                        },
                    ],
                    "modifierExtension": [
                        {
                            "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase",
                            "valueBoolean": True,
                        }
                    ],
                    "period": {"start": "2026-01-01", "end": "2026-12-31"},
                    "group": [
                        {
                            "population": [
                                {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                                {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                                {"code": {"coding": [{"code": "denominator-exclusion"}]}, "count": 0},
                                {"code": {"coding": [{"code": "numerator"}]}, "count": 1},
                            ]
                        }
                    ],
                    "evaluatedResource": [
                        {"reference": "Patient/test-patient-1"},
                        {"reference": "Observation/obs-1"},
                    ],
                },
                "request": {"method": "PUT", "url": "MeasureReport/expected-report-1"},
            },
        ],
    }

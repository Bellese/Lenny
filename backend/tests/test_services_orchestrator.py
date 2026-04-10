"""Tests for the orchestrator service (run_job and helpers)."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobStatus, MeasureResult
from app.services.orchestrator import (
    _extract_patient_name,
    _extract_populations,
    _get_cdr_auth_headers,
    run_job,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------


class TestExtractPopulations:
    def test_all_positive(self, mock_measure_report):
        pops = _extract_populations(mock_measure_report)
        assert pops["initial_population"] is True
        assert pops["denominator"] is True
        assert pops["numerator"] is True
        assert pops["denominator_exclusion"] is False
        assert pops["numerator_exclusion"] is False

    def test_empty_report(self):
        pops = _extract_populations({})
        assert all(v is False for v in pops.values())

    def test_zero_counts(self):
        report = {
            "group": [
                {
                    "population": [
                        {
                            "code": {"coding": [{"code": "initial-population"}]},
                            "count": 0,
                        },
                        {
                            "code": {"coding": [{"code": "denominator"}]},
                            "count": 0,
                        },
                    ]
                }
            ]
        }
        pops = _extract_populations(report)
        assert pops["initial_population"] is False
        assert pops["denominator"] is False


class TestExtractPatientName:
    def test_full_name(self):
        patient = {"name": [{"given": ["John", "Q"], "family": "Doe"}]}
        assert _extract_patient_name(patient) == "John Q Doe"

    def test_family_only(self):
        patient = {"name": [{"family": "Smith"}]}
        assert _extract_patient_name(patient) == "Smith"

    def test_given_only(self):
        patient = {"name": [{"given": ["Jane"]}]}
        assert _extract_patient_name(patient) == "Jane"

    def test_no_name(self):
        assert _extract_patient_name({}) is None
        assert _extract_patient_name({"name": []}) is None


# ---------------------------------------------------------------------------
# Integration tests for run_job
# ---------------------------------------------------------------------------


async def _setup_job(session: AsyncSession) -> int:
    """Insert a queued job and return its ID."""
    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job.id


def _make_session_factory_patch(session_factory):
    """Create a patch for async_session that uses our test session factory."""
    return patch("app.services.orchestrator.async_session", session_factory)


async def test_run_job_happy_path(test_session, session_factory, mock_measure_report):
    """run_job: happy path gathers patients, pushes data, evaluates, stores results."""
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock) as mock_wipe,
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch(
            "app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr.example.com/fhir"
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=[
                {"resourceType": "Patient", "id": "p1"},
                {"resourceType": "Condition", "id": "c1"},
            ],
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
    ):
        await run_job(job_id)

    # Verify job completed
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        assert job.total_patients == 1
        assert job.processed_patients == 1
        assert job.failed_patients == 0
        assert job.completed_at is not None

        # Verify result was stored
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        assert results[0].patient_id == "p1"
        assert results[0].patient_name == "Alice Test"

    mock_wipe.assert_called_once()


async def test_run_job_no_patients(test_session, session_factory):
    """run_job: when no patients found, job completes with zero counts."""
    job_id = await _setup_job(test_session)

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        assert job.total_patients == 0


async def test_run_job_wipe_failure(test_session, session_factory):
    """run_job: wipe failure at start fails the job."""
    job_id = await _setup_job(test_session)

    with (
        _make_session_factory_patch(session_factory),
        patch(
            "app.services.orchestrator.wipe_patient_data",
            new_callable=AsyncMock,
            side_effect=Exception("Measure engine down"),
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert "Measure engine down" in job.error_message


async def test_run_job_cdr_unreachable(test_session, session_factory):
    """run_job: CDR unreachable when gathering patients fails the job."""
    job_id = await _setup_job(test_session)

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            side_effect=ConnectionError("CDR unreachable"),
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert "CDR unreachable" in job.error_message


async def test_run_job_partial_patient_failure(test_session, session_factory, mock_measure_report):
    """run_job: if evaluate fails for one patient, results for others are preserved.

    The 2-phase approach pushes all patients first (Phase 1), then evaluates
    all patients (Phase 2).  A failure during evaluation for one patient
    should not prevent other patients from being evaluated.
    """
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Good"}]},
        {"resourceType": "Patient", "id": "p2", "name": [{"given": ["Bob"], "family": "Bad"}]},
    ]

    async def mock_evaluate(measure_id, patient_id, period_start, period_end):
        if patient_id == "p2":
            raise Exception("Evaluation failed for p2")
        return mock_measure_report

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
        patch("app.services.orchestrator._get_cdr_auth_headers", new_callable=AsyncMock, return_value={}),
        patch("app.services.orchestrator._get_cdr_url", new_callable=AsyncMock, return_value="http://cdr/fhir"),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patients",
            new_callable=AsyncMock,
            return_value=patients,
        ),
        patch.object(
            __import__("app.services.fhir_client", fromlist=["BatchQueryStrategy"]).BatchQueryStrategy,
            "gather_patient_data",
            new_callable=AsyncMock,
            return_value=[{"resourceType": "Patient", "id": "p1"}],
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=mock_evaluate,
        ),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        # One processed, one failed
        assert job.processed_patients == 1
        assert job.failed_patients == 1

        # Only one result should be stored
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        assert results[0].patient_id == "p1"


async def test_run_job_cancelled_before_batches(test_session, session_factory):
    """run_job: if job is cancelled before processing, it exits early."""
    job_id = await _setup_job(test_session)

    # Cancel the job before run_job processes batches
    async with session_factory() as session:
        job = await session.get(Job, job_id)
        job.status = JobStatus.cancelled
        await session.commit()

    with (
        _make_session_factory_patch(session_factory),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        # Should remain cancelled
        assert job.status == JobStatus.cancelled


async def test_run_job_nonexistent(session_factory):
    """run_job: non-existent job_id returns silently."""
    with _make_session_factory_patch(session_factory):
        # Should not raise
        await run_job(99999)


async def test_get_cdr_auth_headers_reads_from_job_row(test_session, session_factory):
    """_get_cdr_auth_headers reads auth from the job row, not the active CDRConfig."""
    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        cdr_auth_type="bearer",
        cdr_auth_credentials={"token": "test-jwt"},
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with (
        patch("app.services.orchestrator.async_session", session_factory),
        patch(
            "app.services.orchestrator._build_auth_headers",
            new_callable=AsyncMock,
            return_value={"Authorization": "Bearer test-jwt"},
        ) as mock_auth,
    ):
        headers = await _get_cdr_auth_headers(job.id)

    assert headers == {"Authorization": "Bearer test-jwt"}
    # Verify _build_auth_headers was called with job's auth fields, not a CDRConfig query
    mock_auth.assert_called_once_with("bearer", {"token": "test-jwt"})

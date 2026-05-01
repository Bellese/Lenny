"""Tests for the orchestrator service (run_job and helpers)."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobStatus, MeasureResult
from app.services.fhir_client import FailedResourceFetch, GatherResult
from app.services.orchestrator import (
    _error_measure_report,
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


def test_error_measure_report_sanitizes_internal_urls():
    report = _error_measure_report("p1", Exception("HTTP 400 at http://hapi-fhir-measure:8080/fhir"))

    assert report["resourceType"] == "OperationOutcome"
    assert report["subject"]["reference"] == "Patient/p1"
    diagnostics = report["issue"][0]["diagnostics"]
    assert "hapi-fhir-measure" not in diagnostics
    assert "8080" not in diagnostics


def test_error_measure_report_preserves_upstream_outcome_via_extension():
    """When upstream OO is provided, it is embedded with a FHIR Extension (not synthetic)."""
    from app.services.orchestrator import LENNY_ERROR_EXT

    upstream = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Measure not found"}],
    }
    report = _error_measure_report("p2", Exception("evaluate failed"), upstream_outcome=upstream)

    assert report["resourceType"] == "OperationOutcome"
    assert report["subject"]["reference"] == "Patient/p2"
    # Original issue preserved
    assert report["issue"][0]["diagnostics"] == "Measure not found"
    # Extension added with sanitized error string
    extensions = report.get("extension", [])
    assert any(e["url"] == LENNY_ERROR_EXT for e in extensions)


def test_error_measure_report_deep_copies_upstream_outcome():
    """Two patients with the same upstream OO must produce independent dicts (no mutation)."""
    upstream = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": "shared error"}],
    }
    report_p1 = _error_measure_report("p1", Exception("fail"), upstream_outcome=upstream)
    report_p2 = _error_measure_report("p2", Exception("fail"), upstream_outcome=upstream)

    # Mutating one report must not affect the other
    report_p1["issue"][0]["diagnostics"] = "mutated"
    assert report_p2["issue"][0]["diagnostics"] == "shared error"
    assert report_p1 is not report_p2


def test_error_measure_report_falls_back_to_synthetic_without_upstream():
    """Without upstream OO, a synthetic OperationOutcome is produced."""
    report = _error_measure_report("p3", Exception("connection refused"))

    assert report["resourceType"] == "OperationOutcome"
    assert "extension" not in report
    assert report["issue"][0]["code"] == "processing"


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
            return_value=GatherResult(
                resources=[
                    {"resourceType": "Patient", "id": "p1"},
                    {"resourceType": "Condition", "id": "c1"},
                ]
            ),
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value=mock_measure_report,
        ),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
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

    mock_wipe.assert_awaited_once_with(strict=False)


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
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=mock_evaluate,
        ),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.complete
        # One processed, one failed
        assert job.processed_patients == 1
        assert job.failed_patients == 1

        # One successful result and one per-patient error result should be stored.
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 2
        by_patient = {r.patient_id: r for r in results}
        assert by_patient["p1"].populations.get("error") is None
        assert by_patient["p2"].populations["error"] is True
        assert "Evaluation failed for p2" in by_patient["p2"].populations["error_message"]


async def test_run_job_all_patient_failures_marks_job_failed(test_session, session_factory):
    """run_job: if every patient evaluation fails, the job must not look successful."""
    job_id = await _setup_job(test_session)

    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Bad"}]},
        {"resourceType": "Patient", "id": "p2", "name": [{"given": ["Bob"], "family": "Bad"}]},
    ]

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
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=Exception("HAPI returned 400"),
        ),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        job = await session.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert job.processed_patients == 0
        assert job.failed_patients == 2
        assert job.error_message == "All 2 patient evaluations failed"

        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 2
        assert all(r.populations["error"] is True for r in results)


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


async def test_get_cdr_auth_headers_reads_live_cdr_config(test_session, session_factory):
    """_get_cdr_auth_headers joins cdr_configs via cdr_id for live credentials."""
    from app.models.config import AuthType, CDRConfig

    cfg = CDRConfig(
        name="Live CDR",
        cdr_url="http://cdr.example.com/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "test-jwt"},
        is_active=False,
        is_default=False,
        is_read_only=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://cdr.example.com/fhir",
        status=JobStatus.queued,
        cdr_auth_type="bearer",
        cdr_id=cfg.id,
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
    # Called with the live CDR's auth_type and credentials (decrypted by TypeDecorator)
    mock_auth.assert_called_once()
    call_auth_type = mock_auth.call_args[0][0]
    assert call_auth_type == AuthType.bearer


async def test_orchestrator_fails_clearly_when_cdr_deleted(test_session, session_factory):
    """_get_cdr_auth_headers raises RuntimeError when the CDR config no longer exists."""
    job = Job(
        measure_id="m-orphan",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://gone.example.com/fhir",
        status=JobStatus.running,
        cdr_id=None,  # CDR was deleted (FK set NULL by ON DELETE SET NULL)
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    with patch("app.services.orchestrator.async_session", session_factory):
        with pytest.raises(RuntimeError, match="has no cdr_id"):
            await _get_cdr_auth_headers(job.id)


async def test_process_batch_uses_everything_strategy(test_session, session_factory, monkeypatch):
    """_process_single_batch uses $everything by default for complete patient graphs."""
    from unittest.mock import MagicMock

    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    monkeypatch.setattr("app.services.orchestrator.settings.PATIENT_DATA_STRATEGY", "batch")

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    batch = Batch(
        job_id=job.id,
        batch_number=1,
        patient_ids=["p1"],
        status=BatchStatus.pending,
    )
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    patient_map = {"p1": {"resourceType": "Patient", "id": "p1"}}

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.BatchQueryStrategy") as mock_strategy_cls,
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
        patch("app.services.orchestrator.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={
                "resourceType": "MeasureReport",
                "status": "complete",
                "group": [
                    {
                        "population": [
                            {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                            {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                            {"code": {"coding": [{"code": "numerator"}]}, "count": 0},
                        ]
                    }
                ],
            },
        ),
        patch("app.services.orchestrator.wipe_patient_data", new_callable=AsyncMock),
    ):
        mock_strategy = MagicMock()
        mock_strategy.gather_patient_data = AsyncMock(
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}])
        )
        mock_strategy_cls.return_value = mock_strategy

        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map=patient_map,
            cdr_url="http://cdr/fhir",
            auth_headers={},
        )

    mock_strategy_cls.assert_called_once_with()


async def test_process_batch_uses_data_requirements_strategy_when_configured(
    test_session, session_factory, monkeypatch
):
    """_process_single_batch can be rolled back to DataRequirementsStrategy by env config."""
    from unittest.mock import MagicMock

    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    monkeypatch.setattr("app.services.orchestrator.settings.PATIENT_DATA_STRATEGY", "data_requirements")

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    batch = Batch(
        job_id=job.id,
        batch_number=1,
        patient_ids=["p1"],
        status=BatchStatus.pending,
    )
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    patient_map = {"p1": {"resourceType": "Patient", "id": "p1"}}

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.DataRequirementsStrategy") as mock_strategy_cls,
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
        patch("app.services.orchestrator.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={
                "resourceType": "MeasureReport",
                "status": "complete",
                "group": [
                    {
                        "population": [
                            {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                            {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                            {"code": {"coding": [{"code": "numerator"}]}, "count": 0},
                        ]
                    }
                ],
            },
        ),
    ):
        mock_strategy = MagicMock()
        mock_strategy.gather_patient_data = AsyncMock(
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}])
        )
        mock_strategy_cls.return_value = mock_strategy

        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map=patient_map,
            cdr_url="http://cdr/fhir",
            auth_headers={},
        )

    mock_strategy_cls.assert_called_once_with("CMS999")


async def test_process_batch_hapi_sync_calls_trigger_reindex(test_session, session_factory, monkeypatch):
    """When HAPI_SYNC_AFTER_UPLOAD=True, _process_single_batch probes reindex with Encounter-bearing patients."""
    from unittest.mock import MagicMock

    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    monkeypatch.setattr("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", True)
    monkeypatch.setattr("app.services.orchestrator.settings.MEASURE_ENGINE_URL", "http://mcs/fhir")

    job = Job(
        measure_id="CMS122",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    batch = Batch(
        job_id=job.id,
        batch_number=1,
        patient_ids=["p1", "p2"],
        status=BatchStatus.pending,
    )
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    patient_map = {
        "p1": {"resourceType": "Patient", "id": "p1"},
        "p2": {"resourceType": "Patient", "id": "p2"},
    }
    mock_reindex = MagicMock()

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.BatchQueryStrategy") as mock_strategy_cls,
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.trigger_reindex_and_wait_for_patients", mock_reindex),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={
                "resourceType": "MeasureReport",
                "status": "complete",
                "group": [{"population": [{"code": {"coding": [{"code": "initial-population"}]}, "count": 1}]}],
            },
        ),
    ):
        mock_strategy = MagicMock()
        mock_strategy.gather_patient_data = AsyncMock(
            side_effect=lambda _url, patient_id, _headers: (
                GatherResult(
                    resources=[
                        {"resourceType": "Patient", "id": patient_id},
                        {"resourceType": "Encounter", "id": f"enc-{patient_id}"},
                    ]
                )
                if patient_id == "p1"
                else GatherResult(resources=[{"resourceType": "Patient", "id": patient_id}])
            )
        )
        mock_strategy_cls.return_value = mock_strategy

        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map=patient_map,
            cdr_url="http://cdr/fhir",
            auth_headers={},
        )

    mock_reindex.assert_called_once_with("http://mcs/fhir", ["p1"])


async def test_process_batch_hapi_sync_sleeps_when_no_encounters(test_session, session_factory, monkeypatch):
    """When no pushed patients have Encounters, fall back to sleep instead of Encounter probe."""
    from unittest.mock import MagicMock, patch

    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    monkeypatch.setattr("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", True)
    monkeypatch.setattr("app.services.orchestrator.settings.MEASURE_ENGINE_URL", "http://mcs/fhir")
    monkeypatch.setattr("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0)

    job = Job(
        measure_id="CMS122",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    batch = Batch(job_id=job.id, batch_number=1, patient_ids=["p1"], status=BatchStatus.pending)
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    mock_reindex = MagicMock()

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator.BatchQueryStrategy") as mock_strategy_cls,
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.trigger_reindex_and_wait_for_patients", mock_reindex),
        patch("app.services.orchestrator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={
                "resourceType": "MeasureReport",
                "status": "complete",
                "group": [{"population": [{"code": {"coding": [{"code": "initial-population"}]}, "count": 1}]}],
            },
        ),
    ):
        mock_strategy = MagicMock()
        mock_strategy.gather_patient_data = AsyncMock(
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}])
        )
        mock_strategy_cls.return_value = mock_strategy

        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map={"p1": {"resourceType": "Patient", "id": "p1"}},
            cdr_url="http://cdr/fhir",
            auth_headers={},
        )

    mock_reindex.assert_not_called()
    mock_sleep.assert_called_once_with(0)


# ---------------------------------------------------------------------------
# Gather failure / evaluate skip invariants (PR-2 new behaviors)
# ---------------------------------------------------------------------------


async def test_run_job_gather_failure_prevents_evaluate_call(test_session, session_factory):
    """When gather raises for a patient, evaluate_measure is NOT called for that patient."""

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    evaluate_mock = AsyncMock()

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
            side_effect=Exception("CDR connection refused"),
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch("app.services.orchestrator.evaluate_measure", evaluate_mock),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
    ):
        await run_job(job_id)

    # evaluate_measure must NOT have been called for the failed-gather patient
    evaluate_mock.assert_not_awaited()

    # A MeasureResult error row must still exist (full exception → gather phase)
    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        assert results[0].populations["error"] is True
        assert results[0].populations["error_message"]  # back-compat field populated
        assert results[0].error_phase == "gather"


async def test_run_job_partial_gather_continues_to_evaluate(test_session, session_factory, mock_measure_report):
    """Partial gather (some resource types failed) proceeds to evaluate — AT-2."""

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]
    partial_result = GatherResult(
        resources=[{"resourceType": "Patient", "id": "p1"}, {"resourceType": "Condition", "id": "c1"}],
        failed_types=[FailedResourceFetch(resource_type="Observation", error="500 Internal Server Error")],
    )

    evaluate_mock = AsyncMock(return_value=mock_measure_report)

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
            return_value=partial_result,
        ),
        patch(
            "app.services.orchestrator.push_resources",
            new_callable=AsyncMock,
        ),
        patch("app.services.orchestrator.evaluate_measure", evaluate_mock),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
    ):
        await run_job(job_id)

    # evaluate_measure MUST have been called despite partial gather
    evaluate_mock.assert_awaited_once()

    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        mr = results[0]
        # populations come from evaluate (real data, not all-False error row)
        assert mr.populations is not None
        assert mr.populations.get("error") is not True
        # partial gather warning annotated on the result
        assert mr.error_phase == "gather_partial"
        assert mr.error_details is not None
        assert "Observation" in mr.error_details["failed_types"]
        assert "Patient" in mr.error_details["succeeded_types"] or "Condition" in mr.error_details["succeeded_types"]


async def test_run_job_evaluate_failure_persists_error_details_and_back_compat(
    test_session, session_factory, mock_measure_report
):
    """Evaluate phase failures persist error_details AND back-compat error_message."""
    from app.services.fhir_errors import FhirOperationError

    job_id = await _setup_job(test_session)
    patients = [
        {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alice"], "family": "Test"}]},
    ]

    fhir_err = FhirOperationError(
        operation="evaluate-measure",
        url="http://mcs/fhir/Measure/m1/$evaluate-measure",
        status_code=404,
        outcome=None,
        latency_ms=42,
    )

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
            return_value=GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}]),
        ),
        patch("app.services.orchestrator.push_resources", new_callable=AsyncMock),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            side_effect=fhir_err,
        ),
        patch("app.services.orchestrator.settings.HAPI_SYNC_AFTER_UPLOAD", False),
        patch("app.services.orchestrator.settings.HAPI_INDEX_WAIT_SECONDS", 0),
    ):
        await run_job(job_id)

    async with session_factory() as session:
        result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
        results = result.scalars().all()
        assert len(results) == 1
        mr = results[0]
        assert mr.populations["error"] is True
        # Back-compat: sanitized string still written
        assert mr.populations["error_message"]
        # Structured details written
        assert mr.error_details is not None
        assert mr.error_details["operation"] == "evaluate-measure"
        assert mr.error_details["status_code"] == 404
        # error_phase set to evaluate
        assert mr.error_phase == "evaluate"

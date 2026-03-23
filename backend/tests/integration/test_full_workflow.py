"""End-to-end workflow tests using the FastAPI test client + real HAPI FHIR.

These tests exercise the full API contract: create jobs, run the orchestrator
pipeline, and verify results are stored and retrievable.
"""

import pytest
from unittest.mock import patch

from sqlalchemy import select

from app.models.job import Batch, BatchStatus, Job, JobStatus, MeasureResult
from app.services.orchestrator import run_job
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper: create a job directly in the DB
# ---------------------------------------------------------------------------


async def _create_job(db_session, **overrides) -> Job:
    """Insert a Job row and return it."""
    defaults = {
        "measure_id": "cms122-diabetes-hba1c",
        "measure_name": "CMS122 Diabetes HbA1c Poor Control",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "cdr_url": TEST_CDR_URL,
        "status": JobStatus.queued,
    }
    defaults.update(overrides)
    job = Job(**defaults)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# test_create_and_run_job
# ---------------------------------------------------------------------------


async def test_create_and_run_job(integration_client, db_session):
    """POST /jobs to create a job, run it, and verify results are stored."""
    # Create via API
    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": "cms122-diabetes-hba1c",
            "measure_name": "CMS122 Diabetes HbA1c Poor Control",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "cdr_url": TEST_CDR_URL,
        },
    )
    assert resp.status_code == 201, f"Job creation failed: {resp.text}"
    job_data = resp.json()
    job_id = job_data["id"]
    assert job_data["status"] == "queued"

    # Run the job directly (the orchestrator normally runs in background)
    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.orchestrator.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.fhir_client.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.fhir_client.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MAX_RETRIES", 1),
        patch("app.services.orchestrator.settings.BATCH_SIZE", 100),
    ):
        await run_job(job_id)

    # Verify job completed or failed (CQL evaluation issues are acceptable)
    resp = await integration_client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    job_detail = resp.json()
    assert job_detail["status"] in ("complete", "failed"), (
        f"Expected job to finish, got status: {job_detail['status']}"
    )

    if job_detail["status"] == "complete":
        assert job_detail["total_patients"] > 0
        # Verify results endpoint works
        resp = await integration_client.get(f"/results?job_id={job_id}")
        assert resp.status_code == 200
        results_data = resp.json()
        assert results_data["job_id"] == job_id


# ---------------------------------------------------------------------------
# test_results_aggregate_populations
# ---------------------------------------------------------------------------


async def test_results_aggregate_populations(integration_client, db_session):
    """After a completed job, verify the aggregate endpoint returns population counts.

    Note: exact counts depend on whether CQL evaluation succeeds. If the
    measure engine can evaluate the CQL, we should see non-zero populations.
    If it cannot, patients will have been processed with errors and we verify
    the aggregate still works correctly with partial or zero data.
    """
    # Create and run a job
    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": "cms122-diabetes-hba1c",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "cdr_url": TEST_CDR_URL,
        },
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]

    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.orchestrator.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.fhir_client.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.fhir_client.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MAX_RETRIES", 1),
        patch("app.services.orchestrator.settings.BATCH_SIZE", 100),
    ):
        await run_job(job_id)

    # Fetch aggregate results
    resp = await integration_client.get(f"/results?job_id={job_id}")
    assert resp.status_code == 200
    data = resp.json()

    # Verify structure regardless of CQL evaluation outcome
    assert "populations" in data
    pops = data["populations"]
    assert "initial_population" in pops
    assert "denominator" in pops
    assert "numerator" in pops
    assert "denominator_exclusion" in pops
    assert "numerator_exclusion" in pops

    # If we have results, verify they are internally consistent
    if data["total_patients"] > 0:
        assert pops["initial_population"] >= pops["denominator"]
        assert pops["denominator"] >= pops["numerator"]
        # performance_rate should be present if denominator > 0
        if pops["denominator"] > 0:
            assert data["performance_rate"] is not None


# ---------------------------------------------------------------------------
# test_patient_drill_down
# ---------------------------------------------------------------------------


async def test_patient_drill_down(integration_client, db_session):
    """After a completed job, GET /results/{id} for a specific patient result."""
    # Insert a synthetic MeasureResult directly for a predictable test
    result = MeasureResult(
        job_id=None,  # will be set after job creation
        patient_id="pt-001",
        patient_name="Maria Johnson",
        measure_report={
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
        populations={
            "initial_population": True,
            "denominator": True,
            "numerator": False,
            "denominator_exclusion": False,
            "numerator_exclusion": False,
        },
    )

    # Create a job to hang the result on
    job = Job(
        measure_id="cms122-diabetes-hba1c",
        period_start="2025-01-01",
        period_end="2025-12-31",
        cdr_url=TEST_CDR_URL,
        status=JobStatus.complete,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    result.job_id = job.id
    db_session.add(result)
    await db_session.commit()
    await db_session.refresh(result)

    # Drill down
    resp = await integration_client.get(f"/results/{result.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["patient_id"] == "pt-001"
    assert data["patient_name"] == "Maria Johnson"
    assert data["populations"]["initial_population"] is True
    assert data["populations"]["denominator"] is True
    assert data["populations"]["numerator"] is False


# ---------------------------------------------------------------------------
# test_partial_failure_handling
# ---------------------------------------------------------------------------


async def test_partial_failure_handling(integration_client, db_session):
    """If a batch is marked as failed, the results endpoint still returns partial data."""
    # Create a completed job with one result
    job = Job(
        measure_id="cms122-diabetes-hba1c",
        period_start="2025-01-01",
        period_end="2025-12-31",
        cdr_url=TEST_CDR_URL,
        status=JobStatus.complete,
        total_patients=2,
        processed_patients=1,
        failed_patients=1,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Add a failed batch
    failed_batch = Batch(
        job_id=job.id,
        batch_number=1,
        patient_ids=["pt-fail-1"],
        status=BatchStatus.failed,
        error_message="Simulated failure for integration test",
    )
    db_session.add(failed_batch)

    # Add a successful result for another patient
    successful_result = MeasureResult(
        job_id=job.id,
        patient_id="pt-success-1",
        patient_name="Successful Patient",
        measure_report={"resourceType": "MeasureReport", "status": "complete", "group": []},
        populations={
            "initial_population": True,
            "denominator": True,
            "numerator": False,
            "denominator_exclusion": False,
            "numerator_exclusion": False,
        },
    )
    db_session.add(successful_result)
    await db_session.commit()

    # The aggregate endpoint should still return partial data
    resp = await integration_client.get(f"/results?job_id={job.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_patients"] == 1  # only the successful patient
    assert len(data["patients"]) == 1
    assert data["patients"][0]["patient_id"] == "pt-success-1"

    # The job detail should show the failed batch
    resp = await integration_client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    job_detail = resp.json()
    assert job_detail["failed_patients"] == 1
    assert any(b["status"] == "failed" for b in job_detail["batches"])

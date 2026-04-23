"""Tests for result endpoints (GET /results, GET /results/{id}, GET /results/{id}/evaluated-resources)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models.job import Job, JobStatus, MeasureResult

pytestmark = pytest.mark.asyncio


async def _create_job_with_results(session, num_results=2):
    """Helper: insert a job with measure results and return (job, results)."""
    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://example.com/fhir",
        status=JobStatus.complete,
        total_patients=num_results,
        processed_patients=num_results,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    results = []
    for i in range(num_results):
        mr = MeasureResult(
            job_id=job.id,
            patient_id=f"patient-{i}",
            patient_name=f"Patient {i}",
            measure_report={
                "resourceType": "MeasureReport",
                "id": f"report-{i}",
                "evaluatedResource": [
                    {"reference": f"Patient/patient-{i}"},
                    {"reference": f"Condition/cond-{i}"},
                ],
            },
            populations={
                "initial_population": True,
                "denominator": True,
                "numerator": i == 0,  # Only first patient in numerator
                "denominator_exclusion": False,
                "numerator_exclusion": False,
            },
        )
        session.add(mr)
        results.append(mr)

    await session.commit()
    for r in results:
        await session.refresh(r)
    return job, results


async def test_get_results_with_population_counts(client, test_session):
    """GET /results?job_id=X returns aggregate population counts."""
    job, results = await _create_job_with_results(test_session, num_results=2)

    resp = await client.get(f"/results?job_id={job.id}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["job_id"] == job.id
    assert data["total_patients"] == 2
    assert data["populations"]["initial_population"] == 2
    assert data["populations"]["denominator"] == 2
    assert data["populations"]["numerator"] == 1
    assert data["populations"]["denominator_exclusion"] == 0
    assert data["populations"]["numerator_exclusion"] == 0
    # Performance rate = 1/2 * 100 = 50.0
    assert data["performance_rate"] == 50.0
    assert len(data["patients"]) == 2
    assert data["failed_patients"] == 0
    assert data["patients"][0]["status"] == "success"


async def test_get_results_includes_patient_evaluation_errors(client, test_session):
    """GET /results includes failed patient rows without counting them in populations."""
    job = Job(
        measure_id="measure-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://example.com/fhir",
        status=JobStatus.failed,
        total_patients=1,
        failed_patients=1,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="patient-error",
        patient_name="Patient Error",
        measure_report={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "processing", "diagnostics": "HAPI returned 400"}],
        },
        populations={
            "initial_population": False,
            "denominator": False,
            "numerator": False,
            "denominator_exclusion": False,
            "numerator_exclusion": False,
            "error": True,
            "error_message": "HAPI returned 400",
        },
    )
    test_session.add(mr)
    await test_session.commit()

    resp = await client.get(f"/results?job_id={job.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_patients"] == 1
    assert data["failed_patients"] == 1
    assert data["populations"]["initial_population"] == 0
    assert data["patients"][0]["status"] == "error"
    assert data["patients"][0]["error_message"] == "HAPI returned 400"


async def test_get_results_empty(client):
    """GET /results?job_id=X with no results returns empty structure."""
    resp = await client.get("/results?job_id=999")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == 999
    assert data["total_patients"] == 0
    assert data["performance_rate"] is None
    assert data["patients"] == []


async def test_get_results_missing_job_id(client):
    """GET /results without job_id query param returns 422."""
    resp = await client.get("/results")
    assert resp.status_code == 422


async def test_get_individual_result(client, test_session):
    """GET /results/{id} returns full result with measure report."""
    job, results = await _create_job_with_results(test_session, num_results=1)
    result_id = results[0].id

    resp = await client.get(f"/results/{result_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == result_id
    assert data["job_id"] == job.id
    assert data["patient_id"] == "patient-0"
    assert data["patient_name"] == "Patient 0"
    assert data["measure_report"]["resourceType"] == "MeasureReport"
    assert data["populations"]["initial_population"] is True


async def test_get_individual_result_not_found(client):
    """GET /results/{id} with non-existent ID returns 404."""
    resp = await client.get("/results/99999")
    assert resp.status_code == 404
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert data["issue"][0]["code"] == "not-found"


async def test_get_evaluated_resources_success(client, test_session):
    """GET /results/{id}/evaluated-resources resolves references from the measure engine."""
    job, results = await _create_job_with_results(test_session, num_results=1)
    result_id = results[0].id

    patient_resource = {"resourceType": "Patient", "id": "patient-0"}
    condition_resource = {"resourceType": "Condition", "id": "cond-0"}

    async def mock_resolve(reference):
        if "Patient" in reference:
            return patient_resource
        return condition_resource

    with patch(
        "app.routes.results.resolve_evaluated_resource",
        new_callable=AsyncMock,
        side_effect=mock_resolve,
    ):
        resp = await client.get(f"/results/{result_id}/evaluated-resources")

    assert resp.status_code == 200
    data = resp.json()
    assert data["result_id"] == result_id
    assert data["patient_id"] == "patient-0"
    assert data["total_references"] == 2
    assert data["resolved"] == 2
    assert len(data["resources"]) == 2
    assert data["errors"] is None


async def test_get_evaluated_resources_partial_failure(client, test_session):
    """GET /results/{id}/evaluated-resources handles partial resolution failures."""
    job, results = await _create_job_with_results(test_session, num_results=1)
    result_id = results[0].id

    patient_resource = {"resourceType": "Patient", "id": "patient-0"}

    call_count = 0

    async def mock_resolve(reference):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return patient_resource
        raise Exception("Resource not found")

    with patch(
        "app.routes.results.resolve_evaluated_resource",
        new_callable=AsyncMock,
        side_effect=mock_resolve,
    ):
        resp = await client.get(f"/results/{result_id}/evaluated-resources")

    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved"] == 1
    assert len(data["resources"]) == 1
    assert len(data["errors"]) == 1
    assert "Resource not found" in data["errors"][0]["error"]


async def test_get_evaluated_resources_not_found(client):
    """GET /results/{id}/evaluated-resources with non-existent ID returns 404."""
    resp = await client.get("/results/99999/evaluated-resources")
    assert resp.status_code == 404


async def test_get_evaluated_resources_error_does_not_leak_hostname(client, test_session):
    """Regression: internal hostnames must not appear in evaluated-resource error entries.

    When resolve_evaluated_resource raises an exception whose message contains an
    internal Docker-network hostname (hapi-fhir-measure:8080), sanitize_error()
    must strip it before the client sees it in the response body errors list.
    """
    job, results = await _create_job_with_results(test_session, num_results=1)
    result_id = results[0].id

    with patch(
        "app.routes.results.resolve_evaluated_resource",
        new_callable=AsyncMock,
        side_effect=Exception("Connection refused at http://hapi-fhir-measure:8080/fhir"),
    ):
        resp = await client.get(f"/results/{result_id}/evaluated-resources")

    assert resp.status_code == 200
    body = resp.text
    assert "hapi-fhir-measure" not in body
    assert "8080" not in body
    data = resp.json()
    assert data["errors"] is not None
    assert len(data["errors"]) > 0

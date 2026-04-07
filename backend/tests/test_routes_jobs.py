"""Tests for job endpoints (POST /jobs, GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel)."""

import pytest

pytestmark = pytest.mark.asyncio


async def test_create_job_valid(client):
    """POST /jobs with valid payload creates a job with QUEUED status."""
    payload = {
        "measure_id": "measure-1",
        "measure_name": "Test Measure",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "http://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["measure_id"] == "measure-1"
    assert data["measure_name"] == "Test Measure"
    assert data["period_start"] == "2024-01-01"
    assert data["period_end"] == "2024-12-31"
    assert data["cdr_url"] == "http://example.com/fhir"
    assert data["status"] == "queued"
    assert data["total_patients"] == 0
    assert data["processed_patients"] == 0
    assert data["failed_patients"] == 0
    assert data["id"] is not None


async def test_create_job_missing_fields(client):
    """POST /jobs with missing required fields returns 422."""
    # Missing measure_id, period_start, period_end
    payload = {"measure_name": "Incomplete"}
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_uses_default_cdr_url(client):
    """POST /jobs without cdr_url falls back to default."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    # Should use the DEFAULT_CDR_URL from settings
    assert data["cdr_url"] is not None
    assert len(data["cdr_url"]) > 0


async def test_list_jobs_empty(client):
    """GET /jobs on empty database returns an empty list."""
    resp = await client.get("/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_jobs_returns_created_jobs(client):
    """GET /jobs returns all created jobs."""
    # Create two jobs
    for i in range(2):
        await client.post(
            "/jobs",
            json={
                "measure_id": f"measure-{i}",
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
                "cdr_url": "http://example.com/fhir",
            },
        )

    resp = await client.get("/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # Both jobs should be present
    measure_ids = {j["measure_id"] for j in data}
    assert measure_ids == {"measure-0", "measure-1"}


async def test_get_job_with_batches(client):
    """GET /jobs/{id} returns job details including batches list."""
    create_resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "cdr_url": "http://example.com/fhir",
        },
    )
    job_id = create_resp.json()["id"]

    resp = await client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == job_id
    assert data["measure_id"] == "measure-1"
    assert "batches" in data
    assert isinstance(data["batches"], list)


async def test_get_job_not_found(client):
    """GET /jobs/{id} with non-existent ID returns 404."""
    resp = await client.get("/jobs/99999")
    assert resp.status_code == 404
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert data["issue"][0]["code"] == "not-found"


async def test_cancel_job_queued(client):
    """POST /jobs/{id}/cancel cancels a queued job."""
    create_resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "cdr_url": "http://example.com/fhir",
        },
    )
    job_id = create_resp.json()["id"]

    resp = await client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"
    assert data["completed_at"] is not None


async def test_cancel_job_not_found(client):
    """POST /jobs/{id}/cancel with non-existent ID returns 404."""
    resp = await client.post("/jobs/99999/cancel")
    assert resp.status_code == 404


async def test_cancel_already_complete_job(client, test_session):
    """POST /jobs/{id}/cancel on a completed job returns 409."""
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="m-1",
        period_start="2024-01-01",
        period_end="2024-12-31",
        cdr_url="http://example.com/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    resp = await client.post(f"/jobs/{job.id}/cancel")
    assert resp.status_code == 409
    data = resp.json()["detail"]
    assert data["issue"][0]["code"] == "conflict"


async def test_create_job_with_group_id(client):
    """POST /jobs with group_id stores it on the job."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "http://example.com/fhir",
        "group_id": "CMS349FHIRHIVScreening",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["group_id"] == "CMS349FHIRHIVScreening"


async def test_create_job_without_group_id(client):
    """POST /jobs without group_id defaults to null."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "http://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["group_id"] is None


async def test_get_groups_success(client):
    """GET /jobs/groups returns list of groups from CDR."""
    from unittest.mock import patch, AsyncMock

    mock_groups = [
        {"id": "CMS122FHIRDiabetes", "name": "CMS122 Diabetes", "type": "person", "member_count": 20},
        {"id": "CMS349FHIRHIVScreening", "name": "CMS349 HIV Screening", "type": "person", "member_count": 36},
    ]
    with patch("app.routes.jobs.list_groups", new=AsyncMock(return_value=mock_groups)):
        resp = await client.get("/jobs/groups")
    assert resp.status_code == 200
    data = resp.json()
    assert "groups" in data
    assert len(data["groups"]) == 2
    assert data["groups"][0]["id"] == "CMS122FHIRDiabetes"


async def test_get_groups_cdr_unreachable(client):
    """GET /jobs/groups returns 502 when CDR is unreachable."""
    import httpx
    from unittest.mock import patch, AsyncMock

    with patch("app.routes.jobs.list_groups", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = await client.get("/jobs/groups")
    assert resp.status_code == 502
    assert "CDR" in resp.json()["detail"]

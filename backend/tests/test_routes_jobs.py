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
        "cdr_url": "https://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["measure_id"] == "measure-1"
    assert data["measure_name"] == "Test Measure"
    assert data["period_start"] == "2024-01-01"
    assert data["period_end"] == "2024-12-31"
    assert data["cdr_url"] == "https://example.com/fhir"
    assert data["status"] == "queued"
    assert data["total_patients"] == 0
    assert data["processed_patients"] == 0
    assert data["failed_patients"] == 0
    assert data["id"] is not None
    assert "cdr_name" in data
    assert "cdr_read_only" in data


async def test_create_job_ssrf_cdr_url_blocked(client):
    """POST /jobs with a private IP cdr_url override returns 400."""
    payload = {
        "measure_id": "measure-1",
        "period_start": "2024-01-01",
        "period_end": "2024-12-31",
        "cdr_url": "https://169.254.169.254/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 400
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "SSRF protection" in diag


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
    assert data["cdr_read_only"] is False


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
                "cdr_url": "https://example.com/fhir",
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
            "cdr_url": "https://example.com/fhir",
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
            "cdr_url": "https://example.com/fhir",
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
        "cdr_url": "https://example.com/fhir",
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
        "cdr_url": "https://example.com/fhir",
    }
    resp = await client.post("/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["group_id"] is None


async def test_get_groups_success(client):
    """GET /jobs/groups returns list of groups from CDR."""
    from unittest.mock import AsyncMock, patch

    mock_groups = [
        {"id": "CMS122FHIRDiabetes", "name": "CMS122 Diabetes", "type": "person", "member_count": 20},
        {"id": "CMS349FHIRHIVScreening", "name": "CMS349 HIV Screening", "type": "person", "member_count": 36},
    ]
    with (
        patch("app.routes.jobs.list_groups", new=AsyncMock(return_value=mock_groups)) as mock_lg,
        patch("app.routes.jobs._build_auth_headers", new=AsyncMock(return_value={})) as mock_auth,
    ):
        resp = await client.get("/jobs/groups")

    assert resp.status_code == 200
    mock_auth.assert_called_once()
    mock_lg.assert_called_once()
    data = resp.json()
    assert "groups" in data
    assert len(data["groups"]) == 2
    assert data["groups"][0]["id"] == "CMS122FHIRDiabetes"


async def test_get_groups_cdr_unreachable(client):
    """GET /jobs/groups returns 502 when CDR is unreachable."""
    from unittest.mock import AsyncMock, patch

    import httpx

    with patch("app.routes.jobs.list_groups", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        resp = await client.get("/jobs/groups")
    assert resp.status_code == 502
    assert "CDR" in resp.json()["detail"]


async def test_create_job_stamps_active_cdr_metadata(client, test_session):
    """POST /jobs stamps cdr_name and cdr_read_only from the active CDR config."""
    from sqlalchemy import update as sa_update

    from app.models.config import AuthType, CDRConfig

    # Deactivate any existing active CDR rows first
    await test_session.execute(sa_update(CDRConfig).values(is_active=False))
    await test_session.commit()

    cdr = CDRConfig(
        cdr_url="http://prod-cdr.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        name="Production CDR",
        is_default=False,
        is_read_only=True,
    )
    test_session.add(cdr)
    await test_session.commit()

    resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["cdr_url"] == "http://prod-cdr.example.com/fhir"
    assert data["cdr_name"] == "Production CDR"
    assert data["cdr_read_only"] is True


async def test_create_job_stamps_cdr_auth_type_as_string_value(client, test_session):
    """POST /jobs stamps cdr_auth_type as the raw string value (e.g. 'bearer'), not 'AuthType.bearer'."""
    from sqlalchemy import update as sa_update

    from app.models.config import AuthType, CDRConfig

    # Deactivate any existing active CDR rows first
    await test_session.execute(sa_update(CDRConfig).values(is_active=False))
    await test_session.commit()

    cdr = CDRConfig(
        cdr_url="http://auth-cdr.example.com/fhir",
        auth_type=AuthType.bearer,
        is_active=True,
        name="Auth CDR",
        is_default=False,
        is_read_only=False,
        auth_credentials="my-token",
    )
    test_session.add(cdr)
    await test_session.commit()

    resp = await client.post(
        "/jobs",
        json={
            "measure_id": "measure-1",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
        },
    )
    assert resp.status_code == 201

    # Verify the stamped value in the DB is the plain string "bearer", not "AuthType.bearer"
    from sqlalchemy import select

    from app.models.job import Job

    result = await test_session.execute(select(Job).order_by(Job.id.desc()).limit(1))
    job = result.scalar_one()
    assert job.cdr_auth_type == "bearer"


# ---------------------------------------------------------------------------
# GET /jobs/{id}/comparison
# ---------------------------------------------------------------------------


async def test_get_comparison_httpx_exception_returns_no_expected(client, test_session):
    """Returns has_expected=False when httpx raises while resolving measure URL."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={"resourceType": "MeasureReport", "group": []},
        populations={"initial_population": True},
    )
    test_session.add(mr)
    await test_session.commit()

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=_httpx.ConnectError("unreachable"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is False


async def test_get_comparison_patient_not_in_expected_skipped(client, test_session):
    """Actual patients with no matching ExpectedResult are excluded from comparison output."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    # p1 has expected results; p2 does not
    for pid in ("p1", "p2"):
        test_session.add(
            MeasureResult(
                job_id=job.id,
                patient_id=pid,
                measure_report={
                    "resourceType": "MeasureReport",
                    "group": [
                        {
                            "population": [
                                {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                            ]
                        }
                    ],
                },
                populations={"initial_population": True},
            )
        )

    er = ExpectedResult(
        measure_url="https://example.com/Measure/CMS124",
        patient_ref="p1",
        expected_populations={"initial-population": 1},
        period_start="2019-01-01",
        period_end="2019-12-31",
        source_bundle="test",
    )
    test_session.add(er)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    # Only p1 appears — p2 had no expected result and was skipped
    assert data["total"] == 1
    assert data["patients"][0]["subject_reference"] == "Patient/p1"


async def test_get_comparison_with_mismatch(client, test_session):
    """Returns match=False and mismatches list when actual populations differ from expected."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    # Actual: numerator=0; Expected: numerator=1 → mismatch
    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={
            "resourceType": "MeasureReport",
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
        populations={"initial_population": True, "denominator": True, "numerator": False},
    )
    test_session.add(mr)

    er = ExpectedResult(
        measure_url="https://example.com/Measure/CMS124",
        patient_ref="p1",
        expected_populations={"initial-population": 1, "denominator": 1, "numerator": 1},
        period_start="2019-01-01",
        period_end="2019-12-31",
        source_bundle="test",
    )
    test_session.add(er)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    assert data["matched"] == 0
    assert data["total"] == 1
    patient = data["patients"][0]
    assert patient["match"] is False
    assert len(patient["mismatches"]) > 0


async def test_get_comparison_no_job(client):
    """Returns 404 when job does not exist."""
    resp = await client.get("/jobs/999/comparison")
    assert resp.status_code == 404


async def test_get_comparison_no_results(client, test_session):
    """Returns has_expected=False when no MeasureResults exist for job."""
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    resp = await client.get(f"/jobs/{job.id}/comparison")
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is False
    assert data["patients"] == []


async def test_get_comparison_no_expected_in_db(client, test_session):
    """Returns has_expected=False when MeasureResults exist but no ExpectedResult in DB."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={"resourceType": "MeasureReport", "group": []},
        populations={"initial_population": True},
    )
    test_session.add(mr)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is False


async def test_get_comparison_with_match(client, test_session):
    """Returns comparison data when expected results exist and populations match."""
    from unittest.mock import AsyncMock, patch

    import httpx as _httpx

    from app.models.job import Job, JobStatus, MeasureResult
    from app.models.validation import ExpectedResult

    job = Job(
        measure_id="CMS124",
        period_start="2019-01-01",
        period_end="2019-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.complete,
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)

    mr = MeasureResult(
        job_id=job.id,
        patient_id="p1",
        measure_report={
            "resourceType": "MeasureReport",
            "group": [
                {
                    "population": [
                        {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                        {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                        {"code": {"coding": [{"code": "numerator"}]}, "count": 1},
                    ]
                }
            ],
        },
        populations={"initial_population": True, "denominator": True, "numerator": True},
    )
    test_session.add(mr)

    er = ExpectedResult(
        measure_url="https://example.com/Measure/CMS124",
        patient_ref="p1",
        expected_populations={"initial-population": 1, "denominator": 1, "numerator": 1},
        period_start="2019-01-01",
        period_end="2019-12-31",
        source_bundle="test",
    )
    test_session.add(er)
    await test_session.commit()

    measure_json = {"resourceType": "Measure", "id": "CMS124", "url": "https://example.com/Measure/CMS124"}
    mock_resp = _httpx.Response(200, json=measure_json, request=_httpx.Request("GET", "http://test"))

    with patch("app.routes.jobs.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get(f"/jobs/{job.id}/comparison")

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_expected"] is True
    assert data["matched"] == 1
    assert data["total"] == 1
    assert data["patients"][0]["match"] is True
    assert data["patients"][0]["subject_reference"] == "Patient/p1"

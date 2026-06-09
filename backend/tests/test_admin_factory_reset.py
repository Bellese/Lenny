"""Unit tests for the admin factory-reset and reseed-bundles endpoints."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.admin_operation import AdminOperation, AdminOperationKind, AdminOperationStatus


def make_session_patcher(test_session):
    """Return a context-manager patcher that routes async_session() calls to test_session."""

    @asynccontextmanager
    async def _fake_session():
        yield test_session

    return _fake_session


@pytest.mark.asyncio
async def test_factory_reset_accepted(client):
    """POST /settings/admin/factory-reset returns 202 with operation_id."""
    # Patch the background task so it never runs; we only check the 202 response here.
    with patch("app.routes.settings._run_factory_reset", new_callable=AsyncMock):
        resp = await client.post(
            "/settings/admin/factory-reset",
            json={"include_cdr": True, "include_measure_engine": True, "include_app_db": True},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert "operation_id" in body
    assert isinstance(body["operation_id"], int)


@pytest.mark.asyncio
async def test_factory_reset_409_if_running_job(client, test_session):
    """POST /settings/admin/factory-reset returns 409 when a job is running."""
    from app.models.job import Job, JobStatus

    job = Job(
        measure_id="CMS122",
        period_start="2025-01-01",
        period_end="2025-12-31",
        cdr_url="http://cdr:8080/fhir",
        status=JobStatus.running,
    )
    test_session.add(job)
    await test_session.commit()

    resp = await client.post("/settings/admin/factory-reset", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_reseed_bundles_accepted(client):
    """POST /settings/admin/reseed-bundles returns 202 with operation_id."""
    with patch("app.routes.settings._run_reseed", new_callable=AsyncMock):
        resp = await client.post("/settings/admin/reseed-bundles")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"
    assert "operation_id" in body


@pytest.mark.asyncio
async def test_get_admin_operation_pending(client, test_session):
    """GET /settings/admin/operations/{id} returns the operation row."""
    op = AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        scopes_json={"include_cdr": True},
        started_at=datetime.now(timezone.utc),
    )
    test_session.add(op)
    await test_session.commit()
    await test_session.refresh(op)

    resp = await client.get(f"/settings/admin/operations/{op.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["kind"] == "factory_reset"
    assert body["scopes"]["include_cdr"] is True


@pytest.mark.asyncio
async def test_get_admin_operation_not_found(client):
    """GET /settings/admin/operations/99999 returns 404."""
    resp = await client.get("/settings/admin/operations/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_factory_reset_wipes_cdr_then_measure_engine(test_session):
    """_run_factory_reset calls wipe_patient_data with CDR URL first, then MEASURE_ENGINE_URL."""
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        started_at=datetime.now(timezone.utc),
    )
    test_session.add(op)
    await test_session.commit()
    await test_session.refresh(op)

    cdr_url = "http://hapi-fhir-cdr:8080/fhir"
    me_url = "http://hapi-fhir-measure:8080/fhir"
    wipe_calls = []

    async def mock_wipe(*, base_url, strict=True):
        wipe_calls.append(base_url)

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_patient_data", side_effect=mock_wipe),
        patch("app.routes.settings.wipe_measure_definitions", new_callable=AsyncMock),
        patch("app.routes.settings._poll_zero_counts", new_callable=AsyncMock),
        patch("app.routes.settings.settings") as mock_settings,
    ):
        mock_settings.DEFAULT_CDR_URL = cdr_url
        mock_settings.MEASURE_ENGINE_URL = me_url

        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=True, include_measure_engine=True, include_app_db=False),
        )

    assert cdr_url in wipe_calls, "CDR wipe must target CDR URL"
    assert me_url in wipe_calls, "Measure engine wipe must target measure engine URL"
    assert wipe_calls.index(cdr_url) < wipe_calls.index(me_url), "CDR wipe must precede ME wipe"


@pytest.mark.asyncio
async def test_run_factory_reset_sets_succeeded(test_session):
    """_run_factory_reset marks the operation as succeeded on completion."""
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        started_at=datetime.now(timezone.utc),
    )
    test_session.add(op)
    await test_session.commit()
    await test_session.refresh(op)

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_patient_data", new_callable=AsyncMock),
        patch("app.routes.settings.wipe_measure_definitions", new_callable=AsyncMock),
        patch("app.routes.settings._poll_zero_counts", new_callable=AsyncMock),
        patch("app.routes.settings.settings"),
    ):
        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=False, include_measure_engine=False, include_app_db=False),
        )

    await test_session.refresh(op)
    assert op.status == AdminOperationStatus.succeeded
    assert op.completed_at is not None


@pytest.mark.asyncio
async def test_run_factory_reset_sets_failed_on_error(test_session):
    """_run_factory_reset marks the operation as failed when a step raises."""
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        started_at=datetime.now(timezone.utc),
    )
    test_session.add(op)
    await test_session.commit()
    await test_session.refresh(op)

    async def boom(*, base_url, strict=True):
        raise RuntimeError("CDR unreachable")

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_patient_data", side_effect=boom),
        patch("app.routes.settings.settings"),
    ):
        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=True, include_measure_engine=False, include_app_db=False),
        )

    await test_session.refresh(op)
    assert op.status == AdminOperationStatus.failed
    assert op.error is not None
    assert op.completed_at is not None

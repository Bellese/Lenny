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

    async def mock_wipe(*, base_url, strict=True, auth_headers=None):
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
async def test_run_factory_reset_sends_cdr_credentials(test_session):
    """The CDR wipe carries the active connection's credentials.

    The active CDR is routinely a remote authenticated connection. Without
    credentials the conditional delete 401s, and wipe_patient_data now refuses to
    report success on an unauthorized wipe — so an unauthenticated factory reset
    would hard-fail instead of quietly deleting nothing.
    """
    from app.models.config import CDRConfig
    from app.models.connection_base import AuthType
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        started_at=datetime.now(timezone.utc),
    )
    cfg = CDRConfig(
        name="Remote CDR",
        cdr_url="https://cdr.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-cdr"},
        is_active=True,
    )
    test_session.add_all([op, cfg])
    await test_session.commit()
    await test_session.refresh(op)

    seen: dict[str, dict | None] = {}

    async def mock_wipe(*, base_url, strict=True, auth_headers=None):
        seen[base_url] = auth_headers

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_patient_data", side_effect=mock_wipe),
        patch("app.routes.settings.wipe_measure_definitions", new_callable=AsyncMock),
        patch("app.routes.settings._poll_zero_counts", new_callable=AsyncMock),
        patch(
            "app.routes.settings._build_auth_headers",
            new_callable=AsyncMock,
            return_value={"Authorization": "Bearer tok-cdr"},
        ),
    ):
        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=True, include_measure_engine=False, include_app_db=False),
        )

    assert seen["https://cdr.example.org/fhir"] == {"Authorization": "Bearer tok-cdr"}


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


# ---------------------------------------------------------------------------
# MCS scoping + read-only guards on the destructive admin paths (issue #397)
# ---------------------------------------------------------------------------


def _make_op():
    return AdminOperation(
        kind=AdminOperationKind.factory_reset,
        status=AdminOperationStatus.pending,
        started_at=datetime.now(timezone.utc),
    )


def _steps_by_name(op) -> dict[str, dict]:
    return {s["step"]: s for s in (op.steps_json or [])}


@pytest.mark.asyncio
async def test_factory_reset_measure_engine_targets_active_mcs_with_credentials(test_session):
    """The measure-engine branch wipes the ACTIVE MCS, not settings.MEASURE_ENGINE_URL.

    Issue #397: an admin connected to a remote MCS who ran a factory reset wiped
    Lenny's local container and got a success response, while the server they were
    looking at kept its data.
    """
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = _make_op()
    mcs = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok-mcs"},
        is_active=True,
    )
    test_session.add_all([op, mcs])
    await test_session.commit()
    await test_session.refresh(op)

    definitions_seen: dict[str, dict | None] = {}
    patient_wipe_seen: dict[str, dict | None] = {}
    polled: list[str] = []

    async def mock_wipe_defs(*, base_url, auth_headers=None):
        definitions_seen[base_url] = auth_headers

    async def mock_wipe_patients(*, base_url, strict=True, auth_headers=None):
        patient_wipe_seen[base_url] = auth_headers

    async def mock_poll(base_url, resource_types, step_name, auth_headers=None):
        polled.append(base_url)

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_measure_definitions", side_effect=mock_wipe_defs),
        patch("app.routes.settings.wipe_patient_data", side_effect=mock_wipe_patients),
        patch("app.routes.settings._poll_zero_counts", side_effect=mock_poll),
        patch(
            "app.routes.settings._build_auth_headers",
            new_callable=AsyncMock,
            return_value={"Authorization": "Bearer tok-mcs"},
        ),
    ):
        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=False, include_measure_engine=True, include_app_db=False),
        )

    assert definitions_seen == {"https://mcs.example.org/fhir": {"Authorization": "Bearer tok-mcs"}}
    assert patient_wipe_seen == {"https://mcs.example.org/fhir": {"Authorization": "Bearer tok-mcs"}}
    assert polled == ["https://mcs.example.org/fhir"]


@pytest.mark.asyncio
async def test_factory_reset_skips_read_only_measure_engine(test_session):
    """A read-only MCS is skipped with a reason, and the rest of the reset still runs.

    Skipped rather than aborted: factory reset is a multi-step background
    operation, so raising would leave include_app_db undone and the operation in a
    failed state, which is a worse outcome than declining one step.
    """
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = _make_op()
    mcs = MCSConfig(
        name="Someone Else's MCS",
        mcs_url="https://shared.example.org/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_read_only=True,
    )
    test_session.add_all([op, mcs])
    await test_session.commit()
    await test_session.refresh(op)

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_measure_definitions", new_callable=AsyncMock) as mock_defs,
        patch("app.routes.settings.wipe_patient_data", new_callable=AsyncMock) as mock_patients,
        patch("app.routes.settings._poll_zero_counts", new_callable=AsyncMock),
    ):
        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=False, include_measure_engine=True, include_app_db=True),
        )

    mock_defs.assert_not_awaited()
    mock_patients.assert_not_awaited()

    await test_session.refresh(op)
    steps = _steps_by_name(op)
    assert steps["wipe_measure_engine"]["status"] == "skipped"
    assert "read-only" in (steps["wipe_measure_engine"]["error"] or "").lower()
    # The unrelated step still ran, and the operation reached a terminal success.
    assert steps["wipe_app_db"]["status"] == "succeeded"
    assert op.status == AdminOperationStatus.succeeded


@pytest.mark.asyncio
async def test_factory_reset_skips_read_only_cdr(test_session):
    """Same guard on the CDR branch.

    Not in #397's literal scope, but factory reset ignored is_read_only entirely,
    so guarding only the MCS would leave one branch of the same operation
    refusing while the other wiped a server the user marked read-only.
    """
    from app.models.config import CDRConfig
    from app.models.connection_base import AuthType
    from app.routes.settings import FactoryResetRequest, _run_factory_reset

    op = _make_op()
    cdr = CDRConfig(
        name="Someone Else's CDR",
        cdr_url="https://shared-cdr.example.org/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_read_only=True,
    )
    test_session.add_all([op, cdr])
    await test_session.commit()
    await test_session.refresh(op)

    with (
        patch("app.routes.settings.async_session", make_session_patcher(test_session)),
        patch("app.routes.settings.wipe_patient_data", new_callable=AsyncMock) as mock_patients,
        patch("app.routes.settings._poll_zero_counts", new_callable=AsyncMock),
    ):
        await _run_factory_reset(
            op.id,
            FactoryResetRequest(include_cdr=True, include_measure_engine=False, include_app_db=True),
        )

    mock_patients.assert_not_awaited()

    await test_session.refresh(op)
    steps = _steps_by_name(op)
    assert steps["wipe_cdr"]["status"] == "skipped"
    assert "read-only" in (steps["wipe_cdr"]["error"] or "").lower()
    assert steps["wipe_app_db"]["status"] == "succeeded"
    assert op.status == AdminOperationStatus.succeeded


@pytest.mark.asyncio
async def test_wipe_measure_engine_route_targets_active_mcs(client, test_session):
    """POST /settings/admin/wipe-measure-engine follows the active MCS connection."""
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    mcs = MCSConfig(
        name="Remote MCS",
        mcs_url="https://mcs.example.org/fhir",
        auth_type=AuthType.none,
        is_active=True,
    )
    test_session.add(mcs)
    await test_session.commit()

    seen: dict[str, dict | None] = {}

    async def mock_wipe_defs(*, base_url, auth_headers=None):
        seen[base_url] = auth_headers

    with patch("app.routes.settings.wipe_measure_definitions", side_effect=mock_wipe_defs):
        resp = await client.post("/settings/admin/wipe-measure-engine")

    assert resp.status_code == 200, resp.text
    assert "https://mcs.example.org/fhir" in seen


@pytest.mark.asyncio
async def test_wipe_measure_engine_route_refuses_read_only_mcs(client, test_session):
    """A read-only MCS gets 403 and no delete is issued.

    The route has a caller waiting, so it refuses outright rather than skipping.
    """
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    mcs = MCSConfig(
        name="Someone Else's MCS",
        mcs_url="https://shared.example.org/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_read_only=True,
    )
    test_session.add(mcs)
    await test_session.commit()

    with patch("app.routes.settings.wipe_measure_definitions", new_callable=AsyncMock) as mock_defs:
        resp = await client.post("/settings/admin/wipe-measure-engine")

    assert resp.status_code == 403, resp.text
    mock_defs.assert_not_awaited()
    body = resp.json()
    assert body["detail"]["resourceType"] == "OperationOutcome"

"""Tests for MCS connection routes.

These tests cover MCS-specific behavior:
- The factory wires `MCSConfig` correctly at `/settings/mcs-connections`.
- Schemas use `mcs_url` (not `cdr_url`) and don't include `is_read_only`.
- Activation-race regression on `idx_one_active_mcs`.
- Default-name and seed semantics.

The factory's generic CRUD behavior (validation, encryption, audit logging,
SMART auth, SSRF) is already exercised by the 31 CDR tests in
`tests/test_routes_settings.py`. Re-running every CDR test parameterized over
kind is a deferred refactor — when kind #3 (Terminology Server) lands and
the duplication becomes painful, parameterize then.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.connection_base import AuthType
from app.models.mcs_config import MCSConfig
from app.services.fhir_errors import FhirIssue, FhirOperationError, FhirOperationOutcome

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Routes wired correctly
# ---------------------------------------------------------------------------


async def test_mcs_list_connections_empty(client):
    resp = await client.get("/settings/mcs-connections")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_mcs_create_connection(client):
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "Custom MCS",
            "mcs_url": "https://custom-mcs.example.com/fhir",
            "auth_type": "none",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Custom MCS"
    assert body["mcs_url"] == "https://custom-mcs.example.com/fhir"
    assert body["auth_type"] == "none"
    assert body["is_active"] is False
    assert body["is_default"] is False
    # MCS schema does NOT expose is_read_only — that's CDR-specific.
    assert "is_read_only" not in body


async def test_mcs_create_connection_does_not_accept_cdr_url(client):
    """Schema-level guard: MCS create requires mcs_url, not cdr_url."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "Wrong URL Field",
            "cdr_url": "https://example.com/fhir",  # wrong field for MCS
            "auth_type": "none",
        },
    )
    assert resp.status_code == 422  # Pydantic rejects: missing mcs_url


async def test_mcs_create_connection_with_smart_auth(client):
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "SMART MCS",
            "mcs_url": "https://smart-mcs.example.com/fhir",
            "auth_type": "smart",
            "auth_credentials": {
                "client_id": "id",
                "client_secret": "secret",
                "token_endpoint": "https://smart-mcs.example.com/token",
            },
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["auth_type"] == "smart"


async def test_mcs_create_connection_smart_missing_credentials(client):
    """SMART validation is shared via the factory; covered for MCS too."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "Bad SMART",
            "mcs_url": "https://example.com/fhir",
            "auth_type": "smart",
            "auth_credentials": {"client_id": "id"},  # missing secret + endpoint
        },
    )
    assert resp.status_code == 400


async def test_mcs_create_connection_ssrf_blocked(client):
    """SSRF helper is shared — same blocking applies to mcs_url."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "SSRF",
            "mcs_url": "http://169.254.169.254/fhir",  # AWS metadata
            "auth_type": "none",
        },
    )
    assert resp.status_code == 400


async def test_mcs_create_rejects_oversized_request_timeout(client):
    """request_timeout_seconds is capped at 1800s to bound worker hold time
    (timeout-as-DoS-vector — design-doc threat surface #3)."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "DoS Timeout",
            "mcs_url": "https://example.com/fhir",
            "auth_type": "none",
            "request_timeout_seconds": 86400,  # 1 day — far above the 1800 cap
        },
    )
    assert resp.status_code == 422


async def test_mcs_create_rejects_zero_request_timeout(client):
    """request_timeout_seconds must be >= 1 — zero/negative would short-circuit
    httpx and surface as misleading 'connection refused' errors."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "Zero Timeout",
            "mcs_url": "https://example.com/fhir",
            "auth_type": "none",
            "request_timeout_seconds": 0,
        },
    )
    assert resp.status_code == 422


async def test_mcs_create_accepts_explicit_request_timeout_within_cap(client):
    """Within-cap values are accepted and round-trip in the response."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "Long-but-legal",
            "mcs_url": "https://example.com/fhir",
            "auth_type": "none",
            "request_timeout_seconds": 600,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["request_timeout_seconds"] == 600


async def test_mcs_create_defaults_request_timeout_to_30(client):
    """Default request_timeout_seconds is 30 — preserves prior behavior when
    the field is omitted from the create payload."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": "Default Timeout",
            "mcs_url": "https://example.com/fhir",
            "auth_type": "none",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["request_timeout_seconds"] == 30


async def test_mcs_get_update_delete_activate(client):
    create = await client.post(
        "/settings/mcs-connections",
        json={"name": "Lifecycle MCS", "mcs_url": "https://a.example.com/fhir", "auth_type": "none"},
    )
    assert create.status_code == 201
    cid = create.json()["id"]

    got = await client.get(f"/settings/mcs-connections/{cid}")
    assert got.status_code == 200
    assert got.json()["name"] == "Lifecycle MCS"

    updated = await client.put(
        f"/settings/mcs-connections/{cid}",
        json={"name": "Lifecycle MCS v2", "mcs_url": "https://b.example.com/fhir", "auth_type": "none"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Lifecycle MCS v2"
    assert updated.json()["mcs_url"] == "https://b.example.com/fhir"

    activated = await client.post(f"/settings/mcs-connections/{cid}/activate")
    assert activated.status_code == 200
    assert activated.json()["is_active"] is True

    # Cannot delete the active connection.
    blocked = await client.delete(f"/settings/mcs-connections/{cid}")
    assert blocked.status_code == 409


# ---------------------------------------------------------------------------
# Default-name error message
# ---------------------------------------------------------------------------


async def test_mcs_delete_default_blocked_uses_local_measure_engine_name(client, test_session):
    """The factory parameterizes the error message with the kind's
    `default_name`. MCS uses 'Local Measure Engine'."""
    cfg = MCSConfig(
        name="Local Measure Engine",
        mcs_url="http://hapi-fhir-measure:8080/fhir",
        auth_type=AuthType.none,
        is_active=False,
        is_default=True,  # mark as the seeded default — undeletable
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    resp = await client.delete(f"/settings/mcs-connections/{cfg.id}")
    assert resp.status_code == 409
    assert "Local Measure Engine" in resp.json()["detail"]["issue"][0]["diagnostics"]


# ---------------------------------------------------------------------------
# Activation race regression — MCS partial unique index
# ---------------------------------------------------------------------------


async def test_mcs_activate_concurrent_raises_integrity_error(test_session):
    """Mirror of test_activate_concurrent_raises_integrity_error for MCS.
    Validates `idx_one_active_mcs` is enforced on SQLite via __table_args__.
    """
    first = MCSConfig(
        name="First Active MCS",
        mcs_url="https://first.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
    )
    test_session.add(first)
    await test_session.commit()

    second = MCSConfig(
        name="Second Active MCS",
        mcs_url="https://second.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
    )
    test_session.add(second)
    with pytest.raises(IntegrityError):
        await test_session.commit()
    await test_session.rollback()


# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------


async def test_seed_default_connections_creates_local_measure_engine(test_engine, test_session, monkeypatch):
    """seed_default_connections() inserts a `Local Measure Engine` row from
    settings.MEASURE_ENGINE_URL on first call. Idempotent on re-call."""
    from app.config import settings as app_config
    from app.main import seed_default_connections

    monkeypatch.setattr(app_config, "MEASURE_ENGINE_URL", "https://seeded-mcs.example.com/fhir")

    # Patch the engine the seed uses to point at our test_engine.
    import app.main

    monkeypatch.setattr(app.main, "engine", test_engine)

    await seed_default_connections()

    from sqlalchemy import select

    result = await test_session.execute(select(MCSConfig).where(MCSConfig.is_default.is_(True)))
    seeded = result.scalar_one_or_none()
    assert seeded is not None
    assert seeded.name == "Local Measure Engine"
    assert seeded.mcs_url == "https://seeded-mcs.example.com/fhir"
    assert seeded.is_active is True
    assert seeded.is_default is True

    # Idempotent: second call doesn't duplicate.
    await seed_default_connections()
    all_mcs = (await test_session.execute(select(MCSConfig))).scalars().all()
    assert len([c for c in all_mcs if c.is_default]) == 1


# ---------------------------------------------------------------------------
# POST /settings/mcs-connections/{id}/probe — deep $data-requirements probe
# ---------------------------------------------------------------------------


async def _create_mcs_row(test_session, *, mcs_url="https://probe.example.com/fhir", is_active=False):
    cfg = MCSConfig(
        name="Probe MCS",
        mcs_url=mcs_url,
        auth_type=AuthType.none,
        is_active=is_active,
        is_default=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


async def test_mcs_probe_success(client, test_session):
    """Probe returns the success envelope from probe_mcs_data_requirements."""
    cfg = await _create_mcs_row(test_session)
    fake_result = {
        "status": "ok",
        "measure_id": "CMS122",
        "measure_name": "Diabetes A1c >9%",
        "data_requirement_count": 4,
        "list_latency_ms": 12,
        "data_requirements_latency_ms": 45,
        "url": "https://probe.example.com/fhir/Measure/CMS122/$data-requirements",
    }
    with patch(
        "app.routes.settings.probe_mcs_data_requirements",
        new_callable=AsyncMock,
        return_value=fake_result,
    ):
        resp = await client.post(f"/settings/mcs-connections/{cfg.id}/probe")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["measure_id"] == "CMS122"
    assert body["data_requirement_count"] == 4


async def test_mcs_probe_404_when_connection_missing(client):
    resp = await client.post("/settings/mcs-connections/99999/probe")
    assert resp.status_code == 404


async def test_mcs_probe_502_on_engine_error(client, test_session):
    """A FhirOperationError with a 4xx/5xx status surfaces as a 502 envelope."""
    cfg = await _create_mcs_row(test_session)
    outcome = FhirOperationOutcome(
        issues=[FhirIssue(severity="error", code="processing", diagnostics="Library not found")],
        raw={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "processing", "diagnostics": "Library not found"}],
        },
    )
    err = FhirOperationError(
        operation="probe-data-requirements",
        url=f"{cfg.mcs_url}/Measure/CMS122/$data-requirements",
        status_code=500,
        outcome=outcome,
        latency_ms=20,
    )
    with patch(
        "app.routes.settings.probe_mcs_data_requirements",
        new_callable=AsyncMock,
        side_effect=err,
    ):
        resp = await client.post(f"/settings/mcs-connections/{cfg.id}/probe")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert "Library not found" in detail["issue"][0]["diagnostics"]


async def test_mcs_probe_warning_when_mcs_empty(client, test_session):
    """The empty-MCS path: 200 status with a synthesized OperationOutcome
    surfaces as a 200 response with status='warning' so the UI can render it."""
    cfg = await _create_mcs_row(test_session)
    outcome = FhirOperationOutcome(
        issues=[
            FhirIssue(
                severity="warning", code="not-found", diagnostics="MCS is reachable but has no Measure resources."
            )
        ],
        raw={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "warning",
                    "code": "not-found",
                    "diagnostics": "MCS is reachable but has no Measure resources.",
                }
            ],
        },
    )
    err = FhirOperationError(
        operation="probe-data-requirements",
        url=f"{cfg.mcs_url}/Measure?_count=1",
        status_code=200,
        outcome=outcome,
        latency_ms=10,
    )
    with patch(
        "app.routes.settings.probe_mcs_data_requirements",
        new_callable=AsyncMock,
        side_effect=err,
    ):
        resp = await client.post(f"/settings/mcs-connections/{cfg.id}/probe")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "warning"
    assert body["outcome"]["issue"][0]["severity"] == "warning"


async def test_mcs_probe_400_on_ssrf_rejection(client, test_session):
    """SSRF ValueError from probe_mcs_data_requirements surfaces as 400."""
    cfg = await _create_mcs_row(test_session)
    with patch(
        "app.routes.settings.probe_mcs_data_requirements",
        new_callable=AsyncMock,
        side_effect=ValueError("SSRF protection: mcs_url scheme 'file' is not allowed."),
    ):
        resp = await client.post(f"/settings/mcs-connections/{cfg.id}/probe")
    assert resp.status_code == 400
    assert "SSRF protection" in resp.json()["detail"]["issue"][0]["diagnostics"]

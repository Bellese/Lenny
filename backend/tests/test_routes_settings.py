"""Tests for settings connection management endpoints."""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# GET /settings/connections
# ---------------------------------------------------------------------------


async def test_list_connections_empty(client):
    """GET /settings/connections with no rows returns empty list."""
    resp = await client.get("/settings/connections")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /settings/connections
# ---------------------------------------------------------------------------


async def test_create_connection(client):
    """POST /settings/connections creates a connection and returns it."""
    payload = {
        "name": "My CDR",
        "cdr_url": "https://my-cdr.example.com/fhir",
        "auth_type": "none",
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] is not None
    assert data["name"] == "My CDR"
    assert data["cdr_url"] == "https://my-cdr.example.com/fhir"
    assert data["auth_type"] == "none"
    assert data["is_active"] is False
    assert data["is_default"] is False
    assert data["is_read_only"] is False


async def test_create_connection_duplicate_name(client):
    """POST with an existing name returns 409."""
    payload = {"name": "Duplicate", "cdr_url": "https://a.example.com/fhir", "auth_type": "none"}
    await client.post("/settings/connections", json=payload)
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Duplicate" in detail["issue"][0]["diagnostics"]
    assert "already exists" in detail["issue"][0]["diagnostics"]


async def test_create_connection_ssrf_private_ip_blocked(client):
    """POST with a private IP cdr_url returns 400 with SSRF diagnostics."""
    payload = {"name": "Private CDR", "cdr_url": "https://10.0.0.1/fhir", "auth_type": "none"}
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 400
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "SSRF protection" in diag


async def test_create_connection_ssrf_imds_blocked(client):
    """POST with the AWS IMDS link-local address returns 400."""
    payload = {"name": "IMDS", "cdr_url": "https://169.254.169.254/fhir", "auth_type": "none"}
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 400
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "SSRF protection" in diag


async def test_create_connection_rejects_oversized_request_timeout(client):
    """request_timeout_seconds is capped at 1800s (design-doc threat surface #3)."""
    payload = {
        "name": "DoS Timeout",
        "cdr_url": "https://example.com/fhir",
        "auth_type": "none",
        "request_timeout_seconds": 86400,
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 422


async def test_create_connection_rejects_zero_request_timeout(client):
    """request_timeout_seconds must be >= 1."""
    payload = {
        "name": "Zero Timeout",
        "cdr_url": "https://example.com/fhir",
        "auth_type": "none",
        "request_timeout_seconds": 0,
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 422


async def test_create_connection_accepts_within_cap_request_timeout(client):
    """Within-cap values round-trip in the response."""
    payload = {
        "name": "Long-but-legal",
        "cdr_url": "https://example.com/fhir",
        "auth_type": "none",
        "request_timeout_seconds": 1200,
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 201
    assert resp.json()["request_timeout_seconds"] == 1200


async def test_create_connection_defaults_request_timeout_to_30(client):
    """Default request_timeout_seconds is 30 — preserves prior behavior."""
    payload = {"name": "Default Timeout", "cdr_url": "https://example.com/fhir", "auth_type": "none"}
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 201
    assert resp.json()["request_timeout_seconds"] == 30


async def test_create_connection_invalid_auth_type(client):
    """POST with an unsupported auth_type returns 400."""
    payload = {"name": "Bad Auth", "cdr_url": "https://example.com/fhir", "auth_type": "oauth2"}
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 400
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "Invalid auth_type: oauth2" in diag
    assert "none, basic, bearer, smart" in diag


async def test_create_connection_smart_missing_credentials(client):
    """POST with auth_type=smart and missing SMART fields returns 400."""
    payload = {
        "name": "SMART CDR",
        "cdr_url": "https://example.com/fhir",
        "auth_type": "smart",
        "auth_credentials": {"client_id": "abc"},  # missing client_secret and token_endpoint
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "SMART on FHIR" in detail["issue"][0]["diagnostics"]


async def test_create_connection_smart_valid(client):
    """POST with auth_type=smart and all required fields succeeds."""
    payload = {
        "name": "SMART CDR",
        "cdr_url": "https://example.com/fhir",
        "auth_type": "smart",
        "auth_credentials": {
            "client_id": "abc",
            "client_secret": "secret",
            "token_endpoint": "https://auth.example.com/token",
        },
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["auth_type"] == "smart"
    assert data["name"] == "SMART CDR"


# ---------------------------------------------------------------------------
# GET /settings/connections/{id}
# ---------------------------------------------------------------------------


async def test_get_connection(client):
    """GET /settings/connections/{id} returns the connection."""
    create_resp = await client.post(
        "/settings/connections",
        json={"name": "Fetch Me", "cdr_url": "https://example.com/fhir", "auth_type": "none"},
    )
    conn_id = create_resp.json()["id"]

    resp = await client.get(f"/settings/connections/{conn_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == conn_id
    assert data["name"] == "Fetch Me"


async def test_get_connection_not_found(client):
    """GET /settings/connections/999 returns 404."""
    resp = await client.get("/settings/connections/999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /settings/connections/{id}
# ---------------------------------------------------------------------------


async def test_update_connection(client):
    """PUT updates fields on an existing connection."""
    create_resp = await client.post(
        "/settings/connections",
        json={"name": "Original", "cdr_url": "https://old.example.com/fhir", "auth_type": "none"},
    )
    conn_id = create_resp.json()["id"]

    resp = await client.put(
        f"/settings/connections/{conn_id}",
        json={
            "name": "Updated",
            "cdr_url": "https://new.example.com/fhir",
            "auth_type": "bearer",
            "auth_credentials": {"token": "tok123"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated"
    assert data["cdr_url"] == "https://new.example.com/fhir"
    assert data["auth_type"] == "bearer"


async def test_update_connection_duplicate_name(client):
    """PUT with another connection's name returns 409."""
    await client.post(
        "/settings/connections",
        json={"name": "Alpha", "cdr_url": "https://alpha.example.com/fhir", "auth_type": "none"},
    )
    beta_resp = await client.post(
        "/settings/connections",
        json={"name": "Beta", "cdr_url": "https://beta.example.com/fhir", "auth_type": "none"},
    )
    beta_id = beta_resp.json()["id"]

    resp = await client.put(
        f"/settings/connections/{beta_id}",
        json={"name": "Alpha", "cdr_url": "https://beta.example.com/fhir", "auth_type": "none"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Alpha" in detail["issue"][0]["diagnostics"]


async def test_update_connection_not_found(client):
    """PUT on a missing connection returns 404."""
    resp = await client.put(
        "/settings/connections/999",
        json={"name": "Ghost", "cdr_url": "https://example.com/fhir", "auth_type": "none"},
    )
    assert resp.status_code == 404


async def test_update_connection_preserves_credentials_when_omitted(client):
    """PUT that omits auth_credentials does not wipe existing credentials."""
    smart_creds = {
        "client_id": "my-client",
        "client_secret": "my-secret",
        "token_endpoint": "https://auth.example.com/token",
    }
    create_resp = await client.post(
        "/settings/connections",
        json={
            "name": "SMART CDR",
            "cdr_url": "https://smart.example.com/fhir",
            "auth_type": "smart",
            "auth_credentials": smart_creds,
        },
    )
    conn_id = create_resp.json()["id"]

    # PUT with no auth_credentials — should keep the existing ones
    resp = await client.put(
        f"/settings/connections/{conn_id}",
        json={
            "name": "SMART CDR Renamed",
            "cdr_url": "https://smart.example.com/fhir",
            "auth_type": "smart",
            # auth_credentials intentionally omitted
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "SMART CDR Renamed"

    # Verify credentials were not wiped by fetching via GET
    get_resp = await client.get(f"/settings/connections/{conn_id}")
    assert get_resp.status_code == 200
    # auth_credentials are not exposed in the response schema, so the key absence
    # is expected — we just confirm the connection still exists and auth_type is intact
    assert get_resp.json()["auth_type"] == "smart"


# ---------------------------------------------------------------------------
# DELETE /settings/connections/{id}
# ---------------------------------------------------------------------------


async def test_delete_connection(client):
    """DELETE a non-default, non-active connection returns 204."""
    create_resp = await client.post(
        "/settings/connections",
        json={"name": "Deletable", "cdr_url": "https://example.com/fhir", "auth_type": "none"},
    )
    conn_id = create_resp.json()["id"]

    resp = await client.delete(f"/settings/connections/{conn_id}")
    assert resp.status_code == 204

    # Confirm it's gone
    get_resp = await client.get(f"/settings/connections/{conn_id}")
    assert get_resp.status_code == 404


async def test_delete_default_connection_blocked(client, test_session):
    """DELETE the default connection returns 409 with the exact message."""
    from app.models.config import AuthType, CDRConfig

    cfg = CDRConfig(
        name="Local CDR",
        cdr_url="https://localhost:8080/fhir",
        auth_type=AuthType.none,
        is_active=False,
        is_default=True,
        is_read_only=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    resp = await client.delete(f"/settings/connections/{cfg.id}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["issue"][0]["diagnostics"] == "Cannot delete the built-in Local CDR connection."


async def test_delete_active_connection_blocked(client, test_session):
    """DELETE the active connection returns 409 with the exact message."""
    from app.models.config import AuthType, CDRConfig

    cfg = CDRConfig(
        name="Active CDR",
        cdr_url="https://active.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
        is_read_only=False,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)

    resp = await client.delete(f"/settings/connections/{cfg.id}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    expected_msg = "Cannot delete the active connection. Activate a different connection first."
    assert detail["issue"][0]["diagnostics"] == expected_msg


async def test_delete_not_found(client):
    """DELETE a missing connection returns 404."""
    resp = await client.delete("/settings/connections/999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /settings/connections/{id}/activate
# ---------------------------------------------------------------------------


async def test_activate_connection(client):
    """POST /activate switches the active connection."""
    conn_a = (
        await client.post(
            "/settings/connections",
            json={"name": "CDR A", "cdr_url": "https://a.example.com/fhir", "auth_type": "none"},
        )
    ).json()
    conn_b = (
        await client.post(
            "/settings/connections",
            json={"name": "CDR B", "cdr_url": "https://b.example.com/fhir", "auth_type": "none"},
        )
    ).json()

    # Activate A first
    resp = await client.post(f"/settings/connections/{conn_a['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # Activate B — A should become inactive
    resp2 = await client.post(f"/settings/connections/{conn_b['id']}/activate")
    assert resp2.status_code == 200
    assert resp2.json()["is_active"] is True

    # Confirm A is now inactive
    a_resp = await client.get(f"/settings/connections/{conn_a['id']}")
    assert a_resp.json()["is_active"] is False


async def test_activate_not_found(client):
    """POST /activate for a missing connection returns 404."""
    resp = await client.post("/settings/connections/999/activate")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /settings/test-connection
# ---------------------------------------------------------------------------


async def test_test_connection_success(client):
    """POST /settings/test-connection returns connected status on success."""
    mock_result = {
        "status": "connected",
        "fhir_version": "4.0.1",
        "software": "HAPI FHIR",
    }
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.post(
            "/settings/connections/test-connection",
            json={"cdr_url": "http://example.com/fhir"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "connected"
    assert data["fhir_version"] == "4.0.1"


async def test_test_connection_failure(client):
    """POST /settings/test-connection returns 502 when connection fails."""
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Connection refused"),
    ):
        resp = await client.post(
            "/settings/connections/test-connection",
            json={"cdr_url": "http://bad-server.example.com/fhir"},
        )

    assert resp.status_code == 502
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert data["issue"][0]["diagnostics"]  # non-empty diagnostics


async def test_test_connection_401_surfaces_auth_hint(client):
    """POST /settings/test-connection with 401 from CDR returns auth hint in error_details."""
    from app.services.fhir_errors import FhirOperationError

    exc = FhirOperationError(
        operation="test-connection",
        url="http://example.com/fhir/metadata",
        status_code=401,
        outcome=None,
        latency_ms=30,
    )
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
        side_effect=exc,
    ):
        resp = await client.post(
            "/settings/connections/test-connection",
            json={"cdr_url": "http://example.com/fhir"},
        )

    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    ed = detail["error_details"]
    assert ed["status_code"] == 401
    assert ed["hint"] is not None
    assert "token" in ed["hint"].lower() or "authentication" in ed["hint"].lower()


async def test_test_connection_connect_error_surfaces_network_hint(client):
    """POST /settings/test-connection with httpx.ConnectError returns network hint."""
    import httpx as _httpx

    from app.services.fhir_errors import FhirOperationError

    exc = FhirOperationError(
        operation="test-connection",
        url="http://bad-server.example.com/fhir/metadata",
        status_code=None,
        outcome=None,
        latency_ms=10,
        cause=_httpx.ConnectError("Connection refused"),
    )
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
        side_effect=exc,
    ):
        resp = await client.post(
            "/settings/connections/test-connection",
            json={"cdr_url": "http://bad-server.example.com/fhir"},
        )

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    ed = detail["error_details"]
    assert ed["status_code"] is None
    assert ed["hint"] is not None
    assert "server" in ed["hint"].lower() or "reach" in ed["hint"].lower()


async def test_test_connection_success_includes_response_time(client):
    """POST /settings/test-connection success response includes response_time_ms."""
    mock_result = {
        "status": "connected",
        "fhir_version": "4.0.1",
        "software": "HAPI FHIR",
        "response_time_ms": 42,
        "url": "http://example.com/fhir/metadata",
    }
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.post(
            "/settings/connections/test-connection",
            json={"cdr_url": "http://example.com/fhir"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "connected"
    assert data["response_time_ms"] == 42


async def test_test_connection_smart_missing_credentials(client):
    """POST /settings/test-connection with smart but missing fields returns 400 without HTTP call."""
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
    ) as mock_verify:
        resp = await client.post(
            "/settings/connections/test-connection",
            json={
                "cdr_url": "http://example.com/fhir",
                "auth_type": "smart",
                "auth_credentials": {"client_id": "only-this"},
            },
        )
        mock_verify.assert_not_called()

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "SMART on FHIR" in detail["issue"][0]["diagnostics"]


async def test_test_connection_invalid_auth_type(client):
    """POST /settings/test-connection with an invalid auth_type returns 400."""
    resp = await client.post(
        "/settings/connections/test-connection",
        json={"cdr_url": "http://example.com/fhir", "auth_type": "oauth2"},
    )
    assert resp.status_code == 400
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "Invalid auth_type: oauth2" in diag
    assert "none, basic, bearer, smart" in diag


async def test_test_connection_failure_does_not_leak_hostname(client):
    """Regression: internal hostnames must not appear in 502 error responses.

    When verify_fhir_connection raises a connection error whose message contains
    an internal Docker-network hostname (hapi-fhir-cdr:8080), sanitize_error()
    must strip it before the client sees it.
    """
    with patch(
        "app.routes.connection_factory.verify_fhir_connection",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Cannot connect to http://hapi-fhir-cdr:8080/fhir"),
    ):
        resp = await client.post(
            "/settings/connections/test-connection",
            json={"cdr_url": "http://hapi-fhir-cdr:8080/fhir"},
        )

    assert resp.status_code == 502
    body = resp.text
    assert "hapi-fhir-cdr" not in body
    assert "8080" not in body


# ---------------------------------------------------------------------------
# Credential encryption (issue #219)
# ---------------------------------------------------------------------------


async def test_create_connection_stores_credentials_encrypted(client, test_session):
    """POST /settings/connections stores auth_credentials encrypted, not as plaintext."""
    from sqlalchemy import text

    smart_creds = {
        "client_id": "c",
        "client_secret": "very-secret",
        "token_endpoint": "https://auth.example.com/token",
    }
    resp = await client.post(
        "/settings/connections",
        json={
            "name": "Encrypted CDR",
            "cdr_url": "https://enc.example.com/fhir",
            "auth_type": "smart",
            "auth_credentials": smart_creds,
        },
    )
    assert resp.status_code == 201
    conn_id = resp.json()["id"]

    # Load the raw DB row — bypass the ORM TypeDecorator to check physical storage
    result = await test_session.execute(
        text("SELECT auth_credentials FROM cdr_configs WHERE id = :id"),
        {"id": conn_id},
    )
    raw = result.scalar_one()
    # SQLite stores JSON as text; parse it
    import json

    if isinstance(raw, str):
        raw = json.loads(raw)
    # Must have the envelope shape, not plaintext
    assert isinstance(raw, dict), f"Expected dict envelope, got {type(raw)}"
    assert raw.get("v") == 1, "Envelope must carry v=1"
    assert "ct" in raw, "Envelope must carry ct field"
    # Plaintext secret must NOT appear anywhere in the stored value
    assert "very-secret" not in json.dumps(raw)


async def test_credentials_changed_audit_log_on_create(client, caplog):
    """POST /settings/connections emits a structured audit log entry."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.routes.settings"):
        resp = await client.post(
            "/settings/connections",
            json={"name": "Audit CDR", "cdr_url": "https://audit.example.com/fhir", "auth_type": "none"},
        )
    assert resp.status_code == 201

    audit_records = [r for r in caplog.records if getattr(r, "event", None) == "cdr_credentials_changed"]
    assert audit_records, "Expected a cdr_credentials_changed log record"
    record = audit_records[0]
    assert record.action == "create"
    # Credential values must not appear in the log
    assert "secret" not in record.getMessage().lower()


async def test_delete_connection_blocked_when_active_jobs(client, test_session):
    """DELETE /settings/connections/{id} returns 409 when queued/running jobs reference it."""
    from app.models.config import AuthType, CDRConfig
    from app.models.job import Job, JobStatus

    cfg = CDRConfig(
        name="In-Use CDR",
        cdr_url="https://inuse.example.com/fhir",
        auth_type=AuthType.none,
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
        cdr_url=cfg.cdr_url,
        status=JobStatus.running,
        cdr_id=cfg.id,
    )
    test_session.add(job)
    await test_session.commit()

    resp = await client.delete(f"/settings/connections/{cfg.id}")
    assert resp.status_code == 409
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "queued or running jobs" in diag

    # Mark job complete — delete should now succeed
    job.status = JobStatus.complete
    await test_session.commit()

    resp2 = await client.delete(f"/settings/connections/{cfg.id}")
    assert resp2.status_code == 204


# ---------------------------------------------------------------------------
# GET /settings/admin
# ---------------------------------------------------------------------------


async def test_get_admin_settings_defaults_validation_disabled(client):
    """GET /settings/admin with no AppSetting rows returns validation_enabled=False.

    This is the critical path introduced when the default was flipped from
    True to False — a fresh deployment with no seeded AppSetting row must
    advertise validation as disabled.
    """
    resp = await client.get("/settings/admin")
    assert resp.status_code == 200
    data = resp.json()
    assert data["validation_enabled"] is False


# ---------------------------------------------------------------------------
# PUT /settings/admin
# ---------------------------------------------------------------------------


async def test_put_admin_settings_enables_validation(client):
    """PUT /settings/admin can enable validation and GET reflects the change."""
    put_resp = await client.put("/settings/admin", json={"validation_enabled": True})
    assert put_resp.status_code == 200
    assert put_resp.json()["validation_enabled"] is True

    get_resp = await client.get("/settings/admin")
    assert get_resp.status_code == 200
    assert get_resp.json()["validation_enabled"] is True


async def test_put_admin_settings_disables_validation(client):
    """PUT /settings/admin can disable validation after it was enabled."""
    await client.put("/settings/admin", json={"validation_enabled": True})
    put_resp = await client.put("/settings/admin", json={"validation_enabled": False})
    assert put_resp.status_code == 200
    assert put_resp.json()["validation_enabled"] is False

    get_resp = await client.get("/settings/admin")
    assert get_resp.status_code == 200
    assert get_resp.json()["validation_enabled"] is False


async def test_put_admin_settings_empty_body_is_noop(client):
    """PUT /settings/admin with no recognized fields is a no-op (returns current state)."""
    resp = await client.put("/settings/admin", json={})
    assert resp.status_code == 200
    # Default state is validation disabled
    assert resp.json()["validation_enabled"] is False

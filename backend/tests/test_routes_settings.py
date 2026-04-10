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
        "cdr_url": "http://my-cdr.example.com/fhir",
        "auth_type": "none",
    }
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] is not None
    assert data["name"] == "My CDR"
    assert data["cdr_url"] == "http://my-cdr.example.com/fhir"
    assert data["auth_type"] == "none"
    assert data["is_active"] is False
    assert data["is_default"] is False
    assert data["is_read_only"] is False


async def test_create_connection_duplicate_name(client):
    """POST with an existing name returns 409."""
    payload = {"name": "Duplicate", "cdr_url": "http://a.example.com/fhir", "auth_type": "none"}
    await client.post("/settings/connections", json=payload)
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Duplicate" in detail["issue"][0]["diagnostics"]
    assert "already exists" in detail["issue"][0]["diagnostics"]


async def test_create_connection_invalid_auth_type(client):
    """POST with an unsupported auth_type returns 400."""
    payload = {"name": "Bad Auth", "cdr_url": "http://example.com/fhir", "auth_type": "oauth2"}
    resp = await client.post("/settings/connections", json=payload)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Invalid auth_type" in detail["issue"][0]["diagnostics"]


async def test_create_connection_smart_missing_credentials(client):
    """POST with auth_type=smart and missing SMART fields returns 400."""
    payload = {
        "name": "SMART CDR",
        "cdr_url": "http://example.com/fhir",
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
        "cdr_url": "http://example.com/fhir",
        "auth_type": "smart",
        "auth_credentials": {
            "client_id": "abc",
            "client_secret": "secret",
            "token_endpoint": "http://auth.example.com/token",
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
        json={"name": "Fetch Me", "cdr_url": "http://example.com/fhir", "auth_type": "none"},
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
        json={"name": "Original", "cdr_url": "http://old.example.com/fhir", "auth_type": "none"},
    )
    conn_id = create_resp.json()["id"]

    resp = await client.put(
        f"/settings/connections/{conn_id}",
        json={
            "name": "Updated",
            "cdr_url": "http://new.example.com/fhir",
            "auth_type": "bearer",
            "auth_credentials": {"token": "tok123"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated"
    assert data["cdr_url"] == "http://new.example.com/fhir"
    assert data["auth_type"] == "bearer"


async def test_update_connection_duplicate_name(client):
    """PUT with another connection's name returns 409."""
    await client.post(
        "/settings/connections",
        json={"name": "Alpha", "cdr_url": "http://alpha.example.com/fhir", "auth_type": "none"},
    )
    beta_resp = await client.post(
        "/settings/connections",
        json={"name": "Beta", "cdr_url": "http://beta.example.com/fhir", "auth_type": "none"},
    )
    beta_id = beta_resp.json()["id"]

    resp = await client.put(
        f"/settings/connections/{beta_id}",
        json={"name": "Alpha", "cdr_url": "http://beta.example.com/fhir", "auth_type": "none"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Alpha" in detail["issue"][0]["diagnostics"]


async def test_update_connection_not_found(client):
    """PUT on a missing connection returns 404."""
    resp = await client.put(
        "/settings/connections/999",
        json={"name": "Ghost", "cdr_url": "http://example.com/fhir", "auth_type": "none"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /settings/connections/{id}
# ---------------------------------------------------------------------------


async def test_delete_connection(client):
    """DELETE a non-default, non-active connection returns 204."""
    create_resp = await client.post(
        "/settings/connections",
        json={"name": "Deletable", "cdr_url": "http://example.com/fhir", "auth_type": "none"},
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
        cdr_url="http://localhost:8080/fhir",
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
        cdr_url="http://active.example.com/fhir",
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
            json={"name": "CDR A", "cdr_url": "http://a.example.com/fhir", "auth_type": "none"},
        )
    ).json()
    conn_b = (
        await client.post(
            "/settings/connections",
            json={"name": "CDR B", "cdr_url": "http://b.example.com/fhir", "auth_type": "none"},
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
        "app.routes.settings.verify_fhir_connection",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.post(
            "/settings/test-connection",
            json={"cdr_url": "http://example.com/fhir"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "connected"
    assert data["fhir_version"] == "4.0.1"


async def test_test_connection_failure(client):
    """POST /settings/test-connection returns 502 when connection fails."""
    with patch(
        "app.routes.settings.verify_fhir_connection",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Connection refused"),
    ):
        resp = await client.post(
            "/settings/test-connection",
            json={"cdr_url": "http://bad-server.example.com/fhir"},
        )

    assert resp.status_code == 502
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert "Connection failed" in data["issue"][0]["diagnostics"]


async def test_test_connection_smart_missing_credentials(client):
    """POST /settings/test-connection with smart but missing fields returns 400 without HTTP call."""
    with patch(
        "app.routes.settings.verify_fhir_connection",
        new_callable=AsyncMock,
    ) as mock_verify:
        resp = await client.post(
            "/settings/test-connection",
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


async def test_test_connection_failure_does_not_leak_hostname():
    """Regression: internal hostnames must not appear in 502 error responses.

    When verify_fhir_connection raises a connection error whose message contains
    an internal Docker-network hostname (hapi-fhir-cdr:8080), sanitize_error()
    must strip it before the client sees it.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        with patch(
            "app.routes.settings.verify_fhir_connection",
            new_callable=AsyncMock,
            side_effect=ConnectionError("Cannot connect to http://hapi-fhir-cdr:8080/fhir"),
        ):
            resp = await ac.post(
                "/settings/test-connection",
                json={"cdr_url": "http://hapi-fhir-cdr:8080/fhir"},
            )

    assert resp.status_code == 502
    body = resp.text
    assert "hapi-fhir-cdr" not in body
    assert "8080" not in body

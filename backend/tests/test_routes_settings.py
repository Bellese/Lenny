"""Tests for settings endpoints (GET /settings, PUT /settings, POST /settings/test-connection)."""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio


async def test_get_settings_default(client):
    """GET /settings with no config in DB returns defaults."""
    resp = await client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] is None
    assert data["auth_type"] == "none"
    assert data["is_active"] is True
    # Should have a default CDR URL
    assert "fhir" in data["cdr_url"].lower() or "http" in data["cdr_url"].lower()


async def test_put_settings_creates_config(client):
    """PUT /settings creates a new active CDR config."""
    payload = {
        "cdr_url": "http://my-cdr.example.com/fhir",
        "auth_type": "none",
    }
    resp = await client.put("/settings", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["cdr_url"] == "http://my-cdr.example.com/fhir"
    assert data["auth_type"] == "none"
    assert data["is_active"] is True
    assert data["id"] is not None


async def test_put_settings_updates_config(client):
    """PUT /settings deactivates old config and creates new one."""
    # Create initial config
    await client.put(
        "/settings",
        json={"cdr_url": "http://old-cdr.example.com/fhir", "auth_type": "none"},
    )

    # Update to new config
    resp = await client.put(
        "/settings",
        json={
            "cdr_url": "http://new-cdr.example.com/fhir",
            "auth_type": "basic",
            "auth_credentials": {"username": "admin", "password": "secret"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["cdr_url"] == "http://new-cdr.example.com/fhir"
    assert data["auth_type"] == "basic"
    assert data["is_active"] is True

    # GET should return the new config
    get_resp = await client.get("/settings")
    get_data = get_resp.json()
    assert get_data["cdr_url"] == "http://new-cdr.example.com/fhir"


async def test_put_settings_invalid_auth_type(client):
    """PUT /settings with invalid auth_type returns 400."""
    payload = {
        "cdr_url": "http://example.com/fhir",
        "auth_type": "oauth2",  # Not supported
    }
    resp = await client.put("/settings", json=payload)
    assert resp.status_code == 400
    data = resp.json()["detail"]
    assert "Invalid auth_type" in data["issue"][0]["diagnostics"]


async def test_put_settings_bearer_auth(client):
    """PUT /settings with bearer auth stores the token."""
    payload = {
        "cdr_url": "http://example.com/fhir",
        "auth_type": "bearer",
        "auth_credentials": {"token": "my-jwt-token"},
    }
    resp = await client.put("/settings", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_type"] == "bearer"


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


async def test_test_connection_failure_does_not_leak_hostname():
    """Regression: internal hostnames must not appear in 502 error responses.

    When verify_fhir_connection raises a connection error whose message contains
    an internal Docker-network hostname (hapi-fhir-cdr:8080), sanitize_error()
    must strip it before the client sees it.
    """
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        with patch(
            "app.routes.settings.verify_fhir_connection",
            new_callable=AsyncMock,
            side_effect=ConnectionError(
                "Cannot connect to http://hapi-fhir-cdr:8080/fhir"
            ),
        ):
            resp = await ac.post(
                "/settings/test-connection",
                json={"cdr_url": "http://hapi-fhir-cdr:8080/fhir"},
            )

    assert resp.status_code == 502
    body = resp.text
    assert "hapi-fhir-cdr" not in body
    assert "8080" not in body

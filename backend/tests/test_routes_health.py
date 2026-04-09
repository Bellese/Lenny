"""Tests for GET /health endpoint."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_health_all_healthy(client, mock_fhir_metadata):
    """All three services (db, measure engine, CDR) report healthy."""
    mock_response = httpx.Response(200, json=mock_fhir_metadata)

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["database"]["status"] == "connected"
    assert data["measure_engine"]["status"] == "connected"
    assert data["cdr"]["status"] == "connected"


async def test_health_database_unreachable(client, mock_fhir_metadata):
    """When the database query fails, status is degraded."""
    mock_response = httpx.Response(200, json=mock_fhir_metadata)

    with (
        patch("app.routes.health.httpx.AsyncClient") as mock_httpx,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        # Override the session execute to raise an exception
        from sqlalchemy.ext.asyncio import AsyncSession

        original_execute = AsyncSession.execute

        async def failing_execute(self, stmt, *args, **kwargs):
            # Only fail for the health check "SELECT 1" query
            stmt_str = str(stmt)
            if "SELECT 1" in stmt_str or "1" == str(getattr(stmt, "text", "")):
                raise ConnectionError("Database unreachable")
            return await original_execute(self, stmt, *args, **kwargs)

        with patch.object(AsyncSession, "execute", failing_execute):
            resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"]["status"] == "disconnected"
    assert "error" in data["database"]


async def test_health_measure_engine_unreachable(client, mock_fhir_metadata):
    """When the measure engine is down, status is degraded."""
    cdr_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is measure engine /metadata, second is CDR /metadata
        if call_count == 1:
            raise httpx.ConnectError("Connection refused")
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["measure_engine"]["status"] == "disconnected"
    assert "error" in data["measure_engine"]
    assert data["cdr"]["status"] == "connected"


async def test_health_cdr_unreachable(client, mock_fhir_metadata):
    """When the CDR is down, status is degraded."""
    engine_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is measure engine /metadata, second is CDR /metadata
        if call_count == 2:
            raise httpx.ConnectError("Connection refused")
        return engine_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["cdr"]["status"] == "disconnected"
    assert "error" in data["cdr"]
    assert data["measure_engine"]["status"] == "connected"


async def test_health_error_does_not_leak_internal_hostname(client, mock_fhir_metadata):
    """Regression: internal hostnames must not appear in HTTP response bodies.

    When the measure engine raises an exception whose message contains an
    internal Docker-network hostname (hapi-fhir-measure:8080), sanitize_error()
    must strip it before it reaches the client.
    """
    cdr_response = httpx.Response(200, json=mock_fhir_metadata)

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Connection refused connecting to http://hapi-fhir-measure:8080/fhir/metadata")
        return cdr_response

    with patch("app.routes.health.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.text
    assert "hapi-fhir-measure" not in body
    assert "8080" not in body
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["measure_engine"]["status"] == "disconnected"
    assert "error" in data["measure_engine"]

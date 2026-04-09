"""Tests for CORS middleware behavior.

Each test builds a minimal FastAPI app with CORSMiddleware configured using
the same logic as app/main.py, allowing per-test control of ALLOWED_ORIGINS
without monkeypatching the shared settings object.
"""

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient


pytestmark = pytest.mark.asyncio


def _build_cors_app(allowed_origins: str) -> FastAPI:
    """Return a minimal FastAPI app with CORSMiddleware configured.

    Uses the same origin-parsing logic as app/main.py so the tests
    exercise the real production logic path.
    """
    origins = ["*"] if allowed_origins == "*" else [
        o.strip() for o in allowed_origins.split(",") if o.strip()
    ]
    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @test_app.get("/ping")
    async def ping():
        return {"ok": True}

    return test_app


async def test_cors_wildcard_returns_origin_header():
    """When ALLOWED_ORIGINS='*', a cross-origin request receives an
    Access-Control-Allow-Origin header (Starlette echoes the requesting
    origin rather than '*' when allow_credentials=True)."""
    app = _build_cors_app("*")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://anything.com"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


async def test_cors_allowed_origin_echoed():
    """When ALLOWED_ORIGINS lists a specific origin and the request matches,
    the response echoes back that origin in Access-Control-Allow-Origin."""
    app = _build_cors_app("https://example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://example.com"})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://example.com"


async def test_cors_rejected_origin_no_header():
    """When ALLOWED_ORIGINS lists a specific origin and the request comes from
    a different origin, no Access-Control-Allow-Origin header is returned."""
    app = _build_cors_app("https://example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://evil.com"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


async def test_cors_preflight_allowed_origin():
    """An OPTIONS preflight from an allowed origin receives the correct
    Access-Control-Allow-Origin and Access-Control-Allow-Methods headers."""
    app = _build_cors_app("https://example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.options(
            "/ping",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://example.com"
    assert "access-control-allow-methods" in resp.headers

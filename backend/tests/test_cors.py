"""Tests for CORS middleware behavior.

Each test builds a minimal FastAPI app with CORSMiddleware configured using
the same logic as app/main.py, allowing per-test control of ALLOWED_ORIGINS
without monkeypatching the shared settings object.
"""

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from app.config import parse_allowed_origins


pytestmark = pytest.mark.asyncio


def _build_cors_app(allowed_origins: str) -> FastAPI:
    """Return a minimal FastAPI app with CORSMiddleware configured.

    Delegates to parse_allowed_origins (from app.config) so tests exercise
    the real production parsing path. allow_credentials mirrors main.py logic:
    disabled for wildcard (invalid per CORS spec), enabled for explicit origins.
    """
    origins = parse_allowed_origins(allowed_origins)
    allow_credentials = origins != ["*"]
    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @test_app.get("/ping")
    async def ping():
        return {"ok": True}

    return test_app


async def test_cors_wildcard_returns_origin_header():
    """When ALLOWED_ORIGINS='*', a cross-origin request receives
    Access-Control-Allow-Origin: * (Starlette returns the wildcard literal)."""
    app = _build_cors_app("*")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://anything.com"})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


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


async def test_cors_multi_origin_list_allows_any_listed():
    """ALLOWED_ORIGINS with a comma-separated list allows any listed origin.

    Also exercises the whitespace-stripping logic in the origin parser
    (' https://b.com' → 'https://b.com').
    """
    app = _build_cors_app("https://a.com, https://b.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://b.com"})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://b.com"


async def test_cors_multi_origin_list_blocks_unlisted():
    """An origin not in the comma-separated ALLOWED_ORIGINS list is blocked."""
    app = _build_cors_app("https://a.com, https://b.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://c.com"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


async def test_cors_preflight_rejected_origin():
    """An OPTIONS preflight from a disallowed origin gets no CORS headers."""
    app = _build_cors_app("https://example.com")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.options(
            "/ping",
            headers={
                "Origin": "https://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert "access-control-allow-origin" not in resp.headers


async def test_cors_empty_allowed_origins_blocks_all():
    """When ALLOWED_ORIGINS is empty, the empty-string filter produces no allowed
    origins and all cross-origin requests are blocked."""
    app = _build_cors_app("")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ping", headers={"Origin": "https://anything.com"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers

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

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.connection_base import AuthType
from app.models.mcs_config import MCSConfig

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

"""Integration tests for the factory-reset and reseed-bundles admin endpoints.

Requires prebaked HAPI images (HAPI_PREBAKED=1) so both HAPI servers are
pre-populated with connectathon data. The test:
  1. POSTs /settings/admin/factory-reset and polls until succeeded
  2. Asserts Patient counts dropped to 0 on both HAPI servers
  3. Asserts key Postgres tables are empty
  4. POSTs /settings/admin/reseed-bundles and polls until succeeded
  5. Asserts Patient counts recovered on the CDR

Run via:
  USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_factory_reset.py
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Skip unless prebaked images are running
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_prebaked_stack():
    """Skip this module unless prebaked HAPI images are in use."""
    if os.environ.get("HAPI_PREBAKED") != "1":
        pytest.skip(
            "test_factory_reset requires HAPI_PREBAKED=1. "
            "Run via: USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_factory_reset.py"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POLL_INTERVAL = 2  # seconds
_POLL_TIMEOUT = 120  # seconds


async def _poll_operation(client, operation_id: int) -> dict:
    """Poll GET /settings/admin/operations/{id} until terminal state."""
    deadline = asyncio.get_event_loop().time() + _POLL_TIMEOUT
    while True:
        resp = await client.get(f"/settings/admin/operations/{operation_id}")
        assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.text}"
        body = resp.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        assert asyncio.get_event_loop().time() < deadline, (
            f"Operation {operation_id} did not complete within {_POLL_TIMEOUT}s. Last status: {body['status']}"
        )
        await asyncio.sleep(_POLL_INTERVAL)


async def _count_resources(base_url: str, resource_type: str) -> int:
    """Return the total count of a resource type on a HAPI server."""
    async with httpx.AsyncClient(timeout=30) as hclient:
        resp = await hclient.get(f"{base_url}/{resource_type}?_summary=count")
        resp.raise_for_status()
        return resp.json().get("total", 0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_factory_reset_and_reseed(integration_client, db_session):
    """Full cycle: factory-reset wipes both HAPIs + DB, reseed restores CDR data."""
    # --- sanity: confirm data exists before reset ---
    pre_cdr_patients = await _count_resources(TEST_CDR_URL, "Patient")
    assert pre_cdr_patients > 0, "Prebaked CDR should have patients before reset"

    # --- POST factory-reset ---
    resp = await integration_client.post(
        "/settings/admin/factory-reset",
        json={"include_cdr": True, "include_measure_engine": True, "include_app_db": True},
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["status"] == "accepted"
    operation_id = body["operation_id"]

    # --- poll until done ---
    result = await _poll_operation(integration_client, operation_id)
    assert result["status"] == "succeeded", f"Factory reset failed: {result.get('error')}"

    # --- verify CDR wiped ---
    post_reset_cdr = await _count_resources(TEST_CDR_URL, "Patient")
    assert post_reset_cdr == 0, f"CDR still has {post_reset_cdr} patients after factory reset"

    # --- verify measure engine wiped (Measure resources gone) ---
    post_reset_me = await _count_resources(TEST_MEASURE_URL, "Measure")
    assert post_reset_me == 0, f"Measure engine still has {post_reset_me} Measure resources after reset"

    # --- verify Postgres app tables empty ---
    from sqlalchemy import text

    for table in ("jobs", "measure_results", "validation_runs"):
        row = await db_session.execute(text(f"SELECT COUNT(*) FROM {table}"))
        count = row.scalar()
        assert count == 0, f"Table {table} has {count} rows after factory reset"

    # --- POST reseed-bundles ---
    resp = await integration_client.post("/settings/admin/reseed-bundles")
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    reseed_body = resp.json()
    reseed_id = reseed_body["operation_id"]

    # --- poll reseed ---
    reseed_result = await _poll_operation(integration_client, reseed_id)
    assert reseed_result["status"] == "succeeded", f"Reseed failed: {reseed_result.get('error')}"

    # --- verify CDR repopulated ---
    post_reseed_cdr = await _count_resources(TEST_CDR_URL, "Patient")
    assert post_reseed_cdr > 0, f"CDR has {post_reseed_cdr} patients after reseed — expected > 0"

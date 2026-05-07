"""Integration tests for the /settings/test-connection endpoint error paths.

These tests verify that connection errors (auth failures, network failures)
are surfaced with structured error details including HTTP status codes and
actionable hints.

Prerequisites:
    docker compose -f docker-compose.test.yml up -d
"""

import pytest

pytestmark = pytest.mark.integration

TEST_CDR_URL = "http://localhost:8180/fhir"


# ---------------------------------------------------------------------------
# /settings/test-connection — error paths
# ---------------------------------------------------------------------------


async def test_test_connection_401_bearer_returns_hint(integration_client):
    """401 from CDR with wrong bearer token → hint present in error_details."""
    resp = await integration_client.post(
        "/settings/connections/test-connection",
        json={
            "cdr_url": TEST_CDR_URL,
            "auth_type": "bearer",
            "auth_credentials": {"token": "invalid-token-that-does-not-exist"},
        },
    )
    # HAPI without auth configured may return 200 (open server) — skip rather than fail.
    if resp.status_code == 200:
        pytest.skip("CDR is open (no auth configured) — cannot test 401 path")

    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("resourceType") == "OperationOutcome"
    issues = detail.get("issue", [])
    assert issues, "Expected at least one issue in OperationOutcome"
    error_details = detail.get("error_details", {})
    assert error_details.get("status_code") == 401
    assert error_details.get("hint"), "Expected a non-empty hint for 401"
    assert "token" in error_details["hint"].lower() or "auth" in error_details["hint"].lower()


async def test_test_connection_unreachable_url_returns_network_hint(integration_client):
    """Unreachable URL → 502 with a network-layer hint in error_details."""
    resp = await integration_client.post(
        "/settings/connections/test-connection",
        json={
            "cdr_url": "https://does-not-exist.invalid:9999/fhir",
            "auth_type": "none",
            "auth_credentials": None,
        },
    )
    assert resp.status_code == 502, f"Expected 502, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("resourceType") == "OperationOutcome"
    issues = detail.get("issue", [])
    assert issues, "Expected at least one issue"
    error_details = detail.get("error_details", {})
    assert error_details.get("hint"), "Expected a non-empty hint for unreachable URL"
    # No status_code for network errors
    assert error_details.get("status_code") is None


async def test_test_connection_success_returns_response_time(integration_client):
    """Valid CDR URL → 200 with response_time_ms present."""
    resp = await integration_client.post(
        "/settings/connections/test-connection",
        json={
            "cdr_url": TEST_CDR_URL,
            "auth_type": "none",
            "auth_credentials": None,
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "response_time_ms" in body, f"Expected response_time_ms in response: {body}"
    assert isinstance(body["response_time_ms"], int)
    assert body["response_time_ms"] >= 0


async def test_test_connection_wrong_basic_auth_returns_hint(integration_client):
    """Wrong basic auth credentials → error with auth hint."""
    resp = await integration_client.post(
        "/settings/connections/test-connection",
        json={
            "cdr_url": TEST_CDR_URL,
            "auth_type": "basic",
            "auth_credentials": {"username": "wrong_user", "password": "wrong_pass"},
        },
    )
    if resp.status_code == 200:
        pytest.skip("CDR is open (no auth configured) — cannot test basic auth failure path")

    assert resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}"
    body = resp.json()
    detail = body.get("detail", body)
    error_details = detail.get("error_details", {})
    assert error_details.get("hint"), "Expected a non-empty hint for auth failure"

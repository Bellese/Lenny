"""Integration tests for MCS-scoped measure management (issue #396).

These exercise the defect the unit tests can only approximate: that `GET /measures`
follows the ACTIVE MCS connection rather than `settings.MEASURE_ENGINE_URL`.

The existing stack gives us two real FHIR servers for free:
  - hapi-fhir-measure (MEASURE_ENGINE_URL) holds the connectathon Measure resources
  - hapi-fhir-cdr     (CDR_URL)            holds patient data and NO Measures

Pointing a second MCS connection at the CDR is therefore a genuine two-server test —
activating it must produce a different (empty) measure set, with no new infrastructure.
A regression to the env var makes the second assertion fail loudly, because the
measure list would not change when the connection did.
"""

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _create_mcs(client, name: str, url: str, *, read_only: bool = False) -> int:
    """Create an MCS connection and return its id."""
    resp = await client.post(
        "/settings/mcs-connections",
        json={
            "name": name,
            "mcs_url": url,
            "auth_type": "none",
            "is_read_only": read_only,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _activate(client, connection_id: int) -> None:
    resp = await client.post(f"/settings/mcs-connections/{connection_id}/activate")
    assert resp.status_code == 200, resp.text


async def test_measures_follow_the_active_mcs(integration_client, measure_url, cdr_url, truncate_tables):
    """Activating a different MCS changes the measure list.

    This is the regression guard for #396. Before the fix, `GET /measures` read
    `settings.MEASURE_ENGINE_URL` unconditionally, so both assertions below
    returned the same measure set regardless of which connection was active.
    """
    engine_id = await _create_mcs(integration_client, "Test Measure Engine", measure_url)
    await _activate(integration_client, engine_id)

    resp = await integration_client.get("/measures")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    engine_measures = body["measures"]
    assert engine_measures, "measure engine should expose seeded Measure resources"
    assert body["mcs"]["id"] == engine_id
    assert body["mcs"]["name"] == "Test Measure Engine"

    # Point a second MCS at the CDR, which holds patient data but no Measures.
    cdr_as_mcs_id = await _create_mcs(integration_client, "CDR As MCS", cdr_url)
    await _activate(integration_client, cdr_as_mcs_id)

    resp = await integration_client.get("/measures")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["mcs"]["id"] == cdr_as_mcs_id
    assert body["mcs"]["name"] == "CDR As MCS"
    assert body["measures"] == [], (
        "the CDR holds no Measure resources — GET /measures should return an empty "
        "list; anything else means it is still reading settings.MEASURE_ENGINE_URL"
    )
    assert body["total"] == len(body["measures"])


async def test_measures_response_does_not_leak_the_mcs_url(integration_client, measure_url, truncate_tables):
    """The success payload names the MCS but never publishes its URL.

    The 502 path runs upstream errors through `sanitize_error`, which strips
    host:port. The 200 path must not undo that by publishing the raw URL.
    """
    engine_id = await _create_mcs(integration_client, "No Leak Engine", measure_url)
    await _activate(integration_client, engine_id)

    resp = await integration_client.get("/measures")
    assert resp.status_code == 200, resp.text

    assert set(resp.json()["mcs"]) == {"id", "name"}
    assert measure_url not in resp.text


async def test_read_only_mcs_rejects_upload_and_delete(integration_client, measure_url, truncate_tables):
    """A read-only MCS refuses writes with 403 and never reaches the server."""
    read_only_id = await _create_mcs(integration_client, "Read Only Engine", measure_url, read_only=True)
    await _activate(integration_client, read_only_id)

    # Reading still works — read-only restricts writes, not reads.
    resp = await integration_client.get("/measures")
    assert resp.status_code == 200, resp.text

    bundle = b'{"resourceType": "Bundle", "type": "transaction", "entry": []}'
    resp = await integration_client.post(
        "/measures/upload",
        files={"file": ("measure.json", bundle, "application/json")},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["resourceType"] == "OperationOutcome"

    resp = await integration_client.delete("/measures/any-measure-id")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["resourceType"] == "OperationOutcome"


async def test_job_creation_rejects_a_measure_absent_from_the_active_mcs(
    integration_client, measure_url, cdr_url, truncate_tables
):
    """POST /jobs 400s for a measure the active MCS does not have.

    Without the pre-flight check this returned 201 and the mismatch only surfaced
    later as an all-patients-failed job, which is far harder to diagnose.
    """
    engine_id = await _create_mcs(integration_client, "Job Guard Engine", measure_url)
    await _activate(integration_client, engine_id)

    measures = (await integration_client.get("/measures")).json()["measures"]
    assert measures, "need at least one seeded measure to exercise the happy path"
    real_measure_id = measures[0]["id"]

    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": real_measure_id,
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
        },
    )
    assert resp.status_code == 201, resp.text

    # Same measure id, but now the active MCS is the CDR, which has no Measures.
    cdr_as_mcs_id = await _create_mcs(integration_client, "Job Guard CDR", cdr_url)
    await _activate(integration_client, cdr_as_mcs_id)

    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": real_measure_id,
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
        },
    )
    assert resp.status_code == 400, resp.text
    diagnostics = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert real_measure_id in diagnostics
    assert "Job Guard CDR" in diagnostics


async def test_health_reports_the_active_mcs_identity(integration_client, measure_url, truncate_tables):
    """/health carries the active MCS id, which the frontend keys change detection on."""
    engine_id = await _create_mcs(integration_client, "Health Engine", measure_url)
    await _activate(integration_client, engine_id)

    resp = await integration_client.get("/health")
    assert resp.status_code == 200, resp.text

    engine_block = resp.json()["measure_engine"]
    assert engine_block["id"] == engine_id
    assert engine_block["name"] == "Health Engine"
    assert engine_block["is_read_only"] is False

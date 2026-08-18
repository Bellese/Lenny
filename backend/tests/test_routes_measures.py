"""Tests for measure endpoints (GET /measures, POST /measures/upload, DELETE /measures/{id})."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import app.routes.measures as measures_module

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Reset the 10/minute upload rate limiter so tests don't 429 each other."""
    from app.limiter import limiter

    limiter.reset()


@pytest_asyncio.fixture
async def active_mcs(test_session):
    """Make a named, writable MCS the active connection and return the row.

    Without a row in `mcs_configs`, `get_active_mcs` falls back to the env-var
    engine — which is precisely the state issue #396 was about, so tests that
    care about MCS scoping need a real row.
    """
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name="Attendee MCS",
        mcs_url="https://attendee-mcs.example.com/fhir",
        auth_type=AuthType.bearer,
        auth_credentials={"token": "tok"},
        is_active=True,
        is_default=False,
        is_read_only=False,
        request_timeout_seconds=45,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


@pytest_asyncio.fixture
async def read_only_mcs(test_session):
    """Make a read-only MCS the active connection."""
    from sqlalchemy import update as sa_update

    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    await test_session.execute(sa_update(MCSConfig).values(is_active=False))
    cfg = MCSConfig(
        name="Shared Read-Only MCS",
        mcs_url="https://shared-mcs.example.com/fhir",
        auth_type=AuthType.none,
        is_active=True,
        is_default=False,
        is_read_only=True,
    )
    test_session.add(cfg)
    await test_session.commit()
    await test_session.refresh(cfg)
    return cfg


async def test_get_measures_success(client, mock_measure_bundle):
    """GET /measures returns a simplified list of measures from the engine."""
    with patch(
        "app.routes.measures.list_measures",
        new_callable=AsyncMock,
        return_value=mock_measure_bundle,
    ):
        resp = await client.get("/measures")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["measures"]) == 1
    m = data["measures"][0]
    assert m["id"] == "measure-1"
    assert m["name"] == "TestMeasure"
    assert m["title"] == "Test Measure"
    assert m["version"] == "1.0"
    assert m["status"] == "active"


async def test_get_measures_reads_from_active_mcs(client, mock_measure_bundle, active_mcs):
    """Regression (issue #396): GET /measures hits the ACTIVE MCS, not MEASURE_ENGINE_URL."""
    from app.config import settings

    with patch(
        "app.routes.measures.list_measures",
        new_callable=AsyncMock,
        return_value=mock_measure_bundle,
    ) as mock_list:
        resp = await client.get("/measures")

    assert resp.status_code == 200
    called_base = mock_list.await_args.args[0]
    assert called_base == "https://attendee-mcs.example.com/fhir"
    assert called_base != settings.MEASURE_ENGINE_URL
    # Credentials + per-connection timeout are threaded through.
    assert mock_list.await_args.kwargs["auth_headers"] == {"Authorization": "Bearer tok"}
    assert mock_list.await_args.kwargs["timeout"] == 45.0


async def test_get_measures_includes_mcs_block(client, mock_measure_bundle, active_mcs):
    """GET /measures reports which MCS the list came from."""
    with patch(
        "app.routes.measures.list_measures",
        new_callable=AsyncMock,
        return_value=mock_measure_bundle,
    ):
        resp = await client.get("/measures")

    data = resp.json()
    assert data["mcs"] == {
        "id": active_mcs.id,
        "name": "Attendee MCS",
        "url": "https://attendee-mcs.example.com/fhir",
    }


async def test_get_measures_502_names_the_mcs(client, active_mcs):
    """A failed upstream call names the MCS rather than falling back to the local engine."""
    with patch(
        "app.routes.measures.list_measures",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Connection refused"),
    ):
        resp = await client.get("/measures")

    assert resp.status_code == 502
    diag = resp.json()["detail"]["issue"][0]["diagnostics"]
    assert "Attendee MCS" in diag


async def test_get_measures_engine_unreachable(client):
    """GET /measures returns 502 when the measure engine is unreachable."""
    with patch(
        "app.routes.measures.list_measures",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Connection refused"),
    ):
        resp = await client.get("/measures")

    assert resp.status_code == 502
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert "Cannot reach measure engine" in data["issue"][0]["diagnostics"]


async def test_upload_measure_success(client):
    """POST /measures/upload with a valid FHIR Bundle succeeds."""
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": {
                    "resourceType": "Measure",
                    "id": "measure-1",
                }
            }
        ],
    }
    engine_response = {"resourceType": "Bundle", "type": "transaction-response"}

    with patch(
        "app.routes.measures.upload_measure_bundle",
        new_callable=AsyncMock,
        return_value=engine_response,
    ):
        resp = await client.post(
            "/measures/upload",
            files={"file": ("measure.json", json.dumps(bundle).encode(), "application/json")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["message"] == "Measure bundle uploaded successfully"
    assert data["result"] == engine_response


async def test_upload_measure_not_json_file(client):
    """POST /measures/upload with a non-JSON file returns 400."""
    resp = await client.post(
        "/measures/upload",
        files={"file": ("measure.xml", b"<Bundle/>", "application/xml")},
    )
    assert resp.status_code == 400
    data = resp.json()["detail"]
    assert "must be a .json" in data["issue"][0]["diagnostics"]


async def test_upload_measure_invalid_json(client):
    """POST /measures/upload with invalid JSON returns 400."""
    resp = await client.post(
        "/measures/upload",
        files={"file": ("measure.json", b"not valid json{{{", "application/json")},
    )
    assert resp.status_code == 400
    data = resp.json()["detail"]
    assert "Invalid JSON" in data["issue"][0]["diagnostics"]


async def test_upload_measure_not_bundle(client):
    """POST /measures/upload with a non-Bundle resource returns 400."""
    non_bundle = {"resourceType": "Patient", "id": "p1"}
    resp = await client.post(
        "/measures/upload",
        files={"file": ("measure.json", json.dumps(non_bundle).encode(), "application/json")},
    )
    assert resp.status_code == 400
    data = resp.json()["detail"]
    assert "must be a FHIR Bundle" in data["issue"][0]["diagnostics"]


async def test_upload_measure_no_file(client):
    """POST /measures/upload with no file returns 422."""
    resp = await client.post("/measures/upload")
    assert resp.status_code == 422


async def test_upload_measure_engine_rejects(client):
    """POST /measures/upload returns 502 when the measure engine rejects the bundle."""
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [],
    }
    with patch(
        "app.routes.measures.upload_measure_bundle",
        new_callable=AsyncMock,
        side_effect=Exception("500 Internal Server Error"),
    ):
        resp = await client.post(
            "/measures/upload",
            files={"file": ("measure.json", json.dumps(bundle).encode(), "application/json")},
        )

    assert resp.status_code == 502
    data = resp.json()["detail"]
    diag = data["issue"][0]["diagnostics"]
    assert diag.startswith("Measure engine ")
    assert "rejected bundle" in diag


async def test_get_measures_engine_unreachable_does_not_leak_hostname(client):
    """Regression: internal hostnames must not appear in 502 error responses.

    When list_measures raises a connection error whose message contains
    hapi-fhir-measure:8080, sanitize_error() must strip it before the client sees it.
    """
    with patch(
        "app.routes.measures.list_measures",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Cannot connect to http://hapi-fhir-measure:8080/fhir"),
    ):
        resp = await client.get("/measures")

    assert resp.status_code == 502
    body = resp.text
    assert "hapi-fhir-measure" not in body
    assert "8080" not in body


async def test_upload_measure_engine_rejects_does_not_leak_hostname(client):
    """Regression: internal hostnames must not appear in 502 error responses.

    When upload_measure_bundle raises an exception whose message contains an
    internal hostname, sanitize_error() must strip it before the client sees it.
    """
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    with patch(
        "app.routes.measures.upload_measure_bundle",
        new_callable=AsyncMock,
        side_effect=Exception("502 Bad Gateway from http://hapi-fhir-measure:8080/fhir"),
    ):
        resp = await client.post(
            "/measures/upload",
            files={"file": ("bundle.json", json.dumps(bundle).encode(), "application/json")},
        )

    assert resp.status_code == 502
    body = resp.text
    assert "hapi-fhir-measure" not in body
    assert "8080" not in body


# ---------------------------------------------------------------------------
# Size guard tests (upload hardening)
# ---------------------------------------------------------------------------


async def test_upload_measure_oversized_returns_413(client, monkeypatch):
    """POST /measures/upload with a file exceeding MAX_UPLOAD_SIZE returns 413."""
    monkeypatch.setattr(measures_module, "MAX_UPLOAD_SIZE", 10)
    # 11 bytes > 10-byte limit
    oversized = b"x" * 11
    resp = await client.post(
        "/measures/upload",
        files={"file": ("big.json", oversized, "application/json")},
    )
    assert resp.status_code == 413
    data = resp.json()["detail"]
    assert data["resourceType"] == "OperationOutcome"
    assert "size limit" in data["issue"][0]["diagnostics"]


async def test_upload_measure_small_file_not_rejected_by_size_check(client, monkeypatch):
    """POST /measures/upload with a small valid JSON Bundle is not rejected by the size guard.

    The endpoint may return 200 (success) or 502 (engine unreachable), but must
    not return 413 — verifying the size check does not block legitimate uploads.
    """
    monkeypatch.setattr(measures_module, "MAX_UPLOAD_SIZE", 10 * 1024 * 1024)
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    content = json.dumps(bundle).encode()
    assert len(content) < 10 * 1024 * 1024, "Sanity: test payload must be smaller than the limit"

    with patch(
        "app.routes.measures.upload_measure_bundle",
        new_callable=AsyncMock,
        return_value={"resourceType": "Bundle", "type": "transaction-response"},
    ):
        resp = await client.post(
            "/measures/upload",
            files={"file": ("bundle.json", content, "application/json")},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"


async def test_delete_measure_success(client):
    """DELETE /measures/{id} proxies measure deletion to the engine."""
    with patch(
        "app.routes.measures.delete_measure",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_delete:
        resp = await client.delete("/measures/measure-1")

    assert resp.status_code == 204
    assert mock_delete.await_args.args[0] == "measure-1"


async def test_delete_measure_not_found(client):
    """DELETE /measures/{id} returns 404 when the engine reports the measure is missing."""
    request = httpx.Request("DELETE", "http://test/measures/measure-1")
    response = httpx.Response(404, request=request)
    with patch(
        "app.routes.measures.delete_measure",
        new_callable=AsyncMock,
        side_effect=httpx.HTTPStatusError("not found", request=request, response=response),
    ):
        resp = await client.delete("/measures/measure-1")

    assert resp.status_code == 404
    data = resp.json()["detail"]
    assert data["issue"][0]["code"] == "not-found"


async def test_delete_measure_engine_error(client):
    """DELETE /measures/{id} returns 502 for upstream delete failures."""
    with patch(
        "app.routes.measures.delete_measure",
        new_callable=AsyncMock,
        side_effect=ConnectionError("Connection refused"),
    ):
        resp = await client.delete("/measures/measure-1")

    assert resp.status_code == 502
    data = resp.json()["detail"]
    assert "Cannot reach measure engine" in data["issue"][0]["diagnostics"]


# ---------------------------------------------------------------------------
# Active-MCS targeting + read-only guard (issue #396)
# ---------------------------------------------------------------------------


async def test_upload_measure_targets_active_mcs(client, active_mcs):
    """POST /measures/upload sends the bundle to the active MCS, not the env-var engine."""
    from app.config import settings

    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    with patch(
        "app.routes.measures.upload_measure_bundle",
        new_callable=AsyncMock,
        return_value={"resourceType": "Bundle", "type": "transaction-response"},
    ) as mock_upload:
        resp = await client.post(
            "/measures/upload",
            files={"file": ("bundle.json", json.dumps(bundle).encode(), "application/json")},
        )

    assert resp.status_code == 200
    called_base = mock_upload.await_args.args[1]
    assert called_base == "https://attendee-mcs.example.com/fhir"
    assert called_base != settings.MEASURE_ENGINE_URL
    assert mock_upload.await_args.kwargs["auth_headers"] == {"Authorization": "Bearer tok"}
    assert mock_upload.await_args.kwargs["timeout"] == 45.0


async def test_delete_measure_targets_active_mcs(client, active_mcs):
    """DELETE /measures/{id} deletes from the active MCS, not the env-var engine."""
    from app.config import settings

    with patch(
        "app.routes.measures.delete_measure",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_delete:
        resp = await client.delete("/measures/measure-1")

    assert resp.status_code == 204
    called_base = mock_delete.await_args.args[1]
    assert called_base == "https://attendee-mcs.example.com/fhir"
    assert called_base != settings.MEASURE_ENGINE_URL
    assert mock_delete.await_args.kwargs["auth_headers"] == {"Authorization": "Bearer tok"}


async def test_upload_measure_read_only_mcs_returns_403(client, read_only_mcs):
    """A read-only MCS rejects uploads with a 403 OperationOutcome."""
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    with patch(
        "app.routes.measures.upload_measure_bundle",
        new_callable=AsyncMock,
    ) as mock_upload:
        resp = await client.post(
            "/measures/upload",
            files={"file": ("bundle.json", json.dumps(bundle).encode(), "application/json")},
        )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["resourceType"] == "OperationOutcome"
    assert detail["issue"][0]["code"] == "forbidden"
    assert "Shared Read-Only MCS" in detail["issue"][0]["diagnostics"]
    mock_upload.assert_not_awaited()


async def test_upload_measure_read_only_rejected_before_reading_body(client, read_only_mcs):
    """The 403 fires before the upload body is read.

    A read-only MCS must not require transferring a 100MB bundle to learn it
    can't be written. The file here is *not* valid JSON and doesn't end in
    .json — both of which produce a 400 in the normal path — so a 403 proves
    neither the filename check nor the body read happened first.
    """
    resp = await client.post(
        "/measures/upload",
        files={"file": ("bundle.xml", b"not json at all{{{", "application/xml")},
    )
    assert resp.status_code == 403


async def test_delete_measure_read_only_mcs_returns_403(client, read_only_mcs):
    """A read-only MCS rejects deletes with a 403 OperationOutcome."""
    with patch(
        "app.routes.measures.delete_measure",
        new_callable=AsyncMock,
    ) as mock_delete:
        resp = await client.delete("/measures/measure-1")

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["issue"][0]["code"] == "forbidden"
    assert "Shared Read-Only MCS" in detail["issue"][0]["diagnostics"]
    mock_delete.assert_not_awaited()

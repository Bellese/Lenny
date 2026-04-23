"""Tests for measure endpoints (GET /measures, POST /measures/upload)."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.routes.measures as measures_module

pytestmark = pytest.mark.asyncio


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
    assert "Measure engine rejected bundle" in data["issue"][0]["diagnostics"]


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
    mock_delete.assert_awaited_once_with("measure-1")


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

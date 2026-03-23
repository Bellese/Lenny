"""Tests for measure endpoints (GET /measures, POST /measures/upload)."""

import io
import json
from unittest.mock import AsyncMock, patch

import pytest

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

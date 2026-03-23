"""Tests for the FHIR client service (fhir_client.py)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.fhir_client import (
    BatchQueryStrategy,
    _build_auth_headers,
    evaluate_measure,
    list_measures,
    push_resources,
    resolve_evaluated_resource,
    test_connection as fhir_test_connection,
    upload_measure_bundle,
    wipe_patient_data,
)

pytestmark = pytest.mark.asyncio

# Dummy request used to construct httpx.Response objects that support raise_for_status()
_DUMMY_REQUEST = httpx.Request("GET", "http://test")


def _make_response(status_code: int, json_data: dict) -> httpx.Response:
    """Build an httpx.Response with a request set so raise_for_status() works."""
    return httpx.Response(status_code, json=json_data, request=_DUMMY_REQUEST)


# ---------------------------------------------------------------------------
# _build_auth_headers
# ---------------------------------------------------------------------------


class TestBuildAuthHeaders:
    def test_no_auth(self):
        result = _build_auth_headers("none", None)
        assert result == {}

    def test_no_auth_with_credentials(self):
        """Even with credentials, 'none' auth type returns empty."""
        result = _build_auth_headers("none", {"username": "u", "password": "p"})
        assert result == {}

    def test_basic_auth(self):
        result = _build_auth_headers("basic", {"username": "admin", "password": "secret"})
        assert "Authorization" in result
        assert result["Authorization"].startswith("Basic ")
        import base64

        decoded = base64.b64decode(result["Authorization"].split(" ")[1]).decode()
        assert decoded == "admin:secret"

    def test_bearer_auth(self):
        result = _build_auth_headers("bearer", {"token": "my-jwt"})
        assert result == {"Authorization": "Bearer my-jwt"}

    def test_unknown_auth_type(self):
        result = _build_auth_headers("oauth2", {"token": "abc"})
        assert result == {}

    def test_basic_auth_no_credentials(self):
        result = _build_auth_headers("basic", None)
        assert result == {}


# ---------------------------------------------------------------------------
# BatchQueryStrategy.gather_patients
# ---------------------------------------------------------------------------


async def test_gather_patients_single_page(mock_patient_bundle):
    """gather_patients returns patient resources from a single page."""
    mock_response = _make_response(200, mock_patient_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert len(patients) == 2
    assert patients[0]["id"] == "patient-1"
    assert patients[1]["id"] == "patient-2"


async def test_gather_patients_paginated():
    """gather_patients follows pagination links."""
    page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1"}},
        ],
        "link": [
            {"relation": "next", "url": "http://cdr/fhir/Patient?_count=100&page=2"},
        ],
    }
    page2 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p2"}},
        ],
        "link": [],
    }

    call_count = 0

    async def mock_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(200, page1)
        return _make_response(200, page2)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert len(patients) == 2
    assert patients[0]["id"] == "p1"
    assert patients[1]["id"] == "p2"
    assert call_count == 2


async def test_gather_patients_empty():
    """gather_patients returns empty list when no patients found."""
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}
    mock_response = _make_response(200, empty_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert patients == []


# ---------------------------------------------------------------------------
# BatchQueryStrategy.gather_patient_data
# ---------------------------------------------------------------------------


async def test_gather_patient_data():
    """gather_patient_data returns resources from $everything."""
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1"}},
            {"resource": {"resourceType": "Condition", "id": "c1"}},
            {"resource": {"resourceType": "Observation", "id": "o1"}},
        ],
        "link": [],
    }
    mock_response = _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = BatchQueryStrategy()
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert len(resources) == 3
    assert resources[0]["resourceType"] == "Patient"
    assert resources[1]["resourceType"] == "Condition"


# ---------------------------------------------------------------------------
# push_resources
# ---------------------------------------------------------------------------


async def test_push_resources():
    """push_resources sends a transaction bundle to the measure engine."""
    resources = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Condition", "id": "c1"},
    ]
    mock_response = _make_response(200, {"resourceType": "Bundle"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources)

    # Verify post was called
    mock_ctx.post.assert_called_once()
    call_args = mock_ctx.post.call_args
    posted_bundle = call_args.kwargs.get("json") or call_args[1].get("json")
    assert posted_bundle["resourceType"] == "Bundle"
    assert posted_bundle["type"] == "transaction"
    assert len(posted_bundle["entry"]) == 2


async def test_push_resources_empty():
    """push_resources with no valid resources does nothing."""
    resources = [{"no_resourceType": True}]  # Invalid -- missing resourceType and id

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources)

    # post should NOT have been called
    mock_ctx.post.assert_not_called()


# ---------------------------------------------------------------------------
# evaluate_measure
# ---------------------------------------------------------------------------


async def test_evaluate_measure(mock_measure_report):
    """evaluate_measure calls $evaluate-measure and returns the MeasureReport."""
    mock_response = _make_response(200, mock_measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    assert result["resourceType"] == "MeasureReport"
    mock_ctx.get.assert_called_once()
    url = mock_ctx.get.call_args[0][0]
    assert "Measure/measure-1/$evaluate-measure" in url
    assert "periodStart=2024-01-01" in url
    assert "subject=Patient/patient-1" in url


# ---------------------------------------------------------------------------
# wipe_patient_data
# ---------------------------------------------------------------------------


async def test_wipe_patient_data():
    """wipe_patient_data sends DELETE requests for all clinical resource types."""
    mock_response = _make_response(200, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data()

    # Should have made delete calls for each resource type
    assert mock_ctx.delete.call_count >= 10  # At least 10 resource types


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


async def test_fhir_test_connection_success(mock_fhir_metadata):
    """test_connection returns connected status with FHIR version."""
    mock_response = _make_response(200, mock_fhir_metadata)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await fhir_test_connection("http://example.com/fhir")

    assert result["status"] == "connected"
    assert result["fhir_version"] == "4.0.1"
    assert result["software"] == "HAPI FHIR Test"


async def test_fhir_test_connection_failed():
    """test_connection raises when the server is unreachable."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.ConnectError):
            await fhir_test_connection("http://bad-server/fhir")


async def test_fhir_test_connection_401():
    """test_connection raises on 401 Unauthorized."""
    mock_response = httpx.Response(
        401,
        json={"error": "unauthorized"},
        request=httpx.Request("GET", "http://example.com/fhir/metadata"),
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await fhir_test_connection("http://example.com/fhir")


async def test_fhir_test_connection_500():
    """test_connection raises on 500 Internal Server Error."""
    mock_response = httpx.Response(
        500,
        json={"error": "server error"},
        request=httpx.Request("GET", "http://example.com/fhir/metadata"),
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await fhir_test_connection("http://example.com/fhir")


async def test_fhir_test_connection_timeout():
    """test_connection raises on timeout."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.TimeoutException):
            await fhir_test_connection("http://slow-server/fhir")


# ---------------------------------------------------------------------------
# resolve_evaluated_resource
# ---------------------------------------------------------------------------


async def test_resolve_evaluated_resource():
    """resolve_evaluated_resource fetches a resource by reference."""
    resource = {"resourceType": "Patient", "id": "p1"}
    mock_response = _make_response(200, resource)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolve_evaluated_resource("Patient/p1")

    assert result == resource


# ---------------------------------------------------------------------------
# list_measures
# ---------------------------------------------------------------------------


async def test_list_measures(mock_measure_bundle):
    """list_measures returns the bundle from the measure engine."""
    mock_response = _make_response(200, mock_measure_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await list_measures()

    assert result["resourceType"] == "Bundle"
    assert len(result["entry"]) == 1


# ---------------------------------------------------------------------------
# upload_measure_bundle
# ---------------------------------------------------------------------------


async def test_upload_measure_bundle():
    """upload_measure_bundle posts a bundle and returns the response."""
    input_bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    response_bundle = {"resourceType": "Bundle", "type": "transaction-response"}
    mock_response = _make_response(200, response_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await upload_measure_bundle(input_bundle)

    assert result["type"] == "transaction-response"

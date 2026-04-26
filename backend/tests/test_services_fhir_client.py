"""Tests for the FHIR client service (fhir_client.py)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.fhir_client import (
    BatchQueryStrategy,
    DataRequirementsStrategy,
    _acquire_smart_token,
    _build_auth_headers,
    evaluate_measure,
    list_measures,
    push_resources,
    resolve_evaluated_resource,
    trigger_reindex_and_wait,
    trigger_reindex_and_wait_for_patients,
    upload_measure_bundle,
    wait_for_valueset_expansion,
    wipe_patient_data,
)
from app.services.fhir_client import (
    verify_fhir_connection as fhir_test_connection,
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
    async def test_no_auth(self):
        result = await _build_auth_headers("none", None)
        assert result == {}

    async def test_no_auth_with_credentials(self):
        """Even with credentials, 'none' auth type returns empty."""
        result = await _build_auth_headers("none", {"username": "u", "password": "p"})
        assert result == {}

    async def test_basic_auth(self):
        result = await _build_auth_headers("basic", {"username": "admin", "password": "secret"})
        assert "Authorization" in result
        assert result["Authorization"].startswith("Basic ")
        import base64

        decoded = base64.b64decode(result["Authorization"].split(" ")[1]).decode()
        assert decoded == "admin:secret"

    async def test_bearer_auth(self):
        result = await _build_auth_headers("bearer", {"token": "my-jwt"})
        assert result == {"Authorization": "Bearer my-jwt"}

    async def test_unknown_auth_type(self):
        result = await _build_auth_headers("oauth2", {"token": "abc"})
        assert result == {}

    async def test_basic_auth_no_credentials(self):
        result = await _build_auth_headers("basic", None)
        assert result == {}

    async def test_smart_auth(self):
        """_build_auth_headers with SMART type calls _acquire_smart_token internally."""
        credentials = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://auth.example.com/token",
        }
        with patch(
            "app.services.fhir_client._acquire_smart_token",
            new=AsyncMock(return_value="smart-token-abc"),
        ):
            result = await _build_auth_headers("smart", credentials)
        assert result == {"Authorization": "Bearer smart-token-abc"}


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
    """push_resources sends a batch bundle to the measure engine."""
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
    assert posted_bundle["type"] == "batch"
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


async def test_push_resources_sorts_patients_first():
    """push_resources MUST place Patient entries before any resource that
    references them. HAPI's bundle import skips writing reference index
    entries for forward-references, so an Encounter appearing in the bundle
    before its referenced Patient persists the Encounter but never indexes
    `Encounter.subject → Patient/{id}`. `Encounter?patient=` then returns 0
    forever. Verified empirically 2026-04-25 (issue #177): same bundle,
    original order = 20/33 indexed; Patients-first = 33/33 at t=0.

    This test pins the sort behavior so we don't regress.
    """
    # Caller passes resources in a "bad" order: Encounter before Patient.
    resources = [
        {"resourceType": "Encounter", "id": "e1", "subject": {"reference": "Patient/p1"}},
        {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}},
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Encounter", "id": "e2", "subject": {"reference": "Patient/p2"}},
        {"resourceType": "Patient", "id": "p2"},
    ]
    mock_response = _make_response(200, {"resourceType": "Bundle"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources)

    posted_bundle = mock_ctx.post.call_args.kwargs.get("json") or mock_ctx.post.call_args[1].get("json")
    types_in_order = [e["resource"]["resourceType"] for e in posted_bundle["entry"]]

    # All Patients come first.
    first_non_patient_idx = next(i for i, t in enumerate(types_in_order) if t != "Patient")
    assert all(t == "Patient" for t in types_in_order[:first_non_patient_idx]), (
        f"Patients must lead; got order: {types_in_order}"
    )
    assert "Patient" not in types_in_order[first_non_patient_idx:], (
        f"No Patient may appear after a non-Patient; got order: {types_in_order}"
    )
    # Stable order is preserved within each group.
    non_patient_types = [t for t in types_in_order if t != "Patient"]
    assert non_patient_types == ["Encounter", "Condition", "Encounter"], (
        f"Non-Patient relative order should be preserved; got: {non_patient_types}"
    )


async def test_push_resources_with_auth_headers():
    """push_resources forwards auth_headers alongside Content-Type."""
    resources = [{"resourceType": "Patient", "id": "p1"}]
    mock_response = _make_response(200, {"resourceType": "Bundle"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(
            resources,
            target_url="http://test-measure/",
            auth_headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )

    mock_ctx.post.assert_called_once()
    call_args = mock_ctx.post.call_args
    sent_headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
    assert sent_headers.get("Authorization") == "Basic dXNlcjpwYXNz"
    assert sent_headers.get("Content-Type", "").startswith("application/fhir+json")


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


async def test_evaluate_measure_retries_transient_5xx(mock_measure_report):
    """Transient HAPI 5xx responses are retried before returning the MeasureReport."""
    responses = [
        _make_response(500, {"resourceType": "OperationOutcome"}),
        _make_response(502, {"resourceType": "OperationOutcome"}),
        _make_response(200, mock_measure_report),
    ]

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=responses)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("app.services.fhir_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    assert result["resourceType"] == "MeasureReport"
    assert mock_ctx.get.call_count == 3
    assert mock_sleep.await_count == 2


async def test_evaluate_measure_does_not_retry_4xx():
    """Known HAPI/MADiE 4xx failures should surface without retrying."""
    response = _make_response(400, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(httpx.HTTPStatusError):
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    mock_ctx.get.assert_awaited_once()


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


async def test_wipe_patient_data_includes_qi_core_types():
    """wipe_patient_data includes QI-Core clinical types added for STU6 bundles."""
    mock_response = _make_response(200, {})
    deleted_urls: list[str] = []

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()

        async def capture_delete(url, **kwargs):
            deleted_urls.append(url)
            return mock_response

        mock_ctx.delete = AsyncMock(side_effect=capture_delete)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data()

    wiped_types = {url.split("/")[-1].split("?")[0] for url in deleted_urls}
    for expected_type in (
        "DeviceRequest",
        "Medication",
        "Task",
        "MedicationAdministration",
        "AdverseEvent",
        "Location",
        "Practitioner",
        "Organization",
    ):
        assert expected_type in wiped_types, f"{expected_type} missing from wipe list"


async def test_wipe_patient_data_strict_raises_after_consecutive_failures():
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Measure engine unreachable"):
            await wipe_patient_data()

    assert mock_ctx.delete.call_count == 3


async def test_wipe_patient_data_non_strict_raises_after_consecutive_failures():
    """non-strict mode now raises after 3 failures (silent return caused the race condition)."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Measure engine unreachable"):
            await wipe_patient_data(strict=False)

    assert mock_ctx.delete.call_count == 3


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

        result = await fhir_test_connection("https://example.com/fhir")

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
            await fhir_test_connection("https://bad-server/fhir")


async def test_fhir_test_connection_401():
    """test_connection raises on 401 Unauthorized."""
    mock_response = httpx.Response(
        401,
        json={"error": "unauthorized"},
        request=httpx.Request("GET", "https://example.com/fhir/metadata"),
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await fhir_test_connection("https://example.com/fhir")


async def test_fhir_test_connection_500():
    """test_connection raises on 500 Internal Server Error."""
    mock_response = httpx.Response(
        500,
        json={"error": "server error"},
        request=httpx.Request("GET", "https://example.com/fhir/metadata"),
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await fhir_test_connection("https://example.com/fhir")


async def test_fhir_test_connection_timeout():
    """test_connection raises on timeout."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.TimeoutException):
            await fhir_test_connection("https://slow-server/fhir")


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


# ---------------------------------------------------------------------------
# _acquire_smart_token
# ---------------------------------------------------------------------------


_SMART_CREDENTIALS = {
    "client_id": "c1",
    "client_secret": "s1",
    "token_endpoint": "https://auth.example.com/token",
}


class TestAcquireSmartToken:
    async def test_success(self):
        """_acquire_smart_token returns the access_token on success."""
        token_response = httpx.Response(
            200,
            json={"access_token": "tok123", "token_type": "bearer"},
            request=httpx.Request("POST", "https://auth.example.com/token"),
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=token_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await _acquire_smart_token(_SMART_CREDENTIALS)

        assert token == "tok123"
        call_args = mock_ctx.post.call_args
        assert call_args[0][0] == "https://auth.example.com/token"
        posted_data = call_args.kwargs.get("data") or call_args[1].get("data")
        assert posted_data["grant_type"] == "client_credentials"
        assert posted_data["client_id"] == "c1"
        assert posted_data["client_secret"] == "s1"

    async def test_401_raises(self):
        """_acquire_smart_token raises HTTPStatusError on 401."""
        error_response = httpx.Response(
            401,
            json={"error": "unauthorized"},
            request=httpx.Request("POST", "http://auth.example.com/token"),
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=error_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.HTTPStatusError):
                await _acquire_smart_token(_SMART_CREDENTIALS)

    async def test_500_raises(self):
        """_acquire_smart_token raises HTTPStatusError on 500."""
        error_response = httpx.Response(
            500,
            json={"error": "server error"},
            request=httpx.Request("POST", "http://auth.example.com/token"),
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=error_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.HTTPStatusError):
                await _acquire_smart_token(_SMART_CREDENTIALS)

    async def test_network_error_raises(self):
        """_acquire_smart_token propagates network errors."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(httpx.ConnectError):
                await _acquire_smart_token(_SMART_CREDENTIALS)

    async def test_ssrf_blocked_http_external(self):
        """_acquire_smart_token rejects plain http for non-localhost token_endpoint."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://evil.example.com/token",
        }
        with pytest.raises(ValueError, match="SSRF protection"):
            await _acquire_smart_token(creds)

    async def test_ssrf_blocked_rfc1918(self):
        """_acquire_smart_token rejects RFC-1918 addresses."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "https://192.168.1.1/token",
        }
        with pytest.raises(ValueError, match="SSRF protection"):
            await _acquire_smart_token(creds)

    async def test_ssrf_allowed_localhost_http(self):
        """_acquire_smart_token allows http://localhost for local dev."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://localhost:9090/token",
        }
        token_response = httpx.Response(
            200,
            json={"access_token": "local-tok"},
            request=httpx.Request("POST", "http://localhost:9090/token"),
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=token_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await _acquire_smart_token(creds)
        assert token == "local-tok"

    async def test_ssrf_allowed_127_http(self):
        """_acquire_smart_token allows http://127.0.0.1 for local dev."""
        creds = {
            "client_id": "c1",
            "client_secret": "s1",
            "token_endpoint": "http://127.0.0.1:8080/token",
        }
        token_response = httpx.Response(
            200,
            json={"access_token": "loopback-tok"},
            request=httpx.Request("POST", "http://127.0.0.1:8080/token"),
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.post = AsyncMock(return_value=token_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await _acquire_smart_token(creds)
        assert token == "loopback-tok"


# ---------------------------------------------------------------------------
# _validate_ssrf_url
# ---------------------------------------------------------------------------


class TestValidateSsrfUrl:
    def test_https_external_allowed(self):
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("https://fhir.example.com/token")  # should not raise

    def test_http_localhost_allowed(self):
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("http://localhost:8080/fhir")  # should not raise

    def test_http_127_allowed(self):
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("http://127.0.0.1/fhir")  # should not raise

    def test_http_external_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="must use https"):
            _validate_ssrf_url("http://external.example.com/fhir")

    def test_ftp_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="not allowed"):
            _validate_ssrf_url("ftp://example.com/file")

    def test_rfc1918_10_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://10.0.0.1/fhir")

    def test_rfc1918_172_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://172.16.0.1/fhir")

    def test_rfc1918_192_168_blocked(self):
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://192.168.100.200/fhir")

    def test_imds_endpoint_http_blocked(self):
        """Classic AWS IMDSv1 endpoint — http with non-local host is blocked."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="must use https"):
            _validate_ssrf_url("http://169.254.169.254/latest/meta-data/")

    def test_imds_endpoint_https_blocked(self):
        """AWS IMDS link-local over https is blocked by IP range check."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://169.254.169.254/latest/meta-data/")

    def test_ipv6_loopback_allowed(self):
        """::1 is in the local dev allowlist."""
        from app.services.fhir_client import _validate_ssrf_url

        _validate_ssrf_url("http://[::1]:8080/fhir")  # should not raise

    def test_ipv6_link_local_blocked(self):
        """fe80:: link-local IPv6 is blocked."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://[fe80::1]/fhir")

    def test_ipv6_ula_blocked(self):
        """fc00::/7 Unique Local Address IPv6 is blocked."""
        from app.services.fhir_client import _validate_ssrf_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_ssrf_url("https://[fd00::1]/fhir")


async def test_verify_fhir_connection_ssrf_blocked():
    """verify_fhir_connection raises ValueError for http non-localhost URLs."""
    with pytest.raises(ValueError, match="SSRF protection"):
        await fhir_test_connection("http://internal.corp.example.com/fhir")


# ---------------------------------------------------------------------------
# DataRequirementsStrategy
# ---------------------------------------------------------------------------


async def test_data_requirements_strategy_uses_requirements():
    """DataRequirementsStrategy fetches resources per $data-requirements entries."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Patient"},
            {"type": "Observation"},
        ],
    }
    patient_resource = {"resourceType": "Patient", "id": "p1"}
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    get_responses = {
        "Measure/m1/$data-requirements": _make_response(200, data_req_response),
        "Observation?subject=Patient/p1": _make_response(200, obs_bundle),
        "Patient/p1": _make_response(200, patient_resource),
    }

    async def mock_get(url, **kwargs):
        for key, resp in get_responses.items():
            if key in url:
                return resp
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert len(resources) == 2
    types = {r["resourceType"] for r in resources}
    assert types == {"Patient", "Observation"}


async def test_data_requirements_strategy_falls_back_on_empty():
    """DataRequirementsStrategy falls back to $everything when $data-requirements returns no entries."""
    empty_lib = {"resourceType": "Library", "dataRequirement": []}
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    call_count = {"n": 0}

    async def mock_get(url, **kwargs):
        call_count["n"] += 1
        if "$data-requirements" in url:
            return _make_response(200, empty_lib)
        return _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert len(resources) == 1
    assert resources[0]["resourceType"] == "Patient"
    assert call_count["n"] >= 2


async def test_data_requirements_strategy_falls_back_on_error():
    """DataRequirementsStrategy falls back to $everything when $data-requirements raises."""
    import httpx as _httpx_module

    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            raise _httpx_module.ConnectError("MCS unreachable")
        return _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert len(resources) == 1


async def test_data_requirements_strategy_fetch_fails_falls_back_to_everything():
    """DataRequirementsStrategy falls back to $everything when CDR fetch raises."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    call_count = {"n": 0}

    async def mock_get(url, **kwargs):
        call_count["n"] += 1
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Patient/p1" in url and "$everything" not in url:
            raise httpx.ConnectError("CDR unreachable")
        # fallback $everything call
        return _make_response(200, everything_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    # Should have fallen back to $everything and returned the patient
    assert any(r.get("resourceType") == "Patient" for r in resources)


async def test_data_requirements_strategy_dedup_skips_duplicate_types():
    """DataRequirementsStrategy skips a resource type that appears twice in requirements."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation"},
            {"type": "Observation"},  # duplicate — should only query once
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(
            side_effect=lambda url, **kw: (
                _make_response(200, data_req_response)
                if "$data-requirements" in url
                else _make_response(200, obs_bundle)
            )
        )
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    # Only one Observation even though type appeared twice
    obs_resources = [r for r in resources if r.get("resourceType") == "Observation"]
    assert len(obs_resources) == 1


async def test_data_requirements_strategy_non_200_patient_not_appended():
    """DataRequirementsStrategy skips Patient resource when CDR returns non-200."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(
            side_effect=lambda url, **kw: (
                _make_response(200, data_req_response)
                if "$data-requirements" in url
                else _make_response(404, {"resourceType": "OperationOutcome"})
            )
        )
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    # 404 from CDR means no resources returned (no fallback for non-200 within _fetch_by_requirements)
    assert resources == []


async def test_data_requirements_strategy_non_200_resource_entries_skipped():
    """DataRequirementsStrategy skips entries when CDR returns non-200 for a resource type."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(
            side_effect=lambda url, **kw: (
                _make_response(200, data_req_response)
                if "$data-requirements" in url
                else _make_response(500, {"resourceType": "OperationOutcome"})
            )
        )
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert resources == []


async def test_fetch_by_requirements_code_filter_appends_code_in():
    """codeFilter.valueSet is translated to code:in= search parameter (AC2)."""
    vs_url = "http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.198.12.1134"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {
                "type": "Observation",
                "codeFilter": [{"valueSet": vs_url}],
            }
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    obs_url = next((u for u in captured_urls if "Observation" in u), None)
    assert obs_url is not None
    assert "code:in=" in obs_url
    assert vs_url in obs_url
    assert len(resources) == 1


async def test_fetch_by_requirements_date_filter_does_not_add_params():
    """dateFilter entries do not modify the URL — type-only query used (AC2)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {
                "type": "Observation",
                "dateFilter": [{"path": "effective", "valuePeriod": {"start": "2024-01-01", "end": "2024-12-31"}}],
            }
        ],
    }
    obs_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    obs_url = next((u for u in captured_urls if "Observation" in u), None)
    assert obs_url is not None
    assert "code:in" not in obs_url


async def test_fetch_by_requirements_no_filter_type_only():
    """dataRequirement with no codeFilter generates plain type+subject query (AC2 baseline)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Encounter"}],
    }
    enc_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Encounter", "id": "e1"}}],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        return _make_response(200, enc_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    enc_url = next((u for u in captured_urls if "Encounter" in u), None)
    assert enc_url is not None
    assert "code:in" not in enc_url
    assert len(resources) == 1


async def test_fetch_by_requirements_one_type_fails_partial_result_no_fallback():
    """One type fails CDR fetch — others succeed; partial result returned without $everything (AC5)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation"},
            {"type": "Condition"},
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Observation" in url:
            return _make_response(200, obs_bundle)
        if "Condition" in url:
            raise httpx.ConnectError("CDR unreachable for Condition")
        return _make_response(200, {"resourceType": "Bundle", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        resources = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    # Observation fetched; Condition skipped; no $everything fallback
    assert any(r.get("resourceType") == "Observation" for r in resources)
    assert not any(r.get("resourceType") == "Condition" for r in resources)
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0


async def test_data_requirements_strategy_gather_patients_delegates_to_batch():
    """DataRequirementsStrategy.gather_patients uses the same BatchQuery logic."""
    patient_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
        "link": [],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(200, patient_bundle))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1")
        patients = await strategy.gather_patients("http://cdr/fhir", {})

    assert len(patients) == 1
    assert patients[0]["id"] == "p1"


class FakeSyncResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSyncClient:
    def __init__(self, *, get_responses: list[FakeSyncResponse], post_responses: list[FakeSyncResponse]) -> None:
        self.get_responses = get_responses
        self.post_responses = post_responses
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str, **kwargs):
        self.get_calls.append(url)
        return self.get_responses.pop(0)

    def post(self, url: str, json: dict | None = None, **kwargs):
        self.post_calls.append((url, json))
        return self.post_responses.pop(0)


def test_trigger_reindex_and_wait_posts_202_and_polls_success(monkeypatch, caplog):
    client = FakeSyncClient(
        get_responses=[
            FakeSyncResponse(200, {"entry": [{"resource": {"id": "p1"}}]}),
            FakeSyncResponse(200, {"entry": [{"resource": {"id": "e1"}}]}),
        ],
        post_responses=[FakeSyncResponse(202)],
    )

    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("app.services.fhir_client.time.sleep", lambda seconds: None)

    with caplog.at_level("INFO"):
        trigger_reindex_and_wait("http://hapi/fhir")

    assert client.post_calls[0][0] == "http://hapi/fhir/$reindex"
    assert client.post_calls[0][1] == {
        "resourceType": "Parameters",
        "parameter": [{"name": "type", "valueString": "Encounter"}],
    }
    assert client.get_calls == [
        "http://hapi/fhir/Patient?_count=1",
        "http://hapi/fhir/Encounter?patient=p1&_count=1",
    ]
    assert "HAPI reindex complete" in caplog.text


def test_trigger_reindex_and_wait_for_patients_polls_until_all_patients_index(monkeypatch, caplog):
    client = FakeSyncClient(
        get_responses=[
            FakeSyncResponse(200, {"entry": [{"resource": {"id": "e1"}}]}),
            FakeSyncResponse(200, {"entry": []}),
            FakeSyncResponse(200, {"entry": [{"resource": {"id": "e2"}}]}),
        ],
        post_responses=[FakeSyncResponse(202)],
    )

    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("app.services.fhir_client.time.sleep", lambda seconds: None)

    with caplog.at_level("INFO"):
        trigger_reindex_and_wait_for_patients("http://hapi/fhir", ["Patient/p1", "p2"])

    assert client.post_calls[0][0] == "http://hapi/fhir/$reindex"
    assert client.get_calls == [
        "http://hapi/fhir/Encounter?patient=p1&_count=1",
        "http://hapi/fhir/Encounter?patient=p2&_count=1",
        "http://hapi/fhir/Encounter?patient=p2&_count=1",
    ]
    assert "HAPI reindex complete" in caplog.text


def test_trigger_reindex_and_wait_logs_timeout(monkeypatch, caplog):
    client = FakeSyncClient(get_responses=[], post_responses=[FakeSyncResponse(202)])
    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)

    with caplog.at_level("WARNING"):
        trigger_reindex_and_wait("http://hapi/fhir", probe_patient_id="p1", timeout_s=0)

    assert client.post_calls[0][0] == "http://hapi/fhir/$reindex"
    assert "HAPI reindex timed out" in caplog.text


def test_wait_for_valueset_expansion_returns_per_url_status(monkeypatch, caplog):
    client = FakeSyncClient(
        get_responses=[
            FakeSyncResponse(200, {"entry": []}),
            FakeSyncResponse(200, {"entry": [{"resource": {"id": "vs-ok"}}]}),
        ],
        post_responses=[
            FakeSyncResponse(200, {"expansion": {"total": 42, "contains": [{"code": "a"}]}}),
        ],
    )
    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)
    monkeypatch.setattr("app.services.fhir_client.time.sleep", lambda seconds: None)

    with caplog.at_level("WARNING"):
        expanded = wait_for_valueset_expansion("http://hapi/fhir", ["http://vs/ok", "http://vs/missing"], timeout_s=1)

    assert expanded == {"http://vs/ok": 42}
    assert client.post_calls == [("http://hapi/fhir/ValueSet/vs-ok/$expand?count=2", None)]
    assert "ValueSet not found for expansion wait" in caplog.text


def test_wait_for_valueset_expansion_logs_timeout(monkeypatch, caplog):
    client = FakeSyncClient(
        get_responses=[FakeSyncResponse(200, {"entry": [{"resource": {"id": "vs-timeout"}}]})],
        post_responses=[],
    )
    monkeypatch.setattr("app.services.fhir_client.httpx.Client", lambda **kwargs: client)

    with caplog.at_level("WARNING"):
        expanded = wait_for_valueset_expansion("http://hapi/fhir", ["http://vs/timeout"], timeout_s=0)

    assert expanded == {}
    assert "ValueSet expansion timed out" in caplog.text

"""Tests for the FHIR client service (fhir_client.py)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.fhir_client import (
    _DEFAULT_PATIENT_SCOPE_PARAM,
    _MAX_OPERATION_DEFINITION_PROBES,
    _PATIENT_SCOPE_PARAM_OVERRIDES,
    _PATIENT_SCOPED_TYPES,
    _PATIENT_UNSCOPABLE_TYPES,
    _WIPE_ENUMERATE_MAX_PAGES,
    _WIPE_ID_CHUNK_SIZE,
    _WIPE_MAX_TXN_ENTRIES,
    SUBMIT_DATA_MODE_BASE,
    SUBMIT_DATA_MODE_STU5,
    BatchQueryStrategy,
    DataRequirementsStrategy,
    GatherResult,
    _acquire_smart_token,
    _build_auth_headers,
    _chunk_request_entries,
    _operation_definition_matches_contract,
    _patient_scope_param,
    _remap_valueset_ids_for_hapi,
    _resolve_operation_definition,
    delete_measure,
    detect_submit_data_capability,
    evaluate_measure,
    get_measure_canonical,
    list_measures,
    measure_exists,
    push_resources,
    resolve_evaluated_resource,
    submit_data,
    upload_measure_bundle,
    wait_for_valueset_expansion,
    wipe_measure_definitions,
    wipe_patient_data,
    wipe_patients_by_id,
)
from app.services.fhir_client import (
    verify_fhir_connection as fhir_test_connection,
)
from app.services.fhir_errors import FhirOperationError

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
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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
    """Known HAPI/MADiE 4xx failures surface as FhirOperationError without retrying."""
    response = _make_response(400, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    mock_ctx.get.assert_awaited_once()
    assert exc_info.value.status_code == 400
    assert exc_info.value.operation == "evaluate-measure"


async def test_evaluate_measure_raises_fhir_error_with_outcome_on_4xx():
    """evaluate_measure preserves the MCS OperationOutcome in FhirOperationError on 4xx."""
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Measure not found"}],
    }
    response = _make_response(404, oo)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    err = exc_info.value
    assert err.status_code == 404
    assert err.outcome is not None
    assert err.outcome.issues[0].diagnostics == "Measure not found"


async def test_evaluate_measure_raises_on_200_with_operation_outcome():
    """evaluate_measure raises FhirOperationError when MCS returns 200 OK with OperationOutcome."""
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": "CQL evaluation error"}],
    }
    response = _make_response(200, oo)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    err = exc_info.value
    assert err.status_code == 200
    assert err.outcome is not None
    assert "CQL evaluation error" in err.outcome.primary_diagnostic()


async def test_evaluate_measure_raises_on_200_with_error_status_measure_report():
    """evaluate_measure raises FhirOperationError when HAPI returns 200 OK with
    MeasureReport.status == 'error' (e.g. Unknown ValueSet).  Populations are all
    zero in that response but the error must not be silently swallowed."""
    contained_oo = {
        "resourceType": "OperationOutcome",
        "id": "oo-1",
        "issue": [
            {
                "severity": "error",
                "code": "exception",
                "diagnostics": (
                    "Exception for subjectId: Patient/p1, "
                    "Message: HAPI-2788: Unknown ValueSet: "
                    "http%3A%2F%2Fcts.nlm.nih.gov%2Ffhir%2FValueSet%2F2.16.840.1.113762.1.4.1248.208"
                ),
            }
        ],
    }
    measure_report = {
        "resourceType": "MeasureReport",
        "status": "error",
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
        "contained": [contained_oo],
        "group": [
            {
                "population": [
                    {"code": {"coding": [{"code": "initial-population"}]}, "count": 0},
                ]
            }
        ],
    }
    response = _make_response(200, measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await evaluate_measure("CMS122FHIRDiabetesAssessGreaterThan9Percent", "p1", "2026-01-01", "2026-12-31")

    err = exc_info.value
    assert err.status_code == 200
    assert err.operation == "evaluate-measure"
    assert err.outcome is not None
    assert "HAPI-2788" in err.outcome.primary_diagnostic()
    assert "2.16.840.1.113762.1.4.1248.208" in err.outcome.primary_diagnostic()


async def test_push_resources_raises_fhir_error_on_http_failure():
    """push_resources raises FhirOperationError on non-2xx response."""
    resources = [{"resourceType": "Patient", "id": "p1"}]
    response = _make_response(500, {"resourceType": "OperationOutcome", "issue": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await push_resources(resources)

    assert exc_info.value.status_code == 500
    assert exc_info.value.operation == "push-resources"


async def test_push_resources_raises_on_200_with_operation_outcome():
    """push_resources raises FhirOperationError on 200 OK with OperationOutcome body (entire-batch rejection)."""
    resources = [{"resourceType": "Patient", "id": "p1"}]
    oo = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "invalid"}]}
    response = _make_response(200, oo)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(FhirOperationError) as exc_info:
            await push_resources(resources)

    assert exc_info.value.status_code == 200
    assert exc_info.value.operation == "push-resources"


async def test_push_resources_returns_bundle_result_with_failed_entries():
    """push_resources returns BundleUploadResult capturing per-entry failures."""
    from app.services.fhir_client import BundleUploadResult

    resources = [
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Condition", "id": "c1"},
    ]
    # HAPI batch response: one 201, one 422
    batch_response = {
        "resourceType": "Bundle",
        "type": "batch-response",
        "entry": [
            {"response": {"status": "201 Created"}},
            {"response": {"status": "422 Unprocessable Entity"}},
        ],
    }
    response = _make_response(200, batch_response)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await push_resources(resources)

    assert isinstance(result, BundleUploadResult)
    assert len(result.succeeded) == 1
    assert len(result.failed) == 1
    assert result.succeeded[0].resource_type == "Patient"
    assert result.failed[0].resource_type == "Condition"
    assert result.has_failures is True


async def test_push_resources_max_bundle_entries_none_posts_once():
    """max_bundle_entries=None preserves the existing single-POST behavior."""
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(5)]
    mock_response = _make_response(200, {"resourceType": "Bundle", "entry": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await push_resources(resources, max_bundle_entries=None)

    assert mock_ctx.post.call_count == 1


async def test_push_resources_chunks_when_max_bundle_entries_set():
    """5 resources with max_bundle_entries=2 should POST 3 bundles
    (sizes 2, 2, 1) and aggregate per-entry results across them.
    """
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(5)]

    def make_resp_for(req_bundle_json):
        n = len(req_bundle_json["entry"])
        body = {
            "resourceType": "Bundle",
            "type": "batch-response",
            "entry": [{"response": {"status": "201 Created"}} for _ in range(n)],
        }
        return _make_response(200, body)

    posted_bundles = []

    async def fake_post(url, *, json, headers):
        posted_bundles.append(json)
        return make_resp_for(json)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(side_effect=fake_post)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await push_resources(resources, max_bundle_entries=2)

    assert mock_ctx.post.call_count == 3
    assert [len(b["entry"]) for b in posted_bundles] == [2, 2, 1]
    assert len(result.succeeded) == 5
    assert len(result.failed) == 0


async def test_push_resources_partial_chunk_failure_does_not_raise():
    """If some chunks succeed and one chunk's HTTP request errors with 400,
    push_resources must NOT raise — it returns an aggregated BundleUploadResult
    with the failed chunk's entries marked as failures and earlier successes
    intact. Mirrors the existing 200-with-per-entry-failures semantics.
    """
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(4)]

    call_idx = {"n": 0}

    async def fake_post(url, *, json, headers):
        call_idx["n"] += 1
        if call_idx["n"] == 2:
            body = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "details": {"text": "Too many entries in bundle. Max supported number of entries is 1"},
                    }
                ],
            }
            return _make_response(400, body)
        n = len(json["entry"])
        body = {
            "resourceType": "Bundle",
            "type": "batch-response",
            "entry": [{"response": {"status": "201 Created"}} for _ in range(n)],
        }
        return _make_response(200, body)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(side_effect=fake_post)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await push_resources(resources, max_bundle_entries=2)

    assert mock_ctx.post.call_count == 2
    assert len(result.succeeded) == 2
    assert len(result.failed) == 2
    for fe in result.failed:
        assert fe.outcome is not None


async def test_push_resources_all_chunks_failed_raises():
    """If every chunk's POST fails atomically, surface the first chunk's
    outcome via FhirOperationError so validation.py marks the upload as
    `failed`. Preserves the existing single-shot raise-on-400 contract.
    """
    from app.services.fhir_errors import FhirOperationError

    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(3)]
    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "exception",
                "details": {"text": "Too many entries in bundle. Max supported number of entries is 1"},
            }
        ],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=_make_response(400, body))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as excinfo:
            await push_resources(resources, max_bundle_entries=2)

    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# _chunk_request_entries — partition helper
# ---------------------------------------------------------------------------


def _entry(resource_type, resource_id):
    return {
        "resource": {"resourceType": resource_type, "id": resource_id},
        "request": {"method": "PUT", "url": f"{resource_type}/{resource_id}"},
    }


def test_chunk_request_entries_none_returns_single_chunk():
    entries = [_entry("Patient", "p1"), _entry("Encounter", "e1")]
    chunks = _chunk_request_entries(entries, max_size=None)
    assert chunks == [entries]


def test_chunk_request_entries_zero_max_returns_single_chunk():
    # Defensive: treat 0 or negative as "no chunking" (the API layer already
    # rejects these at request time, but the service layer should be safe).
    entries = [_entry("Patient", "p1")]
    assert _chunk_request_entries(entries, max_size=0) == [entries]
    assert _chunk_request_entries(entries, max_size=-1) == [entries]


def test_chunk_request_entries_partitions_evenly():
    entries = [_entry("Patient", f"p{i}") for i in range(6)]
    chunks = _chunk_request_entries(entries, max_size=2)
    assert len(chunks) == 3
    assert all(len(c) == 2 for c in chunks)
    # Order within and across chunks must be preserved.
    assert [e["resource"]["id"] for c in chunks for e in c] == [f"p{i}" for i in range(6)]


def test_chunk_request_entries_handles_remainder():
    entries = [_entry("Patient", f"p{i}") for i in range(5)]
    chunks = _chunk_request_entries(entries, max_size=2)
    assert [len(c) for c in chunks] == [2, 2, 1]


def test_chunk_request_entries_preserves_patients_first_across_chunks():
    """Patients must precede non-Patients in the chunk sequence, so HAPI's
    reference index sees the Patient before any Encounter that references it.
    This protects the invariant from issue #177 across chunk boundaries.
    """
    entries = [
        _entry("Patient", "p1"),
        _entry("Patient", "p2"),
        _entry("Patient", "p3"),
        _entry("Encounter", "e1"),
        _entry("Encounter", "e2"),
        _entry("Condition", "c1"),
    ]
    chunks = _chunk_request_entries(entries, max_size=2)
    flat_types = [e["resource"]["resourceType"] for c in chunks for e in c]
    # Find first non-Patient and assert no Patient appears after it.
    first_non_patient = next(i for i, t in enumerate(flat_types) if t != "Patient")
    assert "Patient" not in flat_types[first_non_patient:], (
        f"Patient appeared after a non-Patient across chunks: {flat_types}"
    )


async def test_gather_result_partial_failure_surfaced():
    """DataRequirementsStrategy returns GatherResult with failed_types on partial CDR failure."""
    from app.services.fhir_client import GatherResult

    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Observation"}, {"type": "Condition"}],
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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert isinstance(gather_result, GatherResult)
    assert gather_result.has_partial_failure
    failed_type_names = [f.resource_type for f in gather_result.failed_types]
    assert "Condition" in failed_type_names
    assert any(r.get("resourceType") == "Observation" for r in gather_result.resources)
    # Should NOT have fallen back to $everything
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0


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

        await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    # Should have made delete calls for each resource type
    assert mock_ctx.delete.call_count >= 10  # At least 10 resource types


async def test_wipe_patient_data_raises_on_unauthorized_instead_of_silent_noop():
    """A 401 on the conditional delete must abort, not degrade to a silent no-op.

    Regression guard. `httpx` does not raise on status codes and the wipe never
    called `raise_for_status()`, so a 401 fell through to the search-and-delete
    fallback — which was unauthenticated, 401'd on its own GET, `break`ed, and
    let wipe_patient_data return success having deleted nothing. The prior job's
    resources then inflated the next job's populations with no error signal.
    """
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=_make_response(401, {}))
        mock_ctx.get = AsyncMock(return_value=_make_response(401, {}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="Not authorized to wipe"):
            await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers={"Authorization": "Bearer x"})


async def test_wipe_patient_data_fallback_carries_credentials():
    """When conditional delete is unsupported, the fallback sweep stays authenticated."""
    headers = {"Authorization": "Bearer tok-1"}
    empty_bundle = _make_response(200, {"resourceType": "Bundle", "entry": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        # 405 = conditional delete unsupported, the legitimate fallback trigger.
        mock_ctx.delete = AsyncMock(return_value=_make_response(405, {}))
        mock_ctx.get = AsyncMock(return_value=empty_bundle)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers=headers)

    assert mock_ctx.get.await_count > 0, "fallback sweep never ran"
    for call in mock_ctx.get.await_args_list:
        assert call.kwargs.get("headers") == headers, "fallback GET went out unauthenticated"


async def test_wipe_patient_data_fallback_rejects_cross_origin_next_link():
    """A hostile next link must not steer the fallback sweep at another host."""
    hostile = _make_response(
        200,
        {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Patient", "id": "p1"}}],
            "link": [{"relation": "next", "url": "http://169.254.169.254/latest/meta-data/"}],
        },
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=_make_response(405, {}))
        mock_ctx.get = AsyncMock(return_value=hostile)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers={})

    for call in mock_ctx.get.await_args_list:
        requested = call.args[0] if call.args else ""
        assert "169.254.169.254" not in requested, "followed a cross-origin next link"


async def test_wipe_patient_data_fallback_rejects_traversal_ids():
    """Server-supplied ids containing path separators must not become DELETE paths."""
    malicious = _make_response(
        200,
        {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "../../Measure/CMS130"}},
                {"resource": {"resourceType": "Patient", "id": "safe-1"}},
            ],
        },
    )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=_make_response(405, {}))
        mock_ctx.get = AsyncMock(return_value=malicious)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="https://mcs.example.org/fhir", auth_headers={})

    for call in mock_ctx.delete.await_args_list:
        requested = call.args[0] if call.args else ""
        assert "Measure/CMS130" not in requested, "traversal id became a DELETE path"


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

        await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

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


async def test_wipe_patient_data_patient_deleted_after_clinical_resources():
    """Patient must be deleted AFTER clinical types to avoid HAPI 409 referential-integrity errors.

    HAPI returns 409 when a DELETE targets Patient while Condition/Encounter/etc.
    still reference it.  Regression test for issue #235.
    """
    mock_response = _make_response(200, {})
    delete_order: list[str] = []

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()

        async def capture_delete(url, **kwargs):
            rt = url.split("/")[-1].split("?")[0]
            delete_order.append(rt)
            return mock_response

        mock_ctx.delete = AsyncMock(side_effect=capture_delete)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    assert "Patient" in delete_order, "Patient must be in the wipe list"
    patient_idx = delete_order.index("Patient")
    clinical_types = {
        "Condition",
        "Observation",
        "Encounter",
        "Procedure",
        "MedicationRequest",
        "MedicationAdministration",
    }
    for rt in clinical_types:
        assert rt in delete_order, f"{rt} missing from wipe list"
        assert delete_order.index(rt) < patient_idx, (
            f"{rt} must be deleted before Patient (got {rt} at {delete_order.index(rt)}, Patient at {patient_idx})"
        )


async def test_wipe_patient_data_strict_raises_after_consecutive_failures():
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="FHIR server unreachable"):
            await wipe_patient_data(base_url="http://test-fhir:8080/fhir")

    assert mock_ctx.delete.call_count == 3


async def test_wipe_patient_data_non_strict_raises_after_consecutive_failures():
    """non-strict mode now raises after 3 failures (silent return caused the race condition)."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="FHIR server unreachable"):
            await wipe_patient_data(base_url="http://test-fhir:8080/fhir", strict=False)

    assert mock_ctx.delete.call_count == 3


# ---------------------------------------------------------------------------
# wipe_patients_by_id (issue #392)
# ---------------------------------------------------------------------------


def _delete_urls(mock_ctx) -> list[str]:
    """Every URL passed positionally to client.delete()."""
    return [call.args[0] for call in mock_ctx.delete.await_args_list]


class TestWipePatientsById:
    """The patient-scoped wipe that makes running against a shared MCS safe.

    Issue #392: the full wipe deletes every patient on the target server. Since
    evaluation is per-subject (`$evaluate-measure?subject=Patient/<id>`), deleting
    only the IDs this job is about to push is equivalent for correctness while
    leaving other participants' data alone.
    """

    def _mock_client(self, mock_httpx, response=None):
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=response or _make_response(200, {}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_ctx

    async def test_every_delete_is_scoped_to_the_given_patients(self):
        """No DELETE may go out without a scoping filter.

        This is the whole point of the issue: an unfiltered conditional delete is
        what wipes a shared server. The three legitimate scoping params are
        `patient=`, `subject=` (AdverseEvent) and `_id=` (Patient itself).
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1", "p2"])

        urls = _delete_urls(mock_ctx)
        assert urls, "no deletes issued"
        for url in urls:
            assert any(f"?{p}=" in url for p in ("patient", "subject", "_id")), (
                f"unscoped delete would wipe the server: {url}"
            )
            assert "_lastUpdated" not in url, f"full-wipe filter leaked into the scoped wipe: {url}"

    async def test_unscopable_types_are_never_deleted(self):
        """The wipe skips exactly the types the gather skips — one shared tuple, two consumers.

        `_PATIENT_UNSCOPABLE_TYPES` moved to module scope in #455 so the gather
        could reuse it. Both call sites now depend on it, and the two correct
        answers are not guaranteed to stay identical — a type added here for the
        gather would silently stop being wiped. This pins the wipe half.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = _delete_urls(mock_ctx)
        for resource_type in _PATIENT_UNSCOPABLE_TYPES:
            assert not [u for u in urls if f"/{resource_type}?" in u], (
                f"{resource_type} has no patient-scoped search param — deleting it would hit other tenants"
            )

    async def test_deletes_the_patient_resource_itself(self):
        """The Patient resource must go too, scoped by _id.

        Regression guard: the first implementation swept 19 clinical types and left
        the Patient behind. HAPI 400s on `Patient?patient=`, so Patient needs `_id`
        — and it has to come last, because HAPI 409s while clinical resources still
        reference it. Caught by the integration test, not by a mock.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1", "p2"])

        urls = _delete_urls(mock_ctx)
        patient_urls = [u for u in urls if "/Patient?" in u]
        assert patient_urls, "the Patient resource was never deleted"
        assert "_id=p1,p2" in patient_urls[0]
        assert urls[-1] in patient_urls, "Patient must be deleted last or HAPI returns 409"

    async def test_covers_the_patient_linked_clinical_types(self):
        """The types that carry measure-relevant data must all be swept."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = _delete_urls(mock_ctx)
        for rt in ("MeasureReport", "Condition", "Observation", "Encounter", "Procedure", "MedicationRequest"):
            assert any(f"/{rt}?" in u for u in urls), f"{rt} not wiped"

    async def test_skips_types_with_no_patient_link(self):
        """Medication/Location/Practitioner/Organization have no patient search param.

        Verified against HAPI: both `patient=` and `subject=` return HTTP 400 for
        these. They are also shared infrastructure on a multi-tenant server, and
        re-push restores them by ID, so skipping them is correct as well as required.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = _delete_urls(mock_ctx)
        for rt in ("Medication", "Location", "Practitioner", "Organization"):
            assert not any(f"/{rt}?" in u for u in urls), f"{rt} has no patient link — delete would be unscoped"

    async def test_adverse_event_uses_subject_not_patient(self):
        """AdverseEvent is the one type where `patient=` is a 400 on HAPI."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        adverse = [u for u in _delete_urls(mock_ctx) if "/AdverseEvent?" in u]
        assert len(adverse) == 1
        assert "subject=" in adverse[0]
        assert "patient=" not in adverse[0]

    async def test_batches_ids_into_one_request_per_type(self):
        """IDs are OR'd in a single param so the sweep is 19 requests, not 19*N."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1", "p2", "p3"])

        condition = [u for u in _delete_urls(mock_ctx) if "/Condition?" in u]
        assert len(condition) == 1, "one request per type expected for a small ID list"
        assert "p1,p2,p3" in condition[0]

    async def test_chunks_large_id_lists(self):
        """A 460-patient job must not produce a multi-kilobyte URL."""
        ids = [f"p{i}" for i in range(250)]
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=ids)

        condition = [u for u in _delete_urls(mock_ctx) if "/Condition?" in u]
        assert len(condition) > 1, "large ID list was not chunked"
        for url in condition:
            assert len(url) < 2000, f"URL too long for a GET/DELETE line: {len(url)}"
        # Every ID must appear somewhere — chunking must not silently drop any.
        joined = " ".join(condition)
        for pid in ids:
            assert f"{pid}," in joined or f"{pid} " in joined or joined.endswith(pid) or f"{pid}&" in joined, (
                f"{pid} dropped by chunking"
            )

    async def test_no_patient_ids_issues_no_deletes(self):
        """An empty gather must not fall through to an unscoped sweep."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=[])

        assert mock_ctx.delete.await_count == 0

    async def test_carries_auth_headers(self):
        headers = {"Authorization": "Bearer tok-1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"], auth_headers=headers)

        for call in mock_ctx.delete.await_args_list:
            assert call.kwargs.get("headers") == headers, "scoped delete went out unauthenticated"

    async def test_raises_on_unauthorized(self):
        """Same fail-loud rule as the full wipe: a 401 must not report success.

        Silently failing here leaves the prior run's resources attached to the
        patients this job is about to evaluate, which corrupts its populations.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._mock_client(mock_httpx, response=_make_response(401, {}))
            with pytest.raises(RuntimeError, match="Not authorized"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_raises_after_consecutive_transport_failures(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.delete = AsyncMock(side_effect=httpx.TimeoutException("slow delete"))
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(RuntimeError, match="FHIR server unreachable"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.delete.await_count == 3

    async def test_tolerates_404_on_absent_types(self):
        """A server that doesn't stock a type must not fail the whole wipe."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx, response=_make_response(404, {}))
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.delete.await_count > 0

    async def test_falls_back_to_one_by_one_when_multiple_delete_is_disabled(self):
        """A server with allow_multiple_delete=false must still get wiped.

        This is the deployment the whole feature targets: our own containers set
        `hapi.fhir.allow_multiple_delete=true`, but a shared remote MCS may not.
        HAPI then rejects a conditional delete whose search matches more than one
        resource. Without a fallback the wipe logs a warning, deletes nothing, and
        the prior run's resources stay attached to the patients this job is about
        to evaluate — silently corrupting its populations.
        """
        # 412 = HAPI's "search matched multiple resources and multiple delete is
        # disabled" response.
        rejected = _make_response(412, {})
        found = _make_response(
            200,
            {
                "resourceType": "Bundle",
                "entry": [{"resource": {"resourceType": "Condition", "id": "cond-1"}}],
            },
        )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()

            # Conditional delete (has a "?") is rejected; delete by id succeeds.
            async def _delete(url, **kwargs):
                return rejected if "?" in url else _make_response(200, {})

            mock_ctx.delete = AsyncMock(side_effect=_delete)
            mock_ctx.get = AsyncMock(return_value=found)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        # The fallback searched...
        assert mock_ctx.get.await_count > 0, "no fallback search was issued"
        # ...and every search it issued stayed scoped to the patient.
        for call in mock_ctx.get.await_args_list:
            url = call.args[0]
            assert any(f"&{p}=" in url for p in ("patient", "subject", "_id")), f"fallback search was unscoped: {url}"
        # ...and deleted by id.
        by_id = [u for u in _delete_urls(mock_ctx) if "?" not in u]
        assert any("/Condition/cond-1" in u for u in by_id), "fallback never deleted the matched resource"

    async def test_fallback_search_stays_authenticated(self):
        """The fallback must carry credentials or it 401s and silently no-ops."""
        headers = {"Authorization": "Bearer tok-1"}
        found = _make_response(200, {"resourceType": "Bundle", "entry": []})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = AsyncMock()
            mock_ctx.delete = AsyncMock(return_value=_make_response(412, {}))
            mock_ctx.get = AsyncMock(return_value=found)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"], auth_headers=headers)

        assert mock_ctx.get.await_count > 0
        for call in mock_ctx.get.await_args_list:
            assert call.kwargs.get("headers") == headers, "fallback search went out unauthenticated"


# ---------------------------------------------------------------------------
# Scoped-wipe reference conflicts (issue #458)
# ---------------------------------------------------------------------------


class TestScopedWipeReferenceConflicts:
    """A 409 during the scoped wipe must never be reported as a successful wipe.

    Issue #458. This repo sets `hapi.fhir.enforce_referential_integrity_on_write=false`
    everywhere but never sets the *delete* equivalent, which therefore defaults to
    `true`: HAPI answers 409 to `DELETE {Type}?patient=` while any resource still
    references a match. The sweep absorbed that and moved on, so the resource
    survived a wipe that logged "Scoped wipe complete" — stale data the next
    evaluation of that patient can consume, with no error signal. ADR-012's
    correctness argument does not hold for any resource with a surviving referrer.

    Three behaviours are pinned here, in the order the fix applies them:
    ordered retry (clears the acyclic cases), one transaction Bundle (clears
    reference *cycles*, which no ordering can), then a loud failure.
    """

    def _client(self, mock_httpx, *, delete, get=None, post=None):
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(side_effect=delete)
        mock_ctx.get = AsyncMock(side_effect=get) if get else AsyncMock(return_value=_make_response(200, {}))
        mock_ctx.post = AsyncMock(side_effect=post) if post else AsyncMock(return_value=_make_response(200, {}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_ctx

    @staticmethod
    def _conflict(blocker: str) -> httpx.Response:
        """HAPI's actual 409 body, which names only the first referrer it finds."""
        return _make_response(
            409,
            {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": (
                            "Unable to delete resource because at least one resource has a reference "
                            f"to this resource. First reference found was resource {blocker}"
                        ),
                    }
                ],
            },
        )

    @staticmethod
    def _found(resource_type: str, resource_id: str) -> httpx.Response:
        return _make_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": {"resourceType": resource_type, "id": resource_id}}],
            },
        )

    _EMPTY = {"resourceType": "Bundle", "type": "searchset", "entry": []}

    async def test_a_type_blocked_by_a_later_referrer_is_retried(self):
        """The acyclic case from the issue body, which ordering alone leaves behind.

        `Procedure` references `Encounter` and sits *after* it in the sweep, so
        `DELETE Encounter?patient=` 409s on the first pass. Once the Procedure is
        gone the same delete succeeds — but only if something re-issues it. The
        original sweep never did, and the Encounter survived.
        """
        attempts = {"encounter": 0}

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                attempts["encounter"] += 1
                if attempts["encounter"] == 1:
                    return self._conflict("Procedure/pr-1 in path Procedure.encounter")
            return _make_response(200, {})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert attempts["encounter"] == 2, "a 409 was absorbed — the Encounter survives the wipe"
        # The retry re-issues only what conflicted, not the whole sweep again.
        condition = [u for u in _delete_urls(mock_ctx) if "/Condition?" in u]
        assert len(condition) == 1, "the retry pass re-swept types that had already succeeded"

    async def test_a_reference_cycle_is_cleared_by_one_transaction_bundle(self):
        """`Condition` <-> `Encounter` is a type-level cycle; no order can break it.

        Measured on the CMS connectathon server against unmodified MADiE CMS506
        data: `Encounter.reasonReference -> Condition` and
        `Condition.encounter -> Encounter` pin each other, so whichever is deleted
        first 409s and both survive. Swapping them only moves which one fails.
        HAPI evaluates referential integrity at commit, so a transaction Bundle
        carrying both DELETEs removes the pair atomically.
        """
        posted: list[dict] = []

        async def _delete(url, **kwargs):
            if "/Condition?" in url or "/Encounter?" in url:
                return self._conflict("the other half of the cycle")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if posted:  # the transaction cleared them
                return _make_response(200, self._EMPTY)
            if "/Condition?" in url:
                return self._found("Condition", "c-1")
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert len(posted) == 1, f"expected exactly one transaction Bundle, got {len(posted)}"
        bundle = posted[0]
        assert bundle["type"] == "transaction", (
            "a batch Bundle applies entries independently, so each half of the cycle "
            "still 409s — only a transaction commits them together"
        )
        urls = [e["request"]["url"] for e in bundle["entry"]]
        assert [e["request"]["method"] for e in bundle["entry"]] == ["DELETE"] * len(urls)
        assert "Condition/c-1" in urls and "Encounter/e-1" in urls, (
            f"both halves of the cycle must ride in the same transaction: {urls}"
        )

    async def test_raises_when_the_transaction_cannot_clear_the_conflicts(self):
        """The fail-loud end of the chain, and the whole point of the issue.

        A referrer outside the wipe's scope (a summary MeasureReport, a type not
        in `_PATIENT_SCOPED_TYPES` — see #457) pins a resource that the wipe owns
        and cannot free. Reporting success there leaves the next evaluation of
        this patient reading data the job believes it deleted.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("MeasureReport/summary-1 in path MeasureReport.evaluatedResource")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError) as exc:
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        message = str(exc.value)
        assert "Encounter/e-1" in message, f"the error must name what survived: {message!r}"
        assert "mcs.example.org" in message or "target" in message.lower()

    async def test_retry_stops_once_a_pass_makes_no_progress(self):
        """Bounded, and bounded by *progress* rather than a fixed pass count.

        A cycle 409s identically on every pass. Retrying it until some attempt
        limit runs out would multiply every stuck type by that limit for nothing
        — and on a 460-patient job the sweep is already ~250 requests.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        encounter = [u for u in _delete_urls(mock_ctx) if "/Encounter?" in u]
        assert len(encounter) == 2, (
            f"expected one sweep plus one retry that makes no progress, got {len(encounter)} attempts"
        )

    async def test_a_clean_wipe_issues_no_extra_requests(self):
        """The conflict machinery must not cost anything when nothing conflicts.

        Enumerating every type to check what survived would double the request
        count of every job on the happy path. Enumeration is only allowed for the
        types that actually answered 409.
        """

        async def _delete(url, **kwargs):
            return _make_response(200, {})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.delete.await_count == len(_PATIENT_SCOPED_TYPES)
        assert mock_ctx.get.await_count == 0, "no conflict, so nothing should have been enumerated"
        assert mock_ctx.post.await_count == 0, "no conflict, so no transaction Bundle should be needed"

    async def test_a_409_in_the_per_resource_fallback_is_not_swallowed(self):
        """The second place a 409 disappeared: the `allow_multiple_delete` fallback.

        `_delete_all_of_type` wrapped each DELETE in `except httpx.HTTPError: pass`
        — and a 409 does not raise from httpx at all, so it was not even reached.
        On a server without conditional delete, every conflict in the sweep took
        this path and vanished.
        """
        posted: list[dict] = []

        async def _delete(url, **kwargs):
            if "/Condition?" in url:
                return _make_response(412, {})  # multiple delete disabled
            if url.endswith("/Condition/c-1"):
                return self._conflict("Encounter/e-1 in path Encounter.reasonReference")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Condition?" in url and not posted:
                return self._found("Condition", "c-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert len(posted) == 1, "a conflict raised by the per-resource fallback was absorbed"
        assert [e["request"]["url"] for e in posted[0]["entry"]] == ["Condition/c-1"]

    async def test_conflict_recovery_stays_authenticated_and_scoped(self):
        """Both new requests carry credentials, and the enumeration stays scoped.

        An unauthenticated enumeration 401s and reports nothing left to clear,
        which is the silent success this issue is about. An *unscoped* one would
        list other participants' resources and feed them to a DELETE transaction
        — turning #392's safety feature into the full wipe it exists to prevent.
        """
        headers = {"Authorization": "Bearer tok-1"}
        posted_headers: list[dict] = []
        cleared: list[bool] = []

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url and not cleared:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted_headers.append(kwargs.get("headers") or {})
            cleared.append(True)
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"], auth_headers=headers)

        assert mock_ctx.get.await_count > 0
        for call in mock_ctx.get.await_args_list:
            url = call.args[0]
            assert call.kwargs.get("headers") == headers, "conflict enumeration went out unauthenticated"
            assert any(f"{p}=" in url for p in ("patient", "subject", "_id")), (
                f"conflict enumeration was unscoped — it would list other tenants' resources: {url}"
            )
        assert posted_headers, "no transaction Bundle was posted"
        assert posted_headers[0].get("Authorization") == "Bearer tok-1"

    # -- recovery outcomes ---------------------------------------------------

    async def test_raises_when_the_conflict_enumeration_is_unauthorized(self):
        """A 401 on the enumeration must not read as "nothing left to clear".

        The enumeration is also the verification step, so an unauthenticated or
        expired-token GET that answered "no matches" would let the wipe report
        success over data it never even looked at — the same silent success #458
        exists to remove, one layer down.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            return _make_response(401, {"resourceType": "OperationOutcome"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError, match="Not authorized to enumerate"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.post.await_count == 0, "a DELETE transaction went out against an unreadable server"

    async def test_conflicts_that_clear_themselves_need_no_transaction(self):
        """The 409 was real, but the referrer was gone by the time recovery ran.

        A later delete in the same pass freed it, so there is nothing to put in a
        transaction and nothing to fail over. The wipe must complete quietly
        rather than posting an empty Bundle or raising on an empty leftover set.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            return _make_response(200, self._EMPTY)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.post.await_count == 0, "an empty leftover set still posted a transaction Bundle"

    async def test_a_transaction_that_errors_but_clears_the_data_is_not_a_failure(self):
        """Re-reading the server decides, not the transaction's status code.

        HAPI can answer non-2xx on a transaction whose deletes nonetheless landed,
        and a concurrent job can clear the leftovers regardless. Failing the job on
        the status code alone would abort evaluations whose data is genuinely gone.
        """
        posted: list[dict] = []

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url and not posted:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(400, {"resourceType": "OperationOutcome"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert len(posted) == 1

    async def test_a_transaction_transport_failure_is_named_in_the_error(self):
        """The operator needs to know the recovery never reached the server.

        "could not delete" plus nothing else reads as a referential-integrity
        problem to chase on the target; a connect failure is a different fix
        entirely.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError) as exc:
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        message = str(exc.value)
        assert "transaction request failed" in message, message
        assert "ConnectError" in message, f"the transport failure must be named: {message!r}"

    async def test_the_error_carries_the_transactions_own_diagnostics(self):
        """HAPI's account of what is pinning the resource is the actionable part.

        `_conflict_diagnostics` exists to lift "First reference found was resource
        X in path Y" out of the OperationOutcome; if it never reaches the
        RuntimeError, the operator is left to go find it by hand.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            return self._conflict("MeasureReport/summary-1 in path MeasureReport.evaluatedResource")

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError) as exc:
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        message = str(exc.value)
        assert "HTTP 409" in message, message
        assert "MeasureReport/summary-1" in message, f"the referrer HAPI named is missing: {message!r}"

    async def test_conflict_diagnostics_are_redacted(self):
        """HAPI echoes request context into diagnostics, credentials included.

        The 409 body goes into a log line and into an operator-facing error
        message, so it travels through `redact_outcome` first. A wipe that leaks a
        participant's bearer token into the job log is a worse bug than the one
        being reported.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            return _make_response(
                409,
                {
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "diagnostics": "Rejected. Authorization: Bearer sekret-abc123"}],
                },
            )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError) as exc:
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        message = str(exc.value)
        assert "sekret-abc123" not in message, f"a credential reached the operator-facing error: {message!r}"
        assert "[redacted]" in message, message

    async def test_a_non_json_conflict_body_does_not_break_the_wipe(self):
        """A proxy's HTML 409 must degrade to the status code, not raise.

        The wipe runs against servers Lenny does not control, and a gateway in
        front of one can answer 409 with an HTML body. Letting `.json()` escape
        would turn a recoverable reference conflict into an opaque crash.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return httpx.Response(409, text="<html>Conflict</html>", request=_DUMMY_REQUEST)
            return _make_response(200, {})

        async def _get(url, **kwargs):
            return _make_response(200, self._EMPTY)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        # Still recognised as a conflict and still retried, despite the unparseable body.
        assert len([u for u in _delete_urls(mock_ctx) if "/Encounter?" in u]) == 2

    # -- enumeration mechanics ----------------------------------------------

    async def test_conflict_enumeration_follows_same_origin_pagination(self):
        """Leftovers past the first page must ride in the same transaction.

        A partial enumeration would post a Bundle missing half of a cycle, which
        is exactly the split the transaction exists to avoid — and would then
        raise on the half it never tried to delete.
        """
        posted: list[dict] = []
        page2 = "https://mcs.example.org/fhir?_getpages=abc&_getpagesoffset=200"

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if posted:
                return _make_response(200, self._EMPTY)
            if url == page2:
                return self._found("Encounter", "e-2")
            if "/Encounter?" in url:
                return _make_response(
                    200,
                    {
                        "resourceType": "Bundle",
                        "type": "searchset",
                        "entry": [{"resource": {"resourceType": "Encounter", "id": "e-1"}}],
                        "link": [{"relation": "next", "url": page2}],
                    },
                )
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = [e["request"]["url"] for e in posted[0]["entry"]]
        assert urls == ["Encounter/e-1", "Encounter/e-2"], f"the second page was dropped: {urls}"

    async def test_conflict_enumeration_rejects_a_cross_origin_next_link(self):
        """The SSRF guard, on a code path that did not exist before #458.

        A hostile or misconfigured server can hand back a next link pointing at an
        internal host. The enumeration follows next links, so it needs the same
        same-origin check the rest of the client applies.
        """
        gets: list[str] = []
        evil = "https://evil.example.net/fhir?_getpages=abc"

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            gets.append(url)
            if "/Encounter?" in url:
                return _make_response(
                    200,
                    {
                        "resourceType": "Bundle",
                        "type": "searchset",
                        "entry": [{"resource": {"resourceType": "Encounter", "id": "e-1"}}],
                        "link": [{"relation": "next", "url": evil}],
                    },
                )
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert gets, "nothing was enumerated"
        assert not any("evil.example.net" in u for u in gets), f"followed a cross-origin next link: {gets}"

    async def test_conflict_enumeration_rejects_ids_that_are_not_bare_ids(self):
        """Server-supplied ids become the `url` of a DELETE entry, so they are input.

        `Encounter/../Patient/bystander` as a transaction entry url is a delete
        aimed at another patient — the cross-tenant damage #392 exists to prevent,
        reached through the recovery path this diff added.
        """
        posted: list[dict] = []

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url and not posted:
                return _make_response(
                    200,
                    {
                        "resourceType": "Bundle",
                        "type": "searchset",
                        "entry": [
                            {"resource": {"resourceType": "Encounter", "id": "../Patient/bystander"}},
                            {"resource": {"resourceType": "Encounter", "id": "nested/id"}},
                            {"resource": None},
                            {"resource": {"resourceType": "Encounter"}},
                            {"resource": {"resourceType": "Encounter", "id": "e-ok"}},
                        ],
                    },
                )
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = [e["request"]["url"] for e in posted[0]["entry"]]
        assert urls == ["Encounter/e-ok"], f"a malformed id reached the DELETE transaction: {urls}"

    async def test_conflict_enumeration_is_bounded_by_a_page_cap(self):
        """A server that pages forever must not hang the job — and must not pass.

        HAPI's `next` link is server-generated and a buggy one can be a fixed
        point. The cap bounds the work, and since the pre-landing review the cap
        also fails the wipe: a truncated view of the leftovers is not evidence
        they were cleared, so enumeration stops at the budget and raises rather
        than carrying a partial set into a transaction.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return _make_response(
                    200,
                    {
                        "resourceType": "Bundle",
                        "type": "searchset",
                        "entry": [{"resource": {"resourceType": "Encounter", "id": "e-1"}}],
                        "link": [{"relation": "next", "url": "https://mcs.example.org/fhir/Encounter?_getpages=1"}],
                    },
                )
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.get.await_count == _WIPE_ENUMERATE_MAX_PAGES, (
            f"pagination was not bounded: {mock_ctx.get.await_count} GETs"
        )
        assert mock_ctx.post.await_count == 0, "a truncated enumeration must not be carried into a DELETE transaction"

    # -- retry loop ---------------------------------------------------------

    async def test_the_retry_loop_keeps_passing_while_it_makes_progress(self):
        """More than one retry, when each pass frees something.

        A chain — A pinned by B, B pinned by C — needs as many passes as the
        chain is deep. A fix that retried exactly once would clear the two-type
        case in the issue body and leave anything longer behind.
        """
        attempts: dict[str, int] = {"Encounter": 0, "Condition": 0}

        async def _delete(url, **kwargs):
            for rt in attempts:
                if f"/{rt}?" in url:
                    attempts[rt] += 1
                    # Condition frees on pass 2, Encounter on pass 3.
                    limit = 1 if rt == "Condition" else 2
                    if attempts[rt] <= limit:
                        return self._conflict("the next link in the chain")
            return _make_response(200, {})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert attempts == {"Encounter": 3, "Condition": 2}, f"the retry loop stopped early: {attempts}"
        assert mock_ctx.post.await_count == 0, "the retry cleared everything, so no transaction was needed"
        assert mock_ctx.get.await_count == 0, "nothing was left blocked, so nothing should be enumerated"

    async def test_a_stranded_patient_rides_in_the_recovery_transaction(self):
        """The Patient itself is the resource an operator actually notices.

        It is last in the sweep and cannot be deleted while any clinical resource
        points at it, so a conflict anywhere earlier strands it. The next job
        re-pushes that id and evaluates against a merge of old and new data.
        """
        posted: list[dict] = []

        async def _delete(url, **kwargs):
            if "/Patient?" in url:
                return self._conflict("Condition/c-1 in path Condition.subject")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Patient?" in url and not posted:
                return self._found("Patient", "p1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert [e["request"]["url"] for e in posted[0]["entry"]] == ["Patient/p1"]

    # -- an unreadable target server must never read as a clean wipe (review) ---

    async def test_raises_when_the_conflict_enumeration_is_unreadable(self):
        """A 5xx on the enumeration must not read as "nothing left to clear".

        Found in pre-landing review by two specialists and the coverage audit.
        `_search_scoped_refs` raised only on 401/403; every other non-200 returned
        the refs gathered so far — usually none — and both callers read an empty
        list as "the leftovers were freed". So one 500 after a delete had already
        answered 409 restored #458's exact silent success, one layer down. 5xx is
        ordinary on a shared server nobody owns.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            return _make_response(503, {"resourceType": "OperationOutcome"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError, match="[Cc]ould not enumerate"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_an_unreadable_verification_after_a_failed_transaction_is_not_success(self):
        """The verification read is the ONLY evidence the transaction worked.

        The POST's status code is deliberately not trusted, so a verification that
        cannot be read leaves the wipe with no evidence at all — which must fail,
        not pass.
        """
        posted: list[dict] = []

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if posted:
                return _make_response(503, {"resourceType": "OperationOutcome"})
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(500, {"resourceType": "OperationOutcome"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_a_non_json_enumeration_body_fails_loudly_not_with_a_decode_error(self):
        """A gateway answering 200 with HTML must not crash the wipe with JSONDecodeError.

        Every other failure in this path becomes a RuntimeError carrying
        operator-facing context. A bare decode error escaping from inside a wipe
        tells the operator nothing about which server or which resource.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            return httpx.Response(200, text="<html>gateway</html>", request=_DUMMY_REQUEST)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError, match="[Cc]ould not enumerate"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_a_truncated_enumeration_fails_loudly(self):
        """Hitting the page cap with more pages outstanding means the set is unknown.

        Returning a partial set would delete some leftovers and then report success
        on the strength of a verification that never saw the rest.
        """

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        page = {"n": 0}

        async def _get(url, **kwargs):
            if "/Encounter?" not in url:
                return _make_response(200, self._EMPTY)
            page["n"] += 1
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "Encounter", "id": f"e-{page['n']}"}}],
                    "link": [
                        {"relation": "next", "url": f"https://mcs.example.org/fhir/Encounter?_getpages={page['n']}"}
                    ],
                },
            )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError, match="too many|truncat|could not enumerate"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_a_blocked_target_that_times_out_on_retry_is_not_reported_as_wiped(self):
        """A transport failure must not quietly drop a target already known blocked.

        `except httpx.HTTPError` counted the failure and moved on without putting
        the target back in `blocked`, so a 409'd target whose retry timed out left
        `pending` and was never enumerated or verified — a "Scoped wipe complete"
        over a resource the sweep had already been told it could not delete.
        """
        attempts = {"n": 0}

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return self._conflict("Condition/c-1")
                raise httpx.TimeoutException("slow delete")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert mock_ctx.get.await_count > 0, "the timed-out target was dropped without being verified"

    async def test_the_per_resource_fallback_raises_on_unauthorized(self):
        """The 412 fallback checked only 409, so a 401 there deleted nothing silently.

        The enumeration GET in the same function already raises on 401/403 with
        "Refusing to report a successful wipe". Reachable on exactly the server the
        fallback exists for: one that refuses conditional delete and then refuses
        the per-resource deletes under a read-only scope.
        """

        async def _delete(url, **kwargs):
            if "?" in url:
                return _make_response(412, {})
            return _make_response(401, {})

        async def _get(url, **kwargs):
            return self._found("Condition", "c-1")

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get)
            with pytest.raises(RuntimeError, match="Not authorized"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

    async def test_the_id_guard_admits_only_bare_fhir_ids(self):
        """A denylist is the wrong shape for an id that becomes a DELETE url.

        FHIR ids are an allowlist (`[A-Za-z0-9.\\-]{1,64}`). The old two-substring
        check admitted `?patient=...` — which turns a targeted entry into a
        *conditional* delete scoped to someone else's patient — and
        `?_cascade=delete`, which is exactly the outward reference-following blast
        radius ADR-018 argues must never be used on a shared server.
        """
        posted: list[dict] = []
        hostile = ["?patient=other-tenant", "e-1?_cascade=delete", "%2e%2e%2fMeasure%2fCMS130", "ok-1"]

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url and not posted:
                return _make_response(
                    200,
                    {
                        "resourceType": "Bundle",
                        "type": "searchset",
                        "entry": [{"resource": {"resourceType": "Encounter", "id": i}} for i in hostile],
                    },
                )
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = [e["request"]["url"] for e in posted[0]["entry"]]
        assert urls == ["Encounter/ok-1"], f"a non-bare id reached a DELETE url: {urls}"

    async def test_repeated_ids_across_pages_are_not_deleted_twice(self):
        """A server repeating an entry across pages must not produce a duplicate op.

        `refs` was appended unconditionally, so a repeated entry became two DELETE
        entries for one resource in a single transaction — which a real HAPI may
        reject outright, turning a bounded enumeration into a failed recovery.
        """
        posted: list[dict] = []
        pages = {"n": 0}

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" not in url or posted:
                return _make_response(200, self._EMPTY)
            pages["n"] += 1
            body = {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": {"resourceType": "Encounter", "id": "e-1"}}],
            }
            if pages["n"] == 1:
                body["link"] = [{"relation": "next", "url": "https://mcs.example.org/fhir/Encounter?_getpages=1"}]
            return _make_response(200, body)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        urls = [e["request"]["url"] for e in posted[0]["entry"]]
        assert urls == ["Encounter/e-1"], f"duplicate DELETE entries for one resource: {urls}"

    async def test_the_recovery_transaction_is_posted_to_the_server_base(self):
        """A transaction Bundle is only valid against the base, not a type endpoint.

        No test asserted the POST target, so a regression to `{base}/Bundle` would
        stay green here and only surface against a real HAPI, in the rare path.
        """
        urls: list[str] = []

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url and not urls:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            urls.append(url)
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert urls == ["https://mcs.example.org/fhir"], f"transaction posted to the wrong target: {urls}"

    async def test_each_id_chunk_gets_its_own_transaction(self):
        """The conflict path is chunk-shaped, and the Bundle is bounded per chunk.

        One transaction for every chunk, not one for the whole job: `refs`
        accumulated across every target, and `targets` is 23 types x ceil(N/50)
        chunks, so a 460-patient job could hand a single POST hundreds of
        thousands of DELETE entries — a body whose size the remote server decides.

        Splitting by chunk is safe for the thing the transaction exists for:
        `Condition.encounter` and `Encounter.reasonReference` both point within one
        patient, and a patient never spans two chunks, so a reference cycle is
        always inside one chunk's Bundle. Verified by
        `test_a_cycle_inside_one_chunk_still_commits_together` below.
        """
        posted: list[dict] = []
        ids = [f"p{i}" for i in range(_WIPE_ID_CHUNK_SIZE + 5)]

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url and not posted:
                # One leftover per chunk, distinguished by the first id in the chunk.
                marker = "a" if "p0," in url else "b"
                return self._found("Encounter", f"e-{marker}")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=ids)

        assert len(posted) == 2, f"expected one transaction per id chunk, got {len(posted)}"
        per_txn = sorted(sorted(e["request"]["url"] for e in b["entry"]) for b in posted)
        assert per_txn == [["Encounter/e-a"], ["Encounter/e-b"]], per_txn

    async def test_a_cycle_inside_one_chunk_still_commits_together(self):
        """Bounding per chunk must not split the pair the transaction exists for.

        This is the load-bearing claim behind chunk-grouping: both halves of a
        `Condition` <-> `Encounter` cycle belong to the same patient, so they land
        in the same chunk and therefore the same Bundle. If that ever stopped
        being true, cycles would stop clearing and #458 would be back.
        """
        posted: list[dict] = []
        ids = [f"p{i}" for i in range(_WIPE_ID_CHUNK_SIZE + 5)]

        async def _delete(url, **kwargs):
            if "/Condition?" in url or "/Encounter?" in url:
                return self._conflict("the other half of the cycle")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if posted:
                return _make_response(200, self._EMPTY)
            # Only the first chunk holds the cycle.
            if "p0," not in url:
                return _make_response(200, self._EMPTY)
            if "/Condition?" in url:
                return self._found("Condition", "c-1")
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=ids)

        assert len(posted) == 1, f"one chunk held the cycle, so one transaction: {len(posted)}"
        urls = sorted(e["request"]["url"] for e in posted[0]["entry"])
        assert urls == ["Condition/c-1", "Encounter/e-1"], f"the two halves of the cycle must commit together: {urls}"

    async def test_an_oversized_conflict_set_fails_loudly_instead_of_posting(self):
        """A wipe this stuck is broken; the cap must raise, not POST whatever came back.

        The remote decides how much arrives here, so without a ceiling a server
        that refuses every conditional delete can steer Lenny into a multi-MB
        DELETE transaction against a shared box it does not own.
        """
        posted: list[dict] = []
        many = [{"resource": {"resourceType": "Encounter", "id": f"e-{i}"}} for i in range(_WIPE_MAX_TXN_ENTRIES + 1)]

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return self._conflict("Condition/c-1")
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": many})
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            posted.append(kwargs.get("json"))
            return _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError, match="exceeds"):
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert posted == [], "an oversized conflict set must not be POSTed at all"

    async def test_a_conflict_body_without_diagnostics_still_names_the_status(self):
        """HAPI does emit an OperationOutcome carrying only severity/code.

        The join then returned "", and the operator-facing error composed
        "The transaction answered HTTP 400: ." — a dangling colon where the
        actionable text was supposed to be.
        """
        bare = _make_response(
            409, {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "conflict"}]}
        )

        async def _delete(url, **kwargs):
            if "/Encounter?" in url:
                return bare
            return _make_response(200, {})

        async def _get(url, **kwargs):
            if "/Encounter?" in url:
                return self._found("Encounter", "e-1")
            return _make_response(200, self._EMPTY)

        async def _post(url, **kwargs):
            return _make_response(
                400, {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "processing"}]}
            )

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._client(mock_httpx, delete=_delete, get=_get, post=_post)
            with pytest.raises(RuntimeError) as exc:
                await wipe_patients_by_id(base_url="https://mcs.example.org/fhir", patient_ids=["p1"])

        assert ": ." not in str(exc.value), f"empty diagnostics left a dangling colon: {exc.value}"
        assert "HTTP 400" in str(exc.value)


# ---------------------------------------------------------------------------
# wipe_measure_definitions (issue #397)
# ---------------------------------------------------------------------------


class TestWipeMeasureDefinitions:
    """The admin 'wipe measure engine' primitive, scoped to a caller-supplied server.

    Issue #397: this read `settings.MEASURE_ENGINE_URL` directly, so an admin
    connected to a remote MCS who clicked the control wiped Lenny's LOCAL engine
    and got a success response, while the server they were looking at was
    untouched. The inverse is the real hazard — a naive fix makes it capable of
    wiping a shared remote server — so `base_url` is required and the callers own
    the read-only guard.
    """

    def _mock_client(self, mock_httpx, response=None):
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=response or _make_response(200, {}))
        mock_ctx.get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "entry": []}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_ctx

    async def test_base_url_is_required(self):
        """No env-var default. A default is how this bug class stayed invisible."""
        with pytest.raises(TypeError):
            await wipe_measure_definitions()  # type: ignore[call-arg]

    async def test_targets_the_given_server(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir")

        urls = [call.args[0] for call in mock_ctx.delete.await_args_list]
        assert urls, "no deletes issued"
        for url in urls:
            assert url.startswith("https://mcs.example.org/fhir/"), f"wrong server: {url}"

    async def test_covers_the_definition_types_and_not_clinical_data(self):
        """Definitions only — this control must not delete patients."""
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir")

        urls = [call.args[0] for call in mock_ctx.delete.await_args_list]
        for rt in ("Library", "Measure", "ValueSet", "CodeSystem", "ConceptMap"):
            assert any(f"/{rt}?" in u for u in urls), f"{rt} not wiped"
        for rt in ("Patient", "Encounter", "Condition", "Observation"):
            assert not any(f"/{rt}?" in u for u in urls), f"{rt} must not be touched by a definitions wipe"

    async def test_carries_auth_headers(self):
        """A remote MCS rejects every DELETE without credentials."""
        headers = {"Authorization": "Bearer tok-1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx)
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir", auth_headers=headers)

        for call in mock_ctx.delete.await_args_list:
            assert call.kwargs.get("headers") == headers, "definitions delete went out unauthenticated"

    async def test_raises_on_unauthorized(self):
        """A 401 must abort, naming the wipe — same rule as the other two wipes.

        Without the explicit check this still failed, but via the fallback sweep's
        "not authorized to enumerate" error, which names the wrong operation.
        """
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            self._mock_client(mock_httpx, response=_make_response(401, {}))
            with pytest.raises(RuntimeError, match="Not authorized to wipe"):
                await wipe_measure_definitions(base_url="https://mcs.example.org/fhir")

    async def test_fallback_sweep_carries_auth_headers(self):
        """When conditional delete is unsupported, the per-resource sweep stays authenticated."""
        headers = {"Authorization": "Bearer tok-1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            mock_ctx = self._mock_client(mock_httpx, response=_make_response(405, {}))
            await wipe_measure_definitions(base_url="https://mcs.example.org/fhir", auth_headers=headers)

        assert mock_ctx.get.await_count > 0, "fallback sweep never ran"
        for call in mock_ctx.get.await_args_list:
            assert call.kwargs.get("headers") == headers, "fallback GET went out unauthenticated"


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
    """test_connection raises FhirOperationError when the server is unreachable."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://bad-server/fhir")
        assert exc_info.value.status_code is None
        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


async def test_fhir_test_connection_401():
    """test_connection raises FhirOperationError with status_code=401."""
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

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://example.com/fhir")
        assert exc_info.value.status_code == 401


async def test_fhir_test_connection_500():
    """test_connection raises FhirOperationError with status_code=500."""
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

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://example.com/fhir")
        assert exc_info.value.status_code == 500


async def test_fhir_test_connection_timeout():
    """test_connection raises FhirOperationError wrapping a timeout."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError) as exc_info:
            await fhir_test_connection("https://slow-server/fhir")
        assert exc_info.value.status_code is None
        assert isinstance(exc_info.value.__cause__, httpx.TimeoutException)


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

        result = await resolve_evaluated_resource("Patient/p1", "http://mcs/fhir")

    assert result == resource


async def test_resolve_evaluated_resource_requires_base_url():
    """The env-var fallback is gone (issue #397).

    It defaulted to settings.MEASURE_ENGINE_URL with no credentials, which only
    ever worked for a local unauthenticated HAPI. Every caller now names the server.
    """
    with pytest.raises(TypeError):
        await resolve_evaluated_resource("Patient/p1")  # type: ignore[call-arg]


async def test_resolve_evaluated_resource_targets_the_given_server():
    resource = {"resourceType": "Patient", "id": "p1"}
    seen: dict[str, object] = {}

    async def _get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return _make_response(200, resource)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await resolve_evaluated_resource(
            "Patient/p1", "https://mcs.example.org/fhir", {"Authorization": "Bearer tok-r"}
        )

    assert seen["url"] == "https://mcs.example.org/fhir/Patient/p1"
    assert seen["headers"] == {"Authorization": "Bearer tok-r"}


# ---------------------------------------------------------------------------
# snapshot_evaluated_resources
# ---------------------------------------------------------------------------


async def test_snapshot_evaluated_resources_returns_resolved_list():
    """Each evaluatedResource reference is resolved and returned in order."""
    from app.services.fhir_client import snapshot_evaluated_resources

    measure_report = {
        "resourceType": "MeasureReport",
        "evaluatedResource": [
            {"reference": "Patient/p1"},
            {"reference": "Encounter/e1"},
        ],
    }
    fake_resources = {
        "Patient/p1": {"resourceType": "Patient", "id": "p1"},
        "Encounter/e1": {"resourceType": "Encounter", "id": "e1"},
    }

    async def fake_resolve(ref, base_url=None, auth_headers=None):
        return fake_resources[ref]

    with patch("app.services.fhir_client.resolve_evaluated_resource", side_effect=fake_resolve):
        result = await snapshot_evaluated_resources(measure_report)

    assert result == [fake_resources["Patient/p1"], fake_resources["Encounter/e1"]]


async def test_snapshot_evaluated_resources_skips_failed_refs():
    """Per-reference failures are logged and skipped, not raised — partial snapshots
    are still useful and the caller has already persisted the MeasureReport."""
    from app.services.fhir_client import snapshot_evaluated_resources

    measure_report = {
        "evaluatedResource": [
            {"reference": "Patient/p1"},
            {"reference": "Encounter/e1"},
            {"reference": "Condition/c1"},
        ],
    }

    async def fake_resolve(ref, base_url=None, auth_headers=None):
        if ref == "Encounter/e1":
            raise RuntimeError("404 not found")
        return {"resourceType": ref.split("/")[0], "id": ref.split("/")[1]}

    with patch("app.services.fhir_client.resolve_evaluated_resource", side_effect=fake_resolve):
        result = await snapshot_evaluated_resources(measure_report)

    assert len(result) == 2
    assert {r["id"] for r in result} == {"p1", "c1"}


async def test_snapshot_evaluated_resources_returns_none_when_no_refs():
    """No evaluatedResource entries → helper returns None.

    The orchestrator coalesces this to [] before storing so the column distinguishes
    'legacy row, never snapshotted' (NULL) from 'new row, no refs to snapshot' ([])."""
    from app.services.fhir_client import snapshot_evaluated_resources

    assert await snapshot_evaluated_resources({"resourceType": "MeasureReport"}) is None
    assert await snapshot_evaluated_resources({"evaluatedResource": []}) is None
    assert await snapshot_evaluated_resources({}) is None


# ---------------------------------------------------------------------------
# Measure-management functions: base_url targeting (issue #396)
#
# These four tests are the regression guard for the whole bug. Every measure-
# management call must hit the base_url it was handed and never
# settings.MEASURE_ENGINE_URL — that env-var read is what made the measure list
# ignore the connected MCS. `_ACTIVE_MCS` is deliberately different from the
# env-var default (`http://hapi-fhir-measure:8080/fhir`) so a regression shows
# up as a failed URL assertion rather than an accidental pass.
# ---------------------------------------------------------------------------

_ACTIVE_MCS = "https://attendee-mcs.example.com/fhir"


async def test_list_measures(mock_measure_bundle):
    """list_measures queries the passed base_url, not settings.MEASURE_ENGINE_URL."""
    from app.config import settings

    mock_response = _make_response(200, mock_measure_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await list_measures(_ACTIVE_MCS, auth_headers={"Authorization": "Bearer tok"})

    assert result["resourceType"] == "Bundle"
    assert len(result["entry"]) == 1
    called_url = mock_ctx.get.await_args.args[0]
    assert called_url.startswith(_ACTIVE_MCS)
    assert settings.MEASURE_ENGINE_URL not in called_url
    assert mock_ctx.get.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


async def test_upload_measure_bundle():
    """upload_measure_bundle POSTs to the passed base_url, not the env-var engine."""
    from app.config import settings

    input_bundle = {"resourceType": "Bundle", "type": "transaction", "entry": []}
    response_bundle = {"resourceType": "Bundle", "type": "transaction-response"}
    mock_response = _make_response(200, response_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await upload_measure_bundle(
            input_bundle,
            _ACTIVE_MCS,
            auth_headers={"Authorization": "Bearer tok"},
        )

    assert result["type"] == "transaction-response"
    posted_url = mock_ctx.post.await_args.args[0]
    assert posted_url == _ACTIVE_MCS
    assert posted_url != settings.MEASURE_ENGINE_URL
    headers = mock_ctx.post.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Content-Type"] == "application/fhir+json"


async def test_upload_measure_bundle_threads_base_url_into_remap():
    """upload_measure_bundle hands its base_url + credentials to the ValueSet remap.

    `_remap_valueset_ids_for_hapi` is tested directly elsewhere, so only the
    wiring is at risk — but "the passed base_url is used everywhere" is the
    central guard of issue #396, and an untested wire is where it would rot.
    Asserted with the collaborator mocked so the assertion can't be satisfied
    by the remap silently no-opping on a non-200 lookup.
    """
    from app.config import settings

    input_bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [{"resource": {"resourceType": "ValueSet", "id": "1014", "url": "http://vs.example.com/1014"}}],
    }
    mock_response = _make_response(200, {"resourceType": "Bundle", "type": "transaction-response"})

    with (
        patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx,
        patch(
            "app.services.fhir_client._remap_valueset_ids_for_hapi",
            new_callable=AsyncMock,
            side_effect=lambda entries, client, base_url, auth_headers=None: entries,
        ) as mock_remap,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await upload_measure_bundle(
            input_bundle,
            _ACTIVE_MCS,
            auth_headers={"Authorization": "Bearer tok"},
        )

    mock_remap.assert_awaited_once()
    remap_base = mock_remap.await_args.args[2]
    assert remap_base == _ACTIVE_MCS
    assert remap_base != settings.MEASURE_ENGINE_URL
    assert mock_remap.await_args.args[3] == {"Authorization": "Bearer tok"}


async def test_delete_measure_targets_passed_base_url():
    """delete_measure DELETEs against the passed base_url with its auth headers."""
    from app.config import settings

    mock_response = _make_response(204, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.delete = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await delete_measure("CMS122", _ACTIVE_MCS, auth_headers={"Authorization": "Bearer tok"})

    called_url = mock_ctx.delete.await_args.args[0]
    assert called_url == f"{_ACTIVE_MCS}/Measure/CMS122"
    assert settings.MEASURE_ENGINE_URL not in called_url
    assert mock_ctx.delete.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}


async def test_remap_valueset_ids_queries_passed_base_url():
    """_remap_valueset_ids_for_hapi resolves existing ValueSets on the upload target.

    Querying the env-var engine here would rewrite ids to values that don't
    exist on the server the bundle is about to be POSTed to.
    """
    from app.config import settings

    entries = [
        {
            "resource": {"resourceType": "ValueSet", "id": "1014", "url": "http://vs.example.com/1014"},
            "request": {"method": "PUT", "url": "ValueSet/1014"},
        }
    ]
    existing = {"entry": [{"resource": {"id": "1014-20240112"}}]}
    client = AsyncMock()
    client.get = AsyncMock(return_value=_make_response(200, existing))

    out = await _remap_valueset_ids_for_hapi(entries, client, _ACTIVE_MCS, {"Authorization": "Bearer tok"})

    called_url = client.get.await_args.args[0]
    assert called_url == f"{_ACTIVE_MCS}/ValueSet"
    assert settings.MEASURE_ENGINE_URL not in called_url
    assert client.get.await_args.kwargs["headers"] == {"Authorization": "Bearer tok"}
    # And the remap itself still happened.
    assert out[0]["resource"]["id"] == "1014-20240112"
    assert out[0]["request"]["url"] == "ValueSet/1014-20240112"


# ---------------------------------------------------------------------------
# measure_exists
# ---------------------------------------------------------------------------


async def test_measure_exists_true_when_total_positive():
    """total > 0 in the count bundle means the measure is present."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "total": 1}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        assert await measure_exists("CMS122", _ACTIVE_MCS) is True

    assert mock_ctx.get.await_args.args[0] == f"{_ACTIVE_MCS}/Measure"
    assert mock_ctx.get.await_args.kwargs["params"] == {"_id": "CMS122", "_summary": "count"}


async def test_measure_exists_false_when_total_zero():
    """total == 0 means the measure is absent — a normal answer, not an error."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "total": 0}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        assert await measure_exists("CMS122", _ACTIVE_MCS) is False


async def test_measure_exists_propagates_transport_errors():
    """Connection failures must NOT be swallowed into False.

    POST /jobs distinguishes "measure absent" (400) from "MCS unreachable"
    (502); collapsing the two here would make that impossible.
    """
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.ConnectError):
            await measure_exists("CMS122", _ACTIVE_MCS)


async def test_measure_exists_propagates_http_status_errors():
    """A 500 from the MCS raises rather than reporting the measure as missing."""
    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=_make_response(500, {"resourceType": "OperationOutcome"}))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await measure_exists("CMS122", _ACTIVE_MCS)


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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert len(resources) == 2
    types = {r["resourceType"] for r in resources}
    assert types == {"Patient", "Observation"}


async def test_data_requirements_targets_the_jobs_mcs_with_credentials():
    """$data-requirements goes to the job's MCS, authenticated (issue #397).

    It previously read settings.MEASURE_ENGINE_URL with no credentials, so a job
    against a remote MCS asked the LOCAL engine what data the measure needs — and
    silently fell back to $everything when that answer was wrong or the call 401'd.
    """
    seen: dict[str, object] = {}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Patient"}]})
        return _make_response(200, {"resourceType": "Patient", "id": "p1"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy(
            "m1",
            mcs_url="https://mcs.example.org/fhir",
            mcs_auth_headers={"Authorization": "Bearer tok-dr"},
        )
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert seen["url"] == "https://mcs.example.org/fhir/Measure/m1/$data-requirements"
    assert seen["headers"] == {"Authorization": "Bearer tok-dr"}


async def test_data_requirements_requires_an_mcs_url():
    """No env-var default — the caller must say which engine to ask."""
    with pytest.raises(TypeError):
        DataRequirementsStrategy("m1")  # type: ignore[call-arg]


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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # 404 from CDR means no resources returned (no fallback for non-200 within _fetch_by_requirements)
    assert resources == []


async def test_data_requirements_strategy_patient_failure_reports_partial_gather():
    """F2: a non-200 Patient read must be recorded as a FailedResourceFetch so
    the gather is reported as partial (has_partial_failure) — previously it
    was swallowed with no failure record and no way to attribute a later
    failure to the missing Patient. Patient must still NOT be added to
    required_types: the declared Condition requirement succeeds, so no
    $everything fallback should fire."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }
    cond_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Condition", "id": "c1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/p1" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        if "Condition" in url:
            return _make_response(200, cond_bundle)
        return _make_response(200, {"resourceType": "Bundle", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert gather_result.resources == [{"resourceType": "Condition", "id": "c1"}]
    assert gather_result.has_partial_failure is True
    assert [f.resource_type for f in gather_result.failed_types] == ["Patient"]
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0  # Condition (the only declared requirement) succeeded


async def test_data_requirements_strategy_non_200_resource_entries_skipped():
    """When ALL required types return non-200, the $everything fallback is triggered."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "$everything" in url:
            return _make_response(200, empty_bundle)
        # CDR per-type query fails
        return _make_response(500, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # All types failed → fell back to $everything which returned empty bundle
    assert resources == []
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 1


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
        if "/Patient/" in url:
            # Patient is now always fetched too (I3) — simulate "not found" so
            # this test's resource count stays scoped to the declared Observation
            # requirement, which is what it's actually exercising.
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
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
        if "/Patient/" in url:
            # Patient is now always fetched too (I3) — simulate "not found" so
            # this test's resource count stays scoped to the declared
            # Encounter requirement, which is what it's actually exercising.
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, enc_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    # Observation fetched; Condition skipped; no $everything fallback
    assert any(r.get("resourceType") == "Observation" for r in resources)
    assert not any(r.get("resourceType") == "Condition" for r in resources)
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 0


async def test_fetch_by_requirements_multiple_valuesets_drop_filter_and_overfetch():
    """Two requirements of the same type with DIFFERENT valuesets must not be
    ANDed via repeated code:in= params (I3) — that would narrow, not union,
    the result. The type is fetched unfiltered instead."""
    vs1 = "http://example.org/ValueSet/vs1"
    vs2 = "http://example.org/ValueSet/vs2"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation", "codeFilter": [{"valueSet": vs1}]},
            {"type": "Observation", "codeFilter": [{"valueSet": vs2}]},
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Observation", "id": "o1"}},
            {"resource": {"resourceType": "Observation", "id": "o2"}},
        ],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_urls = [u for u in captured_urls if "Observation" in u]
    assert len(obs_urls) == 1, f"Observation should be fetched exactly once, got {obs_urls}"
    assert "code:in" not in obs_urls[0]
    assert len(resources) == 2


async def test_fetch_by_requirements_single_valueset_filter_preserved():
    """A single distinct valueset across all of a type's requirements still
    keeps the code:in= filter (I3 — the conservative path is only for
    disagreeing/mixed requirements)."""
    vs_url = "http://example.org/ValueSet/vs1"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation", "codeFilter": [{"valueSet": vs_url}]},
            {"type": "Observation", "codeFilter": [{"valueSet": vs_url}]},  # same valueset, repeated
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
        if "/Patient/" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_urls = [u for u in captured_urls if "Observation" in u]
    assert len(obs_urls) == 1
    assert f"code:in={vs_url}" in obs_urls[0]
    assert len(resources) == 1


async def test_fetch_by_requirements_mixed_filtered_and_unfiltered_drops_filter():
    """One requirement for a type carries a codeFilter, another for the SAME
    type has none — the mix must drop the filter and over-fetch, same as the
    multiple-distinct-valuesets case (coverage-audit gap fill for the
    `has_unfiltered` branch of the grouping logic in I3)."""
    vs_url = "http://example.org/ValueSet/vs1"
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Observation", "codeFilter": [{"valueSet": vs_url}]},
            {"type": "Observation"},  # same type, no codeFilter at all
        ],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Observation", "id": "o1"}},
            {"resource": {"resourceType": "Observation", "id": "o2"}},
        ],
        "link": [],
    }

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    obs_urls = [u for u in captured_urls if "Observation" in u]
    assert len(obs_urls) == 1, f"Observation should be fetched exactly once, got {obs_urls}"
    assert "code:in" not in obs_urls[0]
    assert len(resources) == 2


async def test_fetch_by_requirements_patient_always_fetched_even_when_absent():
    """Patient is fetched by direct read even when it's not a declared
    dataRequirement — $everything always includes it, and its absence fails
    evaluation regardless (I3)."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Observation"}],
    }
    obs_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [],
    }
    patient_resource = {"resourceType": "Patient", "id": "p1"}

    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/p1" in url:
            return _make_response(200, patient_resource)
        return _make_response(200, obs_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert any(u.endswith("/Patient/p1") for u in captured_urls), (
        f"Patient should always be fetched by direct read, got {captured_urls}"
    )
    assert any(r.get("resourceType") == "Patient" and r.get("id") == "p1" for r in resources)
    assert any(r.get("resourceType") == "Observation" for r in resources)


async def test_fetch_by_requirements_all_required_types_fail_triggers_fallback_despite_patient():
    """A forced Patient-fetch success/failure must not mask "all REQUIRED types
    failed" — the fallback trigger only counts declared dataRequirement types."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Condition"}],
    }
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "$everything" in url:
            return _make_response(200, empty_bundle)
        if "/Patient/p1" in url:
            # Patient direct-read failure — must not count toward "required
            # types" and must not itself block the fallback below.
            return _make_response(404, {"resourceType": "OperationOutcome"})
        # Condition search fails
        return _make_response(500, {"resourceType": "OperationOutcome"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        gather_result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        resources = gather_result.resources

    assert resources == []
    everything_calls = [c for c in mock_ctx.get.call_args_list if "$everything" in str(c)]
    assert len(everything_calls) == 1


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

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
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


# ---------------------------------------------------------------------------
# MCS auth wiring — evaluate_measure / resolve_evaluated_resource
# (regression: remote MCS connections got 401 because no auth was ever sent)
# ---------------------------------------------------------------------------


async def test_evaluate_measure_sends_auth_headers(mock_measure_report):
    """evaluate_measure forwards auth headers to the MCS."""
    mock_response = _make_response(200, mock_measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await evaluate_measure(
            "measure-1",
            "patient-1",
            "2024-01-01",
            "2024-12-31",
            measure_engine_url="https://mcs.example.org/fhir",
            auth_headers={"Authorization": "Bearer tok-123"},
        )

    sent = mock_ctx.get.call_args.kwargs.get("headers")
    assert sent is not None, "evaluate_measure must pass headers to the MCS"
    assert sent.get("Authorization") == "Bearer tok-123"


async def test_evaluate_measure_without_auth_sends_no_authorization(mock_measure_report):
    """Unauthenticated MCS (local HAPI) keeps working — no Authorization header."""
    mock_response = _make_response(200, mock_measure_report)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await evaluate_measure("measure-1", "patient-1", "2024-01-01", "2024-12-31")

    sent = mock_ctx.get.call_args.kwargs.get("headers") or {}
    assert "Authorization" not in sent


async def test_resolve_evaluated_resource_uses_given_base_and_auth():
    """Snapshot reads target the job's MCS with its credentials, not the env default."""
    mock_response = _make_response(200, {"resourceType": "Condition", "id": "c1"})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(return_value=mock_response)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        await resolve_evaluated_resource(
            "Condition/c1",
            base_url="https://mcs.example.org/fhir",
            auth_headers={"Authorization": "Bearer tok-123"},
        )

    assert mock_ctx.get.call_args[0][0] == "https://mcs.example.org/fhir/Condition/c1"
    assert mock_ctx.get.call_args.kwargs.get("headers", {}).get("Authorization") == "Bearer tok-123"


# ---------------------------------------------------------------------------
# McsTarget (issue #397 slice 3)
# ---------------------------------------------------------------------------


def test_mcs_target_is_frozen():
    """Immutable on purpose: it is threaded through ~16 call sites, and a helper
    mutating the shared target would silently re-point every later call."""
    from dataclasses import FrozenInstanceError

    from app.services.fhir_client import McsTarget

    t = McsTarget(url="https://mcs.example.org/fhir", auth_headers={}, is_read_only=False, wipe_before_job=False)
    with pytest.raises(FrozenInstanceError):
        t.url = "https://elsewhere.example.org/fhir"  # type: ignore[misc]


def test_mcs_target_requires_every_field():
    """No defaults. A default target is exactly how issue #397 stayed invisible."""
    from app.services.fhir_client import McsTarget

    with pytest.raises(TypeError):
        McsTarget(url="https://mcs.example.org/fhir")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# DEQM $submit-data support (spec: 2026-08-21-deqm-submit-data-workflow)
# ---------------------------------------------------------------------------


def _mock_async_client(mock_httpx, *, get=None, post=None):
    """Wire an AsyncMock client into the patched httpx.AsyncClient ctor."""
    ctx = AsyncMock()
    if get is not None:
        ctx.get = get
    if post is not None:
        ctx.post = post
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=ctx)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestGetMeasureCanonical:
    async def test_returns_url_pipe_version(self):
        measure = {"resourceType": "Measure", "id": "m1", "url": "http://ex.org/Measure/m1", "version": "2.0"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1|2.0"

    async def test_returns_bare_url_without_version(self):
        measure = {"resourceType": "Measure", "id": "m1", "url": "http://ex.org/Measure/m1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1"

    async def test_falls_back_to_relative_reference_when_url_missing(self):
        measure = {"resourceType": "Measure", "id": "m1"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "Measure/m1"

    async def test_drops_version_containing_whitespace(self):
        """MADiE stamps every draft measure `Draft based on X.Y.ZZZ`.

        `MeasureReport.measure` is a canonical, i.e. a uri, whose value regex
        is `\\S*` — so a version with spaces cannot ride along. The bare url
        means "any version", which resolves; `url|Draft based on 0.0.000`
        resolves to a 412 for every patient in the job (#452).
        """
        measure = {
            "resourceType": "Measure",
            "id": "m1",
            "url": "http://ex.org/Measure/m1",
            "version": "Draft based on 0.0.000",
        }
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1"

    @pytest.mark.parametrize("version", ["1.0 0", "1.0\t0", "1.0\n0", "  ", "\u00a0"])
    async def test_drops_version_for_any_whitespace_character(self, version):
        measure = {"resourceType": "Measure", "id": "m1", "url": "http://ex.org/Measure/m1", "version": version}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1"

    async def test_warns_naming_the_measure_and_rejected_version(self, caplog):
        """Dropping a version pin narrows nothing on a single-copy server, but it
        does change meaning when the MCS holds several versions. Fall back
        loudly, never silently."""
        measure = {
            "resourceType": "Measure",
            "id": "m1",
            "url": "http://ex.org/Measure/m1",
            "version": "Draft based on 0.0.000",
        }
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            with caplog.at_level("WARNING"):
                await get_measure_canonical("m1", mcs_url="http://mcs")
        assert "whitespace" in caplog.text
        record = next(r for r in caplog.records if "whitespace" in r.message)
        assert record.measure_id == "m1"
        assert record.measure_version == "Draft based on 0.0.000"

    async def test_does_not_warn_for_a_legal_version(self, caplog):
        measure = {"resourceType": "Measure", "id": "m1", "url": "http://ex.org/Measure/m1", "version": "2.0"}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, measure)))
            with caplog.at_level("WARNING"):
                result = await get_measure_canonical("m1", mcs_url="http://mcs")
        assert result == "http://ex.org/Measure/m1|2.0"
        assert caplog.text == ""

    async def test_raises_on_http_error(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(
                mock_httpx, get=AsyncMock(return_value=_make_response(404, {"resourceType": "OperationOutcome"}))
            )
            with pytest.raises(FhirOperationError):
                await get_measure_canonical("m1", mcs_url="http://mcs")


class TestOperationDefinitionMatchesContract:
    def _od(self, **overrides) -> dict:
        od = {
            "resourceType": "OperationDefinition",
            "code": "submit-data",
            "type": True,
            "instance": False,
            "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "*"}],
        }
        od.update(overrides)
        return od

    def test_matches_type_level_submit_data_with_bundle_input(self):
        assert _operation_definition_matches_contract(self._od()) is True

    def test_matches_even_when_also_instance_level(self):
        """HAPI 8.10.1 declares type:true AND instance:true. Supporting the
        instance level too does not stop it supporting the type level."""
        assert _operation_definition_matches_contract(self._od(instance=True)) is True

    def test_rejects_instance_only_operation(self):
        assert _operation_definition_matches_contract(self._od(type=False, instance=True)) is False

    def test_rejects_wrong_code(self):
        assert _operation_definition_matches_contract(self._od(code="deqm-submit-data")) is False

    def test_rejects_when_bundle_parameter_absent(self):
        """Base-only server: measureReport + resource, no bundle."""
        od = self._od(
            parameter=[
                {"name": "measureReport", "use": "in", "min": 1, "max": "1"},
                {"name": "resource", "use": "in", "min": 0, "max": "*"},
            ]
        )
        assert _operation_definition_matches_contract(od) is False

    def test_rejects_when_bundle_is_an_output_parameter(self):
        od = self._od(parameter=[{"name": "bundle", "use": "out", "min": 1, "max": "*"}])
        assert _operation_definition_matches_contract(od) is False

    def test_rejects_missing_type_element(self):
        od = self._od()
        del od["type"]
        assert _operation_definition_matches_contract(od) is False


class TestResolveOperationDefinition:
    _OD = {
        "resourceType": "OperationDefinition",
        "code": "submit-data",
        "type": True,
        "parameter": [{"name": "bundle", "use": "in", "max": "*"}],
    }

    async def test_same_origin_definition_is_fetched_directly(self):
        get = AsyncMock(return_value=_make_response(200, self._OD))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/Measure-it-submit-data", {}
                )
        assert result == self._OD
        assert get.call_args[0][0] == "http://mcs/OperationDefinition/Measure-it-submit-data"

    async def test_version_suffix_is_stripped_before_fetching(self):
        get = AsyncMock(return_value=_make_response(200, self._OD))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                await _resolve_operation_definition(client, "http://mcs", "http://mcs/OperationDefinition/x|5.0.0", {})
        assert get.call_args[0][0] == "http://mcs/OperationDefinition/x"

    async def test_foreign_origin_is_never_fetched_directly(self):
        """SSRF guard: `definition` is a URL chosen by a remote server. It is
        resolved by canonical search against the MCS, never dereferenced."""
        bundle = {"resourceType": "Bundle", "entry": [{"resource": self._OD}]}
        get = AsyncMock(return_value=_make_response(200, bundle))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://hl7.org/fhir/OperationDefinition/Measure-submit-data", {}
                )
        assert result == self._OD
        assert get.call_args[0][0] == "http://mcs/OperationDefinition"
        assert get.call_args.kwargs["params"] == {"url": "http://hl7.org/fhir/OperationDefinition/Measure-submit-data"}
        for call in get.call_args_list:
            assert "hl7.org" not in call[0][0]

    async def test_returns_none_when_direct_fetch_is_not_200(self):
        get = AsyncMock(return_value=_make_response(404, {"resourceType": "OperationOutcome"}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/x", {}
                )
        assert result is None

    async def test_returns_none_when_canonical_search_finds_nothing(self):
        get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "entry": []}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://elsewhere.example/OperationDefinition/x", {}
                )
        assert result is None

    async def test_returns_none_for_a_wrong_resource_type(self):
        get = AsyncMock(return_value=_make_response(200, {"resourceType": "Patient", "id": "p1"}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/x", {}
                )
        assert result is None

    @pytest.mark.parametrize("definition", ["", "   ", "|5.0.0", "  |5.0.0"])
    async def test_returns_none_for_a_blank_definition(self, definition):
        get = AsyncMock()
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(client, "http://mcs", definition, {})
        assert result is None
        get.assert_not_awaited()

    async def test_auth_headers_reach_a_same_origin_fetch(self):
        """Dropping the headers would make every authenticated MCS answer 401,
        and the probe would silently classify it base-fallback."""
        get = AsyncMock(return_value=_make_response(200, self._OD))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/sd", {"Authorization": "Bearer t"}
                )
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer t"}

    async def test_auth_headers_reach_a_canonical_search(self):
        get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "entry": []}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                await _resolve_operation_definition(
                    client, "http://mcs", "http://hl7.org/fhir/OperationDefinition/x", {"Authorization": "Bearer t"}
                )
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer t"}

    async def test_a_non_operation_definition_entry_in_the_search_bundle_is_skipped(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "OperationOutcome"}}, {"resource": self._OD}],
        }
        get = AsyncMock(return_value=_make_response(200, bundle))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://elsewhere.example/OperationDefinition/x", {}
                )
        assert result == self._OD


class TestDetectSubmitDataMode:
    _CONTRACT_OD = {
        "resourceType": "OperationDefinition",
        "code": "submit-data",
        "type": True,
        "instance": True,
        "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "*"}],
    }
    _BASE_ONLY_OD = {
        "resourceType": "OperationDefinition",
        "code": "submit-data",
        "type": True,
        "instance": True,
        "parameter": [
            {"name": "measureReport", "use": "in", "min": 1, "max": "1"},
            {"name": "resource", "use": "in", "min": 0, "max": "*"},
        ],
    }

    def _capability(self, operations: list[dict]) -> dict:
        return {
            "resourceType": "CapabilityStatement",
            "rest": [{"mode": "server", "resource": [{"type": "Measure", "operation": operations}]}],
        }

    def _responder(self, capability: dict, operation_definition: dict | None):
        """GET /metadata returns the capability; anything else returns the OD."""

        async def _get(url, *args, **kwargs):
            if url.endswith("/metadata"):
                return _make_response(200, capability)
            if operation_definition is None:
                return _make_response(404, {"resourceType": "OperationOutcome"})
            return _make_response(200, operation_definition)

        return AsyncMock(side_effect=_get)

    async def test_stu5_for_type_level_submit_data_with_bundle_input(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_STU5

    async def test_base_when_operation_is_instance_only(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "type": False}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_base_when_operation_takes_no_bundle(self):
        """The distinction a CapabilityStatement alone cannot make: this server
        offers a type-level $submit-data, but only in the measureReport +
        resource shape."""
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._BASE_ONLY_OD))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_base_when_only_the_retired_deqm_operation_is_advertised(self):
        """#413 decision 1: support for the retired $deqm-submit-data is dropped,
        not renamed. A server offering only it is a base-fallback server. This
        test is what fails if someone restores historical support without
        revisiting the design doc."""
        cap = self._capability(
            [
                {
                    "name": "deqm-submit-data",
                    "definition": "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data",
                }
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_retired_only_server_is_logged(self):
        cap = self._capability([{"name": "deqm-submit-data", "definition": "http://mcs/OperationDefinition/x"}])
        with (
            patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx,
            patch("app.services.fhir_client.logger.info") as info,
        ):
            _mock_async_client(mock_httpx, get=self._responder(cap, None))
            await detect_submit_data_capability(mcs_url="http://mcs")
        assert any("retired" in str(c.args[0]).lower() for c in info.call_args_list)

    async def test_base_when_candidate_has_no_definition(self):
        cap = self._capability([{"name": "submit-data"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_base_when_operation_definition_is_unfetchable(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, None))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_foreign_origin_definition_is_not_contacted(self):
        cap = self._capability(
            [{"name": "submit-data", "definition": "http://hl7.org/fhir/OperationDefinition/Measure-submit-data"}]
        )
        get = self._responder(cap, None)
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=get)
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE
        for call in get.call_args_list:
            assert "hl7.org" not in call[0][0]

    async def test_operation_on_rest_root_is_also_considered(self):
        cap = {
            "resourceType": "CapabilityStatement",
            "rest": [
                {
                    "mode": "server",
                    "operation": [{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}],
                }
            ],
        }
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_STU5

    async def test_at_most_three_operation_definitions_are_fetched(self):
        cap = self._capability(
            [{"name": "submit-data", "definition": f"http://mcs/OperationDefinition/sd{i}"} for i in range(10)]
        )
        get = self._responder(cap, self._BASE_ONLY_OD)
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=get)
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE
        # 1 metadata call + exactly _MAX_OPERATION_DEFINITION_PROBES definition
        # reads. Asserted as equality: `<=` also passes when the budget is
        # spent early or no definition is read at all.
        assert get.await_count == 1 + _MAX_OPERATION_DEFINITION_PROBES

    async def test_a_repeated_canonical_does_not_consume_two_probe_slots(self):
        """The same operation advertised at rest.operation and at
        rest.resource[Measure].operation is one definition, not two."""
        cap = {
            "resourceType": "CapabilityStatement",
            "rest": [
                {
                    "mode": "server",
                    "operation": [{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}],
                    "resource": [
                        {
                            "type": "Measure",
                            "operation": [{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}],
                        }
                    ],
                }
            ],
        }
        get = self._responder(cap, self._BASE_ONLY_OD)
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=get)
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE
        # 1 metadata call + 1 definition read, not 2.
        assert get.await_count == 2

    async def test_an_unparseable_candidate_does_not_abort_the_ones_after_it(self):
        """A 200 carrying an HTML proxy error page raises out of the JSON parse.
        The contracted definition advertised after it must still be probed."""

        cap = self._capability(
            [
                {"name": "submit-data", "definition": "http://mcs/OperationDefinition/broken"},
                {"name": "submit-data", "definition": "http://mcs/OperationDefinition/good"},
            ]
        )
        unparseable = _make_response(200, {})
        unparseable.json = MagicMock(side_effect=ValueError("not JSON"))

        async def _get(url, *args, **kwargs):
            if url.endswith("/metadata"):
                return _make_response(200, cap)
            if url.endswith("/broken"):
                return unparseable
            return _make_response(200, self._CONTRACT_OD)

        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(side_effect=_get))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_STU5

    async def test_fallback_when_probe_raises(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(side_effect=httpx.ConnectError("boom")))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_never_raises_when_capability_body_is_malformed(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, {"rest": "not-a-list"})))
            assert (await detect_submit_data_capability(mcs_url="http://mcs")).mode == SUBMIT_DATA_MODE_BASE

    async def test_bundle_max_star_is_unbounded(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max is None

    async def test_bundle_max_digit_string_is_retained(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "5"}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max == 5

    async def test_bundle_max_of_one_still_classifies_stu5(self):
        """Spec § Testing: a max:"1" server is STU5 and the max is retained for
        clamping. The declared bound governs how many bundles we send, never
        whether the contract is supported at all."""
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "1"}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max == 1

    async def test_missing_bundle_max_is_unbounded(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max is None

    async def test_unparseable_bundle_max_is_unbounded_and_does_not_disturb_the_verdict(self):
        """An unparseable bound is not evidence of a limit, and must never cost
        a job its STU5 path."""
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "many"}]}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_STU5
        assert result.bundle_max is None

    async def test_base_fallback_reports_no_bundle_max(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(side_effect=httpx.ConnectError("boom")))
            result = await detect_submit_data_capability(mcs_url="http://mcs")
        assert result.mode == SUBMIT_DATA_MODE_BASE
        assert result.bundle_max is None


class TestSubmitData:
    async def test_posts_to_type_level_operation_in_stu5_mode(self):
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_STU5,
                measure_id="M1",
            )
        assert post.call_args[0][0] == "http://mcs/Measure/$submit-data"

    async def test_posts_to_instance_level_operation_in_base_mode(self):
        # Empirically verified against a local prebaked HAPI measure server: the
        # type-level POST /Measure/$submit-data returns 400 not-supported ("does
        # not know how to handle POST operation[Measure/$submit-data]"), while the
        # instance-level POST /Measure/{id}/$submit-data returns 200 and performs a
        # real upsert. Base-fallback mode must use the instance-level shape.
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
        assert post.call_args[0][0] == "http://mcs/Measure/M1/$submit-data"

    async def test_base_and_stu5_modes_produce_different_url_shapes(self):
        # Regression guard for the ruling this test file encodes: base-fallback
        # is instance-level (Measure/{id}/$submit-data) because that's the only
        # shape HAPI's clinical-reasoning module accepts; the selected STU5
        # contract is type-level (Measure/$submit-data). The two now differ only
        # by the measure-id segment, so collapsing them into one shape is an
        # easy mistake to make and this test is what catches it.
        post = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
            base_url = post.call_args[0][0]

            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_STU5,
                measure_id="M1",
            )
            stu5_url = post.call_args[0][0]

        assert base_url == "http://mcs/Measure/M1/$submit-data"
        assert stu5_url == "http://mcs/Measure/$submit-data"
        assert base_url != stu5_url

    async def test_409_conflict_retries_and_succeeds(self):
        """F12: an incidental HAPI ResourceVersionConflictException (409) —
        e.g. a patient's own resources racing a concurrent read/write on the
        same server — is retried once rather than failing the patient
        outright. (The primary source of these conflicts, every patient
        upserting the same shared Organization/lenny-reporter, is fixed
        upstream: that resource is now PUT once per job, not inlined per
        patient — see workflows.build_submission_workflow.)"""
        post = AsyncMock(
            side_effect=[
                _make_response(409, {"resourceType": "OperationOutcome"}),
                _make_response(200, {"resourceType": "Bundle"}),
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with patch("app.services.fhir_client.asyncio.sleep", new=AsyncMock()) as sleep:
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert post.await_count == 2
        sleep.assert_awaited_once()

    async def test_412_conflict_also_retries(self):
        post = AsyncMock(
            side_effect=[
                _make_response(412, {"resourceType": "OperationOutcome"}),
                _make_response(200, {"resourceType": "Bundle"}),
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with patch("app.services.fhir_client.asyncio.sleep", new=AsyncMock()):
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert post.await_count == 2

    async def test_409_conflict_exhausts_retries_and_raises(self):
        """After 1 retry (2 total attempts) a persistent 409 still raises."""
        post = AsyncMock(return_value=_make_response(409, {"resourceType": "OperationOutcome"}))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with patch("app.services.fhir_client.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(FhirOperationError) as exc_info:
                    await submit_data(
                        mcs_url="http://mcs",
                        parameters={"resourceType": "Parameters"},
                        mode=SUBMIT_DATA_MODE_BASE,
                        measure_id="M1",
                    )
        assert exc_info.value.status_code == 409
        assert post.await_count == 2

    async def test_raises_fhir_operation_error_on_4xx(self):
        oo = {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": "invalid", "diagnostics": "bad payload"}],
        }
        post = AsyncMock(return_value=_make_response(400, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with pytest.raises(FhirOperationError) as exc_info:
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert exc_info.value.status_code == 400
        assert exc_info.value.operation == "submit-data"

    # -- #415: a 2xx is not proof of a delivered submission ------------------
    # submit_data used to return on any status < 300 without reading the body.
    # A server that rejects a submission inside a 200 therefore marked the
    # patient transferred, and evaluation then ran against data the measure
    # server never accepted — producing a population figure computed from
    # missing data and reported as a normal result. evaluate_measure in this
    # same module already guards this exact shape; these tests make the two
    # operations agree about whether a 200 can mean failure.

    async def test_200_operation_outcome_with_error_issue_raises(self):
        """AC1: a rejection returned inside a 200 must fail the patient."""
        oo = {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "processing",
                    "diagnostics": "Unable to resolve reference Patient/nope",
                }
            ],
        }
        post = AsyncMock(return_value=_make_response(200, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with pytest.raises(FhirOperationError) as exc_info:
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert exc_info.value.status_code == 200
        assert exc_info.value.operation == "submit-data"
        # AC1: the outcome is preserved, not discarded — this is what reaches
        # MeasureResult.error_details, so a user sees why the patient failed.
        assert exc_info.value.outcome is not None
        assert exc_info.value.outcome.primary_diagnostic() == "Unable to resolve reference Patient/nope"

    async def test_200_operation_outcome_with_fatal_issue_raises(self):
        """AC1: `fatal` is as disqualifying as `error`."""
        oo = {
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "fatal", "code": "exception", "diagnostics": "transaction rolled back"}],
        }
        post = AsyncMock(return_value=_make_response(200, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with pytest.raises(FhirOperationError) as exc_info:
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )
        assert exc_info.value.outcome.primary_diagnostic() == "transaction rolled back"

    async def test_200_transaction_bundle_still_succeeds(self):
        """AC2: HAPI's success shape must keep succeeding.

        Boundary guard for the check above: an implementation that raised on
        any 2xx body, or that required a Bundle-with-no-issues, would fail
        every real submission.
        """
        post = AsyncMock(
            return_value=_make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "transaction-response",
                    "entry": [{"response": {"status": "201 Created"}}],
                },
            )
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
        assert post.await_count == 1

    async def test_200_operation_outcome_with_only_warnings_succeeds(self):
        """AC3: a warning/information OperationOutcome is not a rejection.

        Boundary guard: the naive fix — "a 200 whose body is an
        OperationOutcome raises" — fails this test. Servers legitimately
        return advisory outcomes alongside a successful write, and failing
        those patients would be a new bug in the opposite direction.
        """
        oo = {
            "resourceType": "OperationOutcome",
            "issue": [
                {"severity": "warning", "code": "informational", "diagnostics": "partial code match"},
                {"severity": "information", "code": "informational", "diagnostics": "accepted"},
            ],
        }
        post = AsyncMock(return_value=_make_response(200, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
        assert post.await_count == 1

    async def test_200_operation_outcome_with_issue_missing_severity_raises(self):
        """FHIR requires `severity`; an issue without one is malformed. It is
        treated as an error, so a malformed rejection fails the patient rather
        than passing silently.

        The break this catches: FhirOperationOutcome.from_dict defaults a
        missing severity to "error". If that default were ever relaxed to
        "information", a malformed rejection would start counting as a
        delivered patient — the exact bug #415 exists to close.
        """
        oo = {
            "resourceType": "OperationOutcome",
            "issue": [{"code": "processing", "diagnostics": "rejected, severity omitted"}],
        }
        post = AsyncMock(return_value=_make_response(200, oo))
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            with pytest.raises(FhirOperationError):
                await submit_data(
                    mcs_url="http://mcs",
                    parameters={"resourceType": "Parameters"},
                    mode=SUBMIT_DATA_MODE_BASE,
                    measure_id="M1",
                )

    async def test_200_with_unparseable_body_still_succeeds(self):
        """A server that returns 200 with a non-JSON body must not start
        failing patients because of this check — the guard reads the body to
        find rejections, and an unreadable body is not evidence of one."""
        resp = httpx.Response(200, content=b"OK", request=_DUMMY_REQUEST)
        post = AsyncMock(return_value=resp)
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, post=post)
            await submit_data(
                mcs_url="http://mcs",
                parameters={"resourceType": "Parameters"},
                mode=SUBMIT_DATA_MODE_BASE,
                measure_id="M1",
            )
        assert post.await_count == 1


async def test_data_requirements_cached_across_patients():
    """$data-requirements is called once per job, not once per patient.

    Calling it per patient compiles the measure's CQL in the engine for every
    patient; at 319 patients that drove the measure engine past its container
    memory limit and got it OOM-killed mid-job.
    """
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }
    calls: list[str] = []

    async def mock_get(url, **kwargs):
        calls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "Patient/" in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p"})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        for pid in ("p1", "p2", "p3"):
            await strategy.gather_patient_data("http://cdr/fhir", pid, {})

    assert sum("$data-requirements" in c for c in calls) == 1


async def test_data_requirements_cached_under_concurrent_first_callers():
    """Concurrent first patients do not each issue the same expensive call."""
    data_req_response = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Patient"}],
    }
    calls: list[str] = []

    async def mock_get(url, **kwargs):
        calls.append(url)
        if "$data-requirements" in url:
            await asyncio.sleep(0.01)  # widen the window a stampede would use
            return _make_response(200, data_req_response)
        if "Patient/" in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p"})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        await asyncio.gather(*(strategy.gather_patient_data("http://cdr/fhir", f"p{i}", {}) for i in range(8)))

    assert sum("$data-requirements" in c for c in calls) == 1


async def test_data_requirements_failure_is_not_cached():
    """A transient $data-requirements failure must not pin the job to $everything."""
    attempts = {"n": 0}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return _make_response(500, {})
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Patient"}]})
        if "$everything" in url:
            return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})
        if "Patient/" in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p"})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})  # fails -> $everything
        await strategy.gather_patient_data("http://cdr/fhir", "p2", {})  # retries, succeeds

    assert attempts["n"] == 2


async def test_filtered_query_failure_retries_unfiltered():
    """A code:in= filter the CDR cannot resolve must not silently drop the type.

    HAPI returns HAPI-2788 "Unknown ValueSet" for VSAC canonicals it never
    loaded. Treating that as "no resources of this type" cost nine patients
    their CMS130 denominator-exclusion while the job still reported success.
    """
    data_req = {
        "resourceType": "Library",
        "dataRequirement": [
            {
                "type": "ServiceRequest",
                "codeFilter": [{"path": "code", "valueSet": "http://cts.nlm.nih.gov/fhir/ValueSet/1.2.3"}],
            }
        ],
    }
    seen: list[str] = []

    async def mock_get(url, **kwargs):
        seen.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req)
        if "Patient/p1" in url and "ServiceRequest" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "ServiceRequest" in url:
            if "code:in=" in url:
                return _make_response(400, {"resourceType": "OperationOutcome"})
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "ServiceRequest", "id": "sr1"}}],
                    "link": [],
                },
            )
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    types = {r["resourceType"] for r in result.resources}
    assert "ServiceRequest" in types, "unfiltered retry should have recovered the resource"
    assert result.failed_types == [], "a recovered type must not be reported as a partial failure"
    assert any("code:in=" in u for u in seen), "filtered query should be attempted first"


async def test_filtered_retry_does_not_duplicate_on_partial_pagination():
    """A failure part-way through pagination must not leave half a page behind."""
    data_req = {
        "resourceType": "Library",
        "dataRequirement": [{"type": "Observation", "codeFilter": [{"path": "code", "valueSet": "http://vs/1"}]}],
    }
    page1 = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
        "link": [{"relation": "next", "url": "http://cdr/fhir/Observation?page=2&code:in=http://vs/1"}],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req)
        if "Patient/p1" in url and "Observation" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "Observation" in url:
            if "code:in=" in url:
                # first page succeeds, second page blows up mid-pagination
                return _make_response(200, page1) if "page=2" not in url else _make_response(500, {})
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "Observation", "id": "o1"}}],
                    "link": [],
                },
            )
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    obs = [r for r in result.resources if r["resourceType"] == "Observation"]
    assert len(obs) == 1, f"expected the partial page to be discarded, got {obs}"


async def test_unfiltered_query_failure_still_reported():
    """When even the unfiltered retry fails, the type is still a partial failure."""
    data_req = {
        "resourceType": "Library",
        "dataRequirement": [
            {"type": "Condition", "codeFilter": [{"path": "code", "valueSet": "http://vs/1"}]},
            {"type": "Encounter"},
        ],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, data_req)
        if "Patient/p1" in url and "Condition" not in url and "Encounter" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "Condition" in url:
            return _make_response(500, {})
        if "Encounter" in url:
            return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})
        return _make_response(404, {})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert [f.resource_type for f in result.failed_types] == ["Condition"]


# --- #455: per-type patient scope parameter -------------------------------
#
# The gather hardcoded `subject=Patient/{id}` for every resource type. Seven
# types reject `subject` and need `patient`; one (AdverseEvent) is the
# reverse; four accept neither and cannot be scoped to a patient at all.
# Measured against HAPI 8.8.0 — see issue #455 for the full probe table.


async def _capture_gather(data_req_response: dict) -> tuple[list[str], GatherResult]:
    """Run a gather against mocked CDR responses; return every URL and the result.

    The Patient direct read answers 200 deliberately: a 404 there puts every
    caller into a partial-failure state before its own assertions run, so a test
    that only inspects URLs would still pass if the gather had additionally
    broken resource collection. Returning the `GatherResult` lets callers prove
    the query actually succeeded, not merely that it was spelled correctly.
    """
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}
    captured_urls: list[str] = []

    async def mock_get(url, **kwargs):
        captured_urls.append(url)
        if "$data-requirements" in url:
            return _make_response(200, data_req_response)
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        return _make_response(200, empty_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    return captured_urls, result


async def test_fetch_by_requirements_coverage_is_scoped_by_patient_param():
    """Coverage rejects `subject=` — it must be scoped with `patient=` (#455).

    Witnessed in job 18: every one of 66 patients lost its Coverage, and with it
    the `SDE Payer` supplemental data, because the gather sent `subject=`.
    """
    urls, result = await _capture_gather({"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})

    assert result.failed_types == [], (
        f"the Coverage query must succeed, not just be spelled correctly: "
        f"{[(f.resource_type, f.error) for f in result.failed_types]}"
    )
    coverage_urls = [u for u in urls if "/Coverage?" in u]
    assert coverage_urls, "Coverage was never requested"
    for url in coverage_urls:
        assert "patient=Patient/p1" in url, f"Coverage must be scoped by patient=: {url}"
        assert "subject=" not in url, f"Coverage rejects subject=: {url}"


async def test_fetch_by_requirements_adverse_event_is_scoped_by_subject_param():
    """AdverseEvent is the inverse case: it accepts `subject=` and rejects `patient=`.

    Guards against "fix" #455 by swapping every type to `patient=` — that would
    break this type. The scope parameter is per-type, not global.
    """
    urls, result = await _capture_gather({"resourceType": "Library", "dataRequirement": [{"type": "AdverseEvent"}]})

    ae_urls = [u for u in urls if "/AdverseEvent?" in u]
    assert ae_urls, "AdverseEvent was never requested"
    for url in ae_urls:
        assert "subject=Patient/p1" in url, f"AdverseEvent must be scoped by subject=: {url}"
        assert "patient=" not in url, f"AdverseEvent rejects patient=: {url}"


async def test_fetch_by_requirements_immunization_and_claim_use_patient_param():
    """The other types measured as rejecting `subject=` are scoped with `patient=` (#455)."""
    urls, result = await _capture_gather(
        {
            "resourceType": "Library",
            "dataRequirement": [
                {"type": "Immunization"},
                {"type": "AllergyIntolerance"},
                {"type": "Claim"},
                {"type": "FamilyMemberHistory"},
                {"type": "NutritionOrder"},
                {"type": "Device"},
            ],
        }
    )

    for resource_type in (
        "Immunization",
        "AllergyIntolerance",
        "Claim",
        "FamilyMemberHistory",
        "NutritionOrder",
        "Device",
    ):
        type_urls = [u for u in urls if f"/{resource_type}?" in u]
        assert type_urls, f"{resource_type} was never requested"
        for url in type_urls:
            assert "patient=Patient/p1" in url, f"{resource_type} must use patient=: {url}"
            assert "subject=" not in url, f"{resource_type} rejects subject=: {url}"


async def test_fetch_by_requirements_condition_keeps_subject_param():
    """Types that accept both parameters keep `subject=` — the fix changes nothing for them."""
    urls, result = await _capture_gather({"resourceType": "Library", "dataRequirement": [{"type": "Condition"}]})

    assert result.failed_types == [], (
        f"the Condition query must succeed, not just be spelled correctly: "
        f"{[(f.resource_type, f.error) for f in result.failed_types]}"
    )
    condition_urls = [u for u in urls if "/Condition?" in u]
    assert condition_urls, "Condition was never requested"
    for url in condition_urls:
        assert "subject=Patient/p1" in url, f"Condition should still use subject=: {url}"


async def test_fetch_by_requirements_unscopable_type_is_skipped_not_requested():
    """Medication has no patient-scoped search parameter — don't request it at all (#455).

    `subject=` and `patient=` both 400. Requesting it guarantees a failure on
    every patient of every job, which inflates failed_types and makes a real
    fetch failure indistinguishable from a structural one.
    """
    urls, result = await _capture_gather(
        {
            "resourceType": "Library",
            "dataRequirement": [{"type": "Medication"}, {"type": "Condition"}],
        }
    )

    assert not [u for u in urls if "/Medication?" in u], (
        "Medication cannot be scoped to a patient and must not be requested"
    )
    assert [u for u in urls if "/Condition?" in u], "Condition should still be requested"


async def test_fetch_by_requirements_unscopable_type_reports_a_distinct_reason():
    """A skipped unscopable type is reported, and says why — not as a fetch failure (#455)."""
    empty_bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(
                200,
                {"resourceType": "Library", "dataRequirement": [{"type": "Medication"}, {"type": "Condition"}]},
            )
        if "/Patient/" in url and "?" not in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        return _make_response(200, empty_bundle)

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    medication_failures = [f for f in result.failed_types if f.resource_type == "Medication"]
    assert len(medication_failures) == 1, "the skipped type should be reported exactly once"
    assert "no patient-scoped search parameter" in medication_failures[0].error, (
        f"the reason must distinguish a structural skip from a fetch failure: {medication_failures[0].error!r}"
    )


# --- #455 gap coverage: scope-param interactions and the unscopable fallback ---


def test_patient_scope_param_table_is_internally_consistent():
    """The override table, the default, and the unscopable tuple must not overlap.

    A type listed as unscopable but ALSO given a `patient=` override would be
    unreachable dead config today, and would silently start being requested the
    day the `elif` branch is reordered. Likewise an override that maps back to
    the default is noise that hides a real typo.
    """
    for resource_type in _PATIENT_UNSCOPABLE_TYPES:
        assert resource_type not in _PATIENT_SCOPE_PARAM_OVERRIDES, (
            f"{resource_type} cannot be both unscopable and have a scope-param override"
        )
    for resource_type, param in _PATIENT_SCOPE_PARAM_OVERRIDES.items():
        assert param != _DEFAULT_PATIENT_SCOPE_PARAM, f"{resource_type} override restates the default"
        assert _patient_scope_param(resource_type) == param
    assert _patient_scope_param("Encounter") == _DEFAULT_PATIENT_SCOPE_PARAM
    assert _patient_scope_param("AdverseEvent") == "subject"


async def test_fetch_by_requirements_only_unscopable_types_falls_back_to_everything():
    """A measure whose every requirement is unscopable must fall back to $everything (#455).

    The skip adds the type to `failed_type_names`, so `failed_type_names ==
    required_types` and the outer RuntimeError fires. That is the ONLY remaining
    route to this data — $everything walks the compartment and returns resources
    reached by reference. Dropping the type from `failed_type_names` (tempting,
    since the skip is deliberate rather than a fetch failure) would return an
    empty gather and report success.
    """
    everything_bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Medication", "id": "med1"}}],
        "link": [],
    }

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Library",
                    "dataRequirement": [{"type": "Medication"}, {"type": "Organization"}],
                },
            )
        if "$everything" in url:
            return _make_response(200, everything_bundle)
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    requested = [str(c) for c in mock_ctx.get.call_args_list]
    assert len([u for u in requested if "$everything" in u]) == 1, "the unscopable-only measure must fall back"
    assert [r["resourceType"] for r in result.resources] == ["Medication"], (
        "the fallback's resources must be what the caller receives"
    )
    assert result.failed_types == [], "the fallback result supersedes the per-type skip reports"


async def test_fetch_by_requirements_unscopable_alongside_scopable_does_not_fall_back():
    """One unscopable type among scopable ones is a partial gather, NOT a fallback (#455).

    `failed_type_names` is a strict subset of `required_types`, so the outer
    RuntimeError must not fire — re-fetching the whole compartment because one
    structurally-unfetchable type was declared would undo the point of
    $data-requirements narrowing.
    """

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Library",
                    "dataRequirement": [{"type": "Medication"}, {"type": "Condition"}],
                },
            )
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        return _make_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": {"resourceType": "Condition", "id": "c1"}}],
                "link": [],
            },
        )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    requested = [str(c) for c in mock_ctx.get.call_args_list]
    assert not [u for u in requested if "$everything" in u], "a partial gather must not trigger the fallback"
    assert {r["resourceType"] for r in result.resources} == {"Patient", "Condition"}
    assert [f.resource_type for f in result.failed_types] == ["Medication"]


@pytest.mark.parametrize("unscopable", _PATIENT_UNSCOPABLE_TYPES)
async def test_fetch_by_requirements_every_unscopable_type_is_skipped_and_reported(unscopable):
    """All four unscopable types behave identically — not just the one spot-checked (#455).

    Parametrized rather than looped so each type fails independently and is named
    in the test id, and so the case set tracks `_PATIENT_UNSCOPABLE_TYPES` if a
    fifth type is ever added.
    """

    async def mock_get(url, _t=unscopable, **kwargs):
        if "$data-requirements" in url:
            return _make_response(
                200,
                {"resourceType": "Library", "dataRequirement": [{"type": _t}, {"type": "Condition"}]},
            )
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    requested = [str(c) for c in mock_ctx.get.call_args_list]
    assert not [u for u in requested if f"/{unscopable}?" in u], f"{unscopable} must never be requested"
    assert [f.resource_type for f in result.failed_types] == [unscopable]
    assert "no patient-scoped search parameter" in result.failed_types[0].error


async def test_fetch_by_requirements_overridden_type_keeps_patient_param_with_code_filter():
    """The `patient=` override must survive the `code:in=` filtered query (#455).

    Coverage carrying a valueset is exactly the SDE Payer shape that regressed —
    the filter and the scope parameter are composed into one query string, and
    getting either wrong loses the same data.
    """
    seen: list[str] = []

    async def mock_get(url, **kwargs):
        seen.append(url)
        if "$data-requirements" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Library",
                    "dataRequirement": [
                        {
                            "type": "Coverage",
                            "codeFilter": [{"path": "type", "valueSet": "http://example.org/ValueSet/payer"}],
                        }
                    ],
                },
            )
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        return _make_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": {"resourceType": "Coverage", "id": "cov1"}}],
                "link": [],
            },
        )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    filtered = [u for u in seen if "/Coverage?" in u and "code:in=" in u]
    assert filtered, "the single-valueset filter should still be applied to Coverage"
    for url in filtered:
        assert "patient=Patient/p1" in url, f"the filtered Coverage query must keep patient=: {url}"
        assert "subject=" not in url, f"Coverage rejects subject= even when filtered: {url}"
    assert result.failed_types == []
    assert "Coverage" in {r["resourceType"] for r in result.resources}


async def test_fetch_by_requirements_overridden_type_keeps_patient_param_on_unfiltered_retry():
    """When the filtered query fails, the unfiltered retry must reuse `patient=` (#455).

    The retry rebuilds the query from the same `base_params`; if the scope
    parameter were recomputed (or defaulted) on the retry path, the recovery
    would 400 and the type would be dropped after all.
    """
    seen: list[str] = []

    async def mock_get(url, **kwargs):
        seen.append(url)
        if "$data-requirements" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Library",
                    "dataRequirement": [
                        {
                            "type": "Coverage",
                            "codeFilter": [{"path": "type", "valueSet": "http://cts.nlm.nih.gov/fhir/ValueSet/9.9.9"}],
                        }
                    ],
                },
            )
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "code:in=" in url:
            return _make_response(400, {"resourceType": "OperationOutcome"})
        return _make_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": {"resourceType": "Coverage", "id": "cov1"}}],
                "link": [],
            },
        )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    coverage_urls = [u for u in seen if "/Coverage?" in u]
    assert len(coverage_urls) == 2, f"expected a filtered attempt then an unfiltered retry: {coverage_urls}"
    for url in coverage_urls:
        assert "patient=Patient/p1" in url, f"both attempts must carry patient=: {url}"
        assert "subject=" not in url, f"neither attempt may use subject=: {url}"
    assert "Coverage" in {r["resourceType"] for r in result.resources}
    assert result.failed_types == [], "a recovered type must not be reported as a partial failure"


async def test_fetch_by_requirements_overridden_type_paginates():
    """Pagination of an overridden type collects every page (#455).

    The first page is built by us with `patient=`; later pages come from the
    CDR's own `next` link, so the scope parameter must not be re-derived.
    """
    seen: list[str] = []

    async def mock_get(url, **kwargs):
        seen.append(url)
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if "/Patient/" in url and "?" not in url:
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "page=2" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [{"resource": {"resourceType": "Coverage", "id": "cov2"}}],
                    "link": [],
                },
            )
        return _make_response(
            200,
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "entry": [{"resource": {"resourceType": "Coverage", "id": "cov1"}}],
                "link": [{"relation": "next", "url": "http://cdr/fhir?page=2"}],
            },
        )

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    first_page = next(u for u in seen if "/Coverage?" in u)
    assert first_page.endswith("patient=Patient/p1&_count=100"), first_page
    coverage_ids = sorted(r["id"] for r in result.resources if r["resourceType"] == "Coverage")
    assert coverage_ids == ["cov1", "cov2"], f"both pages should be collected: {coverage_ids}"
    assert result.failed_types == []


# --- #455 / F1: unscopable types are resolved by direct read ----------------
#
# `_PATIENT_UNSCOPABLE_TYPES` have no patient-scoped search parameter, so they
# cannot be FOUND by searching. They can still be READ by id. Gathered resources
# reference them (Coverage.payor -> Organization, Claim.provider -> Practitioner),
# and `$submit-data` is transaction-backed, so shipping the reference without the
# target fails the patient's entire submission on any server enforcing
# referential integrity — HTTP 400 HAPI-1094, nothing stored.


async def test_unscopable_references_are_resolved_by_direct_read():
    """A referenced Organization is fetched by id and included in the gather (#455 F1).

    Measured against stock hapiproject/hapi:v8.8.0-1 (referential integrity on by
    default): a transaction carrying Coverage.payor -> Organization/x without that
    Organization returns 400 and stores nothing at all.
    """
    reads: list[str] = []

    async def mock_get(url, **kwargs):
        reads.append(url)
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if url.endswith("/Patient/p1"):
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if url.endswith("/Organization/org1"):
            return _make_response(200, {"resourceType": "Organization", "id": "org1"})
        if "/Coverage?" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Coverage",
                                "id": "cov1",
                                "beneficiary": {"reference": "Patient/p1"},
                                "payor": [{"reference": "Organization/org1"}],
                            }
                        }
                    ],
                    "link": [],
                },
            )
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert any(u.endswith("/Organization/org1") for u in reads), (
        f"the referenced Organization must be read by id — it cannot be found by search: {reads}"
    )
    types = {r["resourceType"] for r in result.resources}
    assert "Organization" in types, (
        f"the referenced Organization must ship WITH the Coverage that references it, "
        f"or the transaction-backed submission fails the whole patient: {types}"
    )


async def test_unresolvable_reference_is_reported_not_silently_dropped():
    """A reference whose target does not exist anywhere is surfaced (#455 F1 / #456).

    This is the `Practitioner/example` case from job 18: no direct read can
    conjure a resource the CDR does not hold, so the honest outcome is a named
    warning, not a silent omission that fails the submission later.
    """

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if url.endswith("/Patient/p1"):
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "/Organization/missing" in url:
            return _make_response(404, {"resourceType": "OperationOutcome"})
        if "/Coverage?" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Coverage",
                                "id": "cov1",
                                "payor": [{"reference": "Organization/missing"}],
                            }
                        }
                    ],
                    "link": [],
                },
            )
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    unresolved = [f for f in result.failed_types if f.resource_type == "Organization"]
    assert unresolved, (
        f"an unresolvable reference must be reported: {[(f.resource_type, f.error) for f in result.failed_types]}"
    )
    assert "Organization/missing" in unresolved[0].error, unresolved[0].error


async def test_unscopable_reference_reads_are_cached_across_patients():
    """One strategy instance per job, so a shared Organization is read once (#455 F1)."""
    reads: list[str] = []

    def _coverage_for(pid: str) -> dict:
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Coverage",
                        "id": f"cov-{pid}",
                        "payor": [{"reference": "Organization/shared"}],
                    }
                }
            ],
            "link": [],
        }

    async def mock_get(url, **kwargs):
        reads.append(url)
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if "/Organization/shared" in url:
            return _make_response(200, {"resourceType": "Organization", "id": "shared"})
        if url.endswith("/Patient/p1"):
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if url.endswith("/Patient/p2"):
            return _make_response(200, {"resourceType": "Patient", "id": "p2"})
        if "/Coverage?" in url:
            return _make_response(200, _coverage_for("p1" if "p1" in url else "p2"))
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        first = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        second = await strategy.gather_patient_data("http://cdr/fhir", "p2", {})

    org_reads = [u for u in reads if "/Organization/shared" in u]
    assert len(org_reads) == 1, f"a shared infrastructure resource should be read once per job: {org_reads}"
    # Both patients still carry it — the cache must serve, not skip.
    for label, res in (("p1", first), ("p2", second)):
        assert "Organization" in {r["resourceType"] for r in res.resources}, (
            f"{label} lost its Organization to the cache"
        )


async def test_unscopable_reference_transport_error_is_reported_and_not_cached():
    """A transport fault reading a reference is reported, and is NOT cached as absent.

    Also pins the local `sanitize_error` import: `validation` imports this module,
    so a module-scope import is circular and this branch would NameError at
    runtime while every mocked test that never raises still passed.
    """
    attempts: list[str] = []

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if url.endswith("/Patient/p1") or url.endswith("/Patient/p2"):
            return _make_response(200, {"resourceType": "Patient", "id": url.rsplit("/", 1)[-1]})
        if "/Organization/flaky" in url:
            attempts.append(url)
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] host cdr.internal.example")
        if "/Coverage?" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Coverage",
                                "id": "cov1",
                                "payor": [{"reference": "Organization/flaky"}],
                            }
                        }
                    ],
                    "link": [],
                },
            )
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        await strategy.gather_patient_data("http://cdr/fhir", "p2", {})

    org_failures = [f for f in result.failed_types if f.resource_type == "Organization"]
    assert org_failures, "a transport fault reading a reference must be reported"
    assert "cdr.internal.example" not in org_failures[0].error, (
        f"the internal hostname must not reach a client-facing error: {org_failures[0].error!r}"
    )
    assert len(attempts) == 2, (
        f"a transport fault is not proof of absence and must be retried for the next patient: {attempts}"
    )


async def test_scoped_wipe_covers_every_type_the_gather_can_push():
    """The wipe must be able to clear anything the gather can fetch (#455 F9 / #392).

    The gather widened to seven previously-dropped types. Any of them that the
    scoped wipe cannot clear accumulates on a shared measure server with nothing
    to remove it — the exact harm #392 exists to prevent, and the reason this PR
    added Device/FamilyMemberHistory/NutritionOrder to `_PATIENT_SCOPED_TYPES`.
    """
    wipeable = {rt for rt, _ in _PATIENT_SCOPED_TYPES}
    gatherable = set(_PATIENT_SCOPE_PARAM_OVERRIDES)

    missing = sorted(gatherable - wipeable)
    assert not missing, (
        f"the gather can fetch and push {missing}, but the scoped wipe has no entry for them — "
        f"they would persist on a shared measure server with nothing to remove them"
    )


async def test_nutrition_order_is_swept_before_encounter():
    """Ordering, not membership: HAPI 409s deleting a referenced Encounter (#455 F9).

    Measured on the local CDR: `DELETE Encounter?patient=` answers 409 while a
    NutritionOrder still points at it, the sweep moves on, and the Encounter
    survives the wipe. NutritionOrder therefore has to be deleted first.
    """
    order = [rt for rt, _ in _PATIENT_SCOPED_TYPES]
    assert "NutritionOrder" in order and "Encounter" in order
    assert order.index("NutritionOrder") < order.index("Encounter"), (
        "NutritionOrder references Encounter, so it must be deleted first or the "
        "Encounter delete 409s and the resource survives a 'successful' wipe"
    )
    assert order[-1] == "Patient", "Patient must stay last — HAPI 409s while it is still referenced"


async def test_transient_http_failure_on_reference_read_is_not_cached_as_missing():
    """A 503 is not proof of absence — it must not poison the cache (#455).

    A 503 response does not raise from httpx, so it reaches the same branch as a
    404 unless the code distinguishes them. Negative-caching it would make every
    later patient skip the read and ship an unresolvable reference, long after
    the CDR recovered.
    """
    calls = {"n": 0}

    async def mock_get(url, **kwargs):
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if url.endswith("/Patient/p1") or url.endswith("/Patient/p2"):
            return _make_response(200, {"resourceType": "Patient", "id": url.rsplit("/", 1)[-1]})
        if "/Organization/flappy" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                return _make_response(503, {"resourceType": "OperationOutcome"})
            return _make_response(200, {"resourceType": "Organization", "id": "flappy"})
        if "/Coverage?" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Coverage",
                                "id": "cov1",
                                "payor": [{"reference": "Organization/flappy"}],
                            }
                        }
                    ],
                    "link": [],
                },
            )
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://mcs/fhir")
        await strategy.gather_patient_data("http://cdr/fhir", "p1", {})
        second = await strategy.gather_patient_data("http://cdr/fhir", "p2", {})

    assert calls["n"] == 2, "a 503 is not proof of absence — the next patient must retry the read"
    assert "Organization" in {r["resourceType"] for r in second.resources}, (
        "once the CDR recovered, the reference must resolve"
    )


async def test_absolute_reference_to_another_server_is_not_rebased_onto_the_cdr():
    """An off-origin absolute reference must not be fetched from our CDR (#455).

    Taking the last two path segments of
    `https://external.example/fhir/Organization/shared` yields `Organization/shared`,
    which the configured CDR may also hold — as a completely different
    organization. Shipping that into the patient's submission is silent data
    corruption, so it is reported instead.
    """
    reads: list[str] = []

    async def mock_get(url, **kwargs):
        reads.append(url)
        if "$data-requirements" in url:
            return _make_response(200, {"resourceType": "Library", "dataRequirement": [{"type": "Coverage"}]})
        if url.endswith("/Patient/p1"):
            return _make_response(200, {"resourceType": "Patient", "id": "p1"})
        if "/Organization/" in url:
            # The CDR happens to hold an unrelated Organization at the same id.
            return _make_response(200, {"resourceType": "Organization", "id": "shared", "name": "WRONG ORG"})
        if "/Coverage?" in url:
            return _make_response(
                200,
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "Coverage",
                                "id": "cov1",
                                "payor": [{"reference": "https://external.example/fhir/Organization/shared"}],
                            }
                        }
                    ],
                    "link": [],
                },
            )
        return _make_response(200, {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []})

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.get = AsyncMock(side_effect=mock_get)
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        strategy = DataRequirementsStrategy("m1", mcs_url="http://cdr/fhir")
        result = await strategy.gather_patient_data("http://cdr/fhir", "p1", {})

    assert not [u for u in reads if "/Organization/" in u], (
        f"an off-origin reference must not be fetched from our CDR: {reads}"
    )
    names = [r.get("name") for r in result.resources if r["resourceType"] == "Organization"]
    assert "WRONG ORG" not in names, "an unrelated local Organization was shipped for an external reference"
    reported = [f for f in result.failed_types if f.resource_type == "Organization"]
    assert reported, "the unsupported external reference must be reported, not silently ignored"

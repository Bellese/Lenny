"""Unit tests for groups-feature fhir_client helpers (issue #322)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.fhir_client import list_groups_with_expression

pytestmark = pytest.mark.asyncio

CQL_EXTENSION_URL = "http://hl7.org/fhir/StructureDefinition/characteristicExpression"

_DUMMY_REQUEST = httpx.Request("GET", "http://test")


def _make_response(status_code: int, json_data: dict) -> httpx.Response:
    return httpx.Response(status_code, json=json_data, request=_DUMMY_REQUEST)


def _bundle(entries: list[dict]) -> dict:
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in entries], "link": []}


def _patch_async_client(response: httpx.Response):
    """Return a context manager that patches httpx.AsyncClient to return `response`."""
    patcher = patch("app.services.fhir_client.httpx.AsyncClient")
    mock_httpx = patcher.start()
    mock_ctx = AsyncMock()
    mock_ctx.get = AsyncMock(return_value=response)
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher


async def test_list_filters_to_cql_evaluatable_groups():
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "CQL Group",
        "type": "person",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {
                    "language": "text/cql-expression",
                    "expression": "Patient.active",
                },
            }
        ],
    }
    plain_group = {"resourceType": "Group", "id": "g2", "name": "Plain", "type": "person"}
    wrong_lang = {
        "resourceType": "Group",
        "id": "g3",
        "name": "Wrong Lang",
        "type": "person",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {"language": "text/fhirpath", "expression": "Patient.active"},
            }
        ],
    }
    other_extension = {
        "resourceType": "Group",
        "id": "g4",
        "name": "Other Ext",
        "type": "person",
        "extension": [{"url": "http://example.org/other", "valueString": "noop"}],
    }

    response = _make_response(200, _bundle([cql_group, plain_group, wrong_lang, other_extension]))
    patcher = _patch_async_client(response)
    try:
        out = await list_groups_with_expression("http://cdr.example", {})
    finally:
        patcher.stop()

    assert len(out) == 1
    g = out[0]
    assert g["id"] == "g1"
    assert g["name"] == "CQL Group"
    assert g["type"] == "person"
    assert g["expression_language"] == "text/cql-expression"
    assert g["expression_preview"].startswith("Patient.active")


async def test_list_truncates_long_expressions():
    long_expr = "Patient." + ("x" * 500)
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "Long",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {"language": "text/cql-expression", "expression": long_expr},
            }
        ],
    }
    response = _make_response(200, _bundle([cql_group]))
    patcher = _patch_async_client(response)
    try:
        out = await list_groups_with_expression("http://cdr.example", {})
    finally:
        patcher.stop()

    assert len(out[0]["expression_preview"]) <= 123  # 120 chars + ellipsis "..."
    assert out[0]["expression_preview"].endswith("...")


async def test_list_accepts_text_cql_identifier_language():
    cql_group = {
        "resourceType": "Group",
        "id": "g1",
        "name": "Ident",
        "extension": [
            {
                "url": CQL_EXTENSION_URL,
                "valueExpression": {
                    "language": "text/cql-identifier",
                    "expression": "InEligible",
                },
            }
        ],
    }
    response = _make_response(200, _bundle([cql_group]))
    patcher = _patch_async_client(response)
    try:
        out = await list_groups_with_expression("http://cdr.example", {})
    finally:
        patcher.stop()

    assert len(out) == 1
    assert out[0]["expression_language"] == "text/cql-identifier"

"""Unit tests for /api/groups endpoints (issue #322)."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


async def _enable_groups(client: AsyncClient) -> None:
    await client.put("/settings/admin", json={"groups_enabled": True})


@pytest.mark.asyncio
async def test_list_groups_404_when_feature_disabled(client: AsyncClient):
    # Default is disabled.
    resp = await client.get("/api/groups")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_groups_happy(client: AsyncClient):
    await _enable_groups(client)
    fake_groups = [
        {
            "id": "g1",
            "name": "Active Adults",
            "type": "person",
            "expression_language": "text/cql-expression",
            "expression_preview": "Patient.active",
        }
    ]
    with patch(
        "app.routes.groups.list_groups_with_expression",
        new=AsyncMock(return_value=fake_groups),
    ):
        resp = await client.get("/api/groups")
    assert resp.status_code == 200
    assert resp.json() == {"groups": fake_groups}


@pytest.mark.asyncio
async def test_list_groups_502_when_cdr_unreachable(client: AsyncClient):
    await _enable_groups(client)
    with patch(
        "app.routes.groups.list_groups_with_expression",
        new=AsyncMock(side_effect=Exception("connection refused")),
    ):
        resp = await client.get("/api/groups")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_evaluate_404_when_feature_disabled(client: AsyncClient):
    resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evaluate_happy(client: AsyncClient):
    await _enable_groups(client)
    fake_result = {
        "group_id": "g1",
        "evaluated_at": "2026-05-17T14:32:01Z",
        "member_count": 1,
        "members": [
            {
                "id": "p1",
                "name": "Smith, John",
                "gender": "male",
                "birth_date": "1980-04-12",
            }
        ],
    }
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 200
    assert resp.json() == fake_result


@pytest.mark.asyncio
async def test_evaluate_passes_operation_outcome_through(client: AsyncClient):
    from app.services.fhir_client import GroupEvaluateError

    await _enable_groups(client)
    outcome = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-supported", "diagnostics": "no $evaluate"}],
    }
    err = GroupEvaluateError("nope", status_code=400, operation_outcome=outcome)
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(side_effect=err),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 502
    assert resp.json()["operation_outcome"] == outcome


@pytest.mark.asyncio
async def test_evaluate_timeout_returns_504(client: AsyncClient):
    import httpx

    await _enable_groups(client)
    with patch(
        "app.routes.groups.evaluate_group_and_resolve_members",
        new=AsyncMock(side_effect=httpx.TimeoutException("slow")),
    ):
        resp = await client.post("/api/groups/g1/evaluate")
    assert resp.status_code == 504


@pytest.mark.asyncio
async def test_group_id_must_be_safe(client: AsyncClient):
    await _enable_groups(client)
    # An ID with characters outside [A-Za-z0-9_\-\.] must be rejected by the
    # route's validator before any CDR call. (We can't use ``..%2Fevil`` here
    # because httpx/Starlette decode ``%2F`` before path matching, so the
    # route doesn't even match and we get a 404 from the router.)
    resp = await client.post("/api/groups/bad$id/evaluate")
    assert resp.status_code in (400, 422)

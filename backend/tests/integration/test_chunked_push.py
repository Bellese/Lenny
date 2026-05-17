"""Integration: push_resources chunking against a CDR that enforces a per-bundle entry cap.

This test pins the Firely-Sandbox failure mode (HTTP 400 + "Too many entries
in bundle. Max supported number of entries is N") and verifies that setting
max_bundle_entries on the connection allows the same total payload to land
across N chunks.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.fhir_client import BundleUploadResult, push_resources

# ---------------------------------------------------------------------------
# Override the session-scoped autouse fixture from integration/conftest.py so
# these mock-based tests can run without any Docker infrastructure.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _require_infrastructure():  # noqa: PT004  (overrides conftest autouse fixture)
    """No-op override (function scope): these tests use httpx mocks and need
    no running HAPI. Function scope keeps this override local to this module
    so the conftest's session-scoped fixture isn't shadowed for other files
    when the full integration suite runs in one pytest invocation.
    """


@pytest.fixture(autouse=True)
def _load_seed_data():  # noqa: PT004  (overrides conftest autouse fixture)
    """No-op override (function scope): these tests use httpx mocks and need
    no seed data. See _require_infrastructure for scope rationale.
    """


def _make_response(status_code, body):
    class _Resp:
        def __init__(self, sc, b):
            self.status_code = sc
            self._b = b

        def json(self):
            return self._b

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError("error", request=None, response=self)

    return _Resp(status_code, body)


@pytest.mark.integration
async def test_chunked_push_succeeds_against_capped_sandbox():
    """Sandbox enforces 200-entry cap. Unchunked push (size=300) would fail;
    chunked push at max=200 succeeds."""
    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(300)]

    async def fake_post(url, *, json, headers):
        n = len(json["entry"])
        if n > 200:
            body = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "details": {"text": "Too many entries in bundle. Max supported number of entries is 200"},
                    }
                ],
            }
            return _make_response(400, body)
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

        result = await push_resources(resources, max_bundle_entries=200)

    assert isinstance(result, BundleUploadResult)
    assert len(result.succeeded) == 300
    assert len(result.failed) == 0
    assert mock_ctx.post.call_count == 2  # chunks of size 200 + 100


@pytest.mark.integration
async def test_unchunked_push_against_capped_sandbox_raises():
    """Demonstrates the regression we're fixing: with max_bundle_entries=None,
    a >200-entry push raises FhirOperationError on the sandbox cap."""
    from app.services.fhir_errors import FhirOperationError

    resources = [{"resourceType": "Patient", "id": f"p{i}"} for i in range(300)]

    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "exception",
                "details": {"text": "Too many entries in bundle. Max supported number of entries is 200"},
            }
        ],
    }

    with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
        mock_ctx = AsyncMock()
        mock_ctx.post = AsyncMock(return_value=_make_response(400, body))
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(FhirOperationError):
            await push_resources(resources, max_bundle_entries=None)

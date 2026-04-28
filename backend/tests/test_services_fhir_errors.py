"""Unit tests for app.services.fhir_errors."""

import httpx

from app.services.fhir_errors import (
    HINT_BY_STATUS,
    FhirIssue,
    FhirOperationError,
    FhirOperationOutcome,
    build_error_envelope,
    hint_for_network_exception,
    redact_outcome,
    sanitize_url,
)

# ---------------------------------------------------------------------------
# FhirOperationOutcome.from_dict
# ---------------------------------------------------------------------------


def test_from_dict_parses_issues():
    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {"severity": "error", "code": "not-found", "diagnostics": "Measure not found."},
            {"severity": "warning", "code": "processing", "diagnostics": "Partial result."},
        ],
    }
    oo = FhirOperationOutcome.from_dict(body)
    assert len(oo.issues) == 2
    assert oo.issues[0].severity == "error"
    assert oo.issues[0].code == "not-found"
    assert oo.issues[0].diagnostics == "Measure not found."
    assert oo.issues[1].severity == "warning"


def test_from_dict_empty_issues():
    oo = FhirOperationOutcome.from_dict({"resourceType": "OperationOutcome", "issue": []})
    assert oo.issues == []


def test_from_dict_preserves_raw():
    body = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "exception"}]}
    oo = FhirOperationOutcome.from_dict(body)
    assert oo.raw is body


def test_primary_diagnostic_first_non_null():
    oo = FhirOperationOutcome(
        issues=[
            FhirIssue(severity="error", code="exception", diagnostics=None),
            FhirIssue(severity="error", code="exception", diagnostics="Real error"),
        ],
        raw={},
    )
    assert oo.primary_diagnostic() == "Real error"


def test_primary_diagnostic_none_when_all_null():
    oo = FhirOperationOutcome(
        issues=[FhirIssue(severity="error", code="exception", diagnostics=None)],
        raw={},
    )
    assert oo.primary_diagnostic() is None


# ---------------------------------------------------------------------------
# FhirOperationOutcome.from_response
# ---------------------------------------------------------------------------


def test_from_response_parses_oo():
    body = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "security"}]}
    resp = httpx.Response(401, json=body)
    oo = FhirOperationOutcome.from_response(resp)
    assert oo is not None
    assert oo.issues[0].code == "security"


def test_from_response_returns_none_for_non_fhir():
    resp = httpx.Response(500, text="Internal Server Error")
    assert FhirOperationOutcome.from_response(resp) is None


def test_from_response_returns_none_for_measure_report():
    resp = httpx.Response(200, json={"resourceType": "MeasureReport", "status": "complete"})
    assert FhirOperationOutcome.from_response(resp) is None


# ---------------------------------------------------------------------------
# FhirOperationError
# ---------------------------------------------------------------------------


def test_fhir_operation_error_message_includes_status():
    exc = FhirOperationError(
        operation="evaluate-measure",
        url="http://mcs/fhir",
        status_code=404,
        outcome=None,
        latency_ms=50,
    )
    assert "404" in str(exc)
    assert "evaluate-measure" in str(exc)


def test_fhir_operation_error_uses_outcome_diagnostic():
    body = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "not-found", "diagnostics": "Measure xyz not found"}],
    }
    oo = FhirOperationOutcome.from_dict(body)
    exc = FhirOperationError(
        operation="evaluate-measure",
        url="http://mcs/fhir",
        status_code=404,
        outcome=oo,
        latency_ms=50,
    )
    assert "Measure xyz not found" in str(exc)


def test_fhir_operation_error_stores_fields():
    exc = FhirOperationError(
        operation="push-resources",
        url="http://hapi/fhir",
        status_code=401,
        outcome=None,
        latency_ms=12,
    )
    assert exc.operation == "push-resources"
    assert exc.status_code == 401
    assert exc.latency_ms == 12


# ---------------------------------------------------------------------------
# hint_for_network_exception
# ---------------------------------------------------------------------------


def test_hint_connect_error():
    hint = hint_for_network_exception(httpx.ConnectError("refused"))
    assert "URL" in hint or "server" in hint.lower()


def test_hint_connect_timeout():
    hint = hint_for_network_exception(httpx.ConnectTimeout("timed out"))
    assert "timed out" in hint.lower() or "firewall" in hint.lower()


def test_hint_read_timeout():
    hint = hint_for_network_exception(httpx.ReadTimeout("read timeout"))
    assert "respond" in hint.lower()


def test_hint_unknown_exception():
    hint = hint_for_network_exception(ValueError("oops"))
    assert hint  # non-empty


# ---------------------------------------------------------------------------
# HINT_BY_STATUS
# ---------------------------------------------------------------------------


def test_hint_by_status_401():
    assert "bearer token" in HINT_BY_STATUS[401].lower() or "authentication" in HINT_BY_STATUS[401].lower()


def test_hint_by_status_404():
    assert "url" in HINT_BY_STATUS[404].lower() or "endpoint" in HINT_BY_STATUS[404].lower()


# ---------------------------------------------------------------------------
# sanitize_url
# ---------------------------------------------------------------------------


def test_sanitize_url_strips_credentials():
    url = "https://user:password@myhost.example.com/fhir"
    result = sanitize_url(url)
    assert "password" not in result
    assert "user" not in result
    assert "myhost.example.com" in result


def test_sanitize_url_keeps_plain_url():
    url = "https://example.com/fhir"
    assert sanitize_url(url) == "https://example.com/fhir"


def test_sanitize_url_preserves_port():
    url = "http://example.com:8080/fhir"
    result = sanitize_url(url)
    assert "8080" in result


# ---------------------------------------------------------------------------
# redact_outcome
# ---------------------------------------------------------------------------


def test_redact_outcome_strips_bearer_token():
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "security", "diagnostics": "Authorization: Bearer eyJabcdef.xyz.123"}],
    }
    redacted = redact_outcome(oo)
    diag = redacted["issue"][0]["diagnostics"]
    assert "eyJabcdef" not in diag
    assert "Bearer" in diag or "[redacted" in diag


def test_redact_outcome_strips_jwt_bare():
    """JWT embedded without 'Bearer' prefix should still be redacted."""
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "security",
                "diagnostics": (
                    "Token eyJhbGciOiJSUzI1NiJ9"
                    ".eyJzdWIiOiJ1c2VyIn0"
                    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c was rejected"
                ),
            }
        ],
    }
    redacted = redact_outcome(oo)
    diag = redacted["issue"][0]["diagnostics"]
    # The JWT must be gone — accept any [redacted*] placeholder
    assert "eyJhbGci" not in diag
    assert "[redacted" in diag


def test_redact_outcome_strips_jwt_without_auth_prefix():
    """JWT embedded mid-sentence with no auth keyword is stripped by _JWT_RE."""
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "security",
                "diagnostics": (
                    "Credential eyJhbGciOiJSUzI1NiJ9"
                    ".eyJzdWIiOiJ1c2VyIn0"
                    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c embedded here"
                ),
            }
        ],
    }
    redacted = redact_outcome(oo)
    diag = redacted["issue"][0]["diagnostics"]
    assert "eyJhbGci" not in diag


def test_redact_outcome_does_not_mutate_original():
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "exception", "diagnostics": "Authorization: Bearer secret"}],
    }
    original_diag = oo["issue"][0]["diagnostics"]
    redact_outcome(oo)
    assert oo["issue"][0]["diagnostics"] == original_diag


def test_redact_outcome_handles_none_diagnostics():
    oo = {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "exception"}]}
    result = redact_outcome(oo)
    assert result["issue"][0].get("diagnostics") is None


# ---------------------------------------------------------------------------
# build_error_envelope
# ---------------------------------------------------------------------------


def test_build_error_envelope_shape():
    envelope = build_error_envelope(
        operation="test-connection",
        url="https://example.com/fhir",
        status_code=401,
        outcome=None,
        latency_ms=55,
        hint="Re-check your bearer token.",
    )
    assert envelope["resourceType"] == "OperationOutcome"
    assert isinstance(envelope["issue"], list)
    assert len(envelope["issue"]) > 0
    ed = envelope["error_details"]
    assert ed["operation"] == "test-connection"
    assert ed["status_code"] == 401
    assert ed["latency_ms"] == 55
    assert ed["hint"] == "Re-check your bearer token."


def test_build_error_envelope_embeds_outcome_issues():
    body = {
        "resourceType": "OperationOutcome",
        "issue": [
            {"severity": "error", "code": "security", "diagnostics": "Unauthorized"},
            {"severity": "warning", "code": "processing", "diagnostics": "Partial"},
        ],
    }
    oo = FhirOperationOutcome.from_dict(body)
    envelope = build_error_envelope(
        operation="evaluate-measure",
        url="http://mcs/fhir",
        status_code=401,
        outcome=oo,
        latency_ms=10,
    )
    assert len(envelope["issue"]) == 2
    assert envelope["issue"][0]["diagnostics"] == "Unauthorized"
    assert envelope["error_details"]["raw_outcome"] is not None


def test_build_error_envelope_sanitizes_url_credentials():
    envelope = build_error_envelope(
        operation="test-connection",
        url="https://admin:secret@cdr.example.com/fhir",
        status_code=None,
        outcome=None,
        latency_ms=None,
    )
    assert "secret" not in envelope["error_details"]["url"]
    assert "admin" not in envelope["error_details"]["url"]


def test_build_error_envelope_no_creds_in_raw_outcome():
    body = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "security", "diagnostics": "Authorization: Bearer mysecrettoken"}],
    }
    oo = FhirOperationOutcome.from_dict(body)
    envelope = build_error_envelope(
        operation="test-connection",
        url="http://example.com/fhir",
        status_code=401,
        outcome=oo,
        latency_ms=5,
    )
    raw = envelope["error_details"]["raw_outcome"]
    assert "mysecrettoken" not in str(raw)

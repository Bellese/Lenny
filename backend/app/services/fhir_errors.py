"""FHIR error parsing and structured error envelope construction.

Owns: FhirIssue, FhirOperationOutcome, FhirOperationError, build_error_envelope,
redact_outcome, and the hint maps used by callers that need user-facing messages.
"""

import copy
import re
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

# Sanitization regexes — same patterns as validation.sanitize_error but applied
# to plain strings (no Exception wrapping needed here).
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HOSTPORT_RE = re.compile(r"\b[a-z0-9][a-z0-9]*(?:-[a-z0-9]+)+:\d{2,5}\b", re.IGNORECASE)
_AUTH_RE = re.compile(r"(Authorization|Bearer|Basic|password|token|secret)[=:\s]\S+", re.IGNORECASE)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _sanitize_str(s: str) -> str:
    """Apply URL/host/auth/JWT redactions to a plain string (max 2000 chars).

    Order is load-bearing: JWT regex runs before _AUTH_RE so bare JWT-shaped
    tokens (eyJ...) are redacted even when not preceded by "Bearer".
    """
    s = s[:2000]
    s = _URL_RE.sub("[url]", s)
    s = _HOSTPORT_RE.sub("[host]", s)
    s = _JWT_RE.sub("[redacted-jwt]", s)
    s = _AUTH_RE.sub(r"\1=[redacted]", s)
    return s


def sanitize_url(url: str) -> str:
    """Strip embedded credentials and internal Docker hostnames from a URL.

    https://user:pass@cdr.example.com/fhir → https://cdr.example.com/fhir
    http://hapi-fhir-measure:8080/fhir    → http://[host]/fhir
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        cleaned = urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        cleaned = url
    # Strip Docker-style internal hostnames (e.g. hapi-fhir-measure:8080)
    cleaned = _HOSTPORT_RE.sub("[host]", cleaned)
    return cleaned


@dataclass
class FhirIssue:
    severity: str  # "fatal" | "error" | "warning" | "information"
    code: str
    diagnostics: str | None
    location: list[str] = field(default_factory=list)
    expression: list[str] = field(default_factory=list)


@dataclass
class FhirOperationOutcome:
    issues: list[FhirIssue]
    raw: dict  # full original OO JSON, preserved

    @classmethod
    def from_dict(cls, body: dict) -> "FhirOperationOutcome":
        issues = []
        for issue in body.get("issue", []) or []:
            issues.append(
                FhirIssue(
                    severity=issue.get("severity", "error"),
                    code=issue.get("code", "exception"),
                    diagnostics=issue.get("diagnostics"),
                    location=issue.get("location") or [],
                    expression=issue.get("expression") or [],
                )
            )
        return cls(issues=issues, raw=body)

    @classmethod
    def from_response(cls, resp: httpx.Response) -> "FhirOperationOutcome | None":
        """Parse an OperationOutcome from an httpx Response. Returns None if not FHIR JSON."""
        try:
            body = resp.json()
        except Exception:
            return None
        if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
            return cls.from_dict(body)
        return None

    def primary_diagnostic(self) -> str | None:
        for issue in self.issues:
            if issue.diagnostics:
                return issue.diagnostics
        return None


class FhirOperationError(Exception):
    """Raised when a FHIR operation returns a structured error response."""

    def __init__(
        self,
        *,
        operation: str,
        url: str,
        status_code: int | None,
        outcome: FhirOperationOutcome | None,
        latency_ms: int | None,
        cause: BaseException | None = None,
    ):
        diag = outcome.primary_diagnostic() if outcome else None
        msg = diag or f"FHIR operation '{operation}' failed"
        if status_code:
            msg = f"HTTP {status_code}: {msg}"
        super().__init__(msg)
        self.operation = operation
        self.url = url
        self.status_code = status_code
        self.outcome = outcome
        self.latency_ms = latency_ms
        if cause is not None:
            self.__cause__ = cause


# HTTP status → user-facing hint
HINT_BY_STATUS: dict[int, str] = {
    401: "Authentication failed. Re-check your bearer token or username/password.",
    403: "Authenticated but not authorized. The user may lack permission.",
    404: "Endpoint not found. Verify the FHIR base URL.",
    405: "Method not allowed. The server may not support this operation.",
    408: "Server timed out. Retry, or increase the connection timeout.",
    429: "Rate limited. Wait a few seconds and retry.",
    500: "Server error. Check the server's logs.",
    502: "Upstream gateway error. The FHIR server may be restarting.",
    503: "Server unavailable. The FHIR server may be starting up.",
}


def hint_for_network_exception(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "Cannot reach the server. Check the URL is reachable and the server is running."
    if isinstance(exc, httpx.ConnectTimeout):
        return "Connection timed out. Check firewall / VPN / network."
    if isinstance(exc, httpx.ReadTimeout):
        return "The server accepted the connection but did not respond in time."
    if isinstance(exc, ssl.SSLError):
        reason = str(exc)[:80]
        return f"TLS/SSL error: {reason}. Check that the server's certificate is valid."
    return "Network error. Check the server is running and reachable."


def redact_outcome(oo: dict) -> dict:
    """Deep-copy an OperationOutcome and strip tokens from issue[].diagnostics.

    HAPI sometimes echoes failed request bodies into diagnostics — those may
    contain Authorization headers or bare JWT tokens.
    """
    redacted = copy.deepcopy(oo)
    for issue in redacted.get("issue", []) or []:
        diag = issue.get("diagnostics")
        if isinstance(diag, str):
            issue["diagnostics"] = _sanitize_str(diag)
    return redacted


def _fhir_code_for_status(status_code: int | None) -> str:
    if status_code in (401, 403):
        return "security"
    if status_code == 404:
        return "not-found"
    if status_code in (408, 429, 502, 503, 504):
        return "transient"
    return "exception"


def _issues_for_envelope(
    outcome: FhirOperationOutcome | None,
    status_code: int | None,
    hint: str | None,
) -> list[dict[str, Any]]:
    if outcome and outcome.issues:
        return [
            {
                k: v
                for k, v in {
                    "severity": i.severity,
                    "code": i.code,
                    "diagnostics": i.diagnostics,
                }.items()
                if v is not None
            }
            for i in outcome.issues
        ]
    code = _fhir_code_for_status(status_code)
    return [{"severity": "error", "code": code, "diagnostics": hint or "Operation failed."}]


def build_error_envelope(
    *,
    operation: str,
    url: str,
    status_code: int | None,
    outcome: FhirOperationOutcome | None,
    latency_ms: int | None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Return the structured dict for HTTPException(detail=...).

    Shape is back-compat: top-level resourceType/issue keep working for existing
    clients; error_details is additive.
    """
    issues = _issues_for_envelope(outcome, status_code, hint)
    raw_outcome = redact_outcome(outcome.raw) if outcome else None

    return {
        "resourceType": "OperationOutcome",
        "issue": issues,
        "error_details": {
            "operation": operation,
            "url": sanitize_url(url),
            "status_code": status_code,
            "latency_ms": latency_ms,
            "hint": hint,
            "raw_outcome": raw_outcome,
        },
    }

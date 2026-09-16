"""Tests for the structured JSON log formatter (main.JSONFormatter).

The formatter merges `extra=` fields from a hardcoded allowlist, so a
`logger.warning(..., extra={...})` whose keys are absent from that list is
silently stripped down to a bare message. Tests that assert on LogRecord
attributes cannot see this — only formatted output can.
"""

import json
import logging

from app.main import JSONFormatter


def _format(**extra) -> dict:
    record = logging.LogRecord(
        name="app.services.fhir_client",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JSONFormatter().format(record))


def test_emits_the_base_envelope():
    entry = _format()
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "app.services.fhir_client"
    assert entry["message"] == "something happened"


def test_emits_measure_identity_extras():
    """#452: the whitespace-version fallback is only actionable if the operator
    can see WHICH measure and WHICH version were rejected."""
    entry = _format(measure_id="EXMConnectathonSept2026Simple", measure_version="Draft based on 0.0.000")
    assert entry["measure_id"] == "EXMConnectathonSept2026Simple"
    assert entry["measure_version"] == "Draft based on 0.0.000"


def test_drops_keys_outside_the_allowlist():
    entry = _format(not_an_allowed_key="value")
    assert "not_an_allowed_key" not in entry

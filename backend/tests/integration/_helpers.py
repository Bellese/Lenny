"""Shared helper utilities for integration tests."""

import pytest


def fail_with_context(*, measure_id: str, patient: str, phase: str, expected, actual, likely_source: str) -> None:
    """Raise a pytest.fail with structured context for triage.

    Args:
        measure_id: Canonical measure URL or short measure ID.
        patient: Patient reference (e.g. "Patient/abc123" or bare ID).
        phase: Test phase where the failure occurred (e.g. "evaluate", "compare").
        expected: Expected value (populations dict or HTTP status code).
        actual: Actual value returned.
        likely_source: One of:
            - "mcs"     — evaluation divergence (MCS returned wrong counts)
            - "lenny"   — routing/storage bugs (Lenny uploaded wrong data)
            - "cdr"     — CapabilityStatement mismatches (CDR reachability)
            - "unknown" — other / unclassified failure
    """
    pytest.fail(
        f"FAIL [measure={measure_id}] [patient={patient}] "
        f"[phase={phase}] [source={likely_source}]\n"
        f"  expected: {expected}\n  actual: {actual}"
    )

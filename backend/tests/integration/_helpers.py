"""Shared helper utilities for integration tests."""

import copy
import warnings
from typing import Any

import pytest


def fix_valueset_compose_for_hapi(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Patch ValueSets so HAPI can expand them.

    HAPI ignores the pre-computed ``expansion`` element and always re-expands
    ValueSets via their ``compose``.  MADiE/connectathon bundles often contain
    ValueSets with only ``expansion`` (no ``compose``), or with a ``compose``
    that references sub-ValueSets not loaded into HAPI.  In both cases HAPI
    produces empty expansions and CQL evaluation returns all-zero populations.

    Fix: for any ValueSet that has ``expansion`` and either lacks ``compose``
    or has ``compose`` with sub-ValueSet references, synthesise a ``compose``
    from the expansion codes grouped by code system.
    """
    result = []
    for r in resources:
        if r.get("resourceType") != "ValueSet" or "expansion" not in r:
            result.append(r)
            continue

        needs_fix = False
        if "compose" not in r:
            needs_fix = True
        else:
            has_vs_refs = any(inc.get("valueSet") for inc in r.get("compose", {}).get("include", []))
            if has_vs_refs:
                needs_fix = True

        if needs_fix:
            r = copy.deepcopy(r)
            codes_by_system: dict[str, list[dict[str, str]]] = {}

            def _flatten_contains(nodes: list[dict[str, Any]]) -> None:
                for ce in nodes:
                    sys = ce.get("system", "")
                    code = ce.get("code", "")
                    disp = ce.get("display", "")
                    if sys and code:
                        entry: dict[str, str] = {"code": code}
                        if disp:
                            entry["display"] = disp
                        codes_by_system.setdefault(sys, []).append(entry)
                    if ce.get("contains"):
                        _flatten_contains(ce["contains"])

            _flatten_contains(r["expansion"].get("contains", []))
            r["compose"] = {
                "include": [{"system": sys, "concept": codes} for sys, codes in codes_by_system.items()]
            }
        result.append(r)
    return result


def fix_library_deps_for_hapi(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Patch Library relatedArtifact dependency URLs to match actual Library URLs.

    MADiE bundles (≥ v0.4.x) ship Libraries whose canonical ``url`` is
    ``https://madie.cms.gov/Library/{name}`` but whose ``relatedArtifact.depends-on``
    entries reference ``http://ecqi.healthit.gov/ecqms/Library/{name}|{version}``.

    HAPI resolves Library dependencies by canonical URL lookup, so this mismatch
    causes every sub-library (FHIRHelpers, QICoreCommon, etc.) to be silently
    unresolvable — the CQL evaluation proceeds but with a broken library chain,
    returning IP=0 for every patient.

    Fix: rewrite any ``relatedArtifact.resource`` that starts with the ecqi
    prefix to use the ``madie.cms.gov`` prefix, which matches the Library ``url``
    field that was actually loaded into HAPI.
    """
    _ECQI_PREFIX = "http://ecqi.healthit.gov/ecqms/Library/"
    _MADIE_PREFIX = "https://madie.cms.gov/Library/"

    result = []
    for r in resources:
        if r.get("resourceType") != "Library":
            result.append(r)
            continue

        needs_fix = any(
            ra.get("type") == "depends-on" and ra.get("resource", "").startswith(_ECQI_PREFIX)
            for ra in r.get("relatedArtifact", [])
        )
        if not needs_fix:
            result.append(r)
            continue

        r = copy.deepcopy(r)
        for ra in r.get("relatedArtifact", []):
            dep_url = ra.get("resource", "")
            if ra.get("type") == "depends-on" and dep_url.startswith(_ECQI_PREFIX):
                tail = dep_url[len(_ECQI_PREFIX):]  # e.g. "FHIRHelpers|4.4.000"
                ra["resource"] = _MADIE_PREFIX + tail
        result.append(r)
    return result


def fix_duplicate_claim_ids(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign unique IDs to Claim resources that share a duplicate ID.

    MADiE v0.3.x CMS71 bundles export all Claim resources with the same ID.
    When loaded via PUT batch, only the last write survives — every other patient
    loses their Claim, so ``hasPrincipalDiagnosisOf()`` (QICore v6 fluent function
    that looks up principal diagnosis via Claim.diagnosis) returns null for 82 of
    83 patients and every patient evaluates to IP=0.

    Fix: detect Claims with a duplicate ID and replace the ID with a deterministic
    slug derived from the first encounter reference in the Claim's items.  Falls
    back to a sequential counter if no encounter reference is present.
    """
    from collections import Counter

    id_counts = Counter(r.get("id", "") for r in resources if r.get("resourceType") == "Claim")
    duplicates = {id_ for id_, n in id_counts.items() if n > 1}
    if not duplicates:
        return resources

    result = []
    seen: dict[str, int] = {}
    for r in resources:
        if r.get("resourceType") != "Claim" or r.get("id") not in duplicates:
            result.append(r)
            continue

        r = copy.deepcopy(r)
        original_id = r["id"]
        # Derive a unique ID from the first item → encounter reference
        enc_ref = ""
        for item in r.get("item", []):
            for enc in item.get("encounter", []):
                ref = enc.get("reference", "")
                if ref:
                    enc_ref = ref.split("/")[-1][:16]
                    break
            if enc_ref:
                break

        if enc_ref:
            new_id = f"claim-{enc_ref}"
        else:
            seen[original_id] = seen.get(original_id, 0) + 1
            new_id = f"{original_id}-{seen[original_id]}"

        r["id"] = new_id
        result.append(r)
    return result


def make_put_bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap resources in a FHIR batch bundle using PUT (idempotent).

    Uses batch (not transaction) so HAPI processes entries independently —
    test fixtures may reference resources (e.g. Practitioner) that are not
    included in the bundle, and transaction mode would reject those as
    referential integrity violations.

    Resources missing a ``resourceType`` or ``id`` field are silently dropped
    from the bundle entries; a warning is emitted so callers can catch bad
    bundle data early rather than producing wrong-but-passing test results.
    """
    filtered = [r for r in resources if "resourceType" in r and "id" in r]
    dropped = len(resources) - len(filtered)
    if dropped:
        warnings.warn(
            f"make_put_bundle: dropped {dropped} resource(s) missing 'id' — check bundle contents",
            stacklevel=2,
        )
    return {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {
                "resource": r,
                "request": {
                    "method": "PUT",
                    "url": f"{r['resourceType']}/{r['id']}",
                },
            }
            for r in filtered
        ],
    }


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

"""Validation service — bundle triage, expected result extraction, and measure validation.

Handles uploading test bundles (triaging resources to measure engine, CDR, and Lenny DB),
running validation against expected results, and comparing population counts.
"""

import asyncio
import base64
import copy
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import async_session
from app.models.config import CDRConfig
from app.models.validation import (
    BundleUpload,
    ExpectedResult,
    ValidationResult,
    ValidationRun,
    ValidationStatus,
)
from app.services.fhir_client import (
    BatchQueryStrategy,
    FhirOperationError,
    _build_auth_headers,
    evaluate_measure,
    push_resources,
    wait_for_valueset_expansion,
    wipe_patient_data,
)
from app.services.fhir_errors import redact_outcome

logger = logging.getLogger(__name__)

# Resource types that belong on the measure engine (measure definitions).
# Scanned against all 12 connectathon bundles (2026-04-19): PlanDefinition,
# ActivityDefinition, and Questionnaire are NOT present — not added.
_MEASURE_DEF_TYPES = {"Measure", "Library", "ValueSet", "CodeSystem"}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
# Matches Docker-style hyphenated service names followed by a port (e.g. hapi-fhir-cdr:8080).
# Requires at least one hyphen in the hostname to avoid false positives on "code:404" or "line:100".
_HOSTPORT_RE = re.compile(r"\b[a-z0-9][a-z0-9]*(?:-[a-z0-9]+)+:\d{2,5}\b", re.IGNORECASE)
_AUTH_RE = re.compile(r"(Authorization|Bearer|Basic|password|token|secret)[=:\s]\S+", re.IGNORECASE)


def sanitize_error(exc: Exception) -> str:
    """Return a sanitized exception message safe to store and return to clients.

    Strips embedded URLs, internal hostnames, auth headers, and credentials.
    Full details are logged server-side before this function is called.

    Regex application order is load-bearing: URL regex runs first (removes
    http://hostname:port), then _HOSTPORT_RE catches bare hostname:port without
    a scheme (common in httpx ConnectError messages).
    """
    try:
        msg = str(exc)[:2000]
    except Exception:
        msg = f"<{type(exc).__name__}: str() raised>"
    msg = _URL_RE.sub("[url]", msg)
    msg = _HOSTPORT_RE.sub("[host]", msg)
    msg = _AUTH_RE.sub(r"\1=[redacted]", msg)
    return msg


# ---------------------------------------------------------------------------
# Population extraction and comparison
# ---------------------------------------------------------------------------


def _extract_population_counts(measure_report: dict[str, Any]) -> dict[str, int]:
    """Parse a MeasureReport and return population counts with FHIR hyphenated keys.

    Returns e.g. {"initial-population": 1, "denominator": 1, "numerator": 0, ...}
    """
    populations: dict[str, int] = {}
    valid_codes = {
        "initial-population",
        "denominator",
        "denominator-exclusion",
        "numerator",
        "numerator-exclusion",
    }
    for group in measure_report.get("group", []):
        for pop in group.get("population", []):
            for coding in pop.get("code", {}).get("coding", []):
                code = coding.get("code", "")
                if code in valid_codes:
                    populations[code] = populations.get(code, 0) + pop.get("count", 0)
    return populations


def _extract_patient_name(patient_resource: dict[str, Any]) -> Optional[str]:
    """Extract a display name from a Patient FHIR resource."""
    for name_obj in patient_resource.get("name", []):
        parts = []
        given = name_obj.get("given", [])
        if given:
            parts.extend(given)
        family = name_obj.get("family")
        if family:
            parts.append(family)
        if parts:
            return " ".join(parts)
    return None


def compare_populations(expected: dict[str, int], actual: dict[str, int]) -> tuple[bool, list[str]]:
    """Compare expected vs actual population counts.

    Only compares codes present in expected. If a code is absent from actual,
    treat actual count as 0. Returns (passed, list_of_mismatched_codes).
    """
    mismatches: list[str] = []
    for code, expected_count in expected.items():
        actual_count = actual.get(code, 0)
        if expected_count != actual_count:
            mismatches.append(code)
    return (len(mismatches) == 0, mismatches)


async def _stop_or_delete_validation_run(validation_run_id: int) -> bool:
    """Return True when validation work should stop because the run was cancelled or deleted."""
    async with async_session() as session:
        run = await session.get(ValidationRun, validation_run_id)
        if not run:
            return True
        if run.delete_requested:
            await session.delete(run)
            await session.commit()
            return True
        return run.status == ValidationStatus.cancelled


# ---------------------------------------------------------------------------
# Test bundle parsing and expected result extraction
# ---------------------------------------------------------------------------


def _is_test_case_measure_report(resource: dict[str, Any]) -> bool:
    """Check if a MeasureReport represents a test case.

    Supports two formats:
    1. Modern: modifierExtension with cqfm-isTestCase valueBoolean=true
    2. Legacy DBCG connectathon: type=individual and status=complete
    """
    for ext in resource.get("modifierExtension", []):
        if (
            ext.get("url") == "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase"
            and ext.get("valueBoolean") is True
        ):
            return True
    return resource.get("type") == "individual" and resource.get("status") == "complete"


def _extract_test_case_info(
    measure_report: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Extract expected result info from a test case MeasureReport.

    Returns dict with measure_url, patient_ref, test_description,
    expected_populations, period_start, period_end. Or None if extraction fails.
    """
    measure_url = measure_report.get("measure")
    if not measure_url:
        return None

    # Extract patient reference from contained Parameters
    patient_ref = None
    for contained in measure_report.get("contained", []):
        if contained.get("resourceType") == "Parameters":
            for param in contained.get("parameter", []):
                if param.get("name") == "subject":
                    patient_ref = param.get("valueString")
                    break
            if patient_ref:
                break

    if not patient_ref:
        return None

    # Extract test description
    test_description = None
    for ext in measure_report.get("extension", []):
        if ext.get("url") == "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-testCaseDescription":
            test_description = ext.get("valueMarkdown") or ext.get("valueString")
            break

    # Extract expected populations
    expected_populations = _extract_population_counts(measure_report)

    # Extract measurement period
    period = measure_report.get("period", {})
    period_start = period.get("start", "")
    period_end = period.get("end", "")
    if not period_start or not period_end:
        return None

    return {
        "measure_url": measure_url,
        "patient_ref": patient_ref,
        "test_description": test_description,
        "expected_populations": expected_populations,
        "period_start": period_start,
        "period_end": period_end,
    }


def _classify_bundle_entries(
    bundle_json: dict[str, Any],
) -> tuple[list[dict], list[dict], list[dict[str, Any]]]:
    """Classify bundle entries into measure defs, clinical data, and test cases.

    Returns (measure_def_resources, clinical_resources, test_case_infos).
    """
    measure_defs: list[dict] = []
    clinical: list[dict] = []
    test_cases: list[dict[str, Any]] = []

    for entry in bundle_json.get("entry", []):
        resource = entry.get("resource")
        if not resource or "resourceType" not in resource:
            continue

        rt = resource["resourceType"]

        if rt in _MEASURE_DEF_TYPES:
            measure_defs.append(resource)
        elif rt == "MeasureReport" and _is_test_case_measure_report(resource):
            info = _extract_test_case_info(resource)
            if info:
                test_cases.append(info)
        elif rt != "MeasureReport":
            # Non-test-case MeasureReports are skipped; everything else is clinical
            clinical.append(resource)
        # Note: non-test-case MeasureReports fall through silently (expected)

    return measure_defs, clinical, test_cases


def _warn_unknown_bundle_types(bundle_json: dict[str, Any]) -> None:
    """Log a warning for resource types that are neither measure defs nor known clinical types.

    Runs once per bundle upload so unknown types are visible in logs rather than
    silently misrouted. Does not affect routing — call before _classify_bundle_entries.
    """
    # Known clinical types present in the active connectathon bundles or seed/patient-bundle.json
    _KNOWN_CLINICAL_TYPES = {
        "Patient",
        "Condition",
        "Observation",
        "Encounter",
        "Procedure",
        "MedicationRequest",
        "MedicationAdministration",
        "Immunization",
        "DiagnosticReport",
        "AllergyIntolerance",
        "AdverseEvent",
        "CarePlan",
        "CareTeam",
        "Goal",
        "ServiceRequest",
        "DeviceRequest",
        "Medication",
        "Task",
        "Coverage",
        "Claim",
        "Location",
        "Practitioner",
        "Organization",
    }
    seen_unknown: set[str] = set()
    for entry in bundle_json.get("entry", []):
        resource = entry.get("resource")
        if not resource or "resourceType" not in resource:
            continue
        rt = resource["resourceType"]
        if rt in seen_unknown:
            continue
        if rt not in _MEASURE_DEF_TYPES and rt not in _KNOWN_CLINICAL_TYPES and rt != "MeasureReport":
            seen_unknown.add(rt)
            logger.warning(
                "Unknown resource type in bundle — will route as clinical data",
                extra={"resourceType": rt},
            )


# ---------------------------------------------------------------------------
# Bundle upload processing
# ---------------------------------------------------------------------------


def _fix_valueset_compose_for_hapi(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Patch ValueSets so HAPI can expand them.

    HAPI ignores the pre-computed ``expansion`` element and always re-expands
    ValueSets via their ``compose``.  MADiE/connectathon bundles often contain
    ValueSets with only ``expansion`` (no ``compose``), or with a ``compose``
    that references sub-ValueSets not loaded into HAPI.  In both cases HAPI
    produces empty expansions and CQL evaluation returns all-zero populations.

    Fix: for any ValueSet that has ``expansion`` and either lacks ``compose``
    or has a ``compose`` whose include entries carry no explicit codes, synthesise
    a ``compose`` from the expansion codes grouped by code system.

    Must be applied on first load — HAPI's Terminology service caches compose in a
    separate DB table that is not overwritten by a later PUT.
    """
    result = []
    for r in resources:
        if r.get("resourceType") != "ValueSet" or "expansion" not in r:
            result.append(r)
            continue

        include = r.get("compose", {}).get("include", [])
        needs_fix = False
        if "compose" not in r:
            needs_fix = True
        elif not include:
            needs_fix = True
        else:
            total_concepts = sum(len(inc.get("concept", [])) for inc in include)
            has_vs_refs = any(inc.get("valueSet") for inc in include)
            has_filters = any(inc.get("filter") for inc in include)
            if has_vs_refs:
                needs_fix = True
            elif total_concepts == 0 and not has_filters:
                needs_fix = True  # bare CodeSystem refs with no explicit codes

        if needs_fix and r.get("expansion", {}).get("contains"):
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
            r["compose"] = {"include": [{"system": sys, "concept": codes} for sys, codes in codes_by_system.items()]}
        result.append(r)
    return result


def _fix_library_deps_for_hapi(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Patch MADiE Library dependency URLs so HAPI can resolve sub-libraries."""
    ecqi_prefix = "http://ecqi.healthit.gov/ecqms/Library/"
    madie_prefix = "https://madie.cms.gov/Library/"

    result = []
    for resource in resources:
        if resource.get("resourceType") != "Library":
            result.append(resource)
            continue

        needs_fix = any(
            artifact.get("type") == "depends-on" and artifact.get("resource", "").startswith(ecqi_prefix)
            for artifact in resource.get("relatedArtifact", [])
        )
        if not needs_fix:
            result.append(resource)
            continue

        resource = copy.deepcopy(resource)
        for artifact in resource.get("relatedArtifact", []):
            dep_url = artifact.get("resource", "")
            if artifact.get("type") == "depends-on" and dep_url.startswith(ecqi_prefix):
                artifact["resource"] = madie_prefix + dep_url[len(ecqi_prefix) :]
        result.append(resource)
    return result


def _fix_duplicate_claim_ids(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign unique IDs to duplicate Claim resources from affected MADiE bundles."""
    id_counts: dict[str, int] = {}
    for resource in resources:
        if resource.get("resourceType") == "Claim" and resource.get("id"):
            id_counts[resource["id"]] = id_counts.get(resource["id"], 0) + 1
    duplicates = {id_ for id_, count in id_counts.items() if count > 1}
    if not duplicates:
        return resources

    result = []
    seen: dict[str, int] = {}
    for resource in resources:
        if resource.get("resourceType") != "Claim" or resource.get("id") not in duplicates:
            result.append(resource)
            continue

        resource = copy.deepcopy(resource)
        original_id = resource["id"]
        enc_ref = ""
        for item in resource.get("item", []):
            for encounter in item.get("encounter", []):
                ref = encounter.get("reference", "")
                if ref:
                    enc_ref = ref.split("/")[-1][:16]
                    break
            if enc_ref:
                break

        seen[original_id] = seen.get(original_id, 0) + 1
        suffix = enc_ref or str(seen[original_id])
        resource["id"] = f"{original_id}-{suffix}"
        result.append(resource)
    return result


def _get_missing_valueset_stubs(bundle_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Return empty ValueSet stubs for ELM-declared ValueSets omitted by the bundle.

    HAPI's CQL engine raises ``Unknown ValueSet`` when an ELM-declared ValueSet
    is not present at all. An empty ValueSet makes membership checks evaluate
    false instead, which preserves patient-level comparison and matches the
    connectathon harness behavior for incomplete MADiE bundles.
    """
    elm_declared_urls: set[str] = set()
    bundled_urls: set[str] = set()

    for entry in bundle_json.get("entry", []):
        resource = entry.get("resource") or {}
        resource_type = resource.get("resourceType")

        if resource_type == "ValueSet" and resource.get("url"):
            bundled_urls.add(resource["url"])
            continue

        if resource_type != "Library":
            continue

        for content in resource.get("content", []):
            if content.get("contentType") != "application/elm+json" or not content.get("data"):
                continue
            try:
                elm = json.loads(base64.b64decode(content["data"]))
            except Exception:
                logger.warning(
                    "Unable to parse ELM JSON while scanning missing ValueSets",
                    extra={"library_id": resource.get("id")},
                )
                continue

            for valueset in elm.get("library", {}).get("valueSets", {}).get("def", []):
                if url := valueset.get("id"):
                    elm_declared_urls.add(url)

    stubs = []
    for url in sorted(elm_declared_urls - bundled_urls):
        stub_id = "stub-" + re.sub(r"[^A-Za-z0-9.-]", "-", url.removeprefix("http://").removeprefix("https://"))
        stubs.append(
            {
                "resourceType": "ValueSet",
                "id": stub_id[:64],
                "url": url,
                "status": "active",
            }
        )
    return stubs


def _get_codesystem_stubs_from_valuesets(
    resources: list[dict[str, Any]], bundle_json: dict[str, Any]
) -> list[dict[str, Any]]:
    """Create CodeSystem fragments for explicit ValueSet concepts when MADiE omits them."""
    existing_codesystems = {
        (resource.get("url"), resource.get("version"))
        for resource in resources
        if resource.get("resourceType") == "CodeSystem" and resource.get("url")
    }
    concepts_by_system: dict[str, dict[str, str]] = {}
    versions_by_system: dict[str, set[str | None]] = {}

    def add_concept(system: str | None, code: str | None, display: str | None = None) -> None:
        if not system or not code:
            return
        concepts_by_system.setdefault(system, {})
        concepts_by_system[system].setdefault(code, display or "")

    def scan_contains(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            add_concept(node.get("system"), node.get("code"), node.get("display"))
            if node.get("contains"):
                scan_contains(node["contains"])

    def scan_versions(value: Any) -> None:
        if isinstance(value, dict):
            system = value.get("system")
            if system:
                versions_by_system.setdefault(system, set()).add(value.get("version"))
            for child in value.values():
                scan_versions(child)
        elif isinstance(value, list):
            for child in value:
                scan_versions(child)

    scan_versions(bundle_json)

    for resource in resources:
        if resource.get("resourceType") != "ValueSet":
            continue
        for include in resource.get("compose", {}).get("include", []):
            system = include.get("system")
            for concept in include.get("concept", []):
                add_concept(system, concept.get("code"), concept.get("display"))
        scan_contains(resource.get("expansion", {}).get("contains", []))

    stubs = []
    for system, concepts in sorted(concepts_by_system.items()):
        versions = versions_by_system.get(system) or {None}
        for version in sorted(versions, key=lambda value: value or ""):
            if (system, version) in existing_codesystems:
                continue
            digest = hashlib.sha1(f"{system}|{version or ''}".encode("utf-8")).hexdigest()[:16]
            stub = {
                "resourceType": "CodeSystem",
                "id": f"generated-codesystem-{digest}",
                "url": system,
                "status": "active",
                "content": "complete",
                "concept": [
                    {"code": code, **({"display": display} if display else {})}
                    for code, display in sorted(concepts.items())
                ],
            }
            if version:
                stub["version"] = version
            stubs.append(stub)
    return stubs


async def _find_existing_valueset_id(
    url: str,
    client: httpx.AsyncClient,
    *,
    target_url: str | None = None,
) -> str | None:
    """Return the existing HAPI ValueSet resource ID for a canonical URL."""
    resp = await client.get(
        f"{target_url or settings.MEASURE_ENGINE_URL}/ValueSet",
        params={"url": url, "_count": 1, "_elements": "id,url"},
        headers={"Cache-Control": "no-cache", "Accept": "application/fhir+json"},
    )
    resp.raise_for_status()
    entries = resp.json().get("entry", [])
    if not entries:
        return None
    return entries[0].get("resource", {}).get("id")


async def _find_existing_codesystem_id(
    url: str,
    version: str | None,
    client: httpx.AsyncClient,
) -> str | None:
    """Return the existing HAPI CodeSystem resource ID for a canonical URL/version."""
    params: dict[str, str | int] = {"url": url, "_count": 50, "_elements": "id,url,version"}
    if version:
        params["version"] = version
    resp = await client.get(
        f"{settings.MEASURE_ENGINE_URL}/CodeSystem",
        params=params,
        headers={"Cache-Control": "no-cache", "Accept": "application/fhir+json"},
    )
    resp.raise_for_status()
    entries = resp.json().get("entry", [])
    for entry in entries:
        resource = entry.get("resource", {})
        resource_version = resource.get("version")
        if (version and resource_version == version) or (not version and not resource_version):
            return resource.get("id")
    return None


async def _delete_existing_valueset(existing_id: str, client: httpx.AsyncClient) -> None:
    """Delete a stale ValueSet so HAPI rebuilds terminology tables from patched compose."""
    resp = await client.delete(f"{settings.MEASURE_ENGINE_URL}/ValueSet/{existing_id}")
    if resp.status_code not in {200, 204, 404}:
        resp.raise_for_status()


async def _prepare_measure_support_resources(
    resources: list[dict[str, Any]],
    bundle_json: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prepare ValueSets/CodeSystems for HAPI upload.

    Adds missing ELM ValueSet stubs only when HAPI does not already have that URL.
    Existing ValueSets are handled according to VALUESET_RELOAD_MODE: delete stale
    resources before re-upload, or remap to the existing HAPI resource ID.
    """
    prepared = _fix_valueset_compose_for_hapi(resources)
    stubs = _get_missing_valueset_stubs(bundle_json)
    codesystem_stubs = _get_codesystem_stubs_from_valuesets(prepared, bundle_json)
    if codesystem_stubs:
        prepared = [*codesystem_stubs, *prepared]
        logger.info("Prepared generated CodeSystem stubs", extra={"count": len(codesystem_stubs)})

    async with httpx.AsyncClient(timeout=30.0) as client:
        if stubs:
            filtered_stubs = []
            for stub in stubs:
                url = stub.get("url")
                if not url:
                    continue
                if await _find_existing_valueset_id(url, client):
                    continue
                filtered_stubs.append(stub)
            if filtered_stubs:
                prepared = [*prepared, *filtered_stubs]
                logger.info("Prepared missing ValueSet stubs", extra={"count": len(filtered_stubs)})

        aligned = []
        deleted_valueset_urls: set[str] = set()
        for resource in prepared:
            if resource.get("resourceType") == "CodeSystem" and resource.get("id", "").startswith(
                "generated-codesystem-"
            ):
                existing_id = await _find_existing_codesystem_id(
                    resource["url"],
                    resource.get("version"),
                    client,
                )
                if existing_id and existing_id != resource.get("id"):
                    continue
                aligned.append(resource)
                continue

            if resource.get("resourceType") != "ValueSet" or not resource.get("url"):
                aligned.append(resource)
                continue
            existing_id = await _find_existing_valueset_id(resource["url"], client)
            if existing_id:
                logger.info(
                    "ValueSet reload",
                    extra={
                        "mode": settings.VALUESET_RELOAD_MODE,
                        "valueset_id": existing_id,
                        "valueset_url": resource["url"],
                    },
                )
                if settings.VALUESET_RELOAD_MODE == "delete":
                    if resource["url"] not in deleted_valueset_urls:
                        await _delete_existing_valueset(existing_id, client)
                        deleted_valueset_urls.add(resource["url"])
                elif existing_id != resource.get("id"):
                    resource = copy.deepcopy(resource)
                    resource["id"] = existing_id
            else:
                logger.info(
                    "ValueSet reload",
                    extra={"mode": settings.VALUESET_RELOAD_MODE, "valueset_id": None, "valueset_url": resource["url"]},
                )
            aligned.append(resource)

    return aligned


def _valueset_urls(resources: list[dict[str, Any]]) -> list[str]:
    """Return canonical ValueSet URLs from a resource list."""
    return [
        resource["url"] for resource in resources if resource.get("resourceType") == "ValueSet" and resource.get("url")
    ]


async def triage_test_bundle(
    bundle_json: dict[str, Any],
    filename: str,
    session: AsyncSession,
    *,
    progress_fn: Callable[[str, int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Triage a test bundle: send resources to their correct destinations.

    - Measure/Library/ValueSet → measure engine
    - MeasureReport (isTestCase) → ExpectedResult table
    - Patient/clinical data → active CDR (default or external)

    Returns summary dict with counts.
    """
    _warn_unknown_bundle_types(bundle_json)
    measure_defs, clinical, test_cases = _classify_bundle_entries(bundle_json)
    measure_defs = _fix_library_deps_for_hapi(measure_defs)
    clinical = _fix_duplicate_claim_ids(clinical)
    support_resources: list[dict[str, Any]] = []

    # Push measure definitions to measure engine in two phases so shared ValueSets
    # (which trigger HAPI-0902 on re-upload) never block the Measure/Library load.
    if measure_defs:
        primary = [r for r in measure_defs if r.get("resourceType") in ("Measure", "Library")]
        secondary = [r for r in measure_defs if r.get("resourceType") not in ("Measure", "Library")]
        try:
            # ValueSets/CodeSystems plus ELM-declared missing ValueSet stubs.
            support_resources = await _prepare_measure_support_resources(secondary, bundle_json)
            if support_resources:
                await push_resources(support_resources)
            if primary:
                await push_resources(primary)  # Measure + Library always pushed
        except Exception as exc:
            raise ValueError(
                f"Failed to upload measures to HAPI measure engine. "
                f"Ensure the measure engine is running and accessible. "
                f"Details: {str(exc)}"
            ) from exc

    if progress_fn:
        await progress_fn("measures_loaded", sum(1 for r in measure_defs if r.get("resourceType") == "Measure"))

    # Delete stale expected results in two passes:
    # 1. Remove all rows owned by this exact filename so a re-upload of the same file
    #    with fewer patients or a renamed measure URL leaves nothing behind.
    # 2. Remove any rows from OTHER bundles that cover the same measures so uploading
    #    bundle_v2.json after bundle_v1.json for measure M doesn't leave v1's patients
    #    mixed in with v2's patients.  (Fixes issue #64.)
    await session.execute(delete(ExpectedResult).where(ExpectedResult.source_bundle == filename))
    if test_cases:
        covered_measure_urls = list({tc["measure_url"] for tc in test_cases})
        await session.execute(delete(ExpectedResult).where(ExpectedResult.measure_url.in_(covered_measure_urls)))

    for tc in test_cases:
        session.add(
            ExpectedResult(
                measure_url=tc["measure_url"],
                patient_ref=tc["patient_ref"],
                test_description=tc["test_description"],
                expected_populations=tc["expected_populations"],
                period_start=tc["period_start"],
                period_end=tc["period_end"],
                source_bundle=filename,
            )
        )
    # Intentionally no session.commit() here — the commit is deferred to
    # process_bundle_upload so that DB changes and clinical push succeed or fail
    # atomically.  If push_resources raises below, the session context manager in
    # process_bundle_upload rolls back both the delete and these inserts.  (Fixes #65.)

    if progress_fn:
        await progress_fn("expected_results_loaded", len(test_cases))

    # Push clinical data to active CDR (default or external)
    patients_loaded = 0
    cdr_upload_error_details: dict[str, Any] | None = None
    if clinical:
        cdr_result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
        active_cdr = cdr_result.scalar_one_or_none()
        if active_cdr and active_cdr.is_read_only:
            raise ValueError("Cannot upload clinical data: the active CDR connection is configured as read-only.")
        cdr_url = active_cdr.cdr_url if active_cdr else settings.DEFAULT_CDR_URL
        cdr_auth = await _build_auth_headers(active_cdr.auth_type, active_cdr.auth_credentials) if active_cdr else {}
        cdr_push_result = await push_resources(clinical, target_url=cdr_url, auth_headers=cdr_auth)
        patients_loaded = sum(1 for r in clinical if r.get("resourceType") == "Patient")
        logger.info(
            "Clinical resources loaded to CDR",
            extra={"count": len(clinical), "cdr_url": cdr_url},
        )
        if cdr_push_result.has_failures:
            failed_types: dict[str, int] = {}
            for fe in cdr_push_result.failed:
                failed_types[fe.resource_type] = failed_types.get(fe.resource_type, 0) + 1
            type_summary = ", ".join(f"{count} {rt}" for rt, count in sorted(failed_types.items()))
            total_failed = len(cdr_push_result.failed)
            total_entries = len(cdr_push_result.succeeded) + total_failed
            cdr_upload_error_details = {
                "operation": "push-resources",
                "total_entries": total_entries,
                "failed_count": total_failed,
                "succeeded_count": len(cdr_push_result.succeeded),
                "failed_entries": [
                    {
                        "resource_type": fe.resource_type,
                        "resource_id": fe.resource_id,
                        "status": fe.status,
                        "diagnostics": fe.outcome.primary_diagnostic() if fe.outcome else None,
                    }
                    for fe in cdr_push_result.failed
                ],
            }
            logger.warning(
                "CDR bundle push partial failure: %d of %d entries failed (%s)",
                total_failed,
                total_entries,
                type_summary,
                extra={"failed_types": failed_types},
            )

    if progress_fn:
        await progress_fn("patients_loaded", patients_loaded)

    valueset_urls = _valueset_urls([*measure_defs, *clinical])
    valueset_urls.extend(_valueset_urls(support_resources))
    expanded = await asyncio.to_thread(wait_for_valueset_expansion, settings.MEASURE_ENGINE_URL, valueset_urls)
    unique_valueset_count = len({url for url in valueset_urls if url})
    logger.info(
        "ValueSet expansion complete",
        extra={
            "vs_count_expanded": len(expanded),
            "vs_count_timeout": max(unique_valueset_count - len(expanded), 0),
        },
    )

    warning_message = None
    if cdr_upload_error_details:
        fc = cdr_upload_error_details["failed_count"]
        tc = cdr_upload_error_details["total_entries"]
        warning_message = f"{fc} of {tc} CDR upload entries failed"

    return {
        "measures_loaded": sum(1 for r in measure_defs if r.get("resourceType") == "Measure"),
        "patients_loaded": patients_loaded,
        "expected_results_loaded": len(test_cases),
        "warning_message": warning_message,
        "cdr_upload_error_details": cdr_upload_error_details,
    }


async def process_bundle_upload(upload_id: int) -> None:
    """Worker dispatch target: process a queued bundle upload."""
    async with async_session() as session:
        upload = await session.get(BundleUpload, upload_id)
        if not upload:
            logger.error("BundleUpload not found", extra={"upload_id": upload_id})
            return

        upload.status = ValidationStatus.running
        await session.commit()

    try:
        # Read bundle from disk
        async with async_session() as session:
            upload = await session.get(BundleUpload, upload_id)
            if not upload:
                return

            bundle_json = await asyncio.to_thread(lambda: json.loads(Path(upload.file_path).read_bytes()))

            async def _on_progress(field: str, value: int) -> None:
                async with async_session() as prog_session:
                    u = await prog_session.get(BundleUpload, upload_id)
                    if u:
                        setattr(u, field, value)
                        await prog_session.commit()

            summary = await triage_test_bundle(bundle_json, upload.filename, session, progress_fn=_on_progress)

            upload.measures_loaded = summary["measures_loaded"]
            upload.patients_loaded = summary["patients_loaded"]
            upload.expected_results_loaded = summary["expected_results_loaded"]
            upload.warning_message = summary.get("warning_message")
            upload.error_details = summary.get("cdr_upload_error_details")
            upload.status = ValidationStatus.complete
            upload.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info(
            "Bundle upload processed",
            extra={"upload_id": upload_id, **summary},
        )

    except Exception as exc:
        logger.exception("Bundle upload failed", extra={"upload_id": upload_id})
        upload_error_details: dict[str, Any] | None = None
        if isinstance(exc, FhirOperationError):
            upload_error_details = {
                "operation": exc.operation,
                "url": exc.url,
                "status_code": exc.status_code,
                "latency_ms": exc.latency_ms,
            }
            if exc.outcome:
                upload_error_details["raw_outcome"] = redact_outcome(exc.outcome.raw)
        async with async_session() as session:
            upload = await session.get(BundleUpload, upload_id)
            if upload:
                upload.status = ValidationStatus.failed
                upload.error_message = sanitize_error(exc)
                upload.error_details = upload_error_details
                upload.completed_at = datetime.now(timezone.utc)
                await session.commit()


# ---------------------------------------------------------------------------
# Validation run execution
# ---------------------------------------------------------------------------


async def _resolve_measure_id(measure_url: str) -> Optional[str]:
    """Resolve a measure URL or relative reference to a HAPI FHIR resource ID.

    Handles two formats:
    - Canonical URL (http/https): queries HAPI ?url= search parameter
    - Relative reference ("Measure/{id}"): fetches resource directly by ID

    Includes a retry loop and Cache-Control: no-cache to mitigate HAPI search
    indexing lag and search result caching.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        if not measure_url.startswith("http"):
            # Relative reference like "Measure/measure-EXM130-FHIR4-7.2.000"
            parts = measure_url.split("/", 1)
            if len(parts) == 2 and parts[0] == "Measure":
                resp = await client.get(f"{settings.MEASURE_ENGINE_URL}/Measure/{parts[1]}")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json().get("id")
            return None

        # Canonical URL — search by ?url= parameter
        # Retry up to 3 times with 1s delay to handle search indexing lag
        headers = {"Cache-Control": "no-cache", "Accept": "application/fhir+json"}
        params = {"url": measure_url, "_count": 1}

        for attempt in range(3):
            try:
                resp = await client.get(
                    f"{settings.MEASURE_ENGINE_URL}/Measure",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                bundle = resp.json()
                entries = bundle.get("entry", [])
                if entries:
                    return entries[0].get("resource", {}).get("id")

                if attempt < 2:
                    logger.info(
                        "Measure search returned no results — retrying",
                        extra={"measure_url": measure_url, "attempt": attempt + 1},
                    )
                    await asyncio.sleep(1.0)
            except Exception as exc:
                if attempt < 2:
                    logger.warning(
                        "Measure search failed — retrying",
                        extra={"measure_url": measure_url, "attempt": attempt + 1, "error": str(exc)},
                    )
                    await asyncio.sleep(1.0)
                else:
                    raise

    return None


async def _reload_measures_from_seed_bundles() -> dict[str, int]:
    """Reload all Measure/Library resources from seed bundles into HAPI.

    Called when validation detects missing measures. Returns counts of loaded resources.
    Safe to re-run (uses upsert logic in triage_test_bundle).
    """
    scan_dir = Path(__file__).resolve().parents[3] / "seed" / "connectathon-bundles"
    if not scan_dir.exists():
        logger.warning("Seed bundles directory not found", extra={"directory": str(scan_dir)})
        return {"measures_loaded": 0, "libraries_loaded": 0, "failed": 0}

    bundle_files = sorted(scan_dir.glob("*.json"))
    total_measures = 0
    total_libraries = 0
    failed = 0

    for bundle_path in bundle_files:
        if bundle_path.name == "manifest.json":
            continue
        try:
            bundle_json = json.loads(bundle_path.read_bytes())
            measure_defs, _, _ = _classify_bundle_entries(bundle_json)

            if measure_defs:
                primary = [r for r in measure_defs if r.get("resourceType") in ("Measure", "Library")]
                secondary = [r for r in measure_defs if r.get("resourceType") not in ("Measure", "Library")]
                support_resources = await _prepare_measure_support_resources(secondary, bundle_json)
                if support_resources:
                    await push_resources(support_resources)
                if primary:
                    await push_resources(primary)
                    for r in primary:
                        if r.get("resourceType") == "Measure":
                            total_measures += 1
                        elif r.get("resourceType") == "Library":
                            total_libraries += 1
            measure_count = sum(1 for r in measure_defs if r.get("resourceType") == "Measure")
            logger.info(
                "Reloaded measures from seed bundle",
                extra={"file": bundle_path.name, "measures": measure_count},
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "Failed to reload measures from seed bundle: %s",
                bundle_path.name,
                extra={"file": bundle_path.name, "error": str(exc)},
            )

    return {"measures_loaded": total_measures, "libraries_loaded": total_libraries, "failed": failed}


async def run_validation(validation_run_id: int) -> None:
    """Worker dispatch target: execute a validation run."""
    async with async_session() as session:
        run = await session.get(ValidationRun, validation_run_id)
        if not run:
            logger.error("ValidationRun not found", extra={"run_id": validation_run_id})
            return
        if run.delete_requested:
            await session.delete(run)
            await session.commit()
            logger.info("Validation run deleted before start", extra={"run_id": validation_run_id})
            return
        if run.status == ValidationStatus.cancelled:
            logger.info("Validation run already cancelled", extra={"run_id": validation_run_id})
            return
        run.status = ValidationStatus.running
        await session.commit()

    try:
        if await _stop_or_delete_validation_run(validation_run_id):
            return
        # Load expected results
        async with async_session() as session:
            run = await session.get(ValidationRun, validation_run_id)
            if not run:
                return

            query = select(ExpectedResult)
            if run.measure_urls:
                query = query.where(ExpectedResult.measure_url.in_(run.measure_urls))
            result = await session.execute(query)
            expected_results = list(result.scalars().all())

        if not expected_results:
            if await _stop_or_delete_validation_run(validation_run_id):
                return
            async with async_session() as session:
                run = await session.get(ValidationRun, validation_run_id)
                if run:
                    run.status = ValidationStatus.failed
                    run.error_message = "No expected results found"
                    run.completed_at = datetime.now(timezone.utc)
                    await session.commit()
            return

        # Group by measure URL
        measures: dict[str, list[ExpectedResult]] = {}
        for er in expected_results:
            measures.setdefault(er.measure_url, []).append(er)

        # Resolve measure IDs and get periods
        measure_info: dict[str, dict[str, str]] = {}
        missing_measures: list[str] = []
        unresolved_errors: dict[str, str] = {}
        all_results: list[ValidationResult] = []

        for measure_url, ers in measures.items():
            try:
                hapi_id = await _resolve_measure_id(measure_url)
            except Exception as exc:
                unresolved_errors[measure_url] = f"Measure resolution failed: {sanitize_error(exc)}"
                continue
            if not hapi_id:
                missing_measures.append(measure_url)
                continue
            measure_info[measure_url] = {
                "hapi_id": hapi_id,
                "period_start": ers[0].period_start,
                "period_end": ers[0].period_end,
            }
        if await _stop_or_delete_validation_run(validation_run_id):
            return

        # If measures are missing, try to reload from seed bundles (lazy loading)
        if missing_measures:
            logger.warning(
                "Measures not found on engine — attempting to reload from seed bundles",
                extra={"missing_count": len(missing_measures), "total": len(measures)},
            )
            try:
                reload_result = await _reload_measures_from_seed_bundles()
                logger.info("Seed bundle reload complete", extra=reload_result)

                # Retry resolving measures after reload
                for measure_url in missing_measures:
                    try:
                        hapi_id = await _resolve_measure_id(measure_url)
                    except Exception as exc:
                        unresolved_errors[measure_url] = (
                            f"Measure resolution failed after reload attempt: {sanitize_error(exc)}"
                        )
                        continue

                    if hapi_id:
                        ers = measures[measure_url]
                        measure_info[measure_url] = {
                            "hapi_id": hapi_id,
                            "period_start": ers[0].period_start,
                            "period_end": ers[0].period_end,
                        }
                    else:
                        unresolved_errors[measure_url] = (
                            "Measure not found on engine after reload attempt. "
                            "Upload the current bundle or verify the measure engine seed state."
                        )
            except Exception as exc:
                reload_error = sanitize_error(exc)
                logger.warning(
                    "Seed bundle reload failed; unresolved measures will be reported per patient",
                    extra={"run_id": validation_run_id, "error": reload_error},
                )
                for measure_url in missing_measures:
                    unresolved_errors.setdefault(
                        measure_url,
                        f"Failed to reload measures from seed bundles: {reload_error}",
                    )

        for measure_url, error_message in unresolved_errors.items():
            for er in measures[measure_url]:
                all_results.append(
                    ValidationResult(
                        validation_run_id=validation_run_id,
                        measure_url=er.measure_url,
                        patient_ref=er.patient_ref,
                        patient_name=None,
                        expected_populations=er.expected_populations,
                        actual_populations=None,
                        status="error",
                        error_message=error_message,
                        mismatches=[],
                    )
                )

        resolved_expected_results = [er for er in expected_results if er.measure_url in measure_info]
        total_passed = 0
        total_failed = 0
        total_errors = 0

        if resolved_expected_results:
            async with async_session() as session:
                cdr_result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
                active_cdr = cdr_result.scalar_one_or_none()
                if active_cdr:
                    cdr_url = active_cdr.cdr_url
                    auth_headers = await _build_auth_headers(active_cdr.auth_type, active_cdr.auth_credentials)
                else:
                    cdr_url = settings.DEFAULT_CDR_URL
                    auth_headers = {}

            if await _stop_or_delete_validation_run(validation_run_id):
                return

            # Best-effort for validation: stale resources are worse than ideal, but
            # aborting prevents patient-level comparison entirely on slow HAPI deletes.
            await wipe_patient_data(strict=False)
            if await _stop_or_delete_validation_run(validation_run_id):
                return

            # Phase 1: Gather from CDR and push to measure engine
            strategy = BatchQueryStrategy()
            semaphore = asyncio.Semaphore(settings.MAX_WORKERS)

            async def gather_and_push(patient_ref: str) -> set[str]:
                async with semaphore:
                    if await _stop_or_delete_validation_run(validation_run_id):
                        return set()
                    resources = await strategy.gather_patient_data(cdr_url, patient_ref, auth_headers)
                    if resources:
                        await push_resources(resources)
                    return {
                        resource.get("subject", {}).get("reference", "").removeprefix("Patient/")
                        for resource in resources
                        if resource.get("resourceType") == "Encounter"
                        and resource.get("subject", {}).get("reference", "").startswith("Patient/")
                    }

            all_patient_refs = [er.patient_ref for er in resolved_expected_results]
            gather_results = await asyncio.gather(
                *[gather_and_push(pr) for pr in all_patient_refs],
                return_exceptions=True,
            )
            failed_gathers = sum(1 for r in gather_results if isinstance(r, BaseException))
            failed_patient_refs: dict[str, str] = {
                pr: sanitize_error(r) if isinstance(r, Exception) else repr(r)
                for pr, r in zip(all_patient_refs, gather_results)
                if isinstance(r, BaseException)
            }
            if failed_gathers:
                logger.warning(
                    "Patient data gather partial failure — validation may reflect incomplete data",
                    extra={"failed": failed_gathers, "total": len(all_patient_refs), "run_id": validation_run_id},
                )

            if await _stop_or_delete_validation_run(validation_run_id):
                return

            # Phase 1b: Warmup burst to pre-create SearchParameters serially before concurrent eval.
            # First concurrent $evaluate-measure batch against a fresh measure engine triggers a race
            # during lazy SearchParameter indexing; some concurrent requests hit 409 CONFLICT.
            # Running one serial eval per measure avoids the race by forcing indexing to complete
            # in single-threaded context before concurrent batch starts.
            logger.info(
                "Pre-creating SearchParameters via warmup evaluations",
                extra={"run_id": validation_run_id, "measure_count": len(measure_info)},
            )
            for measure_url, info in measure_info.items():
                warmup_er = next(
                    (er for er in resolved_expected_results if er.measure_url == measure_url),
                    None,
                )
                if warmup_er:
                    try:
                        await evaluate_measure(
                            info["hapi_id"],
                            warmup_er.patient_ref,
                            info["period_start"],
                            info["period_end"],
                        )
                        logger.debug(
                            "Warmup evaluation complete",
                            extra={"measure_url": measure_url, "patient_ref": warmup_er.patient_ref},
                        )
                    except Exception as exc:
                        # Warmup failures may indicate indexing race; log at WARNING to surface issues.
                        error_body = None
                        if isinstance(exc, httpx.HTTPStatusError):
                            try:
                                error_body = exc.response.text
                            except Exception:
                                error_body = str(exc)
                        logger.warning(
                            "Warmup evaluate-measure failed",
                            extra={
                                "measure_url": measure_url,
                                "error": sanitize_error(exc),
                                "error_body": error_body or sanitize_error(exc),
                            },
                        )
            if await _stop_or_delete_validation_run(validation_run_id):
                return

            # Phase 2: Evaluate and compare (shared client avoids N connection pools)
            async def evaluate_and_compare(er: ExpectedResult, http_client: httpx.AsyncClient) -> ValidationResult:
                if er.patient_ref in failed_patient_refs:
                    return ValidationResult(
                        validation_run_id=validation_run_id,
                        measure_url=er.measure_url,
                        patient_ref=er.patient_ref,
                        patient_name=None,
                        expected_populations=er.expected_populations,
                        actual_populations=None,
                        status="error",
                        error_message=f"Patient data gather failed: {failed_patient_refs[er.patient_ref]}",
                        mismatches=[],
                    )
                info = measure_info[er.measure_url]
                try:
                    if await _stop_or_delete_validation_run(validation_run_id):
                        raise asyncio.CancelledError("Validation run deletion requested")
                    report = await evaluate_measure(
                        info["hapi_id"],
                        er.patient_ref,
                        info["period_start"],
                        info["period_end"],
                    )
                    actual = _extract_population_counts(report)

                    # Try to get patient name from the report's evaluated resources
                    patient_name = None
                    for eval_ref in report.get("evaluatedResource", []):
                        ref_str = eval_ref.get("reference", "")
                        if ref_str.startswith("Patient/"):
                            try:
                                resp = await http_client.get(f"{settings.MEASURE_ENGINE_URL}/{ref_str}")
                                if resp.status_code == 200:
                                    patient_name = _extract_patient_name(resp.json())
                            except Exception:
                                pass
                            break

                    passed, mismatches = compare_populations(er.expected_populations, actual)
                    return ValidationResult(
                        validation_run_id=validation_run_id,
                        measure_url=er.measure_url,
                        patient_ref=er.patient_ref,
                        patient_name=patient_name,
                        expected_populations=er.expected_populations,
                        actual_populations=actual,
                        status="pass" if passed else "fail",
                        mismatches=mismatches if mismatches else [],
                    )
                except Exception as exc:
                    sanitized = sanitize_error(exc)
                    eval_error_details: dict[str, Any] | None = None
                    if isinstance(exc, FhirOperationError):
                        eval_error_details = {
                            "operation": exc.operation,
                            "url": exc.url,
                            "status_code": exc.status_code,
                            "latency_ms": exc.latency_ms,
                        }
                        if exc.outcome:
                            eval_error_details["raw_outcome"] = redact_outcome(exc.outcome.raw)
                    return ValidationResult(
                        validation_run_id=validation_run_id,
                        measure_url=er.measure_url,
                        patient_ref=er.patient_ref,
                        patient_name=None,
                        expected_populations=er.expected_populations,
                        actual_populations=None,
                        status="error",
                        error_message=sanitized,
                        error_details=eval_error_details,
                        mismatches=[],
                    )

            async with httpx.AsyncClient(timeout=10.0) as http_client:

                async def eval_with_semaphore(er: ExpectedResult) -> ValidationResult:
                    async with semaphore:
                        return await evaluate_and_compare(er, http_client)

                result_coros = [eval_with_semaphore(er) for er in resolved_expected_results]
                all_results.extend(await asyncio.gather(*result_coros))

        if await _stop_or_delete_validation_run(validation_run_id):
            return
        for vr in all_results:
            if vr.status == "pass":
                total_passed += 1
            elif vr.status == "fail":
                total_failed += 1
            else:
                total_errors += 1

        # Store results
        if await _stop_or_delete_validation_run(validation_run_id):
            return
        async with async_session() as session:
            run = await session.get(ValidationRun, validation_run_id)
            if not run:
                return
            for vr in all_results:
                session.add(vr)
            run.measures_tested = len(measures)
            run.patients_tested = len(expected_results)
            run.patients_passed = total_passed
            run.patients_failed = total_failed + total_errors
            run.status = ValidationStatus.complete
            run.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info(
            "Validation run complete",
            extra={
                "run_id": validation_run_id,
                "passed": total_passed,
                "failed": total_failed,
                "errors": total_errors,
            },
        )

    except Exception as exc:
        logger.exception("Validation run failed", extra={"run_id": validation_run_id})
        if await _stop_or_delete_validation_run(validation_run_id):
            return
        async with async_session() as session:
            run = await session.get(ValidationRun, validation_run_id)
            if run:
                run.status = ValidationStatus.failed
                run.error_message = sanitize_error(exc)
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()

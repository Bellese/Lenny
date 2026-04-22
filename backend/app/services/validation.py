"""Validation service — bundle triage, expected result extraction, and measure validation.

Handles uploading test bundles (triaging resources to measure engine, CDR, and MCT2 DB),
running validation against expected results, and comparing population counts.
"""

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    _build_auth_headers,
    evaluate_measure,
    push_resources,
    wipe_patient_data,
)

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
    # Known clinical types present in the 12 connectathon bundles or seed/patient-bundle.json
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


async def triage_test_bundle(
    bundle_json: dict[str, Any],
    filename: str,
    session: AsyncSession,
) -> dict[str, int]:
    """Triage a test bundle: send resources to their correct destinations.

    - Measure/Library/ValueSet → measure engine
    - MeasureReport (isTestCase) → ExpectedResult table
    - Patient/clinical data → active CDR (default or external)

    Returns summary dict with counts.
    """
    _warn_unknown_bundle_types(bundle_json)
    measure_defs, clinical, test_cases = _classify_bundle_entries(bundle_json)

    # Push measure definitions to measure engine in two phases so shared ValueSets
    # (which trigger HAPI-0902 on re-upload) never block the Measure/Library load.
    if measure_defs:
        primary = [r for r in measure_defs if r.get("resourceType") in ("Measure", "Library")]
        secondary = [r for r in measure_defs if r.get("resourceType") not in ("Measure", "Library")]
        try:
            if secondary:
                # ValueSets/CodeSystems: patch compose before loading (HAPI-0902 is OK)
                await push_resources(_fix_valueset_compose_for_hapi(secondary))
            if primary:
                await push_resources(primary)  # Measure + Library always pushed
        except Exception as exc:
            raise ValueError(
                f"Failed to upload measures to HAPI measure engine. "
                f"Ensure the measure engine is running and accessible. "
                f"Details: {str(exc)}"
            ) from exc

    # Upsert expected results atomically (avoids TOCTOU on concurrent uploads)
    for tc in test_cases:
        stmt = (
            pg_insert(ExpectedResult)
            .values(
                measure_url=tc["measure_url"],
                patient_ref=tc["patient_ref"],
                test_description=tc["test_description"],
                expected_populations=tc["expected_populations"],
                period_start=tc["period_start"],
                period_end=tc["period_end"],
                source_bundle=filename,
            )
            .on_conflict_do_update(
                constraint="uq_measure_patient",
                set_={
                    "test_description": tc["test_description"],
                    "expected_populations": tc["expected_populations"],
                    "period_start": tc["period_start"],
                    "period_end": tc["period_end"],
                    "source_bundle": filename,
                },
            )
        )
        await session.execute(stmt)
    await session.commit()

    # Push clinical data to active CDR (default or external)
    patients_loaded = 0
    if clinical:
        cdr_result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
        active_cdr = cdr_result.scalar_one_or_none()
        cdr_url = active_cdr.cdr_url if active_cdr else settings.DEFAULT_CDR_URL
        cdr_auth = await _build_auth_headers(active_cdr.auth_type, active_cdr.auth_credentials) if active_cdr else {}
        await push_resources(clinical, target_url=cdr_url, auth_headers=cdr_auth)
        patients_loaded = sum(1 for r in clinical if r.get("resourceType") == "Patient")
        logger.info(
            "Clinical resources loaded to CDR",
            extra={"count": len(clinical), "cdr_url": cdr_url},
        )

    return {
        "measures_loaded": sum(1 for r in measure_defs if r.get("resourceType") == "Measure"),
        "patients_loaded": patients_loaded,
        "expected_results_loaded": len(test_cases),
        "warning_message": None,
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

            summary = await triage_test_bundle(bundle_json, upload.filename, session)

            upload.measures_loaded = summary["measures_loaded"]
            upload.patients_loaded = summary["patients_loaded"]
            upload.expected_results_loaded = summary["expected_results_loaded"]
            upload.warning_message = summary.get("warning_message")
            upload.status = ValidationStatus.complete
            upload.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info(
            "Bundle upload processed",
            extra={"upload_id": upload_id, **summary},
        )

    except Exception as exc:
        logger.exception("Bundle upload failed", extra={"upload_id": upload_id})
        async with async_session() as session:
            upload = await session.get(BundleUpload, upload_id)
            if upload:
                upload.status = ValidationStatus.failed
                upload.error_message = sanitize_error(exc)
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
                if secondary:
                    await push_resources(secondary)
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
        run.status = ValidationStatus.running
        await session.commit()

    try:
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

        for measure_url, ers in measures.items():
            hapi_id = await _resolve_measure_id(measure_url)
            if not hapi_id:
                missing_measures.append(measure_url)
            else:
                measure_info[measure_url] = {
                    "hapi_id": hapi_id,
                    "period_start": ers[0].period_start,
                    "period_end": ers[0].period_end,
                }

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
                still_missing: list[str] = []
                for measure_url in missing_measures:
                    hapi_id = await _resolve_measure_id(measure_url)
                    if hapi_id:
                        ers = measures[measure_url]
                        measure_info[measure_url] = {
                            "hapi_id": hapi_id,
                            "period_start": ers[0].period_start,
                            "period_end": ers[0].period_end,
                        }
                    else:
                        still_missing.append(measure_url)

                if still_missing:
                    error_msg = (
                        f"Measures not found on engine after reload attempt. "
                        f"This may indicate the HAPI measure engine is unavailable or the seed bundles are missing. "
                        f"Please ensure the backend is properly connected to the measure engine, "
                        f"or manually upload test bundles using the Validation page. "
                        f"Missing measures: {', '.join(still_missing[:3])}{'...' if len(still_missing) > 3 else ''}"
                    )
                    raise ValueError(error_msg)
            except ValueError:
                raise
            except Exception as exc:
                error_msg = (
                    f"Failed to reload measures from seed bundles: {str(exc)}. "
                    f"Please manually upload test bundles using the Validation page."
                )
                raise ValueError(error_msg) from exc

        # Resolve CDR connection
        async with async_session() as session:
            cdr_result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
            active_cdr = cdr_result.scalar_one_or_none()
            if active_cdr:
                cdr_url = active_cdr.cdr_url
                auth_headers = await _build_auth_headers(active_cdr.auth_type, active_cdr.auth_credentials)
            else:
                cdr_url = settings.DEFAULT_CDR_URL
                auth_headers = {}

        # Wipe measure engine patient data for clean evaluation
        await wipe_patient_data()

        # Phase 1: Gather from CDR and push to measure engine
        strategy = BatchQueryStrategy()
        semaphore = asyncio.Semaphore(settings.MAX_WORKERS)

        async def gather_and_push(patient_ref: str) -> None:
            async with semaphore:
                resources = await strategy.gather_patient_data(cdr_url, patient_ref, auth_headers)
                if resources:
                    await push_resources(resources)

        all_patient_refs = [er.patient_ref for er in expected_results]
        gather_results = await asyncio.gather(
            *[gather_and_push(pr) for pr in all_patient_refs],
            return_exceptions=True,
        )
        failed_gathers = sum(1 for r in gather_results if isinstance(r, BaseException))
        if failed_gathers:
            logger.warning(
                "Patient data gather partial failure — validation may reflect incomplete data",
                extra={"failed": failed_gathers, "total": len(all_patient_refs), "run_id": validation_run_id},
            )

        # Wait for HAPI indexing (configurable via HAPI_INDEX_WAIT_SECONDS env var)
        await asyncio.sleep(settings.HAPI_INDEX_WAIT_SECONDS)

        # Phase 2: Evaluate and compare (shared client avoids N connection pools)
        total_passed = 0
        total_failed = 0
        total_errors = 0
        all_results: list[ValidationResult] = []

        async def evaluate_and_compare(er: ExpectedResult, http_client: httpx.AsyncClient) -> ValidationResult:
            info = measure_info[er.measure_url]
            try:
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
                return ValidationResult(
                    validation_run_id=validation_run_id,
                    measure_url=er.measure_url,
                    patient_ref=er.patient_ref,
                    patient_name=None,
                    expected_populations=er.expected_populations,
                    actual_populations=None,
                    status="error",
                    error_message=sanitize_error(exc),
                    mismatches=[],
                )

        async with httpx.AsyncClient(timeout=10.0) as http_client:

            async def eval_with_semaphore(er: ExpectedResult) -> ValidationResult:
                async with semaphore:
                    return await evaluate_and_compare(er, http_client)

            result_coros = [eval_with_semaphore(er) for er in expected_results]
            all_results = list(await asyncio.gather(*result_coros))

        for vr in all_results:
            if vr.status == "pass":
                total_passed += 1
            elif vr.status == "fail":
                total_failed += 1
            else:
                total_errors += 1

        # Store results
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
        async with async_session() as session:
            run = await session.get(ValidationRun, validation_run_id)
            if run:
                run.status = ValidationStatus.failed
                run.error_message = sanitize_error(exc)
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()

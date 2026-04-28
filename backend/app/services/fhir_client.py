"""FHIR client with pluggable data acquisition strategy.

Handles all HTTP communication with HAPI FHIR servers (CDR and measure engine).
"""

from __future__ import annotations

import abc
import asyncio
import ipaddress
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.config import AuthType

logger = logging.getLogger(__name__)

# Hosts explicitly allowed for local dev even though they're loopback/private.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_blocked_ip(host: str) -> bool:
    """Return True if host is a private/reserved IP (RFC-1918, loopback, link-local, ULA).

    Covers IPv4 (including 169.254.0.0/16 AWS IMDS) and IPv6 private ranges.
    Returns False for hostnames that aren't raw IP literals.
    """
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False  # Not a raw IP literal — hostname, can't resolve statically


def _validate_ssrf_url(url: str, label: str = "URL") -> None:
    """Reject URLs that could be used for SSRF attacks.

    Rules:
    - Only https is allowed unless the host is localhost/127.0.0.1/::1 (local dev).
    - Raw IP literals that are private, loopback, or link-local are rejected unless
      the host is in the local dev allowlist.  This covers RFC-1918, 169.254.0.0/16
      (AWS IMDS), IPv6 loopback, link-local, and ULA ranges.

    Raises ValueError with a descriptive message on rejection.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""

    is_local = host in _LOCAL_HOSTS

    if scheme not in ("http", "https"):
        raise ValueError(
            f"SSRF protection: {label} scheme '{scheme}' is not allowed. "
            "Only https (or http for localhost) is permitted."
        )

    if scheme == "http" and not is_local:
        raise ValueError(f"SSRF protection: {label} must use https for non-localhost hosts (got http://{host}).")

    if not is_local and _is_blocked_ip(host):
        raise ValueError(
            f"SSRF protection: {label} resolves to a private/reserved address "
            f"({host}). Use a publicly routable host or localhost."
        )


async def _build_auth_headers(auth_type: str, auth_credentials: Optional[dict]) -> dict[str, str]:
    """Build HTTP auth headers from CDR config. Async to support SMART token acquisition."""
    if auth_type == AuthType.none or not auth_credentials:
        return {}
    if auth_type == AuthType.basic:
        import base64

        username = auth_credentials.get("username", "")
        password = auth_credentials.get("password", "")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    if auth_type == AuthType.bearer:
        token = auth_credentials.get("token", "")
        return {"Authorization": f"Bearer {token}"}
    if auth_type == AuthType.smart:
        token = await _acquire_smart_token(auth_credentials)
        return {"Authorization": f"Bearer {token}"}
    return {}


async def _acquire_smart_token(credentials: dict) -> str:
    """Exchange client_credentials grant for a bearer token at token_endpoint.

    No token caching — fresh token per request is intentional for the connectathon.
    Add TTL-based caching post-connectathon if token endpoint rate-limiting becomes an issue.
    """
    required = {"token_endpoint", "client_id", "client_secret"}
    missing = required - credentials.keys()
    if missing:
        raise ValueError(f"SMART credentials missing required fields: {', '.join(sorted(missing))}")

    _validate_ssrf_url(credentials["token_endpoint"], label="token_endpoint")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            credentials["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
            },
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise ValueError("SMART token endpoint response missing 'access_token'")
        return token


# ---------------------------------------------------------------------------
# Data Acquisition Strategy (ABC + BatchQuery implementation)
# ---------------------------------------------------------------------------


class DataAcquisitionStrategy(abc.ABC):
    """Abstract base class for patient data acquisition from a CDR."""

    @abc.abstractmethod
    async def gather_patients(
        self,
        cdr_url: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Return a list of Patient resources from the CDR.

        Each item is a FHIR Patient resource dict with at least 'id'.
        """
        ...

    @abc.abstractmethod
    async def gather_patient_data(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Return all clinical resources for a single patient.

        Returns a list of FHIR resources (Condition, Observation, etc.).
        """
        ...


class BatchQueryStrategy(DataAcquisitionStrategy):
    """Fetch patients using paginated FHIR REST queries."""

    async def gather_patients(
        self,
        cdr_url: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch all Patient resources from the CDR, following pagination."""
        patients: list[dict[str, Any]] = []
        url: Optional[str] = f"{cdr_url}/Patient?_count=100"
        async with httpx.AsyncClient(timeout=60.0) as client:
            while url:
                logger.info("Fetching patients", extra={"url": url})
                resp = await client.get(url, headers=auth_headers)
                resp.raise_for_status()
                bundle = resp.json()
                for entry in bundle.get("entry", []):
                    resource = entry.get("resource", {})
                    if resource.get("resourceType") == "Patient":
                        patients.append(resource)
                # Follow next link for pagination
                url = None
                for link in bundle.get("link", []):
                    if link.get("relation") == "next":
                        url = link.get("url")
                        break
        logger.info("Gathered patients", extra={"count": len(patients)})
        return patients

    async def gather_patient_data(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch all resources for a patient using $everything."""
        resources: list[dict[str, Any]] = []
        url: Optional[str] = f"{cdr_url}/Patient/{patient_id}/$everything?_count=200"
        async with httpx.AsyncClient(timeout=120.0) as client:
            while url:
                logger.info(
                    "Fetching patient data",
                    extra={"patient_id": patient_id, "url": url},
                )
                resp = await client.get(url, headers=auth_headers)
                resp.raise_for_status()
                bundle = resp.json()
                # Group: population container that references other patients — not
                # a clinical resource for this patient.
                # MeasureReport: test-case expected-result artifact, not clinical data.
                _SKIP_TYPES = {"Group", "MeasureReport"}
                for entry in bundle.get("entry", []):
                    resource = entry.get("resource")
                    if resource and resource.get("resourceType") not in _SKIP_TYPES:
                        resources.append(resource)
                url = None
                for link in bundle.get("link", []):
                    if link.get("relation") == "next":
                        url = link.get("url")
                        break
        logger.info(
            "Gathered patient data",
            extra={"patient_id": patient_id, "resource_count": len(resources)},
        )
        return resources


class DataRequirementsStrategy(DataAcquisitionStrategy):
    """DEQM spec-compliant data acquisition using $data-requirements.

    Calls GET /Measure/{id}/$data-requirements on the measure engine,
    translates each dataRequirement entry into a CDR REST query, and
    collects only the resources the measure actually needs.

    Falls back to BatchQueryStrategy ($everything) if $data-requirements
    returns an empty list or raises any exception.
    """

    def __init__(self, measure_id: str) -> None:
        self._measure_id = measure_id
        self._fallback = BatchQueryStrategy()

    async def gather_patients(
        self,
        cdr_url: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Delegate patient listing to BatchQueryStrategy (CDR search is the same)."""
        return await self._fallback.gather_patients(cdr_url, auth_headers)

    async def gather_patient_data(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Fetch only the resources the measure needs, using $data-requirements."""
        try:
            requirements = await self._get_data_requirements()
        except Exception as exc:
            logger.warning(
                "$data-requirements failed, falling back to $everything",
                extra={"measure_id": self._measure_id, "patient_id": patient_id, "error": str(exc)},
            )
            return await self._fallback.gather_patient_data(cdr_url, patient_id, auth_headers)

        if not requirements:
            logger.info(
                "$data-requirements returned no entries, falling back to $everything",
                extra={"measure_id": self._measure_id, "patient_id": patient_id},
            )
            return await self._fallback.gather_patient_data(cdr_url, patient_id, auth_headers)

        try:
            return await self._fetch_by_requirements(cdr_url, patient_id, auth_headers, requirements)
        except Exception as exc:
            logger.warning(
                "CDR fetch by requirements failed, falling back to $everything",
                extra={"measure_id": self._measure_id, "patient_id": patient_id, "error": str(exc)},
            )
            return await self._fallback.gather_patient_data(cdr_url, patient_id, auth_headers)

    async def _get_data_requirements(self) -> list[dict[str, Any]]:
        """Call $data-requirements on MCS and return the dataRequirement entries."""
        url = f"{settings.MEASURE_ENGINE_URL}/Measure/{self._measure_id}/$data-requirements"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            library = resp.json()
            return library.get("dataRequirement", [])

    async def _fetch_by_requirements(
        self,
        cdr_url: str,
        patient_id: str,
        auth_headers: dict[str, str],
        requirements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Translate dataRequirement entries to CDR REST queries and collect resources.

        Translates codeFilter[].valueSet into code:in={valueSetUrl} search parameters.
        Per-type failures are logged and skipped; only raises if all types fail (triggering
        the outer $everything fallback).
        """
        resources: list[dict[str, Any]] = []
        seen_types: set[str] = set()
        failed_types: set[str] = set()

        async with httpx.AsyncClient(timeout=60.0) as client:
            for req in requirements:
                resource_type = req.get("type", "")
                if not resource_type or not re.match(r"^[A-Za-z][A-Za-z0-9]{0,127}$", resource_type):
                    continue
                if resource_type in seen_types:
                    continue
                seen_types.add(resource_type)

                try:
                    if resource_type == "Patient":
                        resp = await client.get(f"{cdr_url}/Patient/{patient_id}", headers=auth_headers)
                        if resp.status_code == 200:
                            resources.append(resp.json())
                    else:
                        # Translate codeFilter[].valueSet to code:in search parameter
                        params = f"subject=Patient/{patient_id}&_count=100"
                        for cf in req.get("codeFilter", []):
                            vs = cf.get("valueSet")
                            if vs:
                                params += f"&code:in={vs}"
                                break  # Use first valueSet codeFilter only
                        page_url: Optional[str] = f"{cdr_url}/{resource_type}?{params}"
                        while page_url:
                            resp = await client.get(page_url, headers=auth_headers)
                            if resp.status_code != 200:
                                break
                            bundle = resp.json()
                            for entry in bundle.get("entry", []):
                                resource = entry.get("resource")
                                if resource:
                                    resources.append(resource)
                            page_url = None
                            for link in bundle.get("link", []):
                                if link.get("relation") == "next":
                                    page_url = link.get("url")
                                    break
                except Exception as exc:
                    failed_types.add(resource_type)
                    logger.warning(
                        "CDR fetch failed for resource type %s, skipping — %s",
                        resource_type,
                        str(exc),
                        extra={"resource_type": resource_type, "patient_id": patient_id, "error": str(exc)},
                    )

        # Only propagate failure (triggering outer $everything fallback) when all types fail
        if seen_types and failed_types == seen_types:
            raise RuntimeError(f"All resource types failed CDR fetch: {sorted(failed_types)}")

        logger.info(
            "Fetched patient data via $data-requirements",
            extra={
                "measure_id": self._measure_id,
                "patient_id": patient_id,
                "resource_count": len(resources),
                "requirement_types": list(seen_types),
            },
        )
        return resources


# ---------------------------------------------------------------------------
# Direct FHIR helper functions (measure engine interaction)
# ---------------------------------------------------------------------------

_REINDEX_POLL_INTERVAL = 5
_VALUESET_EXPANSION_POLL_INTERVAL = 10


def _normalize_patient_id(patient_ref: str) -> str:
    return patient_ref.removeprefix("Patient/")


def trigger_reindex_and_wait_for_patients(
    base_url: str,
    probe_patient_ids: list[str],
    timeout_s: int = 300,
) -> None:
    """Trigger HAPI Encounter reindexing and wait until current-run patient searches work."""
    headers = {"Content-Type": "application/fhir+json"}
    params = {"resourceType": "Parameters", "parameter": [{"name": "type", "valueString": "Encounter"}]}
    started_at = time.monotonic()
    polls = 0
    pending = sorted({_normalize_patient_id(pid) for pid in probe_patient_ids if pid})

    if not pending:
        logger.warning(
            "HAPI reindex wait skipped because no probe patients were provided",
            extra={"base_url": base_url},
        )
        return

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{base_url}/$reindex", json=params, headers=headers)
        if resp.status_code != 202:
            logger.warning(
                "HAPI reindex trigger returned unexpected status",
                extra={"base_url": base_url, "status_code": resp.status_code, "body": resp.text[:200]},
            )

        deadline = started_at + timeout_s
        while pending and time.monotonic() < deadline:
            polls += 1
            newly_done: set[str] = set()
            for patient_id in pending:
                try:
                    probe_resp = client.get(f"{base_url}/Encounter?patient={patient_id}&_count=1", timeout=10.0)
                    if probe_resp.status_code == 200 and probe_resp.json().get("entry"):
                        newly_done.add(patient_id)
                except Exception:
                    pass
            if newly_done:
                pending = [patient_id for patient_id in pending if patient_id not in newly_done]
            if pending:
                time.sleep(_REINDEX_POLL_INTERVAL)

    if not pending:
        duration_s = round(time.monotonic() - started_at, 3)
        logger.info(
            "HAPI reindex complete",
            extra={"duration_s": duration_s, "polls": polls, "probe_patient_count": len(probe_patient_ids)},
        )
        return

    duration_s = round(time.monotonic() - started_at, 3)
    logger.warning(
        "HAPI reindex timed out",
        extra={"duration_s": duration_s, "polls": polls, "pending_patient_count": len(pending)},
    )


def trigger_reindex_and_wait(base_url: str, probe_patient_id: str | None = None, timeout_s: int = 300) -> None:
    """Trigger HAPI Encounter reindexing and wait until patient reference search works."""
    if probe_patient_id:
        trigger_reindex_and_wait_for_patients(base_url, [probe_patient_id], timeout_s=timeout_s)
        return

    with httpx.Client(timeout=30.0) as client:
        try:
            probe_resp = client.get(f"{base_url}/Patient?_count=1", timeout=10.0)
            probe_resp.raise_for_status()
            entries = probe_resp.json().get("entry", [])
            if entries:
                probe_patient_id = entries[0].get("resource", {}).get("id")
        except Exception as exc:
            logger.warning(
                "Unable to select HAPI reindex probe patient",
                extra={"base_url": base_url, "error": str(exc)},
            )

    if not probe_patient_id:
        logger.warning(
            "HAPI reindex wait skipped because no probe patient was available",
            extra={"base_url": base_url},
        )
        return

    trigger_reindex_and_wait_for_patients(base_url, [probe_patient_id], timeout_s=timeout_s)


def wait_for_valueset_expansion(base_url: str, valueset_urls: list[str], timeout_s: int = 600) -> dict[str, int]:
    """Wait until HAPI can serve $expand for each ValueSet URL and return expansion totals."""
    expanded: dict[str, int] = {}
    if not valueset_urls:
        return expanded

    unique_urls = sorted({url for url in valueset_urls if url})
    pending: dict[str, str] = {}
    with httpx.Client(timeout=30.0) as client:
        for valueset_url in unique_urls:
            try:
                lookup = client.get(
                    f"{base_url}/ValueSet",
                    params={"url": valueset_url, "_count": 1, "_elements": "id,url"},
                    headers={"Cache-Control": "no-cache", "Accept": "application/fhir+json"},
                )
                lookup.raise_for_status()
                entries = lookup.json().get("entry", [])
                if not entries:
                    logger.warning("ValueSet not found for expansion wait", extra={"valueset_url": valueset_url})
                    continue
                valueset_id = entries[0].get("resource", {}).get("id")
                if not valueset_id:
                    logger.warning("ValueSet lookup returned no id", extra={"valueset_url": valueset_url})
                    continue
                pending[valueset_url] = valueset_id
            except Exception as exc:
                logger.warning(
                    "ValueSet lookup failed before expansion wait",
                    extra={"valueset_url": valueset_url, "error": str(exc)},
                )

        deadline = time.monotonic() + timeout_s
        polls = 0
        while pending and time.monotonic() < deadline:
            polls += 1
            newly_done: set[str] = set()
            for valueset_url, valueset_id in list(pending.items()):
                try:
                    resp = client.post(f"{base_url}/ValueSet/{valueset_id}/$expand?count=2", timeout=15.0)
                    if resp.status_code == 200:
                        body = resp.json()
                        expanded[valueset_url] = int(
                            body.get("expansion", {}).get("total", len(body.get("expansion", {}).get("contains", [])))
                        )
                        newly_done.add(valueset_url)
                except Exception:
                    pass
            for valueset_url in newly_done:
                pending.pop(valueset_url, None)
            if pending:
                time.sleep(_VALUESET_EXPANSION_POLL_INTERVAL)

        for valueset_url, valueset_id in pending.items():
            logger.warning(
                "ValueSet expansion timed out",
                extra={"valueset_url": valueset_url, "valueset_id": valueset_id, "polls": polls},
            )

    return expanded


def _normalize_measure_def(r: dict[str, Any]) -> dict[str, Any]:
    """Ensure Library resources have a url so HAPI 8.x can resolve canonical refs.

    DBCG FHIR4 bundles omit Library.url; HAPI resolves Measure.library references
    by canonical URL search, so we backfill url = "Library/{id}" when absent.
    """
    if r.get("resourceType") == "Library" and not r.get("url") and r.get("id"):
        r = {**r, "url": f"Library/{r['id']}"}
    return r


async def push_resources(
    resources: list[dict[str, Any]],
    target_url: str | None = None,
    auth_headers: dict[str, str] | None = None,
) -> None:
    """POST a batch Bundle of resources to the target FHIR server.

    Uses batch (not transaction) so HAPI does not validate cross-references
    between entries — avoids HAPI-2001 when clinical subjects are absent from
    the measure engine.

    Patient resources are placed first in the batch. HAPI's bundle import
    skips writing reference index entries for forward-references (e.g. an
    Encounter whose `subject` points at a Patient that hasn't been written
    yet in the same bundle). The Encounter is durably persisted but
    `Encounter?patient=Patient/{id}` returns 0 forever afterwards. Sorting
    Patients-first makes every Patient reference resolvable at index time.
    Verified empirically 2026-04-25: same bundle, original order = 20/33
    indexed; Patients-first = 33/33 at t=0. See issue #177.
    """
    base = target_url or settings.MEASURE_ENGINE_URL
    valid = [r for r in resources if "resourceType" in r and "id" in r]
    ordered = [r for r in valid if r.get("resourceType") == "Patient"] + [
        r for r in valid if r.get("resourceType") != "Patient"
    ]
    bundle = {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {
                "resource": _normalize_measure_def(r),
                "request": {
                    "method": "PUT",
                    "url": f"{r['resourceType']}/{r['id']}",
                },
            }
            for r in ordered
        ],
    }
    if not bundle["entry"]:
        logger.warning("No valid resources to push")
        return
    headers = {"Content-Type": "application/fhir+json", **(auth_headers or {})}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            base,
            json=bundle,
            headers=headers,
        )
        resp.raise_for_status()
    logger.info("Pushed resources", extra={"count": len(bundle["entry"]), "target": base})


async def evaluate_measure(
    measure_id: str,
    patient_id: str,
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    """Call $evaluate-measure on the measure engine for a single patient."""
    url = (
        f"{settings.MEASURE_ENGINE_URL}/Measure/{measure_id}"
        f"/$evaluate-measure"
        f"?periodStart={period_start}"
        f"&periodEnd={period_end}"
        f"&subject=Patient/{patient_id}"
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(3):
            logger.info(
                "Evaluating measure",
                extra={"measure_id": measure_id, "patient_id": patient_id, "attempt": attempt + 1},
            )
            resp = await client.get(url)
            try:
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code < 500 or attempt == 2:
                    raise
                logger.warning(
                    "Transient measure evaluation failure — retrying",
                    extra={
                        "measure_id": measure_id,
                        "patient_id": patient_id,
                        "status_code": status_code,
                        "attempt": attempt + 1,
                    },
                )
                await asyncio.sleep(0.5 * (attempt + 1))

    raise RuntimeError("Measure evaluation failed without a response")


async def upload_measure_bundle(bundle_json: dict[str, Any]) -> dict[str, Any]:
    """POST a Measure bundle to the measure engine."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            settings.MEASURE_ENGINE_URL,
            json=bundle_json,
            headers={"Content-Type": "application/fhir+json"},
        )
        resp.raise_for_status()
        return resp.json()


async def delete_measure(measure_id: str) -> None:
    """Delete a Measure resource from the measure engine."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(f"{settings.MEASURE_ENGINE_URL}/Measure/{measure_id}")
        if resp.status_code not in (200, 202, 204):
            resp.raise_for_status()


async def wipe_patient_data(*, strict: bool = True) -> None:
    """Delete patient-related data from the measure engine.

    Called at the START of a new job to clean up data from the prior run.
    This allows the previous job's evaluated resources to remain available
    for inspection until a new job begins.

    Raises RuntimeError after 3 consecutive HTTP failures regardless of strict mode.
    A timed-out DELETE leaves HAPI's server-side operation still running; pushing new
    data over it causes the in-flight DELETE to wipe the freshly-pushed resources.
    The strict parameter is kept for API compatibility but no longer silences failures.
    """
    resource_types = [
        "MeasureReport",
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
    ]
    _MAX_CONSECUTIVE_FAILURES = 3
    consecutive_failures = 0
    async with httpx.AsyncClient(timeout=300.0) as client:
        for rt in resource_types:
            try:
                # Use conditional delete: DELETE ResourceType?_lastUpdated=gt1900-01-01
                delete_url = f"{settings.MEASURE_ENGINE_URL}/{rt}?_lastUpdated=gt1900-01-01"
                resp = await client.delete(delete_url)
                if resp.status_code < 300:
                    logger.info("Wiped resource type", extra={"resourceType": rt})
                else:
                    # Fall back to individual delete via search-and-delete
                    await _delete_all_of_type(client, rt)
                consecutive_failures = 0
            except httpx.HTTPError:
                consecutive_failures += 1
                logger.warning(
                    "Failed to wipe resource type (may not exist)",
                    extra={"resourceType": rt},
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"Measure engine unreachable: {consecutive_failures} consecutive "
                        "timeouts during wipe. Job aborted."
                    )


async def _delete_all_of_type(client: httpx.AsyncClient, resource_type: str) -> None:
    """Delete all resources of a given type one by one."""
    url: Optional[str] = f"{settings.MEASURE_ENGINE_URL}/{resource_type}?_count=100"
    while url:
        resp = await client.get(url)
        if resp.status_code != 200:
            break
        bundle = resp.json()
        entries = bundle.get("entry", [])
        if not entries:
            break
        for entry in entries:
            res = entry.get("resource", {})
            res_id = res.get("id")
            if res_id:
                del_url = f"{settings.MEASURE_ENGINE_URL}/{resource_type}/{res_id}"
                try:
                    await client.delete(del_url)
                except httpx.HTTPError:
                    pass
        # Re-check if more remain
        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                url = link.get("url")
                break


async def resolve_evaluated_resource(reference: str) -> dict[str, Any]:
    """Resolve an evaluatedResource reference from the measure engine."""
    url = f"{settings.MEASURE_ENGINE_URL}/{reference}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def list_groups(
    cdr_url: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """List all Group resources on the CDR."""
    groups: list[dict[str, Any]] = []
    url: Optional[str] = f"{cdr_url}/Group?_count=100"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            resp = await client.get(url, headers=auth_headers)
            resp.raise_for_status()
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") == "Group":
                    groups.append(
                        {
                            "id": resource.get("id"),
                            "name": resource.get("name"),
                            "type": resource.get("type"),
                            "member_count": len(resource.get("member", [])),
                        }
                    )
            url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    url = link.get("url")
                    break
    return groups


async def get_group_members(
    cdr_url: str,
    group_id: str,
    auth_headers: dict[str, str],
) -> list[dict[str, Any]]:
    """Fetch Patient resources for all members of a Group (concurrent)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Fetch the Group resource
        resp = await client.get(f"{cdr_url}/Group/{group_id}", headers=auth_headers)
        resp.raise_for_status()
        group = resp.json()

        # Extract Patient IDs from members
        patient_ids: list[tuple[str, str]] = []  # (patient_id, original_ref)
        for member in group.get("member", []):
            ref = member.get("entity", {}).get("reference", "")
            if ref.startswith("Patient/"):
                patient_id = ref.split("/", 1)[1]
                patient_ids.append((patient_id, ref))

        # Fetch all patients concurrently with a semaphore to avoid overwhelming the CDR
        semaphore = asyncio.Semaphore(10)

        async def fetch_patient(patient_id: str, ref: str) -> Optional[dict[str, Any]]:
            async with semaphore:
                patient_resp = await client.get(f"{cdr_url}/Patient/{patient_id}", headers=auth_headers)
                if patient_resp.status_code == 200:
                    return patient_resp.json()
                logger.warning(
                    "Could not fetch group member",
                    extra={"group_id": group_id, "patient_ref": ref},
                )
                return None

        results = await asyncio.gather(
            *[fetch_patient(pid, ref) for pid, ref in patient_ids],
            return_exceptions=True,
        )
        patients = [r for r in results if isinstance(r, dict)]

    logger.info(
        "Gathered group members",
        extra={"group_id": group_id, "count": len(patients)},
    )
    return patients


async def list_measures() -> dict[str, Any]:
    """List all Measure resources on the measure engine."""
    url = f"{settings.MEASURE_ENGINE_URL}/Measure?_count=100"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def verify_fhir_connection(
    fhir_url: str,
    auth_type: str = "none",
    auth_credentials: Optional[dict] = None,
) -> dict[str, Any]:
    """Test connectivity to a FHIR server by fetching its metadata."""
    from app.services.fhir_errors import FhirOperationError, FhirOperationOutcome, sanitize_url

    _validate_ssrf_url(fhir_url, label="cdr_url")
    headers = await _build_auth_headers(auth_type, auth_credentials)
    url = f"{fhir_url}/metadata"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            latency_ms = round((time.monotonic() - t0) * 1000)
            if not resp.is_success:
                outcome = FhirOperationOutcome.from_response(resp)
                raise FhirOperationError(
                    operation="test-connection",
                    url=url,
                    status_code=resp.status_code,
                    outcome=outcome,
                    latency_ms=latency_ms,
                )
            data = resp.json()
            return {
                "status": "connected",
                "fhir_version": data.get("fhirVersion", "unknown"),
                "software": data.get("software", {}).get("name", "unknown"),
                "response_time_ms": latency_ms,
                "url": sanitize_url(url),
            }
    except FhirOperationError:
        raise
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        raise FhirOperationError(
            operation="test-connection",
            url=url,
            status_code=None,
            outcome=None,
            latency_ms=latency_ms,
            cause=exc,
        )

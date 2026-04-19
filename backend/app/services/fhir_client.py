"""FHIR client with pluggable data acquisition strategy.

Handles all HTTP communication with HAPI FHIR servers (CDR and measure engine).
"""

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
                # Skip Group and MeasureReport resources — they reference
                # other patients and are not needed for evaluation
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
        """Translate dataRequirement entries to CDR REST queries and collect resources."""
        resources: list[dict[str, Any]] = []
        seen_types: set[str] = set()

        async with httpx.AsyncClient(timeout=60.0) as client:
            for req in requirements:
                resource_type = req.get("type", "")
                if not resource_type or not re.match(r"^[A-Za-z][A-Za-z0-9]{0,127}$", resource_type):
                    continue
                if resource_type in seen_types:
                    continue
                seen_types.add(resource_type)

                if resource_type == "Patient":
                    resp = await client.get(f"{cdr_url}/Patient/{patient_id}", headers=auth_headers)
                    if resp.status_code == 200:
                        resources.append(resp.json())
                else:
                    url = f"{cdr_url}/{resource_type}?subject=Patient/{patient_id}&_count=100"
                    resp = await client.get(url, headers=auth_headers)
                    if resp.status_code == 200:
                        for entry in resp.json().get("entry", []):
                            resource = entry.get("resource")
                            if resource:
                                resources.append(resource)

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


async def push_resources(
    resources: list[dict[str, Any]],
    target_url: Optional[str] = None,
) -> None:
    """POST a transaction Bundle of resources to the measure engine."""
    base = target_url or settings.MEASURE_ENGINE_URL
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": r,
                "request": {
                    "method": "PUT",
                    "url": f"{r['resourceType']}/{r['id']}",
                },
            }
            for r in resources
            if "resourceType" in r and "id" in r
        ],
    }
    if not bundle["entry"]:
        logger.warning("No valid resources to push")
        return
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            base,
            json=bundle,
            headers={"Content-Type": "application/fhir+json"},
        )
        # HAPI-0902: duplicate canonical URL+version — resources already loaded; treat as success
        if resp.status_code == 422 and "HAPI-0902" in resp.text:
            logger.info("Resources already present (HAPI-0902)", extra={"target": base})
            return
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
        logger.info(
            "Evaluating measure",
            extra={"measure_id": measure_id, "patient_id": patient_id},
        )
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


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


async def wipe_patient_data() -> None:
    """Delete patient-related data from the measure engine.

    Called at the START of a new job to clean up data from the prior run.
    This allows the previous job's evaluated resources to remain available
    for inspection until a new job begins.
    """
    resource_types = [
        "MeasureReport",
        "Patient",
        "Condition",
        "Observation",
        "Encounter",
        "Procedure",
        "MedicationRequest",
        "Immunization",
        "DiagnosticReport",
        "AllergyIntolerance",
        "CarePlan",
        "CareTeam",
        "Goal",
        "ServiceRequest",
        "Coverage",
        "Claim",
    ]
    async with httpx.AsyncClient(timeout=60.0) as client:
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
            except httpx.HTTPError:
                logger.warning(
                    "Failed to wipe resource type (may not exist)",
                    extra={"resourceType": rt},
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
    _validate_ssrf_url(fhir_url, label="cdr_url")
    headers = await _build_auth_headers(auth_type, auth_credentials)
    url = f"{fhir_url}/metadata"
    async with httpx.AsyncClient(timeout=15.0) as client:
        t0 = time.monotonic()
        resp = await client.get(url, headers=headers)
        response_time_ms = round((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "connected",
            "fhir_version": data.get("fhirVersion", "unknown"),
            "software": data.get("software", {}).get("name", "unknown"),
            "response_time": response_time_ms,
        }

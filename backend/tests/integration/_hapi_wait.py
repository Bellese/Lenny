"""HAPI FHIR readiness polling helpers.

Standalone functions callable from pytest fixtures and build-time seed scripts.
Logic is ported directly from conftest.py so both callers stay in sync.
"""

import sys
import time
import warnings

import httpx

REINDEX_TIMEOUT = 300
REINDEX_POLL_INTERVAL = 1

VALUESET_EXPANSION_TIMEOUT = 600
VALUESET_EXPANSION_POLL_INTERVAL = 2


def wait_for_metadata(base_url: str, timeout: int = 300) -> None:
    """Poll /fhir/metadata until HAPI responds or *timeout* seconds elapse.

    Args:
        base_url: FHIR base URL, e.g. ``http://localhost:8080/fhir``.
        timeout: Maximum seconds to wait before raising ``RuntimeError``.

    Raises:
        RuntimeError: If HAPI has not responded within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/metadata", timeout=10)
            if resp.status_code < 500:
                return
        except httpx.RequestError as exc:
            last_exc = exc
        time.sleep(2)
    raise RuntimeError(f"HAPI at {base_url} did not respond within {timeout}s. Last error: {last_exc}")


def trigger_reindex_and_wait(
    base_url: str,
    probe_patient_id: str,
    probe_encounter_id: str,
    timeout: int = REINDEX_TIMEOUT,
) -> None:
    """POST $reindex to HAPI and poll until Encounter?patient search returns results.

    Mirrors the ``_trigger_reindex_and_wait`` logic in conftest.py.

    Args:
        base_url: FHIR base URL, e.g. ``http://localhost:8080/fhir``.
        probe_patient_id: Patient resource ID whose Encounter must become searchable.
        probe_encounter_id: Encounter resource ID (unused in probe but kept for
            parity with conftest signature).
        timeout: Maximum seconds to wait before raising ``RuntimeError``.

    Raises:
        RuntimeError: If reference-param indexing does not complete within *timeout*.
    """
    headers = {"Content-Type": "application/fhir+json"}
    params = {
        "resourceType": "Parameters",
        "parameter": [{"name": "type", "valueString": "Encounter"}],
    }

    r = httpx.post(f"{base_url}/$reindex", json=params, headers=headers, timeout=30)
    if r.status_code >= 400:
        warnings.warn(f"$reindex trigger at {base_url} returned {r.status_code}: {r.text[:200]}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/Encounter?patient={probe_patient_id}&_count=1", timeout=10)
        if resp.status_code == 200:
            try:
                if resp.json().get("entry"):
                    return
            except Exception:
                pass
        time.sleep(REINDEX_POLL_INTERVAL)

    raise RuntimeError(
        f"HAPI at {base_url} reference-param indexing did not complete within {timeout}s "
        f"(probe: Encounter?patient={probe_patient_id})"
    )


def wait_for_valueset_expansion(
    base_url: str,
    large_valueset_ids: list[str] | None = None,
    timeout: int = VALUESET_EXPANSION_TIMEOUT,
) -> None:
    """Poll ValueSet/$expand until HAPI pre-expansion completes.

    HAPI's in-memory expansion is capped at 1000 codes.  ValueSets with
    >1000 codes are queued for async pre-expansion by a background scheduler.
    After pre-expansion completes, ``$expand`` returns HTTP 200 instead of
    HAPI-0831 500.

    If *large_valueset_ids* is ``None``, the function queries HAPI for all
    ValueSets and identifies those likely to need pre-expansion (more than 900
    compose concepts) automatically.

    Mirrors the ``_wait_for_valueset_expansion`` logic in conftest.py.

    Args:
        base_url: FHIR base URL, e.g. ``http://localhost:8080/fhir``.
        large_valueset_ids: Optional explicit list of ValueSet resource IDs to
            monitor.  When ``None``, IDs are discovered from the server.
        timeout: Maximum seconds to wait before issuing a warning and returning.
    """
    if large_valueset_ids is None:
        large_valueset_ids = _discover_large_valueset_ids(base_url)

    if not large_valueset_ids:
        return

    pending = set(large_valueset_ids)
    deadline = time.monotonic() + timeout

    while pending and time.monotonic() < deadline:
        newly_done: set[str] = set()
        for vs_id in list(pending):
            try:
                # count=2 so HAPI-0831 fires for any VS with >1 code until
                # background pre-expansion completes and HAPI can serve from DB.
                resp = httpx.get(f"{base_url}/ValueSet/{vs_id}/$expand?count=2", timeout=15)
                if resp.status_code == 200:
                    newly_done.add(vs_id)
            except httpx.RequestError:
                pass
        pending -= newly_done
        if pending:
            time.sleep(VALUESET_EXPANSION_POLL_INTERVAL)

    if pending:
        warnings.warn(
            f"HAPI at {base_url} ValueSet pre-expansion did not complete within "
            f"{timeout}s for {len(pending)} ValueSet(s): {sorted(pending)[:5]}. "
            f"Tests may fail with IP=0 if large ValueSets are still unexpanded."
        )


def _discover_large_valueset_ids(base_url: str, threshold: int = 900) -> list[str]:
    """Return IDs of ValueSets on the server that have ≥ *threshold* compose concepts.

    Used when the caller does not already know which ValueSets need pre-expansion.
    Returns an empty list if the server is unreachable or returns no ValueSets.
    """
    ids: list[str] = []
    try:
        resp = httpx.get(f"{base_url}/ValueSet?_count=200", timeout=30)
        if resp.status_code != 200:
            return ids
        bundle = resp.json()
        for entry in bundle.get("entry", []):
            vs = entry.get("resource", {})
            if vs.get("resourceType") != "ValueSet":
                continue
            vs_id = vs.get("id")
            if not vs_id:
                continue
            concept_count = sum(len(inc.get("concept", [])) for inc in vs.get("compose", {}).get("include", []))
            if concept_count >= threshold:
                ids.append(vs_id)
    except httpx.RequestError:
        pass
    return ids


def _find_probe_ids(base_url: str) -> tuple[str, str] | tuple[None, None]:
    """Return (patient_id, encounter_id) from the first Encounter found on the server."""
    try:
        resp = httpx.get(f"{base_url}/Encounter?_count=1", timeout=15)
        if resp.status_code != 200:
            return None, None
        entries = resp.json().get("entry", [])
        if not entries:
            return None, None
        enc = entries[0]["resource"]
        enc_id = enc.get("id")
        patient_ref = enc.get("subject", {}).get("reference", "")
        patient_id = patient_ref.removeprefix("Patient/")
        if enc_id and patient_id:
            return patient_id, enc_id
    except httpx.RequestError:
        pass
    return None, None


if __name__ == "__main__":
    # Usage: python _hapi_wait.py <base_url> [seed_type]
    # Runs all checks in sequence.
    # seed_type: "cdr" or "measure" (default "cdr")
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080/fhir"
    seed_type = sys.argv[2] if len(sys.argv) > 2 else "cdr"
    wait_for_metadata(base_url)
    probe_patient_id, probe_encounter_id = _find_probe_ids(base_url)
    if probe_patient_id and probe_encounter_id:
        trigger_reindex_and_wait(base_url, probe_patient_id, probe_encounter_id)
    if seed_type == "measure":
        wait_for_valueset_expansion(base_url)
    print("HAPI is ready.")

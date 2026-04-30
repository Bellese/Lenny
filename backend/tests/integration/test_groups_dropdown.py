"""Integration tests: Patient Group dropdown — 12 Groups present after seed.

Verifies that after a full seed run all 12 connectathon measures have a
corresponding FHIR Group resource on the CDR, and that the synthesized Groups
are not polluting the measure engine.

Run against a local stack that has completed its seed cycle:
    docker compose down -v && docker compose up -d
    cd backend && python -m pytest tests/integration/test_groups_dropdown.py -v
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest

pytestmark = pytest.mark.integration

_BUNDLE_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed" / "connectathon-bundles"
_MANIFEST = _BUNDLE_DIR / "manifest.json"


def _patient_count_from_bundle(bundle_file: str) -> int:
    path = _BUNDLE_DIR / bundle_file
    with open(path) as f:
        bundle = json.load(f)
    return sum(
        1
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == "Patient" and e.get("resource", {}).get("id")
    )


def _fetch_all_groups(base_url: str) -> dict[str, dict]:
    """Return {group_id: group_resource} for all Groups on the given server."""
    resp = httpx.get(f"{base_url}/Group", params={"_count": "100"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    groups: dict[str, dict] = {}
    for entry in data.get("entry", []):
        resource = entry.get("resource", {})
        gid = resource.get("id")
        if gid:
            groups[gid] = resource
    return groups


def test_all_12_groups_present_on_cdr(cdr_url: str) -> None:
    """CDR must expose one Group per connectathon measure after seeding."""
    with open(_MANIFEST) as f:
        manifest = json.load(f)

    expected_ids = {m["id"] for m in manifest["measures"]}
    assert len(expected_ids) == 12, f"Manifest should list 12 measures, got {len(expected_ids)}"

    cdr_groups = _fetch_all_groups(cdr_url)
    missing = expected_ids - set(cdr_groups)
    assert not missing, f"Missing Groups on CDR for: {sorted(missing)}. Present: {sorted(cdr_groups)}"


def test_group_member_counts_match_bundle_patient_counts(cdr_url: str) -> None:
    """Each Group's member count must equal the Patient count in its source bundle."""
    with open(_MANIFEST) as f:
        manifest = json.load(f)

    cdr_groups = _fetch_all_groups(cdr_url)

    mismatches = []
    for m in manifest["measures"]:
        measure_id = m["id"]
        expected = _patient_count_from_bundle(m["bundle_file"])
        group = cdr_groups.get(measure_id, {})
        actual = len(group.get("member", []))
        if expected != actual:
            mismatches.append(f"{measure_id}: bundle has {expected} Patients, Group has {actual} members")

    assert not mismatches, "Group member count mismatches:\n" + "\n".join(mismatches)


def test_cms1017_curated_group_extension_preserved(cdr_url: str) -> None:
    """CMS1017's bundle-supplied Group must still carry its artifact-testArtifact extension.

    This proves the skip-if-present logic did not overwrite the curated resource.
    """
    resp = httpx.get(f"{cdr_url}/Group/CMS1017FHIRHHFI", timeout=10)
    assert resp.status_code == 200, f"Group/CMS1017FHIRHHFI not found: {resp.status_code}"
    group = resp.json()
    extension_urls = [e.get("url", "") for e in group.get("extension", [])]
    assert any("artifact-testArtifact" in url for url in extension_urls), (
        f"CMS1017's curated artifact-testArtifact extension was lost. Extensions present: {extension_urls}"
    )


def test_synthesized_groups_not_on_measure_engine(measure_url: str) -> None:
    """Synthesized Groups should not appear on the measure engine.

    The measure engine doesn't need Groups; polluting it would be unexpected noise.
    CMS1017's Group may appear because it is part of clinical data loaded to both
    servers — that is pre-existing behavior and not tested here. We check that
    synthesized Groups (for the 11 bundles that originally lacked Groups) did not
    end up on the measure engine.

    Strategy: the 11 synthesized Group IDs (all except CMS1017) must not exist on
    the measure engine.
    """
    with open(_MANIFEST) as f:
        manifest = json.load(f)

    synthesized_ids = {m["id"] for m in manifest["measures"] if "CMS1017" not in m["id"]}
    measure_groups = _fetch_all_groups(measure_url)
    leaked = synthesized_ids & set(measure_groups)
    assert not leaked, f"Synthesized Groups unexpectedly present on measure engine: {sorted(leaked)}"

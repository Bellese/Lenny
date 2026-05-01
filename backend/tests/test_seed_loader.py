"""Unit tests for seed/load_seed_data.py — synthesize_group_from_patients."""

from __future__ import annotations

import pathlib
import sys

# Make the repo root importable so `seed.load_seed_data` resolves
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from seed.load_seed_data import synthesize_group_from_patients


def _make_bundle(*resource_types: str, include_group: bool = False) -> dict:
    """Build a minimal FHIR bundle with the given resource types."""
    entries = []
    patient_counter = 0
    for rt in resource_types:
        if rt == "Patient":
            patient_counter += 1
            entries.append({"resource": {"resourceType": "Patient", "id": f"pat-{patient_counter}"}})
        elif rt == "Encounter":
            entries.append({"resource": {"resourceType": "Encounter", "id": "enc-1"}})
        else:
            entries.append({"resource": {"resourceType": rt, "id": f"{rt.lower()}-1"}})
    if include_group:
        entries.append(
            {
                "resource": {
                    "resourceType": "Group",
                    "id": "EXISTING-GROUP",
                    "type": "person",
                    "actual": True,
                    "extension": [{"url": "http://hl7.org/fhir/StructureDefinition/artifact-testArtifact"}],
                }
            }
        )
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


class TestSynthesizeGroupFromPatients:
    def test_returns_group_for_bundle_with_patients(self):
        bundle = _make_bundle("Patient", "Patient", "Patient", "Encounter")
        group = synthesize_group_from_patients(bundle, "CMS122FHIR")

        assert group is not None
        assert group["resourceType"] == "Group"
        assert group["id"] == "CMS122FHIR"
        assert group["name"] == "CMS122FHIR"
        assert group["type"] == "person"
        assert group["actual"] is True
        assert len(group["member"]) == 3
        refs = {m["entity"]["reference"] for m in group["member"]}
        assert refs == {"Patient/pat-1", "Patient/pat-2", "Patient/pat-3"}

    def test_returns_none_when_bundle_already_has_group(self):
        bundle = _make_bundle("Patient", "Patient", include_group=True)
        result = synthesize_group_from_patients(bundle, "CMS1017FHIRHHFI")
        assert result is None, "Should skip synthesis when Group already exists"

    def test_returns_none_when_no_patients(self):
        bundle = _make_bundle("Encounter", "Condition")
        result = synthesize_group_from_patients(bundle, "CMS122FHIR")
        assert result is None, "Should skip synthesis when bundle has no Patients"

    def test_returns_none_for_empty_bundle(self):
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": []}
        result = synthesize_group_from_patients(bundle, "CMS122FHIR")
        assert result is None

    def test_group_id_and_name_equal_measure_id(self):
        bundle = _make_bundle("Patient")
        group = synthesize_group_from_patients(bundle, "CMS816FHIRHHHypo")
        assert group is not None
        assert group["id"] == "CMS816FHIRHHHypo"
        assert group["name"] == "CMS816FHIRHHHypo"

    def test_non_patient_resources_not_included_as_members(self):
        bundle = _make_bundle("Patient", "Encounter", "Condition")
        group = synthesize_group_from_patients(bundle, "CMS122FHIR")
        assert group is not None
        assert len(group["member"]) == 1
        assert group["member"][0]["entity"]["reference"] == "Patient/pat-1"

    def test_skips_patient_entries_without_id(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "pat-1"}},
                {"resource": {"resourceType": "Patient"}},  # no id
                {"resource": {"resourceType": "Patient", "id": "pat-3"}},
            ],
        }
        group = synthesize_group_from_patients(bundle, "CMS122FHIR")
        assert group is not None
        assert len(group["member"]) == 2
        refs = {m["entity"]["reference"] for m in group["member"]}
        assert refs == {"Patient/pat-1", "Patient/pat-3"}

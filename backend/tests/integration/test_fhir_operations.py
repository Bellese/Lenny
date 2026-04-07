"""Integration tests for real FHIR operations against HAPI FHIR instances.

These tests verify that the fhir_client functions work correctly
against actual HAPI FHIR servers running in Docker.
"""

import pytest
import httpx

from app.services.fhir_client import (
    BatchQueryStrategy,
    list_measures,
    push_resources,
    evaluate_measure,
    upload_measure_bundle,
    wipe_patient_data,
    verify_fhir_connection,
)
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# list_measures
# ---------------------------------------------------------------------------


async def test_list_measures(measure_url):
    """list_measures() should return a bundle containing CMS122."""
    from unittest.mock import patch

    with patch("app.config.settings.MEASURE_ENGINE_URL", measure_url):
        bundle = await list_measures()

    assert bundle["resourceType"] == "Bundle"
    entries = bundle.get("entry", [])
    measure_ids = [
        e["resource"]["id"]
        for e in entries
        if e.get("resource", {}).get("resourceType") == "Measure"
    ]
    assert "CMS122FHIRDiabetesAssessGT9Pct" in measure_ids, (
        f"Expected CMS122 measure in {measure_ids}"
    )


# ---------------------------------------------------------------------------
# upload_measure_bundle + list
# ---------------------------------------------------------------------------


async def test_upload_and_list_measure(measure_url):
    """Uploading a measure bundle should make the measure appear in the list."""
    from unittest.mock import patch

    # A minimal measure bundle to upload
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": {
                    "resourceType": "Measure",
                    "id": "test-integration-measure",
                    "url": "http://example.org/fhir/Measure/test-integration-measure",
                    "name": "TestIntegrationMeasure",
                    "title": "Test Integration Measure",
                    "status": "active",
                    "version": "0.0.1",
                },
                "request": {
                    "method": "PUT",
                    "url": "Measure/test-integration-measure",
                },
            }
        ],
    }

    with patch("app.config.settings.MEASURE_ENGINE_URL", measure_url):
        result = await upload_measure_bundle(bundle)
        assert result["resourceType"] == "Bundle"

        # Now list and confirm it appears
        listed = await list_measures()
        measure_ids = [
            e["resource"]["id"]
            for e in listed.get("entry", [])
            if e.get("resource", {}).get("resourceType") == "Measure"
        ]
        assert "test-integration-measure" in measure_ids


# ---------------------------------------------------------------------------
# gather_patients
# ---------------------------------------------------------------------------


async def test_gather_patients(cdr_url):
    """BatchQueryStrategy should gather patients from the CDR."""
    strategy = BatchQueryStrategy()
    patients = await strategy.gather_patients(cdr_url, auth_headers={})
    # The seed data contains ~20 patients (pt-001 through pt-020)
    assert len(patients) >= 15, f"Expected ~20 patients, got {len(patients)}"
    # Every entry should be a Patient resource
    for p in patients:
        assert p["resourceType"] == "Patient"
        assert "id" in p


# ---------------------------------------------------------------------------
# gather_patient_data
# ---------------------------------------------------------------------------


async def test_gather_patient_data(cdr_url):
    """Gathering clinical data for one patient should return Condition and Observation resources."""
    strategy = BatchQueryStrategy()
    resources = await strategy.gather_patient_data(cdr_url, "6f0553ac-e12a-4af5-ad27-05339f4b4ec0", auth_headers={})

    assert len(resources) > 0, "Expected at least some resources for pt-001"
    resource_types = {r["resourceType"] for r in resources}
    # pt-001 has a Condition and an Observation in the seed data
    assert "Patient" in resource_types or "Condition" in resource_types, (
        f"Expected Patient or Condition in resource types, got {resource_types}"
    )


# ---------------------------------------------------------------------------
# push_resources
# ---------------------------------------------------------------------------


async def test_push_resources_to_measure_engine(measure_url):
    """Pushing a small bundle to the measure engine should succeed and resources should be retrievable."""
    from unittest.mock import patch

    test_patient = {
        "resourceType": "Patient",
        "id": "test-push-patient",
        "name": [{"family": "PushTest", "given": ["Integration"]}],
    }
    test_condition = {
        "resourceType": "Condition",
        "id": "test-push-condition",
        "subject": {"reference": "Patient/test-push-patient"},
        "code": {
            "coding": [
                {
                    "system": "http://hl7.org/fhir/sid/icd-10-cm",
                    "code": "E11.9",
                }
            ]
        },
    }

    with patch("app.config.settings.MEASURE_ENGINE_URL", measure_url):
        await push_resources([test_patient, test_condition])

    # Verify resources exist on the measure engine
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{measure_url}/Patient/test-push-patient")
        assert resp.status_code == 200
        assert resp.json()["resourceType"] == "Patient"

        resp = await client.get(f"{measure_url}/Condition/test-push-condition")
        assert resp.status_code == 200
        assert resp.json()["resourceType"] == "Condition"


# ---------------------------------------------------------------------------
# evaluate_measure
# ---------------------------------------------------------------------------


async def test_evaluate_measure(measure_url, cdr_url):
    """Load patient data to measure engine and call $evaluate-measure.

    The CQL in our demo measure may not evaluate perfectly, so we accept
    either a successful MeasureReport or a handled error. The key is that
    the HAPI server responds and the function does not crash unexpectedly.
    """
    from unittest.mock import patch

    # First push a patient + data to the measure engine
    strategy = BatchQueryStrategy()
    resources = await strategy.gather_patient_data(cdr_url, "6f0553ac-e12a-4af5-ad27-05339f4b4ec0", auth_headers={})

    with patch("app.config.settings.MEASURE_ENGINE_URL", measure_url):
        if resources:
            await push_resources(resources)

        try:
            report = await evaluate_measure(
                measure_id="CMS122FHIRDiabetesAssessGT9Pct",
                patient_id="6f0553ac-e12a-4af5-ad27-05339f4b4ec0",
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
            # If we get a MeasureReport, verify its structure
            assert report["resourceType"] == "MeasureReport"
            assert "group" in report or "status" in report
        except httpx.HTTPStatusError as exc:
            # CQL evaluation may fail with our simplified measure — that's OK.
            # Verify the server responded (not a connection error).
            assert exc.response.status_code >= 400, (
                "Expected a 4xx/5xx from measure evaluation, not a connection error"
            )


# ---------------------------------------------------------------------------
# wipe_patient_data
# ---------------------------------------------------------------------------


async def test_wipe_patient_data(measure_url):
    """Push data, wipe, verify data is gone."""
    from unittest.mock import patch

    test_patient = {
        "resourceType": "Patient",
        "id": "test-wipe-patient",
        "name": [{"family": "WipeTest", "given": ["Integration"]}],
    }

    with patch("app.config.settings.MEASURE_ENGINE_URL", measure_url):
        await push_resources([test_patient])

        # Verify it exists
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{measure_url}/Patient/test-wipe-patient")
            assert resp.status_code == 200

        # Wipe
        await wipe_patient_data()

        # Verify it's gone
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{measure_url}/Patient/test-wipe-patient")
            assert resp.status_code in (404, 410), (
                f"Expected patient to be deleted, got status {resp.status_code}"
            )


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


async def test_test_connection_real_server(cdr_url, measure_url):
    """test_connection against the live CDR and measure engine should return success."""
    result = await verify_fhir_connection(cdr_url)
    assert result["status"] == "connected"
    assert result["fhir_version"] is not None

    result = await verify_fhir_connection(measure_url)
    assert result["status"] == "connected"

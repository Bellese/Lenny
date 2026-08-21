"""Tests for the DEQM data-exchange payload builders (deqm.py)."""

from app.services.deqm import (
    DEQM_DATA_EXCHANGE_PROFILE,
    DEQM_UPDATE_TYPE_EXT,
    LENNY_REPORTER_ORG,
    build_base_parameters,
    build_data_exchange_measure_report,
    build_stu5_parameters,
)

_RESOURCES = [
    {"resourceType": "Patient", "id": "p1"},
    {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}},
    {"resourceType": "Encounter", "id": "e1"},
]


def _mr() -> dict:
    return build_data_exchange_measure_report(
        job_id=42,
        patient_id="p1",
        measure_canonical="http://example.org/Measure/CMS122|1.0.0",
        period_start="2025-01-01",
        period_end="2025-12-31",
        resources=_RESOURCES,
        timestamp="2026-08-21T12:00:00+00:00",
    )


class TestBuildDataExchangeMeasureReport:
    def test_required_deqm_elements(self):
        mr = _mr()
        assert mr["resourceType"] == "MeasureReport"
        assert mr["id"] == "deqm-42-p1"
        assert mr["meta"]["profile"] == [DEQM_DATA_EXCHANGE_PROFILE]
        assert mr["status"] == "complete"
        assert mr["type"] == "data-collection"
        assert mr["measure"] == "http://example.org/Measure/CMS122|1.0.0"
        assert mr["subject"] == {"reference": "Patient/p1"}
        assert mr["date"] == "2026-08-21T12:00:00+00:00"
        assert mr["reporter"] == {"reference": "Organization/lenny-reporter"}
        assert mr["period"] == {"start": "2025-01-01", "end": "2025-12-31"}

    def test_snapshot_update_type_extension(self):
        mr = _mr()
        assert {"url": DEQM_UPDATE_TYPE_EXT, "valueCode": "snapshot"} in mr["extension"]

    def test_evaluated_resources_reference_all_submitted(self):
        mr = _mr()
        refs = [er["reference"] for er in mr["evaluatedResource"]]
        assert refs == ["Patient/p1", "Condition/c1", "Encounter/e1"]

    def test_no_group_score_or_stratifier(self):
        mr = _mr()
        assert "group" not in mr  # profile prohibits measureScore/stratifier

    def test_resources_without_ids_are_skipped_in_evaluated_resource(self):
        mr = build_data_exchange_measure_report(
            job_id=1,
            patient_id="p1",
            measure_canonical="http://example.org/Measure/M",
            period_start="2025-01-01",
            period_end="2025-12-31",
            resources=[{"resourceType": "Observation"}],  # no id
            timestamp="2026-08-21T12:00:00+00:00",
        )
        assert mr["evaluatedResource"] == []


class TestParameterEnvelopes:
    def test_stu5_parameters_single_bundle(self):
        mr = _mr()
        params = build_stu5_parameters(mr, [LENNY_REPORTER_ORG, *_RESOURCES])
        assert params["resourceType"] == "Parameters"
        assert len(params["parameter"]) == 1
        p = params["parameter"][0]
        assert p["name"] == "bundle"
        bundle = p["resource"]
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "collection"
        entry_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        # MeasureReport first, then reporter org + data-of-interest
        assert entry_types == ["MeasureReport", "Organization", "Patient", "Condition", "Encounter"]

    def test_base_parameters_measurereport_plus_resources(self):
        mr = _mr()
        params = build_base_parameters(mr, [LENNY_REPORTER_ORG, *_RESOURCES])
        assert params["resourceType"] == "Parameters"
        names = [p["name"] for p in params["parameter"]]
        assert names == ["measureReport", "resource", "resource", "resource", "resource"]
        assert params["parameter"][0]["resource"] is mr
        assert params["parameter"][1]["resource"]["resourceType"] == "Organization"

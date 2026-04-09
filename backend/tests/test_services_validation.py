"""Tests for validation service — bundle triage, population extraction, comparison."""

import pytest

from app.services.validation import (
    _extract_population_counts,
    _extract_test_case_info,
    _is_test_case_measure_report,
    _classify_bundle_entries,
    compare_populations,
    sanitize_error,
)


# ---------------------------------------------------------------------------
# sanitize_error
# ---------------------------------------------------------------------------


class TestSanitizeError:
    def test_url_replaced_with_placeholder(self):
        exc = Exception("Failed to connect to http://example.com/fhir/Patient")
        result = sanitize_error(exc)
        assert "http://example.com" not in result
        assert "[url]" in result

    def test_internal_hostname_url_stripped(self):
        exc = Exception(
            "HTTPStatusError: 404 Not Found for url http://hapi-fhir-measure:8080/fhir/Measure/$evaluate-measure"
        )
        result = sanitize_error(exc)
        assert "hapi-fhir-measure:8080" not in result
        assert "[url]" in result

    def test_schemeless_hostport_stripped(self):
        # httpx ConnectError can emit bare hostname:port without http:// prefix,
        # e.g. "[Errno 111] Connection refused (while connecting to ('hapi-fhir-cdr', 8080))"
        exc = Exception(
            "[Errno 111] Connection refused (while connecting to hapi-fhir-cdr:8080)"
        )
        result = sanitize_error(exc)
        assert "hapi-fhir-cdr" not in result
        assert "8080" not in result
        assert "[host]" in result

    def test_auth_header_redacted(self):
        exc = Exception("Request failed: Authorization: Bearer supersecrettoken123")
        result = sanitize_error(exc)
        assert "supersecrettoken123" not in result
        assert "redacted" in result

    def test_safe_message_unchanged(self):
        exc = Exception("Measure not found on engine")
        result = sanitize_error(exc)
        assert result == "Measure not found on engine"

    def test_very_long_message_truncated(self):
        long_msg = "x" * 5000
        exc = Exception(long_msg)
        result = sanitize_error(exc)
        assert len(result) <= 2000

    def test_http_status_code_not_mangled(self):
        # Regression: _HOSTPORT_RE must not false-positive on "code:404" or
        # similar non-hostname strings that contain a colon followed by digits.
        exc = Exception("FHIR server returned HTTP status code:404, line:30")
        result = sanitize_error(exc)
        assert "[host]" not in result
        assert "404" in result
        assert "30" in result

    def test_str_exc_crash_returns_fallback(self):
        # Regression: if str(exc) raises, sanitize_error should return a safe fallback.
        class BadExc(Exception):
            def __str__(self):
                raise RuntimeError("str failed")

        result = sanitize_error(BadExc())
        assert "BadExc" in result
        assert "str() raised" in result


# ---------------------------------------------------------------------------
# _extract_population_counts
# ---------------------------------------------------------------------------


class TestExtractPopulationCounts:
    def test_happy_path(self, mock_measure_report):
        result = _extract_population_counts(mock_measure_report)
        assert result == {
            "initial-population": 1,
            "denominator": 1,
            "numerator": 1,
            "denominator-exclusion": 0,
            "numerator-exclusion": 0,
        }

    def test_empty_groups(self):
        result = _extract_population_counts({"group": []})
        assert result == {}

    def test_missing_groups(self):
        result = _extract_population_counts({})
        assert result == {}

    def test_zero_counts(self):
        report = {
            "group": [{
                "population": [
                    {"code": {"coding": [{"code": "initial-population"}]}, "count": 0},
                    {"code": {"coding": [{"code": "denominator"}]}, "count": 0},
                ]
            }]
        }
        result = _extract_population_counts(report)
        assert result["initial-population"] == 0
        assert result["denominator"] == 0

    def test_unknown_code_skipped(self):
        report = {
            "group": [{
                "population": [
                    {"code": {"coding": [{"code": "unknown-code"}]}, "count": 5},
                    {"code": {"coding": [{"code": "numerator"}]}, "count": 1},
                ]
            }]
        }
        result = _extract_population_counts(report)
        assert "unknown-code" not in result
        assert result["numerator"] == 1

    def test_multiple_groups_merged(self):
        report = {
            "group": [
                {"population": [
                    {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                ]},
                {"population": [
                    {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                ]},
            ]
        }
        result = _extract_population_counts(report)
        assert result["initial-population"] == 1
        assert result["denominator"] == 1


# ---------------------------------------------------------------------------
# compare_populations
# ---------------------------------------------------------------------------


class TestComparePopulations:
    def test_all_match(self):
        expected = {"initial-population": 1, "denominator": 1, "numerator": 0}
        actual = {"initial-population": 1, "denominator": 1, "numerator": 0}
        passed, mismatches = compare_populations(expected, actual)
        assert passed is True
        assert mismatches == []

    def test_single_mismatch(self):
        expected = {"initial-population": 1, "denominator": 1, "numerator": 1}
        actual = {"initial-population": 1, "denominator": 1, "numerator": 0}
        passed, mismatches = compare_populations(expected, actual)
        assert passed is False
        assert mismatches == ["numerator"]

    def test_multiple_mismatches(self):
        expected = {"initial-population": 1, "denominator": 1, "numerator": 1}
        actual = {"initial-population": 0, "denominator": 0, "numerator": 0}
        passed, mismatches = compare_populations(expected, actual)
        assert passed is False
        assert len(mismatches) == 3

    def test_absent_actual_treated_as_zero(self):
        expected = {"initial-population": 1, "numerator": 0}
        actual = {}
        passed, mismatches = compare_populations(expected, actual)
        assert passed is False
        assert "initial-population" in mismatches
        # numerator: expected 0, actual 0 (absent=0) → match
        assert "numerator" not in mismatches

    def test_extra_actual_codes_ignored(self):
        expected = {"numerator": 1}
        actual = {"numerator": 1, "denominator": 1, "initial-population": 1}
        passed, mismatches = compare_populations(expected, actual)
        assert passed is True

    def test_empty_expected(self):
        passed, mismatches = compare_populations({}, {"numerator": 1})
        assert passed is True
        assert mismatches == []


# ---------------------------------------------------------------------------
# _is_test_case_measure_report
# ---------------------------------------------------------------------------


class TestIsTestCase:
    def test_valid_test_case(self):
        resource = {
            "resourceType": "MeasureReport",
            "modifierExtension": [{
                "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase",
                "valueBoolean": True,
            }],
        }
        assert _is_test_case_measure_report(resource) is True

    def test_not_test_case(self):
        resource = {"resourceType": "MeasureReport"}
        assert _is_test_case_measure_report(resource) is False

    def test_false_value(self):
        resource = {
            "resourceType": "MeasureReport",
            "modifierExtension": [{
                "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase",
                "valueBoolean": False,
            }],
        }
        assert _is_test_case_measure_report(resource) is False


# ---------------------------------------------------------------------------
# _extract_test_case_info
# ---------------------------------------------------------------------------


class TestExtractTestCaseInfo:
    def test_extracts_all_fields(self, mock_test_bundle_with_expected):
        # Get the MeasureReport entry
        mr = None
        for entry in mock_test_bundle_with_expected["entry"]:
            if entry["resource"]["resourceType"] == "MeasureReport":
                mr = entry["resource"]
                break
        assert mr is not None
        info = _extract_test_case_info(mr)
        assert info is not None
        assert info["measure_url"] == "https://example.com/Measure/CMS124"
        assert info["patient_ref"] == "test-patient-1"
        assert info["test_description"] == "Female 24yo, cervical cytology 2yrs prior"
        assert info["period_start"] == "2026-01-01"
        assert info["period_end"] == "2026-12-31"
        assert info["expected_populations"]["initial-population"] == 1
        assert info["expected_populations"]["numerator"] == 1

    def test_missing_measure_url(self):
        mr = {"resourceType": "MeasureReport", "period": {"start": "2026-01-01", "end": "2026-12-31"}}
        assert _extract_test_case_info(mr) is None

    def test_missing_patient_ref(self):
        mr = {
            "resourceType": "MeasureReport",
            "measure": "https://example.com/Measure/X",
            "period": {"start": "2026-01-01", "end": "2026-12-31"},
        }
        assert _extract_test_case_info(mr) is None

    def test_missing_period(self):
        mr = {
            "resourceType": "MeasureReport",
            "measure": "https://example.com/Measure/X",
            "contained": [{
                "resourceType": "Parameters",
                "parameter": [{"name": "subject", "valueString": "p1"}],
            }],
        }
        assert _extract_test_case_info(mr) is None


# ---------------------------------------------------------------------------
# _classify_bundle_entries
# ---------------------------------------------------------------------------


class TestClassifyBundleEntries:
    def test_classifies_correctly(self, mock_test_bundle_with_expected):
        measure_defs, clinical, test_cases = _classify_bundle_entries(
            mock_test_bundle_with_expected
        )
        # Measure + Library = 2 measure defs
        assert len(measure_defs) == 2
        assert measure_defs[0]["resourceType"] == "Measure"
        assert measure_defs[1]["resourceType"] == "Library"
        # Patient + Observation = 2 clinical
        assert len(clinical) == 2
        # 1 test case MeasureReport
        assert len(test_cases) == 1
        assert test_cases[0]["patient_ref"] == "test-patient-1"

    def test_empty_bundle(self):
        measure_defs, clinical, test_cases = _classify_bundle_entries(
            {"resourceType": "Bundle", "entry": []}
        )
        assert len(measure_defs) == 0
        assert len(clinical) == 0
        assert len(test_cases) == 0

    def test_skips_entries_without_resource(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"request": {"method": "DELETE", "url": "Patient/1"}},
                {"resource": {"resourceType": "Patient", "id": "p1"}},
            ],
        }
        measure_defs, clinical, test_cases = _classify_bundle_entries(bundle)
        assert len(clinical) == 1

    def test_non_test_case_measure_report_skipped(self):
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {
                    "resource": {
                        "resourceType": "MeasureReport",
                        "id": "regular-report",
                        "status": "complete",
                    }
                }
            ],
        }
        measure_defs, clinical, test_cases = _classify_bundle_entries(bundle)
        assert len(measure_defs) == 0
        assert len(clinical) == 0
        assert len(test_cases) == 0

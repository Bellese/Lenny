"""Tests for validation service — bundle triage, population extraction, comparison."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.models.validation import ExpectedResult, ValidationResult, ValidationRun, ValidationStatus
from app.services.validation import (
    _classify_bundle_entries,
    _extract_patient_name,
    _extract_population_counts,
    _extract_test_case_info,
    _fix_valueset_compose_for_hapi,
    _is_test_case_measure_report,
    _resolve_measure_id,
    _warn_unknown_bundle_types,
    compare_populations,
    process_bundle_upload,
    run_validation,
    sanitize_error,
    triage_test_bundle,
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
        exc = Exception("[Errno 111] Connection refused (while connecting to hapi-fhir-cdr:8080)")
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
            "group": [
                {
                    "population": [
                        {"code": {"coding": [{"code": "initial-population"}]}, "count": 0},
                        {"code": {"coding": [{"code": "denominator"}]}, "count": 0},
                    ]
                }
            ]
        }
        result = _extract_population_counts(report)
        assert result["initial-population"] == 0
        assert result["denominator"] == 0

    def test_unknown_code_skipped(self):
        report = {
            "group": [
                {
                    "population": [
                        {"code": {"coding": [{"code": "unknown-code"}]}, "count": 5},
                        {"code": {"coding": [{"code": "numerator"}]}, "count": 1},
                    ]
                }
            ]
        }
        result = _extract_population_counts(report)
        assert "unknown-code" not in result
        assert result["numerator"] == 1

    def test_multiple_groups_merged(self):
        report = {
            "group": [
                {
                    "population": [
                        {"code": {"coding": [{"code": "initial-population"}]}, "count": 1},
                    ]
                },
                {
                    "population": [
                        {"code": {"coding": [{"code": "denominator"}]}, "count": 1},
                    ]
                },
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
            "modifierExtension": [
                {
                    "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase",
                    "valueBoolean": True,
                }
            ],
        }
        assert _is_test_case_measure_report(resource) is True

    def test_not_test_case(self):
        resource = {"resourceType": "MeasureReport"}
        assert _is_test_case_measure_report(resource) is False

    def test_false_value(self):
        resource = {
            "resourceType": "MeasureReport",
            "modifierExtension": [
                {
                    "url": "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase",
                    "valueBoolean": False,
                }
            ],
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
            "contained": [
                {
                    "resourceType": "Parameters",
                    "parameter": [{"name": "subject", "valueString": "p1"}],
                }
            ],
        }
        assert _extract_test_case_info(mr) is None


# ---------------------------------------------------------------------------
# _classify_bundle_entries
# ---------------------------------------------------------------------------


class TestClassifyBundleEntries:
    def test_classifies_correctly(self, mock_test_bundle_with_expected):
        measure_defs, clinical, test_cases = _classify_bundle_entries(mock_test_bundle_with_expected)
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
        measure_defs, clinical, test_cases = _classify_bundle_entries({"resourceType": "Bundle", "entry": []})
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


# ---------------------------------------------------------------------------
# _warn_unknown_bundle_types
# ---------------------------------------------------------------------------


class TestWarnUnknownBundleTypes:
    def test_known_clinical_types_no_warning(self, caplog):
        """Known clinical types produce no warning."""
        import logging

        bundle = {
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "p1"}},
                {"resource": {"resourceType": "Condition", "id": "c1"}},
                {"resource": {"resourceType": "DeviceRequest", "id": "dr1"}},
                {"resource": {"resourceType": "MedicationAdministration", "id": "ma1"}},
                {"resource": {"resourceType": "AdverseEvent", "id": "ae1"}},
            ]
        }
        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types(bundle)
        assert not caplog.records

    def test_measure_def_types_no_warning(self, caplog):
        """Measure def types (Measure, Library, ValueSet, CodeSystem) produce no warning."""
        import logging

        bundle = {
            "entry": [
                {"resource": {"resourceType": "Measure", "id": "m1"}},
                {"resource": {"resourceType": "Library", "id": "l1"}},
                {"resource": {"resourceType": "ValueSet", "id": "vs1"}},
                {"resource": {"resourceType": "CodeSystem", "id": "cs1"}},
            ]
        }
        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types(bundle)
        assert not caplog.records

    def test_measure_report_no_warning(self, caplog):
        """MeasureReport (test case container) produces no warning."""
        import logging

        bundle = {"entry": [{"resource": {"resourceType": "MeasureReport", "id": "mr1"}}]}
        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types(bundle)
        assert not caplog.records

    def test_unknown_type_emits_warning(self, caplog):
        """An unrecognised resource type triggers a warning log."""
        import logging

        bundle = {"entry": [{"resource": {"resourceType": "InventoryItem", "id": "x1"}}]}
        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types(bundle)
        # extra fields are attached directly as LogRecord attributes by structlog/stdlib
        assert any(
            getattr(r, "resourceType", None) == "InventoryItem" or "InventoryItem" in r.getMessage()
            for r in caplog.records
        )

    def test_duplicate_unknown_type_warns_once(self, caplog):
        """The same unknown type present multiple times only generates one warning."""
        import logging

        bundle = {
            "entry": [
                {"resource": {"resourceType": "InventoryItem", "id": "x1"}},
                {"resource": {"resourceType": "InventoryItem", "id": "x2"}},
            ]
        }
        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types(bundle)
        warning_count = sum(
            1
            for r in caplog.records
            if getattr(r, "resourceType", None) == "InventoryItem" or "InventoryItem" in r.getMessage()
        )
        assert warning_count == 1

    def test_empty_bundle_no_warning(self, caplog):
        """Empty bundle produces no warnings."""
        import logging

        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types({"entry": []})
        assert not caplog.records

    def test_skips_entries_without_resource(self, caplog):
        """Entries missing 'resource' key are silently skipped."""
        import logging

        bundle = {"entry": [{"request": {"method": "DELETE"}}]}
        with caplog.at_level(logging.WARNING, logger="app.services.validation"):
            _warn_unknown_bundle_types(bundle)
        assert not caplog.records


# ---------------------------------------------------------------------------
# _extract_patient_name
# ---------------------------------------------------------------------------


class TestExtractPatientName:
    def test_given_and_family(self):
        patient = {"name": [{"given": ["Alice"], "family": "Test"}]}
        assert _extract_patient_name(patient) == "Alice Test"

    def test_multiple_given_names(self):
        patient = {"name": [{"given": ["John", "Paul"], "family": "Smith"}]}
        assert _extract_patient_name(patient) == "John Paul Smith"

    def test_given_only(self):
        patient = {"name": [{"given": ["Alice"]}]}
        assert _extract_patient_name(patient) == "Alice"

    def test_family_only(self):
        patient = {"name": [{"family": "Smith"}]}
        assert _extract_patient_name(patient) == "Smith"

    def test_empty_name_list(self):
        patient = {"name": []}
        assert _extract_patient_name(patient) is None

    def test_no_name_key(self):
        patient = {}
        assert _extract_patient_name(patient) is None

    def test_name_object_with_no_parts(self):
        patient = {"name": [{}]}
        assert _extract_patient_name(patient) is None

    def test_returns_first_usable_name(self):
        patient = {
            "name": [
                {},  # empty — no parts
                {"given": ["Bob"], "family": "Jones"},
            ]
        }
        assert _extract_patient_name(patient) == "Bob Jones"


# ---------------------------------------------------------------------------
# triage_test_bundle (async, mocked external calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTriageTestBundle:
    async def test_happy_path_default_cdr(self, test_session, mock_test_bundle_with_expected):
        """Measure defs and clinical data pushed when CDR is default; expected result upserted."""
        with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
            with patch("app.services.validation.settings") as mock_settings:
                mock_settings.DEFAULT_CDR_URL = "http://hapi-fhir-cdr:8080/fhir"
                result = await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        # Measure + Library = 2 measure defs → push_resources called once for defs
        assert result["measures_loaded"] == 1  # only Measure type counts
        assert result["expected_results_loaded"] == 1  # one isTestCase MeasureReport
        # clinical data (Patient + Observation) was pushed because CDR is not read-only
        assert result["patients_loaded"] == 1  # one Patient resource
        assert result.get("warning_message") is None
        assert mock_push.call_count >= 1

    async def test_external_cdr_clinical_is_pushed(self, test_session, mock_test_bundle_with_expected):
        """When an external CDR is active, clinical data IS pushed to that CDR."""
        from app.models.config import AuthType, CDRConfig

        # Insert an active external CDR config (marked read-only in DB, but guard is removed)
        external_cdr = CDRConfig(
            cdr_url="http://external-cdr.example.com/fhir",
            auth_type=AuthType.none,
            is_active=True,
            name="External CDR",
            is_default=False,
            is_read_only=True,
        )
        test_session.add(external_cdr)
        await test_session.commit()

        with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
            result = await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        # clinical data IS pushed to the external CDR
        assert result["patients_loaded"] == 1
        assert result.get("warning_message") is None
        # Verify push_resources was called with the external CDR URL for clinical data
        clinical_push_calls = [
            call
            for call in mock_push.call_args_list
            if call.kwargs.get("target_url") == "http://external-cdr.example.com/fhir"
        ]
        assert len(clinical_push_calls) == 1
        # Measure defs are still pushed (to measure engine, not the external CDR)
        push_calls_for_defs = [
            call
            for call in mock_push.call_args_list
            if call.kwargs.get("target_url") != "http://external-cdr.example.com/fhir"
        ]
        assert len(push_calls_for_defs) == 1

    async def test_external_cdr_with_auth_forwards_headers(self, test_session, mock_test_bundle_with_expected):
        """When an external CDR has basic auth configured, auth headers are forwarded."""
        from app.models.config import AuthType, CDRConfig

        # Insert an active external CDR config with basic auth
        external_cdr = CDRConfig(
            cdr_url="http://external-cdr.example.com/fhir",
            is_active=True,
            auth_type=AuthType.basic,
            auth_credentials={"username": "user", "password": "pass"},
        )
        test_session.add(external_cdr)
        await test_session.commit()

        with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
            with patch("app.services.validation.settings") as mock_settings:
                mock_settings.DEFAULT_CDR_URL = "http://hapi-fhir-cdr:8080/fhir"
                result = await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        # clinical data pushed with auth headers
        assert result["patients_loaded"] == 1
        clinical_push_calls = [
            call
            for call in mock_push.call_args_list
            if call.kwargs.get("target_url") == "http://external-cdr.example.com/fhir"
        ]
        assert len(clinical_push_calls) == 1
        auth_headers = clinical_push_calls[0].kwargs.get("auth_headers", {})
        assert "Authorization" in auth_headers
        assert auth_headers["Authorization"] == "Basic dXNlcjpwYXNz"

        # Measure-def push must NOT carry CDR auth headers
        def_push_calls = [
            call
            for call in mock_push.call_args_list
            if call.kwargs.get("target_url") != "http://external-cdr.example.com/fhir"
        ]
        for def_call in def_push_calls:
            assert "Authorization" not in (def_call.kwargs.get("auth_headers") or {})

    async def test_bundle_with_only_measure_defs(self, test_session):
        """Bundle containing only measure defs: no expected results, no clinical push."""
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Measure",
                        "id": "m1",
                        "url": "http://example.com/m1",
                        "status": "active",
                    }
                },  # noqa: E501
                {
                    "resource": {
                        "resourceType": "Library",
                        "id": "lib1",
                        "url": "http://example.com/lib1",
                        "status": "active",
                    }
                },  # noqa: E501
            ],
        }
        with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
            with patch("app.services.validation.settings") as mock_settings:
                mock_settings.DEFAULT_CDR_URL = "http://hapi-fhir-cdr:8080/fhir"
                result = await triage_test_bundle(bundle, "defs-only.json", test_session)

        assert result["measures_loaded"] == 1
        assert result["expected_results_loaded"] == 0
        assert result["patients_loaded"] == 0
        # push_resources called exactly once for measure defs
        mock_push.assert_called_once()

    async def test_reupload_same_source_bundle_replaces_stale_expected_results(
        self, test_session, mock_test_bundle_with_expected
    ):
        """Refreshing a bundle drops stale rows previously owned by the same source_bundle."""
        test_session.add_all(
            [
                ExpectedResult(
                    measure_url="https://example.com/Measure/old-canonical",
                    patient_ref="legacy-patient",
                    test_description="stale row",
                    expected_populations={"numerator": 0},
                    period_start="2025-01-01",
                    period_end="2025-12-31",
                    source_bundle="test.json",
                ),
                ExpectedResult(
                    measure_url="https://example.com/Measure/CMS124",
                    patient_ref="extra-stale-patient",
                    test_description="stale extra patient",
                    expected_populations={"numerator": 0},
                    period_start="2026-01-01",
                    period_end="2026-12-31",
                    source_bundle="test.json",
                ),
            ]
        )
        await test_session.commit()

        with patch("app.services.validation.push_resources", new_callable=AsyncMock):
            await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        rows = (
            (
                await test_session.execute(
                    select(ExpectedResult)
                    .where(ExpectedResult.source_bundle == "test.json")
                    .order_by(ExpectedResult.patient_ref)
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 1
        assert rows[0].measure_url == "https://example.com/Measure/CMS124"
        assert rows[0].patient_ref == "test-patient-1"

    async def test_reupload_same_source_bundle_updates_count_to_current_bundle(
        self, test_session, mock_test_bundle_with_expected
    ):
        """Bundle refresh removes count drift when the new bundle has fewer patients."""
        stale_rows = [
            ExpectedResult(
                measure_url="https://example.com/Measure/CMS124",
                patient_ref=f"stale-{idx}",
                test_description="stale",
                expected_populations={"numerator": 0},
                period_start="2026-01-01",
                period_end="2026-12-31",
                source_bundle="test.json",
            )
            for idx in range(3)
        ]
        test_session.add_all(stale_rows)
        await test_session.commit()

        with patch("app.services.validation.push_resources", new_callable=AsyncMock):
            result = await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        refreshed_count = await test_session.scalar(
            select(func.count()).select_from(ExpectedResult).where(ExpectedResult.source_bundle == "test.json")
        )

        assert result["expected_results_loaded"] == 1
        assert refreshed_count == 1


# ---------------------------------------------------------------------------
# process_bundle_upload (async, mocked async_session + triage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestProcessBundleUpload:
    def _make_session_ctx(self, session):
        """Build a context manager mock that yields the given session."""
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    async def test_not_found_returns_early(self, test_session):
        """When BundleUpload doesn't exist, function returns without error."""
        # Use the real test_session via async_session patch so .get() returns None
        session_ctx = self._make_session_ctx(test_session)
        with patch("app.services.validation.async_session", return_value=session_ctx):
            # upload_id=999 doesn't exist in the empty test DB
            await process_bundle_upload(999)
        # No exception = pass

    async def test_happy_path_sets_status_complete(self, test_session):
        """Happy path: found upload, file readable, triage succeeds → status complete."""
        import json
        import os
        import tempfile

        from app.models.validation import BundleUpload, ValidationStatus

        # Create a minimal bundle JSON file on disk
        bundle_data = {"resourceType": "Bundle", "type": "transaction", "entry": []}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bundle_data, f)
            tmp_path = f.name

        try:
            # Insert a BundleUpload record
            upload = BundleUpload(
                filename="test.json",
                file_path=tmp_path,
                status=ValidationStatus.queued,
            )
            test_session.add(upload)
            await test_session.commit()
            await test_session.refresh(upload)
            upload_id = upload.id

            # Patch async_session to use our test_session
            def make_ctx():
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=test_session)
                ctx.__aexit__ = AsyncMock(return_value=False)
                return ctx

            triage_summary = {
                "measures_loaded": 1,
                "patients_loaded": 1,
                "expected_results_loaded": 1,
                "warning_message": None,
            }

            with patch("app.services.validation.async_session", side_effect=lambda: make_ctx()):
                with patch(  # noqa: E501
                    "app.services.validation.triage_test_bundle",
                    new_callable=AsyncMock,
                    return_value=triage_summary,
                ):
                    await process_bundle_upload(upload_id)

            # Refresh the record from the session to check status
            await test_session.refresh(upload)
            assert upload.status == ValidationStatus.complete
            assert upload.measures_loaded == 1
            assert upload.patients_loaded == 1
            assert upload.expected_results_loaded == 1
            assert upload.warning_message is None
        finally:
            os.unlink(tmp_path)

    async def test_process_bundle_upload_stores_triage_warning_message(self, test_session):
        """process_bundle_upload stores warning_message on the upload when triage returns one."""
        import json
        import os
        import tempfile

        from app.models.validation import BundleUpload, ValidationStatus

        bundle_data = {"resourceType": "Bundle", "type": "transaction", "entry": []}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bundle_data, f)
            tmp_path = f.name

        try:
            upload = BundleUpload(
                filename="test.json",
                file_path=tmp_path,
                status=ValidationStatus.queued,
            )
            test_session.add(upload)
            await test_session.commit()
            await test_session.refresh(upload)
            upload_id = upload.id

            def make_ctx():
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=test_session)
                ctx.__aexit__ = AsyncMock(return_value=False)
                return ctx

            triage_summary = {
                "measures_loaded": 0,
                "patients_loaded": 0,
                "expected_results_loaded": 0,
                "warning_message": "Clinical test data was not loaded because the active CDR is read-only.",
            }

            with patch("app.services.validation.async_session", side_effect=lambda: make_ctx()):
                with patch(
                    "app.services.validation.triage_test_bundle",
                    new_callable=AsyncMock,
                    return_value=triage_summary,
                ):
                    await process_bundle_upload(upload_id)

            await test_session.refresh(upload)
            assert upload.status == ValidationStatus.complete
            assert upload.warning_message == "Clinical test data was not loaded because the active CDR is read-only."
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# run_validation (async, mocked async_session + FHIR services)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunValidation:
    def _make_session_ctx(self, session):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    async def test_missing_measure_creates_error_results_and_resolved_measure_still_runs(self, test_session):
        run = ValidationRun(status=ValidationStatus.queued)
        test_session.add(run)
        test_session.add_all(
            [
                ExpectedResult(
                    measure_url="https://example.com/Measure/resolved",
                    patient_ref="patient-1",
                    test_description="resolved",
                    expected_populations={"numerator": 1},
                    period_start="2026-01-01",
                    period_end="2026-12-31",
                    source_bundle="resolved.json",
                ),
                ExpectedResult(
                    measure_url="https://example.com/Measure/missing",
                    patient_ref="patient-2",
                    test_description="missing",
                    expected_populations={"numerator": 0},
                    period_start="2026-01-01",
                    period_end="2026-12-31",
                    source_bundle="missing.json",
                ),
            ]
        )
        await test_session.commit()
        await test_session.refresh(run)

        strategy = MagicMock()
        strategy.gather_patient_data = AsyncMock(return_value=[{"resourceType": "Patient", "id": "patient-1"}])

        def make_ctx():
            return self._make_session_ctx(test_session)

        async def resolve_measure_side_effect(measure_url):
            if measure_url.endswith("/resolved"):
                return "measure-1"
            return None

        with patch("app.services.validation.async_session", side_effect=lambda: make_ctx()):
            with patch("app.services.validation._resolve_measure_id", side_effect=resolve_measure_side_effect):
                with patch(
                    "app.services.validation._reload_measures_from_seed_bundles",
                    new_callable=AsyncMock,
                    return_value={"measures_loaded": 0, "libraries_loaded": 0, "failed": 0},
                ):
                    with patch("app.services.validation.BatchQueryStrategy", return_value=strategy):
                        with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
                            with patch(
                                "app.services.validation.wipe_patient_data",
                                new_callable=AsyncMock,
                            ) as mock_wipe:
                                with patch(
                                    "app.services.validation.evaluate_measure",
                                    new_callable=AsyncMock,
                                    return_value={
                                        "group": [
                                            {
                                                "population": [
                                                    {
                                                        "code": {"coding": [{"code": "numerator"}]},
                                                        "count": 1,
                                                    }
                                                ]
                                            }
                                        ],
                                        "evaluatedResource": [],
                                    },
                                ) as mock_evaluate:
                                    with patch("app.services.validation.settings.HAPI_INDEX_WAIT_SECONDS", 0):
                                        await run_validation(run.id)

        await test_session.refresh(run)
        rows = (
            (
                await test_session.execute(
                    select(ValidationResult)
                    .where(ValidationResult.validation_run_id == run.id)
                    .order_by(ValidationResult.patient_ref)
                )
            )
            .scalars()
            .all()
        )

        assert run.status == ValidationStatus.complete
        assert run.measures_tested == 2
        assert run.patients_tested == 2
        assert run.patients_passed == 1
        assert run.patients_failed == 1
        assert len(rows) == 2
        assert rows[0].patient_ref == "patient-1"
        assert rows[0].status == "pass"
        assert rows[1].patient_ref == "patient-2"
        assert rows[1].status == "error"
        assert "Measure not found on engine after reload attempt" in rows[1].error_message
        mock_wipe.assert_awaited_once()
        mock_push.assert_awaited_once()
        mock_evaluate.assert_awaited_once()


# ---------------------------------------------------------------------------
# _resolve_measure_id (async, mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResolveMeasureId:
    async def test_measure_found_returns_id(self):
        """When measure engine returns a matching entry, the HAPI ID is returned."""
        bundle_response = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Measure", "id": "hapi-measure-123"}}],
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = bundle_response

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            result = await _resolve_measure_id("http://example.com/Measure/CMS124")

        assert result == "hapi-measure-123"

    async def test_empty_bundle_returns_none(self):
        """When measure engine returns an empty bundle (no entries), None is returned."""
        bundle_response = {"resourceType": "Bundle", "entry": []}

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = bundle_response

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            result = await _resolve_measure_id("http://example.com/Measure/CMS124")

        assert result is None

    async def test_bundle_with_no_entries_key_returns_none(self):
        """When measure engine response omits 'entry' key entirely, None is returned."""
        bundle_response = {"resourceType": "Bundle"}

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = bundle_response

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            result = await _resolve_measure_id("http://example.com/Measure/CMS124")

        assert result is None

    async def test_relative_ref_found_returns_id(self):
        """EXM-style relative references ("Measure/{id}") resolve via direct GET, not ?url= search.

        Regression: before this fix _resolve_measure_id only used ?url=, which returns no results
        for EXM bundles because their MeasureReport.measure is a relative ref while HAPI indexes
        the measure under its canonical URL.
        """
        measure_resp = {"resourceType": "Measure", "id": "measure-EXM130-FHIR4-7.2.000"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = measure_resp

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            result = await _resolve_measure_id("Measure/measure-EXM130-FHIR4-7.2.000")

        assert result == "measure-EXM130-FHIR4-7.2.000"
        # Must fetch by ID path, NOT by ?url= query
        call_url = mock_client.get.call_args[0][0]
        assert "?url=" not in call_url
        assert "/Measure/measure-EXM130-FHIR4-7.2.000" in call_url

    async def test_relative_ref_not_found_returns_none(self):
        """When HAPI returns 404 for a relative reference, None is returned instead of raising."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            result = await _resolve_measure_id("Measure/measure-EXM130-FHIR4-7.2.000")

        assert result is None

    async def test_malformed_relative_ref_returns_none(self):
        """A non-http string that isn't a valid relative reference returns None without calling HAPI."""
        mock_client = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            result = await _resolve_measure_id("not-a-valid-ref")

        assert result is None

    async def test_canonical_url_http_error_raises(self):
        """Non-2xx responses from HAPI for canonical URL lookups propagate as exceptions."""
        import httpx

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            with pytest.raises(httpx.HTTPStatusError):
                await _resolve_measure_id("http://example.com/Measure/CMS124")

    async def test_relative_ref_http_error_raises(self):
        """Non-404 errors from HAPI for relative reference lookups propagate as exceptions."""
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            with pytest.raises(httpx.HTTPStatusError):
                await _resolve_measure_id("Measure/measure-EXM130-FHIR4-7.2.000")

    async def test_resolve_measure_id_retries_on_empty_bundle(self):
        """Verify that _resolve_measure_id retries when HAPI returns an empty bundle (lag/cache)."""
        # First two calls return empty bundle
        empty_bundle = {"resourceType": "Bundle", "entry": []}
        # Third call returns the measure
        success_bundle = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Measure", "id": "hapi-id-123"}}],
        }

        mock_resp_empty = MagicMock()
        mock_resp_empty.raise_for_status = MagicMock()
        mock_resp_empty.json.return_value = empty_bundle

        mock_resp_success = MagicMock()
        mock_resp_success.raise_for_status = MagicMock()
        mock_resp_success.json.return_value = success_bundle

        mock_client = AsyncMock()
        # Side effect: two empties, then success
        mock_client.get = AsyncMock(side_effect=[mock_resp_empty, mock_resp_empty, mock_resp_success])

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            with patch("app.services.validation.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await _resolve_measure_id("http://example.com/Measure/123")

                assert result == "hapi-id-123"
                assert mock_client.get.call_count == 3
                assert mock_sleep.call_count == 2

                # Verify Cache-Control: no-cache was sent
                last_call_headers = mock_client.get.call_args_list[-1].kwargs["headers"]
                assert last_call_headers["Cache-Control"] == "no-cache"

    async def test_resolve_measure_id_retries_on_exception(self):
        """Verify that _resolve_measure_id retries when HAPI request fails."""
        mock_resp_success = MagicMock()
        mock_resp_success.raise_for_status = MagicMock()
        mock_resp_success.json.return_value = {
            "resourceType": "Bundle",
            "entry": [{"resource": {"resourceType": "Measure", "id": "hapi-id-456"}}],
        }

        mock_client = AsyncMock()
        # Side effect: one failure, then success
        mock_client.get = AsyncMock(side_effect=[Exception("Network error"), mock_resp_success])

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.validation.httpx.AsyncClient", return_value=mock_ctx):
            with patch("app.services.validation.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await _resolve_measure_id("http://example.com/Measure/456")

                assert result == "hapi-id-456"
                assert mock_client.get.call_count == 2
                assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# _fix_valueset_compose_for_hapi
# ---------------------------------------------------------------------------

_EXPANSION_CONTAINS = [
    {"system": "http://snomed.info/sct", "code": "123456", "display": "Frailty"},
    {"system": "http://snomed.info/sct", "code": "789012"},
    {"system": "http://loinc.org", "code": "LA99-0", "display": "Mild frailty"},
]


class TestFixValueSetComposeForHapi:
    def _vs(self, **kwargs):
        base = {"resourceType": "ValueSet", "id": "test-vs", "url": "http://example.com/vs"}
        base.update(kwargs)
        return base

    def test_non_valueset_passed_through(self):
        patient = {"resourceType": "Patient", "id": "p1"}
        result = _fix_valueset_compose_for_hapi([patient])
        assert result == [patient]

    def test_valueset_without_expansion_passed_through(self):
        vs = self._vs(compose={"include": [{"system": "http://snomed.info/sct", "concept": [{"code": "1"}]}]})
        result = _fix_valueset_compose_for_hapi([vs])
        assert result == [vs]

    def test_no_compose_synthesised_from_expansion(self):
        vs = self._vs(expansion={"contains": _EXPANSION_CONTAINS})
        result = _fix_valueset_compose_for_hapi([vs])
        assert len(result) == 1
        include = result[0]["compose"]["include"]
        systems = {inc["system"] for inc in include}
        assert "http://snomed.info/sct" in systems
        assert "http://loinc.org" in systems
        snomed = next(i for i in include if i["system"] == "http://snomed.info/sct")
        assert len(snomed["concept"]) == 2
        assert {"code": "123456", "display": "Frailty"} in snomed["concept"]
        assert {"code": "789012"} in snomed["concept"]

    def test_compose_with_valueset_refs_synthesised(self):
        vs = self._vs(
            compose={"include": [{"valueSet": ["http://other.com/vs"]}]},
            expansion={"contains": _EXPANSION_CONTAINS},
        )
        result = _fix_valueset_compose_for_hapi([vs])
        assert "include" in result[0]["compose"]
        for inc in result[0]["compose"]["include"]:
            assert "valueSet" not in inc

    def test_bare_codesystem_includes_synthesised(self):
        vs = self._vs(
            compose={"include": [{"system": "http://snomed.info/sct"}]},
            expansion={"contains": [{"system": "http://snomed.info/sct", "code": "42"}]},
        )
        result = _fix_valueset_compose_for_hapi([vs])
        include = result[0]["compose"]["include"]
        assert include[0]["concept"] == [{"code": "42"}]

    def test_compose_with_real_concepts_not_touched(self):
        vs = self._vs(
            compose={"include": [{"system": "http://snomed.info/sct", "concept": [{"code": "1"}, {"code": "2"}]}]},
            expansion={"contains": _EXPANSION_CONTAINS},
        )
        result = _fix_valueset_compose_for_hapi([vs])
        assert result[0]["compose"]["include"][0]["concept"] == [{"code": "1"}, {"code": "2"}]

    def test_compose_with_filter_not_touched(self):
        vs = self._vs(
            compose={
                "include": [
                    {
                        "system": "http://snomed.info/sct",
                        "filter": [{"property": "concept", "op": "is-a", "value": "404684003"}],
                    }
                ]
            },
            expansion={"contains": _EXPANSION_CONTAINS},
        )
        result = _fix_valueset_compose_for_hapi([vs])
        assert "filter" in result[0]["compose"]["include"][0]

    def test_nested_expansion_contains_flattened(self):
        nested = [
            {
                "system": "http://snomed.info/sct",
                "code": "1",
                "contains": [{"system": "http://snomed.info/sct", "code": "2"}],
            }
        ]
        vs = self._vs(expansion={"contains": nested})
        result = _fix_valueset_compose_for_hapi([vs])
        concepts = result[0]["compose"]["include"][0]["concept"]
        codes = [c["code"] for c in concepts]
        assert "1" in codes
        assert "2" in codes

    def test_empty_expansion_contains_not_synthesised(self):
        vs = self._vs(expansion={"contains": []})
        result = _fix_valueset_compose_for_hapi([vs])
        assert "compose" not in result[0]

    def test_original_not_mutated(self):
        vs = self._vs(expansion={"contains": _EXPANSION_CONTAINS})
        original_id = id(vs)
        result = _fix_valueset_compose_for_hapi([vs])
        assert id(result[0]) != original_id
        assert "compose" not in vs

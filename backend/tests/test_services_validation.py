"""Tests for validation service — bundle triage, population extraction, comparison."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.validation import (
    _classify_bundle_entries,
    _extract_patient_name,
    _extract_population_counts,
    _extract_test_case_info,
    _is_test_case_measure_report,
    _resolve_measure_id,
    compare_populations,
    process_bundle_upload,
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
                # session.execute needs to handle the pg_insert statement —
                # mock it to avoid PostgreSQL dialect errors with SQLite.
                orig_execute = test_session.execute

                async def _execute_interceptor(stmt, *args, **kwargs):
                    # Intercept pg_insert statements (they have on_conflict_do_update attr)
                    if hasattr(stmt, "excluded"):
                        return MagicMock()
                    return await orig_execute(stmt, *args, **kwargs)

                test_session.execute = _execute_interceptor

                result = await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        # Measure + Library = 2 measure defs → push_resources called once for defs
        assert result["measures_loaded"] == 1  # only Measure type counts
        assert result["expected_results_loaded"] == 1  # one isTestCase MeasureReport
        # clinical data (Patient + Observation) was pushed because CDR is not read-only
        assert result["patients_loaded"] == 1  # one Patient resource
        assert result.get("warning_message") is None
        assert mock_push.call_count >= 1

    async def test_external_cdr_clinical_not_pushed(self, test_session, mock_test_bundle_with_expected):
        """When active CDR is read-only, clinical data is NOT pushed."""
        from app.models.config import AuthType, CDRConfig

        # Insert an active read-only CDR config
        readonly_cdr = CDRConfig(
            cdr_url="http://external-cdr.example.com/fhir",
            auth_type=AuthType.none,
            is_active=True,
            name="External CDR",
            is_default=False,
            is_read_only=True,  # <-- key change
        )
        test_session.add(readonly_cdr)
        await test_session.commit()

        with patch("app.services.validation.push_resources", new_callable=AsyncMock) as mock_push:
            # pg_insert interceptor is still needed for SQLite compat
            orig_execute = test_session.execute

            async def _execute_interceptor(stmt, *args, **kwargs):
                if hasattr(stmt, "excluded"):
                    return MagicMock()
                return await orig_execute(stmt, *args, **kwargs)

            test_session.execute = _execute_interceptor

            result = await triage_test_bundle(mock_test_bundle_with_expected, "test.json", test_session)

        # clinical data NOT pushed (read-only CDR) → patients_loaded == 0
        assert result["patients_loaded"] == 0
        assert result["warning_message"] is not None
        assert "read-only" in result["warning_message"].lower()
        # Measure defs are still pushed (to measure engine)
        push_calls_for_defs = [
            call
            for call in mock_push.call_args_list
            if any(r.get("resourceType") in {"Measure", "Library"} for r in call.args[0])
        ]
        assert len(push_calls_for_defs) == 1

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

    async def test_sets_warning_message_when_read_only(self, test_session):
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

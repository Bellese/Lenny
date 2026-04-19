"""Tests for validation route endpoints."""

import io
import json
import time as time_module

import pytest

from app.models.validation import BundleUpload, ExpectedResult, ValidationRun, ValidationStatus

# ---------------------------------------------------------------------------
# POST /validation/upload-bundle
# ---------------------------------------------------------------------------


class TestUploadBundle:
    @pytest.mark.asyncio
    async def test_upload_valid_bundle(self, client, mock_test_bundle_with_expected, tmp_path, monkeypatch):
        # Monkeypatch UPLOAD_DIR to use tmp_path
        monkeypatch.setattr("app.routes.validation.UPLOAD_DIR", str(tmp_path))
        content = json.dumps(mock_test_bundle_with_expected).encode()
        response = await client.post(
            "/validation/upload-bundle",
            files={"file": ("test-bundle.json", io.BytesIO(content), "application/json")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["filename"] == "test-bundle.json"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_upload_invalid_json(self, client):
        response = await client.post(
            "/validation/upload-bundle",
            files={"file": ("bad.json", io.BytesIO(b"not json"), "application/json")},
        )
        assert response.status_code == 400
        assert "not valid JSON" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_not_a_bundle(self, client):
        content = json.dumps({"resourceType": "Patient", "id": "p1"}).encode()
        response = await client.post(
            "/validation/upload-bundle",
            files={"file": ("patient.json", io.BytesIO(content), "application/json")},
        )
        assert response.status_code == 400
        assert "not a FHIR Bundle" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_wrong_extension(self, client):
        response = await client.post(
            "/validation/upload-bundle",
            files={"file": ("data.csv", io.BytesIO(b"a,b,c"), "text/csv")},
        )
        assert response.status_code == 400
        assert ".json" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_concurrent_same_filename_gets_distinct_paths(self, client, test_session, tmp_path, monkeypatch):
        """Two uploads of the same filename must produce distinct file paths (issue #63).

        Forces both uploads to the same timestamp bucket (via monkeypatched time.time)
        to verify that uuid4 alone differentiates the paths even when the timestamp prefix
        is identical. Requests are sequential, not concurrent — sufficient to prove the
        uuid4 uniqueness guarantee.
        """
        monkeypatch.setattr("app.routes.validation.UPLOAD_DIR", str(tmp_path))
        # Pin timestamp so both uploads land in the same second — the pre-fix
        # code would produce identical paths; the uuid4 fix ensures they don't.
        monkeypatch.setattr(time_module, "time", lambda: 1_000_000_000)

        content = json.dumps({"resourceType": "Bundle", "type": "transaction", "entry": []}).encode()

        resp1 = await client.post(
            "/validation/upload-bundle",
            files={"file": ("bundle.json", io.BytesIO(content), "application/json")},
        )
        resp2 = await client.post(
            "/validation/upload-bundle",
            files={"file": ("bundle.json", io.BytesIO(content), "application/json")},
        )

        assert resp1.status_code == 200
        assert resp2.status_code == 200

        id1 = resp1.json()["id"]
        id2 = resp2.json()["id"]
        assert id1 != id2, "Expected distinct DB records for each upload"

        upload1 = await test_session.get(BundleUpload, id1)
        upload2 = await test_session.get(BundleUpload, id2)

        assert upload1 is not None
        assert upload2 is not None
        assert upload1.file_path != upload2.file_path, f"Collision: both uploads resolved to {upload1.file_path!r}"


# ---------------------------------------------------------------------------
# POST /validation/run
# ---------------------------------------------------------------------------


class TestStartValidationRun:
    @pytest.mark.asyncio
    async def test_run_no_expected_results(self, client):
        response = await client.post("/validation/run", json={})
        assert response.status_code == 400
        assert "No expected results" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_run_with_expected_results(self, client, test_session):
        # Insert an expected result
        er = ExpectedResult(
            measure_url="https://example.com/Measure/CMS124",
            patient_ref="test-patient-1",
            expected_populations={"initial-population": 1, "numerator": 1},
            period_start="2026-01-01",
            period_end="2026-12-31",
            source_bundle="test.json",
        )
        test_session.add(er)
        await test_session.commit()

        response = await client.post("/validation/run", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert "id" in data


# ---------------------------------------------------------------------------
# GET /validation/runs
# ---------------------------------------------------------------------------


class TestListValidationRuns:
    @pytest.mark.asyncio
    async def test_empty_list(self, client):
        response = await client.get("/validation/runs")
        assert response.status_code == 200
        assert response.json()["runs"] == []

    @pytest.mark.asyncio
    async def test_list_with_runs(self, client, test_session):
        run = ValidationRun(status=ValidationStatus.complete, patients_tested=10, patients_passed=8, patients_failed=2)
        test_session.add(run)
        await test_session.commit()

        response = await client.get("/validation/runs")
        assert response.status_code == 200
        runs = response.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["patients_tested"] == 10


# ---------------------------------------------------------------------------
# GET /validation/runs/{run_id}
# ---------------------------------------------------------------------------


class TestGetValidationRun:
    @pytest.mark.asyncio
    async def test_not_found(self, client):
        response = await client.get("/validation/runs/9999")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_run_detail(self, client, test_session):
        run = ValidationRun(
            status=ValidationStatus.complete,
            measures_tested=1,
            patients_tested=2,
            patients_passed=1,
            patients_failed=1,
        )
        test_session.add(run)
        await test_session.commit()

        response = await client.get(f"/validation/runs/{run.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["patients_tested"] == 2
        assert data["measures"] == []  # No results stored yet


# ---------------------------------------------------------------------------
# GET /validation/expected
# ---------------------------------------------------------------------------


class TestListExpectedResults:
    @pytest.mark.asyncio
    async def test_empty(self, client):
        response = await client.get("/validation/expected")
        assert response.status_code == 200
        assert response.json()["total_measures"] == 0

    @pytest.mark.asyncio
    async def test_with_results(self, client, test_session):
        test_session.add(
            ExpectedResult(
                measure_url="https://example.com/Measure/CMS122",
                patient_ref="p1",
                expected_populations={"numerator": 1},
                period_start="2026-01-01",
                period_end="2026-12-31",
                source_bundle="test.json",
            )
        )
        test_session.add(
            ExpectedResult(
                measure_url="https://example.com/Measure/CMS122",
                patient_ref="p2",
                expected_populations={"numerator": 0},
                period_start="2026-01-01",
                period_end="2026-12-31",
                source_bundle="test.json",
            )
        )
        await test_session.commit()

        response = await client.get("/validation/expected")
        assert response.status_code == 200
        data = response.json()
        assert data["total_measures"] == 1
        assert data["measures"][0]["patient_count"] == 2


# ---------------------------------------------------------------------------
# GET /validation/uploads
# ---------------------------------------------------------------------------


class TestListUploads:
    @pytest.mark.asyncio
    async def test_empty(self, client):
        response = await client.get("/validation/uploads")
        assert response.status_code == 200
        assert response.json()["uploads"] == []

    @pytest.mark.asyncio
    async def test_warning_message_included_in_response(self, client, test_session):
        """GET /validation/uploads includes warning_message for each upload."""
        upload = BundleUpload(
            filename="test-bundle.json",
            file_path="/tmp/test-bundle.json",
            status=ValidationStatus.complete,
            warning_message="2 resources could not be loaded",
        )
        test_session.add(upload)
        await test_session.commit()

        response = await client.get("/validation/uploads")
        assert response.status_code == 200
        uploads = response.json()["uploads"]
        assert len(uploads) == 1
        assert uploads[0]["warning_message"] == "2 resources could not be loaded"

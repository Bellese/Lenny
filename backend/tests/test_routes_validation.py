"""Tests for validation route endpoints."""

import io
import json

import pytest
import pytest_asyncio

from app.models.validation import ExpectedResult, ValidationRun, ValidationStatus


# ---------------------------------------------------------------------------
# POST /validation/upload-bundle
# ---------------------------------------------------------------------------


class TestUploadBundle:
    @pytest.mark.asyncio
    async def test_upload_valid_bundle(self, client, mock_test_bundle_with_expected, tmp_path, monkeypatch):
        # Monkeypatch UPLOAD_DIR to use tmp_path
        monkeypatch.setattr(
            "app.routes.validation.UPLOAD_DIR", str(tmp_path)
        )
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
        test_session.add(ExpectedResult(
            measure_url="https://example.com/Measure/CMS122",
            patient_ref="p1",
            expected_populations={"numerator": 1},
            period_start="2026-01-01",
            period_end="2026-12-31",
            source_bundle="test.json",
        ))
        test_session.add(ExpectedResult(
            measure_url="https://example.com/Measure/CMS122",
            patient_ref="p2",
            expected_populations={"numerator": 0},
            period_start="2026-01-01",
            period_end="2026-12-31",
            source_bundle="test.json",
        ))
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

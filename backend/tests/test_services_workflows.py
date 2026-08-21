"""Tests for the per-job submission workflow strategies (workflows.py)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.fhir_client import GatherResult
from app.services.workflows import (
    DeqmSubmitDataWorkflow,
    DirectLoadWorkflow,
    TransferPhaseError,
    build_submission_workflow,
)

pytestmark = pytest.mark.asyncio

_GATHER = GatherResult(
    resources=[
        {"resourceType": "Patient", "id": "p1"},
        {"resourceType": "Condition", "id": "c1"},
    ]
)


class TestDirectLoadWorkflow:
    async def test_gathers_then_pushes(self):
        wf = DirectLoadWorkflow("M1", "http://mcs", {"Authorization": "Bearer t"})
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is _GATHER
        push.assert_awaited_once_with(
            _GATHER.resources, target_url="http://mcs", auth_headers={"Authorization": "Bearer t"}
        )

    async def test_skips_push_when_nothing_gathered(self):
        wf = DirectLoadWorkflow("M1", "http://mcs")
        empty = GatherResult(resources=[])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=empty)),
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        push.assert_not_awaited()

    async def test_gather_failure_raises_gather_phase(self):
        wf = DirectLoadWorkflow("M1", "http://mcs")
        with patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(side_effect=RuntimeError("cdr down"))):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "gather"

    async def test_push_failure_raises_gather_phase(self):
        # Push failures keep today's error_phase="gather" labeling for direct_load.
        wf = DirectLoadWorkflow("M1", "http://mcs")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.push_resources", new=AsyncMock(side_effect=RuntimeError("mcs down"))),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "gather"


def _deqm_workflow(mode: str = "base-fallback") -> DeqmSubmitDataWorkflow:
    return DeqmSubmitDataWorkflow(
        job_id=7,
        measure_id="M1",
        mcs_url="http://mcs",
        mcs_auth_headers={},
        measure_canonical="http://ex.org/Measure/M1|1.0",
        period_start="2025-01-01",
        period_end="2025-12-31",
        mode=mode,
    )


class TestDeqmSubmitDataWorkflow:
    async def test_submits_deqm_measure_report_with_data(self):
        wf = _deqm_workflow()
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is _GATHER
        submit.assert_awaited_once()
        kwargs = submit.call_args.kwargs
        assert kwargs["mcs_url"] == "http://mcs"
        assert kwargs["mode"] == "base-fallback"
        params = kwargs["parameters"]
        assert params["parameter"][0]["name"] == "measureReport"
        mr = params["parameter"][0]["resource"]
        assert mr["type"] == "data-collection"
        assert mr["subject"] == {"reference": "Patient/p1"}
        assert mr["id"] == "deqm-7-p1"
        submitted_types = [p["resource"]["resourceType"] for p in params["parameter"][1:]]
        assert submitted_types == ["Organization", "Patient", "Condition"]

    async def test_stu5_mode_uses_bundle_envelope(self):
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        params = submit.call_args.kwargs["parameters"]
        assert params["parameter"][0]["name"] == "bundle"
        assert submit.call_args.kwargs["mode"] == "stu5"

    async def test_submit_failure_raises_submit_phase(self):
        wf = _deqm_workflow()
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=RuntimeError("rejected"))),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"

    async def test_gather_failure_raises_gather_phase(self):
        wf = _deqm_workflow()
        with patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(side_effect=RuntimeError("cdr down"))):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "gather"


class TestBuildSubmissionWorkflow:
    async def test_direct_load_needs_no_canonical_fetch(self):
        with patch("app.services.workflows.get_measure_canonical", new=AsyncMock()) as canon:
            wf = await build_submission_workflow(
                workflow="direct_load",
                job_id=1,
                measure_id="M1",
                mcs_url="http://mcs",
                mcs_auth_headers=None,
                submit_data_mode=None,
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
        assert isinstance(wf, DirectLoadWorkflow)
        canon.assert_not_awaited()

    async def test_deqm_fetches_canonical_and_defaults_mode(self):
        with patch(
            "app.services.workflows.get_measure_canonical",
            new=AsyncMock(return_value="http://ex.org/Measure/M1|1.0"),
        ) as canon:
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="M1",
                mcs_url="http://mcs",
                mcs_auth_headers={},
                submit_data_mode=None,  # legacy NULL → base
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
        assert isinstance(wf, DeqmSubmitDataWorkflow)
        canon.assert_awaited_once_with("M1", mcs_url="http://mcs", auth_headers={})
        assert wf._mode == "base-fallback"

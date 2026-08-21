"""Tests for the per-job submission workflow strategies (workflows.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.fhir_client import GatherResult
from app.services.fhir_errors import FhirOperationError
from app.services.workflows import (
    DeqmSubmitDataWorkflow,
    DirectLoadWorkflow,
    TransferPhaseError,
    build_submission_workflow,
)


def _fhir_op_error(status_code: int) -> FhirOperationError:
    return FhirOperationError(
        operation="submit-data",
        url="http://mcs/Measure/$deqm-submit-data",
        status_code=status_code,
        outcome=None,
        latency_ms=5,
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
        assert kwargs["measure_id"] == "M1"
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
        assert submit.call_args.kwargs["measure_id"] == "M1"

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

    async def test_stu5_400_downgrades_to_base_and_retry_succeeds(self):
        """I4: a mis-probed stu5 server 400s the STU5 shape; downgrade to base
        and retry once rather than failing the whole job."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[_fhir_op_error(400), None])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is _GATHER
        assert wf._mode == "base-fallback"
        assert submit.await_count == 2
        first_kwargs, second_kwargs = submit.call_args_list[0].kwargs, submit.call_args_list[1].kwargs
        assert first_kwargs["mode"] == "stu5"
        assert first_kwargs["parameters"]["parameter"][0]["name"] == "bundle"
        assert second_kwargs["mode"] == "base-fallback"
        assert second_kwargs["parameters"]["parameter"][0]["name"] == "measureReport"

    async def test_stu5_404_also_downgrades(self):
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[_fhir_op_error(404), None])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        assert wf._mode == "base-fallback"
        assert submit.await_count == 2

    async def test_stu5_downgrade_retry_also_fails_raises_submit_phase(self):
        """If the base-mode retry also fails, raise TransferPhaseError as today."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[_fhir_op_error(400), _fhir_op_error(500)])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "base-fallback"  # downgrade already flipped before the retry failed
        assert submit.await_count == 2

    async def test_base_mode_failure_does_not_retry_and_raises(self):
        """A base-mode failure is not stu5, so no downgrade path applies — it
        still raises immediately."""
        wf = _deqm_workflow(mode="base-fallback")
        submit = AsyncMock(side_effect=_fhir_op_error(400))
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "base-fallback"
        assert submit.await_count == 1

    async def test_concurrent_stu5_downgrade_does_not_strand_second_patient(self):
        """Regression: self._mode is shared, mutable instance state, and one
        DeqmSubmitDataWorkflow instance is reused concurrently across patients
        in the same job (orchestrator batches under
        asyncio.Semaphore(MAX_WORKERS) + asyncio.gather). If two patients
        both send STU5 requests and both 400, the downgrade guard must judge
        EACH attempt against the mode IT was sent under — not against
        self._mode read after the await, which a concurrent sibling may
        already have flipped to base-fallback. Otherwise whichever patient's
        except-handler runs second reads the already-flipped mode, the guard
        evaluates False, and that patient is stranded (raises
        TransferPhaseError) despite having failed for the identical
        mis-probed-STU5 reason as its sibling, which got rescued.

        The mock below uses an asyncio.Event as a barrier so BOTH STU5 400s
        are guaranteed to be in flight/raised before either patient's
        downgrade-and-retry logic runs — this reproduces the race
        deterministically instead of relying on scheduling luck.
        """
        wf = _deqm_workflow(mode="stu5")
        stu5_call_count = 0
        release_first_waiter = asyncio.Event()

        async def submit_data_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            nonlocal stu5_call_count
            if mode == "stu5":
                stu5_call_count += 1
                if stu5_call_count == 1:
                    # First STU5 attempt to arrive: wait for its sibling so
                    # both 400s exist before either except-handler (and thus
                    # any self._mode mutation) runs.
                    await release_first_waiter.wait()
                else:
                    release_first_waiter.set()
                raise _fhir_op_error(400)
            return None  # base-mode retries succeed

        submit = AsyncMock(side_effect=submit_data_side_effect)
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            result_a, result_b = await asyncio.gather(
                wf.transfer_patient("http://cdr", "p1", {}),
                wf.transfer_patient("http://cdr", "p2", {}),
            )

        assert result_a is _GATHER
        assert result_b is _GATHER
        assert stu5_call_count == 2
        base_mode_calls = [c for c in submit.call_args_list if c.kwargs["mode"] == "base-fallback"]
        assert len(base_mode_calls) == 2
        assert wf._mode == "base-fallback"

    async def test_stu5_non_downgrade_status_does_not_retry(self):
        """A non-400/404 STU5 failure (e.g. 500) does NOT trigger a downgrade retry."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=_fhir_op_error(500))
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "stu5"  # no downgrade attempted
        assert submit.await_count == 1


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

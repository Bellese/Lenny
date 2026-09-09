"""Tests for the per-job submission workflow strategies (workflows.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.deqm import LENNY_REPORTER_ORG
from app.services.fhir_client import BatchQueryStrategy, DataRequirementsStrategy, GatherResult
from app.services.fhir_errors import FhirOperationError, FhirOperationOutcome
from app.services.workflows import (
    DeqmSubmitDataWorkflow,
    DirectLoadWorkflow,
    TransferPhaseError,
    _acquisition_strategy,
    build_submission_workflow,
)


def _fhir_op_error_with_outcome(status_code: int, diagnostics: str) -> FhirOperationError:
    """#414: a 400's meaning lives in its OperationOutcome, not its status."""
    return FhirOperationError(
        operation="submit-data",
        url="http://mcs/Measure/$deqm-submit-data",
        status_code=status_code,
        outcome=FhirOperationOutcome.from_dict(
            {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "processing", "diagnostics": diagnostics}],
            }
        ),
        latency_ms=5,
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
        # The reporter Organization is NOT re-sent per patient — it is PUT
        # once per job by build_submission_workflow. Only the patient's own
        # gathered resources travel in the per-patient payload.
        submitted_types = [p["resource"]["resourceType"] for p in params["parameter"][1:]]
        assert submitted_types == ["Patient", "Condition"]
        assert "Organization" not in submitted_types

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

    # -- #414: a 400 is not evidence of a missing operation ------------------
    # DEQM prescribes 400 for an update-type mismatch, and 400 is the generic
    # FHIR answer to a rejected payload. Treating it as "this server lacks
    # STU5" discarded the real OperationOutcome and silently changed the wire
    # format for every later patient. A genuine not-supported 400 does exist
    # (HAPI answers the type-level base operation with "does not know how to
    # handle POST operation[...]"), so the outcome text is what decides.

    async def test_stu5_400_validation_rejection_fails_patient_without_downgrading(self):
        """AC1: the bug. A per-patient validation rejection must not be read as
        a capability verdict for the whole job."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=_fhir_op_error_with_outcome(400, "Bundle.entry[3]: minimum required = 1"))
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "stu5"  # no downgrade
        assert submit.await_count == 1  # no retry
        # The server's own explanation must survive to error_details.
        assert exc_info.value.cause.outcome.primary_diagnostic() == "Bundle.entry[3]: minimum required = 1"

    async def test_stu5_400_saying_operation_unsupported_does_downgrade(self):
        """AC1's other half: a 400 that genuinely reports the operation missing
        is still a capability signal. HAPI's wording is the documented case."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(
            side_effect=[
                _fhir_op_error_with_outcome(
                    400, "does not know how to handle POST operation[Measure/$deqm-submit-data]"
                ),
                None,
            ]
        )
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        assert wf._mode == "base-fallback"
        assert submit.await_count == 2

    async def test_stu5_400_with_no_outcome_does_not_downgrade(self):
        """A 400 carrying no OperationOutcome is not proof of anything. Absent
        evidence, treat it as a payload rejection — the conservative direction,
        since a wrong downgrade changes the format for every later patient."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=_fhir_op_error(400))
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError):
                await wf.transfer_patient("http://cdr", "p1", {})
        assert wf._mode == "stu5"
        assert submit.await_count == 1

    # -- #414 AC4: a job is never half one wire format ------------------------

    async def test_downgrade_refused_once_a_patient_has_succeeded(self):
        """AC4 (ruling: mixed-mode jobs are prohibited).

        A downgrade after a successful STU5 submission would leave the job
        half STU5 and half base with nothing recording the boundary. The later
        patient fails instead, and the job stays uniformly STU5.
        """
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[None, _fhir_op_error(404)])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})  # succeeds under stu5
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p2", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "stu5"  # NOT flipped — no mixed-mode job
        assert submit.await_count == 2  # no rescue retry for p2

    async def test_downgrade_still_allowed_before_any_success(self):
        """AC4 must not cost the mis-probe rescue. With no successful
        submission yet, nothing can be stranded in the other format, so the
        downgrade is safe and still happens."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[_fhir_op_error(404), None])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        assert wf._mode == "base-fallback"
        assert submit.await_count == 2

    async def test_downgrade_is_observable_for_persistence(self):
        """AC2 is persisted by the orchestrator, which owns DB access —
        workflows.py is deliberately session-free. The workflow therefore has
        to expose that a downgrade happened."""
        wf = _deqm_workflow(mode="stu5")
        assert wf.downgraded is False
        submit = AsyncMock(side_effect=[_fhir_op_error(404), None])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        assert wf.downgraded is True
        assert wf.mode == "base-fallback"

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

    @pytest.mark.parametrize("status_code", [405, 501])
    async def test_stu5_405_and_501_also_downgrade(self, status_code):
        """F3: a server that advertises $deqm-submit-data but doesn't
        implement the type-level POST commonly answers 405 or 501."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[_fhir_op_error(status_code), None])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        assert wf._mode == "base-fallback"
        assert submit.await_count == 2

    @pytest.mark.parametrize("status_code", [401, 403, 429, 500])
    async def test_stu5_auth_and_transient_failures_do_not_downgrade(self, status_code):
        """F3: auth failures (401/403) must not be masked as a capability
        downgrade, and transient/overload signals (429/5xx) must not be
        treated as a permanent capability verdict."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=_fhir_op_error(status_code))
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "stu5"  # no downgrade attempted
        assert submit.await_count == 1

    async def test_200_rejection_fails_the_patient_and_does_not_downgrade(self):
        """#415 x #414: a rejection returned inside a 200 is a per-patient
        failure, not evidence that the server lacks STU5.

        The break this catches: widening _DOWNGRADE_STATUS_CODES to include a
        2xx, or dropping the status_code check from the downgrade guard, would
        turn one rejected patient into a silent wire-format change for every
        subsequent patient in the job. #414 is scheduled to revisit that guard,
        so pin the boundary now.
        """
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=_fhir_op_error(200))
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert exc_info.value.phase == "submit"
        assert wf._mode == "stu5"  # no downgrade
        assert submit.await_count == 1  # no retry

    async def test_200_rejection_preserves_the_outcome_for_error_details(self):
        """#415 AC1: the OperationOutcome must survive the wrap into
        TransferPhaseError, because orchestrator.py reads `.outcome.raw` off
        the cause to populate MeasureResult.error_details["raw_outcome"]. A
        wrap that dropped the cause would leave the user with a bare failure
        and no reason for it."""
        wf = _deqm_workflow(mode="base-fallback")
        rejection = FhirOperationError(
            operation="submit-data",
            url="http://mcs/Measure/M1/$submit-data",
            status_code=200,
            outcome=FhirOperationOutcome.from_dict(
                {
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "processing",
                            "diagnostics": "Unable to resolve reference Patient/nope",
                        }
                    ],
                }
            ),
            latency_ms=5,
        )
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=rejection)),
        ):
            with pytest.raises(TransferPhaseError) as exc_info:
                await wf.transfer_patient("http://cdr", "p1", {})
        cause = exc_info.value.cause
        assert isinstance(cause, FhirOperationError)
        assert cause.outcome.raw["issue"][0]["diagnostics"] == "Unable to resolve reference Patient/nope"

    async def test_stu5_downgrade_retry_also_fails_raises_submit_phase(self):
        """If the base-mode retry also fails, raise TransferPhaseError as today."""
        wf = _deqm_workflow(mode="stu5")
        submit = AsyncMock(side_effect=[_fhir_op_error(404), _fhir_op_error(500)])
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

    async def test_concurrent_patients_settle_one_mode_and_none_are_stranded(self):
        """#414: replaces test_concurrent_stu5_downgrade_does_not_strand_second_patient.

        That test's premise — two patients both in flight under STU5 before
        either downgrade runs — is now impossible by construction, which is the
        point of the settlement barrier. It used an asyncio.Event to hold the
        first STU5 attempt until a second one arrived; under the barrier the
        second can never arrive, so the old test deadlocks rather than fails.

        The guarantee it defended (no patient stranded by a sibling's flip) is
        preserved and strengthened here: only ONE STU5 attempt is ever made,
        so there is no second attempt to strand, and no patient can be
        submitted under a mode the job later abandons.

        The break this catches: removing the barrier and letting each patient
        run its own downgrade would push the STU5 attempt count above 1 and
        reintroduce mixed-mode jobs.
        """
        wf = _deqm_workflow(mode="stu5")
        modes_attempted = []

        async def submit_data_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            modes_attempted.append(mode)
            if mode == "stu5":
                raise _fhir_op_error(404)
            return None  # base-mode submissions succeed

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
        assert result_b is _GATHER  # neither patient stranded
        assert modes_attempted.count("stu5") == 1, (
            f"exactly one STU5 attempt expected before settlement, got {modes_attempted}"
        )
        assert modes_attempted.count("base-fallback") == 2  # pioneer retry + sibling
        assert wf._mode == "base-fallback"
        assert wf.downgraded is True

    async def test_concurrent_patients_never_mix_modes_when_stu5_works(self):
        """The mirror case: if STU5 succeeds there is no downgrade, and every
        patient goes out as STU5. A job is uniform in either direction."""
        wf = _deqm_workflow(mode="stu5")
        modes_attempted = []

        async def submit_data_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            modes_attempted.append(mode)
            return None

        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_data_side_effect)),
        ):
            await asyncio.gather(
                wf.transfer_patient("http://cdr", "p1", {}),
                wf.transfer_patient("http://cdr", "p2", {}),
                wf.transfer_patient("http://cdr", "p3", {}),
            )

        assert set(modes_attempted) == {"stu5"}, modes_attempted
        assert wf.downgraded is False

    async def test_empty_gather_still_submits_measure_report_only(self):
        """Coverage-audit gap fill: DeqmSubmitDataWorkflow does not skip
        submission when gather returns zero resources (unlike DirectLoadWorkflow,
        which skips the push entirely). The DEQM MeasureReport must still be
        sent so the MCS gets a snapshot recording 'no data found' for this
        patient/period — with no per-patient resources (and no inline
        reporter Organization; that is PUT once per job separately)."""
        wf = _deqm_workflow()
        empty = GatherResult(resources=[])
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=empty)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is empty
        submit.assert_awaited_once()
        params = submit.call_args.kwargs["parameters"]
        submitted_types = [p["resource"]["resourceType"] for p in params["parameter"][1:]]
        assert submitted_types == []
        mr = params["parameter"][0]["resource"]
        assert mr["evaluatedResource"] == []

    async def test_id_less_resource_excluded_from_submission_and_evaluated_resource(self):
        """F1: an id-less resource must not disagree between the MeasureReport's
        evaluatedResource and the submitted Parameters — both are derived from
        the SAME filtered list, so a resource missing `id` (or `resourceType`)
        is excluded from both."""
        wf = _deqm_workflow()
        gather_with_bad_resource = GatherResult(
            resources=[
                {"resourceType": "Patient", "id": "p1"},
                {"resourceType": "Condition", "id": "c1"},
                {"resourceType": "Observation"},  # no id — must be dropped
                {"id": "no-type"},  # no resourceType — must be dropped
            ]
        )
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=gather_with_bad_resource)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            result = await wf.transfer_patient("http://cdr", "p1", {})
        assert result is gather_with_bad_resource  # GatherResult passed through unfiltered to the caller
        params = submit.call_args.kwargs["parameters"]
        submitted_types = [p["resource"]["resourceType"] for p in params["parameter"][1:]]
        assert submitted_types == ["Patient", "Condition"]
        mr = params["parameter"][0]["resource"]
        refs = [er["reference"] for er in mr["evaluatedResource"]]
        assert refs == ["Patient/p1", "Condition/c1"]

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

    async def test_concurrent_patients_never_submit_organization_inline(self):
        """Regression for the production defect: every patient's $submit-data
        payload used to inline the SAME client-assigned
        Organization/lenny-reporter, and batches run concurrently
        (asyncio.Semaphore(MAX_WORKERS) + asyncio.gather in orchestrator.py).
        HAPI saw multiple transactions upsert that one resource id at once
        and raised ResourceVersionConflictException (HAPI-0550/HAPI-0823),
        failing 100% of patients on the real stack. Drive several
        transfer_patient() calls concurrently on ONE workflow instance and
        assert NO submitted payload contains an Organization resource — the
        shared resource must no longer travel in the per-patient path."""
        wf = _deqm_workflow()
        submit = AsyncMock()
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=submit),
        ):
            await asyncio.gather(*[wf.transfer_patient("http://cdr", f"p{i}", {}) for i in range(8)])

        assert submit.await_count == 8
        for call in submit.call_args_list:
            params = call.kwargs["parameters"]
            submitted_types = [p["resource"]["resourceType"] for p in params["parameter"][1:]]
            assert "Organization" not in submitted_types


class TestAcquisitionStrategy:
    """Direct, parametrized coverage of _acquisition_strategy (coverage-audit
    gap fill — previously only exercised indirectly through DirectLoadWorkflow
    construction in test_services_orchestrator.py)."""

    @pytest.mark.parametrize(
        "configured_strategy,expected_cls",
        [
            ("batch", BatchQueryStrategy),
            ("data_requirements", DataRequirementsStrategy),
        ],
    )
    def test_selects_strategy_from_settings(self, monkeypatch, configured_strategy, expected_cls):
        monkeypatch.setattr(settings, "PATIENT_DATA_STRATEGY", configured_strategy)
        strategy = _acquisition_strategy("M1", "http://mcs", {"Authorization": "Bearer t"})
        assert isinstance(strategy, expected_cls)

    def test_data_requirements_strategy_receives_measure_and_mcs_args(self, monkeypatch):
        monkeypatch.setattr(settings, "PATIENT_DATA_STRATEGY", "data_requirements")
        strategy = _acquisition_strategy("M1", "http://mcs", {"Authorization": "Bearer t"})
        assert strategy._measure_id == "M1"
        assert strategy._mcs_url == "http://mcs"
        assert strategy._mcs_auth_headers == {"Authorization": "Bearer t"}

    def test_batch_strategy_ignores_measure_and_mcs_args(self, monkeypatch):
        monkeypatch.setattr(settings, "PATIENT_DATA_STRATEGY", "batch")
        strategy = _acquisition_strategy("M1", "http://mcs", {"Authorization": "Bearer t"})
        assert isinstance(strategy, BatchQueryStrategy)


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
        with (
            patch(
                "app.services.workflows.get_measure_canonical",
                new=AsyncMock(return_value="http://ex.org/Measure/M1|1.0"),
            ) as canon,
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
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
        # build_submission_workflow must NOT write anything: it runs BEFORE
        # _wipe_prior_run_data, whose full-wipe branch deletes Organization.
        # Staging the reporter here got it deleted before any patient was
        # submitted. The write now belongs to ensure_target_prerequisites().
        push.assert_not_awaited()

    async def test_deqm_ensure_prerequisites_pushes_reporter_once(self):
        """The reporter Organization is PUT exactly once per job by the
        post-wipe hook -- not per patient (that was the HAPI-0823
        version-conflict storm) and not at build time (the wipe deleted it)."""
        with (
            patch(
                "app.services.workflows.get_measure_canonical",
                new=AsyncMock(return_value="http://ex.org/Measure/M1|1.0"),
            ),
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="M1",
                mcs_url="http://mcs",
                mcs_auth_headers={},
                submit_data_mode=None,
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
            push.assert_not_awaited()
            await wf.ensure_target_prerequisites()

        push.assert_awaited_once()
        push_args, push_kwargs = push.call_args
        assert [r["resourceType"] for r in push_args[0]] == ["Organization"]
        assert push_args[0][0]["id"] == LENNY_REPORTER_ORG["id"]
        assert push_kwargs["target_url"] == "http://mcs"
        assert push_kwargs["auth_headers"] == {}

    async def test_direct_load_ensure_prerequisites_is_a_noop(self):
        """direct_load stages nothing, so the orchestrator's unconditional
        post-wipe call must be harmless for it."""
        with patch("app.services.workflows.push_resources", new=AsyncMock()) as push:
            wf = await build_submission_workflow(
                workflow="direct_load",
                job_id=1,
                measure_id="M1",
                mcs_url="http://mcs",
                mcs_auth_headers={},
                submit_data_mode=None,
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
            await wf.ensure_target_prerequisites()
        push.assert_not_awaited()

    async def test_deqm_stu5_mode_raises_on_relative_canonical(self):
        """F4: in STU5 mode, MeasureReport.measure is the only identifier —
        a relative reference (degraded from a Measure with no `url`) is not a
        resolvable canonical there. Fail fast at job build."""
        with patch(
            "app.services.workflows.get_measure_canonical",
            new=AsyncMock(return_value="Measure/M1"),  # degraded relative reference
        ):
            with pytest.raises(ValueError, match="absolute canonical URL"):
                await build_submission_workflow(
                    workflow="deqm_submit_data",
                    job_id=1,
                    measure_id="M1",
                    mcs_url="http://mcs",
                    mcs_auth_headers={},
                    submit_data_mode="stu5",
                    period_start="2025-01-01",
                    period_end="2025-12-31",
                )

    async def test_deqm_base_mode_tolerates_relative_canonical(self):
        """F4: base-fallback mode is unaffected — the measure is already
        named in the instance-level submit URL."""
        with (
            patch(
                "app.services.workflows.get_measure_canonical",
                new=AsyncMock(return_value="Measure/M1"),
            ),
            patch("app.services.workflows.push_resources", new=AsyncMock()),
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="M1",
                mcs_url="http://mcs",
                mcs_auth_headers={},
                submit_data_mode="base-fallback",
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
        assert isinstance(wf, DeqmSubmitDataWorkflow)
        assert wf._measure_canonical == "Measure/M1"

    async def test_deqm_reporter_org_push_failure_raises(self):
        """A failure PUTting the reporter Organization must fail the job fast
        with a clear message, rather than deferring to every patient failing
        later for the same reason (the original HAPI-0823 defect)."""
        with (
            patch(
                "app.services.workflows.get_measure_canonical",
                new=AsyncMock(return_value="http://ex.org/Measure/M1|1.0"),
            ),
            patch(
                "app.services.workflows.push_resources",
                new=AsyncMock(side_effect=RuntimeError("mcs down")),
            ),
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="M1",
                mcs_url="http://mcs",
                mcs_auth_headers={},
                submit_data_mode="base-fallback",
                period_start="2025-01-01",
                period_end="2025-12-31",
            )
            with pytest.raises(ValueError, match="lenny-reporter"):
                await wf.ensure_target_prerequisites()

    async def test_deqm_propagates_canonical_fetch_failure(self):
        """Coverage-audit gap fill: build_submission_workflow must let a
        get_measure_canonical failure propagate (job fails fast) rather than
        swallowing it or returning a partially-built workflow."""
        with patch(
            "app.services.workflows.get_measure_canonical",
            new=AsyncMock(
                side_effect=FhirOperationError(
                    operation="read-measure", url="http://mcs/Measure/M1", status_code=404, outcome=None, latency_ms=1
                )
            ),
        ):
            with pytest.raises(FhirOperationError):
                await build_submission_workflow(
                    workflow="deqm_submit_data",
                    job_id=1,
                    measure_id="M1",
                    mcs_url="http://mcs",
                    mcs_auth_headers={},
                    submit_data_mode=None,
                    period_start="2025-01-01",
                    period_end="2025-12-31",
                )

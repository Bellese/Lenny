"""Tests for the per-job submission workflow strategies (workflows.py)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.deqm import LENNY_REPORTER_ORG
from app.services.fhir_client import (
    SUBMIT_DATA_MODE_BASE,
    SUBMIT_DATA_MODE_STU5,
    BatchQueryStrategy,
    DataRequirementsStrategy,
    GatherResult,
)
from app.services.fhir_errors import FhirOperationError, FhirOperationOutcome
from app.services.workflows import (
    DeqmSubmitDataWorkflow,
    DirectLoadWorkflow,
    PreparedSubject,
    SubjectOutcome,
    SubmissionWorkflow,
    TransferPhaseError,
    _acquisition_strategy,
    build_submission_workflow,
)


def _fhir_op_error_with_outcome(status_code: int, diagnostics: str) -> FhirOperationError:
    """#414: a 400's meaning lives in its OperationOutcome, not its status."""
    return FhirOperationError(
        operation="submit-data",
        url="http://mcs/Measure/$submit-data",
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
        url="http://mcs/Measure/$submit-data",
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
                _fhir_op_error_with_outcome(400, "does not know how to handle POST operation[Measure/$submit-data]"),
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
        """F3: a server that advertises $submit-data but doesn't
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

    async def test_repeated_resource_appears_once_with_one_evaluated_reference(self):
        """A gather that returns the same resource twice must not produce two
        Bundle entries or two evaluatedResource references. The receiver treats
        the Bundle as a transaction, and duplicate entries for one id are a
        conflict rather than a harmless repeat."""
        gather = GatherResult(
            resources=[
                {"resourceType": "Patient", "id": "p1"},
                {"resourceType": "Condition", "id": "c1"},
                {"resourceType": "Condition", "id": "c1"},
            ]
        )
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=gather)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        bundle = submit.call_args.kwargs["parameters"]["parameter"][0]["resource"]
        entries = [e["resource"] for e in bundle["entry"]]
        mr = entries[0]
        identities = [f"{r['resourceType']}/{r['id']}" for r in entries[1:]]
        assert identities == ["Patient/p1", "Condition/c1"]
        refs = [e["reference"] for e in mr["evaluatedResource"]]
        assert refs == ["Patient/p1", "Condition/c1"]

    async def test_evaluated_references_resolve_to_bundle_entries(self):
        """No dangling references: every evaluatedResource must name an entry
        that is actually in the Bundle, and vice versa."""
        gather = GatherResult(
            resources=[
                {"resourceType": "Patient", "id": "p1"},
                {"resourceType": "Condition", "id": "c1"},
                {"resourceType": "Encounter", "id": "e1"},
            ]
        )
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=gather)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        bundle = submit.call_args.kwargs["parameters"]["parameter"][0]["resource"]
        entries = [e["resource"] for e in bundle["entry"]]
        entry_ids = {f"{r['resourceType']}/{r['id']}" for r in entries[1:]}
        refs = {e["reference"] for e in entries[0]["evaluatedResource"]}
        assert refs == entry_ids

    async def test_conflicting_duplicate_keeps_first_and_warns(self):
        gather = GatherResult(
            resources=[
                {"resourceType": "Patient", "id": "p1", "gender": "female"},
                {"resourceType": "Patient", "id": "p1", "gender": "male"},
            ]
        )
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=gather)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
            patch("app.services.workflows.logger.warning") as warn,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        bundle = submit.call_args.kwargs["parameters"]["parameter"][0]["resource"]
        patients = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"]
        assert patients == [{"resourceType": "Patient", "id": "p1", "gender": "female"}]
        warn.assert_called_once()
        assert "Patient/p1" in warn.call_args.kwargs["extra"]["identities"]

    async def test_shared_resource_is_not_suppressed_across_subjects(self):
        """Deduplication is per subject. A Practitioner gathered for two
        patients belongs in BOTH their Bundles — each Bundle may be processed
        on its own, so suppressing the second would leave a dangling
        reference."""
        shared = {"resourceType": "Practitioner", "id": "prac1"}
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(
                    side_effect=[
                        GatherResult(resources=[{"resourceType": "Patient", "id": "p1"}, shared]),
                        GatherResult(resources=[{"resourceType": "Patient", "id": "p2"}, shared]),
                    ]
                ),
            ),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
            await wf.transfer_patient("http://cdr", "p2", {})
        for call in submit.call_args_list:
            bundle = call.kwargs["parameters"]["parameter"][0]["resource"]
            types = [e["resource"]["resourceType"] for e in bundle["entry"]]
            assert "Practitioner" in types


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


class TestGroupProtocolDefaults:
    """The base-class defaults are what keep every pre-grouping workflow
    working under the new protocol. A workflow that implements only
    transfer_patient — which is every workflow in the codebase before this
    PR, and _StubWorkflow in the orchestrator tests — must still transfer
    correctly when the orchestrator drives it through prepare/submit."""

    class _OnlyTransferPatient(SubmissionWorkflow):
        name = "only-transfer"

        def __init__(self, outcome):
            self.outcome = outcome
            self.calls: list[tuple[str, str, dict]] = []

        async def transfer_patient(self, cdr_url, patient_id, cdr_auth_headers):
            self.calls.append((cdr_url, patient_id, cdr_auth_headers))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    async def test_default_group_size_is_one(self):
        wf = self._OnlyTransferPatient(_GATHER)
        assert wf.submission_group_size == 1

    async def test_default_prepare_does_no_io_and_carries_cdr_coordinates(self):
        """The default defers the whole transfer, so it must hand
        submit_prepared the CDR arguments transfer_patient will need."""
        wf = self._OnlyTransferPatient(_GATHER)
        subject = await wf.prepare_patient("http://cdr", "p1", {"Authorization": "Bearer t"})
        assert wf.calls == [], "the default prepare_patient must not touch the CDR"
        assert isinstance(subject, PreparedSubject)
        assert subject.patient_id == "p1"
        assert subject.cdr_url == "http://cdr"
        assert subject.cdr_auth_headers == {"Authorization": "Bearer t"}

    async def test_default_submit_delegates_to_transfer_patient(self):
        wf = self._OnlyTransferPatient(_GATHER)
        subject = await wf.prepare_patient("http://cdr", "p1", {})
        outcomes = await wf.submit_prepared([subject])
        assert wf.calls == [("http://cdr", "p1", {})]
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], SubjectOutcome)
        assert outcomes[0].patient_id == "p1"
        assert outcomes[0].gather is _GATHER
        assert outcomes[0].error is None

    async def test_default_submit_returns_the_failure_instead_of_raising(self):
        """submit_prepared's contract is one outcome per subject. Raising
        would abort the whole group for one subject's failure — the exact
        thing the outcome list exists to prevent."""
        boom = TransferPhaseError("gather", RuntimeError("cdr down"))
        wf = self._OnlyTransferPatient(boom)
        subject = await wf.prepare_patient("http://cdr", "p1", {})
        outcomes = await wf.submit_prepared([subject])
        assert outcomes[0].error is boom
        assert outcomes[0].error.phase == "gather"
        assert outcomes[0].gather is None

    async def test_default_submit_wraps_a_bare_exception_as_a_gather_failure(self):
        """A workflow that raises something other than TransferPhaseError must
        still produce an outcome the orchestrator can persist. 'gather' is the
        historical label for an unclassified transfer failure."""
        wf = self._OnlyTransferPatient(ValueError("nope"))
        subject = await wf.prepare_patient("http://cdr", "p1", {})
        outcomes = await wf.submit_prepared([subject])
        assert isinstance(outcomes[0].error, TransferPhaseError)
        assert outcomes[0].error.phase == "gather"
        assert isinstance(outcomes[0].error.cause, ValueError)

    async def test_default_submit_isolates_failures_across_subjects(self):
        """One subject failing must not deny the others their outcome."""

        class _Selective(SubmissionWorkflow):
            name = "selective"

            async def transfer_patient(self, cdr_url, patient_id, cdr_auth_headers):
                if patient_id == "p2":
                    raise TransferPhaseError("submit", RuntimeError("bad"))
                return _GATHER

        wf = _Selective()
        subjects = [await wf.prepare_patient("http://cdr", pid, {}) for pid in ("p1", "p2", "p3")]
        outcomes = await wf.submit_prepared(subjects)
        assert [o.patient_id for o in outcomes] == ["p1", "p2", "p3"]
        assert [o.error is None for o in outcomes] == [True, False, True]

    async def test_direct_load_works_through_the_group_protocol(self):
        """DirectLoadWorkflow gains no lines in this PR; it must transfer
        correctly purely via the inherited defaults."""
        wf = DirectLoadWorkflow("M1", "http://mcs", {"Authorization": "Bearer t"})
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.push_resources", new=AsyncMock()) as push,
        ):
            subject = await wf.prepare_patient("http://cdr", "p1", {})
            outcomes = await wf.submit_prepared([subject])
        assert wf.submission_group_size == 1
        push.assert_awaited_once()
        assert outcomes[0].error is None
        assert outcomes[0].gather is _GATHER


class TestDeqmPrepareAndSubmit:
    async def test_prepare_does_the_cdr_work_and_no_mcs_io(self):
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            subject = await wf.prepare_patient("http://cdr", "p1", {})
        submit.assert_not_awaited()
        assert subject.patient_id == "p1"
        assert subject.gather is _GATHER
        assert subject.measure_report["id"] == "deqm-7-p1"
        assert [r["id"] for r in subject.resources] == ["p1", "c1"]

    async def test_prepare_wraps_a_gather_failure_as_a_raise(self):
        """prepare_patient raises rather than returning an outcome: a gather
        failure must not be collected into the group it was destined for."""
        wf = _deqm_workflow()
        with patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(side_effect=RuntimeError("cdr down"))):
            with pytest.raises(TransferPhaseError) as caught:
                await wf.prepare_patient("http://cdr", "p1", {})
        assert caught.value.phase == "gather"

    async def test_default_group_size_is_one(self):
        assert _deqm_workflow(mode="stu5").submission_group_size == 1

    async def test_group_size_collapses_to_one_outside_stu5(self):
        """base-fallback has no multi-bundle form, so the size must read 1 no
        matter what was configured."""
        wf = _deqm_workflow(mode="base-fallback")
        wf._group_size = 5
        assert wf.submission_group_size == 1

    async def test_transfer_patient_still_raises_on_a_submit_failure(self):
        """The one-subject entry point keeps its raising contract; only
        submit_prepared returns outcomes."""
        wf = _deqm_workflow()
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch(
                "app.services.workflows.submit_data",
                new=AsyncMock(side_effect=_fhir_op_error(500)),
            ),
        ):
            with pytest.raises(TransferPhaseError) as caught:
                await wf.transfer_patient("http://cdr", "p1", {})
        assert caught.value.phase == "submit"

    async def test_a_single_subject_group_does_not_isolate(self):
        """Isolation resubmits subjects one at a time. With one subject there
        is nothing to isolate, and retrying would POST twice where PR 1 posts
        once — which is what 'byte-identical at size 1' means."""
        wf = _deqm_workflow(mode="stu5")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=_GATHER)),
            patch(
                "app.services.workflows.submit_data",
                new=AsyncMock(side_effect=_fhir_op_error(400)),
            ) as submit,
        ):
            subject = await wf.prepare_patient("http://cdr", "p1", {})
            outcomes = await wf.submit_prepared([subject])
        assert submit.await_count == 1
        assert outcomes[0].error is not None
        assert outcomes[0].error.phase == "submit"


class TestDeqmGrouping:
    """These exercise the SETTLED submit path. The pioneer — the first group
    to submit while the mode is still undecided — belongs to the #414 barrier
    and is covered separately; settling the event here keeps each test aimed at
    one mechanism."""

    async def _prepare(self, wf, patient_ids):
        subjects = []
        for pid in patient_ids:
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])),
            ):
                subjects.append(await wf.prepare_patient("http://cdr", pid, {}))
        return subjects

    async def test_a_group_of_n_is_one_post_with_n_bundles(self):
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = 3
        wf._mode_settled.set()  # past the pioneer
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            outcomes = await wf.submit_prepared(subjects)
        submit.assert_awaited_once()
        params = submit.call_args.kwargs["parameters"]
        assert submit.call_args.kwargs["mode"] == "stu5"
        assert [p["name"] for p in params["parameter"]] == ["bundle", "bundle", "bundle"]
        assert [o.patient_id for o in outcomes] == ["p1", "p2", "p3"]
        assert all(o.error is None for o in outcomes)

    async def test_each_bundle_holds_exactly_one_subject(self):
        """Several subjects never share a Bundle: the receiver processes each
        as a transaction, so merging them would make one subject's bad resource
        fail the others."""
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = 2
        wf._mode_settled.set()  # past the pioneer
        subjects = await self._prepare(wf, ["p1", "p2"])
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            await wf.submit_prepared(subjects)
        bundles = [p["resource"] for p in submit.call_args.kwargs["parameters"]["parameter"]]
        for bundle, pid in zip(bundles, ["p1", "p2"]):
            assert bundle["type"] == "collection"
            mr = bundle["entry"][0]["resource"]
            assert mr["resourceType"] == "MeasureReport"
            assert mr["subject"] == {"reference": f"Patient/{pid}"}
            subjects_in_bundle = {
                e["resource"]["id"] for e in bundle["entry"][1:] if e["resource"]["resourceType"] == "Patient"
            }
            assert subjects_in_bundle == {pid}

    async def test_a_group_of_one_is_byte_identical_to_the_ungrouped_payload(self):
        """The regression that would make this PR non-neutral: a size-1 group
        must produce the same single-bundle envelope PR 1 shipped."""
        wf = _deqm_workflow(mode="stu5")
        wf._mode_settled.set()  # past the pioneer
        subjects = await self._prepare(wf, ["p1"])
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            await wf.submit_prepared(subjects)
        submit.assert_awaited_once()
        params = submit.call_args.kwargs["parameters"]
        assert [p["name"] for p in params["parameter"]] == ["bundle"]

    async def test_submit_group_itself_handles_a_single_subject(self):
        """This test calls `_submit_group` directly, rather than going through
        `submit_prepared`, so the single-subject envelope is covered by a test
        that does not depend on how `submit_prepared` happens to route.
        Without this, `_submit_group`'s own size-1 handling could silently
        break, or the method could be deleted outright, and
        test_a_group_of_one_is_byte_identical_to_the_ungrouped_payload would
        not notice, because it goes through `submit_prepared` and never
        necessarily reaches `_submit_group` at all.
        """
        wf = _deqm_workflow(mode="stu5")
        subjects = await self._prepare(wf, ["p1"])
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            outcomes = await wf._submit_group(subjects)
        submit.assert_awaited_once()
        assert submit.call_args.kwargs["mode"] == "stu5"
        params = submit.call_args.kwargs["parameters"]
        assert [p["name"] for p in params["parameter"]] == ["bundle"]
        bundle = params["parameter"][0]["resource"]
        mr = bundle["entry"][0]["resource"]
        assert mr["resourceType"] == "MeasureReport"
        assert mr["subject"] == {"reference": "Patient/p1"}
        assert [o.patient_id for o in outcomes] == ["p1"]
        assert outcomes[0].error is None

    async def test_a_group_submitted_after_a_downgrade_goes_out_individually(self):
        """A chunk can form a group of N and then have another chunk's pioneer
        downgrade before it submits. Building a multi-bundle envelope for a
        server that has already refused the operation would fail every subject."""
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = 3
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        # Simulate the settled-and-downgraded state the pioneer leaves behind.
        wf._mode = "base-fallback"
        wf._downgraded = True
        wf._mode_settled.set()
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 3
        assert all(c.kwargs["mode"] == "base-fallback" for c in submit.await_args_list)
        for call in submit.await_args_list:
            names = [p["name"] for p in call.kwargs["parameters"]["parameter"]]
            assert names[0] == "measureReport"
            assert "bundle" not in names
        assert all(o.error is None for o in outcomes)

    async def test_an_empty_group_submits_nothing(self):
        wf = _deqm_workflow(mode="stu5")
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            assert await wf.submit_prepared([]) == []
        submit.assert_not_awaited()


class TestGroupFailureIsolation:
    async def _prepare(self, wf, patient_ids):
        subjects = []
        for pid in patient_ids:
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])),
            ):
                subjects.append(await wf.prepare_patient("http://cdr", pid, {}))
        return subjects

    def _settled_stu5(self, group_size: int) -> DeqmSubmitDataWorkflow:
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = group_size
        # Past the pioneer: this group may isolate, but never downgrade.
        wf._mode_settled.set()
        return wf

    @pytest.mark.parametrize("status", [400, 409, 422])
    async def test_a_payload_attributable_failure_isolates(self, status):
        wf = self._settled_stu5(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        calls = {"n": 0}

        async def submit_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _fhir_op_error(status)  # the group POST
            if len(parameters["parameter"]) == 1 and parameters["parameter"][0]["resource"]["entry"][0]["resource"][
                "subject"
            ]["reference"].endswith("p2"):
                raise _fhir_op_error(status)  # p2 owns the bad resource
            return None

        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_side_effect)):
            outcomes = await wf.submit_prepared(subjects)

        assert calls["n"] == 4, "one group POST plus one per subject"
        assert [o.error is None for o in outcomes] == [True, False, True]
        assert outcomes[1].patient_id == "p2"
        assert outcomes[1].error.phase == "submit"

    @pytest.mark.parametrize("status", [401, 403, 404, 405, 429, 500, 503])
    async def test_a_server_failure_fails_the_whole_group_in_one_post(self, status):
        """Resubmitting N times only asks a down or unauthenticated server the
        same question N more times. On a full chunk that is 101 POSTs for one
        answer."""
        wf = self._settled_stu5(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(status))) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 1
        assert all(o.error is not None for o in outcomes)
        assert all(o.error.phase == "submit" for o in outcomes)
        assert [o.patient_id for o in outcomes] == ["p1", "p2", "p3"]

    async def test_a_non_http_failure_fails_the_whole_group_in_one_post(self):
        """A timeout is not a statement about anyone's payload."""
        wf = self._settled_stu5(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        with patch(
            "app.services.workflows.submit_data",
            new=AsyncMock(side_effect=asyncio.TimeoutError("timed out")),
        ) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 1
        assert all(isinstance(o.error.cause, asyncio.TimeoutError) for o in outcomes)

    async def test_isolation_never_downgrades(self):
        """Only _settle_mode may change the mode (#414). A settled
        group that isolates must leave the job's wire format alone."""
        wf = self._settled_stu5(2)
        subjects = await self._prepare(wf, ["p1", "p2"])
        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(400))) as submit:
            await wf.submit_prepared(subjects)
        assert wf.mode == "stu5"
        assert wf.downgraded is False
        assert all(c.kwargs["mode"] == "stu5" for c in submit.await_args_list)

    async def test_isolation_retries_under_the_settled_mode(self):
        wf = self._settled_stu5(2)
        subjects = await self._prepare(wf, ["p1", "p2"])
        with patch(
            "app.services.workflows.submit_data",
            new=AsyncMock(side_effect=[_fhir_op_error(400), None, None]),
        ) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 3
        assert all(c.kwargs["mode"] == "stu5" for c in submit.await_args_list)
        assert all(o.error is None for o in outcomes)


class TestPioneerGroup:
    async def _prepare(self, wf, patient_ids):
        subjects = []
        for pid in patient_ids:
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])),
            ):
                subjects.append(await wf.prepare_patient("http://cdr", pid, {}))
        return subjects

    def _pioneer(self, group_size: int) -> DeqmSubmitDataWorkflow:
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = group_size
        return wf  # _mode_settled is unset: this group IS the pioneer

    async def test_the_pioneer_group_sends_n_bundles_in_one_post(self):
        wf = self._pioneer(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        with patch("app.services.workflows.submit_data", new=AsyncMock()) as submit:
            outcomes = await wf.submit_prepared(subjects)
        submit.assert_awaited_once()
        assert len(submit.call_args.kwargs["parameters"]["parameter"]) == 3
        assert all(o.error is None for o in outcomes)
        assert wf._mode_settled.is_set()

    @pytest.mark.parametrize("status", [404, 405, 501])
    async def test_a_capability_signal_downgrades_and_does_not_isolate(self, status):
        """Capability first, isolation second, never both. The group's subjects
        go out individually in base mode — which is what base-fallback does
        anyway — not as a second STU5 attempt."""
        wf = self._pioneer(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        sent: list[str] = []

        async def submit_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            sent.append(mode)
            if mode == "stu5":
                raise _fhir_op_error(status)
            return None

        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_side_effect)):
            outcomes = await wf.submit_prepared(subjects)

        assert sent == ["stu5", "base-fallback", "base-fallback", "base-fallback"]
        assert wf.mode == "base-fallback"
        assert wf.downgraded is True
        assert all(o.error is None for o in outcomes)

    async def test_a_400_saying_the_operation_is_missing_downgrades(self):
        """#414: a 400's meaning lives in its OperationOutcome, not its status."""
        wf = self._pioneer(2)
        subjects = await self._prepare(wf, ["p1", "p2"])
        sent: list[str] = []

        async def submit_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            sent.append(mode)
            if mode == "stu5":
                raise _fhir_op_error_with_outcome(400, "does not know how to handle POST operation")
            return None

        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_side_effect)):
            outcomes = await wf.submit_prepared(subjects)

        assert sent == ["stu5", "base-fallback", "base-fallback"]
        assert wf.downgraded is True
        assert all(o.error is None for o in outcomes)

    async def test_a_payload_rejection_isolates_and_does_not_downgrade(self):
        wf = self._pioneer(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        with patch(
            "app.services.workflows.submit_data",
            new=AsyncMock(side_effect=[_fhir_op_error(400), None, _fhir_op_error(400), None]),
        ) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 4
        assert all(c.kwargs["mode"] == "stu5" for c in submit.await_args_list)
        assert wf.mode == "stu5"
        assert wf.downgraded is False
        assert [o.error is None for o in outcomes] == [True, False, True]

    async def test_a_server_failure_in_the_pioneer_group_neither_downgrades_nor_isolates(self):
        wf = self._pioneer(3)
        subjects = await self._prepare(wf, ["p1", "p2", "p3"])
        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(503))) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 1
        assert wf.downgraded is False
        assert all(o.error is not None for o in outcomes)

    async def test_the_barrier_releases_even_when_the_pioneer_group_fails(self):
        """A pioneer group whose submission fails must still publish a verdict
        and release every waiter: `_settle_mode` catches the failure and turns
        it into a "fail" _Settlement, then submit_prepared's `finally` sets
        `_mode_settled` on that normal-return path, so other groups never wait
        forever on a verdict that never arrives. (That `finally` also covers
        the raise path, not this one — removing `.set()` itself still fails
        five other tests, so it stays well covered.)"""
        wf = self._pioneer(2)
        subjects = await self._prepare(wf, ["p1", "p2"])
        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(503))):
            await wf.submit_prepared(subjects)
        assert wf._mode_settled.is_set()

    async def test_a_second_group_waits_for_the_pioneers_verdict(self):
        """Two chunks submitting concurrently must not produce a job that is
        half STU5 and half base (#414)."""
        wf = self._pioneer(2)
        first = await self._prepare(wf, ["p1", "p2"])
        second = await self._prepare(wf, ["p3", "p4"])
        modes: list[str] = []
        release = asyncio.Event()

        async def submit_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            modes.append(mode)
            if mode == "stu5":
                await release.wait()
                raise _fhir_op_error(404)
            return None

        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_side_effect)):
            pioneer = asyncio.create_task(wf.submit_prepared(first))
            await asyncio.sleep(0)
            waiter = asyncio.create_task(wf.submit_prepared(second))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(pioneer, waiter)

        assert modes.count("stu5") == 1, "only the pioneer may attempt STU5"
        assert wf.mode == "base-fallback"
        assert all(m == "base-fallback" for m in modes[1:])


class TestPioneerReleasesTheBarrierEarly:
    async def _prepare(self, wf, patient_ids):
        subjects = []
        for pid in patient_ids:
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])),
            ):
                subjects.append(await wf.prepare_patient("http://cdr", pid, {}))
        return subjects

    def _pioneer(self, group_size: int) -> DeqmSubmitDataWorkflow:
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = group_size
        return wf  # _mode_settled is unset: this group IS the pioneer

    async def test_the_barrier_opens_after_the_pioneers_first_post_not_its_last(self):
        """Finding M6. The pioneer's re-sends must happen outside _mode_lock:
        a downgrading group of N otherwise blocks every other chunk for N
        sequential round trips, which is only reachable once operators can
        select N (#413 PR 3)."""
        wf = self._pioneer(3)
        subjects = await self._prepare(wf, ["p0", "p1", "p2"])
        posts: list[str] = []
        barrier_open_after: list[int] = []

        async def _post(parameters, mode):
            posts.append(mode)
            if len(posts) == 1:
                raise FhirOperationError(
                    operation="submit-data", url="http://mcs", status_code=404, outcome=None, latency_ms=1
                )
            # By the time a re-send runs, a waiter must already be admissible.
            if not wf._mode_lock.locked():
                barrier_open_after.append(len(posts))

        wf._post = AsyncMock(side_effect=_post)
        outcomes = await wf.submit_prepared(subjects)

        assert len(outcomes) == 3
        assert posts[0] == SUBMIT_DATA_MODE_STU5
        assert posts[1:] == [SUBMIT_DATA_MODE_BASE] * 3
        # The lock was already free on the FIRST re-send, i.e. post #2.
        assert barrier_open_after and barrier_open_after[0] == 2

    async def test_the_mode_is_already_decided_when_the_barrier_opens(self):
        """#414 must survive the narrowing: self._mode and self._downgraded are
        written inside the lock, before the event is set, so no POST can ever
        observe an open barrier next to a half-decided mode."""
        wf = self._pioneer(2)
        subjects = await self._prepare(wf, ["a", "b"])
        calls: list[int] = []

        async def _post(parameters, mode):
            calls.append(len(calls))
            if len(calls) == 1:
                # The pioneer POST, still inside the lock, still undecided.
                raise FhirOperationError(
                    operation="submit-data", url="http://mcs", status_code=404, outcome=None, latency_ms=1
                )
            # Every later POST is a re-send running outside the lock. If the
            # barrier is open, the decision must already be visible and final.
            assert wf._mode_settled.is_set()
            assert wf.mode == SUBMIT_DATA_MODE_BASE
            assert wf.downgraded is True
            assert mode == SUBMIT_DATA_MODE_BASE

        wf._post = AsyncMock(side_effect=_post)
        outcomes = await wf.submit_prepared(subjects)

        assert len(calls) == 3  # one pioneer POST, then one re-send per subject
        assert len(outcomes) == 2
        assert all(o.error is None for o in outcomes)
        assert wf.mode == SUBMIT_DATA_MODE_BASE
        assert wf.downgraded is True


class TestCrossChunkSafety:
    async def test_two_concurrent_chunks_never_mix_subjects(self):
        """DeqmSubmitDataWorkflow must stay stateless across concurrent
        submit_prepared calls: one workflow instance serves every concurrent
        chunk of a job, so any per-subject state the reviewer injects onto the
        instance (rather than keeping it local to each submit_prepared call)
        interleaves subjects from different chunks and misattributes their
        failures — this is the test that catches it."""
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = 2
        wf._mode_settled.set()

        async def _prepare(pid):
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])),
            ):
                return await wf.prepare_patient("http://cdr", pid, {})

        chunk_a = [await _prepare(p) for p in ("a1", "a2")]
        chunk_b = [await _prepare(p) for p in ("b1", "b2")]
        posted: list[list[str]] = []

        async def submit_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            posted.append(
                [
                    p["resource"]["entry"][0]["resource"]["subject"]["reference"].split("/")[1]
                    for p in parameters["parameter"]
                ]
            )
            await asyncio.sleep(0)  # force interleaving

        with patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_side_effect)):
            await asyncio.gather(wf.submit_prepared(chunk_a), wf.submit_prepared(chunk_b))

        assert sorted(posted) == [["a1", "a2"], ["b1", "b2"]]


class TestDedupeAcrossModes:
    """Carried from PR 1's final review (finding M6, parked for this PR because
    the downgrade path it covers is rewritten here)."""

    _DUPES = GatherResult(
        resources=[
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Condition", "id": "c1", "code": {"text": "first"}},
            {"resourceType": "Condition", "id": "c1", "code": {"text": "first"}},
        ]
    )

    async def test_dedupe_applies_in_base_fallback_mode(self):
        """Deduplication runs upstream of mode selection, but nothing asserted
        it in base mode — where the resources travel as `resource` parameters
        rather than Bundle entries."""
        wf = _deqm_workflow(mode="base-fallback")
        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=self._DUPES)),
            patch("app.services.workflows.submit_data", new=AsyncMock()) as submit,
        ):
            await wf.transfer_patient("http://cdr", "p1", {})
        params = submit.call_args.kwargs["parameters"]
        resources = [p["resource"] for p in params["parameter"] if p["name"] == "resource"]
        assert [(r["resourceType"], r["id"]) for r in resources] == [("Patient", "p1"), ("Condition", "c1")]
        mr = params["parameter"][0]["resource"]
        assert mr["evaluatedResource"] == [
            {"reference": "Patient/p1"},
            {"reference": "Condition/c1"},
        ]

    async def test_the_downgrade_rebuilt_base_payload_preserves_dedupe(self):
        """The downgrade re-sends the group in base form. That payload is built
        from the same prepared resources, so the dedupe must survive the
        rebuild — nothing asserted that before."""
        wf = _deqm_workflow(mode="stu5")
        sent: list[dict] = []

        async def submit_side_effect(*, mcs_url, parameters, mode, measure_id, auth_headers=None):
            sent.append({"mode": mode, "parameters": parameters})
            if mode == "stu5":
                raise _fhir_op_error(404)
            return None

        with (
            patch.object(wf._strategy, "gather_patient_data", new=AsyncMock(return_value=self._DUPES)),
            patch("app.services.workflows.submit_data", new=AsyncMock(side_effect=submit_side_effect)),
        ):
            await wf.transfer_patient("http://cdr", "p1", {})

        assert wf.downgraded is True
        base = [s for s in sent if s["mode"] == "base-fallback"][0]["parameters"]
        resources = [p["resource"] for p in base["parameter"] if p["name"] == "resource"]
        assert [(r["resourceType"], r["id"]) for r in resources] == [("Patient", "p1"), ("Condition", "c1")]


class TestGroupSizeThreading:
    async def test_factory_passes_the_stored_value_to_the_workflow(self):
        with patch(
            "app.services.workflows.get_measure_canonical",
            AsyncMock(return_value="http://example.org/Measure/CMS999"),
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="CMS999",
                mcs_url="http://mcs",
                mcs_auth_headers=None,
                submit_data_mode=SUBMIT_DATA_MODE_STU5,
                period_start="2025-01-01",
                period_end="2025-12-31",
                bundles_per_submission=20,
            )
        assert wf.submission_group_size == 20

    async def test_a_legacy_null_resolves_to_one(self):
        """Rows created before the column existed read as None. One subject per
        POST is what those jobs actually did, so that is what None must mean."""
        with patch(
            "app.services.workflows.get_measure_canonical",
            AsyncMock(return_value="http://example.org/Measure/CMS999"),
        ):
            wf = await build_submission_workflow(
                workflow="deqm_submit_data",
                job_id=1,
                measure_id="CMS999",
                mcs_url="http://mcs",
                mcs_auth_headers=None,
                submit_data_mode=SUBMIT_DATA_MODE_STU5,
                period_start="2025-01-01",
                period_end="2025-12-31",
                bundles_per_submission=None,
            )
        assert wf.submission_group_size == 1

    async def test_direct_load_ignores_the_value_entirely(self):
        wf = await build_submission_workflow(
            workflow="direct_load",
            job_id=1,
            measure_id="CMS999",
            mcs_url="http://mcs",
            mcs_auth_headers=None,
            submit_data_mode=None,
            period_start="2025-01-01",
            period_end="2025-12-31",
            bundles_per_submission=50,
        )
        assert wf.submission_group_size == 1

"""Per-job data submission workflows (spec: 2026-08-21-deqm-submit-data-workflow).

A SubmissionWorkflow owns phase 1 of a job for one patient: gather from the
CDR, deliver to the MCS. The orchestrator picks the concrete class from
Job.workflow and calls transfer_patient(); phase 2 ($evaluate-measure) is
identical for every workflow and stays in the orchestrator.
"""

import abc
import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.services.deqm import (
    LENNY_REPORTER_ORG,
    build_base_parameters,
    build_data_exchange_measure_report,
    build_stu5_parameters,
)
from app.services.fhir_client import (
    SUBMIT_DATA_MODE_BASE,
    SUBMIT_DATA_MODE_STU5,
    BatchQueryStrategy,
    DataAcquisitionStrategy,
    DataRequirementsStrategy,
    GatherResult,
    get_measure_canonical,
    push_resources,
    submit_data,
)
from app.services.fhir_errors import FhirOperationError

# Capability-mismatch signals: a server that advertises $deqm-submit-data but
# doesn't actually implement the type-level POST commonly answers with one of
# these. Each is a statement about the SERVER, not about the payload, so it is
# a credible capability verdict on its own.
#
# 401/403 are deliberately excluded — auth failures, not capability mismatches,
# and must not be masked as a silent downgrade. 429/5xx are excluded too —
# transient/overload signals, not "this operation doesn't exist here";
# downgrading on them would paper over a retry-able failure as a permanent
# verdict.
#
# 400 is NOT here (#414). DEQM prescribes 400 for an update-type mismatch, and
# 400 is the generic FHIR answer to a rejected payload — so a per-patient
# validation failure was being read as a verdict about the server, discarding
# the real OperationOutcome. A genuine not-supported 400 does exist (HAPI
# answers the type-level base operation with "does not know how to handle POST
# operation[...]"), so a 400 is inspected rather than trusted: see
# _outcome_reports_unsupported_operation.
_DOWNGRADE_STATUS_CODES = {404, 405, 501}

# Substrings that mark an OperationOutcome as "this operation isn't implemented
# here" rather than "your payload was wrong". Deliberately narrow: a false
# positive changes the wire format for the rest of the job, so anything not
# clearly about the operation's existence is treated as a payload rejection.
#
# Text matching is fragile and this is the one place it is accepted, because
# the status code alone cannot distinguish the two cases and HAPI's wording is
# the documented real-world instance. A server whose phrasing differs simply
# does not downgrade on 400 — it fails that patient with the server's own
# explanation intact, which is the conservative direction.
_UNSUPPORTED_OPERATION_MARKERS = (
    "does not know how to handle",
    "not supported",
    "unsupported operation",
    "unknown operation",
    "operation not found",
)


def _outcome_reports_unsupported_operation(exc: FhirOperationError) -> bool:
    """True when a 400's OperationOutcome says the operation is missing (#414)."""
    if exc.outcome is None:
        return False
    for issue in exc.outcome.issues:
        text = (issue.diagnostics or "").lower()
        if any(marker in text for marker in _UNSUPPORTED_OPERATION_MARKERS):
            return True
    return False


logger = logging.getLogger(__name__)


class TransferPhaseError(Exception):
    """A transfer failed; `phase` says which half, for MeasureResult.error_phase.

    direct_load labels both halves "gather" — the historical behavior, kept so
    existing dashboards/tests keep meaning the same thing. deqm_submit_data
    labels delivery failures "submit".
    """

    def __init__(self, phase: str, cause: Exception):
        super().__init__(str(cause))
        self.phase = phase
        self.cause = cause


def _acquisition_strategy(
    measure_id: str, mcs_url: str, mcs_auth_headers: dict[str, str] | None = None
) -> DataAcquisitionStrategy:
    """The env-configured CDR acquisition strategy (moved from orchestrator).

    `mcs_url`/`mcs_auth_headers` are threaded to DataRequirementsStrategy so
    `$data-requirements` asks the job's own measure engine (issue #397).
    BatchQueryStrategy ignores them — it only talks to the CDR.
    """
    if settings.PATIENT_DATA_STRATEGY == "data_requirements":
        return DataRequirementsStrategy(measure_id, mcs_url, mcs_auth_headers)
    return BatchQueryStrategy()


class SubmissionWorkflow(abc.ABC):
    """Gathers one patient's data from the CDR and delivers it to the MCS."""

    name: str

    async def ensure_target_prerequisites(self) -> None:
        """Write any job-scoped resources the workflow needs on the MCS.

        Called by the orchestrator AFTER `_wipe_prior_run_data` -- which is the
        entire reason this is separate from `build_submission_workflow`. Build
        runs BEFORE the wipe on purpose, so a canonical-fetch failure aborts
        without wiping anything; but that ordering means anything build *writes*
        is deleted by the wipe moments later. `wipe_patient_data`'s full-wipe
        list includes "Organization", so the DEQM reporter created at build time
        was being removed before a single patient was submitted.

        Default is a no-op: direct_load needs nothing staged.
        """
        return None

    @abc.abstractmethod
    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        """Transfer one patient's data; return the GatherResult for
        partial-failure bookkeeping. Raises TransferPhaseError on failure."""
        ...


class DirectLoadWorkflow(SubmissionWorkflow):
    """Today's behavior: env-configured gather, then a batch Bundle of PUTs."""

    name = "direct_load"

    def __init__(self, measure_id: str, mcs_url: str, mcs_auth_headers: dict[str, str] | None = None):
        self._strategy = _acquisition_strategy(measure_id, mcs_url, mcs_auth_headers)
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers

    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        try:
            gather = await self._strategy.gather_patient_data(cdr_url, patient_id, cdr_auth_headers)
            if gather.resources:
                await push_resources(
                    gather.resources,
                    target_url=self._mcs_url,
                    auth_headers=self._mcs_auth_headers,
                )
        except Exception as exc:
            raise TransferPhaseError("gather", exc) from exc
        return gather


class DeqmSubmitDataWorkflow(SubmissionWorkflow):
    """DEQM data exchange: targeted queries, then Measure/$submit-data."""

    name = "deqm_submit_data"

    def __init__(
        self,
        *,
        job_id: int,
        measure_id: str,
        mcs_url: str,
        mcs_auth_headers: dict[str, str] | None,
        measure_canonical: str,
        period_start: str,
        period_end: str,
        mode: str,
    ):
        # Targeted queries are part of the DEQM workflow by design, independent
        # of the env-configured default strategy.
        self._strategy = DataRequirementsStrategy(measure_id, mcs_url, mcs_auth_headers)
        self._job_id = job_id
        self._measure_id = measure_id
        self._mcs_url = mcs_url
        self._mcs_auth_headers = mcs_auth_headers
        self._measure_canonical = measure_canonical
        self._period_start = period_start
        self._period_end = period_end
        self._mode = mode
        # #414 (ruling: mixed-mode jobs are prohibited). The runtime downgrade
        # exists to rescue a mis-probed job, but under
        # asyncio.Semaphore(MAX_WORKERS) + asyncio.gather one patient's STU5
        # submission can still be in flight when another patient's failure
        # decides to downgrade — so a plain "has anything succeeded yet?" flag
        # leaves a window where a job ends up half STU5 and half base.
        #
        # Instead the mode is SETTLED ONCE, behind a barrier: the first patient
        # to reach the submit step under STU5 becomes the pioneer and is the
        # only one allowed to downgrade. Everyone else waits for its verdict
        # and then submits under the settled mode, with no downgrade path of
        # their own. One patient's submission is therefore serialized; the rest
        # run fully concurrent as before.
        #
        # The barrier only engages while the mode is STU5. base-fallback has
        # nowhere to downgrade to, and per the v0.1.0.0 notes every server
        # tested so far resolves to base — so in practice this costs nothing.
        self._mode_lock = asyncio.Lock()
        self._mode_settled = asyncio.Event()
        self._downgraded = False
        if mode != SUBMIT_DATA_MODE_STU5:
            self._mode_settled.set()

    @property
    def mode(self) -> str:
        """The mode actually in use — the settled one, once settlement ran."""
        return self._mode

    @property
    def downgraded(self) -> bool:
        """True once a runtime downgrade has happened.

        workflows.py is deliberately session-free, so the orchestrator (which
        already owns DB access and writes Job fields) reads this to persist
        Job.submit_data_mode — without which the Jobs badge keeps reporting the
        creation-time probe's verdict and states the opposite of what happened
        (#414).
        """
        return self._downgraded

    async def ensure_target_prerequisites(self) -> None:
        """Store the shared reporter Organization once, after the wipe.

        DEQM requires MeasureReport.reporter 1..1 and every patient in the job
        references the same client-assigned Organization/lenny-reporter. Sending
        it inline per patient made concurrent batches upsert one id at once,
        which HAPI answered with ResourceVersionConflictException, failing 100%
        of patients; storing it once server-side and referencing it is the fix.

        This must run AFTER `_wipe_prior_run_data`, or the full-wipe branch
        deletes it and every submission then carries a dangling reporter
        reference -- which, because $submit-data is transaction-backed, fails
        that patient's whole submission.

        Raising here fails the job fast with one clear error rather than every
        patient failing later for the same reason.
        """
        try:
            await push_resources(
                [dict(LENNY_REPORTER_ORG)],
                target_url=self._mcs_url,
                auth_headers=self._mcs_auth_headers,
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to store reporter Organization '{LENNY_REPORTER_ORG['id']}' on the MCS "
                f"for job {self._job_id}: {exc}"
            ) from exc

    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        try:
            gather = await self._strategy.gather_patient_data(cdr_url, patient_id, cdr_auth_headers)
        except Exception as exc:
            raise TransferPhaseError("gather", exc) from exc

        # Filter once, and derive BOTH the MeasureReport (evaluatedResource)
        # and the submitted Parameters from the SAME filtered list. Without
        # this, an id-less resource is silently excluded from
        # evaluatedResource (build_data_exchange_measure_report has its own
        # filter) but still shipped as a `resource` parameter — the
        # MeasureReport and the payload disagree, and under HAPI's
        # transaction semantics one bad entry can 400 the whole patient.
        filtered_resources = [r for r in gather.resources if "resourceType" in r and "id" in r]
        measure_report = build_data_exchange_measure_report(
            job_id=self._job_id,
            patient_id=patient_id,
            measure_canonical=self._measure_canonical,
            period_start=self._period_start,
            period_end=self._period_end,
            resources=filtered_resources,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        # The reporter Organization is NOT re-sent here. build_submission_workflow
        # PUTs it to the MCS once per job, before any batch starts; every
        # patient's MeasureReport.reporter reference resolves against that
        # server-side copy. See the comment there for why inlining a copy of
        # the SAME client-assigned Organization/lenny-reporter into every
        # patient's payload is unsafe under concurrent batches.
        submitted = filtered_resources
        # The mode used for THIS attempt. self._mode is shared, mutable state:
        # one DeqmSubmitDataWorkflow is built per job (orchestrator.py) and its
        # transfer_patient() runs concurrently across patients under
        # asyncio.Semaphore(MAX_WORKERS) + asyncio.gather. The settlement
        # barrier below is what makes that safe — self._mode is only ever
        # written by the pioneer, while every other patient is still waiting on
        # self._mode_settled, so no patient can send one envelope while the job
        # is deciding on another (#414).
        attempt_mode = self._mode
        if attempt_mode == SUBMIT_DATA_MODE_STU5:
            parameters = build_stu5_parameters(measure_report, submitted)
        else:
            parameters = build_base_parameters(measure_report, submitted)

        # Non-pioneers wait for the mode verdict, then re-derive their payload
        # under it — a patient that queued while STU5 was still unsettled must
        # not send an STU5 envelope to a server that has since been downgraded.
        if not self._mode_settled.is_set():
            async with self._mode_lock:
                if not self._mode_settled.is_set():
                    try:
                        await self._settle_mode_and_submit(measure_report, submitted, patient_id)
                    finally:
                        self._mode_settled.set()
                    return gather
            # Settled by the pioneer while we queued on the lock; fall through
            # and submit under whatever it decided.
            attempt_mode = self._mode
            parameters = (
                build_stu5_parameters(measure_report, submitted)
                if attempt_mode == SUBMIT_DATA_MODE_STU5
                else build_base_parameters(measure_report, submitted)
            )

        # Settled path: no downgrade is available here, by design. Allowing one
        # would be exactly the mixed-mode job this barrier prohibits.
        try:
            await submit_data(
                mcs_url=self._mcs_url,
                parameters=parameters,
                mode=attempt_mode,
                measure_id=self._measure_id,
                auth_headers=self._mcs_auth_headers,
            )
        except Exception as exc:
            raise TransferPhaseError("submit", exc) from exc
        return gather

    async def _settle_mode_and_submit(self, measure_report, submitted, patient_id: str) -> None:
        """The pioneer's submission: the only one that may downgrade (#414).

        Runs under self._mode_lock with self._mode_settled unset, so it is the
        single point where the job's wire format is decided.
        """
        attempt_mode = self._mode
        parameters = build_stu5_parameters(measure_report, submitted)
        try:
            await submit_data(
                mcs_url=self._mcs_url,
                parameters=parameters,
                mode=attempt_mode,
                measure_id=self._measure_id,
                auth_headers=self._mcs_auth_headers,
            )
        except FhirOperationError as exc:
            # A mis-probed capability stamps Job.submit_data_mode="stu5" for a
            # server that doesn't actually implement $deqm-submit-data. Rather
            # than fail every patient in the job, downgrade to base mode and
            # retry once. Because this runs before the mode is settled, no
            # patient has been submitted under STU5 yet, so the downgrade
            # cannot strand anyone in the other format.
            #
            # A bare status is only trusted when it is a statement about the
            # server (_DOWNGRADE_STATUS_CODES). A 400 is ambiguous, so it
            # downgrades only when its OperationOutcome says the operation is
            # missing — otherwise it is a payload rejection and belongs to this
            # patient alone, with the server's explanation preserved (#414).
            capability_signal = exc.status_code in _DOWNGRADE_STATUS_CODES or (
                exc.status_code == 400 and _outcome_reports_unsupported_operation(exc)
            )
            if capability_signal:
                logger.warning(
                    "STU5 $deqm-submit-data rejected (HTTP %s) — downgrading job %s to base $submit-data",
                    exc.status_code,
                    self._job_id,
                    extra={"job_id": self._job_id, "patient_id": patient_id, "status_code": exc.status_code},
                )
                self._mode = SUBMIT_DATA_MODE_BASE
                # Read by the orchestrator to persist Job.submit_data_mode, so
                # the Jobs badge reports the mode actually used rather than the
                # probe's verdict.
                self._downgraded = True
                retry_parameters = build_base_parameters(measure_report, submitted)
                try:
                    await submit_data(
                        mcs_url=self._mcs_url,
                        parameters=retry_parameters,
                        mode=SUBMIT_DATA_MODE_BASE,
                        measure_id=self._measure_id,
                        auth_headers=self._mcs_auth_headers,
                    )
                except Exception as retry_exc:
                    raise TransferPhaseError("submit", retry_exc) from retry_exc
            else:
                raise TransferPhaseError("submit", exc) from exc
        except Exception as exc:
            raise TransferPhaseError("submit", exc) from exc


async def build_submission_workflow(
    *,
    workflow: str,
    job_id: int,
    measure_id: str,
    mcs_url: str,
    mcs_auth_headers: dict[str, str] | None,
    submit_data_mode: str | None,
    period_start: str,
    period_end: str,
) -> SubmissionWorkflow:
    """Build the job's workflow. For DEQM, fetches the measure canonical from
    the MCS — raising (job fails fast) when the Measure can't be read, or when
    the resolved mode is STU5 and the canonical isn't an absolute URL."""
    if workflow == "deqm_submit_data":
        canonical = await get_measure_canonical(measure_id, mcs_url=mcs_url, auth_headers=mcs_auth_headers or {})
        resolved_mode = submit_data_mode or SUBMIT_DATA_MODE_BASE
        if resolved_mode == SUBMIT_DATA_MODE_STU5 and not canonical.startswith("http"):
            # In STU5 mode the type-level POST makes MeasureReport.measure the
            # ONLY identifier for the submitted measure — a relative reference
            # (degraded from a Measure with no `url`) is not a resolvable
            # canonical there. Fail fast at job build rather than emitting an
            # unattributable submission. Base-fallback mode is unaffected: the
            # measure is already named in the instance-level submit URL.
            raise ValueError(
                f"Measure '{measure_id}' has no absolute canonical URL (got {canonical!r}), "
                "which is required for DEQM STU5 $deqm-submit-data submissions."
            )
        # DEQM STU5 says a submission's references should resolve WITHIN the
        # submission, which is why the reporter Organization used to travel
        # inline in every patient's $submit-data payload. In practice that
        # inline copy is the SAME client-assigned Organization/lenny-reporter
        # on every patient in the job, and batches run concurrently
        # (asyncio.Semaphore(settings.MAX_WORKERS) in orchestrator.py) — HAPI
        # saw up to MAX_WORKERS transactions try to upsert that one resource
        # id at the same time and answered every one of them with
        # ResourceVersionConflictException (HAPI-0550/HAPI-0823), failing
        # 100% of patients on the real stack (two 319-patient runs both
        # status=failed, processed=0). Store the reporter ONCE per job here,
        # before any batch starts, and let each patient's
        # MeasureReport.reporter reference resolve against this server-side
        # copy instead. Tradeoff: a strict-STU5 receiver that refuses to
        # resolve references outside the submitted payload would need the
        # inline per-patient copy back — and would then also need
        # submissions serialized (not concurrent) to avoid reintroducing
        # this same version conflict. Let failure here raise: it fails the
        # job fast with one clear error instead of every patient failing
        # later for the same reason.
        # The reporter push itself now lives in
        # DeqmSubmitDataWorkflow.ensure_target_prerequisites(), which the
        # orchestrator calls AFTER the wipe. Pushing it here meant any job with
        # mcs_wipe_before_job set deleted it again immediately, because the full
        # wipe removes Organization.
        return DeqmSubmitDataWorkflow(
            job_id=job_id,
            measure_id=measure_id,
            mcs_url=mcs_url,
            mcs_auth_headers=mcs_auth_headers,
            measure_canonical=canonical,
            period_start=period_start,
            period_end=period_end,
            mode=resolved_mode,
        )
    return DirectLoadWorkflow(measure_id, mcs_url, mcs_auth_headers)

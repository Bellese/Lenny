"""Per-job data submission workflows (spec: 2026-08-21-deqm-submit-data-workflow).

A SubmissionWorkflow owns phase 1 of a job: gather each subject from the CDR
and deliver it to the MCS, one subject at a time or, where the wire format
supports it, in a group. The orchestrator picks the concrete class from
Job.workflow and calls prepare_patient() then submit_prepared() for each
group; transfer_patient() is the per-subject operation both build on. Phase 2
($evaluate-measure) is identical for every workflow and stays in the
orchestrator.
"""

import abc
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings
from app.services.deqm import (
    LENNY_REPORTER_ORG,
    SubjectBundle,
    build_base_parameters,
    build_data_exchange_measure_report,
    build_stu5_parameters,
    dedupe_by_identity,
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

# Capability-mismatch signals: a server whose CapabilityStatement/
# OperationDefinition probe confirmed the type-level $submit-data bundle
# contract, but whose type-level POST doesn't actually work, commonly
# answers with one of these. Each is a statement about the SERVER, not
# about the payload, so it is a credible capability verdict on its own.
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


# Statuses on which a failed GROUP submission is resubmitted subject by subject.
# Each says something about the PAYLOAD, so one subject's bad resource is the
# plausible cause and isolation finds the owner.
#
# 409 is here because HAPI's ResourceVersionConflictException (HAPI-0550/0823)
# is a per-resource verdict, and it has already failed this workflow once.
#
# Everything else is deliberately absent. A 401, 403, 404, 405, 429, any 5xx, a
# timeout, or a transport error is a statement about the SERVER or the
# connection; resubmitting N times only asks a down server the same question N
# more times. With a chunk of 100 that turns one failed POST into 101. Those
# fail every subject in the group with the one verdict the server gave.
_ISOLATE_STATUS_CODES = {400, 409, 422}


def _is_payload_attributable(exc: Exception) -> bool:
    """True when a group's failure plausibly belongs to ONE subject's payload."""
    return isinstance(exc, FhirOperationError) and exc.status_code in _ISOLATE_STATUS_CODES


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


@dataclass(frozen=True)
class PreparedSubject:
    """One subject's CDR work, done and ready to submit — no MCS I/O yet.

    Splitting gather-and-build from submit is what lets several subjects share
    one POST. A workflow with no separable build step leaves the payload fields
    None; the base-class default then defers the whole transfer to
    submit_prepared, which is why the CDR coordinates travel here too. Keeping
    them on the subject rather than on the workflow instance is deliberate: one
    workflow instance serves every concurrent chunk of a job, so per-subject
    state on the instance would interleave across chunks.
    """

    patient_id: str
    gather: GatherResult | None = None
    measure_report: dict | None = None
    resources: list[dict] | None = None
    cdr_url: str | None = None
    cdr_auth_headers: dict[str, str] | None = None


@dataclass(frozen=True)
class SubjectOutcome:
    """What happened to one subject of a submitted group.

    `error` is None on success. The orchestrator reads `gather` for its
    partial-failure bookkeeping and the "Gathered N resources" log, exactly as
    it read transfer_patient's return value before grouping.
    """

    patient_id: str
    gather: GatherResult | None = None
    error: TransferPhaseError | None = None


@dataclass(frozen=True)
class _Settlement:
    """What the pioneer's single POST decided, so the re-sends can happen
    outside the mode lock (finding M6).

    `action` is one of:
      "done"         — the group POST succeeded; every subject is good.
      "resend-base"  — a capability signal; the job downgraded and the group
                       must be re-sent one subject at a time in base mode.
      "isolate-stu5" — a payload rejection on a group of more than one; re-send
                       each subject alone under the settled STU5 mode.
      "fail"         — one verdict for the whole group; `error` carries it.
    """

    action: str
    error: Exception | None = None


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
    """Gathers each subject's data from the CDR and delivers it to the MCS,
    one subject at a time or, where the wire format supports it, in a group.
    """

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

    @property
    def submission_group_size(self) -> int:
        """How many subjects may share one submission call.

        1 means one subject per call — today's behavior, and the default for
        every workflow that has no multi-subject wire format.
        """
        return 1

    async def prepare_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> PreparedSubject:
        """Do this subject's CDR work, with no MCS I/O. Raises TransferPhaseError.

        Default: defer everything. direct_load pushes a Bundle of PUTs and has
        nothing to assemble beforehand, so it returns an identity-only subject
        and lets submit_prepared run the whole transfer. Raising here (rather
        than returning a failed outcome) is what keeps a gather failure from
        poisoning the group the subject was being collected into.
        """
        return PreparedSubject(patient_id=patient_id, cdr_url=cdr_url, cdr_auth_headers=cdr_auth_headers)

    async def submit_prepared(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """Submit a group; return one outcome per subject, never raising for one.

        Default: fan back out to transfer_patient, one subject per call. This is
        what keeps DirectLoadWorkflow — and any workflow implementing only
        transfer_patient — working unchanged under the group protocol, with no
        edits of its own.
        """
        outcomes: list[SubjectOutcome] = []
        for subject in subjects:
            try:
                gather = await self.transfer_patient(
                    subject.cdr_url or "",
                    subject.patient_id,
                    subject.cdr_auth_headers or {},
                )
            except TransferPhaseError as exc:
                outcomes.append(SubjectOutcome(patient_id=subject.patient_id, error=exc))
            except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is this method's contract
                # "gather" is the historical label for an unclassified transfer
                # failure (see TransferPhaseError), so an unwrapped exception
                # lands in the same bucket the orchestrator already handles.
                outcomes.append(SubjectOutcome(patient_id=subject.patient_id, error=TransferPhaseError("gather", exc)))
            else:
                outcomes.append(SubjectOutcome(patient_id=subject.patient_id, gather=gather))
        return outcomes

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
        group_size: int = 1,
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
        # Instead the mode is SETTLED ONCE, behind a barrier: the first group
        # to reach the submit step under STU5 becomes the pioneer and is the
        # only one allowed to downgrade. Everyone else waits for its verdict
        # and then submits under the settled mode, with no downgrade path of
        # their own. One group's submission is therefore serialized; the rest
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
        # How many subjects may share one STU5 POST. PR 2 never sets this above
        # 1 in production — build_submission_workflow does not pass it — so the
        # grouping mechanics land under test before anything can select them.
        # PR 3 threads the operator's clamped value into this same argument.
        # max(1, ...) guards against a 0 leaking through: "0 means unlimited"
        # is resolved to a real number at job creation, never here.
        self._group_size = max(1, group_size)

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

    @property
    def submission_group_size(self) -> int:
        """Subjects per submission, read fresh by the orchestrator each group.

        Collapses to 1 outside STU5: base-fallback has no multi-bundle form, so
        a runtime downgrade must return the job to one subject per POST for the
        remainder of the chunk.
        """
        return self._group_size if self._mode == SUBMIT_DATA_MODE_STU5 else 1

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

    async def prepare_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> PreparedSubject:
        """Gather, filter, deduplicate, and build the MeasureReport. No MCS I/O."""
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
        # The predicate matches build_data_exchange_measure_report's own filter
        # exactly — truthiness, not key presence — so a resource carrying an
        # empty id is dropped from BOTH the Bundle entries and
        # evaluatedResource, rather than shipping as an entry nothing refers to.
        filtered_resources = [r for r in gather.resources if r.get("resourceType") and r.get("id")]
        # Dedupe BEFORE the MeasureReport is built, so evaluatedResource and the
        # Bundle entries stay 1:1 by construction rather than by a second rule
        # maintained somewhere else. Within this subject only — see
        # dedupe_by_identity on why a shared Practitioner must survive in every
        # subject's Bundle.
        filtered_resources, conflicts = dedupe_by_identity(filtered_resources)
        if conflicts:
            logger.warning(
                "Conflicting representations of the same resource identity in gathered data "
                "— keeping the first of each",
                extra={
                    "job_id": self._job_id,
                    "patient_id": patient_id,
                    "identities": conflicts[:10],
                    "conflict_count": len(conflicts),
                },
            )
        measure_report = build_data_exchange_measure_report(
            job_id=self._job_id,
            patient_id=patient_id,
            measure_canonical=self._measure_canonical,
            period_start=self._period_start,
            period_end=self._period_end,
            resources=filtered_resources,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        # The reporter Organization is NOT re-sent here. ensure_target_prerequisites
        # PUTs it to the MCS once per job, before any batch starts; every
        # patient's MeasureReport.reporter reference resolves against that
        # server-side copy. Inlining a copy of the SAME client-assigned
        # Organization/lenny-reporter into every patient's payload is unsafe
        # under concurrent batches — see that method.
        return PreparedSubject(
            patient_id=patient_id,
            gather=gather,
            measure_report=measure_report,
            resources=filtered_resources,
        )

    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        """One subject, end to end. Raises, where submit_prepared returns outcomes.

        Kept as the single-subject entry point: it is the honest convenience
        for callers with exactly one patient, and the base class's default
        submit_prepared is defined in terms of it.
        """
        subject = await self.prepare_patient(cdr_url, patient_id, cdr_auth_headers)
        outcome = (await self.submit_prepared([subject]))[0]
        if outcome.error is not None:
            raise outcome.error
        return subject.gather  # type: ignore[return-value]

    async def _post(self, parameters: dict, mode: str) -> None:
        """The one place this workflow talks to the MCS."""
        await submit_data(
            mcs_url=self._mcs_url,
            parameters=parameters,
            mode=mode,
            measure_id=self._measure_id,
            auth_headers=self._mcs_auth_headers,
        )

    def _parameters_for(self, subject: PreparedSubject, mode: str) -> dict:
        if mode == SUBMIT_DATA_MODE_STU5:
            return build_stu5_parameters([SubjectBundle(subject.measure_report, subject.resources)])
        return build_base_parameters(subject.measure_report, subject.resources)

    async def _submit_individually(self, subjects: list[PreparedSubject], mode: str) -> list[SubjectOutcome]:
        """One POST per subject under `mode`, each failing on its own."""
        outcomes: list[SubjectOutcome] = []
        for subject in subjects:
            try:
                await self._post(self._parameters_for(subject, mode), mode)
            except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is the contract
                outcomes.append(
                    SubjectOutcome(
                        patient_id=subject.patient_id,
                        gather=subject.gather,
                        error=TransferPhaseError("submit", exc),
                    )
                )
            else:
                outcomes.append(SubjectOutcome(patient_id=subject.patient_id, gather=subject.gather))
        return outcomes

    async def submit_prepared(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """Submit a prepared group, returning one outcome per subject.

        The mode is SETTLED ONCE behind a barrier (#414): the first group to
        reach the submit step under STU5 becomes the pioneer and is the only one
        allowed to downgrade. Everyone else waits for its verdict and then
        submits under the settled mode, with no downgrade path of their own.
        """
        if not subjects:
            return []
        if not self._mode_settled.is_set():
            settlement: _Settlement | None = None
            async with self._mode_lock:
                if not self._mode_settled.is_set():
                    try:
                        settlement = await self._settle_mode(subjects)
                    finally:
                        # In a finally: a pioneer group that fails outright must
                        # still release every group waiting on its verdict.
                        self._mode_settled.set()
            # Deliberately outside the `async with`: the re-sends are N
            # sequential POSTs, and holding the barrier across them would stall
            # every other chunk for the whole group (finding M6). The mode is
            # already decided and published at this point, so nothing a waiter
            # does can race it.
            if settlement is not None:
                return await self._apply_settlement(settlement, subjects)
            # Settled by the pioneer while we queued on the lock; fall through
            # and submit under whatever it decided.
        # Re-read the settled mode rather than trusting the size this group was
        # formed at: another chunk's pioneer may have downgraded in between, and
        # base-fallback has no multi-bundle envelope.
        if self._mode != SUBMIT_DATA_MODE_STU5:
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_BASE)
        return await self._submit_group(subjects)

    async def _submit_group(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """One POST carrying every subject's bundle; isolate only on a payload
        rejection.

        Each `bundle` parameter is a collection Bundle for exactly ONE subject:
        the receiver processes each Bundle as a transaction, so merging subjects
        would make one subject's bad resource fail the others.
        """
        parameters = build_stu5_parameters([SubjectBundle(s.measure_report, s.resources) for s in subjects])
        try:
            await self._post(parameters, SUBMIT_DATA_MODE_STU5)
        except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is the contract
            # A single-subject group never isolates: retrying the one subject it
            # holds would POST twice where the ungrouped path posts once, and
            # size 1 would stop being byte-identical to PR 1.
            if len(subjects) > 1 and _is_payload_attributable(exc):
                return await self._submit_individually(subjects, SUBMIT_DATA_MODE_STU5)
            return [
                SubjectOutcome(
                    patient_id=s.patient_id,
                    gather=s.gather,
                    error=TransferPhaseError("submit", exc),
                )
                for s in subjects
            ]
        return [SubjectOutcome(patient_id=s.patient_id, gather=s.gather) for s in subjects]

    async def _settle_mode(self, subjects: list[PreparedSubject]) -> _Settlement:
        """Decide the job's wire format with ONE POST. Runs under
        self._mode_lock with self._mode_settled unset, so it is the single
        point where the mode is decided (#414).

        Returns the decision rather than acting on it: the follow-up re-sends
        are N sequential POSTs, and holding the barrier across them would stall
        every other chunk for the whole group (finding M6). Writes to self._mode
        and self._downgraded happen HERE, inside the lock and before the caller
        sets the event, so a waiter can never observe a half-decided mode.
        """
        parameters = build_stu5_parameters([SubjectBundle(s.measure_report, s.resources) for s in subjects])
        try:
            await self._post(parameters, SUBMIT_DATA_MODE_STU5)
        except FhirOperationError as exc:
            # A mis-probed capability stamps Job.submit_data_mode="stu5" for a
            # server that doesn't actually implement the type-level $submit-data
            # bundle contract. Rather than fail every patient in the job,
            # downgrade to base mode and re-send this group individually.
            # Because this runs before the mode is settled, nobody has been
            # submitted under STU5 yet, so the downgrade cannot strand anyone in
            # the other format.
            #
            # A bare status is only trusted when it is a statement about the
            # server (_DOWNGRADE_STATUS_CODES). A 400 is ambiguous, so it
            # downgrades only when its OperationOutcome says the operation is
            # missing — otherwise it is a payload rejection and belongs to the
            # subjects, with the server's explanation preserved (#414).
            capability_signal = exc.status_code in _DOWNGRADE_STATUS_CODES or (
                exc.status_code == 400 and _outcome_reports_unsupported_operation(exc)
            )
            if capability_signal:
                logger.warning(
                    "STU5 $submit-data rejected (HTTP %s) — downgrading job %s to base $submit-data",
                    exc.status_code,
                    self._job_id,
                    extra={
                        "job_id": self._job_id,
                        "patient_id": subjects[0].patient_id,
                        "subject_count": len(subjects),
                        "status_code": exc.status_code,
                    },
                )
                self._mode = SUBMIT_DATA_MODE_BASE
                # Read by the orchestrator to persist Job.submit_data_mode, so
                # the Jobs badge reports the mode actually used rather than the
                # probe's verdict.
                self._downgraded = True
                # Capability first, isolation second, never both: base-fallback
                # has no multi-bundle form, so this is a re-send, not a retry.
                return _Settlement("resend-base")
            if len(subjects) > 1 and _is_payload_attributable(exc):
                return _Settlement("isolate-stu5")
            return _Settlement("fail", exc)
        except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is the contract
            return _Settlement("fail", exc)
        return _Settlement("done")

    async def _apply_settlement(self, settlement: _Settlement, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """Carry out what _settle_mode decided, OUTSIDE the mode lock.

        Every branch returns exactly one outcome per subject — losing one here
        would silently drop a patient from the job's counters.
        """
        if settlement.action == "resend-base":
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_BASE)
        if settlement.action == "isolate-stu5":
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_STU5)
        if settlement.action == "fail":
            return [
                SubjectOutcome(
                    patient_id=s.patient_id,
                    gather=s.gather,
                    error=TransferPhaseError("submit", settlement.error),
                )
                for s in subjects
            ]
        return [SubjectOutcome(patient_id=s.patient_id, gather=s.gather) for s in subjects]


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
    bundles_per_submission: int | None = None,
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
                "which is required for DEQM STU5 $submit-data submissions."
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
            # None is a row created before #413 PR 3 added the column; those
            # jobs submitted one subject per POST, so that is what None means.
            group_size=bundles_per_submission or 1,
        )
    return DirectLoadWorkflow(measure_id, mcs_url, mcs_auth_headers)

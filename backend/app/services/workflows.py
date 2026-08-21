"""Per-job data submission workflows (spec: 2026-08-21-deqm-submit-data-workflow).

A SubmissionWorkflow owns phase 1 of a job for one patient: gather from the
CDR, deliver to the MCS. The orchestrator picks the concrete class from
Job.workflow and calls transfer_patient(); phase 2 ($evaluate-measure) is
identical for every workflow and stays in the orchestrator.
"""

import abc
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

_DOWNGRADE_STATUS_CODES = {400, 404}

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

    async def transfer_patient(self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> GatherResult:
        try:
            gather = await self._strategy.gather_patient_data(cdr_url, patient_id, cdr_auth_headers)
        except Exception as exc:
            raise TransferPhaseError("gather", exc) from exc

        measure_report = build_data_exchange_measure_report(
            job_id=self._job_id,
            patient_id=patient_id,
            measure_canonical=self._measure_canonical,
            period_start=self._period_start,
            period_end=self._period_end,
            resources=gather.resources,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        submitted = [dict(LENNY_REPORTER_ORG)] + gather.resources
        if self._mode == SUBMIT_DATA_MODE_STU5:
            parameters = build_stu5_parameters(measure_report, submitted)
        else:
            parameters = build_base_parameters(measure_report, submitted)

        try:
            await submit_data(
                mcs_url=self._mcs_url,
                parameters=parameters,
                mode=self._mode,
                measure_id=self._measure_id,
                auth_headers=self._mcs_auth_headers,
            )
        except FhirOperationError as exc:
            # A mis-probed capability stamps Job.submit_data_mode="stu5" for a
            # server that doesn't actually implement $deqm-submit-data. Rather
            # than fail every patient in the job, downgrade to base mode on the
            # first 400/404 and retry once. Job.submit_data_mode still shows
            # the probe's original verdict — reconciling the UI badge with a
            # runtime downgrade is deliberately out of scope here.
            if self._mode == SUBMIT_DATA_MODE_STU5 and exc.status_code in _DOWNGRADE_STATUS_CODES:
                logger.warning(
                    "STU5 $deqm-submit-data rejected (HTTP %s) — downgrading job %s to base $submit-data",
                    exc.status_code,
                    self._job_id,
                    extra={"job_id": self._job_id, "patient_id": patient_id, "status_code": exc.status_code},
                )
                self._mode = SUBMIT_DATA_MODE_BASE
                retry_parameters = build_base_parameters(measure_report, submitted)
                try:
                    await submit_data(
                        mcs_url=self._mcs_url,
                        parameters=retry_parameters,
                        mode=self._mode,
                        measure_id=self._measure_id,
                        auth_headers=self._mcs_auth_headers,
                    )
                except Exception as retry_exc:
                    raise TransferPhaseError("submit", retry_exc) from retry_exc
            else:
                raise TransferPhaseError("submit", exc) from exc
        except Exception as exc:
            raise TransferPhaseError("submit", exc) from exc
        return gather


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
    the MCS — raising (job fails fast) when the Measure can't be read."""
    if workflow == "deqm_submit_data":
        canonical = await get_measure_canonical(measure_id, mcs_url=mcs_url, auth_headers=mcs_auth_headers or {})
        return DeqmSubmitDataWorkflow(
            job_id=job_id,
            measure_id=measure_id,
            mcs_url=mcs_url,
            mcs_auth_headers=mcs_auth_headers,
            measure_canonical=canonical,
            period_start=period_start,
            period_end=period_end,
            mode=submit_data_mode or SUBMIT_DATA_MODE_BASE,
        )
    return DirectLoadWorkflow(measure_id, mcs_url, mcs_auth_headers)

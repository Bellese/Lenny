# DEQM $submit-data Grouping Implementation Plan (#413 PR 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the mechanics that let one `$submit-data` POST carry N subjects' bundles — the group protocol, per-chunk buffer, failure isolation, and the downgrade interaction — while production stays at exactly one subject per POST.

**Architecture:** `SubmissionWorkflow` gains a two-phase group protocol (`prepare_patient` → `submit_prepared`) whose base-class defaults delegate to today's `transfer_patient`, so every existing workflow keeps working unchanged and a group of one is the degenerate case rather than a second code path. The orchestrator's Phase 1 walks its chunk in groups, reading the group size fresh before each one. `DeqmSubmitDataWorkflow` overrides the pair: `prepare_patient` does gather + dedupe + MeasureReport build with no MCS I/O, and `submit_prepared` issues one POST per group, isolating only failures attributable to a payload.

**Tech Stack:** Python 3.10+, asyncio, pytest (`asyncio_mode = auto`), SQLAlchemy async, ruff.

**Spec:** `docs/superpowers/specs/2026-09-14-deqm-submit-data-contract-design.md` — §§ *Submission grouping*, *Failure isolation*, *Testing*, and the **PR 2 — Grouping** acceptance criteria. Read those four before Task 1.

**Issue:** #413 (PR 2 of 3). PR 1 merged as `e11415d`.

## Global Constraints

- **Python 3.10+.** Use `X | None`, never `Optional[X]`. Type hints are required on new functions.
- **Behavior-neutral in production.** `build_submission_workflow` must NOT pass `group_size`. If `git diff` shows production selecting a group size above 1, the PR is wrong.
- **No DB column, no API field, no frontend.** Those are PR 3. A migration or a `JobsPage.js` edit in this PR is out of scope.
- **At group size 1 the wire behavior is byte-identical to PR 1** — one POST per subject, and on failure exactly one POST, not two.
- **`_mode_settled.set()` stays in a `finally`.** A pioneer group that fails outright must still release every waiter (#414).
- **Isolation never downgrades.** Only `_settle_mode_and_submit` may change `self._mode`.
- **These regressions must stay green untouched:** #414 single-mode settlement (`test_services_workflows.py`, `test_services_orchestrator.py:940`) and #415 OperationOutcome-inside-HTTP-200 rejection.
- **Lint:** `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` must be clean before every commit.
- **Commits:** conventional (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`). Commit at the end of each task.
- **Worktree:** `/Users/bill/dev/bellese/lenny-413-grouping`, branch `fix/413-grouping`. Never commit on `main`.

## File Structure

| File | Responsibility in this PR |
|---|---|
| `backend/app/services/workflows.py` | The group protocol (two dataclasses + three members on the base class), the isolation taxonomy, and `DeqmSubmitDataWorkflow`'s override of the pair. All new logic lands here. |
| `backend/app/services/orchestrator.py` | Phase 1 walks groups instead of patients; the per-patient failure-persistence block becomes one helper reached from both halves of the protocol. |
| `backend/tests/test_services_workflows.py` | Protocol defaults, grouping, isolation, downgrade interaction, the carried dedupe findings. |
| `backend/tests/test_services_orchestrator.py` | The group loop, stop-mid-group, and that per-patient accounting is unchanged. |
| `docs/architecture.md` | The DEQM workflow description gains the grouping mechanics. |

Nothing else is touched. `deqm.py` already takes a list (`build_stu5_parameters(subjects)`) and needs no change — that was PR 1's job.

---

### Task 1: The group protocol on `SubmissionWorkflow`

Two dataclasses and three members, with defaults that make every existing workflow a correct group-of-one participant without editing it.

**Files:**
- Modify: `backend/app/services/workflows.py` (add after `TransferPhaseError`, ~line 102; extend `SubmissionWorkflow` at ~line 118)
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Consumes: `GatherResult` from `app.services.fhir_client` (already imported), `TransferPhaseError` (already defined in this file).
- Produces:
  - `PreparedSubject(patient_id: str, gather: GatherResult | None = None, measure_report: dict | None = None, resources: list[dict] | None = None, cdr_url: str | None = None, cdr_auth_headers: dict[str, str] | None = None)` — frozen dataclass
  - `SubjectOutcome(patient_id: str, gather: GatherResult | None = None, error: TransferPhaseError | None = None)` — frozen dataclass
  - `SubmissionWorkflow.submission_group_size -> int` (property, default `1`)
  - `SubmissionWorkflow.prepare_patient(cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]) -> PreparedSubject`
  - `SubmissionWorkflow.submit_prepared(subjects: list[PreparedSubject]) -> list[SubjectOutcome]`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_workflows.py`:

```python
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
        assert subject.patient_id == "p1"
        assert subject.cdr_url == "http://cdr"
        assert subject.cdr_auth_headers == {"Authorization": "Bearer t"}

    async def test_default_submit_delegates_to_transfer_patient(self):
        wf = self._OnlyTransferPatient(_GATHER)
        subject = await wf.prepare_patient("http://cdr", "p1", {})
        outcomes = await wf.submit_prepared([subject])
        assert wf.calls == [("http://cdr", "p1", {})]
        assert len(outcomes) == 1
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
```

Add `PreparedSubject`, `SubjectOutcome`, and `SubmissionWorkflow` to the `from app.services.workflows import (...)` block at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestGroupProtocolDefaults -v`
Expected: FAIL — `ImportError: cannot import name 'PreparedSubject'`.

- [ ] **Step 3: Implement the protocol**

In `backend/app/services/workflows.py`, add `from dataclasses import dataclass` to the imports, then insert after the `TransferPhaseError` class:

```python
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
```

Then add to `SubmissionWorkflow`, after `ensure_target_prerequisites`:

```python
    @property
    def submission_group_size(self) -> int:
        """How many subjects may share one submission call.

        1 means one subject per call — today's behavior, and the default for
        every workflow that has no multi-subject wire format.
        """
        return 1

    async def prepare_patient(
        self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]
    ) -> PreparedSubject:
        """Do this subject's CDR work, with no MCS I/O. Raises TransferPhaseError.

        Default: defer everything. direct_load pushes a Bundle of PUTs and has
        nothing to assemble beforehand, so it returns an identity-only subject
        and lets submit_prepared run the whole transfer. Raising here (rather
        than returning a failed outcome) is what keeps a gather failure from
        poisoning the group the subject was being collected into.
        """
        return PreparedSubject(
            patient_id=patient_id, cdr_url=cdr_url, cdr_auth_headers=cdr_auth_headers
        )

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
                outcomes.append(
                    SubjectOutcome(
                        patient_id=subject.patient_id, error=TransferPhaseError("gather", exc)
                    )
                )
            else:
                outcomes.append(SubjectOutcome(patient_id=subject.patient_id, gather=gather))
        return outcomes
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "feat: add the group submission protocol to SubmissionWorkflow

prepare_patient/submit_prepared split one subject's CDR work from its
delivery so several subjects can share one POST. The base-class defaults
delegate to transfer_patient, so every existing workflow is a correct
group-of-one participant without an edit of its own.

Refs #413"
```

---

### Task 2: Extract the per-patient failure persistence (pure refactor)

Grouping needs this block reachable from two places — a `prepare_patient` raise and a failed `SubjectOutcome`. Extract it first, with the existing loop still calling it, so a reviewer can confirm the move is behavior-preserving before any control flow changes.

**Files:**
- Modify: `backend/app/services/orchestrator.py:608-686` (the `except Exception as transfer_exc:` block inside Phase 1)
- Test: `backend/tests/test_services_orchestrator.py` (existing tests are the gate; no new ones)

**Interfaces:**
- Produces: `_record_transfer_failure(*, job_id: int, batch_id: int, patient_id: str, patient_map: dict[str, dict[str, Any]], exc: Exception) -> bool` — module-level in `orchestrator.py`. Returns `False` when the job was stopped before the row could be written, in which case the caller must `return` without incrementing `failed`.

- [ ] **Step 1: Add the helper**

Insert above `_process_single_batch` in `backend/app/services/orchestrator.py`:

```python
async def _record_transfer_failure(
    *,
    job_id: int,
    batch_id: int,
    patient_id: str,
    patient_map: dict[str, dict[str, Any]],
    exc: Exception,
) -> bool:
    """Persist one patient's Phase-1 transfer failure as a MeasureResult row.

    Extracted so a gather failure (raised by prepare_patient) and a submit
    failure (returned as a SubjectOutcome) reach the database by exactly the
    same path — with grouping, those are two different call sites for what must
    remain one behavior.

    Returns False when the job was stopped before the row was written; the
    caller returns without counting the failure, which is what the inline
    version did.
    """
    if isinstance(exc, TransferPhaseError):
        error_phase = exc.phase
        push_exc: Exception = exc.cause
    else:
        error_phase = "gather"
        push_exc = exc
    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))
    sanitized_msg = sanitize_error(push_exc)
    error_details: dict[str, Any] = {"operation": error_phase, "error": sanitized_msg}
    if isinstance(push_exc, FhirOperationError):
        error_details["url"] = push_exc.url
        error_details["status_code"] = push_exc.status_code
        error_details["latency_ms"] = push_exc.latency_ms
        if push_exc.outcome:
            error_details["raw_outcome"] = redact_outcome(push_exc.outcome.raw)
    error_report = _error_measure_report(
        patient_id,
        push_exc,
        push_exc.outcome.raw if isinstance(push_exc, FhirOperationError) and push_exc.outcome else None,
    )
    populations = {
        "initial_population": False,
        "denominator": False,
        "numerator": False,
        "denominator_exclusion": False,
        "numerator_exclusion": False,
        "error": True,
        "error_message": sanitized_msg,
        "error_phase": error_phase,
    }
    logger.warning(
        "Failed to transfer patient data",
        extra={
            "job_id": job_id,
            "batch_id": batch_id,
            "patient_id": patient_id,
            "error": sanitized_msg,
            "error_phase": error_phase,
        },
    )
    if await _stop_or_delete_job(job_id):
        return False
    async with async_session() as session:
        existing_row = (
            await session.execute(
                select(MeasureResult).where(
                    MeasureResult.job_id == job_id,
                    MeasureResult.patient_id == patient_id,
                )
            )
        ).scalar_one_or_none()
        if existing_row:
            existing_row.measure_report = error_report
            existing_row.populations = populations
            existing_row.error_details = error_details
            existing_row.error_phase = error_phase
        else:
            session.add(
                MeasureResult(
                    job_id=job_id,
                    patient_id=patient_id,
                    patient_name=patient_name,
                    measure_report=error_report,
                    populations=populations,
                    error_details=error_details,
                    error_phase=error_phase,
                )
            )
        await session.commit()
    return True
```

- [ ] **Step 2: Replace the inline block with a call**

In `_process_single_batch`, the entire `except Exception as transfer_exc:` body (everything from `if isinstance(transfer_exc, TransferPhaseError):` through `failed += 1`) becomes:

```python
                except Exception as transfer_exc:
                    gather_failed_patients.add(patient_id)
                    if not await _record_transfer_failure(
                        job_id=job_id,
                        batch_id=batch_id,
                        patient_id=patient_id,
                        patient_map=patient_map,
                        exc=transfer_exc,
                    ):
                        return
                    failed += 1
```

- [ ] **Step 3: Run the orchestrator suite to verify nothing moved**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -v`
Expected: PASS, every test, with no test edits. A failure here means the extraction changed behavior — fix the helper, do not edit the tests.

- [ ] **Step 4: Run the full unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/orchestrator.py
git commit -m "refactor: extract per-patient transfer-failure persistence

Grouping needs this block reachable from a prepare_patient raise and from a
failed SubjectOutcome. Extracting it first, with the existing loop still
calling it, keeps the move reviewable as a no-op.

Refs #413"
```

---

### Task 3: Walk the chunk in groups

Phase 1 becomes group-outer / subject-inner, driving the protocol from Task 1. Group size is still 1 everywhere (no workflow overrides it yet), so this task is observably a no-op except for the stop-mid-group rule.

**Files:**
- Modify: `backend/app/services/orchestrator.py:34` (import), `:574-686` (the Phase 1 loop)
- Test: `backend/tests/test_services_orchestrator.py`

**Interfaces:**
- Consumes: `PreparedSubject`, `SubjectOutcome`, `SubmissionWorkflow.submission_group_size`, `.prepare_patient`, `.submit_prepared` from Task 1; `_record_transfer_failure` from Task 2.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_orchestrator.py`:

```python
class _RecordingGroupWorkflow(SubmissionWorkflow):
    """A workflow that records the exact group shapes it was handed."""

    name = "recording"

    def __init__(self, group_size: int = 1, fail: set[str] | None = None):
        self._group_size = group_size
        self._fail = fail or set()
        self.groups: list[list[str]] = []
        self.prepared: list[str] = []

    async def transfer_patient(self, cdr_url, patient_id, cdr_auth_headers):
        # SubmissionWorkflow marks this abstract, so the stub must define it.
        # Asserting rather than merely satisfying the ABC turns a silent
        # fallback to the per-patient path into a loud failure.
        raise AssertionError("the group path must not fall back to transfer_patient")

    @property
    def submission_group_size(self) -> int:
        return self._group_size

    async def prepare_patient(self, cdr_url, patient_id, cdr_auth_headers):
        self.prepared.append(patient_id)
        return PreparedSubject(
            patient_id=patient_id,
            gather=GatherResult(resources=[{"resourceType": "Patient", "id": patient_id}]),
        )

    async def submit_prepared(self, subjects):
        self.groups.append([s.patient_id for s in subjects])
        return [
            SubjectOutcome(
                patient_id=s.patient_id,
                gather=s.gather,
                error=(
                    TransferPhaseError("submit", RuntimeError("bad"))
                    if s.patient_id in self._fail
                    else None
                ),
            )
            for s in subjects
        ]


async def test_phase_one_walks_the_chunk_in_groups(test_session, session_factory):
    """The orchestrator must hand submit_prepared groups of
    submission_group_size, not one subject at a time."""
    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
        workflow="deqm_submit_data",
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)
    patients = ["p1", "p2", "p3", "p4", "p5"]
    batch = Batch(job_id=job.id, batch_number=1, patient_ids=patients, status=BatchStatus.pending)
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    workflow = _RecordingGroupWorkflow(group_size=2)
    with (
        _make_session_factory_patch(session_factory),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={"resourceType": "MeasureReport", "status": "complete", "group": []},
        ),
    ):
        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map={p: {"resourceType": "Patient", "id": p} for p in patients},
            cdr_url="http://cdr/fhir",
            auth_headers={},
            mcs_url="http://mcs/fhir",
            workflow=workflow,
        )

    assert workflow.groups == [["p1", "p2"], ["p3", "p4"], ["p5"]], (
        "the trailing partial group must still be submitted"
    )
    assert workflow.prepared == patients


async def test_a_stop_mid_group_discards_the_buffer(test_session, session_factory):
    """Buffered subjects are dropped, never flushed. Flushing would land data
    on the MCS after the operator told the job to stop."""
    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
        workflow="deqm_submit_data",
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)
    batch = Batch(
        job_id=job.id, batch_number=1, patient_ids=["p1", "p2", "p3"], status=BatchStatus.pending
    )
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    workflow = _RecordingGroupWorkflow(group_size=3)
    # _process_single_batch checks once at the top of the retry loop, once per
    # subject before gathering it (3), and once more after the buffer is full
    # and before the group's POST. Letting the first four through and stopping
    # on the fifth lands the stop exactly at that pre-POST gate, with all three
    # subjects gathered and buffered.
    calls = {"n": 0}

    async def stop_after_two(_job_id):
        calls["n"] += 1
        return calls["n"] > 4

    with (
        _make_session_factory_patch(session_factory),
        patch("app.services.orchestrator._stop_or_delete_job", new=AsyncMock(side_effect=stop_after_two)),
    ):
        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map={p: {"resourceType": "Patient", "id": p} for p in ["p1", "p2", "p3"]},
            cdr_url="http://cdr/fhir",
            auth_headers={},
            mcs_url="http://mcs/fhir",
            workflow=workflow,
        )

    assert workflow.prepared == ["p1", "p2", "p3"], "all three were gathered"
    assert workflow.groups == [], "a stop must not flush the buffered subjects"


async def test_group_size_is_read_per_group_not_snapshotted(test_session, session_factory):
    """A downgrade mid-chunk must shrink the REMAINING groups to 1. Reading
    submission_group_size once before the loop would keep submitting groups of
    N in a mode that has no multi-bundle form."""
    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
        workflow="deqm_submit_data",
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)
    patients = ["p1", "p2", "p3", "p4"]
    batch = Batch(job_id=job.id, batch_number=1, patient_ids=patients, status=BatchStatus.pending)
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    class _ShrinkingWorkflow(_RecordingGroupWorkflow):
        async def submit_prepared(self, subjects):
            self._group_size = 1  # as a downgrade would
            return await super().submit_prepared(subjects)

    workflow = _ShrinkingWorkflow(group_size=2)
    with (
        _make_session_factory_patch(session_factory),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={"resourceType": "MeasureReport", "status": "complete", "group": []},
        ),
    ):
        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map={p: {"resourceType": "Patient", "id": p} for p in patients},
            cdr_url="http://cdr/fhir",
            auth_headers={},
            mcs_url="http://mcs/fhir",
            workflow=workflow,
        )

    assert workflow.groups == [["p1", "p2"], ["p3"], ["p4"]]


async def test_a_failed_outcome_is_persisted_and_skips_evaluate(test_session, session_factory):
    """Per-patient accounting is unchanged: a subject whose submit failed gets
    its error row and is excluded from Phase 2, while its group-mates proceed."""
    from app.models.job import Batch, BatchStatus
    from app.services.orchestrator import _process_single_batch

    job = Job(
        measure_id="CMS999",
        period_start="2026-01-01",
        period_end="2026-12-31",
        cdr_url="http://cdr/fhir",
        status=JobStatus.running,
        workflow="deqm_submit_data",
    )
    test_session.add(job)
    await test_session.commit()
    await test_session.refresh(job)
    patients = ["p1", "p2", "p3"]
    batch = Batch(job_id=job.id, batch_number=1, patient_ids=patients, status=BatchStatus.pending)
    test_session.add(batch)
    await test_session.commit()
    await test_session.refresh(batch)

    workflow = _RecordingGroupWorkflow(group_size=3, fail={"p2"})
    with (
        _make_session_factory_patch(session_factory),
        patch(
            "app.services.orchestrator.evaluate_measure",
            new_callable=AsyncMock,
            return_value={"resourceType": "MeasureReport", "status": "complete", "group": []},
        ) as mock_eval,
    ):
        await _process_single_batch(
            job_id=job.id,
            batch_id=batch.id,
            patient_map={p: {"resourceType": "Patient", "id": p} for p in patients},
            cdr_url="http://cdr/fhir",
            auth_headers={},
            mcs_url="http://mcs/fhir",
            workflow=workflow,
        )

    evaluated = [c.kwargs.get("patient_id") or c.args[0] for c in mock_eval.await_args_list]
    # Phase 2 stays per-patient: one $evaluate-measure per surviving subject,
    # never one per group.
    assert evaluated == ["p1", "p3"]
    rows = (
        await test_session.execute(select(MeasureResult).where(MeasureResult.job_id == job.id))
    ).scalars().all()
    failed_rows = [r for r in rows if r.error_phase == "submit"]
    assert [r.patient_id for r in failed_rows] == ["p2"]
```

`select`, `Job`, `JobStatus`, `MeasureResult`, `GatherResult`, `SubmissionWorkflow` and `TransferPhaseError` are already imported in this module. Add only the two new names to the existing `from app.services.workflows import (...)` line: `PreparedSubject`, `SubjectOutcome`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -k "group or stop_mid" -v`
Expected: FAIL — `submit_prepared` is never called; `workflow.groups` stays `[]`.

- [ ] **Step 3: Replace the Phase 1 loop**

Change the import at `orchestrator.py:34` to:

```python
from app.services.workflows import (
    PreparedSubject,
    SubjectOutcome,
    SubmissionWorkflow,
    TransferPhaseError,
    build_submission_workflow,
)
```

Replace the whole `for patient_id in patient_ids:` loop (from `for patient_id in patient_ids:` down to and including `failed += 1`) with:

```python
            # Subjects are walked in groups of workflow.submission_group_size.
            # The buffer is a LOCAL: one workflow instance serves every
            # concurrent chunk of the job, so buffering on the instance would
            # interleave subjects from different chunks and misattribute their
            # failures.
            index = 0
            while index < len(patient_ids):
                # Read fresh, never snapshotted: a runtime downgrade in this or
                # another chunk returns the job to one subject per POST, and a
                # size captured before the loop would keep sending groups of N
                # in a mode that has no multi-bundle form.
                group_size = max(1, workflow.submission_group_size)
                group = patient_ids[index : index + group_size]
                index += len(group)

                prepared: list[PreparedSubject] = []
                for patient_id in group:
                    if await _stop_or_delete_job(job_id):
                        return
                    try:
                        prepared.append(
                            await workflow.prepare_patient(cdr_url, patient_id, auth_headers)
                        )
                    except Exception as prepare_exc:
                        gather_failed_patients.add(patient_id)
                        if not await _record_transfer_failure(
                            job_id=job_id,
                            batch_id=batch_id,
                            patient_id=patient_id,
                            patient_map=patient_map,
                            exc=prepare_exc,
                        ):
                            return
                        failed += 1

                if not prepared:
                    continue
                if await _stop_or_delete_job(job_id):
                    # The buffer is DISCARDED, not flushed. A stop must not land
                    # data on the MCS after the operator asked the job to stop;
                    # these subjects simply never happened, exactly as the rest
                    # of the chunk never happens.
                    return

                outcomes: list[SubjectOutcome] = await workflow.submit_prepared(prepared)
                for outcome in outcomes:
                    if outcome.error is not None:
                        gather_failed_patients.add(outcome.patient_id)
                        if not await _record_transfer_failure(
                            job_id=job_id,
                            batch_id=batch_id,
                            patient_id=outcome.patient_id,
                            patient_map=patient_map,
                            exc=outcome.error,
                        ):
                            return
                        failed += 1
                        continue

                    gather_result = outcome.gather
                    if gather_result is None:
                        continue
                    logger.info(
                        # The gathered count, not the submitted one: the workflow
                        # filters and deduplicates before building the payload.
                        f"Gathered {len(gather_result.resources)} resources for {outcome.patient_id[:8]}",
                        extra={"job_id": job_id, "patient_id": outcome.patient_id},
                    )
                    if gather_result.has_partial_failure:
                        # Partial gather — continue to evaluate with available data (AT-2).
                        # Record which types failed so we can annotate the result after evaluate.
                        failed_type_names = [f.resource_type for f in gather_result.failed_types]
                        succeeded_type_names = sorted(
                            {
                                r.get("resourceType")
                                for r in gather_result.resources
                                if r.get("resourceType")
                            }
                        )
                        partial_gather_patients[outcome.patient_id] = {
                            "operation": "gather",
                            "failed_types": failed_type_names,
                            "succeeded_types": succeeded_type_names,
                        }
                        logger.warning(
                            "Partial CDR gather — continuing evaluation with available data",
                            extra={
                                "job_id": job_id,
                                "patient_id": outcome.patient_id,
                                "failed_types": failed_type_names,
                            },
                        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_orchestrator.py -v`
Expected: PASS — the new tests and every pre-existing one, including the `_StubWorkflow` tests at `:1672` which exercise the base-class delegation.

- [ ] **Step 5: Run the full unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/orchestrator.py backend/tests/test_services_orchestrator.py
git commit -m "feat: walk each chunk in submission groups

Phase 1 becomes group-outer/subject-inner, reading submission_group_size
fresh before each group so a downgrade shrinks the remainder. A stop
arriving mid-group discards the buffer rather than flushing it.

Refs #413"
```

---

### Task 4: Split `DeqmSubmitDataWorkflow` at the build/submit seam

`transfer_patient`'s body divides into `prepare_patient` (gather, filter, dedupe, build the MeasureReport — no MCS I/O) and `submit_prepared` (the POST plus the #414 settlement barrier). Group size stays 1: no multi-bundle envelope yet.

**Files:**
- Modify: `backend/app/services/workflows.py:169-423` (`DeqmSubmitDataWorkflow`)
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Consumes: `PreparedSubject`, `SubjectOutcome` from Task 1.
- Produces:
  - `DeqmSubmitDataWorkflow.__init__(..., group_size: int = 1)`
  - `DeqmSubmitDataWorkflow.submission_group_size -> int`
  - `_post(parameters: dict, mode: str) -> None`, `_submit_individually(subjects: list[PreparedSubject], mode: str) -> list[SubjectOutcome]` — private helpers Task 5 and Task 6 build on.
  - `transfer_patient` retained, now a thin one-subject wrapper that raises.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_workflows.py`:

```python
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
        with patch.object(
            wf._strategy, "gather_patient_data", new=AsyncMock(side_effect=RuntimeError("cdr down"))
        ):
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestDeqmPrepareAndSubmit -v`
Expected: FAIL — `AttributeError`/`TypeError` on `prepare_patient` and `_group_size`.

- [ ] **Step 3: Restructure the workflow**

In `DeqmSubmitDataWorkflow.__init__`, add the parameter and field:

```python
        mode: str,
        group_size: int = 1,
    ):
```

and after `self._mode = mode`:

```python
        # How many subjects may share one STU5 POST. PR 2 never sets this above
        # 1 in production — build_submission_workflow does not pass it — so the
        # grouping mechanics land under test before anything can select them.
        # PR 3 threads the operator's clamped value into this same argument.
        # max(1, ...) guards against a 0 leaking through: "0 means unlimited"
        # is resolved to a real number at job creation, never here.
        self._group_size = max(1, group_size)
```

Add the property after `downgraded`:

```python
    @property
    def submission_group_size(self) -> int:
        """Subjects per submission, read fresh by the orchestrator each group.

        Collapses to 1 outside STU5: base-fallback has no multi-bundle form, so
        a runtime downgrade must return the job to one subject per POST for the
        remainder of the chunk.
        """
        return self._group_size if self._mode == SUBMIT_DATA_MODE_STU5 else 1
```

Replace `transfer_patient` and `_settle_mode_and_submit` with:

```python
    async def prepare_patient(
        self, cdr_url: str, patient_id: str, cdr_auth_headers: dict[str, str]
    ) -> PreparedSubject:
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

    async def _submit_individually(
        self, subjects: list[PreparedSubject], mode: str
    ) -> list[SubjectOutcome]:
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
                outcomes.append(
                    SubjectOutcome(patient_id=subject.patient_id, gather=subject.gather)
                )
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
            async with self._mode_lock:
                if not self._mode_settled.is_set():
                    try:
                        return await self._settle_mode_and_submit(subjects)
                    finally:
                        # In a finally: a pioneer group that fails outright must
                        # still release every group waiting on its verdict.
                        self._mode_settled.set()
            # Settled by the pioneer while we queued on the lock; fall through
            # and submit under whatever it decided.
        # Re-read the settled mode rather than trusting the size this group was
        # formed at: another chunk's pioneer may have downgraded in between, and
        # base-fallback has no multi-bundle envelope.
        if self._mode != SUBMIT_DATA_MODE_STU5:
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_BASE)
        return await self._submit_individually(subjects, SUBMIT_DATA_MODE_STU5)

    async def _settle_mode_and_submit(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """The pioneer's submission: the only one that may downgrade (#414).

        Runs under self._mode_lock with self._mode_settled unset, so it is the
        single point where the job's wire format is decided. In THIS task it
        still handles one subject, which is all that can reach it while the
        group size is 1 — Task 7 generalizes it to the whole group. Keeping it
        single-subject here is what keeps the #414 tests green at every commit.
        """
        subject = subjects[0]
        try:
            await self._post(
                self._parameters_for(subject, SUBMIT_DATA_MODE_STU5), SUBMIT_DATA_MODE_STU5
            )
        except FhirOperationError as exc:
            # A bare status is only trusted when it is a statement about the
            # server (_DOWNGRADE_STATUS_CODES). A 400 is ambiguous, so it
            # downgrades only when its OperationOutcome says the operation is
            # missing — otherwise it is a payload rejection and belongs to this
            # subject alone, with the server's explanation preserved (#414).
            capability_signal = exc.status_code in _DOWNGRADE_STATUS_CODES or (
                exc.status_code == 400 and _outcome_reports_unsupported_operation(exc)
            )
            if not capability_signal:
                return [
                    SubjectOutcome(
                        patient_id=subject.patient_id,
                        gather=subject.gather,
                        error=TransferPhaseError("submit", exc),
                    )
                ]
            logger.warning(
                "STU5 $submit-data rejected (HTTP %s) — downgrading job %s to base $submit-data",
                exc.status_code,
                self._job_id,
                extra={
                    "job_id": self._job_id,
                    "patient_id": subject.patient_id,
                    "status_code": exc.status_code,
                },
            )
            self._mode = SUBMIT_DATA_MODE_BASE
            # Read by the orchestrator to persist Job.submit_data_mode, so the
            # Jobs badge reports the mode actually used rather than the probe's.
            self._downgraded = True
            return await self._submit_individually(subjects, SUBMIT_DATA_MODE_BASE)
        except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is the contract
            return [
                SubjectOutcome(
                    patient_id=subject.patient_id,
                    gather=subject.gather,
                    error=TransferPhaseError("submit", exc),
                )
            ]
        return [SubjectOutcome(patient_id=subject.patient_id, gather=subject.gather)]
```

Add `PreparedSubject`/`SubjectOutcome` usage; no new imports are needed beyond what Task 1 added.

- [ ] **Step 4: Run the workflow suite**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: PASS — the new class and all ~30 pre-existing DEQM tests, including the #414 settlement tests.

- [ ] **Step 5: Run the full unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "feat: split the DEQM workflow at the build/submit seam

prepare_patient does gather, filter, dedupe and the MeasureReport with no
MCS I/O; submit_prepared owns the POST and the #414 settlement barrier.
Group size stays 1, so the wire output is unchanged.

Refs #413"
```

---

### Task 5: One POST for N subjects

The grouped STU5 envelope. `_submit_group` sends every subject's bundle in a single `Parameters`; the downgrade race is closed by the mode re-read Task 4 already put in `submit_prepared`.

**Files:**
- Modify: `backend/app/services/workflows.py` (`DeqmSubmitDataWorkflow.submit_prepared`, new `_submit_group`)
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Consumes: `_post`, `_parameters_for`, `_submit_individually` from Task 4; `build_stu5_parameters(subjects: list[SubjectBundle])` and `SubjectBundle(measure_report, resources)` from `app.services.deqm` (both already imported).
- Produces: `_submit_group(subjects: list[PreparedSubject]) -> list[SubjectOutcome]`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_workflows.py`:

```python
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
                new=AsyncMock(
                    return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])
                ),
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestDeqmGrouping -v`
Expected: FAIL — `test_a_group_of_n_is_one_post_with_n_bundles` reports `submit.await_count == 3`, because `submit_prepared` still submits individually.

- [ ] **Step 3: Add `_submit_group` and route STU5 through it**

Add to `DeqmSubmitDataWorkflow`:

```python
    async def _submit_group(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """One POST carrying every subject's bundle.

        Each `bundle` parameter is a collection Bundle for exactly ONE subject:
        the receiver processes each Bundle as a transaction, so merging subjects
        would make one subject's bad resource fail the others.
        """
        parameters = build_stu5_parameters(
            [SubjectBundle(s.measure_report, s.resources) for s in subjects]
        )
        await self._post(parameters, SUBMIT_DATA_MODE_STU5)
        return [SubjectOutcome(patient_id=s.patient_id, gather=s.gather) for s in subjects]
```

In `submit_prepared`, replace the final line:

```python
        return await self._submit_individually(subjects, SUBMIT_DATA_MODE_STU5)
```

with:

```python
        return await self._submit_group(subjects)
```

Note `_submit_group` currently lets a failure propagate — Task 6 wraps it. Until then a group failure raises out of `submit_prepared`, which is why Task 6 follows immediately.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: PASS — `TestDeqmGrouping` and every pre-existing test. No pre-existing test settles `_mode_settled`, so every one of them routes to the pioneer and none can reach `_submit_group`; its unwrapped failure path is therefore unreachable until Task 6 wraps it. **Do not commit with a red suite.** If something is red, it is a defect in this task, not deferred work for Task 6.

- [ ] **Step 5: Commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "feat: submit a group of subjects in one \$submit-data POST

N subjects become N bundle parameters in one Parameters envelope, one
subject per Bundle. A group formed at N but submitted after a downgrade
goes out individually in base mode.

Refs #413"
```

---

### Task 6: Isolate only payload-attributable failures

A failed group POST is either about one subject's payload or about the server. Isolating the second kind turns one failed POST into N+1 against a server that is down.

**Files:**
- Modify: `backend/app/services/workflows.py` (module constant + `_is_payload_attributable` + `_submit_group`)
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Produces: `_ISOLATE_STATUS_CODES: set[int]`, `_is_payload_attributable(exc: Exception) -> bool` — module level, beside `_DOWNGRADE_STATUS_CODES`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_workflows.py`:

```python
class TestGroupFailureIsolation:
    async def _prepare(self, wf, patient_ids):
        subjects = []
        for pid in patient_ids:
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(
                    return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])
                ),
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
            if len(parameters["parameter"]) == 1 and parameters["parameter"][0]["resource"]["entry"][0][
                "resource"
            ]["subject"]["reference"].endswith("p2"):
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
        with patch(
            "app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(status))
        ) as submit:
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
        """Only _settle_mode_and_submit may change the mode (#414). A settled
        group that isolates must leave the job's wire format alone."""
        wf = self._settled_stu5(2)
        subjects = await self._prepare(wf, ["p1", "p2"])
        with patch(
            "app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(400))
        ) as submit:
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestGroupFailureIsolation -v`
Expected: FAIL — the group failure propagates out of `submit_prepared` instead of becoming outcomes.

- [ ] **Step 3: Add the taxonomy and wrap `_submit_group`**

Add beside `_DOWNGRADE_STATUS_CODES` in `workflows.py`:

```python
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
```

Replace `_submit_group`'s body with:

```python
    async def _submit_group(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """One POST carrying every subject's bundle; isolate only on a payload
        rejection.

        Each `bundle` parameter is a collection Bundle for exactly ONE subject:
        the receiver processes each Bundle as a transaction, so merging subjects
        would make one subject's bad resource fail the others.
        """
        parameters = build_stu5_parameters(
            [SubjectBundle(s.measure_report, s.resources) for s in subjects]
        )
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: PASS, including every pre-existing test.

- [ ] **Step 5: Run the full unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "feat: isolate only payload-attributable group failures

400/409/422 resubmit subject by subject to find the owner of the bad
resource. A 401, a 503 or a timeout describes the server, so the group
fails once with that verdict instead of asking N more times.

Refs #413"
```

---

### Task 7: The pioneer is a group

`_settle_mode_and_submit` takes the whole group: one POST carrying N bundles, and a capability signal downgrades the job and re-sends that group's subjects individually in base mode — without also isolating.

**Files:**
- Modify: `backend/app/services/workflows.py` (`_settle_mode_and_submit`)
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Consumes: `_submit_group`, `_submit_individually`, `_is_payload_attributable`, `_DOWNGRADE_STATUS_CODES`, `_outcome_reports_unsupported_operation`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_workflows.py`:

```python
class TestPioneerGroup:
    async def _prepare(self, wf, patient_ids):
        subjects = []
        for pid in patient_ids:
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(
                    return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])
                ),
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
        with patch(
            "app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(503))
        ) as submit:
            outcomes = await wf.submit_prepared(subjects)
        assert submit.await_count == 1
        assert wf.downgraded is False
        assert all(o.error is not None for o in outcomes)

    async def test_the_barrier_releases_even_when_the_pioneer_group_fails(self):
        """_mode_settled.set() lives in a finally. Without it, every other group
        in the job waits forever on a verdict that will never come."""
        wf = self._pioneer(2)
        subjects = await self._prepare(wf, ["p1", "p2"])
        with patch(
            "app.services.workflows.submit_data", new=AsyncMock(side_effect=_fhir_op_error(503))
        ):
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestPioneerGroup -v`
Expected: FAIL — `test_the_pioneer_group_sends_n_bundles_in_one_post` reports 3 POSTs, because Task 4's `_settle_mode_and_submit` handles `subjects[0]` only.

- [ ] **Step 3: Generalize `_settle_mode_and_submit`**

Replace it with:

```python
    async def _settle_mode_and_submit(self, subjects: list[PreparedSubject]) -> list[SubjectOutcome]:
        """The pioneer group's submission: the only one that may downgrade (#414).

        Runs under self._mode_lock with self._mode_settled unset, so it is the
        single point where the job's wire format is decided. The caller sets the
        event in a finally, so a group that fails outright still releases every
        group waiting on its verdict.
        """
        parameters = build_stu5_parameters(
            [SubjectBundle(s.measure_report, s.resources) for s in subjects]
        )
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
                return await self._submit_individually(subjects, SUBMIT_DATA_MODE_BASE)
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
        except Exception as exc:  # noqa: BLE001 - an outcome, not a raise, is the contract
            return [
                SubjectOutcome(
                    patient_id=s.patient_id,
                    gather=s.gather,
                    error=TransferPhaseError("submit", exc),
                )
                for s in subjects
            ]
        return [SubjectOutcome(patient_id=s.patient_id, gather=s.gather) for s in subjects]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py -v`
Expected: PASS, including the pre-existing #414 tests.

- [ ] **Step 5: Run the full unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -q`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "feat: make the #414 pioneer a group rather than one subject

The pioneer group sends N bundles in one POST. A capability signal
downgrades the job and re-sends that group individually in base mode
without also isolating; a payload rejection isolates without downgrading.

Refs #413"
```

---

### Task 8: Cross-chunk safety, the carried dedupe findings, and docs

Three things the PR is not finished without: proof that two concurrent chunks never mix subjects, the two dedupe gaps PR 1's review parked here, and the architecture doc.

**Files:**
- Test: `backend/tests/test_services_workflows.py`
- Modify: `docs/architecture.md` (the DEQM workflow description, ~line 109-114)

**Interfaces:**
- Consumes: everything from Tasks 1-7.

- [ ] **Step 1: Write the tests**

Append to `backend/tests/test_services_workflows.py`:

```python
class TestCrossChunkSafety:
    async def test_two_concurrent_chunks_never_mix_subjects(self):
        """The buffer is a LOCAL in the orchestrator, not state on the shared
        workflow instance. If it ever moves onto the instance, subjects from
        different chunks interleave into each other's submissions and their
        failures are misattributed — this is the test that catches it."""
        wf = _deqm_workflow(mode="stu5")
        wf._group_size = 2
        wf._mode_settled.set()

        async def _prepare(pid):
            with patch.object(
                wf._strategy,
                "gather_patient_data",
                new=AsyncMock(
                    return_value=GatherResult(resources=[{"resourceType": "Patient", "id": pid}])
                ),
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
```

- [ ] **Step 2: Run the tests**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestCrossChunkSafety tests/test_services_workflows.py::TestDedupeAcrossModes -v`
Expected: PASS — these assert behavior Tasks 1-7 already produce. If one fails, it has found a real defect; fix the source, not the test.

- [ ] **Step 3: Update `docs/architecture.md`**

Find the DEQM workflow description (search for `submit_data_mode`, around line 109-114) and add after it:

```markdown
A DEQM job submits in **groups**. The orchestrator walks each processing chunk
in groups of `submission_group_size`, gathering each subject in turn and then
issuing one `$submit-data` POST carrying one `bundle` parameter per subject.
Group size is 1 today; #413 PR 3 adds the operator control that raises it. The
size is read fresh before every group, so a runtime downgrade to `base-fallback`
— which has no multi-bundle form — returns the job to one subject per POST for
the remainder.

A group POST that fails with 400, 409 or 422 is resubmitted subject by subject,
so one malformed resource fails only the subject that owns it. Any other failure
(401, 403, 404, 405, 429, 5xx, a timeout) is a statement about the server rather
than a payload, and fails every subject in the group with that one verdict
instead of repeating the question N more times.
```

- [ ] **Step 4: Run the full unit suite and lint**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
cd backend && python3 -m pytest tests/ --ignore=tests/integration -q
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_services_workflows.py docs/architecture.md
git commit -m "test: cover cross-chunk isolation and the carried dedupe gaps

Two concurrent chunks must never mix subjects into each other's
submissions — the test that catches a buffer wrongly placed on the shared
workflow instance. Plus PR 1's two parked findings: dedupe in
base-fallback mode, and dedupe surviving the downgrade-rebuilt payload.

Refs #413"
```

---

## Pre-push verification

Per `CLAUDE.md`'s mandatory checklist, **all** of these must pass locally before the PR is pushed. No exceptions.

- [ ] **Lint:** `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`
- [ ] **Unit:** `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
- [ ] **Coverage floor (≥70%):** `cd backend && python3 -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing`
- [ ] **Frontend (unchanged, but the suite must still be green):** `cd frontend && CI=true npm test -- --watchAll=false`
- [ ] **CI-equivalent integration** — the `USE_PREBAKED=1 REQUIRE_PREBAKED=1` prefix is NOT optional:

```bash
USE_PREBAKED=1 REQUIRE_PREBAKED=1 ./scripts/run-integration-tests.sh \
  --ignore=tests/integration/test_golden_measures.py \
  --ignore=tests/integration/test_connectathon_measures.py \
  --ignore=tests/integration/test_full_workflow.py \
  --ignore=tests/integration/test_groups_dropdown.py \
  --ignore=tests/integration/test_full_jobs_pipeline.py \
  --ignore=tests/integration/test_factory_reset.py
```

- [ ] **Full workflow** — required, because this PR touches `orchestrator.py` and the measure pipeline:

```bash
USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py
```

- [ ] **DEQM integration** — the suite that proves base-fallback still works end to end against real HAPI:

```bash
USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_deqm_submit_data_workflow.py
```

Bundled HAPI is `v8.8.0-1`, which does not implement the type-level operation, so these runs exercise **base-fallback only**. `test_deqm_submit_data_workflow.py:187` asserts exactly that and must stay green. The grouped STU5 path is fixture-verified and has no real-server coverage — say so in the PR description rather than implying otherwise.

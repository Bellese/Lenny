# DEQM submit-data contract (PR 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move STU5 submission to type-level `POST [base]/Measure/$submit-data`, select that mode only for servers that demonstrably implement the bundle contract, and deduplicate each subject's gathered resources.

**Architecture:** Three pure additions to `deqm.py` and `fhir_client.py` land first and are individually tested while nothing calls them. A single atomic task then flips the contract — detection, URL, and constants together — because any smaller split leaves the repo in a state where the probe and the POST disagree. Frontend copy and documentation follow.

**Tech Stack:** Python 3.10+, `httpx.AsyncClient`, pytest + `unittest.mock.AsyncMock`, SQLAlchemy 2.0 async, React 18 (plain JS) + Jest/React Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-14-deqm-submit-data-contract-design.md`

**Issue:** [#413](https://github.com/Bellese/Lenny/issues/413) — this plan covers **PR 1 (Contract)** only. PR 2 (Grouping) and PR 3 (Operator control) get their own plans.

## Global Constraints

- **Worktree:** all work happens in `/Users/bill/dev/bellese/lenny-fix-413` on branch `fix/413-submit-data-contract`. Never commit on `main`.
- **Python style:** 3.10+, `X | None` unions (never `Optional[X]`), type hints required.
- **Commits:** conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`).
- **`deqm.py` is pure.** No I/O, no logging, no `httpx`. It returns values; callers log.
- **`detect_submit_data_mode` must never raise and must never block job creation.** Every unconfirmable case returns `SUBMIT_DATA_MODE_BASE`.
- **Never fetch a foreign origin.** Reuse the existing hardened `fhir_client._same_origin(base_url, next_url)` — do not write a second origin comparator.
- **Do not touch** `build_base_parameters`, the `_settle_mode_and_submit` downgrade logic (#414), or `_submit_data_rejection` (#415). Their tests must stay green untouched.
- **Naming:** the word "batch" belongs to `settings.BATCH_SIZE` / the `batches` table / `batch_number`. Nothing in this PR introduces a second meaning for it.
- **Lint before every commit:** `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/app/services/deqm.py` | Pure payload builders | Add `SubjectBundle`, `dedupe_by_identity`; change `build_stu5_parameters` signature |
| `backend/app/services/fhir_client.py` | FHIR I/O, capability probe, submission | Add OperationDefinition resolution + contract check; rewrite `detect_submit_data_mode`; change STU5 URL; remove retired-operation matching |
| `backend/app/services/workflows.py` | Per-job orchestration of gather → submit | Call `dedupe_by_identity`; adapt to the new builder signature |
| `backend/tests/test_services_deqm.py` | Builder unit tests | New dedupe + multi-bundle cases |
| `backend/tests/test_services_fhir_client.py` | Probe + submission unit tests | Rewrite the detection block; flip URL assertions |
| `backend/tests/test_services_workflows.py` | Workflow unit tests | Dedupe wiring; update hardcoded URLs |
| `backend/tests/test_services_orchestrator.py` | Orchestrator tests | Update hardcoded URL at `:990` |
| `frontend/src/pages/JobsPage.js` | Job UI | Four user-facing strings + the workflow option label |
| `frontend/src/pages/JobsPage.workflow.test.js` | Job UI tests | Update three regexes |
| `docs/architecture.md` | Service map | Probe description at `:109-114` |
| `docs/decisions.md` | ADRs | New ADR for dropping the retired endpoint |

---

### Task 1: `dedupe_by_identity` in `deqm.py`

A pure first-wins deduplicator. Nothing calls it yet — Task 3 wires it in.

**Files:**
- Modify: `backend/app/services/deqm.py`
- Test: `backend/tests/test_services_deqm.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `dedupe_by_identity(resources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]`. Returns `(deduped, conflicts)` where `conflicts` holds `"{resourceType}/{id}"` strings, each at most once. **Precondition:** every resource has both `resourceType` and `id` — the caller filters first.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_services_deqm.py`:

```python
class TestDedupeByIdentity:
    def test_identical_duplicates_collapse_without_conflict(self):
        resources = [
            {"resourceType": "Patient", "id": "p1", "gender": "female"},
            {"resourceType": "Patient", "id": "p1", "gender": "female"},
        ]
        deduped, conflicts = dedupe_by_identity(resources)
        assert deduped == [{"resourceType": "Patient", "id": "p1", "gender": "female"}]
        assert conflicts == []

    def test_conflicting_duplicates_keep_first_and_report(self):
        first = {"resourceType": "Patient", "id": "p1", "gender": "female"}
        second = {"resourceType": "Patient", "id": "p1", "gender": "male"}
        deduped, conflicts = dedupe_by_identity([first, second])
        assert deduped == [first]
        assert conflicts == ["Patient/p1"]

    def test_preserves_first_seen_order(self):
        resources = [
            {"resourceType": "Encounter", "id": "e1"},
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Encounter", "id": "e1"},
            {"resourceType": "Condition", "id": "c1"},
        ]
        deduped, conflicts = dedupe_by_identity(resources)
        assert [f"{r['resourceType']}/{r['id']}" for r in deduped] == [
            "Encounter/e1",
            "Patient/p1",
            "Condition/c1",
        ]
        assert conflicts == []

    def test_same_id_different_type_is_not_a_duplicate(self):
        resources = [{"resourceType": "Patient", "id": "x"}, {"resourceType": "Encounter", "id": "x"}]
        deduped, conflicts = dedupe_by_identity(resources)
        assert len(deduped) == 2
        assert conflicts == []

    def test_conflict_reported_once_across_three_occurrences(self):
        resources = [
            {"resourceType": "Patient", "id": "p1", "gender": "female"},
            {"resourceType": "Patient", "id": "p1", "gender": "male"},
            {"resourceType": "Patient", "id": "p1", "gender": "other"},
        ]
        deduped, conflicts = dedupe_by_identity(resources)
        assert len(deduped) == 1
        assert conflicts == ["Patient/p1"]

    def test_key_order_does_not_make_a_conflict(self):
        """Canonical comparison: the same data spelled in a different key order
        is the same resource, not a conflict. A CDR is under no obligation to
        serialise keys consistently across two reads."""
        a = {"resourceType": "Patient", "id": "p1", "gender": "female", "active": True}
        b = {"active": True, "gender": "female", "id": "p1", "resourceType": "Patient"}
        deduped, conflicts = dedupe_by_identity([a, b])
        assert deduped == [a]
        assert conflicts == []

    def test_empty_list(self):
        assert dedupe_by_identity([]) == ([], [])
```

Add `dedupe_by_identity` to the existing `from app.services.deqm import (...)` block at the top of the file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_deqm.py::TestDedupeByIdentity -v`
Expected: FAIL — `ImportError: cannot import name 'dedupe_by_identity'`

- [ ] **Step 3: Implement**

In `backend/app/services/deqm.py`, add `import json` beside the existing `import hashlib`, then add after `build_data_exchange_measure_report`:

```python
def _canonical_json(resource: dict[str, Any]) -> str:
    """Stable serialisation for content comparison — key order is not meaning."""
    return json.dumps(resource, sort_keys=True, separators=(",", ":"), default=str)


def dedupe_by_identity(
    resources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """First-wins dedupe on `(resourceType, id)`.

    Returns the deduped list in first-seen order, and the identities whose
    later occurrences differed in content. Byte-identical duplicates are
    dropped silently — repetition alone is not worth an operator's attention;
    disagreement is.

    Deduplication is deliberately WITHIN one subject. Callers must not reuse
    one accumulator across subjects: a Practitioner shared by two subjects
    belongs in both their Bundles, and suppressing the second would leave a
    dangling reference in a Bundle the receiver may process on its own.

    Precondition: every resource carries `resourceType` and `id`. The caller
    filters first (see workflows.DeqmSubmitDataWorkflow.transfer_patient), and
    deriving the MeasureReport and the payload from that same filtered list is
    what keeps `evaluatedResource` aligned with the Bundle entries.
    """
    first_by_identity: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    conflicts: list[str] = []
    for resource in resources:
        identity = f"{resource['resourceType']}/{resource['id']}"
        if identity not in first_by_identity:
            first_by_identity[identity] = resource
            order.append(identity)
            continue
        if identity in conflicts:
            continue
        if _canonical_json(resource) != _canonical_json(first_by_identity[identity]):
            conflicts.append(identity)
    return [first_by_identity[identity] for identity in order], conflicts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_deqm.py -v`
Expected: PASS — the new class plus every pre-existing test in the file.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
cd .. && git add backend/app/services/deqm.py backend/tests/test_services_deqm.py
git commit -m "feat: add first-wins resource deduplication to deqm builders"
```

---

### Task 2: `SubjectBundle` and the 1..N `build_stu5_parameters`

Changing the signature breaks its three call sites, so the call-site updates belong in this task — the suite must be green at every commit.

**Files:**
- Modify: `backend/app/services/deqm.py`
- Modify: `backend/app/services/workflows.py:303`, `:322`, `:348`
- Test: `backend/tests/test_services_deqm.py:151-165`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `SubjectBundle(measure_report: dict[str, Any], resources: list[dict[str, Any]])`, a frozen dataclass; `build_stu5_parameters(subjects: list[SubjectBundle]) -> dict[str, Any]`. Raises `ValueError` on an empty list. Task 3 constructs `SubjectBundle`; PR 2 passes lists longer than one.

- [ ] **Step 1: Write the failing tests**

Replace `test_stu5_parameters_single_bundle` in `backend/tests/test_services_deqm.py` with:

```python
    def test_stu5_parameters_single_bundle(self):
        mr = _mr()
        params = build_stu5_parameters([SubjectBundle(mr, [LENNY_REPORTER_ORG, *_RESOURCES])])
        assert params["resourceType"] == "Parameters"
        assert len(params["parameter"]) == 1
        p = params["parameter"][0]
        assert p["name"] == "bundle"
        bundle = p["resource"]
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "collection"
        entry_types = [e["resource"]["resourceType"] for e in bundle["entry"]]
        # MeasureReport first, then reporter org + data-of-interest
        assert entry_types == ["MeasureReport", "Organization", "Patient", "Condition", "Encounter"]

    def test_stu5_parameters_one_bundle_parameter_per_subject(self):
        """1..N: each subject gets its own `bundle` parameter, never a shared
        Bundle holding several subjects."""
        mr_a = _mr()
        mr_b = build_data_exchange_measure_report(
            job_id=42,
            patient_id="p2",
            measure_canonical="http://example.org/Measure/CMS122|1.0.0",
            period_start="2025-01-01",
            period_end="2025-12-31",
            resources=[{"resourceType": "Patient", "id": "p2"}],
            timestamp="2026-08-21T12:00:00+00:00",
        )
        params = build_stu5_parameters(
            [
                SubjectBundle(mr_a, [{"resourceType": "Patient", "id": "p1"}]),
                SubjectBundle(mr_b, [{"resourceType": "Patient", "id": "p2"}]),
            ]
        )
        assert [p["name"] for p in params["parameter"]] == ["bundle", "bundle"]
        subjects = [p["resource"]["entry"][0]["resource"]["subject"]["reference"] for p in params["parameter"]]
        assert subjects == ["Patient/p1", "Patient/p2"]
        for p in params["parameter"]:
            assert p["resource"]["type"] == "collection"
            mr_count = sum(1 for e in p["resource"]["entry"] if e["resource"]["resourceType"] == "MeasureReport")
            assert mr_count == 1

    def test_stu5_parameters_rejects_empty_subject_list(self):
        """`bundle` is 1..* — a Parameters body with zero bundles is not a
        submission, and sending one would be a silent no-op at the receiver."""
        with pytest.raises(ValueError):
            build_stu5_parameters([])
```

Add `import pytest` at the top of the file if absent, and add `SubjectBundle` to the `from app.services.deqm import (...)` block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_deqm.py::TestParameterEnvelopes -v`
Expected: FAIL — `ImportError: cannot import name 'SubjectBundle'`

- [ ] **Step 3: Implement the builder**

In `backend/app/services/deqm.py`, add `from dataclasses import dataclass` to the imports, then replace `build_stu5_parameters` with:

```python
@dataclass(frozen=True)
class SubjectBundle:
    """One subject's submission payload: its MeasureReport and its resources.

    `resources` must already be filtered and deduplicated — see
    `dedupe_by_identity`. Pairing them in one object is what stops a caller
    from accidentally deriving the MeasureReport from one list and the Bundle
    entries from another.
    """

    measure_report: dict[str, Any]
    resources: list[dict[str, Any]]


def build_stu5_parameters(subjects: list[SubjectBundle]) -> dict[str, Any]:
    """STU5 envelope: one `bundle` parameter per subject, 1..N.

    Each parameter carries a collection Bundle for exactly one subject, its
    MeasureReport first. Several subjects never share a Bundle: the receiver
    processes each Bundle as a transaction, and merging subjects would make one
    subject's bad resource fail the others.

    With a single subject the output is byte-identical to the pre-#413 payload.
    """
    if not subjects:
        raise ValueError("build_stu5_parameters requires at least one subject (`bundle` is 1..*)")
    return {
        "resourceType": "Parameters",
        "parameter": [
            {
                "name": "bundle",
                "resource": {
                    "resourceType": "Bundle",
                    "type": "collection",
                    "entry": [{"resource": subject.measure_report}]
                    + [{"resource": r} for r in subject.resources],
                },
            }
            for subject in subjects
        ],
    }
```

- [ ] **Step 4: Update the three call sites**

In `backend/app/services/workflows.py`, add `SubjectBundle` to the `from app.services.deqm import (...)` block, then change all three call sites from `build_stu5_parameters(measure_report, submitted)` to:

```python
build_stu5_parameters([SubjectBundle(measure_report, submitted)])
```

They are at `:303` (the pre-settlement branch), `:322` (the re-derive after another patient settled the mode), and `:348` (inside `_settle_mode_and_submit`). Verify with:

```bash
grep -n "build_stu5_parameters" backend/app/services/workflows.py
```

Expected: three hits, each wrapping a single-element list.

- [ ] **Step 5: Run the full backend unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
Expected: PASS. The STU5 payload is unchanged on the wire, so every existing workflow assertion still holds.

- [ ] **Step 6: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
cd .. && git add backend/app/services/deqm.py backend/app/services/workflows.py backend/tests/test_services_deqm.py
git commit -m "feat: build_stu5_parameters takes 1..N subject bundles"
```

---

### Task 3: Deduplicate each subject before its MeasureReport is built

**Files:**
- Modify: `backend/app/services/workflows.py:276`
- Test: `backend/tests/test_services_workflows.py`

**Interfaces:**
- Consumes: `dedupe_by_identity` (Task 1), `SubjectBundle` (Task 2).
- Produces: no new public API. Guarantees that `MeasureReport.evaluatedResource` and the Bundle entries are derived from one deduplicated list.

- [ ] **Step 1: Write the failing tests**

Append to `class TestDeqmSubmitDataWorkflow` in `backend/tests/test_services_workflows.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_workflows.py::TestDeqmSubmitDataWorkflow -v -k "repeated or conflicting or evaluated or shared"`
Expected: FAIL — `test_repeated_resource_appears_once_with_one_evaluated_reference` reports `['Patient/p1', 'Condition/c1', 'Condition/c1']`, the duplicate that this task removes.

- [ ] **Step 3: Implement**

In `backend/app/services/workflows.py`, add `dedupe_by_identity` to the `from app.services.deqm import (...)` block. Then, in `transfer_patient`, replace the single `filtered_resources = ...` line at `:276` with:

```python
        filtered_resources = [r for r in gather.resources if "resourceType" in r and "id" in r]
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
```

Leave the existing explanatory comment above these lines in place — it documents why both derivations read one list, which is exactly what this change preserves.

- [ ] **Step 4: Run the full backend unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
Expected: PASS, including the untouched #414 settlement and #415 rejection tests.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
cd .. && git add backend/app/services/workflows.py backend/tests/test_services_workflows.py
git commit -m "fix: deduplicate a subject's resources before building its MeasureReport"
```

---

### Task 4: OperationDefinition resolution and contract check

Two helpers, fully tested, with no caller yet. Task 5 uses them. Splitting them out keeps Task 5 reviewable.

**Files:**
- Modify: `backend/app/services/fhir_client.py`
- Test: `backend/tests/test_services_fhir_client.py`

**Interfaces:**
- Consumes: the existing `fhir_client._same_origin(base_url, next_url) -> bool` at `:177`.
- Produces:
  - `_operation_definition_matches_contract(operation_definition: dict[str, Any]) -> bool`
  - `async _resolve_operation_definition(client: httpx.AsyncClient, mcs_url: str, definition: str, auth_headers: dict[str, str]) -> dict[str, Any] | None`

- [ ] **Step 1: Write the failing tests**

Insert into `backend/tests/test_services_fhir_client.py`, immediately before `class TestDetectSubmitDataMode`:

```python
class TestOperationDefinitionMatchesContract:
    def _od(self, **overrides) -> dict:
        od = {
            "resourceType": "OperationDefinition",
            "code": "submit-data",
            "type": True,
            "instance": False,
            "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "*"}],
        }
        od.update(overrides)
        return od

    def test_matches_type_level_submit_data_with_bundle_input(self):
        assert _operation_definition_matches_contract(self._od()) is True

    def test_matches_even_when_also_instance_level(self):
        """HAPI 8.10.1 declares type:true AND instance:true. Supporting the
        instance level too does not stop it supporting the type level."""
        assert _operation_definition_matches_contract(self._od(instance=True)) is True

    def test_rejects_instance_only_operation(self):
        assert _operation_definition_matches_contract(self._od(type=False, instance=True)) is False

    def test_rejects_wrong_code(self):
        assert _operation_definition_matches_contract(self._od(code="deqm-submit-data")) is False

    def test_rejects_when_bundle_parameter_absent(self):
        """Base-only server: measureReport + resource, no bundle."""
        od = self._od(
            parameter=[
                {"name": "measureReport", "use": "in", "min": 1, "max": "1"},
                {"name": "resource", "use": "in", "min": 0, "max": "*"},
            ]
        )
        assert _operation_definition_matches_contract(od) is False

    def test_rejects_when_bundle_is_an_output_parameter(self):
        od = self._od(parameter=[{"name": "bundle", "use": "out", "min": 1, "max": "*"}])
        assert _operation_definition_matches_contract(od) is False

    def test_rejects_missing_type_element(self):
        od = self._od()
        del od["type"]
        assert _operation_definition_matches_contract(od) is False


class TestResolveOperationDefinition:
    _OD = {
        "resourceType": "OperationDefinition",
        "code": "submit-data",
        "type": True,
        "parameter": [{"name": "bundle", "use": "in", "max": "*"}],
    }

    async def test_same_origin_definition_is_fetched_directly(self):
        get = AsyncMock(return_value=_make_response(200, self._OD))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/Measure-it-submit-data", {}
                )
        assert result == self._OD
        assert get.call_args[0][0] == "http://mcs/OperationDefinition/Measure-it-submit-data"

    async def test_version_suffix_is_stripped_before_fetching(self):
        get = AsyncMock(return_value=_make_response(200, self._OD))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/x|5.0.0", {}
                )
        assert get.call_args[0][0] == "http://mcs/OperationDefinition/x"

    async def test_foreign_origin_is_never_fetched_directly(self):
        """SSRF guard: `definition` is a URL chosen by a remote server. It is
        resolved by canonical search against the MCS, never dereferenced."""
        bundle = {"resourceType": "Bundle", "entry": [{"resource": self._OD}]}
        get = AsyncMock(return_value=_make_response(200, bundle))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://hl7.org/fhir/OperationDefinition/Measure-submit-data", {}
                )
        assert result == self._OD
        assert get.call_args[0][0] == "http://mcs/OperationDefinition"
        assert get.call_args.kwargs["params"] == {
            "url": "http://hl7.org/fhir/OperationDefinition/Measure-submit-data"
        }
        for call in get.call_args_list:
            assert "hl7.org" not in call[0][0]

    async def test_returns_none_when_direct_fetch_is_not_200(self):
        get = AsyncMock(return_value=_make_response(404, {"resourceType": "OperationOutcome"}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/x", {}
                )
        assert result is None

    async def test_returns_none_when_canonical_search_finds_nothing(self):
        get = AsyncMock(return_value=_make_response(200, {"resourceType": "Bundle", "entry": []}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://elsewhere.example/OperationDefinition/x", {}
                )
        assert result is None

    async def test_returns_none_for_a_wrong_resource_type(self):
        get = AsyncMock(return_value=_make_response(200, {"resourceType": "Patient", "id": "p1"}))
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(
                    client, "http://mcs", "http://mcs/OperationDefinition/x", {}
                )
        assert result is None

    async def test_returns_none_for_an_empty_definition(self):
        get = AsyncMock()
        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", get):
                result = await _resolve_operation_definition(client, "http://mcs", "", {})
        assert result is None
        get.assert_not_awaited()
```

Add `_operation_definition_matches_contract` and `_resolve_operation_definition` to the `from app.services.fhir_client import (...)` block at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_fhir_client.py::TestOperationDefinitionMatchesContract tests/test_services_fhir_client.py::TestResolveOperationDefinition -v`
Expected: FAIL — `ImportError: cannot import name '_operation_definition_matches_contract'`

- [ ] **Step 3: Implement**

In `backend/app/services/fhir_client.py`, add immediately after the `SUBMIT_DATA_MODE_BASE` constant:

```python
# Capability probing for the selected $submit-data contract (#413).
#
# A CapabilityStatement's `rest.resource.operation` carries only `name` and a
# `definition` canonical — it expresses neither type-level support nor the
# parameter list. Confirming the contract therefore means dereferencing the
# OperationDefinition. At most this many are fetched per probe, so a
# pathological CapabilityStatement cannot fan out into unbounded requests.
_MAX_OPERATION_DEFINITION_PROBES = 3


def _operation_definition_matches_contract(operation_definition: dict[str, Any]) -> bool:
    """True when this OperationDefinition is the selected bundle contract.

    All three checks are load-bearing:
      - `code` is what governs the `$submit-data` invocation (it is a separate
        field from the canonical URL's last segment — conflating the two is
        the error #413 was originally filed on).
      - `type: true` is required because the contract is type-level; an
        instance-only operation does not answer POST Measure/$submit-data.
      - a `bundle` INPUT parameter is what separates this contract from a
        base-only server offering `measureReport` + `resource`.

    `instance: true` alongside `type: true` is fine — HAPI 8.10.1 declares
    both, and supporting the instance level does not remove the type level.
    """
    if operation_definition.get("code") != "submit-data":
        return False
    if operation_definition.get("type") is not True:
        return False
    return any(
        param.get("name") == "bundle" and param.get("use") == "in"
        for param in operation_definition.get("parameter", [])
    )


async def _resolve_operation_definition(
    client: httpx.AsyncClient,
    mcs_url: str,
    definition: str,
    auth_headers: dict[str, str],
) -> dict[str, Any] | None:
    """Resolve a CapabilityStatement operation's `definition` canonical.

    `definition` is a URL chosen by a remote server, so a foreign origin is
    NEVER dereferenced — that is the same SSRF vector `_same_origin` already
    guards for pagination links. A foreign canonical is instead resolved the
    FHIR way, by searching the MCS itself for a resource carrying that `url`.
    This also keeps the probe correct for an air-gapped MCS whose
    OperationDefinitions cite hl7.org canonicals it hosts locally.

    Returns None whenever the definition cannot be resolved into an
    OperationDefinition. The caller treats None as "contract not proven".
    """
    canonical = definition.split("|", 1)[0].strip()
    if not canonical:
        return None

    if _same_origin(mcs_url, canonical):
        resp = await client.get(canonical, headers=auth_headers)
        if resp.status_code != 200:
            return None
        body = resp.json()
        if isinstance(body, dict) and body.get("resourceType") == "OperationDefinition":
            return body
        return None

    resp = await client.get(
        f"{mcs_url}/OperationDefinition",
        params={"url": canonical},
        headers=auth_headers,
    )
    if resp.status_code != 200:
        return None
    bundle = resp.json()
    if not isinstance(bundle, dict):
        return None
    for entry in bundle.get("entry", []):
        resource = entry.get("resource")
        if isinstance(resource, dict) and resource.get("resourceType") == "OperationDefinition":
            return resource
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python3 -m pytest tests/test_services_fhir_client.py -v`
Expected: PASS — the two new classes plus every pre-existing test in the file.

- [ ] **Step 5: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
cd .. && git add backend/app/services/fhir_client.py backend/tests/test_services_fhir_client.py
git commit -m "feat: resolve and verify a server's submit-data OperationDefinition"
```

---

### Task 5: Flip the contract — detection, URL, and retired-operation removal

**This task is deliberately atomic.** Changing detection without the URL (or the reverse) leaves a state where the probe says `stu5` and the POST targets the other endpoint. There is no smaller split that keeps the repo coherent.

**Files:**
- Modify: `backend/app/services/fhir_client.py:1054-1055` (constants), `:1093-1141` (probe), `:1168-1205` (docstring + URL)
- Modify: `backend/tests/test_services_fhir_client.py` (the `TestDetectSubmitDataMode` and `TestSubmitData` blocks)
- Modify: `backend/tests/test_services_workflows.py:25`, `:40`, `:200`
- Modify: `backend/tests/test_services_orchestrator.py:990`

**Interfaces:**
- Consumes: `_operation_definition_matches_contract`, `_resolve_operation_definition`, `_MAX_OPERATION_DEFINITION_PROBES` (Task 4).
- Produces: `detect_submit_data_mode` keeps its exact signature and return values. `submit_data` keeps its signature; only the STU5 URL changes. `_DEQM_SUBMIT_DATA_CANONICAL` is deleted; `_DEQM_SUBMIT_DATA_OP_NAME` is renamed `_RETIRED_DEQM_OP_NAME` and used **only** to log why a server fell back — never to classify.

- [ ] **Step 1: Replace the detection tests**

Replace the entire body of `class TestDetectSubmitDataMode` in `backend/tests/test_services_fhir_client.py` with:

```python
class TestDetectSubmitDataMode:
    _CONTRACT_OD = {
        "resourceType": "OperationDefinition",
        "code": "submit-data",
        "type": True,
        "instance": True,
        "parameter": [{"name": "bundle", "use": "in", "min": 1, "max": "*"}],
    }
    _BASE_ONLY_OD = {
        "resourceType": "OperationDefinition",
        "code": "submit-data",
        "type": True,
        "instance": True,
        "parameter": [
            {"name": "measureReport", "use": "in", "min": 1, "max": "1"},
            {"name": "resource", "use": "in", "min": 0, "max": "*"},
        ],
    }

    def _capability(self, operations: list[dict]) -> dict:
        return {
            "resourceType": "CapabilityStatement",
            "rest": [{"mode": "server", "resource": [{"type": "Measure", "operation": operations}]}],
        }

    def _responder(self, capability: dict, operation_definition: dict | None):
        """GET /metadata returns the capability; anything else returns the OD."""

        async def _get(url, *args, **kwargs):
            if url.endswith("/metadata"):
                return _make_response(200, capability)
            if operation_definition is None:
                return _make_response(404, {"resourceType": "OperationOutcome"})
            return _make_response(200, operation_definition)

        return AsyncMock(side_effect=_get)

    async def test_stu5_for_type_level_submit_data_with_bundle_input(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_STU5

    async def test_base_when_operation_is_instance_only(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        od = {**self._CONTRACT_OD, "type": False}
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, od))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_base_when_operation_takes_no_bundle(self):
        """The distinction a CapabilityStatement alone cannot make: this server
        offers a type-level $submit-data, but only in the measureReport +
        resource shape."""
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._BASE_ONLY_OD))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_base_when_only_the_retired_deqm_operation_is_advertised(self):
        """#413 decision 1: support for the retired $deqm-submit-data is dropped,
        not renamed. A server offering only it is a base-fallback server. This
        test is what fails if someone restores historical support without
        revisiting the design doc."""
        cap = self._capability(
            [
                {
                    "name": "deqm-submit-data",
                    "definition": "http://hl7.org/fhir/us/davinci-deqm/OperationDefinition/submit-data",
                }
            ]
        )
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_retired_only_server_is_logged(self):
        cap = self._capability([{"name": "deqm-submit-data", "definition": "http://mcs/OperationDefinition/x"}])
        with (
            patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx,
            patch("app.services.fhir_client.logger.info") as info,
        ):
            _mock_async_client(mock_httpx, get=self._responder(cap, None))
            await detect_submit_data_mode(mcs_url="http://mcs")
        assert any("retired" in str(c.args[0]).lower() for c in info.call_args_list)

    async def test_base_when_candidate_has_no_definition(self):
        cap = self._capability([{"name": "submit-data"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_base_when_operation_definition_is_unfetchable(self):
        cap = self._capability([{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}])
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, None))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_foreign_origin_definition_is_not_contacted(self):
        cap = self._capability(
            [{"name": "submit-data", "definition": "http://hl7.org/fhir/OperationDefinition/Measure-submit-data"}]
        )
        get = self._responder(cap, None)
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=get)
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE
        for call in get.call_args_list:
            assert "hl7.org" not in call[0][0]

    async def test_operation_on_rest_root_is_also_considered(self):
        cap = {
            "resourceType": "CapabilityStatement",
            "rest": [{"mode": "server", "operation": [{"name": "submit-data", "definition": "http://mcs/OperationDefinition/sd"}]}],
        }
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=self._responder(cap, self._CONTRACT_OD))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_STU5

    async def test_at_most_three_operation_definitions_are_fetched(self):
        cap = self._capability(
            [{"name": "submit-data", "definition": f"http://mcs/OperationDefinition/sd{i}"} for i in range(10)]
        )
        get = self._responder(cap, self._BASE_ONLY_OD)
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=get)
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE
        # 1 metadata call + at most _MAX_OPERATION_DEFINITION_PROBES definition reads
        assert get.await_count <= 1 + _MAX_OPERATION_DEFINITION_PROBES

    async def test_fallback_when_probe_raises(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(side_effect=httpx.ConnectError("boom")))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE

    async def test_never_raises_when_capability_body_is_malformed(self):
        with patch("app.services.fhir_client.httpx.AsyncClient") as mock_httpx:
            _mock_async_client(mock_httpx, get=AsyncMock(return_value=_make_response(200, {"rest": "not-a-list"})))
            assert await detect_submit_data_mode(mcs_url="http://mcs") == SUBMIT_DATA_MODE_BASE
```

Add `_MAX_OPERATION_DEFINITION_PROBES` to the test file's `from app.services.fhir_client import (...)` block.

- [ ] **Step 2: Update the three submission-URL tests**

In `class TestSubmitData`, rename `test_posts_to_deqm_operation_in_stu5_mode` to `test_posts_to_type_level_operation_in_stu5_mode` and change its final assertion to:

```python
        assert post.call_args[0][0] == "http://mcs/Measure/$submit-data"
```

In `test_base_and_stu5_modes_produce_different_url_shapes`, replace the comment and the STU5 assertion:

```python
        # Regression guard for the ruling this test file encodes: base-fallback
        # is instance-level (Measure/{id}/$submit-data) because that's the only
        # shape HAPI's clinical-reasoning module accepts; the selected STU5
        # contract is type-level (Measure/$submit-data). The two now differ only
        # by the measure-id segment, so collapsing them into one shape is an
        # easy mistake to make and this test is what catches it.
```

```python
        assert stu5_url == "http://mcs/Measure/$submit-data"
```

`test_posts_to_instance_level_operation_in_base_mode` is unchanged — it is the base-mode regression AC.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && python3 -m pytest tests/test_services_fhir_client.py::TestDetectSubmitDataMode tests/test_services_fhir_client.py::TestSubmitData -v`
Expected: FAIL — detection still classifies the retired operation as `stu5`, and the STU5 POST still targets `$deqm-submit-data`.

- [ ] **Step 4: Replace the constants**

In `backend/app/services/fhir_client.py`, replace lines `1054-1055`:

```python
# The DEQM IG's own submission operation (`code: deqm-submit-data`, canonical
# .../OperationDefinition/submit-data) was RETIRED upstream on 2026-03-05:
#   deprecated  https://github.com/HL7/davinci-deqm/commit/65c053f76cae7d0dda784547be177d3fbc0b39f5
#   retired     https://github.com/HL7/davinci-deqm/commit/bc0d01b6ee86e7d571e76e2ba0cafb640c376fa3
#   guidance    https://github.com/HL7/davinci-deqm/commit/0d6b646861c1fe36371ef706f28ba4209e05968c
# Lenny deliberately no longer matches it (#413, decision 1): a server offering
# only the retired operation is classified base-fallback. Do NOT reintroduce it
# as a classification signal — read
# docs/superpowers/specs/2026-09-14-deqm-submit-data-contract-design.md first.
# The name survives ONLY to explain the fallback in a log line.
_RETIRED_DEQM_OP_NAME = "deqm-submit-data"

# The operation `code` of the selected contract. NOT the canonical's last
# segment — those are separate fields, and conflating them is the error #413
# was originally filed on.
_SUBMIT_DATA_OP_CODE = "submit-data"
```

- [ ] **Step 5: Rewrite the probe**

Replace the body of `detect_submit_data_mode` (`:1093-1141`) with:

```python
async def detect_submit_data_mode(
    *,
    mcs_url: str,
    auth_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> str:
    """Probe the MCS for the selected type-level $submit-data bundle contract.

    Returns SUBMIT_DATA_MODE_STU5 only when the server advertises an operation
    whose OperationDefinition has `code: submit-data`, `type: true`, and a
    `bundle` input parameter. A CapabilityStatement cannot express the last two,
    so the definition is dereferenced (never across origins — see
    `_resolve_operation_definition`).

    Everything else is SUBMIT_DATA_MODE_BASE, including every case where the
    contract merely cannot be CONFIRMED: an unreachable /metadata, a missing or
    unfetchable OperationDefinition, a malformed body. That direction is the
    safe one — base-fallback is the empirically verified path against HAPI,
    whereas a false stu5 costs the job a pioneer round trip before
    `_settle_mode_and_submit` downgrades it.

    Never raises: the probe decides the envelope, it must not block job
    creation (the measure pre-flight already proved the MCS reachable).
    """
    headers = auth_headers or {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{mcs_url}/metadata", headers=headers)
            resp.raise_for_status()
            capability = resp.json()

            candidates: list[str] = []
            saw_retired_operation = False
            for rest in capability.get("rest", []):
                operations = list(rest.get("operation", []))
                for res in rest.get("resource", []):
                    if res.get("type") == "Measure":
                        operations.extend(res.get("operation", []))
                for op in operations:
                    name = op.get("name")
                    if name == _RETIRED_DEQM_OP_NAME:
                        saw_retired_operation = True
                    elif name == _SUBMIT_DATA_OP_CODE and op.get("definition"):
                        candidates.append(str(op["definition"]))

            for definition in candidates[:_MAX_OPERATION_DEFINITION_PROBES]:
                operation_definition = await _resolve_operation_definition(client, mcs_url, definition, headers)
                if operation_definition is not None and _operation_definition_matches_contract(
                    operation_definition
                ):
                    return SUBMIT_DATA_MODE_STU5

            if saw_retired_operation and not candidates:
                logger.info(
                    "MCS advertises only the retired DEQM $%s operation (retired upstream "
                    "2026-03-05) — using base $submit-data",
                    _RETIRED_DEQM_OP_NAME,
                    extra={"mcs_url": sanitize_url(mcs_url)},
                )
    except Exception as exc:
        # Deferred import: app.services.validation imports from this module at
        # module load time, so a top-level import here would be circular.
        from app.services.validation import sanitize_error

        logger.warning(
            "CapabilityStatement probe for the $submit-data bundle contract failed "
            "— assuming base $submit-data",
            extra={"mcs_url": sanitize_url(mcs_url), "error": sanitize_error(exc)},
        )
    return SUBMIT_DATA_MODE_BASE
```

- [ ] **Step 6: Change the submission URL and its docstring**

In `submit_data`, replace the first bullet of the "two modes" docstring paragraph (`:1171-1175`) with:

```
    - STU5 mode POSTs to the type-level `Measure/$submit-data`. The contract
      selected in #413 is type-level, and `detect_submit_data_mode` only
      resolves to this mode after confirming the server's OperationDefinition
      declares `type: true` with a `bundle` input.
```

Then change `:1203`:

```python
        url = f"{mcs_url}/Measure/$submit-data"
```

Add this sentence to the end of that same docstring paragraph, after the base-mode bullet:

```
    The two URLs now differ ONLY by the measure-id segment, which makes the
    warning above more important, not less: collapsing them into one shape
    would silently send every base-fallback job to an endpoint HAPI does not
    implement.
```

- [ ] **Step 7: Update the hardcoded URLs in other test files**

`backend/tests/test_services_workflows.py` at `:25` and `:40` — change `url="http://mcs/Measure/$deqm-submit-data"` to `url="http://mcs/Measure/$submit-data"`.

At `:200`, change the diagnostics string to `"does not know how to handle POST operation[Measure/$submit-data]"`.

`backend/tests/test_services_orchestrator.py:990` — change `url="http://mcs/Measure/$deqm-submit-data"` to `url="http://mcs/Measure/$submit-data"`.

Confirm none remain:

```bash
grep -rn 'deqm-submit-data' backend/app backend/tests
```

Expected: hits only in the `_RETIRED_DEQM_OP_NAME` comment block, the retired-operation tests, and the docstring in `deqm.py` if it mentions the history.

- [ ] **Step 8: Run the full backend unit suite**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v`
Expected: PASS, including the untouched #414 settlement and #415 rejection tests.

- [ ] **Step 9: Lint and commit**

```bash
cd backend && ruff check app/ tests/ && ruff format --check app/ tests/
cd .. && git add backend/app/services/fhir_client.py backend/tests/
git commit -m "fix: submit STU5 data to type-level Measure/\$submit-data

Detection now confirms code, type-level support, and a bundle input
against the server's OperationDefinition rather than matching an
operation name. Support for the retired \$deqm-submit-data is dropped:
a server offering only it classifies base-fallback.

Closes part of #413."
```

---

### Task 6: Frontend copy

**Files:**
- Modify: `frontend/src/pages/JobsPage.js:176`, `:427-431`, `:514`
- Test: `frontend/src/pages/JobsPage.workflow.test.js:90`, `:99`, `:104`

**Interfaces:**
- Consumes: nothing. `job.submit_data_mode` values (`stu5` / `base-fallback`) are unchanged.
- Produces: no API change.

- [ ] **Step 1: Update the test expectations**

In `frontend/src/pages/JobsPage.workflow.test.js`:

- `:90` — change the matcher to `/MCS does not support type-level \$submit-data with bundles — falling back to instance-level \$submit-data\./i`
- `:99` — change `findByTitle(/does not support DEQM STU5/i)` to `findByTitle(/does not support type-level \$submit-data/i)`
- `:104` — change `getByLabelText(/does not support DEQM STU5/i)` to `getByLabelText(/does not support type-level \$submit-data/i)`

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && CI=true npx react-scripts test --testPathPattern JobsPage.workflow --watchAll=false`
Expected: FAIL — the rendered copy still says "DEQM STU5 $deqm-submit-data".

- [ ] **Step 3: Update the copy**

`JobsPage.js:176`:

```javascript
        toast.warning('MCS does not support type-level $submit-data with bundles — falling back to instance-level $submit-data.');
```

`JobsPage.js:427-431`:

```javascript
                            title={isFallback
                              ? 'MCS does not support type-level $submit-data with bundles — instance-level $submit-data fallback used.'
                              : 'DEQM $submit-data (type-level, bundle)'}
                            aria-label={isFallback
                              ? 'DEQM — MCS does not support type-level $submit-data with bundles — instance-level $submit-data fallback used.'
                              : 'DEQM — DEQM $submit-data (type-level, bundle)'}
```

`JobsPage.js:514`:

```javascript
                  <option value="deqm_submit_data">DEQM Data Exchange — $submit-data (bundle)</option>
```

Both modes are now `$submit-data`, so the copy names the distinction that actually matters to an operator: type-level with bundles versus instance-level.

- [ ] **Step 4: Run the frontend tests**

Run: `cd frontend && CI=true npx react-scripts test --watchAll=false`
Expected: PASS — the whole frontend suite.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/JobsPage.js frontend/src/pages/JobsPage.workflow.test.js
git commit -m "fix: job UI copy names the type-level bundle contract, not \$deqm-submit-data"
```

---

### Task 7: Documentation and ADR

**Files:**
- Modify: `docs/architecture.md:109-114`
- Modify: `docs/decisions.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Update the architecture probe description**

In `docs/architecture.md`, replace the sentences at `:109-114` (from "Also home to the" through "original probe verdict.") with:

```
                         DEQM $submit-data capability probe: detect_submit_data_mode() reads the
                         MCS CapabilityStatement at job creation, then dereferences the
                         OperationDefinition behind any advertised `submit-data` operation —
                         a CapabilityStatement carries only `name` + a `definition` canonical, so
                         type-level support and the `bundle` input are invisible to it. `stu5`
                         requires `code: submit-data`, `type: true`, and a `bundle` input; every
                         other case, including anything merely unconfirmable, is `base-fallback`.
                         The retired DEQM `$deqm-submit-data` (retired upstream 2026-03-05) is
                         deliberately NOT a classification signal (#413). A foreign-origin
                         `definition` is never fetched — it is resolved via
                         `OperationDefinition?url=` against the MCS itself. The verdict stamps
                         `Job.submit_data_mode`, deciding which URL shape/envelope submit_data()
                         uses: type-level `Measure/$submit-data` with 1..* `bundle` parameters, or
                         instance-level `Measure/{id}/$submit-data`. A mis-probed `stu5` that
                         400s/404s on the real POST downgrades to base mode at runtime and retries
                         once (workflows.py); the stored `Job.submit_data_mode` still reflects the
                         original probe verdict.
```

- [ ] **Step 2: Determine the ADR number**

Run:

```bash
grep -n "^## ADR-" docs/decisions.md | tail -3
```

At the time of writing the last is ADR-015, so use **ADR-016** — unless that grep shows an ADR-016 already exists (#420's versioning ADR is pending and unwritten), in which case take the next free number. Do not renumber existing ADRs, and do not touch the ADR-014/ADR-015 heading arrangement — that is #428's job.

- [ ] **Step 3: Append the ADR**

Append to `docs/decisions.md`:

```markdown
## ADR-016: The retired DEQM `$deqm-submit-data` is dropped, not preserved behind a third mode (2026-09-14)

**Context.** Lenny's STU5 path POSTed to `[base]/Measure/$deqm-submit-data`, correctly matching
published US DEQM STU5 (5.0.0), whose operation `code` really is `deqm-submit-data`. HL7 retired
that operation upstream on 2026-03-05 and redirected submission guidance to the core FHIR API. The
maintainer selected a concrete replacement contract: type-level `POST [base]/Measure/$submit-data`
carrying 1..* `bundle` parameters, each a single-subject collection Bundle.

**Decision.** Two modes, not three. `stu5` now means the selected contract only. A server that
advertises only the retired `$deqm-submit-data` is classified `base-fallback`. Detection is purely
structural — `code: submit-data`, `type: true`, and a `bundle` input parameter, confirmed against
the server's OperationDefinition — so an ordinary HAPI 8.10.x classifies `stu5`, which is correct:
the contract genuinely works there.

**Why not keep a third mode.** It would cost a new `jobs.submit_data_mode` value, a new badge state,
and a third payload path to maintain and test, for a server shape Lenny has never successfully
talked to. Claiming support we cannot demonstrate is worse than dropping it openly.

**Consequences.** A published-STU5-only server that previously reached `$deqm-submit-data` now
falls back to base mode; the probe logs an `info` line naming the retirement so the badge is
explainable. Detection costs up to three extra HTTP reads at job creation, capped, non-raising, and
never crossing origins. The STU5 branch still has no real-server execution — bundled HAPI is pinned
at `v8.8.0-1`, which does not implement the type-level operation; a bump to 8.10.x is the follow-up
that would first exercise it.

**Full rationale and rejected alternatives:**
`docs/superpowers/specs/2026-09-14-deqm-submit-data-contract-design.md`. Issue #413.
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md docs/decisions.md
git commit -m "docs: record the type-level \$submit-data contract and its ADR"
```

---

### Task 8: Pre-push verification

CLAUDE.md's checklist is mandatory and this change touches `fhir_client.py` and the measure pipeline, so the full-workflow suite is required on top of the CI-equivalent run.

**Files:** none modified.

- [ ] **Step 1: Lint**

Run: `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/`
Expected: clean.

- [ ] **Step 2: Unit suite with coverage**

Run: `cd backend && python3 -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing`
Expected: PASS, coverage at or above the 70% floor.

- [ ] **Step 3: Frontend build**

Run: `cd frontend && CI=true npm run build`
Expected: succeeds with no warnings-as-errors.

- [ ] **Step 4: CI-equivalent integration suite**

The `USE_PREBAKED=1 REQUIRE_PREBAKED=1` prefix is not optional — without it the script silently falls back to vanilla HAPI images with no FHIR Groups, and the run is not CI-equivalent.

Run:

```bash
USE_PREBAKED=1 REQUIRE_PREBAKED=1 ./scripts/run-integration-tests.sh \
  --ignore=tests/integration/test_golden_measures.py \
  --ignore=tests/integration/test_connectathon_measures.py \
  --ignore=tests/integration/test_full_workflow.py \
  --ignore=tests/integration/test_groups_dropdown.py \
  --ignore=tests/integration/test_full_jobs_pipeline.py \
  --ignore=tests/integration/test_factory_reset.py
```

Expected: PASS (~3–5 min).

- [ ] **Step 5: Full-workflow suite**

Required because this change touches the measure pipeline / FHIR data flow.

Run: `./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py`
Expected: PASS.

- [ ] **Step 6: Confirm the bundled-HAPI probe still says base-fallback**

`test_deqm_submit_data_workflow.py:187` asserts bundled HAPI probes to `base-fallback`. That must remain true: HAPI `v8.8.0-1` does not implement the type-level operation, so the new structural probe should reach the same verdict by a different route. A failure here means detection is classifying a server that cannot serve the contract.

Run: `USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_deqm_submit_data_workflow.py`
Expected: PASS.

- [ ] **Step 7: Push and open the PR**

Only if every step above passed. Use `.github/pull_request_template.md` sections — `gh pr create` does not auto-populate them. The PR body must state that PR 1 of 3 for #413 is landing, that the STU5 path remains fixture-verified only, and that PRs 2 and 3 follow.

```bash
git push -u origin fix/413-submit-data-contract
```

---

## Self-Review

**Spec coverage.** Every PR 1 acceptance criterion in the design doc maps to a task: type-level URL → Task 5; bundle-only parameters and per-subject Bundles → Task 2; `evaluatedResource` 1:1 → Task 3; dedupe and conflict policy → Tasks 1 and 3; no cross-subject suppression → Task 3; reporter Organization untouched → unchanged code, asserted by the existing suite; the five detection criteria → Tasks 4 and 5; foreign-origin guard → Task 4; never-raises → Task 5; base-fallback regression → Task 5 step 2; docs → Task 7.

**Deliberate refinement of the spec.** The spec says both retired-operation constants are removed. `_DEQM_SUBMIT_DATA_CANONICAL` is; `_DEQM_SUBMIT_DATA_OP_NAME` is renamed `_RETIRED_DEQM_OP_NAME` and kept **solely** so the fallback log line can name the operation. It is never a classification signal, which is what the spec's intent protects. A bare string literal in a log call would have been worse.

**Known gap, carried deliberately.** The spec's PR 1 criteria include an end-to-end test of the STU5 request. Bundled HAPI cannot serve it, so that coverage is the fixture-backed unit tests in Tasks 4 and 5 plus the Task 8 step 6 check that real HAPI still resolves to base-fallback. The PR body must say so rather than implying real-server verification.

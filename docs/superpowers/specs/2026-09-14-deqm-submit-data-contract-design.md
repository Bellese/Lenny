# DEQM STU5 submission contract: type-level `Measure/$submit-data` with 1..* bundles

**Issue:** #413 · **Amends:** `2026-08-21-deqm-submit-data-workflow-design.md` · **Date:** 2026-09-14

## Problem

Lenny's STU5 submission path POSTs to `[base]/Measure/$deqm-submit-data`. That
matched the published US DEQM STU5 OperationDefinition (5.0.0, generated
2025-05-19), whose `code` really is `deqm-submit-data` — the September 13
investigation confirmed the constants, the type-level invocation, and the
`instance: false` docstring were all correct.

Two things have since changed that.

**Upstream retired the operation.** On 2026-03-05 HL7 marked it deprecated
([65c053f](https://github.com/HL7/davinci-deqm/commit/65c053f76cae7d0dda784547be177d3fbc0b39f5)),
flipped its status from active to retired
([bc0d01b](https://github.com/HL7/davinci-deqm/commit/bc0d01b6ee86e7d571e76e2ba0cafb640c376fa3)),
and redirected submission guidance to the core FHIR API
([0d6b646](https://github.com/HL7/davinci-deqm/commit/0d6b646861c1fe36371ef706f28ba4209e05968c)).
The published IG still reflects May 2025. The current continuous build directs
producers to batch/transaction or a bulk operation, but it is an unpublished
draft under the universal-realm guide and is not what this change implements.

**The maintainer selected a concrete contract.** Type-level
`POST [base]/Measure/$submit-data`, carrying **1..\*** `bundle` parameters. Each
submitted Bundle should hold the resources for a single subject, and should hold
all of that subject's MeasureReport and data of interest. This is an explicit
application contract; it is neither the published STU5 endpoint nor a mandate of
the current draft, and must not be described as either. The 1..\* cardinality
does match published STU5's `bundle` element, which was also 1..\*.

There is a third shape in the wild that this design has to tell apart from the
first two. HAPI 8.10.1 advertises, on its *base* `$submit-data`
(`Measure-it-submit-data`): `code: submit-data`, `type: true`, `instance: true`,
parameters `measureReport` 1..1 + `resource` 0..* + **`bundle` 1..\***. On every
axis the selected contract names, that server supports it.

Meanwhile detection cannot see any of this. `CapabilityStatement.rest.resource.
operation` carries only `name` and a `definition` canonical — it expresses
neither type-level-ness nor the parameter list. The current probe
(`fhir_client.py:1093`) matches on `name == "deqm-submit-data"` or the DEQM
canonical, which is the only thing a CapabilityStatement alone can support.

## Goal

Submit under the maintainer-selected contract, select that mode only for servers
that demonstrably implement it, and let an operator choose how many subject
Bundles travel in one submission.

## Non-goals

- Migrating to the draft's batch/transaction submission guidance.
- Multi-measure aggregation, or any change to which measures a job runs. One
  measure at a time satisfies #413.
- Multiple subjects inside one Bundle. N subjects means N Bundles.
- Cross-Bundle deduplication. A Practitioner or Organization shared across
  subjects legitimately appears in more than one subject's Bundle.
- Re-inlining the reporter Organization per patient. It is PUT once per job and
  referenced; inlining it caused the `ResourceVersionConflictException` storm the
  2026-08-21 spec fixed.
- Changing the orchestrator's chunking, the `batches` table, `BATCH_SIZE`, or
  `MAX_WORKERS`. Submission grouping nests *inside* a chunk.
- Batching in `base-fallback` or `direct_load`. Grouping applies to STU5 mode only.
- Bumping bundled HAPI. See *Real-server coverage* — the natural follow-up, not
  part of this change.

## Decisions

Taken with the maintainer on 2026-09-14, before any code was designed. Each is
load-bearing; *Rejected alternatives* records what was given up.

1. **Two modes, not three.** `stu5` means the selected contract only. A server
   advertising only the retired `$deqm-submit-data` resolves to `base-fallback`.
   Support for the historical endpoint is dropped, not renamed.
2. **Detection is purely structural.** A type-level `submit-data` accepting a
   `bundle` input classifies `stu5` whether or not the server mentions DEQM.
   Capability is the contract. A plain HAPI 8.10.x therefore classifies `stu5`,
   which is correct — the contract does work there.
3. **The payload carries 1..N bundles**, each single-subject, each with that
   subject's MeasureReport and data of interest.
4. **Bundles-per-submission is chosen by the operator at job creation.**
   Default 1. The last value chosen is remembered across jobs, 1 included. `0`
   means "no limit beyond what the job already imposes" — every subject in the
   current processing chunk.
5. **A failed multi-bundle submission retries its subjects individually.**
   Batching stays a throughput optimization with no failure-granularity
   regression.
6. **Deduplication happens once per subject, before that subject's MeasureReport
   is built.** Not in the payload builder, not in the gather layer.
7. **First-wins on conflict, with a warning.** Byte-identical duplicates collapse
   silently; differing representations of one identity keep the first and are
   reported.
8. **Naming: "bundles-per-submission."** "Batch" keeps its existing meaning
   throughout the codebase.

### A note on the word "batch"

`settings.BATCH_SIZE = 100` chunks a job's patients into `Batch` rows
(`batches` table, `batch_number`, `BatchStatus`, `retry_count`) for progress and
retry; `MAX_WORKERS = 4` bounds how many chunks run at once. That concept is
untouched here and keeps the name. The new control is *bundles-per-submission*
and never appears as "batch size" in code, config, API, or UI.

## Design

### Capability detection

The probe keeps its `stu5` / `base-fallback` verdicts and its hard guarantee that
it never raises and never blocks job creation. The algorithm inside becomes two
steps. (In PR 1 it is `detect_submit_data_mode`, returning the verdict as a bare
string; PR 3 renames it to `detect_submit_data_capability` and widens the return
to carry the declared `bundle` max as well — see § The bundles-per-submission
control. The verdict and the guarantee are identical in both.)

**Step 1 — candidates.** `GET {mcs_url}/metadata`. Collect operations from
`rest[].operation[]` and from `rest[].resource[type=Measure].operation[]`; keep
those whose `name` is `submit-data`.

**Step 2 — confirmation.** For each candidate, dereference its `definition` and
require all three of:

| Check | Why it is not optional |
|---|---|
| `code == "submit-data"` | The invocation is `$submit-data`; `code` is the field that governs it. |
| `type == true` | The contract is type-level. An instance-only operation does not answer `POST Measure/$submit-data`. |
| an `in` parameter named `bundle` | Distinguishes the selected contract from base-only servers offering `measureReport` + `resource`. |

Any candidate satisfying all three → `stu5`. Otherwise → `base-fallback`. At
most 3 candidates are dereferenced, so a pathological CapabilityStatement cannot
fan out into unbounded requests.

The `bundle` parameter's `max` is also read and retained, because it bounds
grouping — see *Clamping*. A `max` of `1` does **not** disqualify a server: it
supports the contract, at one bundle per call.

**Dereferencing is not a blind fetch.** `definition` is a URL supplied by a
remote server, so:

- Same origin as `mcs_url` → `GET` it directly.
- Different origin → resolve it the FHIR way, `GET {mcs_url}/OperationDefinition?url={canonical}`,
  and read the first entry.

A foreign origin is never contacted. That is an SSRF guard first, and it also
keeps the probe correct for an air-gapped MCS whose OperationDefinitions cite
`hl7.org` canonicals it hosts locally.

**Unconfirmable means `base-fallback`.** If the candidate carries no `definition`
at all, or its OperationDefinition cannot be fetched or parsed, the contract is
not proven and the verdict is base. This is the safe direction: base-fallback is
the empirically verified path against HAPI, whereas a false `stu5` costs the job
a pioneer round trip before `_settle_mode_and_submit` downgrades it.

An OperationDefinition-dereferencing probe was designed once before, in the
brainstorming pass that preceded #413's refutation, and abandoned with the rest
of that pass. It returns here for an unrelated reason: not to catch a name
mismatch, but because `CapabilityStatement` structurally cannot express
type-level-ness or parameters.

### Submission URL

`fhir_client.py:1203` becomes `f"{mcs_url}/Measure/$submit-data"`. Base-fallback
at `:1205` is untouched: `f"{mcs_url}/Measure/{measure_id}/$submit-data"`.

The two URLs now differ only by the measure-id segment, which makes the existing
"the two modes deliberately use DIFFERENT URL shapes — do not simplify them into
one" docstring more load-bearing, not less. Its STU5 rationale changes: no longer
that published STU5 declares `instance: false`, but that the selected contract is
type-level. The base-mode rationale is unchanged and stays empirical — HAPI's
clinical-reasoning module does not register the type-level operation.

### Dropping the retired operation

`_DEQM_SUBMIT_DATA_CANONICAL` and `_DEQM_SUBMIT_DATA_OP_NAME`
(`fhir_client.py:1054-1055`) are removed. In their place, a comment records the
March 2026 retirement, links the three HL7 commits, and states that the operation
is deliberately not matched. Without that note the next reader re-derives the
history and "fixes" the code back — the loop this issue has already been through
twice.

Because the consequence is user-visible, it is not left silent: when a server
advertises only the retired operation, the probe logs one `info` line saying so,
giving an operator a real explanation for the base-fallback badge.

### Payload: 1..N bundles

`build_stu5_parameters` changes from one MeasureReport plus resources to a list
of per-subject bundles:

```python
def build_stu5_parameters(subjects: list[SubjectBundle]) -> dict[str, Any]:
    """STU5 envelope: one `bundle` parameter per subject, 1..N."""
```

where `SubjectBundle` pairs one subject's MeasureReport with its deduplicated
resources. Each produces exactly the collection Bundle built today — MeasureReport
first, then that subject's resources. With one subject the wire output is
byte-identical to the current payload, which is what keeps the default path a
no-op.

`build_base_parameters` is untouched and stays single-subject; base-fallback has
no multi-bundle form.

### Deduplication

A new pure helper in `deqm.py`, which by its module contract does no I/O and no
logging — so it reports conflicts rather than logging them:

```python
def dedupe_by_identity(resources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """First-wins dedupe on (resourceType, id).

    Returns the deduped list and the identities whose later occurrences
    differed in content. Byte-identical duplicates are dropped silently.
    """
```

Content comparison is canonical JSON (`json.dumps(..., sort_keys=True)`).

It runs per subject, at `workflows.py:276`, immediately after the existing
`filtered_resources` filter and before `build_data_exchange_measure_report`.
Conflicts are logged with job and patient context.

Placing it there is what makes the guarantee free rather than fragile. The
comment already at that line explains why the MeasureReport and the payload must
derive from the *same* list: when they diverge, a resource can ship that
`evaluatedResource` does not mention, and under the receiver's transaction
semantics one bad entry fails the whole subject. Deduping upstream of both keeps
`evaluatedResource` 1:1 with Bundle entries by construction rather than by a
second, separately-maintained rule.

Gather order is deterministic — `types_in_order`, then pagination order — so
first-wins is reproducible and testable.

Deduplication is deliberately **within** a subject, never across subjects. A
Practitioner shared by two subjects appears in both their Bundles; suppressing
the second would leave a dangling reference in a Bundle that may be processed
independently.

Base-fallback's endpoint and envelope shape are unchanged. Its payload stops
repeating duplicate `resource` parameters, which is the same defect fixed in one
place instead of two.

### Why `evaluatedResource` needs no `fullUrl`

`MeasureReport.evaluatedResource` entries are relative references
(`Observation/123`). Strictly, resolving a reference *within* a Bundle requires
`entry.fullUrl`, and our collection Bundle entries have none — the confirmed
contract does not include it.

They resolve anyway, through the server rather than within the payload. The
collection Bundle is processed by the receiver as a transaction, which stores
each entry at its own id; the relative references then resolve against the
receiver's base to exactly those resources. Adding `fullUrl` would deviate from
the confirmed shape for a property already obtained.

Recorded because it is the kind of thing a reader reopens as an oversight.

### Submission grouping

Grouping nests inside the existing chunk. Within `_process_single_batch`,
patients are processed **serially** (`orchestrator.py:576`) — `MAX_WORKERS`
bounds concurrent *chunks*, not patients within one. That is what makes grouping
simple: a group buffer needs no accumulator, no flush timer, and cannot deadlock
against the worker pool.

It also dictates where the buffer lives. One `DeqmSubmitDataWorkflow` instance
serves the whole job and is shared by all four concurrent chunks, so a buffer on
the instance would interleave subjects from different chunks and misattribute
their failures. **The buffer is per-chunk**, owned by the caller.

`SubmissionWorkflow` therefore gains a group protocol, and the orchestrator uses
it for **every** workflow — a group of one is the degenerate case, not a separate
code path:

```python
@dataclass(frozen=True)
class PreparedSubject:
    patient_id: str
    gather: GatherResult | None
    measure_report: dict | None
    resources: list[dict] | None        # filtered + deduped

@dataclass(frozen=True)
class SubjectOutcome:
    patient_id: str
    gather: GatherResult | None
    error: TransferPhaseError | None

class SubmissionWorkflow:
    @property
    def submission_group_size(self) -> int: ...   # 1 for direct_load and base-fallback
    async def prepare_patient(...) -> PreparedSubject   # raises TransferPhaseError
    async def submit_prepared(list[PreparedSubject]) -> list[SubjectOutcome]
```

The two halves fail differently on purpose. `prepare_patient` **raises**
`TransferPhaseError`, which attributes a gather failure to one subject without
poisoning the group it was being collected into. `submit_prepared` **never raises
for a subject**; it returns one outcome per subject, because a single POST's
failure has to be apportioned across N of them. The orchestrator routes both into
one `_record_transfer_failure` helper, so a failure reaches the database by the
same path whichever half produced it.

The base class's defaults defer everything: `prepare_patient` returns an
identity-only `PreparedSubject`, and `submit_prepared` loops calling
`transfer_patient`, wrapping each result or exception into an outcome.
`DirectLoadWorkflow` therefore gains **no lines at all** and behaves identically —
at size 1 its group is one patient, and the `_stop_or_delete_job` check still runs
per patient exactly where it does today. Only `DeqmSubmitDataWorkflow` overrides
the pair, and only its STU5 path ever uses N > 1.

`_process_single_batch` walks its chunk in groups of `submission_group_size`,
gathering each subject in turn and then issuing one POST per group. Outcomes carry
the `GatherResult`, so the existing per-patient bookkeeping — the `failed`
counter, `gather_failed_patients`, `partial_gather_patients`, per-patient
`MeasureResult` rows, the "Gathered N resources" log — works unchanged and reads
from one place for both paths.

`direct_load` and base-fallback report `submission_group_size == 1`, which routes
them through the default `transfer_patient` delegation: today's behavior, reached
by a different call shape. **At size 1 the STU5 path is also byte-identical to
today**: gather one subject, submit one bundle, and on failure post exactly once
(see the single-subject guard under Failure isolation). Every existing path is
therefore untouched on the wire, and the new mechanics are confined to the case
that asked for them.

**Group size is read fresh, never snapshotted.**

```python
@property
def submission_group_size(self) -> int:
    return self._group_size if self._mode == SUBMIT_DATA_MODE_STU5 else 1
```

The orchestrator reads it at the top of each group, so a downgrade returns the job
to one subject per POST for the remainder. That leaves one race worth naming: a
chunk can form a group of N and *then* have another chunk's pioneer downgrade
before it submits. `submit_prepared` closes it by re-reading the settled mode at
submit time — handed N subjects under a settled `base` mode, it submits them
individually in base form rather than building a multi-bundle envelope the server
has already refused.

**In PR 2 the group size is a constructor argument defaulting to 1** —
`DeqmSubmitDataWorkflow(..., group_size: int = 1)`, which
`build_submission_workflow` does not pass. Production is therefore size 1 by
construction and the PR is behavior-neutral; tests construct the workflow with N
directly. PR 3 threads the operator's value into that same argument, so nothing
about the interface changes between the two.

**A stop mid-group discards the buffer.** When `_stop_or_delete_job` reports a stop
after some subjects are gathered but before the group's POST, the chunk returns and
nothing is submitted. This matches what a stop already does to the remainder of a
chunk — those subjects simply never happened. Flushing the buffer first would land
data on the MCS after the operator told the job to stop.

Phase 2 (`$evaluate-measure`) remains per-patient and is unaffected.

### Failure isolation

`submit_prepared` issues one POST for the group. When that POST fails, the failure
is either about the *payload* — one subject's bad resource — or about the *server
and the connection*, and the two deserve opposite treatment.

**Payload-attributable failures isolate.**

```python
_ISOLATE_STATUS_CODES = {400, 409, 422}
```

Each subject is resubmitted alone, one bundle per call, and reported separately,
so one malformed resource fails exactly the subject that owns it — as it does
today, at the cost of one wasted round trip for the group that contained it. 409
is in the set because HAPI's `ResourceVersionConflictException` (HAPI-0550/0823)
is a per-resource verdict and has already broken this workflow once.

**Everything else does not isolate.** A 401, 403, 404, 405, 429, any 5xx, a
timeout, or a non-HTTP exception is a statement about the server or the
connection; resubmitting N times only asks a down or unauthenticated server the
same question N more times. With a chunk of 100 that turns one failed POST into
101. These fail every subject in the group with that one verdict and issue exactly
one POST. The taxonomy deliberately mirrors the reasoning already in
`_DOWNGRADE_STATUS_CODES`: a status that describes the server is never read as a
statement about a payload.

**A single-subject group never isolates.** Resubmitting the one subject it holds
would POST twice where PR 1 posts once, which would make size 1 not byte-identical
after all. This guard is what keeps `direct_load`, base-fallback, and unconfigured
STU5 on exactly today's wire behavior.

Isolation retries under the **settled** mode; it never downgrades. That rule
belongs to `_settle_mode_and_submit` alone (#414).

The two mechanisms compose in a defined order. The pioneer is now a *group*:
`_settle_mode_and_submit` takes the list, issues one POST carrying N bundles, and
returns N outcomes. Its failure is first tested for a capability signal by the
existing logic — `_DOWNGRADE_STATUS_CODES`, or a 400 whose OperationOutcome
reports an unsupported operation. If it is one, the job downgrades to
`base-fallback` and that group's subjects are submitted individually in base mode,
which is what base-fallback does anyway. If it is not, the failure is a payload
rejection and the isolation rules above apply. Capability first, isolation second;
never both. `_mode_settled.set()` stays in a `finally`, so a pioneer group that
fails outright still releases everyone waiting on its verdict.

Once a job has downgraded, `submission_group_size` becomes 1 for the remainder —
base-fallback has no multi-bundle form.

### The bundles-per-submission control

**Where the probe learns the ceiling.** The clamp below needs the server's
declared `bundle` maximum, and after PR 1 nothing retains it:
`detect_submit_data_mode` returns a bare mode string. PR 3 renames it to
`detect_submit_data_capability`, returning a frozen
`SubmitDataCapability(mode: str, bundle_max: int | None)`. The max is read off
the same `bundle` input parameter the contract match already found: `"*"`
becomes `None`, a digit string becomes an `int`, and anything else becomes
`None`. An unparseable max resolves permissive because it is not evidence of a
limit — the mode verdict is unaffected either way, so a malformed bound can
never cost a job its STU5 path.

**Effective value.** Resolved at job creation as a three-way branch, not the
`or`-shorthand formula an earlier draft of this section described:

```
candidate = 1                    if requested is None   (unspecified: conservative default)
          = CHUNK                if requested == 0      (explicit "as many as fit")
          = requested            otherwise
effective = min(candidate, server_bundle_max or INF, CHUNK)
effective = 1                    if the probed mode is not `stu5`
```

where `CHUNK` is `settings.BATCH_SIZE`. The `or`-shorthand `requested or CHUNK`
is deliberately avoided: `0` is falsy, so it would merge the "unspecified"
and "explicit unlimited" cases and silently turn every unspecified request into
a `CHUNK`-sized POST. A server advertising `bundle` `max: "1"` clamps any
request to 1 with a warning — sending what the server declared it will not
accept is not a useful experiment. The clamp is recorded in the log line, so a
silently reduced group size is explainable.

**The mode rule is not optional.** `base-fallback` (and any mode other than
`stu5`) has no multi-bundle envelope — `DeqmSubmitDataWorkflow.submission_group_size`
collapses to 1 outside STU5 from the moment the workflow is built, regardless of
what `bundle_max` the probe returned (`base-fallback` never carries one). A
clamp computed only from `bundle_max` and `CHUNK` is mode-blind: against the
bundled HAPI image, which always probes `base-fallback`, it would store
whatever the operator requested (up to `CHUNK`) while the job actually runs at
group size 1. The effective value must be forced to 1 whenever the mode is not
`stu5`, applied at job creation alongside the other ceilings; the raw request
still lands in `bundles_per_submission_requested` unchanged.

**Job record, and why there is no settings row.** Two nullable integer columns on
`jobs`, both NULL for `direct_load`:

- `bundles_per_submission_requested` — what the operator asked for, verbatim,
  `0` included.
- `bundles_per_submission` — the effective value after clamping, the way
  `submit_data_mode` already snapshots the probe verdict.

Both are added with the established lightweight pattern at `main.py:242`
(`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

Storing both is what lets the preference survive a clamp. The remembered value is
the **requested** column, so one job run against a `max: "1"` server does not
permanently rewrite the operator's choice to 1; switching back to a capable MCS
restores their intent. Reading the *effective* column instead would silently
ratchet the preference down and never let it back up.

An earlier draft of this spec persisted the preference as an `AppSetting` row
under `deqm_bundles_per_submission`. That is no longer the design. The job row
already records the value per job, `GET /jobs` already returns every job
newest-first and unpaginated, and the creation form already holds that list — so
the default is derived client-side from the most recent DEQM job, with no
settings row, no new endpoint, and no second fetch. A settings row would have
been a second source of truth for a fact the job table already carries.

**A downgrade rewrites the stored value.** A job that downgrades to
`base-fallback` mid-run submits one subject per call regardless of what was
requested, so leaving `bundles_per_submission` at the pre-downgrade number would
make the record claim something that never happened. The orchestrator already
persists a runtime downgrade to `Job.submit_data_mode`; it writes the group size
back to 1 in the same place.

**API.** `POST /jobs` accepts `bundles_per_submission: int | None` (>= 0);
a negative or non-integer value is rejected with 422. The job response returns
both the requested and the effective value.

**UI.** A numeric input on the DEQM branch of the job-creation form, defaulted
from the most recent DEQM job's requested value — or 1 when no DEQM job exists.
Not shown for `direct_load`, which has no such concept.

The helper text states the ceiling as a **rule rather than a number**: `0`
submits every subject in a processing batch, and larger values are reduced to the
batch size. `BATCH_SIZE` is a backend environment value the frontend has never
been given, and exposing it is the endpoint this design just removed. Naming the
rule also stays correct if `BATCH_SIZE` is ever retuned, which a hardcoded `100`
would not. The operator still learns the exact number when it matters: the
creation response carries the effective value, so a clamp that actually bites is
reported as a toast naming it.

**The pioneer's lock hold (PR 2 finding M6).** `submit_prepared` currently holds
`_mode_lock` across the whole of `_settle_mode_and_submit`, and that method's
downgrade and isolation paths each issue N *sequential* POSTs. At group size 1
that is one or two round trips and harmless, which is why PR 2 deferred it. This
PR is what makes larger sizes selectable, so it is also what makes the stall
reachable: a pioneer group of 100 would hold the barrier across up to 101 round
trips while the other three concurrent chunks block on it.

`_settle_mode_and_submit` therefore splits in two. `_settle_mode` runs under the
lock, issues the single pioneer POST, writes `_mode` and `_downgraded`, and
returns a decision — succeeded, downgrade-and-resend, isolate, or fail-all. The
follow-up re-sends move outside the `async with`. `_mode_settled.set()` stays in
the `finally`. Waiters still observe a fully settled mode, because the decision is
committed before the event is set and before the lock is released, so #414's
guarantee is unchanged: a job can still never be half STU5 and half base.

### Documentation and decision record

`docs/architecture.md:109-114` describes the probe as reading the
CapabilityStatement and stamping `Job.submit_data_mode`; it needs the second step,
the new criteria, and the grouping control.

Dropping support for a published-IG endpoint is a decision a future reader will
question, so it also gets an ADR in `docs/decisions.md` pointing here. Two notes
on that file: #428 is a pending fix relocating ADR-014's body out from under the
ADR-015 heading, and #420's ADR-016 is still unwritten — so take the next free
number at implementation time rather than assuming 016.

## Testing

**Detection** (`tests/test_services_fhir_client.py`, DEQM block at `:2895`).
Each case distinguishes one thing the old probe could not see:

| Case | Expected |
|---|---|
| Type-level `submit-data` with a `bundle` in-param | `stu5` |
| Instance-only (`type: false`) with a `bundle` in-param | `base-fallback` |
| Type-level, parameters `measureReport` + `resource` only | `base-fallback` |
| Retired `deqm-submit-data` advertised alone | `base-fallback` |
| Candidate with no `definition` | `base-fallback` |
| OperationDefinition unfetchable | `base-fallback` |
| `definition` on a foreign origin | not contacted; canonical search attempted |
| `/metadata` unreachable | `base-fallback` |
| `bundle` `max: "1"` | `stu5`, and the max is retained for clamping |

The retired-operation case regression-locks decision 1 — it is the test that
fails if someone restores historical support without revisiting this spec.

**Wire format.** STU5 URL assertions at `:3020` and `:3065` flip to
`Measure/$submit-data`. Base mode's instance-level URL gets its own explicit
regression test rather than being asserted only in passing. One subject produces
a payload byte-identical to today's; N subjects produce N `bundle` parameters,
each a single-subject collection Bundle.

**Deduplication** (`tests/test_services_deqm.py`). Identical duplicates collapse
with no conflict reported; differing duplicates keep the first and report the
identity; order is preserved; the empty list is handled.

**Workflow** (`tests/test_services_workflows.py`). Dedupe runs before the
MeasureReport is built; `evaluatedResource` carries one reference per identity
with no dangling references; a Practitioner shared by two subjects appears in
both Bundles, proving no cross-subject suppression; the once-per-job reporter
Organization stays resolvable.

**Grouping and isolation.** Group size 1 issues one POST per subject and is
indistinguishable from today. Group size N issues one POST per N subjects. A
group whose POST fails with a payload-attributable status resubmits each subject
individually, and only the subject owning the bad resource is marked failed — the
others are processed. A pioneer group failing with a capability signal downgrades
and does not also isolate; a pioneer group failing with a payload rejection
isolates and does not downgrade. Two chunks running concurrently do not mix
subjects into each other's submissions — the test that would catch a buffer
wrongly placed on the shared workflow instance.

Five more follow from the decisions above, each counting POSTs rather than
inspecting state, because the cost of getting these wrong is measured in round
trips:

| Case | Expected |
|---|---|
| Size-1 group fails | Exactly one POST — no isolation retry |
| Group of N fails 503 (or 401, or times out) | Exactly one POST; all N marked failed with that verdict |
| Group of N fails 400 (or 409/412) | 1 + N POSTs for a plain 400; up to 2 + 2N when the failing status is 409 or 412, since `submit_data` retries those once internally (`fhir_client.py:1342`) before the isolation error ever reaches `_submit_group` — the initial group POST and each of the N isolation POSTs can each consume its own retry. Only the owning subject(s) failed either way. |
| Group formed at N, job downgraded before its submit | N individual base-mode POSTs, no multi-bundle envelope |
| Stop requested mid-group | Zero POSTs; buffer discarded |

**Carried from PR 1's final review** (parked there because the code they cover is
rewritten here): deduplication has no coverage in base-fallback mode, and nothing
asserts that the downgrade-*rebuilt* base payload preserves it. Both land in this
PR, which rewrites the downgrade path anyway.

**Clamping.** A request above the chunk size clamps to it; a request against a
`max: "1"` server clamps to 1 and warns; `0` resolves to the chunk size. The
capability probe's max parsing gets its own cases: `"*"`, a digit string, a
missing `max`, and an unparseable one, the last three of which must not disturb
the mode verdict.

**Persistence.** Creating a job stores the request and the effective value in
their own columns; the creation form then defaults to the most recent DEQM job's
*requested* value, so a job clamped from 50 to 1 still offers 50 next time, and
choosing 1 offers 1. With no DEQM job at all the form offers 1. A `direct_load`
job leaves both columns NULL and never contributes a default.

**The narrowed pioneer lock.** A pioneer group that downgrades or isolates must
release `_mode_lock` before its re-sends — the test holds a second group at the
barrier and asserts it is admitted after the pioneer's *first* POST rather than
its last. Paired with it, the #414 regression that no job mixes modes must stay
green: the decision is committed before the event is set, and that ordering is
what the narrowing must not break.

**Regressions that this change could plausibly break**, and must stay green
untouched: #414's single-mode settlement barrier and #415's
error-`OperationOutcome`-inside-HTTP-200 rejection handling. Hardcoded
`$deqm-submit-data` URLs at `test_services_workflows.py:25,40,200` and
`test_services_orchestrator.py:990` need updating.

**Frontend.** `JobsPage.js` carries four user-facing strings naming
`$deqm-submit-data` — creation toast, badge title, `aria-label`, tooltip;
`JobsPage.workflow.test.js` matches them by regex at `:90`, `:99`, `:104`. The
new control needs its own tests: shown for DEQM only, defaulted from the setting,
0 accepted.

### Real-server coverage

The STU5 branch has never executed against any server, and **this change does not
fix that.** Bundled HAPI is pinned at `v8.8.0-1`, which does not implement the
type-level operation — `test_deqm_submit_data_workflow.py:187` asserts the probe
records `base-fallback` against it, and that assertion stays true and correct.
The STU5 end-to-end test added here is fixture-backed via mock transport, and
multi-bundle grouping is therefore also only fixture-verified.

Per decision 2, a bump to HAPI 8.10.x would be the first thing to genuinely
exercise the path, since that version advertises the contract structurally. That
is a follow-up issue.

## Implementation sequencing

Three PRs, each independently landable and independently green, in order. Each
leaves the product coherent — never half-built — so if the later ones slip,
what shipped still stands on its own.

### PR 1 — Contract

URL, detection rewrite, retired-operation removal, per-subject deduplication,
and `build_stu5_parameters` taking a list. The list has one element at every call
site, so the wire output does not change beyond the endpoint. This PR alone
satisfies #413's originally published acceptance criteria.

- [ ] A STU5 submission POSTs to `[base]/Measure/$submit-data` — no measure-id
      segment, no `deqm-` prefix.
- [ ] Its `Parameters` body carries `bundle` parameters only; no top-level
      `measureReport` or `resource` parameters appear in STU5 mode.
- [ ] Each `bundle` is a collection Bundle for one subject, containing that
      subject's MeasureReport and its gathered data of interest.
- [ ] `evaluatedResource` references correspond one-for-one with Bundle entries,
      with none dangling.
- [ ] A resource identity gathered more than once appears once in the Bundle.
      Byte-identical duplicates collapse silently; differing representations keep
      the first and log a warning naming the identity.
- [ ] A Practitioner or Organization shared by two subjects appears in both
      subjects' Bundles — deduplication never crosses subjects.
- [ ] The once-per-job reporter Organization is still PUT once and still
      resolves; it is not re-inlined per subject.
- [ ] Detection classifies `stu5` only when an advertised operation has
      `code: submit-data`, `type: true`, and a `bundle` input parameter.
- [ ] A server advertising only the retired `$deqm-submit-data` classifies
      `base-fallback`, and the probe logs why.
- [ ] A `definition` on a foreign origin is never fetched; resolution is
      attempted via `OperationDefinition?url=` on the target server.
- [ ] Detection still never raises and never blocks job creation; every
      unconfirmable case resolves to `base-fallback`.
- [ ] Base-fallback's endpoint and envelope are unchanged, with regression
      coverage for #414 single-mode settlement and #415 OperationOutcome rejection.
- [ ] Comments and docs distinguish the maintainer-selected contract from
      published STU5 and from the current draft.

### PR 2 — Grouping

The group protocol, per-chunk buffer, failure isolation, and the downgrade
interaction. No user-facing control yet: group size is a constructor argument
defaulting to 1 that `build_submission_workflow` does not pass, so this PR is
behavior-neutral by construction and its value is that the mechanics land under
test before anything can select them. No DB column, no API field, no frontend —
those are all PR 3.

- [ ] At group size 1 the STU5 path is byte-identical to PR 1 — one POST per
      subject, one `bundle` parameter.
- [ ] At group size N a single POST carries N `bundle` parameters, one per
      subject.
- [ ] A group whose POST fails resubmits each subject individually; only the
      subject owning the bad resource is marked failed and the rest are processed.
- [ ] A pioneer group failing with a capability signal downgrades the job and
      does **not** also isolate.
- [ ] A pioneer group failing with a payload rejection isolates and does **not**
      downgrade.
- [ ] Isolation retries under the settled mode and never downgrades.
- [ ] Two chunks running concurrently never mix subjects into each other's
      submissions.
- [ ] Per-patient accounting is unchanged: `processed`/`failed` counters,
      `gather_failed_patients`, `partial_gather_patients`, and per-patient
      `MeasureResult` rows all still reflect individual subjects.
- [ ] `direct_load` and `base-fallback` report group size 1 and keep using
      `transfer_patient` unchanged.
- [ ] Phase 2 `$evaluate-measure` remains per-patient.
- [ ] A group failure that is not payload-attributable — 401, 403, 404, 405, 429,
      any 5xx, a timeout — fails every subject in the group with that one verdict
      and issues exactly one POST.
- [ ] A single-subject group never isolates: its failure posts once, not twice.
- [ ] `submission_group_size` is read per group, not snapshotted, so a downgrade
      returns the job to one subject per POST for the remainder.
- [ ] A group formed at size N but submitted after the job downgraded is submitted
      individually in base mode.
- [ ] A stop requested mid-group discards the buffer and submits nothing.
- [ ] The orchestrator's per-patient failure persistence is one helper, reached
      identically from a `prepare_patient` raise and a failed `SubjectOutcome`.
- [ ] Deduplication is covered in base-fallback mode and survives the
      downgrade-rebuilt base payload (carried from PR 1's review).

### PR 3 — The user-facing control

The probe's capability return, the two `jobs` columns and their migration, the
API field, clamping, the M6 lock narrowing, and the UI.

- [ ] The job-creation form shows a **Bundles per submission** numeric input on
      the DEQM workflow branch only; `direct_load` does not show it.
- [ ] The input defaults to the most recent DEQM job's requested value, or **1**
      when no DEQM job exists.
- [ ] The remembered value is the one the operator *requested*, not the clamped
      result: after a job whose 50 was clamped to 1, the form still offers 50.
- [ ] Helper text states that `0` means every subject in a processing batch and
      that larger values are reduced to the batch size. It does not hardcode the
      batch size itself.
- [ ] `POST /jobs` accepts `bundles_per_submission` as a non-negative integer;
      a negative or non-integer value is rejected with 422.
- [ ] `0` is accepted and resolves to the processing-chunk size
      (`settings.BATCH_SIZE`).
- [ ] The effective value resolves the three-way candidate (1 if unspecified,
      CHUNK if an explicit 0, else the raw request) against `min(candidate,
      server bundle max, CHUNK)`, then forces the result to 1 whenever the
      probed mode is not `stu5` — base-fallback has no multi-bundle envelope,
      so a job in that mode always runs at group size 1 regardless of what was
      requested or what `bundle_max` allowed. Any clamp that reduces the
      request is logged with its reason.
- [ ] `detect_submit_data_capability` returns the declared `bundle` max alongside
      the mode: `"*"` and any unparseable value resolve to unbounded, a digit
      string to that integer.
- [ ] A server advertising `bundle` `max: "1"` clamps the group to 1 and still
      classifies `stu5`.
- [ ] `jobs.bundles_per_submission` stores the effective value and
      `jobs.bundles_per_submission_requested` the raw request, both `NULL` for
      `direct_load`; the columns are added idempotently via the `main.py:242`
      pattern.
- [ ] The job API response returns the value the job actually used. A later job
      created with a different value does not rewrite an existing job's record.
- [ ] A job that downgrades to `base-fallback` submits one subject per call
      regardless of the stored value, and its stored effective value is rewritten
      to 1 where the runtime downgrade is already persisted.
- [ ] The pioneer releases `_mode_lock` before its follow-up re-sends: a
      downgrading or isolating pioneer group of N holds the barrier for one POST,
      not N.
- [ ] #414's guarantee still holds under the narrowed lock — no job submits some
      subjects as STU5 and others as base.
- [ ] The control is absent from, and has no effect on, `direct_load` jobs.

## Rejected alternatives

**A third mode for the retired operation.** Keeping `$deqm-submit-data` as its own
mode alongside `stu5` and `base-fallback` would preserve published-STU5 support
explicitly. Rejected: a new `jobs.submit_data_mode` value, a new badge state, and
a third payload path to maintain and test — for a server shape Lenny has never
successfully talked to. Dropping it is also the honest option, since the
alternative is claiming support we cannot demonstrate.

**Endpoint failover inside one mode.** Try `$submit-data`, fall back to
`$deqm-submit-data`, then downgrade to base. Rejected: a second settlement step
inside the #414 pioneer barrier, which is the concurrency code that has already
cost two PRs.

**Requiring a DEQM marker before classifying `stu5`.** Demanding the DEQM canonical
or an IG declaration alongside the structural checks would keep ordinary HAPI in
`base-fallback`. Rejected: it would leave the STU5 path unexercised against every
server available to us, and it is wrong on the merits — if a server accepts the
contract, the contract works there.

**Renaming the mode.** `stu5` is now a misnomer: it names a published spec this
contract deliberately departs from. Rejected for this change: renaming costs a
migration on `jobs.submit_data_mode`, an API-shape change, and frontend copy
churn, none of which serve #413. The inaccuracy is documented here instead.

**Deduping in the gather layer.** Would fix `direct_load` too. Rejected: it changes
behavior for workflows this issue does not scope, in the data-acquisition
strategies that #397 and #409 have already churned.

**Failing the patient on a content conflict.** Loud and unambiguous. Rejected: a
transient pagination race would fail a whole subject's submission for data that
would have evaluated identically either way.

**Failing every subject in a failed multi-bundle submission.** Simplest, and
truthful about what the server did. Rejected: a single dangling reference (#409's
live failure mode) would fail N subjects that would each have succeeded alone,
with no indication which one was at fault. Batching must not trade correctness
for throughput.

**Unbounded "unlimited".** Letting `0` mean every subject in the *job* rather than
the chunk. Rejected: it would require a DEQM-specific path around the `Batch` row
model, per-chunk progress, and retry, and would build one JSON body holding every
patient's data of interest — an untested memory profile on a pipeline with prior
OOM history at 319 patients.

**An `AppSetting` row for the remembered bundles-per-submission.** The key-value
table already holds `groups_enabled`, so a `deqm_bundles_per_submission` row was
the obvious home, and this spec originally specified it. Rejected: the job row
must record the value anyway, `GET /jobs` already returns every job newest-first,
and the creation form already holds that list — so the settings row would be a
second source of truth for a fact the job table carries, plus an endpoint to read
it back. Deriving the default from the most recent DEQM job costs no new API
surface and cannot drift from what the jobs actually did.

**Remembering the clamped value rather than the request.** One column instead of
two. Rejected: a single job against a `max: "1"` server would rewrite the
operator's preference to 1 permanently, and nothing would ever raise it back —
the preference would ratchet down across MCS connections that have nothing to do
with each other.

**Naming `BATCH_SIZE` in the helper text.** What the acceptance criteria
originally asked for. Rejected: the frontend has never been given that value, and
exposing it means the endpoint this design removed. A hardcoded `100` would also
go quietly wrong the first time `BATCH_SIZE` is retuned. The text states the rule;
the creation response names the number when a clamp actually bites.

**Capping the pioneer group at one subject.** An alternative M6 fix: make the
first group always size 1 so the barrier is inherently short. Rejected: it costs
an extra POST on every job and leaves the first group behaving differently from
every other, which is a wrinkle each future reader has to learn. Narrowing the
lock removes the stall without introducing a special case.

**A shared buffer on the workflow instance.** Simpler to write. Rejected: one
instance serves all four concurrent chunks, so it would interleave subjects
across chunks and misattribute their failures.

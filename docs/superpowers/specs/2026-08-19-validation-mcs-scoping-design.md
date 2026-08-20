# Design: MCS-scoping the validation pipeline (issue #397, slice 3)

**Status:** implemented on `fix/397-validation-mcs-scoping` (v0.0.22.0); see ADR-014 in `docs/decisions.md` for the as-built record and the limitations that shipped open
**Date:** 2026-08-19
**Branch:** `fix/397-validation-mcs-scoping` (off `main` @ `b10621b`)
**Predecessors:** ADR-011 (#393), ADR-012 (#392), ADR-013 (#397 slices 1–2)

## Problem

Issue #397 lists nine `settings.MEASURE_ENGINE_URL` reads in `validation.py`. The
real number of unscoped measure-engine interactions is **sixteen**, because seven
of them do not read the env var at all — they call a helper whose target parameter
has a back-compat default:

| Kind | Count | Sites |
|---|---|---|
| Direct `settings.MEASURE_ENGINE_URL` reads | 9 | 566, 587, 603, 719, 878, 988, 1003, 1239, 1359 |
| `push_resources(...)` with no `target_url` | 5 | 776, 779, 1060, 1063, 1254 |
| `evaluate_measure(...)` with no `measure_engine_url`/`auth_headers` | 2 | 1298, 1345 |

Only the CDR push at 832 names its target explicitly.

The pipeline therefore has **no MCS awareness whatsoever**. It is internally
consistent today only because every path defaults to the same local container,
which is why validation appears to work: it silently ignores the connection the
user selected.

### Why this is not just a scoping cleanup

**`run_validation:1239` is a second copy of the #392 destructive wipe.**

```python
await wipe_patient_data(base_url=settings.MEASURE_ENGINE_URL, strict=False)
```

Unfiltered — it deletes every patient on the target. #392 fixed exactly this in
`run_job` and never touched the validation path. It is harmless *today* only
because it hits Lenny's own container. **A naive fix that re-points it at the
active MCS would activate the #392 bug on the validation path**, deleting every
participant's data on a shared connectathon server. This is the trap #397's own
text warned about for `wipe_measure_definitions`.

### Why partial fixes are worse than none

Scoping the writes but not the evaluation makes validation push test data to a
remote MCS and grade against the local engine. The result is not an error — it is
confidently wrong pass/fail numbers. That is ADR-011's bug in a new place. Slice 3
is therefore close to atomic: it cannot be split by call site.

### Known gap in the existing guard

`tests/test_mcs_scoping_inventory.py` (shipped in #402) counts AST attribute
accesses. It cannot see a call that relies on a defaulted parameter, so it misses
7 of the 16 sites here. It was described in its own docstring as pinning the bug
class; that claim is currently too strong. Fixing it is in scope (§6).

## Decisions

Two were settled by the maintainer before this spec was written:

- **D2 — the validation wipe reuses `MCSConfig.wipe_before_job`.** Scoped by
  default (delete only the run's own test patients), full sweep only where the
  connection owner opted in. One flag governs every pre-run wipe, so "what does
  Lenny delete before it runs?" has a single answer.
- **D3 — `validation_runs` snapshots its MCS.** Mirrors `Job`. Validation's whole
  output is a correctness claim; a correctness claim with no record of which
  server produced it is hard to trust or debug.

A third was settled during design review:

- **Threading uses one required value object (`McsTarget`), not per-value
  parameters and not ambient context.** A contextvar was considered and rejected:
  it recreates the exact failure mode this issue is about, a helper acquiring a
  target from invisible state.

## Design

### 1. `McsTarget` value object

A frozen dataclass, constructed once per entry point:

```python
@dataclass(frozen=True)
class McsTarget:
    url: str
    auth_headers: dict[str, str]
    is_read_only: bool
    wipe_before_job: bool
```

**Home: `app/services/fhir_client.py`**, and the direction matters. `dependencies.py`
imports `fhir_client` (for `_build_auth_headers`), so `fhir_client` must not import
`dependencies` — putting `McsTarget` next to `ConnectionContext` would create a
cycle. The dataclass therefore lives in `fhir_client` beside the functions that
consume it, and `dependencies` (which may import it freely) provides the bridge
from a `ConnectionContext`.

It deliberately carries resolved `auth_headers` rather than raw credentials, so no
helper deep in the pipeline re-derives them, and the flags travel with the target so
no helper re-queries the database.

`ConnectionContext` already carries url/credentials/`is_read_only`/`wipe_before_job`
but holds *unresolved* credentials and is FastAPI-dependency-shaped. `McsTarget` is
what crosses into the pipeline.

### 2. Target resolution at the entry points

Three entry points resolve the active MCS once and thread the result down. No
helper resolves anything itself.

| Entry point | Callers | Notes |
|---|---|---|
| `triage_test_bundle` | `validation.py:930` (upload), `bundle_loader.py:88` (seed) | takes `mcs: McsTarget` as a required keyword arg |
| `run_validation` | `worker.py:172` | resolves from the run's **snapshot** (§3), not the active connection |
| `_reload_measures_from_seed_bundles` | internal | takes the target from its caller |

Helpers gaining a required `mcs: McsTarget` parameter:

- `_prepare_measure_support_resources` (covers sites 635, 648, 661, 673)
- `_find_existing_valueset_id` — already has a partial `target_url` kwarg; it is
  replaced, not supplemented, so there is one way to say where to look
- `_find_existing_codesystem_id`
- `_delete_existing_valueset` — **destructive**: deletes ValueSets, so on a shared
  engine this removes another participant's terminology
- `_assert_no_canonical_url_clash`
- `_resolve_measure_id`

Sites 1254 (`gather_and_push`) and 1345 (`evaluate_and_compare`) are closures
defined *inside* `run_validation`; they capture the target from enclosing scope and
need no signature change. Site 1298 is directly in `run_validation`.

The five `push_resources` and two `evaluate_measure` calls pass `target_url=` /
`measure_engine_url=` and `auth_headers=` from the target explicitly. This is the
part the current inventory guard cannot see, and the part that makes the pipeline
actually consistent.

### 3. Snapshot and migration

`validation_runs` gains, mirroring `Job`:

- `mcs_id` — `Integer`, FK to `mcs_configs.id`, `ON DELETE SET NULL`, indexed
- `mcs_url` — `String(1024)`, nullable
- `mcs_name` — `String(512)`, nullable
- `mcs_auth_type` — `String(32)`, nullable
- `mcs_wipe_before_job` — `Boolean`, `NOT NULL DEFAULT FALSE`

`mcs_auth_type` is not decorative: because `mcs_id` is `ON DELETE SET NULL`, a NULL
id means either "this run never had a config" or "the config was deleted after
creation", and the snapshotted auth type is the only thing that distinguishes them.
Without it, a run whose connection was deleted would execute unauthenticated
against a still-snapshotted URL. Same reasoning as `Job.mcs_auth_type` (ADR-011).

Populated when the run row is created (the route that enqueues it), from the
active MCS. `run_validation` reads the snapshot, so a run that sits in the queue
while the user switches connections still executes against the server it was
created for.

**Credentials are not snapshotted** — resolved live from `mcs_id`, per ADR-011.
This requires generalising the existing helper: `resolve_job_mcs_auth_headers`
(added in slice 2) does `session.get(Job, job_id)` and therefore cannot serve a
`ValidationRun`. It becomes:

```python
async def resolve_mcs_auth_headers(
    session, *, mcs_id, mcs_url, mcs_auth_type, owner_label: str
) -> dict[str, str]
```

taking the three snapshot fields rather than a row id. `resolve_job_mcs_auth_headers`
stays as a thin `Job`-shaped wrapper so slice 2's call sites are untouched, and the
validation path calls the generalised form. `owner_label` (e.g. `"job 12"`,
`"validation run 4"`) only shapes the error message. The point is that the
URL-drift guard — which decides whether to hand a config's credentials to a
snapshotted host — continues to exist in **exactly one** place; a second copy for
validation is precisely the rot ADR-013 called out.

**`is_read_only` is read live, not snapshotted.** It answers "may I write to this
server *now*", which is a current property of the connection, not a historical fact
about the run. Snapshotting it would let a run write to a server the user has since
marked protected. This is the one field that deliberately does not follow the
snapshot pattern, and §5 depends on it.

Migration: idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for the three
nullable columns, plus a column-absence-guarded block for
`mcs_wipe_before_job` so any backfill runs **exactly once**. That guard is the
lesson from #401 — an unguarded `UPDATE` re-asserts itself on every restart and
silently undoes a user's choice.

Legacy rows (NULL `mcs_url`) fall back to `settings.MEASURE_ENGINE_URL`, matching
`orchestrator._get_mcs_url` and the two route fallbacks from slice 2.

### 4. The wipe

Replaces site 1239:

```python
patient_ids = [er.patient_ref for er in resolved_expected_results]
```

`resolved_expected_results` is computed before the wipe point, and `patient_ref`
is a bare FHIR id (it is passed directly as `patient_id` to
`gather_patient_data`), so no prefix handling is needed.

- `mcs_wipe_before_job` false (default) → `wipe_patients_by_id(...)` with those ids
- true → `wipe_patient_data(...)` as today, against the snapshotted target

A run with zero resolved expected results wipes nothing, consistent with #392's
zero-patient job behavior.

### 5. Read-only

A validation run must write Measures, Libraries, ValueSets, and patient data. It
cannot function against a read-only connection, so it **refuses at the start**
rather than failing partway through having already written some resources.

Shape follows the existing precedent in this same file (`validation.py:827`),
which raises `ValueError` for a read-only CDR; `run_validation` turns that into a
failed run with a clear `error_message`. `triage_test_bundle` refuses the same way.

The check reads the **live** connection (see §3), so a run queued against a
then-writable engine still refuses if the connection was marked read-only before it
executed. That is the intended ordering: the flag exists to protect a server, and
the protection should apply at the moment of writing, not the moment of queueing.

**This is a behavior change** for anyone who has marked their measure engine
read-only and still expects validation to run. It was flagged at design review as
the section most worth disagreeing with and was accepted.

### 6. Inventory guard fix

Extend `tests/test_mcs_scoping_inventory.py` to also flag *defaulted-parameter
reliance*: an `ast` pass over calls to `push_resources`, `evaluate_measure`, and
`resolve_evaluated_resource` that omit their target keyword. Pin the allowed count
per file the same way, with reasons.

Without this, the guard keeps reporting a clean inventory while seven unscoped
sites sit in plain sight — and its docstring currently claims more than it delivers.

## Error handling

| Condition | Behavior |
|---|---|
| Active MCS is read-only | Refuse before any write; run marked failed with a clear reason |
| MCS config deleted after run creation | `resolve_job_mcs_auth_headers` raises; run fails rather than running unauthenticated |
| Config URL changed since snapshot | Same guard raises — credentials are never sent to a host the snapshot does not name |
| Legacy run, NULL `mcs_url` | Falls back to the env-var engine |
| Wipe 401/403 | `wipe_patients_by_id` already aborts loudly; the run fails rather than grading against stale data |

## Testing

**Unit**
- One test per entry point asserting **every** downstream measure-engine call
  received the same server — the anti-divergence guard, since divergence is the
  silent-wrong-answer mode.
- Read-only refusal for `run_validation` and `triage_test_bundle`, asserting no
  push happened.
- Both wipe modes; zero-patient run wipes nothing.
- Snapshot written on run creation; legacy NULL falls back.
- Each helper rejects a missing `mcs` (TypeError), mirroring slices 1–2.
- `resolve_mcs_auth_headers` generalisation: the existing `Job` tests must still
  pass unchanged (proving the wrapper preserves slice 2's behavior), plus the
  URL-drift guard fires for a `ValidationRun` snapshot the same way.
- Read-only checked at execution time, not queue time: a run queued while writable
  and executed after the connection was marked read-only must refuse.
- Extended inventory guard, mutation-verified: a call with its target keyword
  removed must fail it.

**Integration**
- A validation run against a second MCS leaves a bystander patient intact
  (`test_scoped_wipe.py` shape).
- A validation run's results are attributable to the snapshotted MCS.

**Pre-push checklist:** this touches `validation.py`, so beyond lint + unit +
CI-equivalent integration it needs `test_full_workflow.py`, and per CLAUDE.md's
decision tree the golden/connectathon suites are in scope for a measure-pipeline
change.

## Out of scope

- **`bundle_loader.py:32` (`_wait_for_hapi`) is reclassified as NOT a defect.**
  It probes both local containers for readiness at boot, before any connection
  row exists. #397 lists it as a defect; that appears to be a
  miscategorisation. The real bundle_loader issue is `:88` passing no target to
  `triage_test_bundle`, which §2 fixes. Seeding *should* target Lenny's own
  engine — the fix makes that explicit rather than accidental.
- **Issue #399** (factory reset hangs). Slice 1 changed that code path, so #399
  should be re-characterised against current `main` before being worked.
- **Independent adversarial review.** Neither slice 1–2 nor this design has had a
  cross-model pass; Codex is not installed on this machine.

## Risks

1. **Largest slice, most call sites.** 16 sites across 3 entry points and 6
   helpers. The anti-divergence tests exist because a missed site produces wrong
   numbers rather than an error.
2. **Carries a migration.** #401's migration was the highest-risk part of that
   deploy. Same one-shot-backfill guard applies.
3. **Read-only refusal is user-visible** and could surprise someone relying on
   current behavior.
4. **Golden/connectathon suites are slow** (600+ patient tests) but are the real
   check that validation still produces the same numbers after re-targeting.
   Expect a long pre-merge run.

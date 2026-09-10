# Measure readiness indicator on the Measures page

**Issue:** #434 · **Supersedes:** #296 (closed as superseded) · **Date:** 2026-09-10

## Problem

A measure listed on the Measures page may not be evaluable on the active MCS. The
Measure resource can be present while the Libraries its CQL includes, or the
ValueSets those Libraries reference, are not. Nothing surfaces the difference
until a job runs, and then it fails for every patient identically.

Observed 2026-09-10. Job 7 — `CMS122FHIRDiabetesAssessGreaterThan9Percent`,
`direct_load`, 56 patients — against a newly added MCS
(`https://fhir-connectathon.test.cms.gov/fhir`). Result: `status=failed`,
`processed_patients=0`, `failed_patients=56`. Every patient carried the same
error:

```
HTTP 200: Exception for library: CMS122FHIRDiabetesAssessGreaterThan9Percent,
Message: Could not load source for library Status, version 1.15.000, namespace uri null.
```

Lenny reported this correctly — `evaluate_measure`'s HTTP-200-with-`OperationOutcome`
guard did its job. The defect is *when* the user finds out, not whether.

Measured gap on that server: 7 Libraries present; four of CMS122's seven includes
absent (`Status 1.15.000`, `AdvancedIllnessandFrailty 1.27.000`, `Hospice 6.18.000`,
`PalliativeCare 1.18.000`); 21 of the 23 ValueSets in the dependency closure
absent; all 18 referenced CodeSystems absent.

The failure is deterministic and knowable before the first patient: a measure whose
Library graph does not resolve fails for every subject. Running 56 evaluations to
discover it wastes ~60s and, worse, presents a content error as 56 per-patient
clinical failures — which reads as a data problem, not a server-content problem.

## Goal

Show, on the Measures page, whether each listed measure is completely defined on
the active MCS, before the user starts a job.

## Non-goals

- **Uploading or repairing missing content.** Explicitly out of scope. The
  indicator reports; repair is a manual operation via
  `scripts/copy_measure_content.py`.
- **Gating job creation.** No warning and no refusal on the job path. Deferred to
  a possible follow-up once the check is proven on one surface.
- **Checking that ValueSets expand to a non-empty set.** Presence only — see
  "Rejected alternatives".

## Definition of ready

A measure is **ready** when both hold on the active MCS:

1. `$data-requirements` on the Measure returns successfully — the server could
   resolve and compile the whole Library graph.
2. Every ValueSet canonical named in that response exists on the MCS.

## Design

### Deriving the dependency closure

The server computes it. `Measure/{id}/$data-requirements` returns a `Library`
whose `dataRequirement[].codeFilter[].valueSet` and `relatedArtifact[]` entries
name the ValueSet canonicals. No CQL is parsed anywhere in Lenny.

Verified against the local measure engine: CMS122 returned 23 ValueSet canonicals,
identical to the 23 derived by hand from the CQL's `include` graph.

The same call is the compile check — it is what fails, loudly, when a Library is
missing (`HAPI-0389: ... Could not load source for library Status, version
1.15.000`).

### States

Four, rendered as three icons.

| State | Icon | Meaning |
|---|---|---|
| `ready` | good | Compiled; every ValueSet present |
| `not_ready` | bad | The server gave a definite negative answer |
| `checking` | ? / spinner | A sweep is in flight for this row |
| `unknown` | ? | Never checked, **or the check itself could not complete** |

**`unknown` is distinct from `not_ready` on purpose.** `$data-requirements`
measured at **6–11 s** per measure against the local engine (11.4 s for CMS122),
while `MCSConfig.request_timeout_seconds` defaults to **30**. A slower or emulated
server will exceed it. If a timeout rendered as red, one slow server would mark
every measure broken and the indicator would become noise. Only an answer *from
the server* turns a row red; a transport failure, timeout, 401 or 403 yields
`unknown` with the reason recorded.

### Check algorithm

Per measure, read-only throughout:

1. `GET {mcs}/Measure/{id}/$data-requirements` — **no period parameters**
   - Non-2xx, or 2xx carrying an error/fatal `OperationOutcome` → `not_ready`,
     storing the server's diagnostic verbatim.
   - Transport error, timeout, 401, 403 → `unknown`, storing the reason.

   The period is deliberately omitted. Whether the Library graph resolves does not
   depend on a measurement period, and `_get_data_requirements`
   (`services/fhir_client.py:434`) already calls the operation bare on the DEQM
   path, so the parameterless form is proven against HAPI. Note the two existing
   call sites disagree — `probe_mcs_data_requirements` hardcodes
   `periodStart=2024-01-01&periodEnd=2024-12-31`. This spec follows the DEQM path.

2. Collect ValueSet canonicals from the returned Library (version suffixes
   stripped at `|`). Query the MCS for their presence, chunked.
   - Any absent → `not_ready`, storing the missing list.
   - The presence query itself failing → `unknown`.
3. Otherwise → `ready`.

Missing Libraries are reported from the step-1 diagnostic rather than enumerated
independently: the CQL engine names only the *first* unresolvable include, so the
stored text is the server's own message, not a claim of completeness. This is
worth stating in the UI copy — fixing the named Library may reveal another.

### Data

One new table, `measure_readiness`:

| Column | Notes |
|---|---|
| `id` | PK |
| `mcs_id` | FK → `mcs_configs.id`, `ON DELETE CASCADE` |
| `measure_id` | FHIR resource id on that MCS |
| `measure_version` | In the key, so a re-published measure re-checks |
| `state` | enum: `ready` / `not_ready` / `checking` / `unknown` |
| `missing_libraries` | JSON, nullable |
| `missing_valuesets` | JSON, nullable |
| `error` | Text, nullable — the server diagnostic or the transport failure |
| `duration_ms` | Integer, nullable |
| `checked_at` | Timestamp, nullable |

Unique on `(mcs_id, measure_id, measure_version)`.

This repo has no alembic. The table is added the way every other schema change
here is: `Base.metadata.create_all` covers fresh databases, and
`main.py::_run_schema_migrations` carries the `CREATE TABLE IF NOT EXISTS` for
existing ones.

### Orchestration

`POST /measures/readiness/refresh` → `202`, following the precedent in
`routes/settings.py` for factory-reset and reseed: mark the target rows
`checking`, commit, then `asyncio.create_task` the sweep.

- **Concurrency capped at 2** via `asyncio.Semaphore`. Deliberate:
  `services/fhir_client.py:371-375` records `$data-requirements` OOM-killing the
  measure engine when called per patient at 319 patients — which is why that call
  site memoises behind a lock. A shared connectathon server deserves the same
  restraint. At ~10 s per measure, 9 measures complete in roughly 45–60 s.
- **A dedicated timeout**, default 60 s, configured separately from
  `request_timeout_seconds` — the measured 11 s cost needs headroom, and raising
  the connection-wide timeout to suit this one operation would slow every other
  failure path. (Worth noting in passing: `_get_data_requirements` hardcodes 30 s
  for the same operation on the DEQM path. Not changed here, but 11 s measured
  against a 30 s ceiling is thinner headroom than it looks.)
- `asyncio.create_task` does not survive a restart. Rows left `checking` by a
  crash are reclaimed to `unknown` at startup, mirroring how `main.py` already
  reclaims stranded jobs.

**Triggers**, all event-driven — no TTL, since content on a FHIR server does not
rot on a timer:

- MCS activation
- MCS URL edit
- Measure upload / delete
- Manual "Re-check" control
- Measures-page load, for measures with no cached row

### API

`GET /measures` gains a `readiness` object per measure. Additive; existing
consumers are unaffected.

```json
{
  "id": "CMS122FHIRDiabetesAssessGreaterThan9Percent",
  "readiness": {
    "state": "not_ready",
    "checked_at": "2026-09-10T18:04:11Z",
    "missing_libraries": ["Status 1.15.000"],
    "missing_valuesets": ["http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.1003"],
    "error": "Could not load source for library Status, version 1.15.000, namespace uri null."
  }
}
```

The endpoint stays a live proxy to the MCS for the measure list itself; readiness
is a left join from the cache, never a blocking call.

### UI

`MeasuresPage.js`: an icon per row. Hover or expand reveals the missing Libraries
and ValueSets, or the server's error text. A page-level "Re-check" control. While
any row is `checking`, the page polls `GET /measures` and repaints; polling stops
when none are.

## Testing

- **Unit** — the check logic against mocked MCS responses: all four states, and
  specifically that a timeout produces `unknown` rather than `not_ready`; that a
  2xx carrying an error `OperationOutcome` produces `not_ready`; that a
  `warning`-severity outcome does not.
- **Unit** — the sweep: concurrency cap respected, `checking` rows reclaimed at
  startup, cache keyed on version so a version bump re-checks.
- **Integration** (local prebaked stack) — all 9 seeded measures resolve to
  `ready`.
- **Regression fixture** — CMS122 against an MCS missing `Status` must render
  `not_ready` and name `Status 1.15.000`. Job 7 is the real-world instance.
- **Frontend** — `MeasuresPage.test.js`, one case per rendered state plus the
  hover detail.

## Rejected alternatives

**Checking that ValueSets `$expand` to a non-empty set.** Would catch a ValueSet
present as an empty shell, which yields green-but-wrong population counts. Costs
20–30 extra calls per measure, `$expand` is slow and not universally supported,
and an empty expansion can be legitimate. Presence is the 90% signal at a fraction
of the cost.

**Compile-only, skipping the ValueSet check.** One call per measure, much cheaper.
Rejected because it misses exactly the larger half of the observed gap — 21 of 23
ValueSets absent on the connectathon server.

**Modelling the sweep as an `AdminOperation`.** Reuses the existing
operation-tracking table and its polling endpoint, but that table means
"destructive admin action" today, and per-measure verdicts would still need their
own storage. Per-row `state` gives row-level progress without a second object.

**Synchronous check on page load, no persistence.** No table, no migration. At
6–11 s per measure it blocks the page, re-runs on every visit, loads a shared
server continuously, and loses every verdict on restart.

**A TTL on cached verdicts.** Considered for the case where someone changes a
shared server's content behind Lenny's back. Rejected for now: it spends a 60 s
sweep on a clock rather than on a reason, and the manual "Re-check" control covers
the case. Revisit if stale verdicts are observed in practice.

# DEQM STU5 submission contract: type-level `Measure/$submit-data` with one `bundle`

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

**The maintainer selected a concrete contract.** On 2026-09-14: type-level
`POST [base]/Measure/$submit-data` carrying exactly one `bundle` parameter,
preserving the existing single-subject collection Bundle. This is an explicit
application contract. It is neither the published STU5 endpoint nor a mandate of
the current draft, and must not be described as either.

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

Submit under the maintainer-selected contract, and select that mode only for
servers that demonstrably implement it.

## Non-goals

- Migrating to the draft's batch/transaction submission guidance.
- Multi-measure aggregation, or any change to job orchestration. One measure at
  a time satisfies #413.
- Combining subjects. One `bundle` parameter, one collection Bundle, one subject.
- Cross-Bundle deduplication. A Practitioner or Organization shared across
  subjects may legitimately appear in more than one subject's Bundle.
- Re-inlining the reporter Organization per patient. It is PUT once per job and
  referenced; inlining it is what caused the `ResourceVersionConflictException`
  storm the 2026-08-21 spec fixed.
- Bumping bundled HAPI. See *Real-server coverage* below — it is the natural
  follow-up, not part of this change.

## Decisions

Four decisions were taken with the maintainer on 2026-09-14, before any code was
designed. Each is load-bearing; the *Rejected alternatives* section records what
was given up.

1. **Two modes, not three.** `stu5` means the selected contract only. A server
   advertising only the retired `$deqm-submit-data` resolves to `base-fallback`.
   Support for the historical endpoint is dropped, not preserved under another
   name.
2. **Detection is purely structural.** A type-level `submit-data` accepting a
   `bundle` input is classified `stu5` whether or not the server says anything
   about DEQM. Capability is the contract. A plain HAPI 8.10.x therefore
   classifies `stu5`, which is correct — the contract does work there.
3. **Deduplication happens once, before the MeasureReport is built.** Not in the
   STU5 builder, not in the gather layer.
4. **First-wins on conflict, with a warning.** Byte-identical duplicates
   collapse silently; differing representations of one identity keep the first
   and are reported.

## Design

### Capability detection

`detect_submit_data_mode` keeps its signature, its `stu5` / `base-fallback`
return values, and its hard guarantee that it never raises and never blocks job
creation. The algorithm inside becomes two steps.

**Step 1 — candidates.** `GET {mcs_url}/metadata`. Collect operations from
`rest[].operation[]` and from `rest[].resource[type=Measure].operation[]`, keep
those whose `name` is `submit-data`.

**Step 2 — confirmation.** For each candidate, dereference its `definition` and
require all three of:

| Check | Why it is not optional |
|---|---|
| `code == "submit-data"` | The invocation is `$submit-data`; `code` is the field that governs it. |
| `type == true` | The contract is type-level. An instance-only operation does not answer `POST Measure/$submit-data`. |
| an `in` parameter named `bundle` | Distinguishes the selected contract from base-only servers offering `measureReport` + `resource`. |

Any candidate satisfying all three → `stu5`. Otherwise → `base-fallback`.

At most 3 candidates are dereferenced, so a pathological CapabilityStatement
cannot fan out into unbounded requests.

**Dereferencing is not a blind fetch.** `definition` is a URL supplied by a
remote server, so:

- Same origin as `mcs_url` → `GET` it directly.
- Different origin → resolve it the FHIR way, `GET {mcs_url}/OperationDefinition?url={canonical}`,
  and read the first entry.

A foreign origin is never contacted. That is an SSRF guard first, and it also
keeps the probe correct for an air-gapped MCS whose OperationDefinitions cite
`hl7.org` canonicals it hosts locally.

**Unconfirmable means `base-fallback`.** If the candidate carries no
`definition` at all, or its OperationDefinition cannot be fetched or parsed, the
contract is not proven and the verdict is base. This is
the safe direction: base-fallback is the empirically verified path against
HAPI, whereas a false `stu5` costs the job a pioneer-patient round trip before
`_settle_mode_and_submit` downgrades it.

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
one" docstring more load-bearing, not less. Its STU5 rationale changes: the
reason is no longer that published STU5 declares `instance: false`, but that the
selected contract is type-level. The base-mode rationale is unchanged and stays
empirical — HAPI's clinical-reasoning module does not register the type-level
operation, so base mode must target the instance.

### Dropping the retired operation

`_DEQM_SUBMIT_DATA_CANONICAL` and `_DEQM_SUBMIT_DATA_OP_NAME`
(`fhir_client.py:1054-1055`) are removed. In their place, a comment records that
the operation was retired upstream in March 2026, links the three HL7 commits,
and states that it is deliberately not matched. Without that note the next
reader re-derives the history and "fixes" the code back — the loop this issue
has already been through twice.

Because the consequence is user-visible, it is not left silent: when a server
advertises only the retired operation, the probe logs one `info` line saying so.
An operator then gets a real explanation for the base-fallback badge rather than
an unexplained downgrade.

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

The call site is `workflows.py:276`, immediately after the existing
`filtered_resources` filter and before `build_data_exchange_measure_report`.
`transfer_patient` logs any reported conflicts with job and patient context.

Placing it there is what makes the guarantee free rather than fragile. The
comment already at that line explains why the MeasureReport and the payload must
be derived from the *same* list: when they diverge, a resource can be shipped
that `evaluatedResource` does not mention, and under the receiver's transaction
semantics one bad entry fails the whole patient. Deduping upstream of both
preserves that invariant, so `evaluatedResource` is 1:1 with Bundle entries by
construction rather than by a second, separately-maintained rule.

Gather order is deterministic — `types_in_order`, then pagination order — so
first-wins is reproducible and testable.

Base-fallback's endpoint and envelope shape are unchanged. Its payload stops
repeating duplicate `resource` parameters, which is the same defect being fixed
in one place instead of two.

### Why `evaluatedResource` needs no `fullUrl`

`MeasureReport.evaluatedResource` entries are relative references
(`Observation/123`). Strictly, resolving a reference *within* a Bundle requires
`entry.fullUrl`, and our collection Bundle entries have none — the contract
confirmed on 2026-09-14 does not include it.

They resolve anyway, through the server rather than within the payload. The
collection Bundle is processed by the receiver as a transaction, which stores
each entry at its own id; the relative references then resolve against the
receiver's base to exactly those resources. Adding `fullUrl` would deviate from
the confirmed shape for a property already obtained.

This is recorded because it is the kind of thing a reader re-opens: the
reasoning is deliberate, not an oversight.

### Documentation and decision record

`docs/architecture.md:109-114` describes the probe as reading the
CapabilityStatement and stamping `Job.submit_data_mode`; it needs the second
step and the new criteria.

Dropping support for a published-IG endpoint is a decision a future reader will
question, so it also gets an ADR in `docs/decisions.md` pointing here. Two
notes on that file: #428 is a pending fix relocating ADR-014's body out from
under the ADR-015 heading, and #420's ADR-016 is still unwritten — so take the
next free number at implementation time rather than assuming 016.

## Testing

**Detection** (`tests/test_services_fhir_client.py`, the DEQM block at `:2895`).
Seven cases, each distinguishing one thing the old probe could not see:

| Case | Expected |
|---|---|
| Type-level `submit-data` with a `bundle` in-param | `stu5` |
| Instance-only (`type: false`) with a `bundle` in-param | `base-fallback` |
| Type-level, parameters `measureReport` + `resource` only | `base-fallback` |
| Retired `deqm-submit-data` advertised alone | `base-fallback` |
| OperationDefinition unfetchable | `base-fallback` |
| `definition` on a foreign origin | not contacted; canonical search attempted |
| `/metadata` unreachable | `base-fallback` |

Case 4 regression-locks decision 1 — it is the test that fails if someone
restores historical support without revisiting this spec.

**Wire format.** The STU5 URL assertions at `:3020` and `:3065` flip to
`Measure/$submit-data`. Base mode's instance-level URL gets its own explicit
regression test rather than being asserted only in passing.

**Deduplication** (`tests/test_services_deqm.py`). Identical duplicates collapse
with no conflict reported; differing duplicates keep the first and report the
identity; order is preserved; the empty list is handled.

**Workflow** (`tests/test_services_workflows.py`). Dedupe runs before the
MeasureReport is built; `evaluatedResource` carries one reference per identity
with no dangling references; a Practitioner or Organization shared across two
subjects appears in both Bundles, proving no cross-subject suppression; the
once-per-job reporter Organization stays resolvable.

**Regressions that this change could plausibly break**, and must stay green
untouched: #414's single-mode settlement barrier (`_settle_mode_and_submit`, the
pioneer/downgrade path) and #415's error-`OperationOutcome`-inside-HTTP-200
rejection handling. Hardcoded `$deqm-submit-data` URLs at
`test_services_workflows.py:25,40,200` and `test_services_orchestrator.py:990`
need updating.

**Frontend.** `JobsPage.js` carries four user-facing strings naming
`$deqm-submit-data` — creation toast, badge title, `aria-label`, tooltip.
`JobsPage.workflow.test.js` matches them by regex at `:90`, `:99`, `:104`.

### Real-server coverage

The STU5 branch has never executed against any server, and **this change does
not fix that.** Bundled HAPI is pinned at `v8.8.0-1`, which does not implement
the type-level operation — `test_deqm_submit_data_workflow.py:187` asserts the
probe records `base-fallback` against it, and that assertion stays true and
correct. The STU5 end-to-end test added here is fixture-backed via mock
transport.

Per decision 2, a bump to HAPI 8.10.x would be the first thing to genuinely
exercise the path, since that version advertises the contract structurally.
That is a follow-up issue.

## Rejected alternatives

**A third mode for the retired operation.** Keeping `$deqm-submit-data` as its
own mode alongside `stu5` and `base-fallback` would preserve published-STU5
support explicitly. Rejected: it costs a new `jobs.submit_data_mode` value, a
new badge state, a third payload path to maintain and test — for a server shape
Lenny has never successfully talked to. Dropping it is also the honest option,
since the alternative is claiming support we cannot demonstrate.

**Endpoint failover inside one mode.** Try `$submit-data`, fall back to
`$deqm-submit-data` on a not-supported signal, then downgrade to base. Rejected:
it adds a second settlement step inside the #414 pioneer barrier, which is the
concurrency code that has already cost two PRs.

**Requiring a DEQM marker before classifying `stu5`.** Demanding the DEQM
canonical or an IG declaration in addition to the structural checks would keep
ordinary HAPI in `base-fallback` and keep the badge meaning "not a DEQM server".
Rejected: it would leave the STU5 path unexercised against every server
available to us, and it would be wrong on the merits — if a server accepts the
contract, the contract works there.

**Renaming the mode.** `stu5` is now a misnomer: it names a published spec this
contract deliberately departs from. Rejected for this change: renaming costs a
migration on `jobs.submit_data_mode`, an API-shape change, and frontend copy
churn, none of which serve #413. The inaccuracy is documented here instead.

**Deduping in the gather layer.** Would fix `direct_load` too. Rejected: it
changes behaviour for workflows this issue does not scope, in the
data-acquisition strategies that #397 and #409 have already churned.

**Failing the patient on a content conflict.** Loud and unambiguous. Rejected:
a transient pagination race would fail a whole patient's submission for data
that would have evaluated identically either way.

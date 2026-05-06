# Connectathon Measures Status

> **2026-05-06 — 5 measures removed from Lenny** (see issue #278).
> CMS2, CMS71, CMS165, CMS1017, and CMS1218 had upstream bundle/HAPI issues that could not be
> fixed before the connectathon. They have been removed from the seed bundles, manifest, and test
> suite. The original status detail for each is preserved below under
> [Removed measures (2026-05-06)](#removed-measures-2026-05-06).
> To re-add a measure once upstream fixes ship, drop the refreshed bundle into
> `seed/connectathon-bundles/` and add its entry back to `manifest.json`.

**HAPI version:** v8.8.0-1
**Target:** MADiE May 2026 Connectathon (7 active measures)
**Last updated:** 2026-05-06 (5 broken measures removed; 7 strict=true measures remain)

> **Maintenance note:** This file is hand-edited and drifts within days of a connectathon-measures workflow run. Auto-generation from nightly output is tracked as a follow-up.

---

## Current connectathon measures (7)

All 7 active measures are `strict=true` — every population mismatch is a hard test failure.

| Measure | Cases | Pass | Fail (xfail) | Status |
|---|---|---|---|---|
| CMS122FHIRDiabetesAssessGreaterThan9Percent | 56 | 50 | 6 | MOSTLY PASSING (89%) — 6 HAPI CQL divergences xfailed |
| CMS124FHIRCervicalCancerScreening | 33 | 33 | 0 | ✅ PASS (100%) |
| CMS125FHIRBreastCancerScreening | 66 | 56 | 10 | MOSTLY PASSING (85%) — 10 HAPI CQL divergences xfailed |
| CMS130FHIRColorectalCancerScreening | 64 | 63 | 1 | MOSTLY PASSING (98%) — 1 HAPI CQL divergence xfailed |
| CMS506FHIRSafeUseofOpioids | 38 | 38 | 0 | ✅ PASS (100%) |
| CMS816FHIRHHHypo | 9 | 9 | 0 | ✅ PASS (100%) |
| CMSFHIR529HybridHospitalWideReadmission | 53 | 53 | 0 | ✅ PASS (100%) |

---

## Summary

| | Count |
|---|---|
| Total active test cases | 319 |
| ✅ Correct — strict=true, populations match expected | 302 (of 319; 95%) |
| ❌ Known HAPI CQL divergence — strict=true xfailed | 17 |
| ⏭ Skip | 0 |

The meaningful pass rate is **302/319 = 95%**, with all 17 failures traced to a known HAPI upstream CQL divergence (not a Lenny defect).

Previous pass rate was 29% before session 11 infrastructure fixes.

---

## Status Rollup

- **4/7** Passing 100% — CMS124, CMS506, CMS816, CMSFHIR529
- **3/7** Mostly passing (known HAPI CQL divergence, 17 xfails total) —
  CMS122 (89%), CMS125 (85%), CMS130 (98%)
- **0/7** Broken (5 broken measures removed in issue #278)

---

## A+B Retest Result (2026-05-04, pre-removal baseline)

> *These numbers are the last full A+B run before the 5 broken measures were removed.
> The 5 broken-bundle measures are excluded from current test runs.*

**A: Direct HAPI/source-of-truth harness** — `test_golden_measures.py` plus `test_connectathon_measures.py` against latest prebaked HAPI finished `488 passed, 66 skipped, 17 xfailed` in 23m36s. The strict=true connectathon baseline is now 302/319 with 17 known xfails and no XPASS. Golden CMS816 and CMS529 passed; `basic-measure` remained skipped.

**B: Lenny orchestration path** — `test_full_jobs_pipeline.py` against the same prebaked images finished `8 passed, 3 failed` in 8m44s. All strict=true Jobs measures passed: CMS122, CMS124, CMS125, CMS130, CMS506, CMS816, and CMSFHIR529. The only Jobs failures were broken strict=false measures: CMS2, CMS165, and CMS1218, each failing all patient evaluations after PR #258 surfaced `MeasureReport.status=error` as `FhirOperationError`.

**Interpretation** — direct HAPI did not regress; the stale `219/233` headline is superseded by `302/319`. Lenny Jobs matches the direct-HAPI baseline for every strict=true measure. The A/B delta was isolated to broken strict=false measures (now removed).

---

## Per-Measure Results (Nightly Connectathon Test)

### Strict=true — populations are correct (trust these results)

| Measure | Cases | Pass | Fail (xfail) | Status | Next Step |
|---|---|---|---|---|---|
| CMS122FHIRDiabetesAssessGreaterThan9Percent | 56 | 50 | 6 | MOSTLY PASSING (89%) | 6 failures xfailed (HAPI DE divergence) — file HAPI upstream issue when ready |
| CMS124FHIRCervicalCancerScreening | 33 | 33 | 0 | ✅ PASS (100%) | — |
| CMS125FHIRBreastCancerScreening | 66 | 56 | 10 | MOSTLY PASSING (85%) | 10 failures xfailed (HAPI DE divergence) — file HAPI upstream issue when ready |
| CMS130FHIRColorectalCancerScreening | 64 | 63 | 1 | MOSTLY PASSING (98%) | 1 failure xfailed (`f9ef1fd1` dementia) — file HAPI upstream issue when ready |
| CMS506FHIRSafeUseofOpioids | 38 | 38 | 0 | ✅ PASS (100%) | — |
| CMS816FHIRHHHypo | 9 | 9 | 0 | ✅ PASS (100%) | — |
| CMSFHIR529HybridHospitalWideReadmission | 53 | 53 | 0 | ✅ PASS (100%) | — |

### Notes (strict=true measures)

- **CMS124, CMS506, CMS816, CMSFHIR529** — 100% pass, strict=true
- **CMS122** — 50/56 (89%); 6 `denominator-exclusion` mismatches confirmed as HAPI CQL divergence (xfailed in test suite)
- **CMS125** — 56/66 (85%); 10 `denominator-exclusion` mismatches confirmed as HAPI CQL divergence (xfailed)
- **CMS130** — 63/64 (98%); 1 failure (`f9ef1fd1` dementia condition) confirmed as HAPI CQL divergence (xfailed)

---

## Removed measures (2026-05-06)

> Removed in issue #278 — unable to fix before the connectathon. Re-add once upstream fixes ship
> by dropping the refreshed bundle into `seed/connectathon-bundles/` and adding its entry back to
> `manifest.json`. Status table preserved as a historical record.

| Measure | Cases | Direct HAPI | Lenny Jobs | Root Cause | Fix Needed |
|---|---|---|---|---|---|
| CMS2FHIRPCSDepressionScreenAndFollowUp | 36 | 36 pass¹ | ❌ 36/36 eval errors | Missing 10 VSAC ValueSets | Refreshed bundle from MADiE with ValueSets |
| CMS71FHIRSTKAnticoagAFFlutter | 83 | 83 pass¹ | ✅ passed (clinically untrusted) | Duplicate Claim IDs in MADiE v0.3.002 export | Refreshed bundle from MADiE (per-patient Claims) |
| CMS165FHIRControllingHighBloodPressure | 10 | 10 pass¹ | ❌ 10/10 eval errors | Library version mismatch (`AdultOutpatientEncounters v4.16.000` vs. available `v4.19.000`) | Refreshed bundle from MADiE with updated library versions |
| CMS1017FHIRHHFI | 65 | 65 skipped | Not run | HAPI DEQM scoring-type incompatibility → HTTP 400 | Await HAPI upstream fix (issue #100 closed; HTTP 400 still present in v8.8.0) |
| CMS1218FHIRHHRF | 55 | 55 pass¹ | ❌ 55/55 eval errors | 0 ValueSets in bundle | Refreshed bundle from MADiE with ValueSets |

¹ *"Pass" in strict=false direct-HAPI tests means the test did not crash — not that populations are correct.* PR #258 surfaces `MeasureReport.status=error` as a Lenny Jobs failure, so CMS2/CMS165/CMS1218 failed as jobs even though direct HAPI recorded soft passes.

---

## Golden Test Directory

| Bundle | Description | Status |
|---|---|---|
| `basic-measure` | Simple EXM test | SKIP |
| `CMS816FHIRHHHypo` | HH Hypoglycemia (inpatient, 9 patients) | PASS |
| `CMS529FHIRHybridHospitalWideReadmission` | Hospital-Wide Readmission (53 patients) | PASS |

### Excluded from golden tests

**CMS1017FHIRHHFI** (removed in PR #98, documented in issue #101):
CMS1017's bundle contains 35 ValueSets including 10 whose canonical URLs overlap with CMS816/CMS529. Alphabetical bundle loading puts CMS1017 first, populating HAPI with VS version `20250419` (125 expansion codes). CMS816/CMS529 ship the correct version `20221118` (167 codes) at the same URLs — the dedup guard skips loading them since the URL is already present. CQL finds no matching encounters; IP=0 for all CMS816/CMS529 golden patients. Additionally, HAPI v8.8.0 returns HTTP 400 for all CMS1017 `$evaluate-measure` calls, so it contributes zero passing tests regardless.

Fix needed: HAPI DEQM update for composite/ratio scoring type support (issue #100 closed — underlying HAPI fix still needed), then verify no VS version conflicts with CMS816/CMS529 before re-adding.

**CMS1218FHIRHHRF** (removed in PR #98, documented in issue #101):
CMS1218 bundle ships 0 ValueSets. Its IP criteria ("Elective Inpatient Encounter With OR Procedure Within 3 Days") requires ValueSets — including `2.16.840.1.113762.1.4.1248.208` "General And Neuraxial Anesthesia" — that are only present when all 12 connectathon bundles are loaded together. In golden test isolation, all patients evaluate to IP=0.

Fix needed: MADiE must include the required VSes in the CMS1218 bundle, or all 12 bundles must be pre-loaded before the golden test (defeats the purpose of isolation).

CMS1218 is covered by the direct nightly connectathon harness as 55 non-strict warning passes in full-bundle context. Lenny Jobs currently fails all 55 evaluations because the missing ValueSets surface as HAPI error reports.

---

## Infrastructure Bugs Fixed

### Session 11 — Connectathon infrastructure (29% → 79% pass rate)

**Bug 1: ValueSet ID conflict (HAPI-0902 silent batch failure)**

Symptom: HAPI-0831 during CQL retrieves even after a "successful" bundle load; `$expand` still failing after the 600s timeout.

Cause: Seed bundle had loaded `VS/1082-20190315` (1000 concepts, truncated). Connectathon bundle tried to `PUT VS/1082` (bare ID, 1797 concepts) with the same `url+version`. HAPI enforces unique `url+version`, so the batch PUT failed silently with HAPI-0902 (per-entry 400 inside a 200 batch response). The 1797-concept VS was never stored; HAPI kept the truncated seed version.

Fix: Before the batch PUT, query HAPI by URL for each ValueSet. If a matching resource exists with a different ID, rewrite the resource `id` in-place so the PUT updates in place instead of creating a URL conflict.

Files: `backend/tests/integration/test_connectathon_measures.py` (Pass 2 block), `scripts/smoke_connectathon.py`

---

**Bug 2: `$expand?count=1` false positive in expansion probe**

Cause: HAPI short-circuits `$expand?count=1` — returns HTTP 200 immediately without attempting full expansion. `_wait_for_valueset_expansion` declared success while background pre-expansion was still running.

Fix: Changed all expansion probes to `count=2`. HAPI correctly raises HAPI-0831 with `count=2` until pre-expansion completes.

Files: `backend/tests/integration/conftest.py`, `scripts/smoke_connectathon.py`

---

**Bug 3: Smoke test reindex chicken-and-egg**

Cause: Smoke test found the probe encounter via `Encounter?patient=` search, which requires the reference index to be ready — which requires the reindex it was trying to trigger.

Fix: Capture probe patient and encounter IDs directly from bundle data during clinical load iteration. No HAPI search needed.

Files: `scripts/smoke_connectathon.py`

---

### Session 12 — Golden test CI unblocking, round 1

**Bug 4: CMS1017 VS conflict poisoning CMS816/CMS529**

Symptom: CMS816 and CMS529 golden tests return IP=0 for all patients even after the 600s gate timeout on fresh CI containers.

Cause: Golden bundles load alphabetically. CMS1017 loads first and populates HAPI with 10 shared VS URLs (e.g. `2.16.840.1.114222.4.11.3591`) at version `20250419` (125 codes). CMS816 and CMS529 ship the same URLs at version `20221118` (167 codes) — the dedup guard skips loading them since the URL is already present. CQL resolves to the undersized CMS1017 version and finds no matching encounters for CMS816/CMS529 patients.

Fix: Removed CMS1017 from the golden test directory. CMS1017 is always skipped (HTTP 400), so it contributes no passing tests and its only effect was poisoning HAPI with wrong VS versions.

Files: `backend/tests/integration/golden/CMS1017FHIRHHFI/` — deleted

---

**Bug 5: CMS1218 golden test not viable in isolation**

Cause: CMS1218 bundle ships 0 ValueSets. IP criteria require ValueSets only present when all 12 connectathon bundles are loaded together. In isolation, IP=0 for all patients.

Fix: Removed CMS1218 from the golden test directory. It is covered by the nightly connectathon test.

Files: `backend/tests/integration/golden/CMS1218FHIRHHRF/` — deleted

---

**Bug 6: Golden test fixture missing inpatient evaluate-measure gate**

Cause: The golden test fixture waited for reindex completion (`Encounter?patient=X`) then ran tests immediately. The reindex probe exits when the first patient's encounter is indexed; HAPI may still be processing batches for later patients.

Fix: Added `$evaluate-measure` gate for CMS816 and CMS529 probe patients (`1a89fbca`, `1a527f21`). Gate polls until IP≥1 for each probe patient, confirming the full CQL evaluation stack is ready. Also improved reindex probing to check both the first and last encounter per bundle.

Files: `backend/tests/integration/test_golden_measures.py`

---

### Session 13 — Golden test CI unblocking, round 2

**Bug 7: `fix_valueset_compose_for_hapi` missed empty-include case**

Symptom: CMS816/CMS529 still return IP=0 in CI even after CMS1017 removal.

Cause: `seed/measure-bundle.json` contains VS `2.16.840.1.114222.4.11.3591-20250419` with a `compose.include` entry that exists but has zero concept codes. `fix_valueset_compose_for_hapi` only patched VSes with no compose or with VS-ref includes; it didn't handle "includes exist but carry no codes." HAPI silently expanded to 0 codes. The golden fixture's dedup guard then saw this URL already in HAPI and skipped loading CMS816/CMS529's correct 167-code version.

Fix: Extended `needs_fix` detection in `fix_valueset_compose_for_hapi` to also catch: `total_concepts == 0 and not has_filters`.

Files: `backend/tests/integration/_helpers.py`

---

**Bug 8: Golden fixture dedup guard blocked seed VS overwrites**

Cause: The dedup set `loaded_vs_urls` was pre-populated from HAPI state at fixture startup. Any VS URL already present in HAPI (e.g. the broken 0-code seed version) was skipped — the golden bundle's correct version was never loaded.

Fix: Replace skip-if-URL-exists with ID remapping. At load time, query HAPI for existing VS by URL. If found with a different ID, rewrite the resource's `id` so the PUT updates in place. The dedup set now tracks only VS URLs loaded within the current golden test run, not pre-populated from HAPI.

Files: `backend/tests/integration/test_golden_measures.py`

---

## Remaining Failure Classes

### Class A: `denominator-exclusion` mismatch (CMS122 ×6, CMS125 ×10, CMS130 ×1)

Patients land in `numerator` when MADiE expects `denominator-exclusion`. The failures span multiple exclusion sub-criteria that HAPI evaluates differently from the MADiE CQL reference engine:

- **Frailty encounter** overlaps MP — HAPI misses the encounter-based frailty signal
- **Frailty diagnosis** overlaps MP — HAPI misses the condition-based frailty signal
- **Frailty symptom** overlaps MP
- **Frailty device** used / device request (doNotPerform=false or no modifier)
- **Frailty observation** (medication device used)
- **Dementia medications** during MP (CMS125 `0ced1e0c`, CMS130 `f9ef1fd1`)
- **Mastectomy date boundary** — bilateral mastectomy or two unilateral mastectomies with period.end on 12/31 of MP (CMS125 `4cf81a94`, `857fec09`); HAPI appears to treat period.end as exclusive

Status: Genuine HAPI vs. MADiE CQL evaluation differences — **not fixable in Lenny**. All 17 marked `xfail` in `test_connectathon_measures.py::_HAPI_DE_XFAIL`. Needs HAPI upstream issue filed at hapifhir/hapi-fhir (update `_HAPI_DE_XFAIL` comment with issue number when filed).

**Related issues (both closed):**
- Issue #99 (CMS122/CMS125/CMS130 frailty exclusion mismatch) — closed 2026-04-25. Lenny component (H1: missing ValueSet compose fix in production path) was already fixed when `_fix_valueset_compose_for_hapi` was moved into `validation.py:_prepare_measure_support_resources`. Remaining 17 failures are this HAPI upstream divergence.
- Issue #140 (CMS122 denominator_exclusion not firing through /jobs) — closed 2026-04-24. Confirmed same HAPI CQL divergence; Lenny-side fix (index consistency gate added to `/jobs` path) shipped in same PR.

**Issue #112 verification (2026-04-22):** Fresh-container run with extended eval gate confirmed exactly 17 failures — the 69 extra failures from an earlier run were timing artifacts (IP=0 from VS expansion not complete). Root cause: the eval gate only probed CMS122 patient `9cba6cfa`; CMS125/CMS130 VSes take longer to expand on slower machines. Fix: added eval gate probes for CMS122 numerator path + CMS125 + CMS130 in `_load_connectathon_bundles_to_hapi`.

**Issue #140 root cause (2026-04-24):** Separate investigation confirmed the CMS122 `denominator-exclusion` gap is a HAPI upstream CQL bug, not a Lenny defect. Evidence: (1) the count of 19 actual vs. 25 expected exactly matches the 6 `_HAPI_DE_XFAIL` frailty patients; (2) Phase 3 testing showed adding an index-consistency gate to the `/jobs` path improved overall CMS122 accuracy (49/56 → 50/56) but left `denominator-exclusion` at 19 → 19, ruling out reference-index timing. The `/jobs` path hardcoded `asyncio.sleep(5.0)` was replaced with an index-consistency gate (same pattern as the validation path) to fix the broader accuracy gap for other patients; that gate was subsequently replaced by `synchronization.strategy=sync` at the HAPI level (PR #214). See issue #140 for full findings.

---

## Known Noise

**DB teardown errors** — `_truncate_tables` autouse fixture fails in teardown for connectathon tests because DB tables don't exist in the test-only stack. ~325 teardown `ERROR` lines appear in the output. Does not affect `PASSED`/`FAILED` counts.

---

## Bundle Version Status

### Missing QI-Core 6 dQM v1.0.000 Bundles

The following measures are currently using older FHIR4 versions as placeholders pending QI-Core 6 dQM v1.0.000 bundles from MADiE (see issue #115):

| Measure | Current | Target | Status |
|---------|---------|--------|--------|
| CMS122 | v0.5.000 (FHIR4) | v1.0.000 (QI-Core 6) | Pending MADiE |
| CMS124 | v0.4.000 (FHIR4) | v1.0.000 (QI-Core 6) | Pending MADiE |
| CMS125 | v0.4.000 (FHIR4) | v1.0.000 (QI-Core 6) | Pending MADiE |
| CMS130 | v0.4.000 (FHIR4) | v1.0.000 (QI-Core 6) | Pending MADiE |

All EXM FHIR4 bundles (EXM104, EXM105, EXM108, EXM124, EXM125, EXM130, EXM165, EXM506, EXM529) have been removed from seed to eliminate duplicate measures. Once the QI-Core 6 versions are obtained, the placeholder versions will be replaced.

---

## Next Steps

1. **File HAPI upstream issue** (when ready) — hapifhir/hapi-fhir for the DE criteria evaluation divergence (frailty, dementia, mastectomy date boundary). When filed, update `_HAPI_DE_XFAIL` comment in `test_connectathon_measures.py` with the issue number.
2. **CMS122/124/125/130** — obtain QI-Core 6 dQM v1.0.000 bundles from MADiE (issue #115)
3. **Re-add removed measures** (once upstream fixes ship) — see [Removed measures](#removed-measures-2026-05-06) section. Drop the refreshed bundle into `seed/connectathon-bundles/` and add the entry back to `manifest.json`.
4. **Remove xfail marks** — when HAPI ships a fix for the DE divergence, run `--runxfail` to confirm xpassed, then remove from `_HAPI_DE_XFAIL`

# Connectathon Measures Status

**HAPI version:** v8.8.0-1
**Target:** MADiE May 2026 Connectathon (12 measures)
**Last updated:** 2026-05-04 (full A+B retest against `:latest` prebaked HAPI images after PR #258: direct HAPI strict=true held at 302/319 with 17 known xfails; Lenny Jobs passed all strict=true measures and failed only broken strict=false measures CMS2, CMS165, and CMS1218)

> **Maintenance note:** This file is hand-edited and drifts within days of a connectathon-measures workflow run. Auto-generation from nightly output is tracked as a follow-up. Resource baselines listed below (Patient: 568, Measure: ≥12, etc.) are connectathon-seed counts, not arbitrary thresholds — they reflect the sum of all 12 bundles' test patients/resources, not a target for a deployed CDR.

---

## Summary

| | Count |
|---|---|
| Total test cases | 568 |
| ✅ Correct — strict=true, populations match expected | 302 (of 319 strict=true cases; 95%) |
| ⚠️ Broken bundle — strict=false / not clinically trusted | 184 direct-HAPI non-strict cases (CMS2 + CMS71 + CMS165 + CMS1218); Lenny Jobs fails CMS2/CMS165/CMS1218 |
| ❌ Known HAPI CQL divergence — strict=true xfailed | 17 |
| ⏭ Skip — HTTP 400 (CMS1017) | 65 |

> **The 71% figure cited in earlier docs is misleading** — it counted strict=false "passes" the same as strict=true passes. The 184 cases from broken-bundle measures are not clinically trusted even when the direct-HAPI harness records them as non-strict passes. The meaningful pass rate across the 7 strict=true measures is 302/319 = 95%, with all 17 failures traced to a known HAPI upstream CQL divergence (not a Lenny defect).

Previous pass rate was 29% before session 11 infrastructure fixes.

---

## Status Rollup

- **4/12** Passing 100% — CMS124, CMS506, CMS816, CMSFHIR529
- **3/12** Mostly passing (known HAPI CQL divergence, 17 xfails total) —
  CMS122 (89%), CMS125 (85%), CMS130 (98%)
- **5/12** Broken
  - **4/5** need corrected MADiE bundles — CMS2, CMS71, CMS165, CMS1218
  - **1/5** needs HAPI upstream fix — CMS1017 (HTTP 400 on scoring type)

**A vs B headline:** Direct HAPI strict=true holds at **302/319** with the
same 17 known xfails. The Lenny Jobs path passes every strict=true measure
and now fails CMS2 / CMS165 / CMS1218 because PR #258 surfaces
`MeasureReport.status=error` as a job failure instead of storing
silent all-zero results.

---

## A+B Retest Result (2026-05-04)

**A: Direct HAPI/source-of-truth harness** — `test_golden_measures.py` plus `test_connectathon_measures.py` against latest prebaked HAPI finished `488 passed, 66 skipped, 17 xfailed` in 23m36s. The strict=true connectathon baseline is now 302/319 with 17 known xfails and no XPASS. Golden CMS816 and CMS529 passed; `basic-measure` remained skipped.

**B: Lenny orchestration path** — `test_full_jobs_pipeline.py` against the same prebaked images finished `8 passed, 3 failed` in 8m44s. All strict=true Jobs measures passed: CMS122, CMS124, CMS125, CMS130, CMS506, CMS816, and CMSFHIR529. The only Jobs failures were broken strict=false measures: CMS2, CMS165, and CMS1218, each failing all patient evaluations after PR #258 surfaced `MeasureReport.status=error` as `FhirOperationError`.

**Interpretation** — direct HAPI did not regress; the stale `219/233` headline is superseded by `302/319`. Lenny Jobs matches the direct-HAPI baseline for every strict=true measure. The A/B delta is isolated to broken strict=false measures where direct HAPI still records non-strict warning passes, but Lenny now fails the job when HAPI returns error reports.

---

## Per-Measure Results (Nightly Connectathon Test)

> **How to read this table.** Two completely different things look the same if you only scan Pass/Fail:
>
> - **Strict = true** → any population mismatch is a hard Fail. Pass means the populations are clinically correct.
> - **Strict = false** → population mismatches are warnings, not failures. A measure can show 0 Fail and still evaluate every single patient wrong. The "Pass" count only means the test didn't crash.
>
> **Never read a strict=false row as clinically accurate.** Those rows are broken measures being tracked until MADiE ships a fix. The nightly test keeps them in the suite so regressions are visible, not because the results are trusted.

### Strict=true — populations are correct (trust these results)

| Measure | Cases | Pass | Fail | Skip | Status | Next Step |
|---|---|---|---|---|---|---|
| CMS122FHIRDiabetesAssessGreaterThan9Percent | 56 | 50 | 6 | 0 | MOSTLY PASSING (89%) | 6 failures xfailed (HAPI DE divergence) — file HAPI upstream issue when ready |
| CMS124FHIRCervicalCancerScreening | 33 | 33 | 0 | 0 | ✅ PASS (100%) | — |
| CMS125FHIRBreastCancerScreening | 66 | 56 | 10 | 0 | MOSTLY PASSING (85%) | 10 failures xfailed (HAPI DE divergence) — file HAPI upstream issue when ready |
| CMS130FHIRColorectalCancerScreening | 64 | 63 | 1 | 0 | MOSTLY PASSING (98%) | 1 failure xfailed (`f9ef1fd1` dementia) — file HAPI upstream issue when ready |
| CMS506FHIRSafeUseofOpioids | 38 | 38 | 0 | 0 | ✅ PASS (100%) | — |
| CMS816FHIRHHHypo | 9 | 9 | 0 | 0 | ✅ PASS (100%) | — |
| CMSFHIR529HybridHospitalWideReadmission | 53 | 53 | 0 | 0 | ✅ PASS (100%) | — |

### Strict=false — ⚠️ BROKEN BUNDLES. Results are NOT clinically accurate.

> These measures have known bundle defects. The nightly test runs them in non-strict mode **only to detect regressions**, not to validate correctness. A "0 Fail" row here does not mean the measure works — it means the defect hasn't gotten worse. Do not run these measures in Lenny and expect meaningful results.

| Measure | Cases | Direct HAPI | Lenny Jobs | UI / Clinical Result | Status | Fix Needed |
|---|---|---|---|---|---|---|
| CMS2FHIRPCSDepressionScreenAndFollowUp | 36 | 36 pass¹ | ❌ failed: 36/36 eval errors | Population warnings; missing ValueSets now surface as job failure | ⚠️ BROKEN — missing 10 VSAC ValueSets | Refreshed bundle from MADiE with ValueSets |
| CMS71FHIRSTKAnticoagAFFlutter | 83 | 83 pass¹ | ✅ passed | Population warnings; Claims remain incomplete | ⚠️ BROKEN — duplicate Claim IDs in export | Refreshed bundle from MADiE (per-patient Claims) |
| CMS165FHIRControllingHighBloodPressure | 10 | 10 pass¹ | ❌ failed: 10/10 eval errors | Population warnings; library mismatch now surfaces as job failure | ⚠️ BROKEN — library version mismatch | Refreshed bundle from MADiE (AdultOutpatientEncounters v4.19.000) |
| CMS1017FHIRHHFI | 65 | 65 skipped | Not run in Jobs pipeline | HTTP 400 — cannot evaluate | ⚠️ BROKEN — HAPI scoring-type incompatibility | Await HAPI DEQM update (issue #100 closed — HAPI upstream fix still needed) |
| CMS1218FHIRHHRF | 55 | 55 pass¹ | ❌ failed: 55/55 eval errors | Population warnings; missing ValueSets now surface as job failure | ⚠️ BROKEN — 0 ValueSets in bundle | Refreshed bundle from MADiE with ValueSets |

¹ **"Pass" in strict=false direct-HAPI tests means the test did not crash, not that populations are correct.** The direct-HAPI harness treats population mismatches as non-fatal warnings for strict=false measures. After PR #258, the Lenny Jobs path raises `FhirOperationError` when HAPI returns `MeasureReport.status=error`, so CMS2, CMS165, and CMS1218 now fail as jobs instead of storing silent all-zero/non-usable results. Earlier revisions of this doc showed lower direct-HAPI pass counts for CMS71 (9/83) and CMS165 (0/10) because pre-v8.8.0 HAPI returned HTTP errors for those cases, which the test counted as failures. The prebaked HAPI v8.8.0 image returns 200 + MeasureReport with population warnings for the same inputs, so strict=false now soft-passes all 83 and 10 respectively. Population correctness is unchanged.

### Notes (strict=true measures)

- **CMS124, CMS506, CMS816, CMSFHIR529** — 100% pass, strict=true
- **CMS122** — 50/56 (89%); 6 `denominator-exclusion` mismatches confirmed as HAPI CQL divergence (xfailed in test suite)
- **CMS125** — 56/66 (85%); 10 `denominator-exclusion` mismatches confirmed as HAPI CQL divergence (xfailed)
- **CMS130** — 63/64 (98%); 1 failure (`f9ef1fd1` dementia condition) confirmed as HAPI CQL divergence (xfailed)

### Notes (strict=false measures)

- **CMS71** — MADiE v0.3.002 exports all 83 Claims with the same ID; `fix_duplicate_claim_ids()` partially recovers the bundle enough for Lenny Jobs to complete, but populations remain warning-only and clinically untrusted. Needs refreshed MADiE bundle.
- **CMS165** — v0.3.000 bundle missing library dependencies (`AdultOutpatientEncounters v4.16.000` vs. available `v4.19.000`). Direct HAPI records non-strict warning passes; Lenny Jobs now fails all 10 evaluations. Needs refreshed MADiE bundle.
- **CMS2** — missing 10 VSAC ValueSets (depression screening/medications); IP=0 for most patients. Direct HAPI records non-strict warning passes; Lenny Jobs now fails all 36 evaluations after error reports are surfaced.
- **CMS1017** — HAPI v8.8.0 returns HTTP 400 for this measure's scoring type; all 65 tests skipped. Issue #100 (tracking this) is closed — HAPI upstream fix for composite/ratio scoring type still needed.
- **CMS1218** — 0 ValueSets in bundle. The required ValueSets (e.g. `2.16.840.1.113762.1.4.1248.208` "General And Neuraxial Anesthesia") are not present in any bundle in this repo. Direct HAPI records non-strict warning passes; Lenny Jobs now fails all 55 evaluations after HAPI error reports are surfaced.

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

### Class B: CMS71 — MADiE bundle export bug (strict=false)

MADiE v0.3.002 exports all 83 Claim resources with the same ID. `fix_duplicate_claim_ids()` assigns unique IDs and the Lenny Jobs pipeline completes, but the direct-HAPI harness still emits population warnings and the measure remains clinically untrusted until MADiE ships corrected per-patient Claims.

### Class C: CMS165 — library version mismatch (strict=false)

Bundle v0.3.000 requires `AdultOutpatientEncounters v4.16.000`; HAPI has `v4.19.000` (loaded by CMS125/CMS130). Direct HAPI records non-strict warning passes; Lenny Jobs fails all 10 patient evaluations after PR #258 surfaces error reports. Needs refreshed bundle from MADiE.

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
2. **Lenny Jobs strict=false expectation** — decide whether `test_full_jobs_pipeline.py` should explicitly expect/xfail failed jobs for CMS2, CMS165, and CMS1218 now that HAPI error reports are surfaced instead of silently stored
3. **CMS122/124/125/130** — obtain QI-Core 6 dQM v1.0.000 bundles from MADiE (issue #115)
4. **CMS165** — get refreshed bundle from MADiE with updated library versions
5. **CMS71** — get refreshed bundle from MADiE with correct per-patient Claim resources
6. **CMS1017** — await HAPI DEQM upstream fix for composite/ratio scoring type (issue #100 closed; HTTP 400 still present in v8.8.0); re-add to golden tests after fix ships and VS conflicts are verified gone
7. **CMS1218** — once MADiE ships bundle with required ValueSets, re-add to golden tests
8. **Remove xfail marks** — when HAPI ships a fix for the DE divergence, run `--runxfail` to confirm xpassed, then remove from `_HAPI_DE_XFAIL`

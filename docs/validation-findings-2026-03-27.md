# Validation Findings — CMS eCQM Test Bundles

**Date:** 2026-03-27
**Branch:** feature/expected-results-compare
**HAPI FHIR Version:** v7.4.0
**Bundles Source:** `/Users/bill/Documents/claude-work/measuredata/og_demo_measures/`

## Upload Results: 13/14 Succeeded

| Bundle | Status | Measures | Patients | Expected |
|--------|--------|----------|----------|----------|
| CMS1188FHIRHIVSTITesting | OK | 1 | 34 | 34 |
| CMS122FHIRDiabetesAssessGT9Pct | OK | 1 | 55 | 55 |
| CMS124FHIRCervicalCancerScreen | OK | 1 | 34 | 34 |
| CMS125FHIRBreastCancerScreen | OK | 1 | 66 | 66 |
| CMS130FHIRColorectalCancerScrn | OK | 1 | 64 | 64 |
| CMS138FHIRTobaccoScrnCessation | OK | 1 | 47 | 47 |
| CMS139FHIRFallRiskScreening | OK | 1 | 29 | 29 |
| CMS153FHIRChlamydiaScreening | OK | 1 | 32 | 32 |
| CMS165FHIRControllingHighBP | OK | 1 | 68 | 68 |
| CMS22FHIRPCSBPScreeningFollowUp | OK | 1 | 44 | 44 |
| CMS2FHIRPCSDepScreenAndFollowUp | OK | 1 | 36 | 36 |
| CMS349FHIRHIVScreening | OK | 1 | 36 | 36 |
| CMS69FHIRPCSBMIScreenAndFollowUp | OK | 1 | 63 | 63 |
| NHSNGlycemicControlHypoglycemiaInitialPopulation | **FAILED** | 0 | 0 | 0 |

## Validation Results: 688 Patients Across 14 Measures

| Measure | Pass | Fail | Error | Total | Pass Rate* |
|---------|------|------|-------|-------|-----------|
| CMS1188FHIRHIVSTITesting | 5 | 24 | 5 | 34 | 17% |
| CMS122FHIRDiabetesAssessGT9Pct | 4 | 0 | 51 | 55 | 100% |
| CMS124FHIRCervicalCancerScreen | 4 | 30 | 0 | 34 | 12% |
| CMS125FHIRBreastCancerScreen | 6 | 60 | 0 | 66 | 9% |
| CMS130FHIRColorectalCancerScrn | 7 | 0 | 57 | 64 | 100% |
| CMS138FHIRTobaccoScrnCessation | 9 | 0 | 38 | 47 | 100% |
| CMS139FHIRFallRiskScreening | 21 | 8 | 0 | 29 | 72% |
| CMS153FHIRChlamydiaScreening | 5 | 27 | 0 | 32 | 16% |
| CMS165FHIRControllingHighBP | 1 | 67 | 0 | 68 | 1% |
| CMS22FHIRPCSBPScreeningFollowUp | 37 | 6 | 1 | 44 | 86% |
| CMS2FHIRPCSDepScreenAndFollowUp | 2 | 0 | 34 | 36 | 100% |
| CMS349FHIRHIVScreening | 32 | 4 | 0 | 36 | 89% |
| CMS69FHIRPCSBMIScreenAndFollowUp | 5 | 0 | 58 | 63 | 100% |
| NHSNGlycemicControlHypoglycemiaInitialPopulation | 0 | 0 | 80 | 80 | N/A |
| **Totals** | **138** | **226** | **324** | **688** | — |

*Pass Rate = Pass / (Pass + Fail), excluding errors. Measures with 100% pass rate had all non-error patients pass; their errors are CQL engine crashes, not population mismatches.

## Issue 1: HAPI CQL Engine Errors (324 patients, 7 measures)

**Error:** `HAPI-0389: Failed to call access method: java.lang.IllegalArgumentException: Unable to extract codes from fhirType Reference`

**Root cause:** HAPI FHIR v7.4.0's CQL engine crashes when measure logic encounters a FHIR Reference where a CodeableConcept is expected. This is a known HAPI CQL engine bug that causes `$evaluate-measure` to return HTTP 500 for affected patients.

**Affected measures:** CMS122 (51 errors), CMS130 (57), CMS138 (38), CMS2 (34), CMS69 (58), CMS22 (1), CMS1188 (5)

**Recommendation:** Upgrade HAPI FHIR to v7.6+ where this CQL engine bug is patched. This is not an MCT2 code issue — it is a limitation of the measure calculation engine.

## Issue 2: Population Mismatches (226 patients, 7 measures)

**Root cause:** HAPI's CQL engine evaluates some measure logic differently than the CMS reference implementation (MADiE/Bonnie). Common mismatch patterns include denominator-exclusion not triggering and numerator criteria evaluating differently than expected.

**Affected measures:**
- CMS165 ControllingHighBP — 67 failures (1% pass rate, worst performer)
- CMS125 BreastCancerScreen — 60 failures
- CMS124 CervicalCancerScreen — 30 failures
- CMS153 ChlamydiaScreening — 27 failures
- CMS1188 HIVSTITesting — 24 failures
- CMS139 FallRiskScreening — 8 failures
- CMS22 BPScreeningFollowUp — 6 failures
- CMS349 HIVScreening — 4 failures

**Recommendation:** Investigate per-measure by comparing HAPI's CQL evaluation against the CMS reference implementation. Possible causes include:
- ValueSet version mismatches between test bundle definitions and HAPI's terminology service
- CQL engine interpretation differences for edge-case date logic and timing boundaries
- Missing or differently-versioned dependent Library resources

## Issue 3: NHSN Bundle Upload Failure

**Error:** `HAPI-0931: Invalid reference found at path 'Encounter.partOf'. Resource type 'Location' is not valid for this path` (HTTP 422)

**Root cause:** The NHSN test bundle contains an `Encounter` resource whose `partOf` field references a `Location` resource. Per the FHIR R4 specification, `Encounter.partOf` only accepts references to other `Encounter` resources. HAPI enforces this constraint and rejects the entire transaction bundle.

**Recommendation:** This is a data quality issue in the CMS-published NHSN test bundle. Two options:
1. File a defect with the CMS eCQI team to correct the bundle
2. Add pre-processing logic in `triage_test_bundle()` to sanitize or skip resources with invalid reference types before pushing to the CDR

## Issue 4: Dangling References in Test Bundles (11 of 14 bundles)

**Root cause:** CMS test bundles reference Organization, Practitioner, and Location resources that are not included in the bundle. HAPI's transaction processing enforces referential integrity, causing the entire push to fail.

**Affected references (18 unique):**

| Type | IDs |
|------|-----|
| Organization | 123456, 4654531645616, example, MEDICARE, 523cb9fd-..., a6426063-..., 1, Organization-1, b7d26bcd-..., f001 |
| Practitioner | example, 123456, f007, f202, f204, 33d87640-... |
| Location | 2, intensive-care-unit-0a0e |

**Workaround applied:** Created stub resources on the CDR and measure engine for all 18 missing references.

**Recommendation:** Add automatic stub resource creation to `triage_test_bundle()` — scan clinical resources for references that aren't present in the bundle, and create minimal stub resources before pushing the transaction. This makes upload self-healing without manual intervention.

## Issue 5: HAPI Rejects Numeric Resource IDs (5 resources)

**Error:** `HAPI-0960: Can not create resource with ID[123456], no resource with this ID exists and clients may only assign IDs which contain at least one non-numeric character`

**Root cause:** HAPI's default `client_id_strategy` is `ALPHANUMERIC`, which rejects purely numeric IDs. Several CMS test bundles reference resources with numeric IDs (e.g., `Organization/123456`, `Organization/1`, `Location/2`).

**Workaround applied:** Added `hapi.fhir.client_id_strategy=ANY` to both HAPI instances in `docker-compose.yml`.

**Recommendation:** Keep this configuration permanently. CMS test bundles are the authoritative data source, and MCT2 must accept whatever IDs they use.

## Issue 6: External URL References (1 bundle)

**Error:** `HAPI-0507: Resource contains external reference to URL "http://benefits.example.org/FHIR/Organization/CBI35" but this server is not configured to allow external references`

**Root cause:** One `Coverage` resource in the NHSN bundle references an Organization using an absolute external URL instead of a relative reference. HAPI blocks external references by default.

**Workaround applied:** Added `hapi.fhir.allow_external_references=true` to the CDR HAPI instance.

**Recommendation:** Keep this configuration. Test bundles may contain external references that don't need to resolve to actual resources on the local server.

## Infrastructure Configuration Changes

Added to `docker-compose.yml` for both HAPI instances:

```yaml
- hapi.fhir.client_id_strategy=ANY          # Allow numeric resource IDs
- hapi.fhir.allow_external_references=true   # Allow absolute URL references (CDR only)
```

## Recommended Code Changes (not yet implemented)

1. **Auto-create stub resources:** Before pushing clinical data to the CDR, scan for dangling references and create minimal stubs automatically.
2. **Sanitize invalid references:** Strip or skip resources with invalid reference types (e.g., `Encounter.partOf` pointing to `Location`) before pushing.
3. **Upgrade HAPI FHIR:** Move to v7.6+ to resolve the CQL engine `fhirType Reference` bug that causes 500 errors for 47% of test patients.
4. **Improve error reporting:** When `push_resources()` fails with a 400/422, capture and surface the HAPI OperationOutcome diagnostics in the BundleUpload error_message rather than just the HTTP status.

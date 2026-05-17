# Draft upstream issue — CMS130 dementia Condition code mismatch

**Status:** draft, not yet submitted
**Target repo:** `cqframework/dqm-content-qicore-2025`
**Suggested title:** `CMS130 test case: Condition.code does not match test-case description ("dementia") — exclusion not reproducible from patient resources`
**Suggested labels:** `bug`, `content`

---

## Summary

In the CMS130 (Colorectal Cancer Screening) test bundle, the `Condition` resource for patient `f9ef1fd1-cced-47ad-a47b-d9c20254511c` has a `code` that doesn't match what the bundle's own `MeasureReport.extension[cqfm-testCaseDescription]` says the test case is about. As a result, an HL7-conformant CQL engine evaluating the published measure against the bundled patient resources cannot reproduce the bundle's expected `denominator-exclusion = 1`. The exclusion the test is designed to demonstrate (Advanced Illness + Frailty) never fires.

## Affected resources

| Resource | ID |
|---|---|
| Measure | `CMS130FHIRColorectalCancerScreen` |
| Patient | `f9ef1fd1-cced-47ad-a47b-d9c20254511c` |
| Expected MeasureReport | `155c6167-2d08-4dfe-86d9-e137afe3a3ef` |
| **Condition (defective)** | **`4af005f4-610d-412e-8ac7-f8399baf2f7b`** |
| MedicationRequest | `ff669692-38ae-4901-b289-86ad02bd628f` |
| DeviceRequest | `272caabd-1e16-4934-8ac0-017767a8be28` |
| Encounter | `caa84e9d-170d-490b-9fec-f3633e9d6c92` |

## What the test case description says

The bundle's expected MeasureReport carries this extension on the patient's report:

```json
{
  "url": "...cqfm-testCaseDescription",
  "valueString": "Patient has dementia that starts during the measaurement period"
}
```

(Note also the typo `measaurement` — minor, but worth flagging.)

The MR lists 5 evaluated resources, one of which is `Condition/4af005f4-610d-412e-8ac7-f8399baf2f7b`. Combined with the test-case description, the implication is that this Condition encodes the dementia diagnosis driving the exclusion.

## What the Condition actually contains

```json
{
  "resourceType": "Condition",
  "id": "4af005f4-610d-412e-8ac7-f8399baf2f7b",
  "meta": {
    "profile": ["http://hl7.org/fhir/us/qicore/StructureDefinition/qicore-condition-problems-health-concerns"]
  },
  "clinicalStatus": { "coding": [{ "system": ".../condition-clinical", "code": "active" }] },
  "category": [{ "coding": [{ "system": ".../condition-category", "code": "problem-list-item" }] }],
  "code": {
    "coding": [{
      "system": "http://snomed.info/sct",
      "code": "371125006",
      "display": "Labile essential hypertension (disorder)"
    }]
  },
  "subject": { "reference": "Patient/f9ef1fd1-cced-47ad-a47b-d9c20254511c" },
  "onsetDateTime": "2026-06-30T23:59:59.000+00:00"
}
```

`sct|371125006` (Labile essential hypertension) is **not** a member of the `Advanced Illness` value set (`http://cts.nlm.nih.gov/fhir/ValueSet/2.16.840.1.113883.3.464.1003.110.12.1082`) used by `AdvancedIllnessandFrailty v1.27.000`.

## Why this breaks the test

CMS130's `Denominator Exclusions` has six branches; for this patient only the AI+Frailty branch is candidate:

```cql
define "Is Age 66 or Older with Advanced Illness and Frailty":
   AgeInYearsAt(date from end of "Measurement Period") >= 66
    and "Has Criteria Indicating Frailty"
    and ( "Has Advanced Illness in Year Before or During Measurement Period"
        or "Has Dementia Medications in Year Before or During Measurement Period" )
```

- Age = 68 → ✓
- Frailty Device retrieve picks up `DeviceRequest` (wheelchair, `sct|183240000` ∈ Frailty Device VS) → ✓
- **Advanced Illness branch:** the bundle's only Condition codes as hypertension → not in Advanced Illness VS → **false**.
- **Dementia Medications branch:** the bundled `MedicationRequest` for rivastigmine (`rxnorm|312836`) has no `dosageInstruction`, and its `dispenseRequest` only carries `expectedSupplyDuration`. Whatever the precise behavior of `CumulativeMedicationDuration.medicationRequestPeriod()` on this minimally-populated resource, no engine we tested produces an exclusion via this branch alone.

Result: `Denominator Exclusions = false`, even though the bundle's expected MR says `denominator-exclusion = 1`.

## Evidence (positive controls)

Two-line fix verified end-to-end against HAPI FHIR Server 8.8.0 (FHIR R4):

1. **Replace `code.coding` on `Condition/4af005f4-…`** with any code in the Advanced Illness VS (we used `icd-10-cm|F01.50` "Vascular dementia, unspecified severity, without behavioral disturbance"), preserving `clinicalStatus=active`, `category=problem-list-item`, and the existing `onsetDateTime` (which is already during the measurement period).
2. Re-run `$evaluate-measure` on the same patient with `periodStart=2026-01-01`, `periodEnd=2026-12-31`.

Result: the patient's `denominator-exclusion` flips from `0` → `1`, and the per-patient comparison goes from 63/64 → **64/64 matched** against the bundle's expected MeasureReports for CMS130.

In other words: with this one-resource fix, the bundle is internally consistent and a conformant engine produces the expected MR.

## Why we think this is content, not implementation

- The expected MeasureReport, the test case description, and the patient's `onsetDateTime` are all consistent with a dementia case.
- The patient resources (DeviceRequest, MedicationRequest) are exactly what AI+Frailty needs *on top of* a dementia Condition.
- The only inconsistent piece is the `Condition.code`. Most plausible cause: a synthetic-data generation step substituted a default/placeholder code (labile essential hypertension) into a slot intended to hold an Advanced Illness diagnosis.

## Suggested fix

Replace `code.coding` on `Condition/4af005f4-610d-412e-8ac7-f8399baf2f7b` with a dementia (or other Advanced Illness) code from VS `2.16.840.1.113883.3.464.1003.110.12.1082`. Any of the 93 dementia/Alzheimer ICD-10-CM codes in that VS will work; `F01.50` matches the test case description most cleanly.

While in the area, fix the typo in the test case description: `measaurement` → `measurement`.

It would also be worth a scripted audit of every per-patient Condition across the test bundles to flag cases where (a) the test case description names a clinical concept and (b) the Condition's `code` is not a member of any value set referenced by the corresponding measure's denominator-exclusion/IP/numerator logic. Same class of bug likely exists for other measures and patients.

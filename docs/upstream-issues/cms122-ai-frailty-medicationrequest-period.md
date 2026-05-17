# Draft upstream issue — AI+Frailty Dementia Medications branch not reproducible

**Status:** draft, not yet submitted
**Target repo:** `cqframework/dqm-content-qicore-2025`
**Suggested title:** `AI+Frailty Dementia Medications branch not reproducible: 24 MedicationRequests carry expectedSupplyDuration but no dosageInstruction.timing.repeat.bounds`
**Suggested labels:** `bug`, `content` (and possibly `framework` — see "Open question for maintainers" below)

---

## Summary

Across the connectathon test bundles for CMS122, CMS125, and CMS130, there are 24 `MedicationRequest` resources whose only timing information is `dispenseRequest.expectedSupplyDuration`. None of them carry `dosageInstruction.timing.repeat.bounds`, `dispenseRequest.quantity`, `dispenseRequest.numberOfRepeatsAllowed`, or `dispenseRequest.validityPeriod`. When the published measure logic is evaluated against these resources on a conformant CQL engine (HAPI FHIR 8.8.0 + the published `CumulativeMedicationDuration v6.0.000` library), the AI+Frailty Dementia Medications branch of `denominator-exclusion` does not fire for the affected patients, and their expected MeasureReports do not reproduce.

We're flagging this here first to ask whether the bundles are intended to support this exclusion branch with only `expectedSupplyDuration` populated. If the bundles are correct as authored, then the defect is on the implementation side and we will escalate to HAPI / CumulativeMedicationDuration; if the bundles are under-specified, the fix is to add the missing structural fields.

## Observed scope

| Measure | MedicationRequests with `expectedSupplyDuration` only | Patients we've directly confirmed are affected |
|---|---|---|
| CMS122FHIRDiabetesAssessGreaterThan9Percent | 7 | `3b62b0a8`, `9cba6cfa`, `cade5021`, `e61be907`, `ede0ee7a`, `f5771b74` (6 of 6 Job 5 mismatches) |
| CMS125FHIRBreastCancerScreen | 8 | 8 of the 10 mismatches we observed in a CMS125 evaluation; all 8 had a MedicationRequest in `evaluatedResource` |
| CMS130FHIRColorectalCancerScrn | 9 | `f9ef1fd1` is a confirmed instance (also affected by a separate Condition-code defect — flagged separately) |

Total across the three bundles: **24** `MedicationRequest` resources of this shape.

## The CQL path the bundles need to exercise

CMS122/125/130 share the `AdvancedIllnessandFrailty v1.27.000` library. The AI+Frailty arm of `denominator-exclusion` is:

```cql
define "Is Age 66 or Older with Advanced Illness and Frailty":
   AgeInYearsAt(date from end of "Measurement Period") >= 66
    and "Has Criteria Indicating Frailty"
    and ( "Has Advanced Illness in Year Before or During Measurement Period"
        or "Has Dementia Medications in Year Before or During Measurement Period" )
```

The Dementia Medications leg is:

```cql
define "Has Dementia Medications in Year Before or During Measurement Period":
  exists (( ([MedicationRequest: "Dementia Medications"]).isMedicationActive()) DementiaMedication
      where DementiaMedication.medicationRequestPeriod() overlaps day of Interval[
          start of "Measurement Period" - 1 year, end of "Measurement Period"
      ]
  )
```

And `CumulativeMedicationDuration v6.0.000`'s `medicationRequestPeriod()` (returns Interval<DateTime>):

```cql
define fluent function medicationRequestPeriod(Request "MedicationRequest"):
  Request R
    let
      dosage:           singleton from R.dosageInstruction,
      doseAndRate:      singleton from dosage.doseAndRate,
      timing:           dosage.timing,
      frequency:        Coalesce(timing.repeat.frequencyMax, timing.repeat.frequency),
      period:           Quantity(timing.repeat.period, timing.repeat.periodUnit),
      doseRange:        doseAndRate.dose,
      doseQuantity:     doseAndRate.dose,
      dose:             Coalesce(end of doseRange, doseQuantity),
      dosesPerDay:      Coalesce(ToDaily(frequency, period), Count(timing.repeat.timeOfDay), 1.0),
      boundsPeriod:     timing.repeat.bounds as Interval<DateTime>,
      daysSupply:       (convert R.dispenseRequest.expectedSupplyDuration to days).value,
      quantity:         R.dispenseRequest.quantity,
      refills:          Coalesce(R.dispenseRequest.numberOfRepeatsAllowed, 0),
      startDate:        Coalesce(
                          date from start of boundsPeriod,
                          date from R.authoredOn,
                          date from start of R.dispenseRequest.validityPeriod ),
      totalDaysSupplied: Coalesce(daysSupply, quantity.value / (dose.value * dosesPerDay)) * (1 + refills)
    return
      if startDate is not null and totalDaysSupplied is not null then
        Interval[startDate, startDate + Quantity(totalDaysSupplied - 1, 'day')]      -- Path A
      else if startDate is not null and boundsPeriod."high" is not null then
        Interval[startDate, date from end of boundsPeriod]                            -- Path B
      else
        null
```

## What the affected MedicationRequests look like

Representative example (the other 23 are structurally identical apart from `id`, `subject`, and `authoredOn`):

```json
{
  "resourceType": "MedicationRequest",
  "id": "ff410bba-88c6-4bce-b6ea-deea89a620d9",
  "meta": { "profile": [".../qicore-medicationrequest"] },
  "status": "active",
  "intent": "order",
  "doNotPerform": false,
  "medicationCodeableConcept": {
    "coding": [{
      "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
      "code": "312836",
      "display": "rivastigmine 6 MG Oral Capsule"
    }]
  },
  "subject": { "reference": "Patient/3b62b0a8-44f2-4365-bcb9-7cadef5bab2e" },
  "authoredOn": "2026-01-01T00:00:00.000+00:00",
  "requester": { "reference": "Practitioner/example" },
  "dispenseRequest": {
    "expectedSupplyDuration": {
      "value": 90,
      "system": "http://unitsofmeasure.org",
      "code": "days"
    }
  }
}
```

Notes:
- `dosageInstruction` is absent.
- `dispenseRequest` carries only `expectedSupplyDuration`. No `quantity`, no `numberOfRepeatsAllowed`, no `validityPeriod`.
- `expectedSupplyDuration.code` is `"days"`. UCUM canonical is `"d"`. We tested both — see below.

Tracing `medicationRequestPeriod()` against this resource:

- `dosage` and downstream timing/dose values are all `null` (no `dosageInstruction`).
- `boundsPeriod` is `null`.
- `daysSupply` should be `90` from `expectedSupplyDuration`.
- `quantity`, `dose` are `null`.
- `startDate` resolves from `authoredOn`.
- `totalDaysSupplied = Coalesce(daysSupply=90, quantity.value / (dose.value * dosesPerDay)=null/null) * (1 + 0)` should resolve to `90`.

So the trace says Path A should produce `Interval[2026-01-01, 2026-03-31]`, which overlaps `[2025-01-01, 2026-12-31]`. Empirically, that is **not** what happens on HAPI 8.8.0.

## Empirical confirmation that Path B works and Path A does not

On a HAPI 8.8.0 (R4) measure-engine container with the published `CumulativeMedicationDuration v6.0.000` loaded:

**Test 1 — baseline:** Run CMS122 `$evaluate-measure` against the bundled patient `3b62b0a8` with `periodStart=2026-01-01`, `periodEnd=2026-12-31`. Result: `denominator-exclusion = 0`, contradicting the bundle's expected MeasureReport (`= 1`).

**Test 2 — change `expectedSupplyDuration.code` from `"days"` to `"d"`:** Result unchanged. `denominator-exclusion = 0`. (Tested on patient `f9ef1fd1` in CMS130 in an earlier session.) So the failure is not a UCUM-strictness issue with `"days"` vs `"d"`.

**Test 3 — add `dosageInstruction[0].timing.repeat.boundsPeriod = { start: 2026-01-01, end: 2026-04-01 }` (preserving the existing `expectedSupplyDuration`):** Result flips. `denominator-exclusion = 1`. The patient's actual MeasureReport now matches the bundle's expected MeasureReport exactly.

So Path B (`boundsPeriod.high` not null) is producing a non-null `Interval<DateTime>` correctly. Path A (`daysSupply` from `expectedSupplyDuration`) is not — or at least is not making it past the `Coalesce → totalDaysSupplied → Interval` chain — and we don't yet have a clean isolation of why. The behavior is independent of the `"days"` / `"d"` unit code.

## Open question for maintainers

Are the bundles' MedicationRequests intended to support the AI+Frailty Dementia Medications exclusion branch with only `dispenseRequest.expectedSupplyDuration` populated, or are they expected to carry richer structural data (one of: `dosageInstruction.timing.repeat.bounds`, or `dispenseRequest.quantity` + dose-rate, etc.)?

- **If yes — the bundles are correct as authored:** then the test cases are passing on the authoring engine but not on HAPI 8.8.0 + CMD v6.0.000, indicating a likely defect in the implementation chain (HAPI's CQL evaluator, the published CumulativeMedicationDuration logic, or both). We're happy to escalate upstream to HAPI / CQF tooling with the evidence above.
- **If no — the bundles should carry richer fields:** the suggested fix is to add `dosageInstruction.timing.repeat.boundsPeriod` to each of the 24 affected MedicationRequests, with `start` = the existing `authoredOn` and `end` = `start + expectedSupplyDuration`. We've verified empirically that this one change is sufficient to reproduce the expected MeasureReports.

Either way the resolution involves measure bundle authors confirming intent. We're not in a position to make that call.

## Reproduction

1. Load the affected bundles (`CMS122FHIRDiabetesAssessGT9Pct`, `CMS125FHIRBreastCancerScreen`, `CMS130FHIRColorectalCancerScrn`) into any HAPI FHIR 8.8.0 server with the QICore IG and CumulativeMedicationDuration v6.0.000 installed.
2. POST `$evaluate-measure` for each measure against the bundled patient set, `periodStart=2026-01-01`, `periodEnd=2026-12-31`, `reportType=subject`.
3. Compare per-patient `denominator-exclusion` against the bundle's expected MeasureReports. The affected patients (six in CMS122 — `3b62b0a8`, `9cba6cfa`, `cade5021`, `e61be907`, `ede0ee7a`, `f5771b74`; eight in CMS125; one or more in CMS130) will return `0` instead of the expected `1`.
4. To verify the cause, PUT one affected MedicationRequest with the added `dosageInstruction[0].timing.repeat.boundsPeriod` and rerun. The patient's `denominator-exclusion` will flip from `0` to `1`.

## Prior art

This exact failure mode was independently observed and documented by `cqframework/clinical-reasoning` contributors in [PR #958](https://github.com/cqframework/clinical-reasoning/pull/958) ("Add unit tests to cover QICore scenarios," filed 2026, closed 2026-03-25 without merging). Verbatim from the PR description:

> **Bug or Data issues**
>
> The current 7 test failures are due to an expected criteria of 'exclusion' to be met due to advanced illness and frailty.
> In all 7 cases the patient has wheelchair and dementia medication, but the below CQL expression is failing on the `DementiaMedication.medicationRequestPeriod()` function, either through poorly defined data that doesn't list expected period or due to a bug in QICore for that particular expression.

Same patient profile (wheelchair + dementia medication), same failing fluent function, same exclusion path, same unresolved bundle-vs-implementation ambiguity. The PR was closed with "As discussed, closing this for now," and **no follow-up issue was opened** to track the question in either repository. We're filing this here to convert that open question into trackable work and to confirm authorial intent so the resolution can be routed correctly (either fix the bundle data here, or escalate to `cqframework/clinical-reasoning`).

## Related (separate) defect

We have a separate draft issue (`docs/upstream-issues/cms130-dementia-condition.md`) for `Condition/4af005f4-…` in CMS130 where the resource's `code` does not match the test case description. That is a content defect with a clear fix (correct the `code`), independent of this MedicationRequest structure question. The two can be filed separately or as a series.

# Validation Failure Recovery Guide

> **HISTORICAL (2026-04-27)** — The recovery flows described here are now
> automatic in `backend/app/services/validation.py` and `_resolve_measure_id()`;
> no manual intervention is required. EXM relative-reference resolution
> shipped in v0.0.6.7 (#108); auto-reload of missing measures is built
> into `run_validation()`. Kept for historical context only.
>
> For current debugging guidance, see `docs/testing.md` (Troubleshooting
> measure IP undercount) and the HAPI async-indexing section of `CLAUDE.md`.

## Problem

Validation runs can fail for two distinct reasons:

1. **Measure not found on engine** — the HAPI measure engine loses its data (e.g., container restart), while expected results persist in PostgreSQL. Root cause: Measure resources have been lost from HAPI.

2. **EXM FHIR4 validation runs always fail** — EXM test bundles store `MeasureReport.measure` as a relative reference (`Measure/{id}`) rather than a canonical URL. The resolver previously only searched HAPI via `?url=`, which only matches canonical URLs, so EXM measures could never be resolved. Fix: `_resolve_measure_id()` now detects relative references and fetches them by direct `GET /Measure/{id}` instead. (v0.0.6.7, #108)

The three recovery strategies below address the first problem. The second problem is fixed in code and requires no manual action.

## Solution: Three-Pronged Fix

### Option 1: Automatic Lazy Measure Loading (Recommended)

**When it activates:** Automatically on the next validation run.

**How it works:**
1. When a validation run starts, it checks if all required measures exist on HAPI
2. If any are missing, the system automatically attempts to reload them from `seed/connectathon-bundles/`
3. If reload succeeds, validation proceeds normally
4. If reload fails, the user gets a clear error message with recovery instructions

**No action needed.** This fix is now built into `run_validation()` in `backend/app/services/validation.py`.

**Code:**
```python
# In run_validation() - lines ~500-550
if missing_measures:
    logger.warning("Measures not found on engine — attempting to reload...")
    try:
        reload_result = await _reload_measures_from_seed_bundles()
        # Retry resolving measures after reload
        ...
    except Exception as exc:
        # Clear error message with recovery instructions
```

**Benefits:**
- Transparent to the user
- Self-healing when measures are lost
- Attempts recovery before failing

---

### Option 2: Improved Error Messages

**When it activates:** When measures can't be loaded, either at upload or validation time.

**Changes:**

**A) Bundle Upload Errors** (`triage_test_bundle()`)
```
Old: "[Silent failure or generic HAPI error]"
New: "Failed to upload measures to HAPI measure engine. 
      Ensure the measure engine is running and accessible. 
      Details: [actual error]"
```

**B) Validation Run Errors** (`run_validation()`)
```
Old: "Measure not found on engine: [url]"
New: "Measures not found on engine after reload attempt. 
      This may indicate the HAPI measure engine is unavailable or the seed bundles are missing. 
      Please ensure the backend is properly connected to the measure engine, 
      or manually upload test bundles using the Validation page. 
      Missing measures: [list]"
```

**Benefits:**
- Users understand what went wrong
- Clear recovery steps provided
- Guidance on how to manually recover if needed

---

### Option 3: Manual Re-upload Script

**When to use:** If automatic reload fails or doesn't trigger, or to force a fresh upload of all bundles.

**Usage:**
```bash
# From repo root, upload to local dev server:
./scripts/reload-validation-bundles.sh

# Or specify a different API endpoint:
./scripts/reload-validation-bundles.sh https://api.example.com
```

**What it does:**
1. Scans `seed/connectathon-bundles/`
2. Uploads each bundle via the `/validation/upload-bundle` endpoint
3. Reports success/failure for each bundle
4. Measures are extracted and pushed to HAPI during processing

**Example output:**
```
Reloading validation bundles from: seed/connectathon-bundles
API endpoint: http://localhost:8000

Found 12 bundles to upload...

Uploading CMS2FHIRPCSDepressionScreenAndFollowUp-bundle.json... ✓ (ID: 1)
Uploading CMS122FHIRDiabetesAssessGreaterThan9Percent-bundle.json... ✓ (ID: 2)
...

==========================================
Upload summary:
  Uploaded: 12
  Failed: 0
==========================================

✓ All bundles uploaded successfully!
```

**Note:** After upload, check the Validation page to verify uploads completed and measures are loaded.

---

## How They Work Together

```
User clicks "Run Validation"
    ↓
[Option 1] Validation checks if measures exist on HAPI
    ├─ If found → proceed with validation
    └─ If missing:
         ├─ Attempt auto-reload from seed bundles
         │   ├─ Success → retry resolution, proceed
         │   └─ Failure:
         │       └─ [Option 2] Show clear error with recovery steps
         │           └─ User runs: [Option 3] ./scripts/reload-validation-bundles.sh
         │               └─ Next validation attempt succeeds
```

---

## Implementation Details

### Code Changes

**File: `backend/app/services/validation.py`**

1. **New function `_reload_measures_from_seed_bundles()`** (lines ~424-471)
   - Loads seed bundles from disk
   - Extracts Measure/Library resources
   - Pushes them to HAPI
   - Returns counts and errors

2. **Modified `run_validation()`** (lines ~500-550)
   - Tracks missing measures
   - Triggers lazy reload if any are missing
   - Retries resolution after reload
   - Provides clear error message on failure

3. **Modified `triage_test_bundle()`** (lines ~305-323)
   - Wrapped measure push in try-except
   - Provides clear error if upload fails

### Testing

**Simulate the issue locally:**
```bash
# Start services
docker-compose up -d

# Wait for services to start
sleep 10

# Upload a bundle
curl -X POST -F "file=@seed/connectathon-bundles/CMS2FHIRPCSDepressionScreenAndFollowUp-bundle.json" \
  http://localhost:8000/validation/upload-bundle

# Verify expected results are loaded
curl http://localhost:8000/validation/expected

# Wipe HAPI data (simulate the problem)
docker-compose exec hapi-fhir-measure rm -rf /data/hapi

# Try validation - should now auto-recover
curl -X POST http://localhost:8000/validation/run

# Verify it succeeded or gave clear error
```

---

## Deployment Notes

- **No database migration needed** - Code only, no schema changes
- **Backward compatible** - Old validation runs will still fail the same way; only new runs benefit from recovery
- **No configuration needed** - Automatically activates
- **Seed bundles must exist** - If `seed/connectathon-bundles/` is missing or empty, automatic recovery won't work

---

## Monitoring

Check backend logs for recovery attempts:
```bash
docker logs lenny-backend-1 2>&1 | grep -E "missing_measures|Reloaded measures|attempting to reload"
```

Example log output:
```
WARNING: Measures not found on engine — attempting to reload from seed bundles
INFO: Reloaded measures from seed bundle CMS2FHIRPCSDepressionScreenAndFollowUp-bundle.json
INFO: Seed bundle reload complete measures_loaded=1 libraries_loaded=0 failed=0
```

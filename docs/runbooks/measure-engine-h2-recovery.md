# Runbook: Measure Engine H2 Recovery

## Symptoms

Use this runbook when ALL of the following are true:

- `/jobs <any-measure>` returns IPP=0 / denom=0 / numer=0 for every in-scope patient
- `$evaluate-measure` directly on the measure engine returns zero populations
- **Container restart alone does NOT fix it** (state persists in the `leonard_measuredata` Docker volume)
- CDR data is intact: `Patient/{id}/$everything` returns full clinical bundles from the CDR

This pattern indicates the measure engine's H2 store (`/data/hapi/h2.mv.db`) has
accumulated bad Library/ValueSet/CodeSystem state. The most common cause is
Library/ValueSet rows accumulating across many `wipe_patient_data` + `push_resources`
cycles until canonical resolution returns the wrong (or no) version, causing
`$evaluate-measure` to return zeros.

**Do NOT use this runbook** for:
- IPP > 0 but wrong count (wrong populations, not zero populations) — that is likely
  a data or CQL logic issue
- CDR returning empty `$everything` — that is a CDR indexing issue, not an H2 issue
- A single measure returning zero while others return non-zero

## Why restart doesn't help

`docker restart` reloads the container but keeps the same volume. The H2 file at
`/data/hapi/h2.mv.db` persists across restarts. Only removing the volume forces a
fresh import.

The `leonard_measuredata` volume is **separate from `leonard_cdrdata`**. Wiping the
measure engine volume does NOT affect clinical patient data on the CDR.

## Procedure

Run from the EC2 instance (SSH or via AWS SSM session) as root or with sudo:

```bash
cd /opt/leonard

# 1. Bring down the stack
docker compose -f docker-compose.yml -f docker-compose.prod.yml down

# 2. Wipe ONLY the measure engine volume — cdrdata is untouched
docker volume rm leonard_measuredata

# 3. Bring everything back up
#    The `seed` service runs once on startup and repopulates ME from the
#    connectathon bundles at /opt/leonard/seed/connectathon-bundles
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 4. Wait for the seed service to finish
docker logs -f leonard-seed-1 | grep -E "Lenny seed data loaded successfully|ERROR"
# Ctrl-C once you see "Lenny seed data loaded successfully"
```

Total downtime: ~5 min (seed completes in ~5 min on a t3.medium with the
prebaked CDR image; the measure engine is already in the same image, no IG
load required).

## Verification

### Step 1: Check resource counts on the measure engine

```bash
docker exec leonard-backend-1 python3 - <<'EOF'
import httpx
for rt in ("Patient", "Encounter", "Observation", "Condition",
           "Measure", "Library", "ValueSet"):
    r = httpx.get(f"http://hapi-fhir-measure:8080/fhir/{rt}?_summary=count", timeout=10)
    print(f"{rt}: {r.json().get('total')}")
EOF
```

Expected baseline (connectathon seed data):

| Resource | Expected |
|----------|----------|
| Patient | 568 |
| Encounter | 793 |
| Observation | 234 |
| Condition | 382 |
| Measure | ≥ 12 |
| Library | 24 |
| ValueSet | ≈ 123 |

If any count is 0, the seed did not complete. Re-check `docker logs leonard-seed-1`.

### Step 2: Run a /jobs cycle and verify populations

```bash
curl -s -X POST https://api.lenny.bellese.dev/jobs \
  -H "Content-Type: application/json" \
  -d '{"measure_id":"CMS124FHIRCervicalCancerScreening",
       "period_start":"2026-01-01","period_end":"2026-12-31",
       "group_id":"CMS124FHIRCervicalCancerScreening"}' \
  | jq .id
```

Wait for the job to complete, then check populations:

```bash
JOB_ID=<id-from-above>
curl -s "https://api.lenny.bellese.dev/jobs/$JOB_ID" | jq '{status,total_patients}'
curl -s "https://api.lenny.bellese.dev/jobs/$JOB_ID/comparison" | jq .
```

Expected CMS124 with connectathon group filter: `matched=33/33  IPP=29  denom=29  numer=4  denom-excl=16`.

If IPP is still 0 after a successful seed, the issue is not H2 corruption — see
the HAPI async-indexing notes in `CLAUDE.md` for next steps.

## Related issues

- [#187](https://github.com/Bellese/lenny/issues/187) — root cause investigation (H2 corruption, RESOLVED via this procedure)
- [#188](https://github.com/Bellese/lenny/issues/188) — orchestrator wipe-races-push (separate issue; fixing #188 reduces the frequency of H2 drift)

# Runbook: Factory Reset

Use this runbook when you want Lenny in a known-good blank state — e.g., before a Connectathon demo, after a corrupted test run, or when starting from scratch.

**What "factory reset" covers:**
- All patient and clinical data on the CDR (Patient, Encounter, Condition, etc.)
- All clinical and definition resources on the measure engine (Measure, Library, ValueSet, plus patient-level data from prior jobs)
- All job history, measure results, validation runs, and expected results in the app database

**What it preserves:**
- CDR and MCS connection settings — your configured connections remain
- The app itself — no containers are stopped

**After a factory reset**, run a re-seed to restore the standard connectathon demo dataset. The reset and re-seed are separate steps by design.

---

## Tier 1: Admin panel (preferred)

1. Open Lenny → Settings → Admin tab.
2. Find the **Factory Reset** card.
3. Click **"Reset everything"**.
4. Review the modal — it lists what will be deleted and names the active CDR. Click **"Reset everything"** to confirm.
5. Watch the step list fill in. The reset completes in under 60 seconds.
6. Click **"Re-seed bundles"** to restore the connectathon demo dataset (~5 min on prod).

---

## Tier 2: curl (when the UI is broken but the backend responds)

```bash
BASE=https://api.lenny.bellese.dev   # or http://localhost:8000 for local

# 1. Trigger factory reset
OP=$(curl -s -X POST "$BASE/settings/admin/factory-reset" \
  -H "Content-Type: application/json" \
  -d '{"include_cdr":true,"include_measure_engine":true,"include_app_db":true}' \
  | jq -r .operation_id)
echo "operation_id: $OP"

# 2. Poll until succeeded/failed (check every 5s, up to 2 min)
for i in $(seq 1 24); do
  STATUS=$(curl -s "$BASE/settings/admin/operations/$OP" | jq -r .status)
  echo "$(date '+%H:%M:%S') $STATUS"
  [ "$STATUS" = "succeeded" ] && break
  [ "$STATUS" = "failed" ] && { echo "FAILED"; curl -s "$BASE/settings/admin/operations/$OP" | jq .error; exit 1; }
  sleep 5
done

# 3. Re-seed
OP_SEED=$(curl -s -X POST "$BASE/settings/admin/reseed-bundles" | jq -r .operation_id)
echo "reseed operation_id: $OP_SEED"

# 4. Poll reseed (can take ~5 min on prod)
for i in $(seq 1 72); do
  STATUS=$(curl -s "$BASE/settings/admin/operations/$OP_SEED" | jq -r .status)
  echo "$(date '+%H:%M:%S') $STATUS"
  [ "$STATUS" = "succeeded" ] && { echo "Reseed complete"; break; }
  [ "$STATUS" = "failed" ] && { echo "Reseed FAILED"; curl -s "$BASE/settings/admin/operations/$OP_SEED" | jq .error; exit 1; }
  sleep 5
done
```

---

## Tier 3: Full container nuke (when the backend won't boot)

Use this only as a last resort — it removes all data and requires a full restart.

### Local

```bash
cd /path/to/lenny

# Bring everything down
docker compose down

# Remove data volumes (CDR, measure engine, Postgres)
docker volume rm leonard_cdrdata leonard_measuredata leonard_pgdata

# Bring everything back up
docker compose up -d

# (For prebaked local stack)
# cp .env.example .env
# docker compose up -d
```

The seed service (or prebaked images) will repopulate HAPI automatically on next boot.

### Production (EC2 — SSH or AWS SSM Session)

```bash
cd /opt/leonard

# 1. Bring everything down
docker compose -f docker-compose.yml -f docker-compose.prod.yml down

# 2. Remove data volumes
docker volume rm leonard_cdrdata leonard_measuredata leonard_pgdata

# 3. Bring everything back up (deploys via normal deploy flow)
sudo ./scripts/deploy-prod.sh

# 4. Wait for the seed service to finish loading connectathon bundles
docker logs -f leonard-seed-1 | grep -E "Lenny seed data loaded successfully|ERROR"
# Ctrl-C once you see "Lenny seed data loaded successfully"
```

Total downtime: ~5–10 min (HAPI IG download + seed).

> **Note:** `docker volume rm leonard_pgdata` also removes all saved CDR and MCS connection configs. You will need to reconfigure connections via Settings → Connections after the restart.

---

## Verification

After reset + re-seed, confirm the system is healthy:

```bash
# Health check
curl -s https://api.lenny.bellese.dev/health | jq '{status,database,measure_engine,cdr}'

# Resource counts on the measure engine
docker exec leonard-backend-1 python3 - <<'EOF'
import httpx
for rt in ("Patient", "Encounter", "Observation", "Condition",
           "Measure", "Library", "ValueSet"):
    r = httpx.get(f"http://hapi-fhir-measure:8080/fhir/{rt}?_summary=count", timeout=10)
    print(f"{rt}: {r.json().get('total')}")
EOF
```

Expected baselines after re-seed (from `docs/runbooks/measure-engine-h2-recovery.md`):

| Resource | Expected |
|----------|----------|
| Patient | 568 |
| Encounter | 793 |
| Observation | 234 |
| Condition | 382 |
| Measure | ≥ 12 |
| Library | 24 |
| ValueSet | ≈ 123 |

---

## Related runbooks

- `docs/runbooks/measure-engine-h2-recovery.md` — recover from measure engine H2 corruption (subset of factory reset; use when only the measure engine is corrupted)
- `docs/runbooks/local-vs-prod-deploy.md` — understand the differences between local and prod deploys

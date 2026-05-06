# `$everything` Probe — verifying HAPI index readiness

After any wipe+push cycle in a local smoke run, confirm that `Patient/{id}/$everything` returns the **full clinical bundle** (not just the Patient resource). If only the Patient comes back, the HAPI index has not caught up — see the "Recurring bug: HAPI async-indexing race" section of `CLAUDE.md`.

The shell strips `$` from these URLs, so probe via Python from inside the backend container:

```bash
docker exec leonard-backend-1 python3 -c "
import httpx, sys
pid = sys.argv[1]
r = httpx.get(f'http://hapi-fhir-measure:8080/fhir/Patient/{pid}/\$everything', timeout=30)
types = {e['resource']['resourceType'] for e in r.json().get('entry', [])}
print('resource types in bundle:', types)
assert 'Encounter' in types, 'FAIL: \$everything returned only Patient — HAPI index not ready'
" <a-patient-id-in-scope>
```

If the assertion fails:

1. Confirm async-indexing is the culprit using the triage rule in `CLAUDE.md`: read the resource directly (`GET /{Type}/{id}`) and compare to what the search endpoint returns. If the direct read has data and the search doesn't, `synchronization.strategy=sync` is not taking effect — check that both HAPI services were restarted with the updated docker-compose config.
2. If still failing under load, see issue #206 — `hibernate.search.indexing.plan.synchronization.strategy=sync` on both HAPI services (applied in PR #206, compensator removed in PR #214) is the structural fix.

Replace `<a-patient-id-in-scope>` with a patient that should have data, e.g. one of the Connectathon seed IDs from `docs/connectathon-measures-status.md`.

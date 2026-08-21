# Architectural Decisions — Lenny

This log records significant technical and process choices with their rationale. When you make a decision that would be non-obvious to someone joining the project next month, add it here. Format: what we decided, why, and any alternatives considered.

---

## ADR-001: Python + React stack (2026-03-22)

**Decision:** Python/FastAPI backend, React (plain JS) frontend.

**Why:** Python has the broadest FHIR library ecosystem and is most accessible to health IT developers. React provides a familiar, well-documented UI layer without requiring TypeScript.

**Alternatives considered:** Node/Express backend (less FHIR library support), TypeScript frontend (unnecessary complexity at this stage).

---

## ADR-002: Two separate HAPI FHIR instances (2026-03-22)

**Decision:** Run distinct Docker containers for the CDR (clinical data repository) and the Measure Engine.

**Why:** The CDR is replaceable — users connect their own organization's FHIR server via Settings. The Measure Engine is permanent and requires `hapi.fhir.cr.enabled=true` for CQL evaluation. Mixing these roles in one instance would prevent users from swapping out the CDR.

**Alternatives considered:** Single HAPI instance (makes CDR replacement harder), using an external measure evaluation service (adds external dependency).

---

## ADR-003: Python 3.10+ target (2026-04-07)

**Decision:** Target Python 3.10 and above. Modern union syntax (`X | None`) is the preferred style over `Optional[X]`.

**Why:** 3.10 union syntax is cleaner and more readable. No meaningful deployment constraints require 3.9 support.

---

## ADR-004: No human review queue in Kanban (2026-04-07)

**Decision:** The board uses five statuses: Backlog, Ready, In Progress, Done, Withdrawn. "Ready for Review" and "In Review" are not used.

**Why:** AI-assisted review (`/review`) runs pre-landing. If a shipped change is wrong, a new issue is opened and the change is reverted or corrected in a follow-on PR. This keeps cycle time short and avoids work accumulating in review queues.

---

## ADR-005: GitHub Issues as the single work tracker (2026-04-07)

**Decision:** All work — development, research, persona development, and backlog items — is tracked in GitHub Issues on the project board. `TODOS.md` will be migrated to Issues and deleted.

**Why:** Issues integrate with PRs (auto-close on merge), provide a shared view for the whole team, and are queryable by Claude and other AI tooling. `TODOS.md` was a workaround that predated this structure.

---

## ADR-007: Test infrastructure — ruff, pytest-cov, and CI gate (2026-04-09)

**Decision:** Add ruff for linting/formatting, pytest-cov with a 70% coverage floor, and a 4-job GitHub Actions PR gate (unit tests + coverage, lint, integration tests, frontend build).

**Why:** The project was accreting tests without any enforcement mechanism. A CI gate prevents regressions from merging silently. 70% is the initial floor — high enough to catch uncovered code, low enough not to block early-stage feature work. Ruff replaces ad-hoc formatting decisions with a single enforced standard. Test deps are split into `requirements-test.txt` to keep the production image lean.

**Alternatives considered:** Black + flake8 (more tools, more config), 80% coverage floor (too aggressive for current codebase state), no CI gate (status quo, unacceptable as the team grows).

---

## ADR-006: gstack + superpowers skill chaining (2026-04-07)

**Decision:** Use gstack for the outer loop (ideation, shipping, QA, browsing) and superpowers for the inner loop (TDD, worktrees, subagent-driven execution). One tool is recommended per workflow phase.

**Why:** The two toolkits are complementary rather than overlapping. Picking one per phase removes ambiguity for both humans and agents. The current approach uses both as-is; the plan is to modify them to fit our needs over time, eventually evolving toward a Bellese-specific skill stack.

**Reference:** `docs/workflow.md`

---

## ADR-008: webpack-dev-server Dependabot alerts #7 and #8 — dismissed as not-used (2026-05-11)

**Decision:** Dismiss Dependabot alerts #7 (`GHSA-4v9v-hfq4-rm2v`) and #8 (`GHSA-9jgg-88mc-972h`) for `webpack-dev-server` with reason "vulnerable code is not actually used in production" rather than upgrading to 5.x.

**Why:** The only patched version is `webpack-dev-server@5.2.1`. No 4.x backport exists; the latest 4.x release is `4.15.2`, which is within the vulnerable range. Upgrading to 5.x via an npm `overrides` block would silently break `npm start` (local dev server): `react-scripts@5.0.1` hard-codes 4.x-only APIs (`onBeforeSetupMiddleware`, `onAfterSetupMiddleware`, `static.directory`) that were removed in webpack-dev-server 5. CI's `frontend-build` job runs only `npm run build` — it would pass green while `npm start` breaks, giving a false sense of safety.

`webpack-dev-server` is not included in the production artifact. The `frontend/Dockerfile` runtime stage copies the static `build/` directory and serves it via `serve@14` — webpack-dev-server is never installed or invoked in the deployed image. Neither CVE is reachable by end users.

**Alternatives considered:** Upgrading or replacing `react-scripts` (CRA successor migration) — deferred as a multi-week refactor unrelated to these alerts. Accepting 5.x with a broken dev server — rejected because it masks a real developer experience regression behind a green CI build.

**Follow-up (2026-05-18):** Alert #28 (`GHSA-79cf-xcqc-c78w` / CVE-2026-6402) is a bypass of the v5.2.1 fix from GHSA-4v9v-hfq4-rm2v (alert #7): browsers don't send `Sec-Fetch-*` headers over plain HTTP, so the header-based block introduced in 5.2.1 is ineffective on the non-HTTPS default. The patched version is `5.2.4`, still 5.x, so the `react-scripts@5.0.1` 4.x-API incompatibility and the CI-stays-green-while-npm-start-breaks failure mode are unchanged. Production image (`serve@14` on static `build/`) still doesn't include webpack-dev-server. Alert #28 dismissed with `dismissed_reason=not_used` under the same rationale. This is the third iteration of this vulnerability class on the same dev-only dependency — future alerts in this family will follow the same disposition until `react-scripts` is replaced.

**Follow-up (2026-05-18):** Alert #29 (`GHSA-58qx-3vcg-4xpx` / CVE-2026-45736) is a different package than the prior chain — it affects `ws` (the WebSocket library), a transitive dep of `webpack-dev-server@4.15.2`, not webpack-dev-server itself. The vulnerability: `ws.close()` leaks uninitialized memory when a `TypedArray` is passed as the reason argument (CWE-908). CVSS 4.4 medium; `AV:N/AC:H/PR:H` — requires high privilege; advisory notes actual severity is "believed to be low." Fix: `ws@8.20.1` (patch bump within the existing `^8.13.0` constraint — no breaking changes). Unlike the prior WDS 4→5 dismissals, this fix is safe: added a scoped npm override (`"webpack-dev-server": { "ws": ">=8.20.1" }`) in `frontend/package.json`, updating the lockfile from `8.20.0` → `8.20.1` without touching `jsdom`'s `ws@7.5.10` or any other entry.

---

## ADR-009: Bundle chunking is per-CDR-connection, not global (2026-05-16)

**Decision:** Configure `max_bundle_entries` as a column on each `CDRConfig` row, not as a global app setting.

**Why:** Different CDRs enforce different per-bundle entry caps (Firely Sandbox = 200; AWS HealthLake ≈ 500; the local HAPI bundled in `docker-compose.yml` has no practical cap). A global setting would either over-chunk (extra round-trips against HAPI) or under-chunk (Firely rejects). Per-connection lets each row carry its own constraint.

**Alternatives considered:** Auto-detect via `CapabilityStatement` — no FHIR-standard element advertises bundle-entry caps. Binary-search probing the cap on first push was considered but the latency cost and the brittleness of "what counts as 'too large'" made manual config the better trade for now.

**Status:** Shipped in #321.

---

## ADR-010: Fix CMS122 canonical-URL drift between seed and connectathon bundles (2026-05-18)

**Decision:** Replace the abbreviated `CMS122FHIRDiabetesAssessGT9Pct` Measure and Library in `seed/measure-bundle.json` with the full MADiE canonical forms (`CMS122FHIRDiabetesAssessGreaterThan9Percent`) from the connectathon bundle. Add `_assert_no_canonical_url_clash()` in `validation.py` to fail loudly if a future bundle upload would introduce the same class of drift.

**Root cause (issue #319):** The bake workflow runs in two phases: Phase 1 seeds `seed/measure-bundle.json` (which contained the abbreviated GT9Pct IDs), then Phase 2 loads all 7 connectathon bundles (which use full GreaterThan9Percent IDs). Both sets of resources were PUT into HAPI by separate IDs, so both coexisted. The comparison endpoint resolved the abbreviated canonical URL from the Job's `measure_id` → found 0 `ExpectedResult` rows (which were written under the full URL) → returned `has_expected: false`.

**Scope of the drift:** Only CMS122 was affected. The other 6 measures (`CMS124`, `CMS125`, `CMS130`, `CMS506`, `CMS529`, `CMS816`) are not present in `seed/measure-bundle.json` at all, so no seed-vs-bundle ID mismatch is possible for them.

**Defense-in-depth:** `_assert_no_canonical_url_clash()` queries `Measure?url={canonical}` before every `push_resources` call during bundle upload. If HAPI already has a Measure at that canonical URL under a different FHIR ID, the upload fails with a clear error message referencing this ADR.

**Alternatives considered:** (a) Canonical-URL normalisation in the comparison endpoint (would mask drift rather than prevent it). (b) Dedup-by-canonical-URL in `push_resources` (silent PUT-overwrite masks the problem and loses the original resource). (c) No seed-file change — runtime normalisation only (would not fix the prebaked images; a rebake would still leave two resources).

---

## ADR-011: MCS credentials are read from the live config, and every MCS interaction targets the job's MCS (2026-08-06)

**Decision:** Add `_get_mcs_auth_headers(job_id)` resolving credentials from the live `MCSConfig` via `Job.mcs_id`, and route all four measure-engine interactions — wipe, push, `$evaluate-measure`, and evaluated-resource snapshot — at the job's MCS with those credentials.

**Root cause:** MCS connections wired the *URL* through to jobs (`Job.mcs_url`, `_get_mcs_url()`) but never the *credentials*. `evaluate_measure()` had no auth parameter at all, and `push_resources()` / `wipe_patient_data()` / `resolve_evaluated_resource()` all defaulted to `settings.MEASURE_ENGINE_URL`. A job against a remote MCS therefore pushed patient data to the local engine and evaluated against the remote one without auth — every patient failed `HTTP 401 "Authorization header missing Bearer token"`.

**Why credentials live on the config, not the job:** `Job` snapshots `mcs_url`/`mcs_name`/`mcs_id` so job rendering never depends on current config state. Credentials are deliberately excluded from that snapshot and read live, matching `_get_cdr_auth_headers()`. Duplicating secrets onto every job row would multiply the blast radius of a database disclosure and leave stale tokens scattered across history. The cost is that deleting an MCS config makes its jobs unrunnable — handled by raising a clear error rather than silently evaluating unauthenticated.

**Why the wipe moved above MCS resolution in `run_job`:** the wipe must clear the same server the job will push to and evaluate against. Wiping a different server leaves the real target's prior-run patients in place, and those inflate the next evaluation's populations — a silent wrong answer rather than a visible failure.

**Consequence — the wipe was destructive against shared infrastructure.** Pointing the wipe at the job's MCS meant Lenny deleted all patient data on a remote MCS at job start. Correct for a dedicated engine, dangerous for a shared connectathon server. Tracked in #392 and **resolved in ADR-012** — the scoped wipe anticipated here is what shipped.

**Alternatives considered:** (a) Snapshot credentials onto `Job` — rejected, secret sprawl. (b) Add `mcs_auth_type` mirroring `cdr_auth_type` — unnecessary: `mcs_id` alone distinguishes "no MCS linked" from "config deleted", so no migration was needed. (c) Fix only the 401 and leave push targeting the local engine — rejected, would have produced clean `200`s with every population at zero.

**Status:** Verified against the CMS connectathon server — 401s went 122 → 0, all responses 200, and patient data reached the remote MCS (0 → 56 Patients). Remaining failures there are server-side (`HSEARCH800001`, Hibernate Search not initialized), not client-side.

---

## ADR-012: The pre-job wipe is patient-scoped by default; full wipe is per-connection opt-in (2026-08-19)

**Decision:** `run_job` no longer deletes every patient on the target measure engine. It deletes only the patients the job is about to push, via a new `fhir_client.wipe_patients_by_id()`. The historical full wipe survives behind a per-connection `MCSConfig.wipe_before_job` flag, snapshotted onto `Job.mcs_wipe_before_job` at creation. The flag is `false` for every connection a user creates and `true` only for the seeded "Local Measure Engine".

**Root cause (issue #392):** ADR-011 correctly pointed the wipe at the job's MCS, which made a previously-safe operation destructive: an attendee pointing Lenny at a shared connectathon server deleted every other participant's test data at job start, with no prompt, no warning-level log, and no undo.

**Why scoping is not a correctness compromise.** `evaluate_measure()` calls `$evaluate-measure?...&subject=Patient/<id>` per patient, and the job aggregates only over the patients it gathered. Resources belonging to patients the job never evaluates cannot affect its populations — so the stale data the full wipe existed to remove is exactly the patient-scoped subset the new wipe removes. Verified empirically: the same measure run in scoped mode and full-wipe mode against the same engine produced identical counts (initial-population 135, denominator 135, numerator 8, denominator-exclusion 41). Note this reasoning is load-bearing on per-subject evaluation: if a future change adds a population-level `reportType=summary` evaluation (issue #273), scoping alone would no longer be sufficient and this ADR must be revisited.

**Why the flag defaults to false, and why locality is not inferred.** The seeded local engine's URL is `http://hapi-fhir-measure:8080/fhir`, so an "is this localhost?" predicate would misclassify Lenny's own container. The flag is set explicitly at seed time instead, and the migration backfill is wrapped in a column-absence guard so it runs exactly once — an unguarded `UPDATE` would re-enable the destructive mode on every restart for a user who had turned it off.

**Why the wipe moved after the patient gather.** The scoped wipe needs the patient IDs. One user-visible consequence: a job that gathers zero patients no longer wipes at all, so an empty job is no longer a way to clear the local engine.

**Search parameters were probed, not read off the spec.** HAPI answers `patient=` for most clinical types, `subject=` for `AdverseEvent` (which 400s on `patient=`), and `_id=` for `Patient` itself. `Medication`, `Location`, `Practitioner`, and `Organization` reject all of them and are therefore skipped — they are shared infrastructure on a multi-tenant server and are re-`PUT` by ID on every push. `Patient` must be deleted last or HAPI 409s while clinical resources still reference it.

**Alternatives considered:** (a) Flag-only, keeping the full wipe as the sole mechanism (issue #392's stated minimum) — rejected: a remote user wanting correctness would still have to nuke a shared server. (b) Scoped wipe only, no flag — rejected: the local engine would accumulate prior jobs' patients indefinitely, growing the dataset and slowing factory reset (#399). (c) Inferring locality from the URL — rejected, see above.

**Known limitation:** the abort threshold counts *consecutive* transport failures, so a server that fails intermittently can defeat it and the wipe reports success with some deletes unapplied. This predates #392 in `wipe_patient_data`, but chunking makes it more reachable (19 types × ceil(N/50) requests instead of 23). Tightening it was deliberately deferred — failing every job against a merely-flaky remote is a worse default.

---

## ADR-013: Destructive admin operations follow the active MCS and honor read-only; job views follow the job's MCS (2026-08-19)

**Decision:** Two different rules, applied deliberately, for the two halves of issue #397.

Admin/destructive paths follow the **active MCS connection**: `wipe_measure_definitions()` takes a required `base_url`, `POST /admin/wipe-measure-engine` resolves the active MCS and returns 403 when it is read-only, and factory reset's measure-engine branch resolves the active MCS from the DB. Job-scoped views follow **`job.mcs_url`** instead — the comparison endpoint, `$data-requirements`, and evaluated-resource resolution all resolve against the server the job actually ran on.

**Why the rules differ.** An admin clicking "wipe measure engine" means the server they are looking at right now. A historical job's comparison view means the server that job ran against, which may no longer be active. Using the active MCS for the second case would make an old job's results silently re-interpret against a different server. Both were reading `settings.MEASURE_ENGINE_URL`, which is neither.

**Read-only enforcement is not uniform, and that is intentional.** The route returns 403 — there is a caller waiting for an answer. Factory reset records the step as `skipped` with a reason and continues, because it is a multi-step background operation: raising would leave `include_app_db` undone and strand the operation in `failed`, a worse outcome than declining the one step whose target Lenny does not own. The CDR branch got the same treatment even though #397 only asked about the MCS: factory reset ignored `is_read_only` entirely, so guarding one branch would have left the same operation refusing on the measure engine while wiping a CDR the user had flagged.

**Why the comparison endpoint now returns 502.** It previously swallowed every failure into a 200 with an empty result, which the UI renders as "No expected results available — load a connectathon bundle via Settings." That is a wrong diagnosis pointed at the user: it sends them to load data they already have when the actual fault is auth or connectivity. `ComparisonView` already renders `Comparison unavailable: {error}` on a rejected fetch and simply never received one. Three cases are now distinguished: 200-empty when the engine answers and the measure is absent (a real empty result), 502 "rejected — check credentials" on 401/403, and 502 "could not resolve" otherwise. Collapsing the 404 into 502 would have replaced one misleading message with another.

**Why job MCS credential resolution moved to `dependencies.resolve_job_mcs_auth_headers`.** The logic lived in `orchestrator._get_mcs_auth_headers`, which opens its own DB session. Calling that from a request handler that already holds one opens a second, and in tests it reaches for a database nobody patched. The rules — including the URL-drift guard that refuses to send a config's credentials to a snapshotted URL the config no longer points at — now live in one session-taking function; the orchestrator keeps a thin wrapper that owns its session. Duplicating a check that decides whether to hand credentials to a host is exactly the kind of thing that rots apart.

**Enforced, not asserted:** `tests/test_mcs_scoping_inventory.py` pins the per-file count of remaining `settings.MEASURE_ENGINE_URL` reads using `ast` (grep cannot distinguish a real attribute access from the many docstrings that mention the name). Any new read fails the suite; each remaining one is annotated as either legitimate or an outstanding defect with the slice that will clear it.

**Alternatives considered:** (a) Using the active MCS everywhere, including job views — rejected, it makes a historical job's numbers depend on current configuration. (b) Keeping the comparison endpoint's 200-with-empty and only fixing the scoping — rejected, it leaves the wrong message on screen, which is the part users actually experience. (c) Raising instead of skipping on a read-only factory-reset target — rejected, see above. (d) Guarding read-only on the MCS only, per the issue's literal wording — rejected, the inconsistency inside one operation would read as a bug and invite a wrong fix.

**Still outstanding after this ADR:** the validation pipeline (9 reads in `validation.py`) and `bundle_loader.py`'s re-seed target. The inventory guard holds their counts so they cannot grow.

---

## ADR-014: The validation pipeline is MCS-scoped by threading a frozen `McsTarget`, and its wipe was a second copy of #392 (2026-08-20)

**Decision:** Every measure-engine interaction in `app/services/validation.py` now takes a required, frozen `McsTarget(url, auth_headers, is_read_only, wipe_before_job)` (defined in `fhir_client.py`). The target is resolved once at each of the three entry points — `process_bundle_upload` from the active connection, the boot seed path explicitly from `settings` (`bundle_loader.py:88-98`; seeding deliberately targets Lenny's own engine, see below), `run_validation` from a per-run MCS snapshot on `validation_runs` — and threaded down as a keyword-only `*, mcs:` parameter through roughly sixteen call sites. `run_validation`'s pre-run wipe becomes patient-scoped, and both write-side entry points refuse a read-only target.

**Root cause — the site count was 16, not the 9 the issue reported.** Issue #397 lists nine `settings.MEASURE_ENGINE_URL` reads in `validation.py`. The real number of unscoped measure-engine interactions was sixteen. The other seven never read the env var at all: they called `push_resources(...)` (5 sites) or `evaluate_measure(...)` (2 sites) and simply omitted the target keyword, letting a back-compat `target_url=None` / `measure_engine_url=None` default resolve to the local container inside the callee. The inventory guard shipped in #402 (ADR-013) counts attribute reads with `ast`, so it could not see them — it reported `validation.py: 9` and a clean bill of health while seven unscoped calls sat next to them. **The guard's scope was therefore narrower than ADR-013 claimed.** The consequence was not "some paths were unscoped": the validation pipeline had *no MCS awareness whatsoever*, and appeared to work only because every path defaulted to the same place. It silently ignored the connection the user selected.

**Why this could not be split by call site — the wipe.** `run_validation` contained a **second, surviving copy of the #392 destructive wipe**: an unfiltered `wipe_patient_data(base_url=settings.MEASURE_ENGINE_URL)` that deletes every patient on the target. #392 (ADR-012) fixed exactly that bug in `run_job` and never touched the validation path. It was harmless only because it pointed at Lenny's own container — so re-pointing it at the user's selected MCS, which is what "scope validation to the active connection" naively means, would have **activated** #392's bug on the validation path and deleted every participant's patient data on a shared connectathon server. That is precisely the trap #397's own text warns about for `wipe_measure_definitions`, arriving on a path the issue **listed but did not classify as destructive**: `validation.py:1239` is among the issue's nine sites and its cluster prose does mention "wipe patient data", but the issue reserved its "Destructive — highest concern" flag for `wipe_measure_definitions` and filed this line under "largest cluster". The site was named; the hazard was not. A slice that scoped the reads and left the wipe for later would have been strictly worse than doing nothing. Correctness of the scoped wipe rests on the same argument as ADR-012 — evaluation is per-subject, so patients a run never evaluates cannot affect its numbers — and inherits ADR-012's caveat that a future population-level `reportType=summary` evaluation (#273) would invalidate it.

**Why a value object and not ambient context.** A `contextvar` holding the current target was considered and rejected: it recreates the exact failure mode this issue is about — a helper deep in the pipeline acquiring its target from invisible state. `McsTarget` is frozen because it passes through sixteen call sites and a helper mutating a shared target would silently re-point every call after it. Every field is required and none has a default; the absence of defaults is the design, since the #397 bug class *is* a target that defaults to something the caller did not mean. It carries resolved auth headers rather than raw credentials, and the two connection flags, so nothing downstream re-derives credentials or re-queries the database.

**Why `validation_runs` gets an MCS snapshot, and what `mcs_auth_type` is for.** A validation run is queued and executed asynchronously. Resolving the target at execution time from "whatever connection is active now" would mean a run that sat in the queue while the user switched connections executes against a server it was never created for — and its stored pass/fail rows would be a claim about correctness with no recorded provenance. `validation_runs` therefore gains `mcs_id`, `mcs_url`, `mcs_name`, `mcs_auth_type`, `mcs_wipe_before_job`, mirroring `Job`'s existing snapshot, backfilled by additive `ADD COLUMN IF NOT EXISTS` migrations in `main.py`. `mcs_auth_type` is not redundant with `mcs_id`: `mcs_id` is `ON DELETE SET NULL`, so once the config is deleted a NULL id cannot distinguish "this run never had MCS auth" from "its credentials are now unrecoverable". Without the snapshotted auth type, a run whose connection was deleted would execute *unauthenticated* against the still-snapshotted URL — the ADR-011 failure mode, re-arriving through a different door. Credentials themselves are still read live and never snapshotted, per ADR-011.

**Why `is_read_only` is read live while everything else is snapshotted.** The snapshot answers "which server did the user mean", a historical fact that must not drift. `is_read_only` answers "may I write to this server *now*", a live permission. Snapshotting it would let a queued run write to a server the user has since marked protected — the flag would be honored only for runs created after it was set, which is indistinguishable from the flag not working. `run_validation` re-reads `MCSConfig.is_read_only` at execution and fails the run with an explanatory message; `triage_test_bundle` refuses up front, before writing any resource, rather than partway through.

**The read-only refusal is a behavior change, deliberately.** Someone who marked their measure engine read-only and still expects validation to run will now see it refuse. Validation must write measures, terminology and patient data to the engine to work at all, so the alternative is a run that half-succeeds against a server the user asked Lenny not to write to. Flagged at design review as the point most worth disagreeing with, and accepted.

---

## ADR-015: Prod's stale HAPI image pins are removed, not repointed; a deploy-time guard replaces silent pull-failure tolerance (2026-08-21)

**Decision:** Remove `HAPI_CDR_IMAGE`/`HAPI_MEASURE_IMAGE` from `/opt/leonard/.env` so production falls through to the compose default, `hapiproject/hapi:v8.8.0-1` (Option A from issue #407), rather than repointing them at a current `lenny-hapi-*` tag (Option B). Add `scripts/check-pinned-images.sh`, run in `deploy-prod.sh` before the tolerant blanket `compose pull`, which resolves each HAPI service's image the way compose itself resolves it and fails the deploy if a *deliberately pinned* image can't be pulled.

**The bug.** PR #261 (2026-05-04) renamed the GHCR packages `mct2-hapi-*` → `lenny-hapi-*`, but could not touch `/opt/leonard/.env` — that file lives only on the prod instance, not in git. The instance kept asking for the old names, which now 403. `deploy-prod.sh` runs `docker compose pull --ignore-pull-failures || true` by design (a registry hiccup shouldn't block a healthy deploy), so the 403 was swallowed on every deploy for over three months with no signal anywhere. Production's HAPI *binary* was frozen on whatever was cached before the rename; its config (`docker-compose.yml`, refreshed from git) and data (named volumes) stayed current — which is exactly why nobody noticed: the two things anyone would think to check were both sourced from outside the image.

**Why this mattered beyond the stale binary.** The image production ran existed in exactly one place — that instance's local Docker cache — selected by a file not in git and not pullable from any registry. An instance rebuild could not have restored it. Disaster recovery had a hole with no signal that it existed.

**Why Option A (remove the pins), not Option B (repoint at a verified tag).** Verified directly on the instance before mutating anything (`docker image inspect`, 2026-08-21): the cached `mct2-hapi-{cdr,measure}:latest` images share all 35 base layers with the `hapiproject/hapi:v8.8.0-1` already cached on the box, and both carry identical OCI labels — `org.opencontainers.image.version: v8.8.0-1`, identical `org.opencontainers.image.revision`. The two extra layers on the mct2 images are the seeded ENV layer (overridden at runtime by `docker-compose.yml`'s `environment:` blocks) and the `docker commit` data layer (shadowed by the `cdrdata`/`measuredata` named volume mounts, since production never applies the prebaked overlay). So removing the pins is a same-binary, same-config, same-data swap — not a version change.

Repointing at `lenny-hapi-*:latest` was rejected: it would silently start feeding the weekly bake into production for the first time, and `:latest` moving underneath production on every bake is the same failure-mode class as this bug, just less severe. A pinned `:${seed-hash}` was also rejected: it reproduces the actual root cause — an untracked value living only in `/opt/leonard/.env` that a human must remember to bump — and buys nothing today, since production doesn't apply the prebaked overlay and so gains nothing from a baked image regardless of which GHCR tag it names.

**The guard.** `scripts/check-pinned-images.sh` resolves each HAPI service's image via `docker compose config --images <service>` — the same resolution path compose itself uses for `.env`, `--project-directory`, and file precedence — rather than re-parsing `.env` by hand; a parsing bug in a hand-rolled resolver must never be able to take down a prod deploy. A service whose resolved image matches the compose default is left alone (the existing tolerant blanket pull still covers it). A service pinned away from the default is treated as deliberate: the guard pulls that exact ref and fails the deploy (exit 1) if the pull fails. An image compose cannot resolve at all — an incompatible compose version, a typo'd service name — is also a hard failure (exit 2), not a silent skip, since silently skipping is the exact shape of the bug this guard exists to catch. The guard logs the resolved ref for both services on every deploy, pinned or not, so a future `.env` drift leaves a trace even on a run where the pull happens to succeed.

**What this does not change.** Moving production onto the prebaked overlay (`docker-compose.prebaked.yml`) remains a separate, open question — see the Production bullet in `docs/architecture.md` § Compose modes. That overlay strips the named-volume mounts that hold production's data, so adopting it requires a data-migration story this decision does not attempt.

**Known limitation — read-only protection vanishes if the connection is deleted after queueing.** The live re-read above is guarded by `if snapshot_mcs_id is not None` (`validation.py:1176`). `mcs_id` is `ON DELETE SET NULL`, so if a user marks a connection read-only *and then deletes it* while a run is queued, `live_read_only` stays `False` and the run proceeds to write to the still-snapshotted URL — unauthenticated, if `mcs_auth_type` was `"none"`, since `resolve_mcs_auth_headers` returns `{}` for that combination by design (`dependencies.py:57-58`). No clean discriminator exists: a NULL `mcs_id` with `mcs_name` set matches both "config deleted after creation" and "no active MCS row existed at creation", so a heuristic here would either miss the case or refuse legitimate runs. This is recorded as a limitation rather than fixed because the fix needs a new column (e.g. a snapshotted `mcs_read_only`, which reintroduces exactly the drift the paragraph above rejects) or a tombstone row. It matters because §5 of the spec argues the protection applies *at the moment of writing*; that argument holds only while the config row still exists.

**Scope note — the MCS snapshot is stored but not surfaced.** `validation_runs` now carries `mcs_id`, `mcs_url`, `mcs_name`, `mcs_auth_type` and `mcs_wipe_before_job`, but no API response returns any of them: neither `GET /validation/runs` (`app/routes/validation.py:228-245`) nor `GET /validation/runs/{id}` (`:304-316`) includes an MCS field, and the UI therefore cannot show one. So the provenance record exists in the database and is unreadable without SQL. The `Job` path is no better off — `JobResponse` / `_job_to_response` (`app/routes/jobs.py:66-88`, `:113-136`) surface `cdr_url` / `cdr_name` but no `mcs_url` either; the only place a job's MCS reaches an operator is a log line's `extra` at creation (`app/routes/jobs.py:307`). Surfacing the snapshot in both APIs and in the UI is follow-up work, not something this slice delivered.

**The guard now checks two things, and the second is why.** Alongside the per-file `settings.MEASURE_ENGINE_URL` read counts, `tests/test_mcs_scoping_inventory.py` gains an independent AST check that flags calls to target-taking helpers which omit their target keyword. It reports `EXPECTED_UNSCOPED_CALLS = {}` — no call in `app/` takes its measure-engine target from a default — verified by a reviewer walking every call site by hand rather than trusting the detector alone. That caveat matters: the detector matches on call-site syntax and is blind to aliased references, `functools.partial`, `getattr`-built calls, and same-named methods on unrelated objects. It also requires the target be passed as an explicit *keyword*, so a call that passes it positionally-but-explicitly is flagged too — fail-closed, accepted deliberately, and noted here because it means a flag from this detector is not by itself proof of a defect while a clean result is still not proof of safety. It is a tripwire for the ordinary case, not a proof. `validation.py`'s read count drops 9 → **1** (`run_validation`'s legacy-NULL fallback for runs created before the snapshot column existed, mirroring the same pattern in `orchestrator.py`, `routes/jobs.py`, `routes/results.py`). `bundle_loader.py` goes 1 → **2**: the boot seed path now builds an explicit `McsTarget` from settings, which is an increase in the count and a decrease in the risk — seeding *should* target Lenny's own engine, and the change makes that intent visible instead of accidental.

**`bundle_loader.py:32` is reclassified as not a defect.** #397 lists `_wait_for_hapi`'s `settings.MEASURE_ENGINE_URL` read among the sites to fix. That is a miscategorisation: it is a boot readiness probe that polls Lenny's own two containers before any MCS connection row is guaranteed to exist. There is no connection to scope it to, and scoping it would break startup. It stays, annotated as legitimate in the guard.

**A correction on CI coverage, since the general rule does not apply here.** The scoped wipe's integration test, `tests/integration/test_validation_mcs_scoping.py`, is **not** on the six-file `--ignore` list in `pr-checks.yml`, so CI's Integration Tests job collects and runs it automatically — reproduced by running CI's exact invocation with all six flags. This contradicts the general claim, repeated in the plan and in `CLAUDE.md`'s pre-push checklist, that CI will silently skip any newly added integration test. That claim holds only for files on the ignore list; it is recorded here because acting on it would mean assuming this test is unguarded when it is not.

**Alternatives considered:** (a) A `contextvar` — rejected, see above. (b) Per-value parameters (`url`, `headers`, `read_only`, …) threaded separately — rejected: sixteen call sites × four values is where a partial thread-through hides. (c) Scoping the reads and deferring the wipe to a later slice — rejected, it would have armed #392's bug on the validation path (see above). (d) Snapshotting `is_read_only` with the rest — rejected, see above. (e) Removing the `target_url=None` / `measure_engine_url=None` back-compat defaults on `push_resources` and `evaluate_measure` so a missed call site raises `TypeError` at call time instead of failing a CI test — deliberately deferred: it ripples into the orchestrator and CDR paths and was out of scope. The consequence is that protection against a *future* missed call site is a CI-time AST check rather than a runtime error.

**Known limitations, stated rather than papered over:** `wait_for_valueset_expansion` receives `mcs.url` but has no auth parameter, so against an authenticated remote MCS its polls will 401 and it will report "not expanded"; the caller treats non-expansion as a warning, so this degrades rather than fails, and threading auth through it is follow-up work. And no independent cross-model adversarial review has been run on this design or on the two already-shipped slices — Codex is not available on the development machine, so every review across #401, #402 and this change was same-model.

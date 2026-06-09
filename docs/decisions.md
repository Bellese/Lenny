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

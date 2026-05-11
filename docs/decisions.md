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

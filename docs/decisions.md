# Architectural Decisions — MCT2

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

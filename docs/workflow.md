# Lenny Development Workflow

This document establishes an orchestration standard for AI-assisted development on Lenny (aka TUFKALenny aka Project Launchpad, aka Leonard, aka Lenny). It defines which AI tool the team uses at each phase of work, how each phase connects back to GitHub Issues, and how two complementary toolkits ([gstack](https://github.com/garrytan/gstack) and [superpowers](https://github.com/obra/superpowers)) fit together into a single pipeline. The goal is a repeatable process that any team member can follow from issue to shipped code.

## How Work Flows

**Reference:** [AI-Enabled Work Management with GitHub](https://docs.google.com/document/d/1qeuNvXWbX4oWYbiNFeV40rvQNi7NNxQS23tDL7sNO9o/edit?tab=t.0)

All work lives in [GitHub Issues](https://github.com/orgs/Bellese/projects/33/views/3) on the project board. Every phase of development starts from and updates back to the issue it came from. Work moves through five statuses: **Backlog**, **Ready**, **In Progress**, **Done**, and **Withdrawn**.

## Development Lifecycle

We use two complementary AI skill suites:

- **gstack** handles the outer loop: talking to users, looking at the product, shipping code, and verifying it works.
- **superpowers** handles the inner loop: disciplined execution with TDD, git worktrees, and subagent-driven implementation.

Each phase uses one recommended tool. The issue gets updated before moving on.

| Phase | Toolkit | Command | What happens | Updated on the issue |
|-------|---------|---------|--------------|----------------------|
| **1. Ideate** | gstack | `/office-hours` | Explore the problem, validate demand, surface approaches | Problem framing, chosen approach, any new sub-issues |
| **2. Plan** | superpowers | `/brainstorming` then `/writing-plans` | Design the solution with a step-by-step plan, tests, and file paths | Link to plan doc |
| **3. Build** | superpowers | `/subagent-driven-development` | Execute the plan in an isolated worktree. Tests first (TDD). | Branch name, commit references |
| **4. Review** | gstack | `/review` | Pre-landing review for scope drift, security, test coverage | Review findings |
| **5. Ship** | gstack | `/ship` | Merge, version bump, create PR, push | PR linked (auto-closes the issue) |
| **6. Verify** | gstack | `/qa` + `/browse` | QA the live result with browser-based checks | QA results; issue moves to Done |

*If someone disagrees with a shipped change, they open a new issue. Software is soft.*

**Shortcuts:** Bug fixes skip Ideate (use `/investigate` for root cause). Small tasks (docs, config) skip Ideate and Plan. Spikes are Ideate only.

## Decision Log

We maintain `docs/decisions.md` to record significant technical and process choices with their rationale. When you make a decision that would be non-obvious to someone joining the project next month, add it to the log.

## Versioning

Four files could carry a version. Three are kept in lockstep; the fourth
deliberately carries none.

| File | Value | Who writes it |
|---|---|---|
| `VERSION` | `MAJOR.MINOR.PATCH.MICRO` (4 components) — the source of truth | `/ship` Step 12 |
| `frontend/package.json` | the **first 3 components** of `VERSION` | `/ship` Step 12 |
| `frontend/package-lock.json` | both version fields track `package.json` | `/ship` Step 12 |
| `backend/app/main.py` | no `version=` argument at all | nobody — see below |

**Why the frontend truncates.** npm rejects a fourth component: `0.0.22.0` is not
valid semver. `/ship`'s writer already knows this and stores the npm-valid
translation in the manifest, so the manifest reads `0.2.0` while `VERSION` reads
`0.2.0.0`. Mirroring `VERSION` verbatim would fight the tooling and reintroduce
an invalid manifest.

**A MICRO-only bump does not move the frontend version.** `0.2.0.0 -> 0.2.0.1`
leaves the manifest at `0.2.0`. This is a known, accepted consequence of the
line above: the CHANGELOG still records MICRO releases, but the on-screen
version does not change for them.

**This is user-visible, not bookkeeping.** `frontend/src/App.js` renders
`Lenny · v{pkg.version}` in the status bar. When the manifest drifts, the
running app misreports its own version — which is what issue #420 actually was.
Between 2026-09-03 and this fix, the app claimed to be v0.0.22.0 while the
shipped release was 0.2.0.0.

**`.gstack/package-json-path` is committed on purpose.** `/ship` resolves the
manifest to bump as `--package-json-path` -> `.gstack/package-json-path` ->
`./package.json`. There is no root `package.json` in this repo — the only one
lives in `frontend/` — so without the pin every `/ship` run silently bumps
`VERSION` alone. That is the whole root cause of #420: the releases bumped by
hand moved both files, the ones bumped by `/ship` did not. `.gitignore`
excludes the rest of `.gstack/` but negates this one file, because a gitignored
pin cannot survive the `git worktree add` that this project's workflow mandates
and each new worktree would start unpinned.

**The backend carries no version deliberately.** It previously passed
`version="0.1.0"` to `FastAPI(...)`, which is FastAPI's own default written out
longhand — so it advertised a maintained version while never being bumped, and
collided confusingly with the real 0.1.0.0 release. Wiring it to `VERSION`
would mean widening the backend image's build context, which is `./backend`
(`docker-compose.yml`) and therefore cannot see the root `VERSION` file. That
touches the prod deploy path, so it was not worth it for an OpenAPI field.
If the backend ever needs to report a real version, widen the context or pass a
build arg — do not re-add a hardcoded string.

**Enforcement.** `scripts/check-version-consistency.sh` asserts all of the
above and runs in the `Config Validation` job of `pr-checks.yml`, which is a
required check. Its own unit tests are `scripts/tests/test_version_consistency.sh`.
To repair drift, use `/ship`'s writer rather than editing by hand:

```bash
bun run ~/.claude/skills/gstack/bin/gstack-version-bump repair
```

## Deploying to prod

### CI/CD deploy (automated — normal path)

Merging to `main` triggers an automatic deploy in roughly 5 minutes via GitHub Actions (`Test and Deploy` workflow).

The sequence:

1. Unit tests and frontend build gate the deploy — a failure here blocks the deploy step entirely.
2. OIDC federated credentials assume the `leonard-github-deploy` IAM role (no long-lived AWS keys stored in GitHub).
3. SSM Run Command invokes the `leonard-deploy` document on the prod EC2 instance.
4. The document runs `git fetch origin && git reset --hard FETCH_HEAD && scripts/deploy-prod.sh` on the instance.
5. The workflow polls SSM for up to 16 minutes, then hits the health endpoint to confirm the deploy succeeded.

No manual steps are needed for routine deploys. If the workflow fails, check the Actions run — SSM command output is also streamed to CloudWatch log group `/leonard/deploy`.

### Manual redeploy (workflow_dispatch)

To redeploy without pushing a new commit (e.g., after changing instance config or environment variables):

Actions tab → **Test and Deploy** → **Run workflow** → select `main` → **Run workflow**.

### SSH / direct (break-glass only)

SSH access and direct `deploy-prod.sh` invocation are reserved for emergencies where the automated path is unavailable.

```bash
cd /opt/leonard
git fetch origin && git reset --hard origin/main
scripts/deploy-prod.sh
```

**Rules:**
- **Always** use `scripts/deploy-prod.sh` — never run `docker compose up` directly in prod
- **Never** run `docker compose restart db` without following it with `scripts/deploy-prod.sh --post-db-restart`
- Authorized maintainers with SSH access: @msutton

### GitHub Actions secrets

| Secret | Status |
|--------|--------|
| `EC2_HOST` | ✅ Deleted (2026-04-23) — no longer used by the deploy workflow |
| `EC2_USER` | ✅ Deleted (2026-04-23) — no longer used by the deploy workflow |
| `EC2_SSH_KEY` | ✅ Deleted (2026-04-23) — GitHub secret removed; SSH key remains on instance and on maintainer laptops for break-glass use |
| `POSTGRES_PASSWORD` | ✅ Deleted (2026-04-23) — now sourced from SSM |
| `AWS_DEPLOY_ROLE_ARN` | **Vestigial** (verified 2026-08-21) — still present in the repo's Actions secrets, but no workflow references it. `deploy.yml` hardcodes the role ARN in the `configure-aws-credentials` step. Safe to delete; kept only to avoid breaking a future workflow that expects it. |

## Reference Docs

| Doc | Contents |
|-----|----------|
| `CLAUDE.md` | Build commands, conventions, workflow shortcuts |
| `docs/architecture.md` | Service map, data flow, HAPI config, environment variables |
| `docs/deploy.md` | Prod CI/CD pipeline end to end, GHCR's role, inventory of everything outside the repo |
| `docs/testing.md` | Testing strategy, CI gate, integration test setup, golden file patterns |
| `docs/decisions.md` | ADR log — significant technical and process choices with rationale |

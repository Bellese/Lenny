# MCT2 Development Workflow

This document establishes an orchestration standard for AI-assisted development on MCT2 (aka TUFKAMCT2 aka Project Launchpad, aka Leonard, aka Lenny). It defines which AI tool the team uses at each phase of work, how each phase connects back to GitHub Issues, and how two complementary toolkits ([gstack](https://github.com/garrytan/gstack) and [superpowers](https://github.com/obra/superpowers)) fit together into a single pipeline. The goal is a repeatable process that any team member can follow from issue to shipped code.

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

## Deploying to prod

### CI/CD deploy (automated — normal path)

Merging to `main` triggers an automatic deploy in roughly 5 minutes via GitHub Actions (`Test and Deploy` workflow).

The sequence:

1. Unit tests and frontend build gate the deploy — a failure here blocks the deploy step entirely.
2. OIDC federated credentials assume the `leonard-github-deploy` IAM role (no long-lived AWS keys stored in GitHub).
3. SSM Run Command invokes the `leonard-deploy` document on instance `i-0f00585639d2f3ef1`.
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

### GitHub Actions secrets — cleanup after Part B stabilizes

Once the SSM-based deploy has run cleanly 3–4 times, prune stale secrets from the repo:

| Secret | Action |
|--------|--------|
| `EC2_HOST` | Delete — no longer used by the deploy workflow |
| `EC2_USER` | Delete — no longer used by the deploy workflow |
| `EC2_SSH_KEY` | Keep for one release cycle, then delete |
| `POSTGRES_PASSWORD` | Already removed from `deploy.yml` |

## Reference Docs

| Doc | Contents |
|-----|----------|
| `CLAUDE.md` | Build commands, conventions, workflow shortcuts |
| `docs/architecture.md` | Service map, data flow, HAPI config, environment variables |
| `docs/testing.md` | Testing strategy, CI gate, integration test setup, golden file patterns |
| `docs/decisions.md` | ADR log — significant technical and process choices with rationale |

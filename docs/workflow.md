# MCT2 Development Workflow

This document defines the orchestration standard for AI-assisted development on MCT2. It covers how work moves through the board, which AI tool the team uses at each phase, and how everything connects back to GitHub Issues.

## How Work Flows

All work lives in [GitHub Issues](https://github.com/orgs/Bellese/projects/33/views/3) on the project board. This includes development tasks, research, persona work, and backlog items. Every phase of development starts from and updates back to the issue it came from.

Work moves through five statuses: **Backlog**, **Ready**, **In Progress**, **Done**, and **Withdrawn**.

### Board Attributes

- **Assignee**: Who is currently working on the item. Assign yourself when you pull it.
- **Labels**: Categorization (bug, documentation, hcd-research, etc.). Optional but helpful.
- **Type**: Bug, Feature, or Task.
- **Milestone**: Groups of issues targeting a date or release.
- **Relationships**: Parent/child, blocked-by/blocking.
- **Priority**: High, Medium, Low. Prefer pulling higher priority work.
- **Estimated Effort**: Hours you'd estimate without AI assistance. Fill when moving to In Progress.
- **Actual Effort**: Hours actually spent (with AI). Fill before moving to Done.

### Guidance

- Work only moves right because someone pulls it. Have time? Grab an issue.
- Use sub-tasks to break large issues into smaller chunks.
- [Link PRs to issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue) so progress tracks automatically.
- Questions that need future user research get the `hcd-research` label.

## Development Lifecycle

We use two complementary AI skill suites. **gstack** handles the outer loop: talking to users, looking at the product, shipping code, and verifying it works. **superpowers** handles the inner loop: disciplined execution with TDD, git worktrees, and subagent-driven implementation.

Each phase uses one recommended tool. The issue gets updated before moving on.

| Phase | Toolkit | Command | What happens | Updated on the issue |
|-------|---------|---------|--------------|----------------------|
| **1. Ideate** | gstack | `/office-hours` | Explore the problem, validate demand, surface approaches | Problem framing, chosen approach, any new sub-issues |
| **2. Plan** | superpowers | `/brainstorming` then `/writing-plans` | Design the solution with a step-by-step plan, tests, and file paths | Link to plan doc |
| **3. Build** | superpowers | `/subagent-driven-development` | Execute the plan in an isolated worktree. Tests first (TDD). | Branch name, commit references |
| **4. Review** | gstack | `/review` | Pre-landing review for scope drift, security, test coverage | Review findings |
| **5. Ship** | gstack | `/ship` | Merge, version bump, create PR, push | PR linked (auto-closes the issue) |
| **6. Verify** | gstack | `/qa` + `/browse` | QA the live result with browser-based checks | QA results; issue moves to Done |

If someone disagrees with a shipped change, they open a new issue. Software is soft.

### Shortcuts

Not every issue needs the full pipeline:

- **Bug fix**: Skip Ideate. Use `/investigate` (gstack) to find the root cause, then Plan or go straight to Build.
- **Small task** (docs, config, one-liner): Skip Ideate and Plan. Build, review, ship.
- **Exploration/spike**: Ideate only. Update the issue with findings, move to Done or create follow-up issues.

## Decision Log

We maintain `docs/decisions.md` to record significant technical and process choices with their rationale. CLAUDE.md instructs Claude to prompt for a decision log entry when non-obvious choices are made during a session.

When you make a decision that would be non-obvious to someone joining the project next month, add it. Format: what we decided, why, and any alternatives considered.

## TODOS.md Migration

`TODOS.md` at the repo root contains five items (Bulk Export Strategy, SMART Auth, MeasureReport Submission, CI/CD Pipeline, Orchestrator Unit Test) that predate this workflow. These will be converted to GitHub Issues and added to the Backlog. Once migrated, `TODOS.md` will be deleted.

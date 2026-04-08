# MCT2 Development Workflow

This document proposes an orchestration standard for AI-assisted development on MCT2. It defines which AI tool the team uses at each phase of work, how each phase connects back to GitHub Issues, and how two complementary toolkits (gstack and superpowers) fit together into a single pipeline. The goal is a repeatable process that any team member can follow from issue to shipped code.

## How Work Flows

All work lives in [GitHub Issues](https://github.com/orgs/Bellese/projects/33/views/3) on the project board. This includes development tasks, research, persona work, and backlog items. Every phase of development starts from and updates back to the issue it came from. Work moves through five statuses: **Backlog**, **Ready**, **In Progress**, **Done**, and **Withdrawn**.

## Development Lifecycle

We use two complementary AI skill suites. **gstack** handles the outer loop: talking to users, looking at the product, shipping code, and verifying it works. **superpowers** handles the inner loop: disciplined execution with TDD, git worktrees, and subagent-driven implementation. We start with both toolkits as-is, learn what works, then adapt them to our needs over time.

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

**Shortcuts:** Bug fixes skip Ideate (use `/investigate` for root cause). Small tasks (docs, config) skip Ideate and Plan. Spikes are Ideate only.

## Decision Log

We maintain `docs/decisions.md` to record significant technical and process choices with their rationale. CLAUDE.md will instruct Claude to prompt for a decision log entry when non-obvious choices are made, so this becomes a natural habit rather than an afterthought.

## What Comes Next

1. **Bill** polishes the draft backlog, has Claude structure it, then pushes to GitHub Issues as the initial board content.
2. We create the following repo files to codify this workflow:
   - `CLAUDE.md` at the repo root: build commands, conventions, domain terms, and behavioral instructions (like decision logging) that Claude reads every session
   - `docs/workflow.md`: the final version of this document
   - `docs/architecture.md`: technical reference for the service map, data flow, and configuration
   - `docs/decisions.md`: the decision log

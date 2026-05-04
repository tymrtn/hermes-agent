---
name: autonomous-daily-sprint-loop
description: Design a full-day autonomous AI dev-team loop (plan → groom → define → 4-6 dev slots → deploy → postmortem) with real-world gaps plugged. Use when a user wants a cron-driven workflow that ships meaningful work daily, not just monitors.
version: 1.0.0
author: Hermes Agent
---

# Autonomous Daily Sprint Loop

Use when the user wants to move beyond a 2-hour "monitoring" cron into a full-day simulated dev-team cadence that plans, builds, reviews, deploys, and retrospects without constant human steering.

Pairs with `autonomy-mode` (which covers the single-loop state-file pattern). This skill is about orchestrating *multiple* time-of-day cron roles that together behave like a real team.

## When to use

- User has an autonomous cron already running but it is not shipping, just observing
- Goal is daily throughput (N stories merged + deployed), not passive surveillance
- User wants multi-agent delegation (Claude Code + Codex + orchestrator) within one day
- Project has a backlog and clear business metric (MRR, signups, inventory, etc.)

## Canonical day cadence (v2)

Numbers are suggestions — adapt to user timezone and budget.

| Time | Role | Purpose |
|---|---|---|
| 06:00 | PM: Plan | Metrics delta, git review, kill-switch check, draft sprint, file-domain ownership, post to task bus |
| 08:00 | PM: Groom | Refine sprint first (cheap), dedupe, punt, apply overnight feedback |
| 09:00 | PM: Define | Write DoD, AC (tests + visual), evidence template for surviving stories only |
| 10:00 | Dev 1 | Story 1 in isolated worktree, one agent implements, another reviews |
| 12:00 | Dev 2 + Triage | Dev slot + 5-min inbox/health ping before kickoff |
| 14:00 | Dev 3 + GTM | Parallel: code story + non-code customer/marketing touch |
| 15:30 | PM: Checkpoint | Mid-day status, reorder afternoon if needed |
| 16:00 | Dev 4 | Story 4 |
| 18:00 | Dev 5 / Flex | Flex slot: deploy queue, yesterday rollback, P1 triage, non-code |
| 20:00 | Integrate + Deploy | Merge in ownership order, test suite, staging → prod per protocol |
| 21:00 | PM: Postmortem | DoD gates check, state-file rewrite, tomorrow hypothesis |

## Sprint-level Definition of Done

A day is only successful if it hits all four:
1. At least one staging deploy
2. At least one customer-facing change live in prod
3. At least one non-code GTM touch sent (or drafted for approval)
4. Yesterday metric delta reported in the morning

Track these explicitly in the postmortem. Story count is a vanity metric; these four are the real gates.

## Slot-level unit of work

Each cron slot should target **one story**.

For this loop, a story means:
- the smallest **user-meaningful** unit
- with clear acceptance criteria
- that is **potentially deployable**

Do not let a slot degrade into a bag of review chores (`review X`, `look at Y`, `plan Z`) unless those truly collapse into one story-sized blocker-removal artifact.

A slot may clear urgent small work first:
- bugs
- ad hoc tasks
- well-understood tech debt
- admin cleanup
- obvious blocker removals

But that does **not** replace the story requirement. The slot should still leave behind one story-sized deliverable.

## Execution model: orchestrator vs builder

The cron agent is the **PM/COO/operator/orchestrator**, not the default code implementer.

Preferred split:
- **Cron agent**: Todoist triage, sprint routing, inbox/admin checks, QA packaging, deploy-readiness packets, GTM drafts, blocker removal, state rewrite, task-bus updates
- **Claude Code**: primary builder for code stories
- **Codex**: adversarial QA / verifier / secondary builder when useful

For code stories, the cron should normally orchestrate Claude Code rather than trying to do the implementation itself.

## Productivity resilience under dirty branches

Real repos are often dirty during active sprint work. Do not let that zero out a slot.

At the start of a dev/integration slot:
1. inspect current branch, dirty files, worktrees, and recent commits
2. identify active coding-agent ownership / collision risk
3. if the current branch is dirty or another agent owns the active path, do one of:
   - create a clean branch/worktree from `main` for a safe non-colliding story-sized unit, or
   - choose an unaffected story surface and produce a story-sized non-code artifact there

Inspection alone does not count as success. The slot should still end with one story advanced by one concrete artifact.

## Acceptable story-sized artifacts

If the slot cannot directly ship code because another agent owns that surface, acceptable outputs include:
- clean branch/worktree setup plus a complete Claude Code implementation handoff
- staging review packet
- QA verification bundle
- acceptance checklist with evidence
- blocker-removal handoff
- deploy-prep package
- GTM/support package tied to one story
- explicit go/no-go gate for one story

The test is simple: did the slot materially advance one potentially deployable unit, or did it just narrate status?

## Critical gaps to plug (common v1 failures)

Naive daily loops miss these. Explicitly design each in:

**1. Yesterday-feedback loop** — 06:00 must pull actual metrics (analytics, DB counts, error rates, inbox volume). Otherwise planning is blind.

**2. Deployment slot** — Code merged to a branch is not shipped. Assign a dedicated deploy window. Specify: auto-promote or gated on human?

**3. Merge-collision strategy** — N concurrent worktrees WILL conflict. Either serialize, or assign file-domain ownership at grooming so Story 1 owns one directory, Story 2 another.

**4. GTM / customer work** — Almost always absent from engineering-heavy loops. Force at least one non-code story per day (outreach, editorial, review ask, seller contact).

**5. Mid-day triage** — Without a 12:00 or 15:30 checkpoint, broken branches only surface at postmortem (6+ hours of waste).

**6. Adversarial review** — Do not let the same agent write and QA. Rotate roles daily. Implementer and reviewer alternate.

**7. Groom before define** — Defining subtasks before grooming wastes agent time on stories that get punted. Flip the order.

**8. Cut criteria** — Explicit thresholds: "60 min no green test → pivot to flex. 90 min hard stop." Otherwise one bad story eats three slots.

**9. Inbox continuity** — Design ignores inboxes between 09:00 and 21:00. Real customer mail stales. Include inbox in triage pings.

**10. Cost / rate-limit reality** — Agent loops across 6 slots is real spend. Set a per-day token budget and alert on burn.

**11. Kill switch** — 06:00 plan needs to detect "yesterday broke prod" and redirect the entire day to triage, not proceed as planned.

**12. Infra job collisions** — Daily scrape, embeddings, enrichment run independently. Plan must be aware and avoid migration conflicts.

**13. Staging-first protocol** — If project has one, write it into the 20:00 step explicitly. Easy to forget under sprint pressure.

**14. Human SPOF / Founder Oracle** — If the user is unreachable, the day must still proceed. Use a Founder Oracle pattern: infer the user's likely preference from project strategy, repo guidance, Todoist/state, and prior decisions; only escalate when the Founder Oracle is genuinely in conflict, cannot safely decide from context, and the decision blocks the day. High-risk prod/destructive actions still require explicit approval.

**14.5. Urgent escalation tiers** — Add a live text escalation path for true urgency, not routine QA noise. For Klasificados, urgent Telegram escalation is appropriate for P0/P1 incidents, prod outage, data-loss/deploy risk, payment/checkout breakage, legal/compliance risk, unresolved Founder Oracle conflict blocking the day, or visual/taste/end-user approval that agents cannot safely judge and that blocks execution. Do not page for routine blocked stories, ordinary QA failures, or items grooming can deprioritize. Visual/taste escalations must include screenshots or accessible visual evidence.

**15. Sprint-level DoD** — "6 stories done" is not the same as "day moved the needle." Define daily gates (see above).

## Required prerequisites

Before building this loop, confirm:
- An executive-team persona set exists (CEO/COO/Head of Growth/Head of Product etc.) — the PM role pulls from these for decision framing. If none, create first.
- A metrics source is wired (admin dashboard, DB query helper, analytics API).
- A canonical task bus exists (Todoist project, Linear team, GitHub project). Planning posts and evidence attaches there.
- Deploy automation works from CLI (no manual console clicks).
- Worktree-based dev delegation is already proven.

## Implementation pattern

1. Confirm the prerequisites above exist.
2. Get user answers on: GTM backlog source, deploy authority (auto or gated), per-day cost cap, metric dashboard source.
3. Write or update a shared state file (`ops/core-loop-state.md`) with new sections: yesterday-metrics, sprint-DoD gates, file-domain ownership, cut-criteria log.
4. Create 8-9 cron jobs (one per time slot), each a distinct role-prompt but all reading the same state file. In prose, tilde-shorthand paths are readable; in any tool `workdir`, Python `cwd`, subprocess cwd, or cron script path, use an expanded path such as `Path.home() / "Dropbox/code/klasificados"` or `/Users/wondermonkey/Dropbox/code/klasificados`. Do not pass literal `~` as cwd.
5. Each cron appends a short run log to the state file and updates its section; full rewrite happens at 21:00 postmortem only.
6. First week: human reviews every morning and evening output. Tune thresholds.
7. After week 1, let it run with 2h feedback windows.

## Cron prompt structure (per slot)

Each slot prompt should include:
- Role name and responsibility (e.g., "You are PM at 08:00 Grooming")
- Tilde paths to project doc files, protocol, and state file (never hardcoded usernames)
- The 4 sprint DoD gates (every prompt sees them)
- What to read, what to do, what section of state to update
- Explicit non-negotiable rules (no email without approval, staging-first, etc.)
- Delivery target (chat channel, with or without threading)

## Code-affecting slot Definition of Done

For Klasificados/dev slots, the minimum DoD for code-affecting work is an opened GitHub PR with tests/evidence in the PR body. A pushed branch, local commit, tests passed, QA packet, or `ready for review/staging` status is not enough. If a PR cannot be opened, the slot must name the concrete external blocker and include proof.

Acceptable endings for code-affecting slots:
- PR opened, with direct PR URL and tests/evidence in the body
- merged/deployed when authorized and verified
- blocked by a named external dependency with proof
- reverted/abandoned because QA proved unsafe

## Reporting rule for Tyler-facing closeouts

Never report an approval blocker as a naked story/task number. If a cron closeout says Tyler approval, review, deploy approval, or a route/data decision is needed, include the direct Todoist/GitHub/staging/review URL needed for that decision and a short note saying where the test instructions live. If no such link exists, say `No review link exists yet` and create/repair the packet before asking Tyler.

Good:
```text
168: needs deploy approval. Compare: https://github.com/tymrtn/klasificados/compare/main...nagaklas/story-168-contact-volume-dashboard. Test instructions: Todoist task description/comments.
```

Bad:
```text
168 still needs Tyler deploy approval.
```

## Pitfalls

- **Over-scheduling.** 9 cron slots sound thorough but each costs tokens. Collapse adjacent slots if they do not need a fresh session.
- **No escape hatch.** Always include "if P0 incident detected, abandon plan and triage."
- **Evidence theater.** Screenshots and links that do not actually prove AC. Require the evidence template to include a test-id or URL that can be independently verified.
- **Human-in-the-loop fatigue.** If every slot pings the user, they will mute. Only notify on material change: plan posted, checkpoint flagged, deploy done, postmortem ready.
- **State file bloat.** Each slot adds, none prunes. 21:00 postmortem must aggressively collapse/delete stale sections.
- **Recursive cron creation.** Per Hermes safety rule, cron sessions should not schedule more cron jobs. Orchestration changes happen in chat, not inside a run.

## Verification

After setup:
- Run one full day manually (trigger each cron via `cronjob run` at the right time).
- Check that state file rewrote cleanly at 21:00 with all four gates answered.
- Confirm task bus has the sprint task with subtask evidence attached.
- Confirm at least one deploy reached staging.
- Measure actual token spend vs budget; adjust.

## Relationship to other skills

- `autonomy-mode` — foundation (single loop, state file). Use first.
- `autonomous-ai-agents/claude-code` — for dev slot delegation.
- `autonomous-ai-agents/codex` — for adversarial review.
- `software-development/development-delegation-protocol` — the inner protocol each dev slot runs.
- `productivity/todoist-task-worker-crons` — if using Todoist as the task bus.

## Good outcome

A good daily sprint loop produces, every day without human steering:
- A morning plan post with yesterday metric delta
- 4+ merged stories with evidence attached
- 1 staging deploy, 1 prod change, 1 GTM touch
- A postmortem that identifies one process improvement
- A state file the next morning PM can read cold and run from

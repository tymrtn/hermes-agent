---
name: todoist-project-backlog-audit
description: Audit whether a project decision is reflected in Todoist SSOT, then update existing tasks or add a new one with explicit constraints instead of relying on fuzzy search.
version: 1.0.0
author: Nagovernor
license: MIT
metadata:
  hermes:
    tags: [todoist, backlog, ssot, planning, project-management]
---

# Todoist Project Backlog Audit

Use this when Tyler asks things like:
- "Is this reflected in our SSOT Todoist backlog?"
- "Does the Governor project backlog capture this constraint?"
- "Update the backlog to match this decision."

## Core principle

Treat Todoist as the canonical task bus, but verify by **project-scoped inspection**, not clever search syntax.

In practice, the reliable sequence is:
1. confirm the project exists with `todo projects`
2. list tasks for that project with `todo list --project <ProjectName> --limit 50`
3. inspect the actual task wording
4. update the closest existing task if the decision changes scope or constraints
5. add a new task only if the constraint is genuinely missing

## Why this exists

Broad Todoist filter/search queries can be brittle and may fail with 400 errors when you try combinations like project names plus `search:` clauses. For SSOT checks, project-scoped reads are more reliable and usually what you actually want.

## Recommended workflow

### 1. Confirm the project
```bash
todo projects
```
Find the exact project name first.

### 2. Read the project backlog directly
```bash
todo list --project Governor --limit 50
```
Do not start with complex search filters unless there is no obvious project.

### 3. Decide whether to update or add
Use this rule:
- **Update existing task** when the new information sharpens an existing deliverable.
- **Add a task** when the new information introduces a new control, dependency, or acceptance criterion.

For example, if the backlog says:
- `ship Governor dogfood in Hermes/OpenClaw with telemetry...`

and Tyler adds:
- must not interfere with current work
- shadow mode first
- zero behavior change until validated
- feature flag and kill switch

then:
- update the existing dogfood task to encode `shadow mode first` and `zero behavior change until validated`
- add a separate task for `feature flag and kill switch after validation` if that operational guardrail is not already represented

### 4. Keep wording operational
Prefer explicit task text like:
- `shadow mode first`
- `zero behavior change until validated`
- `feature flag and kill switch`
- `targeted tests before cutover`

Avoid vague backlog language like:
- `be careful`
- `safe rollout`
- `handle testing`

## Good pattern

```bash
todo update <TASK_ID> --content "Governor phase 1: ship Governor dogfood in Hermes/OpenClaw in shadow mode first - telemetry, precedent capture, review-band logging, and zero behavior change until validated"

todo add "Governor phase 1: gate real Hermes cutover behind a feature flag and kill switch after shadow-mode validation + targeted tests" --project Governor --priority 4 --labels nagatha
```

## Reporting back
When done, answer in three parts:
1. whether the backlog already reflected the decision fully, partly, or not at all
2. exactly which task was updated
3. exactly which task was added, if any

Include Todoist links when possible:
- `https://app.todoist.com/app/task/<TASK_ID>`

## Pitfalls
- Do not assume fuzzy search reflects the canonical backlog.
- Do not add duplicate tasks before checking whether an existing task can be tightened.
- Do not leave rollout constraints implicit when they materially affect execution.
- Do not bulk-rewrite project phrasing when only one or two tasks need sharpening.

## Success looks like
- The project backlog explicitly matches the latest decision.
- Existing roadmap tasks remain clean and non-duplicative.
- Critical rollout constraints are visible in SSOT, not trapped in chat history.

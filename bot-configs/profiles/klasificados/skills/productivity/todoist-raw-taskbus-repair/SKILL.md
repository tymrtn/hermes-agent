---
name: todoist-raw-taskbus-repair
description: Use Todoist raw REST calls to inspect, repair, and route task-bus items when todo add/update cannot set comments, sections, labels, or complete lease/status metadata.
tags: [todoist, task-bus, ops, comments, sections]
triggers: ["Todoist comments", "Todoist sections", "task bus repair", "same-day cron tasks", "complete task object", "raw Todoist"]
---

# Todoist Raw Task-Bus Repair

Use this alongside the main `todoist` skill when an autonomous ops loop must inspect complete Todoist objects, fetch comments, repair labels/sections, or create/update same-day slot tasks.

## When to use

- Before choosing or acting on a Todoist task whose comments, labels, section, or due date can affect routing.
- When `todo add` / `todo update` is insufficient because you need `section_id`, `labels`, comments, or complete task-object fields.
- When a cron slot must create or update a same-day operating plan without duplicating existing tasks.
- When a task is malformed: no labels, wrong section, no owner/status/blocker, stale due date, or missing acceptance-definition context.

## Required inspection sequence

1. List project tasks for duplicate/routing context:
   ```bash
   todo list --project Klasificados --limit 200
   ```
2. Inspect each candidate task completely:
   ```bash
   todo raw GET /tasks/TASK_ID
   ```
3. Fetch comments before acting, because Tyler comments are authoritative input:
   ```bash
   todo raw GET /comments?task_id=TASK_ID
   ```
4. Fetch sections when section repair or exact placement matters:
   ```bash
   todo raw GET /sections
   ```

## Raw REST patterns

Create a task in a specific project section:

```bash
todo raw POST /tasks --body '{"content":"Klasificados 11:00 dev: story 330 staging review surface","project_id":"PROJECT_ID","section_id":"SECTION_ID","labels":["bot-only","nagatha"],"due_string":"today 11am","priority":4,"description":"🇵🇷 Nagaklas: 2026-04-26 plan task. Status: READY_FOR_SPEC. Owner: 11:00 dev slot. Next gate: ..."}'
```

Update an existing task's section, labels, due date, priority, or description:

```bash
todo raw POST /tasks/TASK_ID --body '{"section_id":"SECTION_ID","labels":["nagatha"],"due_string":"today 8am","priority":4,"description":"🇵🇷 Nagaklas: Status: READY_FOR_SPEC. Owner: 08:00 grooming. Blocker: SPEC_GAP. Next gate: ..."}'
```

Add an attributed bot comment:

```bash
todo raw POST /comments --body '{"task_id":"TASK_ID","content":"🇵🇷 Nagaklas: Status: READY_FOR_DEV. Blocker: MERGE_COLLISION. Owner: 12:00 dev slot. Next action: create clean worktree from main, rerun targeted tests, and publish QA packet. Retry/deadline: today 12:00."}'
```

## Same-day cron-plan pattern

For Klasificados-style daily cadence tasks:

1. Search/list existing project tasks first. Dedupe by exact content, story id, branch name, or backlog filename.
2. Use project sections:
   - `Stories` for user-meaningful product/story work.
   - `Bugs` for regressions, incidents, or user-reported broken behavior.
   - `Tasks` for cron slots, checkpoints, deploy prep, postmortems, and bot-only operations.
3. Put cron-slot/checkpoint/deploy-prep/postmortem chores in `Tasks` with labels `bot-only` and the bot owner label, e.g. `nagatha`.
4. Keep titles short and human-readable. Put status, owner, branch, evidence, blockers, and caveats in descriptions or comments.
5. Use natural-language due strings such as `today 11am`; do not manually convert unless required by the API.
6. After creating/updating tasks, verify with:
   ```bash
   todo overview
   todo list --project Klasificados --limit 120
   ```

## Comment content schema

Bot-authored comments should start with the bot attribution required by the main Todoist skill, then include machine-routable fields:

```text
🇵🇷 Nagaklas: 2026-04-26 06:00 plan. Status: READY_FOR_DEV. Owner: 12:00 dev/integration slot. Branch/worktree: root branch is dirty; create clean worktree from main. Next gate: rerun targeted tests and publish QA packet. Blocker: MERGE_COLLISION. Reason: root checkout contains unrelated changes and is not a clean review surface. Retry/deadline: today 12:00.
```

Use canonical status and blocker vocabularies from the project protocol when present. Do not invent near-synonyms.

## Pitfalls

- `todo update` does not expose every field needed for routing. Use `todo raw POST /tasks/TASK_ID` for section and label repair, but verify the returned task and a fresh `todo raw GET /tasks/TASK_ID` before reporting the section as repaired.
- `todo raw POST /tasks/TASK_ID --body '{"section_id":"SECTION_ID"}'` may fail with `HTTP 400 ... At least one of supported fields should be set and non-empty`, and section-only repairs may not move existing tasks in this Todoist v1 environment. If exact section placement matters, pair the attempted section repair with a supported field when safe, then verify; if it still does not move, leave the task status/comment accurate rather than claiming section repair.
- Even when a raw task update succeeds and changes due date, priority, or description, Todoist may silently ignore the supplied `section_id` for existing tasks. Always verify `section_id` in a fresh `todo raw GET /tasks/TASK_ID` after the update. If the section remains wrong, report the routing/status repair as complete but do not claim the section move. Only create a replacement task when exact section placement is operationally critical and duplication has been checked.
- To close a completed bot-only task, use `todo done TASK_ID`. `todo close` and `todo complete` are not valid commands in this CLI. Verify with `todo raw GET /tasks/TASK_ID` and check `checked: true` / `completed_at` before reporting closure.
- Todoist API priority is inverted: `priority: 4` means UI P1 / urgent.
- `todo.py` may unwrap only the first page of paginated REST results. For large task/comment lists, inspect `next_cursor` and paginate manually if needed. The list limit maxes at 200 in this environment; `todo list --project Klasificados --limit 300` returns HTTP 400, so use `--limit 200` plus pagination/targeted searches.
- If terminal quoting or Hermes command classification gets in the way during a same-day cron plan, use the Todoist API v1 fallback from `execute_code` to create/update the slot tasks directly. Create with `POST /api/v1/tasks` including `project_id`, `section_id`, `labels`, `due_string`, `priority`, and `description`; update existing tasks with `POST /api/v1/tasks/<TASK_ID>`; add routing comments with `POST /api/v1/comments`; then verify one fresh `todo raw GET /tasks/<TASK_ID>` before reporting success.
- Do not create duplicate same-day slot tasks on rerun. List existing tasks and update/comment when possible.
- Do not leave anonymous bot text. Prefix descriptions and comments with the bot emoji/name, e.g. `🇵🇷 Nagaklas:`.

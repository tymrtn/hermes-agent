---
name: todoist-raw-task-repairs
description: Repair Todoist task fields and sections via todo raw when todo update is insufficient, especially section moves, comments, and Hermes-safe JSON verification.
version: 1.0.0
author: Nagaklas
---

# Todoist Raw Task Repairs

Use this with the `todoist` skill whenever a bot needs to repair Todoist task shape, sections, comments, labels, or descriptions using `todo raw`.

## When to use

- Moving existing tasks into `Stories`, `Bugs`, or `Tasks` sections.
- Repairing bot-authored descriptions/comments during autonomous cron grooming.
- Adding protocol status, blocker, owner, and next-gate comments.
- Updating fields not covered by `todo update`.
- Verifying Todoist JSON from a Hermes cron without unsafe shell pipelines.

## Section move gotcha

Do **not** move a task to a section with a normal task update.

This may return success-looking JSON but leave `section_id` unchanged:

```bash
todo raw POST /tasks/TASK_ID --body '{"section_id":"SECTION_ID"}'
```

Use the move endpoint instead:

```bash
todo raw POST /tasks/TASK_ID/move --body '{"section_id":"SECTION_ID"}'
```

For Klasificados, first discover sections if needed:

```bash
todo raw GET /sections?project_id=PROJECT_ID
```

Common Klasificados section names:
- `Stories` for user-meaningful product/story work
- `Bugs` for regressions/incidents/user-reported broken behavior
- `Tasks` for operational chores/checkpoints/subtasks

## Ordinary task field updates

Use normal task update endpoint for description, labels, and priority:

```bash
todo raw POST /tasks/TASK_ID --body '{"description":"...","labels":["nagatha"],"priority":4}'
```

Priority uses Todoist API integers:
- `4` = UI P1 / urgent
- `3` = UI P2 / high
- `2` = UI P3 / medium
- `1` = UI P4 / normal

## Comments

Add bot comments with required attribution at the start:

```bash
todo raw POST /comments --body '{"task_id":"TASK_ID","content":"🇵🇷 Nagaklas: ..."}'
```

For Klasificados grooming comments, include:
- canonical status
- blocker type
- plain-English blocker reason
- owner
- next action
- retry/deadline

## Hermes-safe JSON verification

Avoid shell pipelines like:

```bash
todo raw GET /tasks/TASK_ID | python3 -c '...'
```

Hermes security may block pipe-to-interpreter patterns. Instead use `execute_code`:

```python
from hermes_tools import terminal
import json
out = terminal('todo raw GET /tasks/TASK_ID', workdir=root)['output']
task = json.loads(out)
print(task['section_id'])
```

## Verification checklist

After repairs:
1. Re-fetch the task with `todo raw GET /tasks/TASK_ID`.
2. Confirm `section_id`, `description`, labels, due date, and priority match the intended routing.
3. For comments, fetch `todo raw GET /comments?task_id=TASK_ID` and confirm the latest bot comment is present.
4. If the task is a cron/dev-slot routing task, verify the corresponding shared state file or handoff also matches the Todoist status.

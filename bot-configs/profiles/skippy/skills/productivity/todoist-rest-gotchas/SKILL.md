---
name: todoist-rest-gotchas
description: Safe Todoist REST patterns for bot triage - fetch full task state, comment on tasks, and avoid shell quoting footguns when using todo.py from terminal.
tags: [todoist, productivity, scripting, todo.py, heartbeat]
triggers: ["todo raw", "todoist heartbeat", "todoist comments", "todo update content", "todo.py shell quoting"]
---

# Todoist REST gotchas for bot triage

Use this when `todo.py` is the scripting surface and you need more than the basic list/add/done flow.

## Why this exists

In live bot triage, the high-level Todoist commands are not always enough:
- filtered lists hide completed items you still need to inspect
- you often need to leave a machine-readable audit comment on a task
- naive shell quoting can mangle task content when it contains backticks

## Reliable patterns

### 0. Default sink for Tyler's real-world actionable tasks

When Tyler gives a concrete real-world task or blocker (for example, "sell Tesla: handle marbete" or "contact the bank about title"), do not leave it only in the ephemeral Hermes session `todo` list. Create or update a Todoist task when appropriate, then explicitly tell Tyler where it was saved.

Use the session `todo` tool for active scratch-state, but Todoist is the durable sink for personal/real-world tasks unless Tyler specifies another tracker such as Warboard, Linear, GitHub, or Reminders.

Recommended flow:
1. If the project is obvious, add the task there; otherwise use Inbox or ask only if routing materially matters.
2. For home/personal logistics, prefer the `Home 🏡` project when available.
3. Put operational context in the Todoist description/comment, not just the title.
4. If Todoist creation fails transiently (HTTP 5xx), retry once before reporting failure.

Example:

```bash
HOME=/Users/wondermonkey todo add 'Sell Tesla: handle/renew/verify marbete before sale' \
  --project 'Home 🏡' \
  --description 'Skippy captured from Telegram: marbete/tag/registration status must be handled before sale so transfer/dealer/buyer paperwork does not get blocked.'
```

### 1. Use the real user HOME when the Hermes profile HOME is sandboxed

On Skippy/Hermes, `Path.home()` may resolve to the profile sandbox, e.g. `/Users/wondermonkey/.hermes/profiles/skippy/home`, while the live Todoist config/token lives under the real macOS user HOME.

Symptom:

```bash
todo projects
# No Todoist token. Set TODOIST_API_KEY in ~/.hermes/.env
```

Fix for live Todoist operations:

```bash
HOME=/Users/wondermonkey todo projects
HOME=/Users/wondermonkey todo add-batch /tmp/tasks.jsonl
HOME=/Users/wondermonkey todo raw POST /comments --body "$body"
```

Do not burn time hunting for a new token until checking whether the command works with `HOME=/Users/wondermonkey`. If a governor/security gate trips from clever shell piping, stop being clever and use plain CLI output or file-based JSONL.

### 1. Fetch one task directly
Use the raw task endpoint when you need the full object, including whether the task is already closed.

```bash
todo raw GET /tasks/TASK_ID
```

Useful fields:
- `checked` - true means completed
- `completed_at`
- `description`
- `labels`
- `due`

This is the fastest way to verify whether a task still needs action after it drops out of `todo list --filter "today | overdue"`.

### 2. Add a comment to a task
Todoist comments are available through the raw REST escape hatch.

```bash
body='{"task_id":"1234567890","content":"Skippy 2026-04-23 Europe/Madrid: verified X, next gate Y."}'
todo raw POST /comments --body "$body"
```

Comment rules:
- Identify the bot in the comment body itself
- Keep it concise and operational
- Use comments to record what changed, what was verified, and what the next gate is

### 3. Close a task only after verification
Typical pattern:
1. `todo raw GET /tasks/TASK_ID`
2. verify the external reality
3. `todo raw POST /comments ...`
4. `todo done TASK_ID`

Do not close based on stale list output alone.

## Shell quoting footgun

If you run `todo update TASK_ID --content "...`backticks`..."` through a shell, the backticks may be executed as command substitution before `todo.py` ever sees the string.

That can:
- strip the intended text from the task content
- execute garbage commands
- leave a half-corrupted task title

### Safe options

#### Best: avoid backticks in Todoist task titles/comments
Use plain text like:
- `credits_page_bonus_badges`
- `/credits`
- `uv sync --extra dev`

#### If you must include special characters, single-quote the whole argument

```bash
todo update TASK_ID --content 'Focused fix for credits_page_bonus_badges on /credits after uv sync prewarm'
```

#### For comments, build JSON in a variable first

```bash
body='{"task_id":"1234567890","content":"Skippy 2026-04-23 Europe/Madrid: remaining blocker is credits_page_bonus_badges on /credits."}'
todo raw POST /comments --body "$body"
```

## Good bot-triage workflow

For an hourly action loop:
1. Pull `todo overview`
2. Pull `todo list --filter "today | overdue"`
3. For any ambiguous task, fetch it directly with `todo raw GET /tasks/TASK_ID`
4. If the task is development/code/QA/deploy work, open the linked GitHub Issue/PR and update engineering evidence there first
5. Act in the external system first
6. Write a concise Todoist comment only for Tyler-facing state: decision needed, link to GitHub issue, due/follow-up, or why the human action is blocked
7. Close only if the human-facing task is actually done
8. If the blocker changed shape, update the task title to the new concrete human action; do not turn Todoist into the code ledger

## Example

```bash
# inspect
 todo raw GET /tasks/6gRMR53GcXW99Pf4

# comment
body='{"task_id":"6gRMR53GcXW99Pf4","content":"Skippy 2026-04-23 Europe/Madrid: uv sync prewarmed QA, unit probe passes, remaining blocker is credits_page_bonus_badges on /credits."}'
todo raw POST /comments --body "$body"

# retitle safely
 todo update 6gRMR53GcXW99Pf4 --content 'Klasificados trust-drift lane unblocked from env setup - remaining blocker is credits_page_bonus_badges on /credits'
```

That is the non-monkey way to keep Todoist aligned with reality.

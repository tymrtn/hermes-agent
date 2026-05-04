---
name: todoist-tasks
description: Create or verify Todoist tasks from Hermes when Tyler asks, with safe fallback when Todoist is not authenticated in the current profile.
version: 1.0.0
author: Envelopie
license: MIT
metadata:
  hermes:
    tags: [Todoist, tasks, reminders, productivity]
---

# Todoist Tasks

Use when Tyler asks to add, check, or remind about a Todoist task.

## Principle

Do not claim a Todoist task was created unless Todoist access is actually available and the operation succeeds. Hermes' local `todo` tool is not Todoist; use it only as an explicit fallback.

## Discovery

First check for installed/configured Todoist access without exposing tokens:

```bash
for c in todoist todoist-cli todoistctl; do
  command -v "$c" && "$c" --version 2>/dev/null | head -1
done
printf 'env_token_present='; env | grep -E '^TODOIST' | sed -E 's/=.*/=[REDACTED]/' || true
printf 'config_files=\n'
for f in "$HOME/.todoist" "$HOME/.config/todoist/config.json" "$HOME/.config/todoist-cli/config.json" "$HOME/.todoist-cli.json"; do
  [ -e "$f" ] && printf '%s\n' "$f"
done
```

If no CLI/config/token exists, optionally check whether the browser session is logged in:

```text
Navigate to https://todoist.com/app and inspect whether it lands in the app or the login page.
```

## Creating a task

If a Todoist CLI or API token is available, use that path. Avoid printing token values. Include enough task context to be useful, but do not include secrets or personal data unnecessarily.

If Todoist access is unavailable:

1. Say clearly that Todoist is not authenticated/configured here.
2. Create a Hermes local `todo` fallback item with the same task text.
3. If Tyler asked for a reminder, schedule a `cronjob` to retry/check later. The cron prompt should be self-contained and should again avoid exposing secrets.

Example fallback reminder:

```text
Reminder: check the Todoist task Tyler requested: “<task text>”. First verify whether Todoist access is available (CLI, browser session, or configured API token without exposing secrets). If access is available, confirm the task exists or create it if missing. If access is not available, report that Todoist is not authenticated/configured here and keep the local Hermes todo as fallback.
```

## Reporting

Good final wording:

- “I tried Todoist directly: no CLI/config/token/browser session available.”
- “I created a local Hermes fallback todo.”
- “I scheduled a reminder to retry/check Todoist in <time>.”

Bad final wording:

- “Created the Todoist task” when only Hermes `todo` was used.
- “Todoist is broken” when it is merely unauthenticated in this profile.

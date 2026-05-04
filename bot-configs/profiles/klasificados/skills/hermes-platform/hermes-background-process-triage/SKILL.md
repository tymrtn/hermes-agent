---
name: hermes-background-process-triage
description: Triage Hermes background process notifications, especially delayed watch_pattern matches, exited processes, server startup notifications, and repeated ERROR alerts.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [hermes, background-processes, process, debugging, notifications]
    related_skills: [systematic-debugging]
---

# Hermes Background Process Triage

Use this when the user receives or forwards a Hermes background process notification, especially messages like:
- `Background process <id> matched watch pattern "ERROR"`
- `matched watch pattern "Application startup complete"`
- repeated alerts from the same `session_id`
- a server command that may already have exited

## Required first step

Always check the process state before interpreting the notification:

```python
process(action="poll", session_id="proc_xxx")
```

Do not assume the notification means the process is currently running. Watch-pattern notifications can arrive after the process has exited, and multiple notifications from the same process can be delayed duplicates. If repeated stale notifications continue after `poll` returns `not_found`, use `process(action="list")` once to verify there are no active background processes, then explain that already-matched notifications may still be queued for delivery.

## Triage steps

1. Poll the process.
   - Record `status`, `exit_code`, `pid`, and `output_preview`.
   - If `status` is `exited`, treat new notifications for that same `session_id` as historical unless there is a different process ID or command.
   - If `status` is `not_found`, state that the process is no longer tracked/running and treat further same-session notifications as stale watch output.

2. If more context is needed, read the process log.

```python
process(action="log", session_id="proc_xxx", offset=0, limit=200)
```

Do not fetch the full log repeatedly for every repeated notification from the same process once the root error class is known. Repeated watch notifications should get a short status response, not a new investigation, unless the `session_id`, command, or error class changes. If the same stale notification repeats many times in the same chat, do not keep producing full replies for each one; acknowledge once, then use the shortest possible response unless the user asks what it means.

3. Classify the notification.
   - Startup marker: confirms the server reached startup at that time; it does not prove the server is still running.
   - Repeated `ERROR` marker from the same exited or `not_found` process: delayed duplicate notification unless the `session_id` or command changed.
   - Notification timestamps inside matched output can be later than earlier alerts and still be historical buffered output. Use `session_id` plus `process.poll` status as the authority, not the timestamp in the matched log snippet.
   - If a process has already been confirmed `not_found` in the current conversation and the user/system sends another notification with the same `session_id` and command, keep the response short: state that it is the same stale watcher and no new action is needed. Poll again only if the process ID, command, or error class changed, or if the user asks for renewed verification.
   - `InFailedSqlTransaction`: usually a follow-on error after an earlier DB exception in the same transaction; find the first exception.
   - External provider `429`: rate-limit/provider issue; separate it from route-specific failures.
   - Exit code `-15` or `143`: process was terminated; distinguish this from an application crash.
   - If the user asks why stale notifications continue, run `process(action="list")` once. If it returns no active processes, explain that Hermes matched the log lines while the process was alive and the notification delivery queue can continue sending those already-matched events after the process is gone.
   - `InFailedSqlTransaction`: usually a follow-on error after an earlier DB exception in the same transaction; find the first exception.
   - Say whether the process is active or exited.
   - Say whether the notification is new actionable information or historical noise.
   - Name the root error class if known.
   - Avoid treating delayed watch notifications as new incidents.

## Safety rule

If the process command loaded a project `.env` and may point at production services or databases, do not perform write verification through that process until the target database/environment is explicitly confirmed non-production.

## Example response shape

```text
This is the same exited process, not a new active server.
- session: proc_xxx
- status: exited
- exit code: 143 / -15
- notification type: delayed watch-pattern match
- root error: OpenRouter 429 / DB schema mismatch / transaction already aborted
Action: no restart or write test from this process. Use a confirmed staging/local DB for verification.
```

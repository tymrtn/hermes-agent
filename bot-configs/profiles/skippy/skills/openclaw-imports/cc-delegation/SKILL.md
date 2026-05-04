---
name: cc-delegation
description: Delegate coding tasks to Claude Code using the two-phase protocol. Phase 1 plans, Phase 2 executes. Use plain `claude` CLI from the target repo root so local CLAUDE.md is in scope.
---

# Claude Code Delegation Protocol

Skippy delegates coding work to Claude Code. Never code directly — even 3 lines of CSS goes through CC.

## Critical: Launch from CLAUDE.md Directory

**Always `cd` to the directory containing the project's CLAUDE.md before running claude.**

CC reads CLAUDE.md for project-specific rules, conventions, and context. If you launch from the wrong directory, CC won't see those rules.

```bash
# Find the CLAUDE.md first
find ~/Dropbox/Code/myproject -name "CLAUDE.md" -type f | head -1

# Then cd to that directory and run
cd ~/Dropbox/Code/myproject && claude --permission-mode plan --print "task"
```

## Two-Phase Protocol

### Phase 1: Plan (read-only)
```bash
cd /path/to/repo && claude --permission-mode plan --print "Your task description"
```
- CC reads files and outputs a plan
- No files written
- Review the plan before proceeding

### Phase 2: Execute (full auto)
```bash
cd /path/to/repo && claude --permission-mode bypassPermissions --print "Your task description"
```
- CC reads and writes files
- Auto-approves all changes
- `--print` mode outputs everything at end (no interactive UI)

## Background Execution

For long-running tasks, use background mode with wake trigger:

```bash
exec background:true workdir:/path/to/repo command:"claude --permission-mode bypassPermissions --print 'Your task.

When completely finished, run: openclaw system event --text \"Done: [summary]\" --mode now'"
```

This pings Skippy immediately when CC finishes instead of waiting for next heartbeat.

## Monitoring Background Sessions

```bash
# List active sessions
process action:list

# Check specific session
process action:poll sessionId:XXX

# Get output
process action:log sessionId:XXX

# Kill if stuck
process action:kill sessionId:XXX
```

## Key Rules

1. **Always run from repo root** — so CC sees local CLAUDE.md
2. **No PTY for Claude Code** — use `--print` mode instead
3. **Plan before execute** for complex tasks
4. **Add wake trigger** for background tasks
5. **Track in HEARTBEAT.md** — add session ID to "Background Tasks" section
6. **Never code directly** — delegate everything to CC
7. **NEVER use timeout parameter** — it kills CC mid-execution. Let it run to completion.

## When NOT to Use CC

- Simple file reads → use `read` tool
- One-line config edits → use `edit` tool
- Questions about code → just read and answer

## Example: Full Two-Phase Delegation

```bash
# Phase 1: Get the plan
cd ~/Dropbox/Code/myproject && claude --permission-mode plan --print "Add authentication to the API"

# Review plan output...

# Phase 2: Execute
cd ~/Dropbox/Code/myproject && claude --permission-mode bypassPermissions --print "Add authentication to the API"
```

## Example: Overnight Background Task

```bash
exec background:true workdir:~/Dropbox/Code/myproject command:"claude --permission-mode bypassPermissions --print 'Refactor the database layer to use connection pooling.

When completely finished, run: openclaw system event --text \"Done: Refactored DB layer with connection pooling\" --mode now'"
```

Then add to HEARTBEAT.md:
```markdown
## Background Tasks (check on next heartbeat)
- **CC session `session-name`** — DB layer refactor
  - Will auto-notify via wake trigger when done
  - If still running at next heartbeat, check `process action:log sessionId:session-name`
```

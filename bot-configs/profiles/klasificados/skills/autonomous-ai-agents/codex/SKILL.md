---
name: codex
description: Delegate coding tasks to OpenAI Codex CLI agent. Use for building features, refactoring, PR reviews, and batch issue fixing. Requires the codex CLI and a git repository.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [Coding-Agent, Codex, OpenAI, Code-Review, Refactoring]
    related_skills: [claude-code, hermes-agent]
---

# Codex CLI

Delegate coding tasks to [Codex](https://github.com/openai/codex) via the Hermes terminal. Codex is OpenAI's autonomous coding agent CLI.

## Prerequisites

- Codex installed: `npm install -g @openai/codex`
- OpenAI API key configured
- **Must run inside a git repository** — Codex refuses to run outside one
- Use `pty=true` in terminal calls — Codex is an interactive terminal app

## One-Shot Tasks

`codex exec` is non-interactive and does **not** require `pty=true` in Hermes. Prefer foreground runs without PTY for single-pass build/review tasks.

```
terminal(command="codex exec 'Add dark mode toggle to settings'", workdir="~/project", timeout=300)
```

If you want the agent's final message captured to a file for later verification:

```
terminal(command="codex exec -o /tmp/codex-last.txt 'Review the current diff and write a concise verdict'", workdir="~/project", timeout=300)
```

For scratch work (Codex needs a git repo):
```
terminal(command="cd $(mktemp -d) && git init && codex exec 'Build a snake game in Python'", timeout=300)
```

## Background Mode (Long Tasks)

```
# Start in background with PTY
terminal(command="codex exec --full-auto 'Refactor the auth module'", workdir="~/project", background=true, pty=true)
# Returns session_id

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send input if Codex asks a question
process(action="submit", session_id="<id>", data="yes")

# Kill if needed
process(action="kill", session_id="<id>")
```

## Key Flags

| Flag | Effect |
|------|--------|
| `exec "prompt"` | One-shot execution, exits when done |
| `--full-auto` | Sandboxed but auto-approves file changes in workspace |
| `--yolo` | No sandbox, no approvals (fastest, most dangerous) |

## PR Reviews

Clone to a temp directory for safe review:

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && codex review --base origin/main", pty=true)
```

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 /tmp/issue-99 main", workdir="~/project")

# Launch Codex in each
terminal(command="codex --yolo exec 'Fix issue #78: <description>. Commit when done.'", workdir="/tmp/issue-78", background=true, pty=true)
terminal(command="codex --yolo exec 'Fix issue #99: <description>. Commit when done.'", workdir="/tmp/issue-99", background=true, pty=true)

# Monitor
process(action="list")

# After completion, push and create PRs
terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## Isolated Verification / Dirty-Tree Pattern

Use this when the main repo is dirty, the task needs a clean evidence-backed investigation, or Codex must see local uncommitted work plus extra context files.

Why: Codex only sees the workdir you give it. If you launch in a clean worktree without preloading local diffs, story files, or ops evidence, it can waste time on missing-path errors or draw the wrong conclusion.

Recommended flow:

1. Create a detached worktree from the target commit/branch.
2. Copy in any story docs, state files, or investigation notes that exist only in your main workspace.
3. If you already have a useful uncommitted patch, save it and apply it in the worktree before launching Codex.
4. Tell Codex explicitly which preloaded files are authoritative and whether network access may fail.
5. Ask for a final report file in `/tmp/...` so you can harvest the result even if the terminal log is noisy.

Example:

```
# 1) create isolated worktree
terminal(command="WT=/tmp/project-codex-$$ && git worktree add \"$WT\" HEAD >/dev/null && printf '%s\\n' \"$WT\"", workdir="~/project")

# 2) save current local diff from main workspace
terminal(command="git diff -- path/a path/b > /tmp/task.patch", workdir="~/project")

# 3) preload context into the worktree
terminal(command="WT=/tmp/project-codex-123 && mkdir -p \"$WT/ops\" \"$WT/agents/backlog\" && cp ~/project/ops/state.md \"$WT/ops/\" && cp ~/project/agents/backlog/story-123.md \"$WT/agents/backlog/\" && cp /tmp/task.patch \"$WT/\" && git -C \"$WT\" apply --3way task.patch", workdir="~/project")

# 4) launch Codex with explicit constraints and outputs
terminal(command="codex exec --full-auto 'Read the preloaded files first. Network may be unavailable; if so, rely on the copied evidence. Leave your final report at /tmp/task-report.md.'", workdir="/tmp/project-codex-123", background=true, pty=true)
```

Notes:
- Prefer creating a new temp worktree rather than deleting/reusing a fixed `/tmp/...` path; deletion often triggers approval friction.
- If Codex cannot reach production endpoints due to DNS/network limits, pre-copy your own verified ops evidence and instruct it to treat that as the fallback source.
- When verifying Python repos, specify the interpreter version if the project needs newer syntax; otherwise Codex may use the system default and hit avoidable errors.

## Batch PR Reviews

```
# Fetch all PR refs
terminal(command="git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'", workdir="~/project")

# Review multiple PRs in parallel
terminal(command="codex exec 'Review PR #86. git diff origin/main...origin/pr/86'", workdir="~/project", background=true, pty=true)
terminal(command="codex exec 'Review PR #87. git diff origin/main...origin/pr/87'", workdir="~/project", background=true, pty=true)

# Post results
terminal(command="gh pr comment 86 --body '<review>'", workdir="~/project")
```

## Rules

1. **Use `codex exec` without PTY for one-shots** — foreground non-interactive runs are the clean default in Hermes
2. **Use `pty=true` only for interactive Codex sessions** — the top-level Codex TUI needs PTY, but `exec` does not
3. **Git repo required** — Codex won't run outside a git directory. Use `mktemp -d && git init` for scratch
4. **Use `exec` for one-shots** — `codex exec "prompt"` runs and exits cleanly
5. **Use `-o <file>` when you need a durable artifact** — captures the last message for later verification or handoff
6. **`--full-auto` for building** — auto-approves changes within the sandbox
7. **Background for long tasks** — use `background=true` and monitor with `process` tool when you truly need async execution
8. **Don't interfere** — monitor with `poll`/`log`, be patient with long-running tasks
9. **Parallel is fine** — run multiple Codex processes at once for batch work
10. **Be explicit about shell assumptions inside Codex** — some exec environments may miss common PATH tools like `curl`, `rg`, `sort`, or `uniq`; for reliable probes, ask Codex to use Python stdlib or other guaranteed-available tooling instead of assuming those binaries exist
11. **Do not rely on Codex sandbox for authoritative browser/Playwright signoff unless you prove Chromium can launch there first** — a host worktree may pass Playwright-backed pytest locally while `codex exec` fails the same rendered-browser suite because Chromium is blocked by the Codex sandbox. For Lane-A-style browser gates, either (a) prove the browser can launch inside Codex before treating it as a verifier, or (b) run the rendered check on an unsandboxed local/staging surface and have Codex review the artifact/results instead. If Codex cannot reproduce the browser gate, report the branch as packaged-but-not-browser-approved rather than calling it staging-ready.
12. **Model/account compatibility can make Codex unavailable in cron or PR review** — if `codex exec` fails before doing work with errors such as `model requires a newer version of Codex`, `The 'gpt-5.5' model requires a newer version of Codex`, or `model is not supported when using Codex with a ChatGPT account` for models like `gpt-5.1-codex-max`, `gpt-5.1`, `gpt-5`, or `o3`, treat Codex as unavailable for that slot rather than retrying the same model loop. Record the exact rejected models in the artifact/state, keep the story at the conservative gate, and use another independent verifier or direct deterministic checks. Do not claim an independent Codex PASS when no `-o` artifact was written. If the user's protocol requires Codex adversarial QA, say explicitly that Codex was attempted but unavailable, then run a read-only Claude Code verifier fallback plus deterministic tests and document that substitution in the PR artifact.

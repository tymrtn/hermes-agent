---
name: klasificados-immediate-bugfix-pr
description: Run an urgent Klasificados bugfix from diagnosis to GitHub issue, Claude Code implementation, verification, and PR without losing Tyler in vague cron/status noise.
version: 1.0.0
author: Nagaklas
---

# Klasificados Immediate Bugfix PR

Use when Tyler asks to fix a Klasificados product bug immediately and send a PR, especially after a cron or agent worked the wrong task.

## Core rule

Do the work through coding agents, not direct edits by Nagaklas. Claude Code is primary. Codex is adversarial QA when available. If Codex is unavailable, document the exact failure and use a read-only Claude Code verifier fallback plus deterministic tests.

## Workflow

1. Create a clean worktree from current `origin/main`, not from the dirty root checkout.
   - Preferred branch: `nagaklas/fix-<short-bug-name>`.
   - Use `/tmp/klasificados-<short-bug-name>` or another deterministic temp worktree.
   - If Hermes `terminal` is poisoned by the stale `~/Dropbox/code/klasificados` cwd, use `execute_code` + Python `subprocess.run(..., cwd=/Users/wondermonkey/Dropbox/Code/klasificados)`.

2. Diagnose before implementing.
   - Inspect current `main`, the user-referenced behavior, and any candidate stale branch.
   - If a prior branch exists, treat it as prior art, not something to blindly merge.
   - Save a concise plan under `ops/handoffs/YYYYMMDD-<bug>-plan.md`.

3. Create the GitHub issue before implementation.
   - Include bug description, diagnosis, expected behavior, and acceptance criteria.
   - Link the issue in the implementation prompt and final PR body.

4. Run Claude Code implementation with strict TDD.
   - Prompt Claude Code to read `CLAUDE.md`, the plan, relevant files, and candidate prior branch diff.
   - Require: RED tests first, captured failing result, minimal fix, GREEN tests, durable implementation report under `ops/handoffs/`.
   - Require Claude Code to commit, but not push or merge.

5. Rebase before final verification.
   - Fetch and rebase onto current `origin/main` after Claude finishes. `main` can move during a long agent run.
   - Re-run targeted tests after the rebase. A pre-rebase failure can be a false regression if main moved.

6. Verify.
   - Run the targeted bug tests.
   - Run adjacent regression tests for touched surfaces.
   - Run `git diff --check origin/main...HEAD`.
   - If relevant, open the deploy preview after PR creation, but do not overclaim browser proof when the preview cannot exercise the backend API path.

7. Adversarial QA.
   - Try Codex first with `codex exec -o /tmp/<bug>-review.md ...`.
   - If Codex fails before doing work with model/account/version errors, record exact rejected models and do not retry the same loop.
   - Use a read-only Claude Code verifier fallback and deterministic tests; document the substitution in `ops/handoffs/YYYYMMDD-<bug>-qa.md` and PR body.

8. Push and create PR.
   - PR body must include summary, `Closes #issue`, exact test commands/results, QA notes, and review focus.
   - Check PR checks with `gh pr checks --watch`.
   - Add a Todoist comment on the relevant task with PR URL, issue URL, deploy preview URL if available, and where test instructions/evidence live.

## Reporting to Tyler

Use one short item with the PR link first. Include test instruction location. Example:

```text
68: homepage search filters fixed. Test instructions/evidence are in the PR body and `ops/handoffs/*homepage-search-fix*`.
https://github.com/tymrtn/klasificados/pull/68
```

If deploy preview cannot fully exercise backend behavior, say that plainly. Do not imply staging/browser proof when only local backend/unit verification is green.

## Pitfalls

- Do not let a cron-selected unrelated story substitute for Tyler's referenced bug.
- Do not use stale candidate branches as review links if they include unrelated commits.
- Do not trust pre-rebase adjacent test failures; rebase onto current `origin/main` and rerun.
- Do not claim Codex QA if Codex failed before writing an artifact.
- Do not hide missing backend preview coverage behind a green Netlify deploy preview.

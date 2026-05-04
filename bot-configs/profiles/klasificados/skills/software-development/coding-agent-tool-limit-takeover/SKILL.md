---
name: coding-agent-tool-limit-takeover
description: Take over when Claude Code, Codex, or another coding agent stops because of a tool-call/context/time limit after making local changes that were not fully committed or pushed.
version: 1.0.0
author: Nagaklas
---

# Coding Agent Tool-Limit Takeover

Use this when a user forwards a handoff saying an agent hit a tool-call/context/time limit before committing, pushing, updating task state, or finishing final verification.

## Goal

Convert a partial local agent result into a verified branch state without redoing the whole task or losing useful local work.

## Procedure

1. Identify the exact worktree and branch from the handoff or recent context.
   - Run `git status --short --branch` and `git log --oneline -8 --decorate` in that worktree.
   - Confirm whether the branch is ahead/behind its remote.
   - Do not use the dirty project root when the story has a dedicated worktree.

2. Inspect only the uncommitted/local delta first.
   - Run `git diff --stat` and targeted `git diff -- <files>` for the files named in the handoff.
   - Read any handoff/proof artifacts mentioned by the prior agent.
   - Distinguish already-committed work from still-uncommitted recovery work.

3. Verify the local recovery claim before committing.
   - Run the smallest relevant test command.
   - If the change mounts routes or changes app wiring, run an import/route-list check when possible.
   - For browser/Playwright evidence, prefer existing saved artifacts if they are complete; rerun only if the artifacts are missing, contradictory, or from an unsafe environment.

4. Commit and push the exact recovery delta.
   - Stage only the relevant code/proof files, not unrelated untracked scratch files.
   - Use a story-scoped conventional commit message, e.g. `fix(web): mount account routes for seller header proof [story-330]`.
   - Push the same branch.

5. Update the task bus.
   - Add a Todoist comment to the affected story/bug with the latest commit link, compare link, test result, and remaining integration caveat.
   - If a dependent story is stacked on the same branch, update that Todoist task too.
   - Use the project bot attribution prefix required by the Todoist skill.

6. Final report.
   - State what was already committed before takeover.
   - State what you committed and pushed.
   - Include commit and compare links.
   - Include verification results.
   - State any remaining merge/staging caveat, especially stacked-story scope.

## Pitfalls

- A prior agent's phrase `local fixes` may include a mix of already-committed and uncommitted work. Inspect `git log`, `git diff`, and `git status` before assuming.
- Do not blindly `git add .`; agent worktrees often contain untracked scripts, screenshots, empty output files, and older evidence that should not all be committed.
- If Todoist CLI raw calls fail, check the Todoist skill syntax. For `todo raw`, use `--body`, not `--data`.
- For Klasificados, code-affecting implementation should be done by Claude Code/Codex; the takeover agent may commit/push already-produced local work after verifying it.

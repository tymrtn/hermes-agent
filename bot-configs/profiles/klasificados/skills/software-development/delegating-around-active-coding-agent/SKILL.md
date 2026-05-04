---
name: delegating-around-active-coding-agent
description: Safely add parallel delegated work (Claude Code, Codex) while another coding agent is already executing a sprint — without colliding on files, branches, or story ownership.
version: 1.0.0
author: Nagaklas
---

# Delegating around an active coding agent

Use this when the user says something like "Claude Code is working on X, Y, Z — how can you help?" and you need to find useful parallel work without stepping on the active session.

## When to use

- Another coding agent (CC, Codex, OpenCode) has an active todo list or claimed stories
- User wants you to add capacity, not duplicate effort
- The active agent lists items marked "blocked by #N" or similar internal IDs
- You can see in-flight work via worktrees, branches, or `agents/active/<agent>/` claims

## Core principle

Your job is **parallel, not overlapping**. The fastest path to collision is:
- touching the same files the active agent touches
- branching from the same base and committing while they're working
- "helping" by starting a blocked story earlier in the DAG

Find work that is genuinely independent, or pre-stages work the active agent will pick up later.

## Inspection order

Before proposing any delegation:

1. **Read the active agent's todo list literally.** Items marked "blocked by #N" usually refer to the agent's own internal todo IDs, not GitHub issues. Do not treat them as GH issues until proven otherwise (`gh issue list`).

2. **Find the sprint/plan doc.** Story files typically have `parent_sprint: plan-YYYYMMDD-<name>` frontmatter. Read the plan if it exists — it shows the intended ordering.

3. **Read the story frontmatter for `depends_on: []`.** A story with no declared deps is safe to run in parallel. A story with deps on the active agent's current work is not.

4. **Check file overlap.** Each story typically lists a `## Files` section. If your candidate story and the active agent's current story touch the same files, pick something else.

5. **Check worktrees.** `git worktree list` shows what branches are materially checked out. Active worktrees under `.codex/worktrees/`, `.claude/worktrees/`, or `/tmp/<project>-*` indicate running agents. Do not commit to those branches.

6. **Check `agents/active/<agent>/` for claims.** Stories with `claimed_by:` frontmatter are owned until released.

7. **Use git reality as the tie-breaker.** If `agents/active/` is empty or only contains placeholder directories like `.gitkeep`, trust recent commits, dirty tracked files, and active worktrees over the absence of a claim file. In practice on Klasificados, explicit claims can lag behind actual agent activity.

8. **Prune stale worktree metadata before deciding what is active.** Run `git worktree prune` first, then inspect `git worktree list`. Old `prunable` entries can make the repo look far busier than it really is and can obscure the one or two live worktrees that actually matter.

9. **If remote fetch is broken, branch from clean local `main` instead of stalling.** On Tyler's machine, HTTPS git auth can fail when the configured credential helper points at a missing `gh` binary. If `git fetch origin main` fails for that reason, create the safe parallel worktree from the local `main` branch, record that the branch is local-only, and proceed with the story handoff rather than blocking the whole slot on auth repair.

## Selection heuristics (in priority order)

Prefer candidates that satisfy **all** of these:

1. `depends_on: []` in frontmatter
2. Zero file overlap with the active sprint's in-flight story
3. New tables / new routes / new modules rather than edits to shared code
4. P1 or higher business value (don't burn capacity on nice-to-haves just because they're safe)

Also viable:

- **Prep work** for a story the active agent will pick up next: prompt templates, schema docs, fixture scaffolding, test harnesses. These land in separate files and accelerate the next story without touching the current one.
- **Backlog hygiene**: metadata repairs, frontmatter normalization, stale-claim cleanup. Always safe, always useful.

Avoid:

- Starting earlier steps in the same DAG (you'll race the active agent's merge)
- "Refactoring" files the active agent is editing
- Opening PRs against the same branch they're working from

## Worktree discipline

Each delegated track should run in its **own worktree** off `main` (or off a clean base), not the currently dirty working tree:

```
git worktree add /tmp/<project>-<track-name> -b <track-branch> origin/main
```

This isolates commits, makes rollback trivial, and lets you run tests without fighting the active agent's filesystem state.

## Proposal format

When reporting to the user, structure as:

1. **What the active agent owns** (one line summary, don't re-describe their todos)
2. **Chokepoint identified** (which item everything else blocks on)
3. **Safe parallel tracks** in a table: track name, agent (claude-code/codex), scope, why it's non-colliding
4. **Explicit "will not touch"** list (the active agent's files/stories)
5. **Ask for green-light** per-track — don't bundle approval

## Pitfalls

- **"Blocked by #39" is rarely a GH issue.** Almost always an internal todo ID inside the active agent's checklist. Verify with `gh issue list` before acting on it.
- **Don't start the blocked stories "just to unblock".** If 311B is blocked by 311A and CC is on 311A, starting 311B now means racing CC's commits. Do the prep work (prompts, schemas, fixtures) instead.
- **Don't commit to the active worktree's branch.** Even if `git status` looks clean in your shell, the agent may be mid-edit.
- **Don't reassign claimed stories.** If `claimed_by: claude-code` is in frontmatter, leave it.
- **Don't delegate to the same agent type already active** for overlapping scope. If CC is running, prefer Codex for your parallel track (different identity, different worktree, fewer collision risks).

## Verification before reporting

- [ ] Candidate stories have `depends_on: []` or deps already met
- [ ] Zero file overlap confirmed by diffing `## Files` sections
- [ ] Candidate worktrees are distinct from the active agent's
- [ ] No candidate story has a `claimed_by:` on another agent
- [ ] Report lists explicit "will not touch" boundaries

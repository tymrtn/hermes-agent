---
name: klasificados-review-packets
description: Prepare Klasificados Tyler review/approval packets with Todoist comments, formatted GitHub links, staging/prod links, and screenshot evidence so Tyler never has to hunt for context.
version: 1.0.0
author: Nagaklas
---

# Klasificados Review Packets

Use whenever asking Tyler to review, approve, unblock, taste-check, or decide on a Klasificados story, branch, visual artifact, staging gate, or production decision.

## Core rule

Do not ask Tyler to hunt, and do not paste the hunt into Telegram.

Telegram closeouts for status/deploy/review reports must use Tyler's compressed format exactly:

```text
168: ready, needs deploy approval.
https://github.com/tymrtn/klasificados/compare/main...nagaklas/story-168-contact-volume-dashboard

308: not ready. Visual/spec mismatch. No action from you.
```

Rules: one line per item; decision/action first; one raw visible URL only if useful; omit everything else unless Tyler asks. Full review packets belong behind links unless Tyler explicitly asks for the full packet in chat.

A review request is not ready unless it includes:
1. clickable Todoist task link(s),
2. a real task description/user story rather than a cron status dump,
3. Tyler comments from those tasks checked and incorporated,
4. formatted GitHub links for pushed branches/commits/compare views or a clear statement that no GitHub link exists yet,
5. staging/prod URLs when relevant,
6. screenshots or accessible image links for visual/taste work,
7. one crisp decision requested,
8. a sanity check that the reported branch/story actually matches the user-visible problem or Tyler request being replied to.

If Tyler says a report appears unrelated to the issue he expected, immediately audit rather than defend: search prior conversation/session context, inspect the reported branch diff/log, inspect Todoist/backlog for the intended issue, identify the correct candidate branch/task if it exists, and create or update a P1 blocker/bug task that points the next slot at the right review surface. Do not substitute a nearby story number or generic “ready for review” report for the issue Tyler actually asked about.

If the work is merely dev bookkeeping, cleanup, QA packaging, or a checkpoint, keep it in the `Tasks` section or as a subtask/comment on its parent. Do not create noisy top-level Tyler-facing approval tasks for subtask-grade status updates.

## Todoist task shape and sections

Use the Klasificados Todoist project as the same-day task bus, but keep it readable.

Sections:
- `Stories` — user-meaningful product/story work from the repo backlog.
- `Bugs` — regressions, incidents, and user-reported broken behavior.
- `Tasks` — operational chores, grooming, QA packets, checkpoint cleanup, and subtask-grade work.
- Parent/no section — only non-dev-specific items Tyler explicitly wants at the parent level.

Task titles:
- Must be short and human-readable.
- Must not be cron closeout summaries.
- Must not begin with bot name/flag/timestamp/test-count noise.
- Put status, branch names, test counts, blockers, and caveats in the description or comments.

Bad title:
`Nagaklas: Klasificados Lane A - story-320/321 12:00 cleanup is locally QA-packaged with caveats...`

Good title:
`Story 320/321: XPath parser integration needs staging-ready package`

For bugs, include a user story or observed/expected behavior in the description and attach/link the screenshot evidence if the user provided a screenshot.

## Todoist comments are mandatory context

Normal `todo list` output does not include task comments. Before acting on or reporting a Klasificados task that is `needs-approval`, `blocker`, active sprint work, or a review gate, fetch comments explicitly:

```bash
todo raw GET /comments?task_id=TASK_ID
```

Treat Tyler comments as Founder Oracle input. They can approve, revise, block, or supersede task descriptions. Do not rely on the task title/description alone.

When reporting a task, format it as a Markdown link:

```markdown
[Task title](https://app.todoist.com/app/task/TASK_ID)
```

If the API omits `url`, construct it from the id.

## GitHub link rules

For Tyler-facing Telegram reports, use visible raw URLs, not hidden Markdown links. Tyler wants to see the actual target before tapping. Keep the digest short; include at most the one or two links needed for the decision.

Good for Telegram:

```text
Compare: https://github.com/tymrtn/klasificados/compare/main...nagaklas/story-168-contact-volume-dashboard
Commit: https://github.com/tymrtn/klasificados/commit/FULL_SHA
Todoist: https://app.todoist.com/app/task/TASK_ID
```

Avoid in Telegram:

```markdown
[compare](https://github.com/tymrtn/klasificados/compare/main...branch)
[725edab](https://github.com/tymrtn/klasificados/commit/FULL_SHA)
```

Inside GitHub issues, PRs, repo artifacts, and Todoist descriptions, Markdown links are acceptable when they improve readability.

If a commit is local-only, a GitHub commit link may 404. Either:
- push a review branch first, when safe and allowed, then provide links; or
- state clearly: `No GitHub link exists yet; branch is local-only`, and give local worktree/branch as secondary evidence.

## Branch naming

New review/work branches must be named:

```text
bot_name/story-id-story-name
```

Examples:
- `nagaklas/story-330-seller-dashboard-edit-links`
- `nagaklas/story-309-entity-hero-render-prod`
- `claudedev/story-320-xpath-parser-integration`

Avoid timestamp-first or opaque branch names. Add a short suffix only if there is a collision.

## Visual/taste work

Do not ask for visual approval from prose.

Include:
- screenshots as `MEDIA:` attachments in chat, or accessible GitHub/hosted image links in Todoist,
- what each screenshot demonstrates,
- desktop + mobile for primary path,
- variant/fallback state when relevant,
- staging/prod URL if deployed.

Repo-local paths are not enough for Todoist review unless also delivered as media or linked via pushed branch.

## Staging/prod link rules

If a story affects live behavior, include:
- current prod URL(s),
- staging URL(s) if deployed,
- `No staging URL yet` if not deployed,
- exact gate: local-only, branch pushed, staging-ready, staging-deployed, prod-gated.

Do not leave missing staging links implicit.

## Review packet template

```markdown
## Review packet: story-NNN — short title

Decision needed: [approve / reject / choose A vs B / taste call]

Todoist:
- [Task title](https://app.todoist.com/app/task/TASK_ID)
- Comments checked: yes/no; summary: ...

GitHub:
- [branch](...)
- [compare](...)
- Commits: [short](full commit URL), [short](full commit URL)

Code/docs touched:
- `path/to/file.py`
- `path/to/template.html`

Staging/prod:
- Prod: [route](...)
- Staging: [route](...) or `No staging URL yet`

Visual evidence, if applicable:
- MEDIA:/absolute/path.png or [screenshot](...)
- what it proves

Status:
- proven:
- not proven:
- next gate:

Decision options:
- Approve
- Hold for X
- Founder Oracle may decide under condition Y
```

## Common failure modes

- Giving Tyler bare commit hashes instead of clickable links.
- Linking commits before pushing the branch, causing 404s.
- Mentioning screenshots exist but not attaching or linking them.
- Saying `staging` without providing a staging URL or explicitly saying none exists.
- Ignoring Todoist comments because `todo list` did not show them.
- Asking Tyler to approve a visual/taste story from prose only.

---
name: todoist-comment-retry
description: Handle Todoist comment/update failures from todo.py, especially silent long-comment failures during bot task-bus updates.
version: 1.0.0
author: Nagaklas
---

# Todoist Comment Retry

Use when adding bot-authored Todoist comments with `todo raw POST /comments` fails, returns exit `-1`, returns no useful output, or needs verification before reporting task-bus updates as complete.

## Trigger

- You are updating Todoist comments/descriptions as part of an ops loop.
- `todo raw POST /comments --body ...` fails with exit `-1` or empty output.
- The comment body is long, contains a detailed review packet, or includes multiple bullets/links.

## Procedure

1. Verify the attempted comment did not land:

```bash
todo raw GET '/comments?task_id=TASK_ID'
```

2. Shorten the comment to the routing-critical fields only:

- bot attribution prefix, for example `🇵🇷 Nagaklas:`;
- timestamp or slot name;
- canonical status;
- classification;
- blocker type if any;
- plain-English reason;
- owner;
- next action;
- retry condition or deadline.

3. Retry with `todo raw POST /comments --body ...`.

4. Verify again with:

```bash
todo raw GET '/comments?task_id=TASK_ID'
```

5. Only report the Todoist update as complete after the new comment appears in the GET response.

## Notes

- Put long evidence packets in repo artifacts such as `ops/handoffs/...md` or `ops/review-packets/...md`, then reference the path or link from the shorter Todoist comment.
- Do not keep retrying the same long body. If the first long-body POST fails silently, shorten before retrying.
- Keep Tyler-facing task titles short; move evidence, links, caveats, and gate details into descriptions/comments.

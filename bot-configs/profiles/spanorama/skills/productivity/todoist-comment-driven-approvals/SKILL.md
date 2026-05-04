---
name: todoist-comment-driven-approvals
description: When Tyler says he commented on a Todoist task or approval item, immediately open the task, read the latest comment, and act without waiting for pasted text.
tags: [todoist, approvals, comments, task-management]
triggers: ["I commented on that task", "I posted a comment on that approval", "read my Todoist comment", "check the task thread"]
---

# Todoist comment-driven approvals

Use this when Tyler references a Todoist task comment indirectly instead of pasting the comment text.

## Why
The comment itself is the instruction. Waiting for Tyler to restate it creates friction and loses the point of using Todoist as the task bus.

## Workflow
1. Identify the likely task from current conversation context, linked thread text, or recent project/task references.
2. Fetch the task object first so you have the exact task, project, status, and description.
3. Fetch the task comments and read the latest relevant comment.
4. Act on the comment immediately.
   - If the comment requests missing context, add that context in a new task comment.
   - If the task description is vague, tighten it so the approval item is self-contained.
   - If the comment implies a follow-up task, create or update the task accordingly.
5. Report back with the task title, what the comment said, and what action you already took.

## Default actions
Common approval-thread actions:
- "Can you post a link to what needs approval?" → add the source doc/link and summarize the decision points in the comment thread.
- "Revise this" → update the task description or linked draft, then comment with the revised version.
- "Approved" or task completed → treat as approval and proceed only if the surrounding workflow explicitly allows execution.

## Rules
- Do not ask Tyler to paste the comment if the task can be identified from context.
- Read the thread before acting.
- Prefer making the task self-contained so later reviewers do not have to hunt for context.
- Report the action succinctly after doing it.

## Example
In the SpainExpat Membership project, Tyler said he had commented on the approval task. The correct behavior was:
- fetch the approval task
- read the latest task comment: "Can you post a link to what needs approval?"
- add a comment containing the source plan path and the concrete approval points
- update the task description to include the same decision context
- then tell Tyler it was handled

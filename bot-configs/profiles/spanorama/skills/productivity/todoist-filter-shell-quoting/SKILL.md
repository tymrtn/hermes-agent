---
name: todoist-filter-shell-quoting
description: Prevent shell metacharacters in Todoist filter expressions from breaking terminal calls.
tags: [todoist, shell, quoting, filters, productivity]
triggers: ["todoist filter", "todo list --filter", "shell quoting", "& backgrounding", "todoist project filter"]
---

# Todoist filter shell quoting

When calling `todo.py` or the `todo` wrapper from a shell, Todoist filter expressions often contain shell metacharacters such as `&`, `|`, and parentheses.

## The gotcha
If you run something like:

```bash
todo list --filter "#SpainExpat Membership & @needs-approval"
```

the shell can treat `&` as a background operator instead of part of the filter. In Hermes terminal calls this can produce an error like:

- `Foreground command uses '&' backgrounding.`

This is a shell quoting failure, not a Todoist failure.

## Safe pattern
Wrap the **entire filter expression in single quotes** at the shell level:

```bash
todo list --filter '#SpainExpat Membership & @needs-approval'
todo list --filter '#SpainExpat Membership & (today | overdue)'
```

## When to use this
Any time the Todoist filter includes:
- `&`
- `|`
- parentheses
- project names starting with `#`
- labels like `@needs-approval`

## Rule of thumb
- In direct shell/terminal commands: prefer **single quotes** around the whole filter.
- Only use double quotes if you have a specific interpolation need and know how to escape shell metacharacters.

## Verification
If the command unexpectedly errors before reaching Todoist, especially with backgrounding language, retry immediately with the filter wrapped in single quotes.

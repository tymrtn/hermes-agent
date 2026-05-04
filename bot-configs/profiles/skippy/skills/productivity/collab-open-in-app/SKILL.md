---
name: collab-open-in-app
description: When collaborating with Tyler on a document (resume, draft, spec, outline), open it in his preferred editor via `open -a <App>` instead of pasting file paths or inlining long content. He can edit in place; Skippy re-renders/processes after.
tags: [collaboration, editing, workflow, macos]
triggers: ["edit the resume", "make manual edits", "edit this document", "open in cursor", "open in pages", "let me tweak"]
---

# Open-in-App Collaboration Pattern

## When to Use
Any time Tyler needs to make manual edits to a file we're collaborating on — resumes, long-form drafts, specs, outlines, configs, markdown docs. Saves him from copy-pasting, asking for paths, or switching mental context.

## The Pattern
```bash
open -a <AppName> <filepath>
```

Common apps:
- `Cursor` — code, markdown, any text he's going to edit with AI assist
- `Pages` — letters, formal docs
- `Preview` — PDFs (read-only or quick look)
- Default (no `-a`): `open <filepath>` uses system default for that filetype

Other useful flags:
- `open .` — opens current directory in Finder
- `open -R <file>` — reveals file in Finder (useful for "where is this?" questions)
- `open -a Cursor <dirname>` — opens a whole folder as a Cursor workspace

## Workflow
1. Skippy produces v1 of the document (markdown source + rendered output)
2. Save both to `~/Dropbox/Skippy/<category>/` (survives across sessions, synced across devices)
3. `open -a Cursor <markdown source>`
4. Tyler edits in place
5. When Tyler says "done" — Skippy re-renders / re-processes / ships

## Why This Beats Alternatives
- **Beats pasting long content in chat:** chat is ephemeral, clutters context, no edit history
- **Beats sending file paths:** Tyler can't see paths on Telegram; they're dead-ends on mobile
- **Beats asking "what edits":** he can make them himself in 30s vs narrating them

## Canonical Output Location
`~/Dropbox/Skippy/<category>/<project>/` — e.g.
- `~/Dropbox/Skippy/resumes/` — resumes, CVs
- `~/Dropbox/Skippy/drafts/` — email drafts, letters
- `~/Dropbox/Skippy/specs/` — technical specs, proposals

Dropbox = available across all Tyler's machines; persists across Skippy sessions.

## Don't
- Don't open destructive or auto-running files (no `open -a "Script Editor"` for `.sh`/`.command`, etc.) without confirmation
- Don't open multiple files at once without flagging what's happening
- Don't open files in apps Tyler doesn't use (no VS Code if he uses Cursor; check system first with `ls /Applications/` if unsure)

## Note
This is a macOS-only pattern. Falls back to `xdg-open` on Linux, `start` on Windows.

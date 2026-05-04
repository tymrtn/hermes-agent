---
name: hermes-mac-survival-copy
description: Create a fast USB/survival-copy plan for moving Skippy/Hermes between Macs when Time Machine is slow or a deadline is active.
tags: [hermes, migration, macos, usb, backup, legal-retention]
triggers: ["move Skippy to another Mac", "USB stick", "Time Machine stuck", "new box", "migrate Hermes", "survival copy"]
---

# Hermes Mac Survival Copy

Use when Tyler needs to move or preserve Skippy/Hermes quickly between Macs, especially if Time Machine is stuck in “Preparing…” and another deadline matters more.

## Principle

Do not let migration perfection block the urgent user task. If an application/submission deadline is closing, preserve the survival payload and finish the deadline first.

## First checks

```bash
date
tmutil status 2>&1 || true
df -h /System/Volumes/Data /Volumes/* 2>/dev/null || true
```

If Time Machine is only in `BackupPhase = Starting` / preparing, treat it as unreliable for immediate deadline planning.

## Required survival payload

Copy these first:

```text
/Users/wondermonkey/.hermes
/Users/wondermonkey/.claude
/Users/wondermonkey/.codex
/Users/wondermonkey/.local
/Users/wondermonkey/.config
/Users/wondermonkey/Library/LaunchAgents
```

Why:
- `.hermes`: profiles, config, skills, memory, scripts, shared stores, session/conversation history.
- `.claude` / `.codex`: coding-agent auth/state. Browser re-login may still be required on the target Mac.
- `.local` / `.config`: Envelope shim, CLI tools, MCP/email config.
- `LaunchAgents`: gateway/cron job definitions. Do not blindly load on the target until paths/config are checked.

If Dropbox is not fully synced or the urgent work lives there, also copy:

```text
/Users/wondermonkey/Dropbox
```

## Optional / usually not deadline-critical

```text
/Users/wondermonkey/Library/Application Support/Claude
/Users/wondermonkey/Library/Application Support/Codex
```

Claude Desktop app support can be huge (~12GB on Neo, mostly `vm_bundles`). Do not block a deadline on it unless Tyler explicitly wants a full app-state clone.

Sensitive maybe-copy items:

```text
/Users/wondermonkey/.ssh
/Users/wondermonkey/.gitconfig
/Users/wondermonkey/.gnupg
```

Treat as secrets. Prefer re-auth where possible unless Git/SSH continuity is urgent.

## Fast USB copy command

Adjust destination volume as needed:

```bash
DEST="/Volumes/Drive 1/skippy-survival-$(date +%Y%m%d)"
mkdir -p "$DEST"
rsync -aE --info=progress2 \
  /Users/wondermonkey/.hermes \
  /Users/wondermonkey/.claude \
  /Users/wondermonkey/.codex \
  /Users/wondermonkey/.local \
  /Users/wondermonkey/.config \
  /Users/wondermonkey/Library/LaunchAgents \
  "$DEST/"
```

If copying Dropbox too:

```bash
rsync -aE --info=progress2 /Users/wondermonkey/Dropbox "$DEST/"
```

## Legal retention rule

Hermes session history, conversation transcripts, chat exports, and archives that may contain old conversations are protected legal-retention data. Do not prune or omit them unless Tyler explicitly approves. A survival copy must preserve `.hermes/sessions`.

Verify after copy:

```bash
du -xsh "$DEST/.hermes/sessions" 2>/dev/null || true
find "$DEST/.hermes/sessions" -type f | wc -l
```

## Target Mac restoration notes

- Copy files into the same paths under `/Users/wondermonkey` if possible.
- Re-run Claude/Codex browser auth from the target Hermes context; copied state may not survive Keychain/session boundaries.
- Review LaunchAgents before loading: paths, model providers, local ports, and profile names may differ.
- Verify Telegram gateway, Claude, Codex, Envelope, session search, and cron jobs before declaring migration complete.

## Pitfalls

- `.hermes` alone is necessary but not sufficient for a smooth Skippy transplant.
- Do not assume Time Machine is progressing just because it says “Preparing.” Check `tmutil status`.
- Do not spend deadline minutes copying giant optional app-state folders when a paste-ready submission is the true gate.
- Do not store credential values in chat summaries. If encountered, redact as `[REDACTED]`.

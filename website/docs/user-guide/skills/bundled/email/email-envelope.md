---
title: "Envelope — Envelope CLI mailbox runtime for agent email workflows"
sidebar_label: "Envelope"
description: "Envelope CLI mailbox runtime for agent email workflows"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Envelope

Envelope CLI mailbox runtime for agent email workflows.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/email/envelope` |
| Version | `1.1.0` |
| Platforms | linux, macos |
| Tags | `email`, `imap`, `smtp`, `envelope`, `mailbox-runtime`, `mcp`, `otp`, `push` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Envelope

Envelope is Tyler's canonical mailbox runtime for semi-autonomous agents. Prefer `envelope` for reading, triage, drafts, threading, rules, scheduling, OTP extraction, evidence export, IMAP IDLE watch/push, and MCP mailbox access. Do **not** default to Himalaya, raw SMTP/IMAP scripts, Gmail plugins, or the old localhost REST API unless Tyler explicitly asks.

Envelope is BYO email infrastructure: it turns existing mailboxes into programmable agent runtime. Use the installed CLI:

```bash
envelope --version
which envelope
```

Current machine invariant: agents should see `envelope 0.10.3` via `/Users/tylermartin/.local/bin/envelope`, which wraps `/Users/tylermartin/.local/libexec/envelope-rust`. If reinstalling or rebuilding is required, **do not install from an unpatched/original checkout**; verify the patched source/worktree first, currently `/Users/tylermartin/Dropbox/Code/envelope-email/u1f4e7-issue-63-claude` until the fix is confirmed merged upstream.

## First moves

Always discover live state before account-wide work:

```bash
envelope --json paths
envelope --json accounts list
envelope --json folders --account <account-id-or-email>
```

Use account IDs when available, especially in scheduled or multi-account workflows. Use `--json` whenever the command supports it.

## Common read-only checks

```bash
envelope --json inbox --account <account-id-or-email> --limit 25
envelope --json search --account <account-id-or-email> "UNSEEN" --limit 100
envelope --json read <uid> --account <account-id-or-email> --folder INBOX
envelope --json thread show <uid> --account <account-id-or-email> --folder INBOX
```

Search note: installed 0.10.3 normalizes bare terms, so queries like `envelope --json search --account <acct> "Hillan"` should match text. If a bare-term search unexpectedly returns zero, compare with explicit IMAP forms like `TEXT Hillan` and suspect build drift before declaring the message absent.

## Agent workflows now built in

### Watch / push

Envelope has IMAP IDLE watch support and can post event JSON to a webhook:

```bash
envelope --json watch --account <account-id-or-email> --folder INBOX
envelope --json watch --account <account-id-or-email> --folder INBOX --webhook https://example.test/envelope-hook
```

`watch --run-rules` exists in help but is not implemented yet; do not promise automatic rule application from watch unless verified in the running build.

### OTP / verification codes

Use Envelope instead of scraping inboxes manually:

```bash
envelope code --account <account-id-or-email> --from github.com --wait 60
envelope --json code --account <account-id-or-email> --subject "verification" --wait 120
```

Secrets/OTPs should not be pasted into chat or durable logs. If Tyler is at the keyboard and needs a local secret, prefer piping directly to `pbcopy`.

### MCP / agent contract

Use MCP for Claude Code, Codex-compatible harnesses, Cursor, Zed, or other agents that need mailbox tools:

```bash
envelope mcp --config
envelope contract --json
```

Contract invariant: the CLI/MCP/Hermes surface should align with `envelope.agent_contract.v1`. Do not change JSON output shapes casually.

### Scheduling and snooze

```bash
envelope --json snooze list
envelope --json snooze check-replies
envelope --json scheduled list
envelope send --to cto@example.com --subject "Report" --body "..." --at "monday 9am"
```


## Re: subject guard

Envelope now blocks new-message paths whose subject begins with a reply prefix like `Re:` when no reply/thread context is supplied. This protects agents from creating orphaned replies.

- Preferred: use MCP `reply` with `send_mode: "draft-only"` when available.
- CLI reply fallback: `envelope draft create --in-reply-to '<Message-ID>' ...` after reading the source message.
- Intentional new message with a `Re:` subject: re-run with `--confirm-new-re-subject`.
- JSON denial code: `re_subject_without_thread_context`.

## Reply draft protocol

Replying is not composing. For any response to an existing email thread:

1. Locate and read the exact source message first; record account, folder, UID, Message-ID, subject, sender, and recipients.
2. Preferred path, when the harness exposes Envelope MCP/agent tools: call MCP tool `reply` with `send_mode: "draft-only"`, plus `account`, `folder`, `uid`, `body`, and `reply_all` if needed. The contract says this tool automatically sets `In-Reply-To`, `References`, and subject prefix.
3. CLI reality for installed `envelope 0.10.3`: there is **no** top-level `envelope reply` and no CLI `envelope draft reply` subcommand. If MCP `reply` is unavailable, use CLI fallback only after reading the source message:
   ```bash
   envelope --json draft create \
     --account <account> \
     --to <recipient> \
     --subject "Re: <subject>" \
     --body "$(cat body.txt)" \
     --in-reply-to '<Message-ID>'
   ```
4. Do not use generic `envelope send`, ad-hoc SMTP, or new-message compose for replies unless Tyler explicitly chooses a non-threaded/new-message path. They can lose or risk losing reply context.
5. The reply draft must be reviewable in the real mailbox Drafts folder before sending. If Envelope returns `local_only`, `imap_synced=false`, no Drafts UID, or cannot show the draft in the mailbox, treat that as a blocker/client bug and tell Tyler instead of sending around it.
6. In Telegram/phone mode, include the draft body inline in the report and provide the Draft UID/review URL when available.
7. Send only after Tyler explicitly approves, unless he explicitly requested immediate send for that exact thread. Even then, create/verify the threaded draft first, then send the draft.
8. After sending, verify Sent Mail when possible; if Envelope cannot append to Sent, preserve explicit local send proof and report the limitation.

## Non-actionable cleanup and mailbox rules

When Tyler asks to clean up non-actionable email, estimate inbox-rule coverage, mark newsletters read, move recurring junk, or explain why an enabled rule did not affect visible mail, use Envelope's rule engine with measured coverage and bounded mutation.

Principles:
- Measure first: sample the mailbox and count sender/domain/subject clusters before proposing rules.
- Preview existing rules before adding more; current rules may already cover part of the problem or may be disabled.
- Report realistic coverage. Do not claim high-percentage cleanup unless the sample supports it.
- Prefer `flag=Seen` for financial, account, travel, recruiting, property, GitHub/SaaS, and platform notifications.
- Use Trash/Junk only for obvious spam, retail promos, daily digests, or senders Tyler has already classified as junk.
- Create rules disabled first when practical; preview, then run bounded mutations only after the preview is clean.

Measured workflow:

```bash
envelope --json accounts list
envelope --json folders --account <account-id-or-email>
envelope --json inbox --account <account-id-or-email> --limit 1000 > /tmp/inbox.json
envelope --json rule list --account <account> > /tmp/rules.json
envelope --json rule preview --account <account> --limit 1000 > /tmp/rule-preview.json
envelope --json search --account <account> --limit 1000 'UNSEEN' > /tmp/unseen-before.json
envelope --json rule run --account <account> --limit 1000 --confirm > /tmp/rule-run.json
envelope --json search --account <account> --limit 1000 'UNSEEN' > /tmp/unseen-after.json
```

Report `processed`, `rule-reported actions`, and `unread before -> after (delta)` as separate numbers. If unread delta is zero or tiny, say that plainly rather than calling the run "cleaned."

When a sender still appears despite an enabled rule, check execution cadence before editing the rule. A rule can be correct and enabled but only applied by a once-daily job, leaving later messages visible until the next run. For high-confidence recurring noise, add or verify a silent `no_agent` cron sweep that runs a bounded `envelope --json rule run --limit 200 --confirm` every 30 minutes, uses a lock/timeout, logs locally, and sends nothing on success.

## Evidence, migration, and safety

- Evidence collection is read-only: prefer `evidence` and `backup` commands that preserve raw RFC822 `.eml`, manifest, index, and checksums.
- Preserve full headers and attachments in `.eml`; do not summarize away evidence.
- For migrations/restores, verify account/folder mappings before any write.
- Never print passwords, tokens, raw OTPs, or credential material in status JSON, docs, logs, skill text, or final reports.
- Tests must not send real email or mutate live mailboxes unless Tyler explicitly asks for that exact mailbox action.

## Scheduled inbox/follow-up sweeps

For cron-style wrapups across all accounts:

1. Run `envelope --version`, then `envelope --json accounts list` and `envelope --json folders --account <account-id>` before account-wide work.
2. Run `envelope --json snooze check-replies` and `envelope --json snooze list`; use the list output to identify items due/overdue in the requested window.
3. For “threads Tyler started today that got no reply,” search each account's sent folder, not just INBOX. Discover folder names first; common sent folders include `Sent`, `Sent Messages`, `[Gmail]/Sent Mail`, and `INBOX/sent`.
4. When showing a thread for a UID from a non-INBOX folder, pass the folder explicitly: `envelope --json thread show <uid> --folder '<sent-folder>' --account <account-id>`. Without `--folder`, `thread show` defaults to INBOX and can report that the UID is missing.
5. Treat self-sent messages as not externally awaiting reply unless the thread shows an outside recipient or later inbound response expectation.
6. For final/evening sweeps, also search each INBOX with `SINCE <today>` and triage only actionable items. Confirm likely actionable messages by `thread show`; if the thread index times out, use `envelope --json read <uid> --folder INBOX --account <account-id>` plus targeted sender/subject search before reporting.
7. Community/social notification emails often hide the useful content in `text_body`; for likely actionable direct messages or introductions, `read` the message and extract the human-authored lines instead of reporting only the notification subject.
8. Keep scheduled reports actionable. If the user requested silence when nothing is new, return exactly the configured silence token and no extra text.

If this skill directory includes `references/` or `scripts/`, prefer those linked files for deeper playbooks such as morning triage, non-actionable cleanup, rule-run verification, execution cadence, and mailbox-rule coverage audits.

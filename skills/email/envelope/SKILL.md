---
name: envelope
description: "Use Envelope CLI for agent mailbox workflows."
version: 1.3.0
author: Tyler Martin
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [email, imap, smtp, envelope, mailbox-runtime, mcp, otp, push]
    homepage: https://github.com/tymrtn/U1F4E7
---

# Envelope

Envelope is Tyler's canonical mailbox runtime for semi-autonomous agents. Use the installed CLI:

```bash
envelope --version
which envelope
envelope --json paths
envelope --json accounts list
envelope --json folders --account <account-id-or-email>
```

Current machine invariant: agents should see Envelope **0.12.5** via `/Users/tylermartin/.local/bin/envelope`, which wraps `/Users/tylermartin/.local/libexec/envelope-rust`. Active source is `/Users/tylermartin/Dropbox/Code/envelope-email/u1f4e7-repo` on `main`. If rebuilding/installing, bump the workspace version first, update lock/schema/docs as needed, run tests, build release, back up the installed binary, install, then verify `/Users/tylermartin/.local/bin/envelope --version`. Do not leave source-only fixes uninstalled.

macOS install gotcha: do not truncate a running binary in place. Prefer `install`, or copy to a fresh temp path then move/replace so existing processes keep their inode.

## Send / proof semantics

SMTP acceptance is the authoritative “send happened” event. Sent-folder entries are traceability/mail-client hygiene, not legal delivery proof.

After a real send, preserve these JSON fields:

- `status`
- `message_id`
- `sent_folder`
- `sent_uid`
- `sent_message_url`
- `sent_mail.lookup_status`
- `sent_mail.copy_source`
- `provider_sent_copy`
- `client_appended_copy`

`sent_mail.copy_source` values:

- `provider` — SMTP provider auto-filed the Sent copy.
- `client_appended` — Envelope IMAP-APPENDed a client-side archive copy because the provider does not auto-save.
- `unresolved` — provider should auto-save but lookup has not found it yet.
- `not_attempted` — no IMAP proof attempt was possible.

Never describe `client_appended` as provider proof. It is mailbox hygiene.

## From identity / display-name checks

Outbound trust includes the visible sender identity. Expected precedence is explicit `--from` override → non-empty `display_name` → non-empty account `name` when it is not just the email address → bare username.

Envelope **0.12.5** fixed the client-appended Sent archive `From` header. For `tyler@martin.fm`, raw delivered and client-appended Sent `.eml` headers should serialize as:

```text
From: "Tyler Martin" <tyler@martin.fm>
```

When debugging “send from name still broken,” verify the raw RFC822 header/evidence bundle, not just a UI card or the test body text. Envelope `read.from_addr`/summary fields may be address-oriented for rules/search; raw `.eml` is the source of truth for SMTP/MIME header correctness.

## Agent compose/reply surface

Agent-facing workflows should use the CLI by default. Compose/reply/forward should be reviewable/draft-first unless Tyler explicitly requests immediate send for that exact message.

For replies, use contextual reply paths; never create a fresh new message with `Re:` unless deliberately non-threaded:

```bash
envelope --json draft reply <uid> --account <account> --folder <folder> --body "$(cat body.txt)"
```

Only send after explicit approval unless the current request explicitly asks to send. Immediate send requires the explicit bypass flags:

```bash
envelope --json send \
  --account <account> \
  --to <recipient> \
  --subject <subject> \
  --body "$(cat body.txt)" \
  --send-now --confirm-send-now
```

## Attachments

Use repeated `--attach <path>` flags. Envelope JSON should expose only safe attachment summaries (`filename`, `content_type`, `size`), not bytes. For attachment changes, test direct send, draft create/send, MCP send/reply/send_draft, and Sent/inbound visibility.

## Useful commands

```bash
envelope --json inbox --account <account> --limit 25
envelope --json search --account <account> "UNSEEN" --limit 100
envelope --json read <uid> --account <account> --folder INBOX
envelope --json thread show <uid> --account <account> --folder INBOX
envelope --json evidence collect --account <account> --folder <folder> --subject <subject> --out /tmp/envelope-evidence --json
envelope --json scheduled list
envelope --json snooze list
envelope mcp --config
envelope --json contract
```

## Release / QA discipline

For behavior-changing Envelope fixes: validate with real output, create/close a GitHub issue, delegate implementation to Claude Code when substantial, run local focused and workspace tests, run Codex QA as an independent reviewer for send/proof/contract changes, merge only after CI green, install locally, verify installed runtime/contract, and update harness skills/invariants.

For Sent-proof/source-semantics changes, enumerate every actual send path: CLI immediate send, CLI draft send, MCP send, MCP reply, MCP send_draft, scheduled sweep, and dashboard send if touched. Strict schemas with `additionalProperties=false` must include actual variant keys such as `error`, `cooldown_seconds`, `send_after`, `parent_ui`, `draft_ui`, and `imap_draft_deleted` where emitted.

## Do not use by default

- old hosted Envelope REST API / Railway API
- raw SMTP/IMAP scripts
- Himalaya
- Gmail plugins

Use them only for explicit legacy archaeology or when Tyler asks.

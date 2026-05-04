---
name: envelope-imap-migration
description: Build, harden, review, install, and safely operate Envelope's IMAP-to-IMAP migration feature for mailbox backup/provider migration, including Claude Code delegation, idempotency gates, dry-run coverage, and live smoke tests.
version: 1.0.0
author: Envelopie
license: MIT
metadata:
  hermes:
    tags: [Envelope, IMAP, migration, backup, mailbox, Claude-Code, release, safety]
    related_skills: [claude-code, requesting-code-review, test-driven-development, systematic-debugging, envelope-release-ops, envelope-project-hygiene]
---

# Envelope IMAP Migration Operations

Use this when Tyler asks whether the Envelope backup/migration system is ready, asks to migrate mailboxes between providers, or asks to finish/test/install `envelope migrate`.

The migration use case is provider/mailbox migration such as WorkMail/AWS-hosted mailbox access to Migadu. Remember: SES is outbound SMTP, not the mailbox store. The mailbox migration path is usually IMAP source -> raw RFC822 copy/append -> IMAP destination.

## Canonical locations

Project root:

```text
/Users/wondermonkey/Dropbox/Code/envelope-email
```

Rust repo:

```text
/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo
```

Installed public command:

```text
/Users/wondermonkey/.local/bin/envelope
```

Installed raw binary:

```text
/Users/wondermonkey/.local/libexec/envelope-rust
```

Shared Envelope HOME for agents/dashboard:

```text
/Users/wondermonkey/.hermes/shared/envelope-home
```

Always set real-user Rust PATH when building:

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
```

For live Envelope operations, use shared HOME:

```bash
export HOME=/Users/wondermonkey/.hermes/shared/envelope-home
export PATH=/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
```

## Safety principles

- Do not run a real non-dry-run migration until the source/destination pair and folder set are explicit.
- Migration must be copy-only: no source delete, expunge, or source flag mutation.
- Dry-run first, always.
- Prefer small/bounded batches before large runs.
- Treat migration idempotency as a release blocker, not a nice-to-have.
- Do not inspect `.env` or credential values.
- Do not install over the live binary until tests and review pass.
- Preserve the singleton wrapper path; install only to `/Users/wondermonkey/.local/libexec/envelope-rust`.
- Preserve dashboard Rules Control Plane when merging migration work.

## Required implementation properties

A safe `envelope migrate` implementation should have:

1. `envelope migrate folders --from <source> --to <dest>` for non-mutating folder planning.
2. `envelope migrate run --from <source> --to <dest> --dry-run` with real per-folder copy/skip counts.
3. Source UIDVALIDITY included in the migration key.
4. Idempotency that works even when destination UID is unknown/null.
5. Bounded UID batches; default 25 is good, upper bound 500 is safer than unbounded raw RFC822 fetches.
6. Safe IMAP quoting/literal handling for Message-ID search.
7. Same-account guard by account ID.
8. Same-physical-mailbox guard by effective IMAP username + host + port.
9. Structured JSON events including `message_failed` and aggregate `run_dry_run_done`.
10. Non-zero exit if any append fails.
11. Tests covering INBOX, Archive/other folders, Sent/Sent Items, Junk/Junk E-mail, nested folders, and folders with spaces.
12. Dashboard webhook/redaction tests preserved if dashboard rules are in the same change set.

For non-overlapping source/destination access, use staged backup/restore, not `migrate`:

```bash
envelope backup export --account <source> --out <archive-dir> --include INBOX --include Archive --include 'Sent*' --include 'Junk*'
envelope backup verify --from <archive-dir>
envelope backup restore --account <destination> --from <archive-dir> --dry-run \
  --map 'Junk E-mail=Junk' --map 'Sent Items=Sent'
envelope backup restore --account <destination> --from <archive-dir> \
  --map 'Junk E-mail=Junk' --map 'Sent Items=Sent'
```

Backup archives must be local RFC822 `.eml` files plus a manifest with folder, UIDVALIDITY/UID, flags, INTERNALDATE, Message-ID, byte size, and SHA-256. Restore is append-only and uses a destination-specific NDJSON sidecar for rerun idempotency.

## Delegating hardening to Claude Code

Use Claude Code for substantial migration implementation. Give it a self-contained prompt and prohibit live mailbox writes/install/version bumps.

Good prompt shape:

```text
Fix the Envelope IMAP migration feature safely. Work in /Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo. Do not run real mailbox migrations, read credentials, install the binary, or bump version. Preserve dashboard rules-control work. Implement/verify: UIDVALIDITY idempotency, bounded batches, safe Message-ID quoting, same-account and same-physical-mailbox guards, structured failure events, dry-run aggregate output, and non-zero failure exit. Run cargo fmt --check and cargo test --workspace. Report files changed, tests, remaining risks.
```

Use `claude -p` for one-shot work. If Claude exits in plan mode with “Plan rejected,” resume the same session and explicitly tell it to proceed with the plan.

## Verification checklist

From `u1f4e7-repo`:

```bash
export HOME=/Users/wondermonkey
export PATH=/Users/wondermonkey/.cargo/bin:/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
cargo fmt --check
cargo test -p envelope-email-transport migrate -- --nocapture
cargo test -p envelope-email-transport backup -- --nocapture
cargo test -p envelope-email-transport missing_body -- --nocapture
cargo test -p envelope-email-store migration -- --nocapture
cargo test -p envelope-email-store migrations -- --nocapture
cargo test -p envelope-email migrate -- --nocapture
cargo test -p envelope-email backup -- --nocapture
cargo test --workspace
cargo build --release -p envelope-email
```

Run clippy to separate baseline from new issues:

```bash
cargo clippy --workspace --all-targets -- -D warnings
```

Known baseline on 2026-05-04: clippy fails on unrelated store files (`accounts.rs`, `action_log.rs`, `credential_store.rs`, `drafts.rs`, `snoozed.rs`, `threads.rs`). New migration-touching files should be clean.

## Independent review checklist

Before install, request an independent review or manually inspect for:

- no source `STORE`, `EXPUNGE`, delete, or source flag mutation in the migration path;
- no silent skipping of requested UIDs;
- no whole-folder raw RFC822 Vec in the migration run path;
- destination Message-ID query quotes/escapes untrusted header values;
- dry-run does not create folders, append, or write migration state;
- failures are observable as JSON and as non-zero process exit;
- DB migration is additive/upgrade-safe;
- dashboard webhook URLs/secrets are redacted if dashboard rules code is present.

## Install after passing review

Stop the running dashboard if necessary, then install only the raw binary:

```bash
SRC=/Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo/target/release/envelope
DST=/Users/wondermonkey/.local/libexec/envelope-rust
pgrep -f '/Users/wondermonkey/.local/libexec/envelope-rust serve --port 3141' | xargs kill 2>/dev/null || true
install -m 0755 "$SRC" "$DST"
/Users/wondermonkey/.local/bin/envelope --version
/Users/wondermonkey/.local/bin/envelope --help | grep -A2 -n 'migrate'
```

Restart dashboard with shared HOME:

```bash
export HOME=/Users/wondermonkey/.hermes/shared/envelope-home
export PATH=/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH
exec /Users/wondermonkey/.local/libexec/envelope-rust serve --port 3141
```

Then verify dashboard loads and console is clean.

## Safe smoke tests

Use only help, folder planning, dry-run, and guard checks until Tyler approves a real migration.

```bash
export HOME=/Users/wondermonkey/.hermes/shared/envelope-home
export PATH=/Users/wondermonkey/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH

envelope migrate --help
envelope migrate run --help

envelope --json migrate folders \
  --from editor@spainexpat.com \
  --to partners@spainexpat.com \
  --include INBOX --include Archive --include 'Junk*' --include 'Sent*'

envelope --json migrate run \
  --from editor@spainexpat.com \
  --to partners@spainexpat.com \
  --include Archive \
  --dry-run \
  --batch-size 25

envelope migrate run \
  --from partners@spainexpat.com \
  --to partners@spainexpat.com \
  --dry-run

envelope migrate run \
  --from partners@spainexpat.com \
  --to editor@spainexpat.com \
  --dry-run \
  --batch-size 501
```

Expected guard behavior:

- same account should be rejected;
- too-large batch size should be rejected;
- dry-run should end with `run_dry_run_done` and `would_copy`.

## Known limitation to report honestly

If `APPEND` succeeds but SQLite `record_migration` fails immediately after, and the message has no Message-ID, a rerun can duplicate that one message. Fixing this properly requires UIDPLUS/APPENDUID support or a local append-intent journal. Do not hide this; decide whether it is acceptable for the specific migration.

## Dropbox cleanup after build

After install, clean Cargo artifacts and preserve Dropbox-ignore on `target/`:

```bash
cd /Users/wondermonkey/Dropbox/Code/envelope-email/u1f4e7-repo
cargo clean
mkdir -p target
xattr -w com.dropbox.ignored 1 target
xattr -p com.dropbox.ignored target
```

## Completion report

Report:

- installed version;
- commit hash if committed;
- tests and counts;
- smoke-test source/destination/folders;
- whether any real migration was run;
- remaining limitation around DB-write-after-APPEND;
- dashboard URL if dashboard was restarted.

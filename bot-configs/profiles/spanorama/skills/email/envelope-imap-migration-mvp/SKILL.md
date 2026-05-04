---
name: envelope-imap-migration-mvp
description: Continue or verify the Envelope IMAP migration MVP for copying mail between configured accounts using raw RFC822 fetch and destination append.
---

# Envelope IMAP Migration MVP

Use this when Tyler asks to migrate `editor@spainexpat.com`, archive WorkMail into Migadu using Envelope, or continue the Envelope migration feature.

## Known worktree

- Worktree: `/tmp/envelope-imap-migrate`
- Branch: `feat/imap-migrate`

This worktree reportedly had full `cargo test` passing after implementation.

## Goal

Build migration into Envelope itself instead of relying on `imapsync`, so configured Envelope accounts can be used without exposing plaintext passwords.

## Implemented MVP commands

```bash
envelope migrate folders --from <source-account> --to <dest-account>
envelope migrate run --from <source-account> --to <dest-account> --dry-run
envelope migrate run --from <source-account> --to <dest-account>
```

Supported flags:

- `--from`
- `--to`
- `--include`
- `--exclude`
- `--dry-run`
- global `--json`

## Files touched in MVP

Added:

- `crates/cli/src/commands/migrate.rs`
- `crates/email/src/migrate.rs`
- `crates/store/src/migration.rs`

Modified:

- `crates/cli/src/main.rs`
- `crates/cli/src/commands/mod.rs`
- `crates/email/src/imap.rs`
- `crates/email/src/lib.rs`
- `crates/store/src/db.rs`
- `crates/store/src/lib.rs`
- `crates/store/src/migrations.rs`

## Implemented behavior

- Uses configured Envelope accounts, avoiding command-line plaintext passwords.
- Lists source folders and planned destination folders.
- Supports include/exclude folder globs.
- Dry-run emits folder/message plan without appending.
- Real run connects to source and destination IMAP accounts.
- Creates destination folders if missing.
- Fetches source messages as raw RFC822 using `BODY.PEEK[]`.
- Preserves flags, excluding unsafe/unsettable flags:
  - strips `\\Recent`
  - strips `\\Deleted` by default
- Preserves `INTERNALDATE` on destination append.
- Appends to destination via IMAP `APPEND`.
- Records copied messages in `migration_uid_map`.
- Skips already migrated source UID rows on rerun.
- Checks destination by `Message-ID` before appending.
- No source deletes. No purge feature.

## Tests added / expected

Transport/migration tests:

- glob matching
- include/exclude precedence
- append flag sanitization

CLI tests:

- `migrate run` requires `--from` / `--to`
- `--dry-run` parses correctly

Store tests:

- migration record idempotency
- account-pair isolation
- migrated UID listing
- counts across folders

Migration schema tests:

- `migration_uid_map` table exists
- expected columns exist
- migration remains idempotent

Expected verification commands:

```bash
cd /tmp/envelope-imap-migrate
cargo fmt
cargo test -p envelope-email-transport migrate::tests -- --nocapture
cargo test -p envelope-email migrate::tests -- --nocapture
cargo test -p envelope-email-store migration::tests -- --nocapture
cargo test -p envelope-email-store migrations::tests -- --nocapture
cargo test
```

Reported final full result:

- CLI: 30 passed
- Store: 67 passed
- Transport: 130 passed
- Doctests: passed

## Caveats before using on SpainExpat 39K-message migration

This is not yet imapsync-grade. Harden before running against `editor@spainexpat.com` archive/migration:

- Add batching instead of fetching an entire folder at once.
- Track UIDVALIDITY.
- Add better destination folder mapping, for example:
  - `Junk E-mail` -> `Junk`
  - `Sent Items` -> `Sent`
- Capture append return UID where supported.
- Add resume/status command.
- Emit better per-message failure JSON events.
- Add rate/concurrency controls.
- Add attachment/date spot-check tooling.
- Run a small test mailbox end-to-end smoke before the real migration.

## SpainExpat context

- Envelope `editor@spainexpat.com` is AWS WorkMail source, not Migadu.
- Migadu destination has `editor@spainexpat.com` and `partners@spainexpat.com` IMAP working and initially empty.
- DNS cutover should wait until archive/import is verified.
- Do not send, delete, purge, or change DNS without Tyler approval.

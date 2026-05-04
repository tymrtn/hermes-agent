---
name: envelope-email
description: First-stop Envelope Email skill for Skippy. Load for any Envelope/IMAP/mailbox task, especially after Migadu password resets, account-store updates, inbox/search/read/send/code/watch work, or "test Envelope" requests. Use the local `envelope` CLI directly; do not rediscover Railway, FastAPI, old Python app files, repo docs, or ENVELOPE_API_KEY first.
tags: [envelope, email, cli, imap, smtp, mailbox, credentials, migadu]
triggers: ["envelope", "Envelope", "envelope email", "Envelope Email", "update Envelope", "Envelope store", "test IMAP", "Migadu password", "mailbox password", "inbox", "verification code", "OTP"]
---

# Envelope Email — Skippy first stop

## Correct operating sequence

When Tyler asks for Envelope work, do this first:

```bash
command -v envelope
envelope --version
envelope accounts list --json
```

Then run the relevant Envelope command directly:

```bash
envelope inbox --account <email> --limit 10 --json
envelope folders --account <email> --json
envelope search --account <email> 'FROM example.com' --json
envelope read --account <email> <uid> --json
envelope code --account <email> --wait 60 --json
envelope rule list --account <email> --json
envelope rule test --account <email> --folder INBOX <uid> --json
envelope rule run --account <email> --folder INBOX --limit 100 --json
```

Do **not** start with filesystem/codebase discovery such as searching for `envelope`, `FastAPI`, `Railway`, `ENVELOPE_API_KEY`, or account tables. That is stale architecture archaeology. Envelope is operated here as a local Rust CLI.

## Rust CLI / computer-wide singleton rule

Use plain `envelope` by default, but verify what it resolves to. Envelope is a Rust CLI; there should be no Python Envelope implementation.

On wondermonkey, there should be exactly one active public `envelope` command path:

```text
/Users/wondermonkey/.local/bin/envelope
```

That shim sets the shared HOME and execs the raw Rust binary:

```text
HOME=/Users/wondermonkey/.hermes/shared/envelope-home
store=/Users/wondermonkey/.hermes/shared/envelope-home/Library/Application Support/envelope-email/
raw binary=/Users/wondermonkey/.local/libexec/envelope-rust
```

Do **not** recreate `/Users/wondermonkey/bin/envelope`, Hermes venv `bin/envelope`, or profile-local `bin/envelope` compatibility links/wrappers. If an agent cannot find Envelope, fix that agent's PATH so `/Users/wondermonkey/.local/bin` is visible. Profile/user `Library/Application Support/envelope-email` paths should point at the shared store; do not create or repair per-profile stores unless Tyler explicitly asks for isolation.

## After an upstream mailbox password reset

Current installed builds may not have `accounts update`, so the safe pattern is remove/re-add, then verify with a real mailbox operation:

```bash
envelope accounts list --json
envelope accounts remove <email> --json
envelope accounts add --email <email> --password <new-password> \
  --imap-host <host> --imap-port <port> \
  --smtp-host <host> --smtp-port <port> --json
envelope inbox --account <email> --limit 1 --json
envelope folders --account <email> --json
```

Never paste passwords back into chat. Use local files or stdin-safe handling when needed.

## Related skills

Also load when relevant:

- `envelope-cli-gotchas` — search/folder/account quirks.
- `envelope-junk-rules` — junk/spam cleanup, rule listing, rule creation, `rule test`, and `rule run`.
- `envelope-credential-recovery` — AEAD, corrupted credential store, or mailbox password reset recovery.

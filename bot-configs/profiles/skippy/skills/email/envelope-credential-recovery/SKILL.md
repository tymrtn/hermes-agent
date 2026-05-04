---
name: envelope-credential-recovery
description: Diagnose and recover from envelope-email `aead::Error`, credential-store, mailbox-password, and account update failures. Load when Tyler says to update Envelope after a Migadu/mailbox password reset, test IMAP, or fix "failed to decrypt credentials".
tags: [envelope, email, recovery, credentials, aead, password, migadu, imap]
triggers: ["aead::Error", "failed to decrypt credentials", "envelope inbox fails", "envelope search error", "envelope all accounts broken", "update Envelope", "Envelope store", "Migadu password", "mailbox password", "test IMAP"]
---

# Envelope credential recovery

## If Tyler just reset a mailbox password upstream

Do not search for the Envelope repo, Railway app, FastAPI account tables, or `ENVELOPE_API_KEY`. Use the local Envelope CLI/store directly.

Minimum flow:

```bash
envelope accounts list --json
# if account exists and there is no accounts update subcommand on this build:
envelope accounts remove <email> --json
envelope accounts add --email <email> --password <new-password> \
  --imap-host <host> --imap-port <port> \
  --smtp-host <host> --smtp-port <port> --json
envelope inbox --account <email> --limit 1 --json
envelope folders --account <email> --json
```

Use plain `envelope`, not `/Users/wondermonkey/.local/bin/envelope`, unless you intentionally want to bypass the shared Hermes wrapper. Plain `envelope` should use `HOME=/Users/wondermonkey/.hermes/shared/envelope-home` through the wrapper and therefore operate on the shared agent mail store.


When every `envelope` subcommand that touches an account (inbox, search, folders, send) errors with:

```
Error: failed to decrypt credentials: decryption error: aead::Error
```

…the encrypted password blobs in the SQLite DB can no longer be opened with the master key on disk. Don't guess. Follow this procedure.

## 1. Confirm it's a key/DB mismatch, not an install problem

```bash
envelope --version
gh release list --repo tymrtn/envelope-email --limit 3

ls -la ~/Library/Application\ Support/envelope-email/
stat -f "%Sm %N" ~/Library/Application\ Support/envelope-email/credentials.json
stat -f "%Sm %N" ~/Library/Application\ Support/envelope-email/envelope.db
```

Expect two files:

- `credentials.json` — ~180 bytes, holds the master key
- `envelope.db` — big SQLite file, holds AES-GCM encrypted account passwords

**Diagnostic signal:** if `credentials.json` mtime is NEWER than `envelope.db` mtime, the master key was regenerated after the DB was last written. Stored passwords are now orphaned. That is the root cause.

Reinstalling envelope does NOT fix this. A new binary with the same broken key/DB pair will fail identically.

## 2. Don't be fooled by version-bump theories

Before assuming there's a newer version that fixes this:

```bash
envelope --version
gh release list --repo tymrtn/envelope-email --limit 3
brew info tymrtn/envelope/u1f4e7   # brew tap often lags the real release
```

If the installed version already matches the latest GitHub release, the fix is data, not code.

## 3. credentials.json is NOT in Dropbox

`~/Library/Application Support/envelope-email/` is outside `~/Dropbox/`. Dropbox version history has nothing. Do not waste time with the Dropbox web UI — the file was never synced.

Valid backup sources, in order of preference:

1. Another Mac the user syncs with (prior machine still reachable on the user's private network)
2. Time Machine on the same Mac (if enabled)
3. macOS local APFS snapshots (`tmutil listlocalsnapshots /` — can hang on machines with many snapshots; run with a timeout)

## 4. Recover by copying both files from a good machine

The two files are a pair. **Always move `credentials.json` and `envelope.db` together** from the same pre-breakage point in time. Mixing a new key with an old DB (or vice versa) reproduces the same `aead::Error`.

```bash
# Back up the current broken state first — ALWAYS
cp ~/Library/Application\ Support/envelope-email/credentials.json \
   ~/Library/Application\ Support/envelope-email/credentials.json.broken-$(date +%Y%m%d)
cp ~/Library/Application\ Support/envelope-email/envelope.db \
   ~/Library/Application\ Support/envelope-email/envelope.db.broken-$(date +%Y%m%d)

# Replace both files with the matched pair from the good machine.
# Use whatever transport the user already trusts between those machines.
# Verify the restored files have matching pre-breakage mtimes before proceeding.

envelope accounts list
envelope inbox --account <any-account> --limit 1
```

If no transport between the two machines is already set up, pause and ask the user how they want to move the files — do not wire up new inter-machine access implicitly.

### Verification nuance: `accounts list` is not enough

In live recovery, `envelope accounts list` can still succeed while any command that actually needs the stored password (`inbox`, `search`, `send`, folder sync) fails with:

```bash
Error: credential store error: decryption error: aead::Error
```

So after restoring a candidate pair, **do not treat a healthy `accounts list` as proof of recovery**. It only proves the DB metadata is readable. The decisive test is a real mailbox operation such as:

```bash
envelope inbox --account <known-account> --limit 1
```

If `accounts list` works but `inbox` still throws `aead::Error`, the restore did not actually fix the credential store.

## 5. Nuclear option: re-add all accounts

Only if recovery from another machine is impossible.

### 5a. First classify which accounts are actually recoverable from infra

Before assuming you need every password by hand, snapshot the Envelope inventory and split it by backend:

- `imap.migadu.com` / `smtp.migadu.com` → Migadu-hosted, password can often be reset via Migadu API
- Gmail / Google Workspace → app password or OAuth path, not covered by Migadu
- WorkMail / Exchange / other custom hosts → separate recovery path, not covered by Migadu

This prevents wasting time treating the whole account set as equally manual.

### 5b. Migadu-backed fallback when passwords are missing

If the broken Envelope set is mostly Migadu-hosted and you have Migadu admin API access, a practical recovery path is:

1. Inventory the currently configured Envelope accounts with `envelope accounts list --json`
2. Confirm which of those addresses still exist in Migadu by listing `/v1/domains/<domain>/mailboxes`
3. Reset passwords only for the confirmed Migadu mailboxes you actually need first
4. Write the fresh credentials to a local file with restrictive permissions (`chmod 600`)
   - Put it under `~/Library/Application Support/envelope-email/`
   - Use a descriptive, date-stamped name like `migadu-recovery-credentials-YYYY-MM-DD.json`
   - Include `purpose`, `generated_at`, and an `accounts` object keyed by email, with IMAP/SMTP host/port plus the generated password
5. Remove/re-add those accounts in Envelope using the new passwords
6. Verify with a real mailbox op like `envelope inbox --account <email> --limit 1 --json`

If Tyler later asks where the reset credentials went, first check this local app-data directory and session history before saying you do not know. In the 2026-04-23 Neo recovery, the priority-account file was:

```text
/Users/wondermonkey/Library/Application Support/envelope-email/migadu-recovery-credentials-2026-04-23.json
```

Do not paste live mailbox passwords into chat by default. If Tyler explicitly asks for the JSON over Telegram, attach the file with `MEDIA:/absolute/path` rather than merely citing the path.

Critical caveat: resetting a Migadu mailbox password changes the real mailbox password everywhere, not just in Envelope. Expect Apple Mail, phone clients, scripts, and any other IMAP/SMTP clients using that mailbox to need the new password too.

## 5. Nuclear option: rebuild on a fresh local Envelope store

Only if recovery from another machine is impossible. Requires every account's password / app-password up front — do not start without them.

**Important live finding:** when the local credential store itself is corrupted, `envelope accounts remove <email>` may fail with SQLite foreign-key errors, and `envelope accounts add ...` can also fail immediately with:

```bash
Error: failed to access credential store for encryption: decryption error: aead::Error
```

That means you cannot trust an in-place per-account repair on the existing store, even after resetting mailbox passwords upstream. In that state, the practical recovery is to back up the broken local store and force Envelope to create a brand-new one.

```bash
# Snapshot the current broken store first
base="$HOME/Library/Application Support/envelope-email"
ts=$(date +%Y%m%d-%H%M%S)
cp "$base/credentials.json" "$base/credentials.json.before-clean-reset-$ts"
cp "$base/envelope.db" "$base/envelope.db.before-clean-reset-$ts"

# Move the broken store out of the way entirely
mv "$base/credentials.json" "$base/credentials.json.corrupt-reset-$ts"
mv "$base/envelope.db" "$base/envelope.db.corrupt-reset-$ts"

# Then re-add accounts into the fresh store Envelope creates
# (first command touching accounts will recreate the store)
envelope accounts list --json

envelope accounts add --email <email> --password <app-password> \
  --imap-host <imap-host> --imap-port <imap-port> \
  --smtp-host <smtp-host> --smtp-port <smtp-port> --json
```

After each add, verify with a real mailbox operation:

```bash
envelope inbox --account <email> --limit 1 --json
```

If that succeeds, the new store is healthy.

Tyler's setup has ~15 accounts. Real time cost. Avoid unless forced, but if you do it, prefer a **clean-store rebuild** over trying to surgically repair a poisoned store in place.

## 6. Prevention

- Whenever envelope prompts about rotating or regenerating the master key, STOP and copy `envelope.db` + `credentials.json` to a safe offline location first.
- Consider a regular job that copies the pair into a backup location (ideally inside Dropbox or Time Machine coverage) so version history can restore them.

## Common pitfalls seen in the wild

- **"Just update to 0.5.0"** — that version may not exist. `gh release list` before believing a version rumor.
- **`brew info` shows old version** — tap can lag GitHub releases by days. `gh release list` is the source of truth.
- **Dropbox placeholder files** on a new Mac read as 0 bytes. Touching them triggers materialization but can hang the shell on large files. Use `open <file>` to force download via Finder without blocking the terminal.
- **`tmutil listlocalsnapshots`** can hang 30s+. Always run with a timeout / in background if used from an agent.
- **Unpaired file restore** — restoring only `credentials.json` or only `envelope.db` from a backup reproduces the same error. Always restore both from the same point in time.

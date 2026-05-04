---
name: envelope-cli-gotchas
description: "Envelope CLI operating guide for Skippy. Load for ANY Envelope work: inbox/search/read/send, account/password/store updates, IMAP tests, OTP/code checks, dashboard/serve, or questions about where Envelope state lives. Use the `envelope` CLI directly; do not rediscover the repo, Railway, FastAPI, or API keys first."
tags: [envelope, email, imap, cli, account, credentials, password]
triggers: ["envelope", "Envelope", "envelope search", "envelope inbox", "envelope account", "Envelope store", "update Envelope", "test IMAP", "Migadu password", "mailbox password", "verification code", "OTP", "no messages matching", "imap", "gmail all mail"]
---

# Envelope CLI Gotchas

## First principle: run the CLI, do not rediscover the product

For normal Envelope work on wondermonkey/Hermes, the sequence is:

1. Load this skill.
2. Run `envelope accounts list --json` to see the live configured store.
3. Run the concrete Envelope CLI command needed (`inbox`, `folders`, `search`, `read`, `code`, `watch`, `accounts add/remove`, etc.).
4. Verify with a real mailbox operation.

Do **not** start by searching the filesystem for `envelope`, `FastAPI`, `ENVELOPE_API_KEY`, Railway config, old Python app files, or repo docs. That is stale discovery behavior and annoys Tyler when the CLI should answer directly. Envelope is operated here as a local Rust CLI with a computer-wide singleton store at `/Users/wondermonkey/.hermes/shared/envelope-home/Library/Application Support/envelope-email/`.

Before claiming Envelope is unavailable, run:

```bash
type -a envelope
command -v envelope
envelope --version
envelope accounts list --json
```

For paste-safe password rotation commands on wondermonkey, prefer an explicit binary path instead of bare `envelope` when PATH shadowing is observed:

```bash
ENV="/Users/wondermonkey/bin/envelope"
"$ENV" accounts list --json
```

Do **not** call anything in a Python venv a Python Envelope. There should be no Python implementation of `envelope` installed. If `command -v envelope` resolves under `/Users/wondermonkey/.hermes/hermes-agent/venv/bin/envelope`, treat that as a legacy wrapper/PATH-placement problem and inspect it before drawing conclusions. The intended implementation is the Rust CLI.

On this machine, every public Envelope entrypoint should be a wrapper to the singleton store:

```text
/Users/wondermonkey/.local/bin/envelope
/Users/wondermonkey/bin/envelope
/Users/wondermonkey/.hermes/hermes-agent/venv/bin/envelope
```

These wrappers set `HOME=/Users/wondermonkey/.hermes/shared/envelope-home` and exec the raw Rust binary at `/Users/wondermonkey/.local/libexec/envelope-rust`. The shared store is:

```text
/Users/wondermonkey/.hermes/shared/envelope-home/Library/Application Support/envelope-email/
```

If `type -a envelope` shows a venv path, that is acceptable as long as the wrapper routes to the shared HOME. Do not create profile-local stores; profile/user app-data paths should symlink to the shared store.

If Tyler says “reinstall from gh original,” he means reinstall the Rust CLI from the original GitHub repo (`tymrtn/U1F4E7` / Envelope), not `pip install` into the Hermes Python venv. Never reinstall Envelope account tooling with `python -m pip install ...` unless the task is explicitly about the Python server package.


## Search syntax: IMAP, not a flag

`envelope search` takes a raw IMAP search expression as its positional argument. There is no `--query` flag.

Wrong:
```
envelope search --account ty@tmrtn.com --query "movistar"
```

Right:
```
envelope search --account ty@tmrtn.com 'FROM movistar'
envelope search --account ty@tmrtn.com 'SUBJECT factura'
envelope search --account ty@tmrtn.com 'BODY movistar'
envelope search --account ty@tmrtn.com 'FROM "@movistar.es"'
envelope search --account ty@tmrtn.com 'SINCE 1-Apr-2026 FROM movistar'
```

A bare word with no key only matches a subset of headers on some servers. Always be explicit: FROM, SUBJECT, BODY, or SINCE.

## Folder paths differ per account

INBOX is the default. Gmail-backed accounts keep most mail under `[Gmail]/All Mail`. Non-Gmail providers do not have that folder and will error on it.

```
# First time touching an account, check what folders exist
envelope folders --account <acct>

# Gmail accounts: use [Gmail]/All Mail for full archive
envelope search --account ty@tmrtn.com --folder '[Gmail]/All Mail' 'FROM movistar'

# Non-Gmail: INBOX, Sent, Archive, maybe Trash
envelope search --account tyler@copyright.sh 'FROM laurent'
```

The subcommand is `folders` (plural). `folder list` does not exist.

## When a search returns zero but you know it exists

Checklist in order:

1. Wrong account.
2. Wrong folder (missing `--folder '[Gmail]/All Mail'` on a Gmail account).
3. Wrong IMAP key (FROM matches the From header only, BODY matches text, SUBJECT matches the subject line).
4. Literal `@` in From: wrap in quotes, e.g. `'FROM "@movistar.es"'`.

## Account enumeration pattern

When the account is unspecified, sweep them in parallel:

```
for acct in tyler@aposema.com tmartin@aposema.com ty@tmrtn.com \
            tyler@expatriator.com tyler@copyright.sh tyler@u1f99e.com \
            editor@spainexpat.com admin@clef.pro; do
  echo "=== $acct ==="
  envelope search --account "$acct" --limit 5 'FROM <target>' 2>&1 | grep -v "No messages"
done
```

Full account list:
```
envelope accounts list
```

## Gmail vs non-Gmail routing

- Gmail backed (use `[Gmail]/All Mail` for full archive): `ty@tmrtn.com`.
- Non-Gmail (INBOX is enough): `tyler@aposema.com`, `tmartin@aposema.com`, `skippy@aposema.com`, `tyler@u1f99e.com`, `admin@clef.pro`, `editor@spainexpat.com`, etc.
- When unsure, run `envelope folders --account <acct>` first. One second, saves ten.

## Attachment handling

Spanish PDFs with non-ASCII filenames need to be downloaded by UID, then OCR'd if the PDF is image-only (common from small business senders):

```
envelope attachment list --account <acct> <UID>
envelope attachment download --account <acct> <UID> "<exact filename>" --output /tmp/safe.pdf

# Try text extraction first
pdftotext -layout /tmp/safe.pdf -

# If empty, OCR it
pdftoppm -r 200 /tmp/safe.pdf /tmp/out -png
tesseract /tmp/out-1.png /tmp/out_ocr  # add -l spa for Spanish
cat /tmp/out_ocr.txt
```

## Inbox view shows recent only

`envelope inbox --limit 30` shows the most recent N messages only. For anything older than today, use `envelope search` with an explicit folder, not `inbox`.

## Credential decrypt failures (aead::Error)

Symptom: every command returns `Error: failed to decrypt credentials: decryption error: aead::Error`. Affects all accounts, both `--credential-store file` and `--credential-store keychain`.

Root cause: master key in `~/Library/Application Support/envelope-email/credentials.json` no longer matches the AES-GCM blobs in `envelope.db`. Cipher mismatch.

Important: resetting the upstream mailbox password in Migadu does **not** repair Envelope. Envelope keeps its own encrypted credential copy. If you rotate Migadu first while Envelope is corrupt, direct IMAP/SMTP can pass while Envelope still cannot update/test. For password rotations, stop and repair/replace the Envelope credential path before declaring the reset complete.

Diagnostic — compare mtimes:

```bash
ls -la ~/Library/Application\ Support/envelope-email/
# If credentials.json mtime is LATER than envelope.db mtime,
# the master key was regenerated without re-encrypting the DB.
# That's the corruption.
```

Recovery options (cleanest first):

1. **Restore old `credentials.json` from Time Machine / Dropbox version history.** Pick a version timestamped before the mtime split. DB stays put. No re-add.
2. **Nuke + re-add all accounts.** `envelope accounts remove <email>` + `envelope accounts add --email ... --password <app-password>` for each. Requires all app passwords on hand.

Do NOT delete `envelope.db` — the message store, threads, tags, scores, and rules all live there. Only the encrypted password column is unrecoverable; everything else is intact.

The `envelope serve` daemon may still be running on port 3141 with stale credentials cached in memory. Kill it before testing the fix:

```bash
pkill -f "envelope serve"
```

## Production Envelope API fallback

Only use this after trying the CLI directly or when Tyler explicitly refers to the dashboard/production backend.

Railway production project/service known-good API base:

```text
https://envelope-api-production.up.railway.app
```

The custom domain `https://envelope.aposema.com` has previously failed TLS hostname verification even though Railway variables advertise it. Do not bypass TLS casually. Prefer the generated Railway domain, then call `/health` and `/accounts` with the Railway `ENVELOPE_API_KEY` from variables, redacting secrets in all outputs.

Caveat: production API account update models may not include password fields. If password rotation is needed for an existing account, verify available endpoints first; replacement account + dependent config update may be safer than assuming PUT/PATCH can rotate the encrypted credential.

## Version and store verification

When in doubt about which Envelope is installed and where:

```bash
type -a envelope                  # confirm active binary/wrapper routing
command -v envelope
envelope --version                # actual installed version
gh release list --repo tymrtn/U1F4E7 --limit 5  # latest available
brew info tymrtn/envelope/u1f4e7  # if installed via tap
```

The desired `envelope` implementation is the installed Rust CLI, normally:

```bash
/Users/wondermonkey/.local/bin/envelope
```

Envelope stores local state under the active HOME:

```bash
$HOME/Library/Application Support/envelope-email/
```

If wrappers exist in `~/bin`, profile-local `bin`, or the Hermes Python venv, treat them as legacy compatibility shims until Tyler explicitly chooses a wrapper-based topology. Do not call them Python Envelope, do not prefer the venv location, and do not treat alternate HOME stores as canonical without verifying the account list and Tyler's intended runtime.


Install on a fresh machine:

```bash
brew tap tymrtn/envelope
brew install tymrtn/envelope/u1f4e7
```

The brew formula may lag the GitHub release by a day or two. If you need bleeding-edge, build from source: `cd ~/Dropbox/Code/envelope-email/envelope-email-rs && cargo build --release && cp target/release/envelope ~/.local/bin/envelope`.

## Dropbox placeholder trap on new Macs

The envelope-email repo path (`~/Dropbox/Code/envelope-email/envelope-email-rs/`) on a fresh Mac is full of 0-byte Dropbox smart-sync placeholders. `find`, `ls`, and `cat` will all "succeed" but return nothing useful — Cargo.toml is empty, README is empty, source files are empty.

Detect:

```bash
ls -la <file>            # 0 bytes + @ extended attrs = placeholder
xattr -l <file>          # look for com.dropbox.placeholder
find <repo> -size 0 | wc -l   # count placeholders
```

Materialize a single file by opening it (triggers macOS File Provider download):

```bash
open ~/Dropbox/Code/envelope-email/envelope-email-rs/README.md
sleep 2
cat ~/Dropbox/Code/envelope-email/envelope-email-rs/README.md
```

For the whole repo, use Finder → right-click folder → "Make Available Offline", or open files individually in a script. Don't waste cycles `grep`ing across placeholder forests — confirm the file is materialized first.

## `envelope serve` fails on missing workspace cwd

Hermes backgrounds commands under a pinned workspace cwd. If that path is stale (e.g., `/Users/tylermartin/.hermes/profiles/skippy/workspace` on a machine whose user is `wondermonkey`), `envelope serve` — or any background command — fails at spawn time with:

```
Failed to start background process: [Errno 2] No such file or directory: '/Users/<other>/.hermes/profiles/skippy/workspace'
```

Workaround: run from an explicit cwd the process can see, e.g.:

```bash
cd ~ && (envelope serve --port 3141 > /tmp/envelope-serve.log 2>&1 &)
```

Or launch via `mcp_terminal` with `workdir=/tmp` or any real path. Fix the harness workspace config separately; don't let it block the dashboard when Tyler needs to reset credentials in a hurry.

## Sending reference

Identity routing and CC conventions live in the main agent instructions. Short form:

- Project outbound as Skippy: `skippy@<project>.com`, CC the project owner on the project domain.
- Non-project outbound as Tyler: `ty@tmrtn.com`.
- Spanish transactional admin (autoescuela, Movistar, Digi, DGT, Hacienda): `ty@tmrtn.com`. These are not cold outreach — do NOT load `cold-email-anti-ai`.

```
envelope send --account <from> --to <to> [--cc <cc>] \
  --subject "..." \
  --body "$(cat /tmp/body.txt)"
```

For bodies with multiple paragraphs, UTF-8, or quotes, put the body in `/tmp/body.txt` via a file tool first, then cat into `--body`. Inline multi-line shell strings break escaping quickly.

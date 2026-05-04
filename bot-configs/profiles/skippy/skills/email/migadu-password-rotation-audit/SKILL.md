---
name: migadu-password-rotation-audit
description: Preflight and execute Migadu mailbox password rotations without breaking Envelope, Railway, or agent-local mail consumers.
tags: [email, migadu, envelope, railway, credentials, password-rotation]
---

# Migadu Password Rotation Audit

Use this before resetting any Migadu mailbox password for Tyler. Migadu password resets are global and can break live Railway services, local Envelope stores, and agent-specific mail clients.

## Core rule

Do **not** reset first and clean up later. First map every consumer, then rotate and update all dependents in one controlled pass.

## Preflight checklist

1. Confirm the mailbox exists and is enabled in Migadu:
   - `GET https://api.migadu.com/v1/domains/<domain>/mailboxes`
   - Check `may_access_imap`, `may_send`, and `may_receive`.
2. Check the active Envelope store directly before searching elsewhere:
   - `envelope accounts list --json`
   - Use the active wrapper first; Tyler expects direct Envelope CLI action, not broad filesystem archaeology.
   - If the CLI returns `aead::Error`, pause the rotation/update plan. Resetting Migadu will not fix Envelope's encrypted credential copy; repair/replace the Envelope credential entry or identify the production API path before claiming success.
   - Default state path: `~/Library/Application Support/envelope-email/`.
3. Check agent/profile-local Envelope stores:
   - Search under `~/.hermes/profiles/*/` for `bin/envelope`, `envelope-home`, `home/Library/Application Support/envelope-email`, `envelope.db`, and `credentials.json`.
   - Wrapper scripts may force a separate `HOME`; inspect them before assuming the default store.
4. Inspect local Envelope DBs directly when CLI calls would be slow or risky:
   - Search for `envelope.db`.
   - Query `accounts(username, imap_host, smtp_host)` with SQLite.
5. Check Railway consumers before rotating:
   - `railway project list --json`
   - For likely projects, link by project/environment/service IDs in a temp dir.
   - Inspect variables for names like `SMTP_USER`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_HOST`, `SMTP_FROM`, `MAILGUN_*`, `ENVELOPE_*`, `DATABASE_URL`.
   - Redact values in reports; raw secret values should not be pasted into chat.
6. Build an update plan: new password file, Migadu reset, local Envelope updates, Railway variable updates, redeploy/restart if needed, then verification.

## Known dependency examples from 2026-04-24

- `hello@loftly.com` exists on Migadu and is used in Spanorama's isolated Envelope store.
- Historical Spanorama wrapper: `~/.hermes/profiles/spanorama/bin/envelope` once set `HOME=/Users/wondermonkey/.hermes/profiles/spanorama/envelope-home`.
- Current observation from 2026-04-27: `~/.hermes/profiles/spanorama/bin/envelope` sets `HOME=/Users/wondermonkey/.hermes/shared/envelope-home`, while the older isolated store still exists at `~/.hermes/profiles/spanorama/envelope-home/Library/Application Support/envelope-email/envelope.db`. Always inspect the wrapper before assuming which store is live.
- Spanorama/shared Envelope accounts included:
  - `spainexpat@gmail.com`
  - `tyler@expatriator.com`
  - `hello@loftly.com`
  - `editor@spainexpat.com`
- Railway project `loftly-pitch-deck` had `SMTP_USER=hello@loftly.com`, `SMTP_PASS`, and legacy `MAILGUN_*` variables. Resetting `hello@loftly.com` without updating Railway would likely break outbound mail.
- Railway project `envelope-email` service `envelope-api` exists and had `ENVELOPE_API_KEY`, `ENVELOPE_DB_PATH`, and `ENVELOPE_SECRET_KEY`.
- Railway services for `admin.copyright.sh`, `platform.copyright.sh`, and `api.copyright.sh` had Migadu SMTP variables including `SMTP_HOST=mail.migadu.com`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM`. Treat copyright.sh mailbox resets as production-impacting until exact usernames are mapped.

## Safe rotation sequence

1. Generate strong passwords and write a local JSON credential file under a protected path.
2. `chmod 600` the file and verify permissions.
3. Reset passwords in Migadu with form-urlencoded `PUT`, not JSON. For copy/paste shell use `curl -fsS -u "$MIGADU_USER:$MIGADU_API_KEY" -X PUT "https://api.migadu.com/v1/domains/$DOMAIN/mailboxes/$LOCAL" -H "Content-Type: application/x-www-form-urlencoded" --data-urlencode "password_method=password" --data-urlencode "password=$PASS"`; do not rediscover this via docs unless the skill appears stale.
4. Update every identified local Envelope store.
5. Update every identified Railway `SMTP_PASSWORD` / related variable for the matching mailbox.
6. Redeploy or restart services that read env vars at boot.
7. Verify:
   - Migadu direct IMAP/SMTP auth where appropriate.
   - `envelope inbox --account <email> --limit 1 --json` for local stores.
   - Railway service health/logs or a real outbound mail smoke test.

## Pitfalls

- Migadu cannot reveal existing passwords. If the current password is unknown, the only path is reset.
- Local `envelope accounts list --json` only shows the active store for the current `HOME`; profile wrappers can hide other stores.
- `ENVELOPE_DB_PATH` alone is not reliable isolation for the Rust CLI on macOS; separate `HOME` is the observed isolation mechanism.
- A local `envelope accounts list --json` can disagree with the same store's SQLite DB if the CLI blocks, uses a different store, or cached state is odd. When credentials are being rotated, inspect both CLI-visible accounts and known DB paths such as `~/Library/Application Support/envelope-email/envelope.db`, `~/.hermes/profiles/*/envelope-home/Library/Application Support/envelope-email/envelope.db`, and `~/.hermes/shared/envelope-home/Library/Application Support/envelope-email/envelope.db`.
- Production Envelope API currently exposes create/list/delete/verify, but not password update. To rotate a production Envelope account without DB shell access: create a replacement account with the new credentials, preserve host/ports/policy metadata, then update every dependent `ENVELOPE_ACCOUNT_ID` Railway variable from the old account ID to the new ID. Do not delete the old account until consumers are verified, or you may orphan audit rows/messages.
- Railway variables can still include old `MAILGUN_*` alongside active SMTP variables. Do not infer the active sender without checking code/config or logs.
- Direct Railway SMTP consumers need exact mailbox mapping, not just domain matching. Example found in 2026-04 rotation: copyright.sh services used `SMTP_USERNAME=postmaster@copyright.sh`, but Migadu showed `postmaster@copyright.sh` was an alias to `admin@copyright.sh`; fix by switching auth username to `admin@copyright.sh` and using the rotated admin mailbox password.
- After setting Railway variables, service restarts in non-interactive Hermes require `railway service restart --yes`; without `--yes` the CLI exits with `Cannot prompt for confirmation in non-interactive mode`.
- Avoid piping raw Railway JSON containing secrets into chat or final responses. Redact values unless Tyler explicitly asks for the credential file via attachment.

---
name: envelope-mailbox-backup
description: Back up and triage mailboxes with Envelope, with a direct IMAP fallback when Envelope credential storage is broken. Use for mailbox migration prep, inbox cleanup, or pre-DNS-cutover backups.
version: 1.0.0
author: Spanorama
license: MIT
metadata:
  hermes:
    tags: [email, envelope, imap, backup, migration, inbox-triage]
---

# Envelope Mailbox Backup + Migration Prep

Use this when Tyler asks to clean, triage, or back up an inbox before migration/cutover.

## Rules

- Use the profile-local Envelope wrapper on PATH: `envelope` from `~/.hermes/profiles/spanorama/bin/`.
- Do **not** send email without explicit approval.
- Do **not** delete or move messages during backup unless explicitly asked.
- Do **not** modify DNS or mailbox routing.
- Never print passwords, API keys, app passwords, or credential file contents. Redact as `[REDACTED]`.
- For SpainExpat, remember DNS may still point old mail at AWS/WorkMail while new Migadu mailboxes exist separately. Backing up Migadu alone may only prove the destination mailbox is empty.

## Primary Envelope Workflow

1. Confirm configured accounts:

```bash
envelope accounts list --json
```

2. If the target account appears, list folders:

```bash
envelope folders --account target@example.com --json
```

3. Create timestamped backup root:

```bash
mkdir -p ~/.hermes/profiles/spanorama/mail-backups/<account-slug>/<YYYYMMDDTHHMMSSZ>/
```

4. For each folder:
   - list messages with JSON where supported;
   - read/export each message;
   - preserve raw `.eml` if Envelope exposes raw export;
   - otherwise preserve JSON/read output and metadata.

5. Write a `manifest.json` containing:
   - account
   - source host/account
   - timestamp
   - folders
   - source counts
   - saved counts
   - per-message UID/file/hash/subject/from/date/message-id where available

6. Verify:
   - folder count matches source listing;
   - saved message count matches source count where possible;
   - `.eml` file count matches saved count when raw backup is used.

## Envelope Credential-Store Failure

If Envelope returns:

```text
credential store error: decryption error: aead::Error
```

then do **not** keep trying Envelope blindly. Treat it as a real blocker for that account/config store.

Check keychain only as a diagnostic:

```bash
envelope --credential-store keychain accounts list --json
```

If keychain fails with no default keychain, report it directly.

## Direct IMAP Fallback

Use direct IMAP only when credentials are already available from an authorized local secret source and can be used without printing them. This is useful to verify a new destination mailbox or back up a source mailbox when Envelope is broken.

Python pattern:

```python
import imaplib, email, hashlib, json, re
from pathlib import Path
from datetime import datetime, timezone

account = 'target@example.com'
host = 'imap.example.com'
password = load_password_without_printing()
root = Path('~/.hermes/profiles/spanorama/mail-backups').expanduser()
ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out = root / account.replace('@','-').replace('.','-') / ts
rawdir = out / 'raw'
rawdir.mkdir(parents=True, exist_ok=True)

summary = {'account': account, 'source': host, 'timestamp_utc': ts, 'folders': [], 'total_messages': 0}
M = imaplib.IMAP4_SSL(host, 993, timeout=30)
M.login(account, password)
typ, boxes = M.list()
for b in boxes or []:
    line = b.decode(errors='replace')
    parts = line.split(' "/" ', 1)
    name = parts[1].strip().strip('"') if len(parts) > 1 else line.rsplit(' ', 1)[-1].strip('"')
    typ, data = M.select(f'"{name}"', readonly=True)
    if typ != 'OK':
        summary['folders'].append({'folder': name, 'select': 'failed', 'count': None})
        continue
    count = int(data[0] or 0)
    fslug = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_') or 'folder'
    fdir = rawdir / fslug
    fdir.mkdir(exist_ok=True)
    info = {'folder': name, 'count': count, 'saved': 0, 'messages': []}
    typ, ids = M.search(None, 'ALL')
    for num in (ids[0].split() if typ == 'OK' and ids else []):
        typ, msgdata = M.fetch(num, '(RFC822 UID FLAGS INTERNALDATE)')
        if typ != 'OK' or not msgdata:
            continue
        raw = None
        meta = ''
        for item in msgdata:
            if isinstance(item, tuple):
                meta = item[0].decode(errors='replace') if isinstance(item[0], bytes) else str(item[0])
                raw = item[1]
        if raw is None:
            continue
        h = hashlib.sha256(raw).hexdigest()
        uid_match = re.search(r'UID (\d+)', meta)
        uid = uid_match.group(1) if uid_match else num.decode()
        path = fdir / f'{uid}-{h[:12]}.eml'
        path.write_bytes(raw)
        msg = email.message_from_bytes(raw)
        info['saved'] += 1
        info['messages'].append({
            'uid': uid,
            'sha256': h,
            'file': str(path),
            'date': msg.get('Date'),
            'from': msg.get('From'),
            'to': msg.get('To'),
            'subject': msg.get('Subject'),
            'message_id': msg.get('Message-ID'),
        })
    summary['folders'].append(info)
    summary['total_messages'] += info['saved']
M.logout()
(out / 'manifest.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))
```

Then verify:

```bash
python3 - <<'PY'
from pathlib import Path
import json
p = Path('/path/to/manifest.json')
d = json.loads(p.read_text())
print('MANIFEST_OK', p.exists(), 'folders', len(d['folders']), 'total', d['total_messages'])
print('EML_FILES', len(list((p.parent/'raw').rglob('*.eml'))))
PY
```

## Migration-Specific Pitfalls

- Destination mailbox backup may be empty and still successful. Say so clearly: “Migadu destination backed up; old/source inbox still inaccessible.”
- If old/source mail lives on AWS WorkMail/SES, destination Migadu credentials will not access old mail.
- For SpainExpat specifically, the Envelope-visible `editor@spainexpat.com` account can be the AWS WorkMail source even while Migadu `editor@spainexpat.com` exists separately. Do not assume Envelope editor@ is Migadu unless the host is `imap.migadu.com`.
- AWS WorkMail IMAP may require the real WorkMail password/app password; trying the new Migadu password can yield temporary auth failure or access denied.
- Envelope account absence plus credential-store `aead::Error` means inbox cleanup/triage cannot happen until the source account is re-added or old credentials are supplied.
- If Envelope lacks a cross-account migration command, prefer extending Envelope over defaulting to `imapsync` when Tyler is explicitly working on Envelope itself. The reusable implementation shape is: configured source account + configured destination account, `FETCH BODY.PEEK[] FLAGS INTERNALDATE RFC822.SIZE` from source, destination `APPEND` with sanitized flags and preserved internal date, no source deletes, dry-run first, JSON/NDJSON progress, include/exclude folder globs, and a `migration_uid_map` table for idempotent reruns. Avoid printing passwords because Envelope can decrypt configured account credentials internally.
- A minimal Envelope migration feature was prototyped in `/tmp/envelope-imap-migrate` on branch `feat/imap-migrate`: `envelope migrate folders --from <src> --to <dst>` and `envelope migrate run --from <src> --to <dst> [--dry-run] [--include glob] [--exclude glob] [--json]`; full `cargo test` passed. Before using on a large production mailbox, harden batching, UIDVALIDITY tracking, folder mapping like WorkMail `Junk E-mail` → Migadu `Junk`, status/resume reporting, and per-message failure events.
- When Envelope can read the source but the plaintext password is not available, you can still create a verified metadata inventory archive with `envelope folders` + `envelope inbox --limit <folder_count+buffer> --json`; if the new Envelope migration command is unavailable or not production-hardened, `imapsync` still needs the source plaintext/app password.
- If using local Migadu credentials from a skill file, scripts may parse them without printing secrets; output only account, host, status, folder counts, and redacted errors.
- Migadu IMAP success does not prove SMTP readiness. An auth-only SMTP check can fail with `421 4.7.0 too many errors`; report it as a blocker and do not send test mail without approval.
- For SpainExpat/Dorado, adding `partners@spainexpat.com` to Envelope is a low-risk outreach unblock separate from migrating old `editor@` history.

## Final Report Format

Lead with the answer:

- Backup status and path.
- Source account/host actually backed up.
- Folder/message counts.
- Verification result.
- What could not be done and exact blocker.
- Next required credential/config action.

---
name: envelope-junk-rules
description: Operate Envelope's shipped rules system for junk/spam/noise cleanup: list, create, dry-run, and run `envelope rule` rules. The old `envelope junk` CLI is not shipped.
version: 1.1.0
author: Envelopie
license: FSL-1.1-ALv2
metadata:
  hermes:
    tags: [Envelope, email, junk, spam, rules, IMAP, triage]
---

# Envelope Junk Rules

Use this for any Envelope task involving junk filtering, spam cleanup, noisy inboxes, rule candidates, mail automation, or questions like “why didn’t the bot move this?”

## Production reality

Envelope currently ships a **rule engine**:

```bash
envelope rule ...
```

Envelope does **not** currently ship the older draft junk-profile commands:

```text
envelope junk status|allow|block|learn|review   # not available
```

So bots must operate the shipped `envelope rule` commands. Do not tell Tyler the junk system is absent just because `envelope junk` does not exist.

## Runtime singleton on wondermonkey

There must be exactly one active public Envelope command path:

```text
/Users/wondermonkey/.local/bin/envelope
```

That shim sets:

```text
HOME=/Users/wondermonkey/.hermes/shared/envelope-home
```

and execs:

```text
/Users/wondermonkey/.local/libexec/envelope-rust
```

Do **not** create compatibility `envelope` symlinks/wrappers in profile dirs, `~/bin`, or Hermes venvs. If an agent cannot find Envelope, fix its PATH so `/Users/wondermonkey/.local/bin` is visible.

## First commands

Always start by checking live state:

```bash
command -v envelope
envelope --version
envelope accounts list --json
```

List rules per account, not globally:

```bash
envelope rule list --account editor@spainexpat.com --json
```

Rules live in the shared Envelope store and are passive until run.

## Create a narrow junk rule

First verify the destination spam folder:

```bash
envelope folders --account editor@spainexpat.com --json
```

Then create a narrow rule:

```bash
envelope rule create   --account editor@spainexpat.com   --name 'editor junk: fake Walmart points'   --match-subject '*Walmart*points*'   --action 'move=Junk E-mail'   --priority 20   --stop   --json
```

Provider spam folders differ. Verify live, but current useful defaults are:

- `editor@spainexpat.com` / Amazon WorkMail: `Junk E-mail`
- Migadu accounts: `Junk`
- Gmail accounts: `[Gmail]/Spam`

Never copy a move rule between providers without changing the folder.

## Dry-run before applying

Dry-run one selected UID:

```bash
envelope rule test --account editor@spainexpat.com --folder INBOX 83963 --json
```

Apply enabled rules only with a bounded limit:

```bash
envelope rule run --account editor@spainexpat.com --folder INBOX --limit 100 --json
```

Read the JSON log and report:

- processed count
- action count
- rules that fired
- errors, especially move/copy failures

## Safe junk-rule workflow

1. Inspect recent INBOX headers:
   ```bash
   envelope inbox --account <email> --limit 50 --json
   ```
2. Cluster obvious spam/noise by stable sender domain, exact sender, or stable subject token.
3. Verify the real spam folder with `envelope folders --account <email> --json`.
4. Create the narrowest rule that catches the cluster.
5. Dry-run a known UID with `rule test`.
6. Apply with `rule run --limit 50` or `--limit 100`.
7. Report exactly what changed.

## Current rule inventory pattern

Do not trust snapshots; verify live. Useful inventory command:

```bash
python3 - <<'PY'
import json, subprocess
accounts=json.loads(subprocess.run(['envelope','accounts','list','--json'], text=True, capture_output=True).stdout)
for account in accounts:
    email=account.get('username') or account.get('email') or account.get('id')
    cp=subprocess.run(['envelope','rule','list','--account',email,'--json'], text=True, capture_output=True, timeout=30)
    if cp.returncode == 0:
        rules=json.loads(cp.stdout)
        if rules:
            print(f"
{email}: {len(rules)} rule(s)")
            for r in rules:
                print(' ', r.get('name'), '=>', r.get('action'))
PY
```

## Dashboard surface

The Envelope dashboard has a Rules Control Plane. Use it to inspect rules and dry-run enabled rules against an open message. It is not a replacement for the CLI yet; it should agree with `envelope rule list --account ... --json`.

## Agent/Nagatha failure mode

If Nagatha, Skippy, Spanorama, or another bot misses rules, the likely cause is not Envelope: it is the bot not loading this skill or using stale docs that only mention inbox/search.

For any junk/rules task, the bot must know:

- `envelope rule` is the shipped system.
- `envelope junk` is old draft language and not a CLI surface.
- rules are per-account and passive until `rule run`.
- spam destination folders are provider-specific.
- dry-run before moving mail.

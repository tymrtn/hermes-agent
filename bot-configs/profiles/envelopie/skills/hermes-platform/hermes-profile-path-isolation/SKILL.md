---
name: hermes-profile-path-isolation
description: Diagnose when a binary is installed on the machine but missing inside a Hermes profile because subprocess HOME isolation and PATH assembly diverge.
version: 1.0.0
author: Envelopie
---

# Hermes Profile PATH Isolation Debugging

Use this when a command works in the user's normal shell but fails inside a Hermes profile session or gateway run.

## Problem pattern

A binary is genuinely installed on the machine, but Hermes profile subprocesses cannot resolve it with `command -v`, or the binary resolves by absolute path but sees the isolated profile `HOME` and therefore misses user-scoped auth/config.

Examples observed on Tyler's macOS setup:
- `envelope` existed and ran at `/Users/wondermonkey/.local/bin/envelope`
- inside the `envelopie` profile, `command -v envelope` failed
- subprocess `HOME` was `/Users/wondermonkey/.hermes/profiles/envelopie/home`
- subprocess `PATH` omitted all of:
  - `/Users/wondermonkey/.local/bin`
  - `/Users/wondermonkey/.hermes/profiles/envelopie/home/.local/bin`
  - `/Users/wondermonkey/.hermes/profiles/envelopie/bin`
- Claude Code existed at `/Users/wondermonkey/.local/bin/claude`, but running it inside the isolated Envelopie profile returned `Not logged in · Please run /login` because Claude looked under the profile-local `HOME`; running `HOME=/Users/wondermonkey /Users/wondermonkey/.local/bin/claude auth status --text` showed the real user Claude Max auth was healthy.

## What is happening

Hermes intentionally isolates subprocesses per profile by overriding `HOME` to `{HERMES_HOME}/home` when that directory exists.

Relevant implementation points:
- `hermes_constants.get_subprocess_home()`
- `tools/environments/local.py` → `_make_run_env()` and `_sanitize_subprocess_env()`

This isolation is correct in spirit, but PATH may not be rebuilt to match the new HOME. That produces a false impression that the target app's installer is broken.

## Diagnosis flow

1. Check the profile subprocess environment:
```bash
printf 'HOME=%s\nPATH=%s\n' "$HOME" "$PATH"
```

2. Check normal PATH resolution:
```bash
command -v envelope
```

3. Check the expected absolute-path binary directly:
```bash
/Users/<user>/.local/bin/envelope --version
```

4. If needed, inspect the user's shell rc for expected PATH setup:
```bash
python3 - <<'PY'
from pathlib import Path
p = Path.home() / '.zshrc'
if p.exists():
    for i, line in enumerate(p.read_text(errors='ignore').splitlines(), 1):
        if '.local/bin' in line or 'PATH' in line:
            print(f'{i}:{line}')
PY
```

## Interpretation

If:
- `command -v <tool>` fails inside the Hermes profile,
- the absolute-path binary works, and
- `HOME` points at `~/.hermes/profiles/<name>/home`,

then the installation is probably fine. The real issue is Hermes profile PATH/bootstrap behavior.

## Correct product diagnosis

Treat this as a **Hermes subprocess PATH/HOME assembly issue**, not primarily as an app installer bug or tool-auth outage.

That distinction matters:
- fixing the app installer may do nothing
- re-running OAuth/login under the isolated profile may create duplicate/confusing auth state
- fixing Hermes PATH assembly solves missing binaries
- using the real user `HOME` for user-authenticated CLIs solves profile-local auth misses when that is intentionally acceptable

## Short-term workaround

For missing binary resolution, call the binary by absolute path.

For user-scoped auth/config misses, run with the real user home explicitly, e.g.:
```bash
HOME=/Users/wondermonkey /Users/wondermonkey/.local/bin/claude auth status --text
HOME=/Users/wondermonkey /Users/wondermonkey/.local/bin/claude -p "task" ...
```

Use this only when the task should use Tyler's normal user-level credentials/config. If profile isolation is the goal, authenticate/configure the tool inside the profile home instead.

## Durable fix direction

In Hermes subprocess env construction, prepend profile-aware bin directories such as:
- `{HERMES_HOME}/bin`
- `{HERMES_HOME}/home/.local/bin`

Depending on the intended isolation model, also consider preserving the real OS user's `~/.local/bin`.

## Envelope-specific PATH correction

Envelope is a Rust CLI. There should not be a Python `envelope` implementation installed or preferred.

Desired invariant on wondermonkey/Hermes:

```bash
command -v envelope
# may resolve to /Users/wondermonkey/.hermes/hermes-agent/venv/bin/envelope or /Users/wondermonkey/.local/bin/envelope
# but every public path must be a wrapper to the shared singleton store
```

Envelope's raw Rust binary lives at `/Users/wondermonkey/.local/libexec/envelope-rust`. Public wrappers export `HOME=/Users/wondermonkey/.hermes/shared/envelope-home` before execing it. Do not describe a venv wrapper as Python/server Envelope; it is acceptable if it routes to the shared HOME.

Envelope's singleton store is:

```text
/Users/wondermonkey/.hermes/shared/envelope-home/Library/Application Support/envelope-email/
```

Profile/user `Library/Application Support/envelope-email` paths should symlink to that store so all bots see the same accounts.

Operational sequence:

```bash
command -v envelope
type -a envelope
envelope --version
envelope accounts list --json
envelope inbox --account <email> --limit 1 --json
```

Do not start by searching for stale Envelope implementation details such as `FastAPI`, `Railway`, `ENVELOPE_API_KEY`, repo paths, or old Python account tables. After an upstream Migadu/mailbox password reset, update the Envelope store through the Rust CLI, not by rediscovering old architecture.

## Good final wording to the user

Say:
- the app is installed
- Envelope is the Rust CLI; anything under a Python venv should be treated as a legacy wrapper/PATH placement issue, not as Python Envelope
- the durable fix is PATH/routing so bare `envelope` resolves to the installed Rust CLI and uses the intended application-data HOME

Avoid saying:
- the app is not installed
- the app installer is definitely broken
- Envelope needs repo/API/Railway discovery before trying the CLI
- the Hermes venv wrapper is canonical

unless you have verified the absolute-path binary is actually missing.

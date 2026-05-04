---
name: skippy-openclaw-comms
description: Verify and use Hermes↔Skippy communication paths on macOS/OpenClaw, including fallback behavior and common gateway failures.
version: 1.0.0
author: Hermes Agent
---

# Skippy / OpenClaw Comms

Use this when you need to talk to Skippy (the OpenClaw `main` agent), verify the link works, or debug claims that Hermes↔Skippy cross-agent comms are configured.

## What this is for

- Verifying Nagatha/Hermes can reach Skippy
- Checking whether `nagatha-comms` / `skippy-comms` style routes are actually active
- Distinguishing a broken gateway from a usable embedded fallback
- Running a quick proof test before relying on Skippy for context

## Known environment facts

- OpenClaw binary: `/opt/homebrew/bin/openclaw`
- Default Skippy agent id: `main`
- Gateway target may fail with:
  - `gateway closed (1006 abnormal closure (no close frame))`
- Even when that happens, `openclaw agent ...` may still return a usable embedded fallback reply.

## Fast verification sequence

### 1. Confirm agent exists

```bash
/opt/homebrew/bin/openclaw agents list --json
```

Expected: agent `main` with identity `Skippy (Skippy the Magnificent)`.

### 2. Test direct agent reply

Use a deterministic prompt:

```bash
/opt/homebrew/bin/openclaw agent --agent main --json --message "Reply with exactly: SKIPPY DIRECT OK."
```

Interpretation:
- If you get the exact reply, Hermes → Skippy works well enough.
- If you also see a 1006 gateway error first, note that the command still succeeded via embedded fallback.

### 3. Test system-event path separately

```bash
/opt/homebrew/bin/openclaw --dev system event --json --mode now --expect-final --text "Nagatha test from Hermes. If you receive this, reply with exactly: SKIPPY LINK OK."
```

Interpretation:
- `{"ok": true}` means the event was accepted.
- This does **not** guarantee you'll get the response body in the shell output.
- Treat this as enqueue/acceptance verification, not proof of two-way content delivery.

## Important pitfall

A claimed comms setup is not the same as an active one.

In testing, Skippy could be reached directly, but when asked to use the supposed `nagatha-comms` route, Skippy replied that no such route/contact was configured in the active runtime.

So always verify by asking Skippy to actually use the route, e.g.:

```bash
/opt/homebrew/bin/openclaw agent --agent main --json --message "Test the Nagatha comms path now. Use your nagatha-comms route to send Hermes this exact text: TOKEN123. Then reply here with exactly one line: SENT if you sent it, or FAILED if not."
```

If Skippy says the route does not exist, the skill/config is either:
- in a different workspace/profile
- not loaded in the active runtime
- or only documented, not actually wired up

A concrete failure pattern seen in practice:
- `~/.openclaw/workspace-dev/skills/nagatha-comms/SKILL.md` existed
- but OpenClaw's active agent workspace in `~/.openclaw/openclaw.json` was `~/.openclaw/workspace`
- and `~/.openclaw/workspace/skills/` did not contain `nagatha-comms`

In that state, the named comms setup is real on disk but invisible to the active Skippy runtime.

Verify this explicitly:

```bash
# Active OpenClaw workspace
python3 - <<'PY'
import json, pathlib
cfg = json.loads(pathlib.Path('~/.openclaw/openclaw.json').expanduser().read_text())
print(cfg['agents']['defaults']['workspace'])
PY

# Skills actually visible in the active workspace
find ~/.openclaw/workspace/skills -maxdepth 2 -name 'SKILL.md'

# Claimed comms skill in alternate workspace
find ~/.openclaw/workspace-dev/skills -maxdepth 2 -name 'SKILL.md' | grep nagatha-comms
```

Also verify the Hermes side instead of trusting memory. In one real check, the claimed global path `~/.hermes/skills/skippy-comms/SKILL.md` did not exist; the only relevant active Hermes-side skill was a local verification skill (`skippy-openclaw-comms`), not a live messaging bridge.

Concrete failure mode observed:
- OpenClaw active workspace from `~/.openclaw/openclaw.json` was `~/.openclaw/workspace`
- `nagatha-comms` actually existed at `~/.openclaw/workspace-dev/skills/nagatha-comms/SKILL.md`
- so the route was real, but invisible to the running Skippy agent
- Hermes side also did not have a global `skippy-comms` skill at `~/.hermes/skills/skippy-comms/`; the active local skill was `skippy-openclaw-comms`, which is only for verification/debugging, not message relay

So when comms are claimed to exist but fail in practice, check the active workspace/path mismatch before assuming the route is broken.

## How to report status

Use these buckets:

- `Direct agent path works`
- `System event accepted`
- `Named cross-agent route unverified`
- `Named cross-agent route failed`
- `Gateway flaky but embedded fallback usable`

## Recommended default behavior

If the named comms route is unverified or failing, use direct OpenClaw agent calls for Skippy rather than blocking on the nicer integration.

## Do not overclaim

Do **not** say Hermes↔Skippy is fully bidirectional unless you have verified all of the following:
1. Hermes can get a deterministic reply from Skippy
2. Skippy can use the named route/tool to send a message back
3. You can observe receipt on the Hermes side, not just command acceptance

## One-line conclusion template

`Direct Skippy access works; gateway is flaky; bidirectional named comms are not proven until Skippy successfully uses the configured route.`

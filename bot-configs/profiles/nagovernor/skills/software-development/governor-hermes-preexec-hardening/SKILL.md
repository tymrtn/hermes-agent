---
name: governor-hermes-preexec-hardening
description: Fast-path Governor rollout in Hermes after a shell/tool security incident. Use when you need immediate pre-exec enforcement for a concrete incident class before full Governor/OpenClaw integration is ready.
version: 1.0.0
author: Nagovernor
license: MIT
metadata:
  hermes:
    tags: [governor, hermes, security, preexec, approvals, incident-response]
    related_skills: [systematic-debugging, test-driven-development, writing-plans]
---

# Governor Hermes Pre-Exec Hardening

Important correction:
This skill describes an emergency containment pattern, not Governor itself.
If the user wants actual Governor, do not use this skill as the implementation plan.
Do not invent a "Governor-style" guard, shadow layer, or regex firewall and present it as Governor.

Use this only when the user explicitly accepts temporary containment logic.
If the user says "use Governor itself" or objects to hard-coded blocking, stop and wire the real Governor engine instead.

## What this approach does

Instead of waiting for full Governor productization, add a deterministic Governor-style guard to Hermes' existing command approval path.

Current best insertion points:
- `tools/approval.py` → `check_all_command_guards()`
- `tools/governor_shadow.py` for normalized shell heuristics + incident-specific routing
- `tools/terminal_tool.py` already consumes the guard result before execution, so usually no direct edit is needed there

This lets you ship:
- `deny` for the exact incident pattern
- `review` for suspicious but maybe-legitimate variants
- immediate test coverage

## When to choose this path

Choose this path if all are true:
1. The threat is a shell/terminal command class Hermes can recognize deterministically
2. The incident needs immediate mitigation
3. Full Governor review-band integration across all tools is not done yet
4. You can define a narrow rule without pretending to solve all governance at once

Examples:
- recursive credential/config sweep over home directory
- broad hidden-file search outside workspace
- shell access to highly sensitive local paths

## Root-cause findings this skill encodes

These were learned during a real incident response:
- do not assume a Governor integration path exists; verify the actual files and runtime before claiming one
- a fabricated local module or heuristic wrapper is not Governor, even if it calls itself Governor
- the real enforcement choke point in Hermes is still `check_all_command_guards()` in `tools/approval.py`
- `tools/terminal_tool.py` already blocks based on that guard result before execution
- if the user wants Governor proper, the work must route through the real Governor binary/engine and real attribution, not ad hoc shell heuristics
- only use narrow incident-focused rules if the user explicitly accepts temporary containment rather than true Governor integration

## Minimal implementation pattern

### 1. Add deterministic routing in `tools/governor_shadow.py`

Create or extend a function like:
- `evaluate_governor_guard(command, env_type) -> dict`

Return shape should be:
```python
{
  "route": "allow|review|deny",
  "pattern_key": "governor:..." or None,
  "description": "human-readable reason" or None,
  "attrs": [...],
  "reasons": [...],
  "command": command,
  "env_type": env_type,
}
```

Keep this deterministic and narrow.
Do not ask an LLM whether the command is dangerous.

### 2. Merge guard attrs into `infer_shell_action()`

If `infer_shell_action()` already emits attrs/reasons for shadow scoring, merge in the new guard attrs/reasons so:
- telemetry stays useful
- shadow scoring remains aligned with enforcement

### 3. Enforce in `tools/approval.py`

Import the new guard:
```python
from tools.governor_shadow import evaluate_governor_guard, maybe_shadow_score
```

Inside `check_all_command_guards()`:
- compute `shadow = maybe_shadow_score(...)`
- compute `governor_guard = evaluate_governor_guard(...)`

Then enforce in this order:

1. container skip
2. yolo / approvals off bypass
3. Governor `deny` → hard block immediately
4. if non-interactive and Governor `review` → block instead of silently passing
5. otherwise continue existing Tirith + dangerous-command workflow
6. if Governor `review` in interactive/gateway mode → append it to warnings so it goes through normal approval flow

This preserves existing approval UX while giving Governor real teeth.

## Proven incident pattern: recursive credential sweep

For suspected `rg`-style credential/config sweeps, use these signals.

### Strong deny signals
Block when all or most are true:
- command verb is discovery/search capable:
  - `rg`, `ripgrep`, `grep`, `find`, `fd`
- search scope includes home/system paths:
  - `~`, `~/...`, `/Users/...`, `/home/...`, `/root`, `/`
- command expands into hidden/ignored scope:
  - `-uu`, `-uuu`, `--hidden`, `--no-ignore`, `--no-ignore-vcs`
- command text includes credential/config hunting patterns:
  - `api[_-]?key`
  - `secret`
  - `token`
  - `password`
  - `credential`
  - `auth`
  - `DATABASE_URL`
  - `ENVELOPE_SECRET_KEY`
  - `private key`
  - `BEGIN ... PRIVATE KEY`
  - `.env`

Recommended result:
- `route = "deny"`
- `pattern_key = "governor:credential_sweep"`
- description should explicitly say it is a credential-oriented recursive search across home/system scope

### Review signals
Require approval when:
- broad recursive search over home scope
- hidden/ignored files included
- no explicit credential-hunting pattern, but still unbounded and suspicious

Recommended result:
- `route = "review"`
- `pattern_key = "governor:broad_home_search"`

## Test-first workflow

Always use TDD here.

### Tests to add first
In `tests/tools/test_governor_shadow.py`:
- deny credential-oriented recursive home sweep
- review broad hidden home search

In `tests/tools/test_command_guards.py`:
- non-interactive Governor deny still blocks
- gateway Governor review returns `approval_required`

### Recommended test commands
Run focused tests first:
```bash
pytest -q tests/tools/test_governor_shadow.py tests/tools/test_command_guards.py
```

Then run adjacent approval tests:
```bash
pytest -q tests/tools/test_approval.py tests/tools/test_yolo_mode.py
```

If you touched broader approval behavior, run more of the tools suite.

## Practical implementation notes

- Prefer narrow incident-focused rules over broad speculative policy
- Reuse existing Hermes approval return shapes (`approved`, `status`, `pattern_key`, `description`, `message`)
- Include `governor_shadow` in returned payloads so telemetry remains available
- Do not silently allow Governor `review` cases in unattended/non-interactive flows
- Keep pattern keys stable; they become part of approval state and regression tests

## Known limitations

This is not the full Governor product.
It is a fast, defensible pre-exec hardening layer.

It does not by itself provide:
- full OpenClaw `before_tool_call` interception
- complete allow/review/deny routing across all tools
- calibrated catalog-backed scoring for every action family
- perfect replay semantics for reviewed actions

Those come next.

## Next step after this skill

After shipping Hermes hardening, move to:
1. OpenClaw plugin `before_tool_call` interception
2. broader Governor routing beyond shell
3. unified review-band semantics across Hermes/OpenClaw
4. telemetry-driven calibration

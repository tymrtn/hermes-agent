---
name: expressionengine-eecli-mcp-workflow
description: Work on the SpainExpat/SECOM ExpressionEngine MCP by treating EE's native eecli as the source of truth and the MCP as a thin adapter.
version: 1.0.0
author: Spanorama
license: MIT
metadata:
  hermes:
    tags: [expressionengine, eecli, mcp, spainexpat, secom]
---

# ExpressionEngine eecli + MCP Workflow

Use this when Tyler asks about ExpressionEngine CLI work, EE MCP status, native EE API, or the `eecli.php` architecture for SpainExpat/SECOM.

## Core Architecture Decision

- Do **not** make the MCP server the source of truth.
- Do **not** keep extending the custom SpainExpat `api.php` as the long-term control plane unless Tyler explicitly asks for a short-term patch.
- Preferred path:
  1. Enhance EE's native CLI (`eecli.php`) with JSON-capable commands.
  2. Make the MCP a thin adapter over `php .../eecli.php ... --format=json`.
  3. Avoid duplicate business logic and fragile custom API shims.

## Important Local Paths

- Upstream ExpressionEngine clone:
  - `/Users/wondermonkey/Dropbox/Code/eecli-overhaul`
- SpainExpat MCP repo:
  - `/Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp`
- Refreshed local SpainExpat EE checkout:
  - `/Users/wondermonkey/Dropbox/Code/SECOM/spainexpat.com/htdocs`
- Live/installed EE launcher in SpainExpat checkout:
  - `ee_system/eecli.php`
- Upstream EE launcher path:
  - `system/ee/eecli.php`
- Upstream CLI internals:
  - `system/ee/ExpressionEngine/Cli/`

## Status-Check Procedure

1. Inspect upstream EE tree:

```bash
cd /Users/wondermonkey/Dropbox/Code/eecli-overhaul
git status --short --branch
git log --oneline -5
python3 - <<'PY'
from pathlib import Path
for p in ['system/ee/ExpressionEngine/Cli','system/eecli.php','system/ee/eecli.php']:
    q=Path(p)
    print(p, q.exists(), q.stat().st_size if q.exists() and q.is_file() else '')
print('cli_php_files', len(list(Path('system/ee/ExpressionEngine/Cli').rglob('*.php'))))
PY
```

2. Inspect MCP repo separately:

```bash
cd /Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp
git status --short --branch
git diff --stat
git diff --cached --stat
git ls-files --others --exclude-standard | sed -n '1,80p'
```

3. Check whether MCP work is still using `api.php` instead of eecli:

```bash
cd /Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp
python3 - <<'PY'
from pathlib import Path
for p in Path('.').rglob('*'):
    if 'venv' in p.parts or not p.is_file():
        continue
    try:
        s=p.read_text(errors='ignore')
    except Exception:
        continue
    if 'eecli' in s or 'api.php' in s or '--format=json' in s:
        print(p)
PY
```

## Known Implementation State

As of the manual MVP pass:

- `/Users/wondermonkey/Dropbox/Code/eecli-overhaul` contains native EE CLI read/introspection work:
  - `system/ee/ExpressionEngine/Cli/Commands/CommandEntriesGet.php`
  - `system/ee/ExpressionEngine/Cli/Cli.php` registers `entries:get`
  - `system/ee/language/english/cli_lang.php` includes `entries:get` help strings
- Existing EE commands already support JSON:
  - `channels:list --format=json`
  - `fields:list --format=json`
- Added command surface:
  - `php system/ee/eecli.php entries:get <entry_id> --format=json`
  - `php system/ee/eecli.php entries:get <entry_id> --format=json --no-fields`
- `/Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp` now has a thin adapter seam:
  - `src/eecli_client.py` shells out to `php EECLI_PATH ... --format=json`
  - `src/main.py` exposes `channels_list`, `fields_list`, and `get_entry`
  - `README.md`, `env.example`, and `CLAUDE.md` document `EECLI_PATH`, `PHP_BINARY`, and `EECLI_CWD`
  - `test_eecli_client.py` covers command construction, JSON array parsing, and invalid JSON errors
- Verification that passed after the follow-up hardening pass:
  - `php -l system/ee/ExpressionEngine/Cli/Commands/CommandEntriesGet.php`
  - `php -l system/ee/ExpressionEngine/Cli/Commands/CommandChannelsList.php`
  - `php -l system/ee/ExpressionEngine/Cli/Commands/CommandFieldsList.php`
  - `php -l system/ee/ExpressionEngine/Cli/Cli.php`
  - `php -l system/ee/language/english/cli_lang.php`
  - `python3 -m py_compile src/*.py test_eecli_client.py`
  - `python3 test_eecli_client.py` → 5 tests passed
- Follow-up hardening completed:
  - `entries:get --format=json` now handles missing `entry_id` as JSON instead of falling through generic CLI failure.
  - `entries:get` custom-field payload filters to real `field_id_*` fields so EE default publish fields do not pollute `fields`.
  - `channels:list --format=json` and `fields:list --format=json` return `[]` for empty results instead of localized text.
  - `fields:list --type` now filters by field type rather than accidentally using `--group`.
  - `ee-mcp/src/main.py` has a `main()` console entry point for `pyproject.toml`.
  - Dangerous legacy `test_file_field.py` was moved to `legacy-api-disabled/test_file_field.py.disabled` with a README warning.
- Live local EE CLI smoke against installed SpainExpat checkout now passes after suppressing JSON-mode CLI warning noise:
  - Local installed launcher patched for smoke: `/Users/wondermonkey/Dropbox/Code/SECOM/spainexpat.com/htdocs/ee_system/eecli.php` and `ee_system/ee/eecli.php` set `$debug = 0` when `--format=json` is present.
  - Backup before local patch: `/Users/wondermonkey/Dropbox/Code/SECOM/spainexpat.com/htdocs/ee_system/ee/.eecli-mvp-backup-20260503T223755Z`
  - `python3 test_installed_eecli_smoke.py` passes: channels=9, fields=49, entry 29 no-fields returns 16-key dict.
  - `python3 test_installed_mcp_eecli_smoke.py` passes through `EECLIClient`.
  - `venv/bin/python test_installed_mcp_tool_smoke.py` passes through the MCP `manage_content` tool dispatcher.
- Additional hardening completed after Codex QA:
  - Upstream `system/ee/eecli.php` now suppresses debug/error display when `--format=json` is present, to keep JSON stdout machine-parseable under noisy PHP/addon environments.
  - `ee-mcp` legacy `src/api_client.py` and `src/auth.py` moved to `legacy-api-disabled/src/*.disabled`; `pyproject.toml` and `requirements.txt` no longer declare HTTP client deps for the active eecli-first path.
  - Active verification command: `python3 -m py_compile src/*.py test_eecli_client.py test_installed_eecli_smoke.py test_installed_mcp_eecli_smoke.py test_installed_mcp_tool_smoke.py && python3 test_eecli_client.py && python3 test_installed_eecli_smoke.py && python3 test_installed_mcp_eecli_smoke.py && venv/bin/python test_installed_mcp_tool_smoke.py`
- Remaining caveat: changes are local/upstream-checkout and local installed-site smoke only; production deploy/copy is not done without explicit Tyler approval.
- `SECOM/ee-mcp` had pre-existing dirty files before the manual pass: `pyproject.toml`, `src/api_client.py`, `src/auth.py`, `.claude/`, and `test_file_field.py`. Keep those boundaries clear when reviewing diffs.

## Development Protocol

If Tyler asks to implement this, follow Spanorama's development protocol:

1. Claude planning mode.
2. Claude implementation pass.
3. Codex adversarial QA / sanity check.
4. Claude GTM / launch / onboarding pass if relevant.
5. Codex operational/GTM autonomy pass if relevant.
6. Claude updates project-root `CLAUDE.md`.
7. Load `bot-postmortem-handoff` and close with postmortem if the task was actual development.

## Pitfalls

- The local SECOM checkout previously had zero-byte placeholder files due to Dropbox hydration/sync. Verify file sizes before trusting local EE files.
- Do not conflate the installed SpainExpat path `ee_system/eecli.php` with upstream `system/ee/eecli.php`.
- Do not treat clean upstream EE clone status as proof the MCP adapter is done; MCP work lives in a separate repo.
- Do not call syntax-check success a functional MCP test.
- Hermes profiles can isolate `HOME`; Claude Code/Codex auth that works for another bot or for `/Users/wondermonkey/.codex` may not exist under Spanorama's profile home (`~/.hermes/profiles/spanorama/home`). If agent delegation fails with auth errors, verify `whoami`, `$HOME`, `claude auth status --text`, `codex --version`, and whether `$HOME/.codex/auth.json` exists before assuming the machine is globally broken.
- If Claude/Codex delegation is blocked but Tyler asks to keep going, proceed manually with file tools and explicit verification rather than stalling on auth setup.

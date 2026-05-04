---
name: expressionengine-eecli-mcp-mvp
description: Continue or verify the SpainExpat SECOM ExpressionEngine CLI and MCP MVP using native EE CLI JSON read commands with a thin MCP adapter.
---

# ExpressionEngine EE CLI + MCP MVP

Use this when Tyler asks about the EE CLI project, SECOM MCP, ExpressionEngine MCP, or making the SpainExpat EE MCP work.

## Repositories / paths

- Native EE CLI source repo: `/Users/wondermonkey/Dropbox/Code/eecli-overhaul`
- MCP repo: `/Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp`
- Local installed EE checkout patched for smoke testing: `/Users/wondermonkey/Dropbox/Code/SECOM/spainexpat.com/htdocs/ee_system`
- Backup of local installed EE files made during MVP work: `/Users/wondermonkey/Dropbox/Code/SECOM/spainexpat.com/htdocs/ee_system/ee/.eecli-mvp-backup-20260503T223755Z`

## Intended architecture

1. `eecli.php` / native EE CLI is the source of truth.
2. MCP is a thin adapter that shells out to:
   - `php <EECLI_PATH> <command> --format=json`
3. Do not revive the legacy `api.php` / HTTP API path unless Tyler explicitly asks.
4. MVP is read-only.

## Working MVP commands

Native EE CLI:

```bash
php eecli.php channels:list --format=json
php eecli.php fields:list --format=json
php eecli.php entries:get <entry_id> --format=json
php eecli.php entries:get <entry_id> --format=json --no-fields
```

MCP tools:

- `channels_list`
- `fields_list`
- `get_entry`

Active adapter files in MCP repo:

- `src/eecli_client.py`
- `src/main.py`

Legacy path was disabled/quarantined under `legacy-api-disabled/`.

## Verification commands

From native EE CLI source repo, lint relevant PHP files:

```bash
cd /Users/wondermonkey/Dropbox/Code/eecli-overhaul
php -l system/ee/eecli.php
php -l system/ee/EllisLab/ExpressionEngine/Controller/Cli/Command/CommandEntriesGet.php
php -l system/ee/EllisLab/ExpressionEngine/Controller/Cli/Command/CommandChannelsList.php
php -l system/ee/EllisLab/ExpressionEngine/Controller/Cli/Command/CommandFieldsList.php
php -l system/ee/EllisLab/ExpressionEngine/Controller/Cli/Cli.php
php -l system/ee/legacy/language/english/cli_lang.php
```

From MCP repo:

```bash
cd /Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp
python3 -m py_compile src/*.py test_eecli_client.py
python3 test_eecli_client.py
```

Installed-site smoke tests previously used:

```bash
python3 test_installed_eecli_smoke.py
python3 test_installed_mcp_eecli_smoke.py
venv/bin/python test_installed_mcp_tool_smoke.py
```

Expected smoke coverage:

- `channels:list --format=json` returns JSON list, previously 9 channels locally.
- `fields:list --format=json` returns JSON list, previously 49 fields locally.
- `entries:get 29 --format=json --no-fields` returns a valid entry dict, previously 16 top-level keys locally.
- MCP `manage_content` dispatcher works for `channels_list`, `fields_list`, and `get_entry`.

## Known fixes implemented during MVP work

- `entries:get` registered in native EE CLI.
- `entries:get --format=json` missing/invalid `entry_id` returns JSON error.
- `entries:get` custom fields filtered to real `field_id_*` fields only.
- `channels:list --format=json` and `fields:list --format=json` return `[]` on empty results.
- `fields:list --type` fixed; it had been filtering using `--group`.
- `eecli.php` suppresses debug/error display when `--format=json` is present so PHP 8.5 addon warnings do not corrupt JSON stdout.
- MCP console `main()` entry point added.
- Active HTTP client dependencies removed from MCP active path.

## Pitfalls

- Do not claim production is done unless deployed and verified there. Current known state was local end-to-end only.
- Local EE/SECOM files can have quirks/placeholders; refresh from server after server-side upgrades if needed.
- PHP 8.5 warning/deprecation noise can corrupt JSON unless suppressed in CLI JSON mode.
- Keep communications/email work separate from this development work unless Tyler explicitly merges them.

## Done criteria for future continuation

Before saying this project is fully done beyond local MVP:

1. Commit/branch or PR is clean in both repos.
2. Native EE CLI commands pass syntax and functional tests.
3. MCP adapter tests pass.
4. Installed local EE smoke passes.
5. If production deployment is requested: production files are backed up first, deployed explicitly, then smoke-tested against production paths.
6. Update project-root `CLAUDE.md` if Tyler asked for full development protocol completion.

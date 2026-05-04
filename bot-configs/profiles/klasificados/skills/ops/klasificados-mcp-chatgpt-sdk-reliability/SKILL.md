---
name: klasificados-mcp-chatgpt-sdk-reliability
description: Debug and verify Klasificados MCP / ChatGPT Apps SDK reliability, especially first-call hangs caused by DB schema init or DDL on request paths.
---

# Klasificados MCP / ChatGPT SDK Reliability

Use when Tyler asks to make sure MCP works for ChatGPT SDK, when `/mcp` or Apps SDK tool calls hang, or when local MCP smoke tests show connection refused / DDL locks / first-call timeouts.

## Key rule

MCP tool request paths must not run DB schema initialization, DDL, or migrations.

In particular, `src/clasificados/mcp/handlers.py:get_engine()` should only create/cache the SQLAlchemy engine. It must not call `init_database(_engine)` or anything that can run `ALTER TABLE`, `CREATE INDEX`, migrations, or backfills during a ChatGPT tool call.

App startup/deploy owns schema init/migrations. If standalone MCP usage lacks schema, fail clearly rather than silently doing DDL on the request path.

## Local setup

Repo path commonly used by Hermes for this project:

```bash
/private/tmp/klasificados-story-...   # story worktrees
/Users/wondermonkey/Dropbox/Code/klasificados
```

Do not rely on `~/Dropbox/code/klasificados`; that path may not exist under Hermes.

If `.env` is needed, do not `source .env`: this repo can contain variables whose names are not shell identifiers, e.g. starting with a digit. Use `python-dotenv`:

```bash
PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache \
uv run --with python-dotenv python -m dotenv -f .env run -- \
uvicorn clasificados.api:app --host 127.0.0.1 --port 28037
```

Never print secrets.

## Verification workflow

1. Inspect current diff before testing:

```bash
git status --short --branch
git diff --stat
git diff -- src/clasificados/mcp/handlers.py tests/unit/test_mcp_tool_handlers.py
```

2. Confirm `get_engine()` is request-path safe:

- It creates/caches the SQLAlchemy engine.
- It does not call `init_database()`.
- Add/keep a unit test that monkeypatches `init_database` and asserts it is never called while `create_db_engine` is called once and the cached engine is reused.

3. Run targeted tests:

```bash
PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/unit/test_mcp_tool_handlers.py \
  tests/unit/test_mcp_listing_creation.py \
  tests/integration/test_mcp_flows.py \
  tests/unit/test_search_v2_fts.py
```

Include Apps SDK tests if touched:

```bash
PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/unit/test_apps_sdk_server.py
```

4. Start API with dotenv, on a free port:

```bash
PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache \
uv run --with python-dotenv python -m dotenv -f .env run -- \
uvicorn clasificados.api:app --host 127.0.0.1 --port 28037
```

5. Smoke MCP JSON-RPC quickly:

- `initialize`
- `tools/list`
- `tools/call` for search, e.g. category `vehicles`, query `Toyota`, limit `3`, lang `es`
- A bounded OR query, e.g. `Toyota OR Honda OR Kia`

6. Direct handler latency smoke:

```python
from dotenv import load_dotenv
load_dotenv('.env')
import asyncio, time
from clasificados.mcp.handlers import handle_search

async def main():
    for q in ['Toyota', 'Toyota OR Honda OR Kia']:
        t=time.time()
        r=await handle_search({'category':'vehicles','query':q,'limit':3,'lang':'es'})
        print({'query':q,'sec':round(time.time()-t,2),'total':r.get('total'),'len':len(r.get('results',[]))})
asyncio.run(main())
```

7. Check DB activity after MCP calls to ensure no request-path DDL or lock waits:

```sql
select pid,state,wait_event_type,wait_event,now()-query_start age,left(query,160) q
from pg_stat_activity
where datname=current_database()
  and state <> 'idle'
  and pid<>pg_backend_pid()
order by query_start;
```

Bad signs:

- `ALTER TABLE ... search_vector` during MCP calls
- `CREATE INDEX` during MCP calls
- long `Lock` waits
- first `handle_search` taking ~85s or later calls timing out

## Known pitfalls

- Background-process watch notifications can arrive late from stale local servers; poll before treating repeats as active incidents.
- `Application startup complete` followed by `address already in use` means a previous local server still owns the port.
- Apps SDK errors pointing at `http://localhost:8000/... connection refused` can be local smoke wiring noise if the API base URL was not pointed at the test server.
- Scheduled enrichment/backfill errors from local test API processes may be noise if the DB connection was intentionally terminated during lock cleanup.

## Reporting Tyler

Use compressed status only, for example:

```text
332: fixed + debugged. No JS errors; MCP first-call lock fixed.
https://github.com/tymrtn/klasificados/compare/main...nagaklas/story-332-category-insights-below-fold

Prod unchanged. Tests passed.
```

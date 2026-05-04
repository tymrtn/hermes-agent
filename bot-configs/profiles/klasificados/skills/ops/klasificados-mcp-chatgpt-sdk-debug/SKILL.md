---
name: klasificados-mcp-chatgpt-sdk-debug
description: Debug and verify Klasificados ChatGPT Apps SDK / MCP runtime issues, including browser JS checks, live MCP JSON-RPC smoke tests, DB schema drift, and request-path DDL/lock problems.
version: 1.0.0
author: Nagaklas
license: MIT
metadata:
  hermes:
    tags: [klasificados, mcp, chatgpt-apps-sdk, qa, debugging]
    related_skills: [native-mcp, systematic-debugging, dogfood, claude-code, codex]
---

# Klasificados MCP / ChatGPT SDK Debug

Use this when Tyler says the ChatGPT SDK/MCP/app may have JS errors, MCP issues, runtime failures, or “make sure the MCP works.” This is not just unit testing; verify the browser, HTTP MCP endpoint, handler layer, and DB runtime path.

## Core rule

Do not stop at green unit tests. For ChatGPT/MCP readiness, perform live protocol smokes against a running local API with the same `.env` DB target the app will use, while redacting all secrets in notes.

## Workflow

1. **Use coding agents for code-affecting work**
   - Delegate implementation/fixes to Claude Code first.
   - Use Codex/adversarial review after fixes.
   - Keep Tyler-facing reports compressed; put detail in repo artifacts.

2. **Browser/JS check**
   - Run the web app locally.
   - Open the relevant page with browser tooling.
   - Check console errors and DOM ordering/state, not just screenshots.
   - For category/listing pages, verify search/listing controls appear before SEO/internal-link modules.

3. **Unit/integration baseline**
   - Run targeted tests for the changed area.
   - Clear any inherited `PYTHONPATH` before running tests. Hermes/agent shells can leak a path from another worktree, causing pytest to import stale code and show false failures:
     ```bash
     env -u PYTHONPATH PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run pytest ...
     ```
   - For MCP/Apps SDK, include at least:
     ```bash
     env -u PYTHONPATH PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
       tests/unit/test_apps_sdk_category_guidance.py \
       tests/unit/test_apps_sdk_contact_review.py \
       tests/unit/test_apps_sdk_http.py \
       tests/unit/test_apps_sdk_protocol.py \
       tests/unit/test_mcp_tool_handlers.py \
       tests/unit/test_mcp_listing_creation.py \
       tests/integration/test_mcp_flows.py \
       tests/unit/test_search_v2_fts.py -q
     ```
   - Add focused tests for any runtime bug discovered.

4. **Run live API with `.env` safely**
   - Do not `source .env` directly if it contains invalid shell variable lines or unquoted values; use dotenv:
     ```bash
     PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache \
       uv run --with python-dotenv python -m dotenv -f .env run -- \
       uvicorn clasificados.api:app --host 127.0.0.1 --port 28035
     ```
   - For ChatGPT Apps SDK `search`, set the internal API base explicitly when smoking outside the canonical port. Otherwise the SDK may default to `http://localhost:$PORT` or `http://localhost:8000` and return `search_error: [Errno 61] Connection refused` even though `/mcp initialize` and `tools/list` pass:
     ```bash
     CLASIFICADOS_API_BASE_URL=http://127.0.0.1:28035 \
       PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache \
       uv run --with python-dotenv python -m dotenv -f .env run -- \
       uvicorn clasificados.api:app --host 127.0.0.1 --port 28035
     ```
   - Never paste `ADMIN_SECRET`, DB URLs, API keys, or tokens in chat.

5. **HTTP MCP protocol smoke**
   - Probe the endpoint:
     ```bash
     curl -sS -i http://127.0.0.1:28035/mcp
     curl -sS -X POST http://127.0.0.1:28035/mcp \
       -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"qa","version":"1"}}}'
     curl -sS -X POST http://127.0.0.1:28035/mcp \
       -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
     ```
   - Then call real tools via `tools/call`, especially `start_here` and `search`.

6. **Direct handler smoke**
   - Test the handler directly with `.env` loaded to separate transport bugs from handler/DB bugs:
     ```bash
     PYTHONPATH=src UV_CACHE_DIR=/tmp/uv-cache uv run --with python-dotenv python - <<'PY'
     import asyncio, time, traceback
     from dotenv import load_dotenv
     load_dotenv('.env')
     from clasificados.mcp.handlers import handle_search
     async def main():
         t=time.time()
         try:
             r=await handle_search({'category':'vehicles','query':'Toyota OR Honda OR Toyota OR Kia OR Nissan OR Mazda','limit':3,'lang':'es'})
             print({'ok': True, 'seconds': round(time.time()-t,2), 'total': r.get('total'), 'len': len(r.get('results',[])), 'query': r.get('query')})
         except Exception as e:
             print({'ok': False, 'seconds': round(time.time()-t,2), 'error': repr(e)})
             traceback.print_exc()
     asyncio.run(main())
     PY
     ```

## Known Klasificados MCP pitfalls

### Apps SDK transport can pass protocol checks while tool calls fail

Symptom:
- JSON-RPC `initialize` and `tools/list` succeed instantly.
- `tools/call search` returns `structuredContent.type=search_error` with `[Errno 61] Connection refused`.

Cause: the Apps SDK HTTP handler calls the app's REST API through `CLASIFICADOS_API_BASE_URL` / `PORT`. If the local API is running on an ad-hoc port and the env var still points at `localhost:8000` or a different port, MCP protocol works but the actual search tool fails.

Fix/verification:
- Start the API with `CLASIFICADOS_API_BASE_URL=http://127.0.0.1:<same-port>` for local live smoke, or set it in-process before importing `clasificados.apps_sdk.server`.
- Treat `initialize`/`tools/list` as necessary but not sufficient; require `tools/call search` to return `search_results`, not `search_error`.

### Missing `listings_v2.search_vector`

Symptom:
```text
psycopg.errors.UndefinedColumn: column listings_v2.search_vector does not exist
```

Cause: ORM/search code expects `ListingV2.search_vector`, but older DBs may predate the column. Fix should be additive:
- add a startup migration that checks `information_schema.columns`
- `ALTER TABLE listings_v2 ADD COLUMN IF NOT EXISTS search_vector tsvector`
- `CREATE INDEX IF NOT EXISTS ix_listings_v2_search_vector ON listings_v2 USING GIN (search_vector)`
- backfill `WHERE search_vector IS NULL`
- update `migrations/create_listings_v2.sql`
- add tests proving idempotent/additive migration behavior

### Do not run schema DDL on first MCP tool call

Symptom:
- `/mcp` initializes but first `tools/call` hangs for 90–240s.
- `pg_stat_activity` shows locks around `ALTER TABLE ...` or `CREATE INDEX ...` during tool calls.

Cause: `clasificados.mcp.handlers.get_engine()` calling `init_database()` lazily inside request/tool execution.

Fix:
- MCP handlers should only create/cache the SQLAlchemy engine.
- Schema init and migrations belong to app startup/deploy, not request path.
- Add a unit test monkeypatching `create_db_engine` and `init_database` to assert `get_engine()` does not call `init_database()` and caches the engine.

### DB lock triage during local verification

If local QA creates stale blocking sessions, inspect before assuming app code is hung. Also remember that Hermes background watch alerts can be delayed; use `process list`/`process poll` as the authority before treating repeated `ERROR` or `Application startup complete` notifications as live incidents.

```python
from dotenv import load_dotenv
load_dotenv('.env')
import os
from sqlalchemy import create_engine, text
url=os.environ['DATABASE_URL'].replace('postgresql://','postgresql+psycopg://',1)
eng=create_engine(url, connect_args={'connect_timeout':10})
with eng.connect() as c:
    rows=c.execute(text("""
      select pid, state, wait_event_type, wait_event, now()-query_start age, left(query,160) q
      from pg_stat_activity
      where datname=current_database() and state <> 'idle' and pid<>pg_backend_pid()
      order by query_start
    """)).fetchall()
    for r in rows:
        print(dict(r._mapping))
```
Only terminate sessions you started or clearly stale local QA blockers. Do not kill production-owned work casually.

## Acceptance criteria for “MCP works”

- Browser page has no relevant console JS errors.
- `/mcp` GET returns server/tools metadata.
- JSON-RPC `initialize` succeeds.
- JSON-RPC `tools/list` returns expected tools.
- `tools/call start_here` succeeds.
- `tools/call search` succeeds or returns a valid empty result — not `search_error`, not `UndefinedColumn`, not connection refused, not timeout.
- Direct `handle_search()` with `.env` loaded returns without exception.
- Targeted tests pass.
- Adversarial review has no blockers.

## Reporting

Tyler-facing Telegram output should stay compressed. Example:

```text
332/MCP: fixed, needs final live smoke.
https://github.com/tymrtn/klasificados/compare/main...nagaklas/story-332-category-insights-below-fold

Found: request-path DDL and missing search_vector. Unit tests pass.
```
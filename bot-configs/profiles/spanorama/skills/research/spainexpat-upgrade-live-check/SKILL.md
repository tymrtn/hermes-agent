---
name: spainexpat-upgrade-live-check
description: Check whether the SpainExpat ExpressionEngine site upgrade is finished using live endpoint probes, then run the safest possible read-only API smoke test.
version: 1.0.0
author: Spanorama
license: FSL-1.1-ALv2
metadata:
  hermes:
    tags: [SpainExpat, ExpressionEngine, upgrade, API, smoke-test, QA]
---

# SpainExpat Upgrade Live Check

Use this for recurring operator checks during or just after a SpainExpat ExpressionEngine upgrade when you need a live-status answer, not a repo-based guess.

## Core rules
- Prefer live endpoint checks over local repo assumptions.
- Treat local Dropbox code as suspect until proven hydrated; `ee-mcp` and related files may appear but read as `0 bytes`.
- Do not modify content.
- A full API smoke-test **only** passes if a read-only authenticated action succeeds.

## Endpoints to probe
- Site root: `https://spainexpat.com/`
- Admin: `https://spainexpat.com/secom.php`
- API: `https://spainexpat.com/api.php`

## Workflow

### 1) Check public reachability
Use both browser and raw HTTP:
- `browser_navigate` to `/` and `/secom.php`
- `terminal` with `curl -I -L` or GET requests to `/`, `/secom.php`, and `/api.php`

Important: a browser navigation timeout does **not** by itself mean the site is down. In practice, browser loading can hang while direct HTTP GETs still return healthy responses quickly. If browser tools time out, immediately verify with raw HTTP before concluding the upgrade is broken.

Expected healthy signals:
- `/` returns HTTP 200 and renders the current homepage
- `/secom.php` redirects to `secom.php?/cp/login` and shows the ExpressionEngine login page
- `/api.php` returns JSON, not an Apache/PHP fatal or maintenance page

### 2) Decide whether upgrade is still in progress
Report **mid-upgrade / broken** if you see things like:
- 5xx responses
- maintenance pages
- PHP fatals / stack traces
- blank responses / connection failures
- admin login not rendering at all

If those are present, stop after a short status update.

### 3) Run the safest API smoke test
Start with unauthenticated GET/POST to `https://spainexpat.com/api.php`.

Interpretation:
- `{"error":"Invalid API key"}` = endpoint is live, routing correctly, and enforcing auth
- HTML maintenance page / PHP warning / 5xx = API not healthy

If you have a valid API key available, run exactly one read-only authenticated action, then stop.

For this SpainExpat API, the confirmed read-only path is:
- `GET /api.php?api_key=...&entry_id=<id>&strip_html=true`
- Healthy authenticated responses return a JSON object with an `entries` array

Before concluding no credential is available, check sources in this order:
- relevant env vars in the current shell/session
- local keychain entries if this is a macOS operator machine
- nearby project/config files only if they are non-empty and clearly hydrated
- if local sources fail and you have Lightsail SSH access, fetch the key from the live server/db instead of giving up

#### Live-server fallback (preferred over placeholder local files)
If SSH access works, use the live server as the authority:
- SSH key path may need to be absolute in Hermes cron/profile contexts because `HOME` can be profile-scoped; use `/Users/wondermonkey/.ssh/LightsailDefaultKey-ca-central-1.pem` rather than `~/.ssh/...` if the latter fails
- inspect `/opt/bitnami/apache/htdocs/api.php` on the server to confirm the API shape and DB config
- the production `api.php` contains the DB credentials in a top-level `$db_config` array (do not assume the credentials are inline inside the `new PDO(...)` call)
- the production `api.php` validates against `exp_global_variables.variable_name = 'SECOM_API_key'`
- safest retrieval is to query the DB over SSH with a prepared statement, then use that key for exactly one read-only GET

Known-good fallback pattern:
1. SSH to the Lightsail box with the absolute key path
2. Use PHP/PDO on the server to fetch `SECOM_API_key` from `exp_global_variables`
3. Use that key in a local GET to `/api.php?api_key=...&entry_id=<id>&strip_html=true`
4. Expect HTTP 200 JSON with `entries[0].entry_id`, `title`, `url_title`, `status`

Shell pitfall:
- when passing the fetched key from shell to Python, export it as an env var and read it from `os.environ`; this avoids quote/escaping mistakes that can produce a false `Invalid API key`

Do **not** trust Dropbox/local repo placeholders; confirm file sizes before using them.

If no API key is available after those checks, do **not** fake a pass. Report:
- reachability passed
- auth enforcement confirmed
- authenticated read-only smoke test not verified
### 4) Use local artifacts only as hints
If you inspect `/Users/wondermonkey/Dropbox/Code/SECOM/ee-mcp`, verify files are not zero-byte placeholders before trusting them. Empty files there are not evidence that no implementation exists.

## Report format
Keep it operator-style:
- Upgrade status
- Which endpoints responded
- Whether API endpoint was reachable
- Whether authenticated read-only smoke test passed, failed, or was not verified

## Example good conclusion
- Upgrade appears complete
- Root homepage returns 200
- Admin login renders via `secom.php?/cp/login`
- API returns JSON auth error `Invalid API key`
- Reachability pass; authenticated read smoke test not verified because no credential was available

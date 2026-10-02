# Dream Cycle v3 — real conversation compression (Nagatha)

Status: **prepared, not activated.** Nothing in this change runs until the owner installs the two
artifacts beside this file. The default for every new field is off.

## What was wrong

`dream_cycle_v3/daily_compression.py` compacts the continuity **store**: deterministic bookkeeping
over rows the collector already wrote, explicitly metadata-only, no transcript digest, no session
mutation. It never touched the conversation. The receipt called it "compression", so a morning
message reported compression that had not happened.

## What the repair does

The cycle now *asks the gateway that owns the conversation* to run the compression that already
exists, and reports only what the answer proves.

- `gateway/scheduled_compression.py` — new `compress-session` control verb. It drives the existing
  manual-`/compress` path (`GatewaySessionCommandsMixin._run_manual_compression` →
  `agent.conversation_compression_manual.compress_now` → the real `ContextCompressor`) **inside the
  gateway process**, so the rotation/in-place rewrite and the cached-agent eviction are done by the
  owner. No offline `state.db` edit, no cached-agent divergence.
- No new summarizer, no reflection store, no new nightly inference path of its own. The handoff
  summary the compressor already writes is what preserves constraints, corrections, key decisions,
  blockers and the live user ask; `## Active Task`/tail protection is what keeps the current request
  and prevents a closed thread from resurrecting.
- A busy conversation **defers** (`turn_running`, `compression_in_flight`) and is never interrupted
  or forced. A short conversation defers rather than spending a summarization.
- Idempotent per cycle: the outcome is stamped on the routing entry's own metadata
  (`set_session_metadata`, which does not advance the activity clock), so a retry the same night
  reports `already_applied`. A deferral is not stamped as settled, so it stays retryable.
- Only the cycle's own profile is eligible. On a multiplexer the namespace slot (`agent:<profile>:`)
  decides; on a single-profile gateway the keys are the legacy `agent:main`, so the request must name
  that gateway's own profile or it is refused outright.
- `dream_cycle_v3/conversation_compression.py` — the client. `claim` is `applied` only for rows the
  gateway reported as applied **and** that carry before/after token and message counts. An absent,
  pending, or evidence-free answer degrades to `unavailable`/`deferred`. A stored request is never an
  outcome.
- The status line now reads `continuity=<outcome>/rev<N> conversation=<claim>[counts]`. The old
  `compression=` token is gone: it was the misleading label.
- Report rows carry a `session_ref` digest, never the session key (it embeds chat and user ids).
  Highlight bullets pass through the dream-cycle sanitizers (secretguard + PII redaction + caps).

## Activation (owner)

1. Install the wrapper artifact over the live shim (back it up first):
   ```
   cp ~/.hermes/profiles/nagatha/scripts/dream_cycle_v3_live_cron.sh \
      ~/.hermes/profiles/nagatha/scripts/dream_cycle_v3_live_cron.sh.bak
   cp <repo>/docs/dream-cycle-v3/20261002-nagatha-live-cron.sh.artifact \
      ~/.hermes/profiles/nagatha/scripts/dream_cycle_v3_live_cron.sh
   chmod +x ~/.hermes/profiles/nagatha/scripts/dream_cycle_v3_live_cron.sh
   ```
2. Apply `20261002-nagatha-live-job-patch.json` to job `42cd1ef532ac` (prompt only; schedule stays
   `30 6 * * *` Europe/Madrid).
3. **Reload:** the gateway must be restarted once, because the `compress-session` verb is registered
   at control-socket startup (`gateway/run.py::_start_gateway_start_control_socket`). Until then the
   verb is unknown and the cycle records `claim: unavailable` — honest, and harmless. The wrapper and
   the job prompt need no reload (the cron ticker reads the script and `jobs.json` per run).
4. Dry check without compressing anything, against the running gateway:
   ```
   python - <<'PY'
   from pathlib import Path
   from gateway.control_socket import compress_gateway_session
   print(compress_gateway_session(Path.home()/".hermes", profile="nagatha",
                                  cycle_id="probe", limit=1, dry_run=True))
   PY
   ```
   `dry_run` reports `eligible` rows and touches no transcript. A `None` answer means that home's
   gateway is not the owner (try `~/.hermes/profiles/nagatha`) or it predates the verb.

### Tested concrete payload

One JSON line in, one out, over the gateway's existing control socket (exercised in
`tests/gateway/test_scheduled_compression.py::test_control_verb_round_trips_the_cycle_over_the_real_wire`):

```json
{"verb": "compress-session", "id": 7, "protocol": 1,
 "params": {"profile": "nagatha", "cycle_id": "2026-10-01", "limit": 1}}
```
```json
{"ok": true, "protocol": 1, "id": 7,
 "result": {"ok": true, "profile": "nagatha", "cycle_id": "2026-10-01", "dry_run": false,
            "eligible": 1, "counts": {"applied": 1},
            "sessions": [{"session_ref": "…", "platform": "telegram", "chat_type": "dm",
                          "status": "applied", "before_tokens": …, "after_tokens": …,
                          "before_messages": …, "after_messages": …, "rotated": false,
                          "highlights": {"open_threads": […], "learnings": […]}}]}}
```
Optional params: `session_keys` (exact keys instead of "most recent"), `active_minutes`,
`include_highlights: false`, `timeout`, `dry_run`.

## Rollback

Fully reversible, in increasing order of effect:

1. **Stop requesting it** — unset `DC3_CONVERSATION_COMPRESSION` in the live shim (or restore the
   `.bak`). The cycle returns to continuity bookkeeping only; nothing else changes. No reload needed.
2. **Remove the verb** — revert the commit and restart the gateway. The control socket goes back to
   four verbs; the dream-cycle report section becomes `{"enabled": false}`.
3. **Already-compressed conversations** — compression is the normal, sanctioned history mutation, so
   there is nothing to undo per se. The pre-compression segment stays readable: in-place compaction
   soft-archives the old rows (`compression.in_place: true`, the default) and a rotating
   configuration leaves the parent session intact under its own id. `/undo` and `/branch` work as
   they always do.

## What the tests prove, and what they do not

`tests/gateway/test_scheduled_compression.py` runs the real chain against a real `SessionStore` and
`state.db` under a temp home, with a real `AIAgent`/`ContextCompressor`: scheduler → compression →
persisted continuation → the next turn's `load_transcript` of the live id. Only
`ContextCompressor._call_summary_llm` is a fixture, so this is a **simulated model boundary** — the
plumbing is proven, summary *quality* is not and needs one real live run to judge. Nothing here has
been run against the live Nagatha profile, gateway, or Telegram.

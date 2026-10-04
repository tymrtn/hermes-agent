# Dream Cycle v3: deterministic daily brief and reconciled handoff

Follows `20261002-real-conversation-compression-activation.md`.

## Two failures this fixes

1. **Raw metrics delivered.** No-agent jobs delivered the wrapper's stdout, which was the machine
   status line (`dream-cycle-v3 cycle ok ... sources=0 excluded=107 ...`).
2. **Stale blocker in a correct-looking receipt.** The compressor keeps the latest user message and
   everything after it verbatim, and by contract that tail wins over the summary. The receipt was
   built from the summary's `## Blocked` alone, so it reported a blocker that the final, verbatim
   exchange had already resolved. On 2026-10-04 the handoff covered turns up to about 01:04, and
   the fix landed at 01:11 inside the retained tail.

## What changed

- `gateway/scheduled_compression.py`: the scheduled run sets `reconcile_with_retained_tail` on
  its throwaway agent's compressor. The single summarizer call also receives the verbatim tail as
  reference, and the prompt tells it to write `## Active State` / `## Blocked` as of the end of
  that tail. There is no extra call and no change to manual `/compress` or auto-compression. The
  tail stays verbatim. Highlights gain one-heading brief sections (`active_task`, `open_items`,
  `corrections`) plus `latest_reply`, the newest assistant text the handoff retained.
- `dream_cycle_v3/daily_brief.py`: renders the brief from the cycle report and this attempt's
  conversation record. It adds no model call, invents no thread IDs or priorities, and emits no
  counters, paths or JSON. The compression line states only what the record proves (applied with
  before/after figures, already applied, deferred with reason, unavailable, failed, not requested).
- `dream_cycle_v3 run --output brief|status`. `scripts/dream_cycle_v3_run.sh` defaults to `brief`.
  The status line goes to stderr, and the JSON reports are unchanged. `DC3_OUTPUT=status` restores
  the old stdout.

## Activation

- The renderer and wrapper load per cron run, so no reload is needed.
- Reconciliation lives in the gateway, so it applies only after that profile's gateway restarts.
  Until then the brief labels the older handoff format "compacted history; may predate the final
  turns".
- An agent job that only reformatted the script output can become `no_agent: true`, and its stdout
  is then delivered verbatim.

## Rollback

1. `DC3_OUTPUT=status` in a profile shim restores the raw line. No reload is needed.
2. A job switched to `no_agent: true` can be switched back with `hermes cron edit`.
3. Reverting the commit and restarting the gateway removes reconciliation. Compression itself is
   unaffected.

## Limits

Tests prove the plumbing: the summarizer input carries the tail, and the tail stays verbatim.
Whether the summarizer actually drops a resolved blocker depends on the model, and only a live
night can show that. Plugin context engines keep their own policy, so the flag is ignored there.

# Dream Cycle v3 — Nagatha shadow cron publication checklist

Status: INACTIVE. Nothing in this directory activates anything by being
merged; every step below is an explicit operator action performed after
independent review. Verified by
`tests/dream_cycle_v3/test_cron_contract.py`.

Companion artifacts (tracked in this repo):

- `docs/dream-cycle-v3/nagatha-shadow-cron.job.json` — the exact cron
  definition, shipped `"enabled": false`, `"state": "paused"`.
- `scripts/dream_cycle_v3_shadow_cron.sh` — the no-agent shim the job
  runs: SHADOW mode only, real Nagatha paths, computed one-day UTC
  window, execs the tracked wrapper `scripts/dream_cycle_v3_run.sh`.

## Hard rules

1. The live legacy v2 job (`dream-cycle-v2`) and the separate productivity
   job live in the Nagatha cron store
   `$HOME/.hermes/profiles/nagatha/cron/jobs.json`. Store ownership is
   distinct from each job's execution `profile` field: as observed on
   2026-07-13, legacy job `0ec6fab53a91` executes as `default` on schedule
   `30 2 * * *`, while productivity job `3165dba05f75` executes as
   `nagatha`. Both are enabled. They MUST NOT be paused, removed, or
   modified by any step in this checklist; disabling either is a separate,
   later cutover decision per design §13. These identifiers are a snapshot:
   re-verify the store, job ids, execution profiles, and schedules with
   `hermes -p nagatha cron list --all` at publication time rather than
   trusting this document.
2. Cutover requires the `cutover-gate` subcommand to pass, and the gate
   refuses until seven distinct elapsed shadow days are evidenced in the
   shadow store (successful `runtime_cycle_completed` events on seven
   wall-clock dates). `shadow-replay` / `historical-replay` output is
   accelerated historical evidence and is NOT a substitute for those
   seven days.
3. The v3 RUNTIME performs no destination promotion and writes only
   under `$HOME/.hermes/dream-cycle/v3-shadow`. Memory, skills,
   projects, and task trackers are never written. The SCHEDULER
   additionally owns artifacts of its own (see "Scheduler-owned
   artifacts" below) — rollback must clean those up too.
4. Never edit jobs.json by hand. All cron store changes go through the
   `hermes cron` commands, which hold the store lock and save
   atomically; a raw edit bypasses both and can corrupt or race the
   scheduler. Every command below is profile-qualified because cron
   stores are per-profile — an unqualified command inspects or mutates
   whatever profile the shell happens to resolve.

## Scheduler-owned artifacts (what publication actually touches)

- `$HOME/.hermes/profiles/nagatha/cron/jobs.json` — the Nagatha profile
  cron store, updated by `hermes -p nagatha cron ...` (created by
  hermes with the proper `{"jobs": [...]}` shape if absent).
- `$HOME/.hermes/profiles/nagatha/cron/output/<job id>/` — per-run
  output documents the scheduler writes each time the job fires.
- `$HOME/.hermes/profiles/nagatha/scripts/dream_cycle_v3_shadow_cron.sh`
  — the installed shim.
- `$HOME/.hermes/dream-cycle/v3-shadow/` — everything the runtime writes
  (lock, manifests, store, reports).

## Publication steps (operator, after review)

1. Place the operator-reviewed inputs at the real intended locations:
   - `$HOME/.hermes/dream-cycle/v3-config/projects.json`
   - `$HOME/.hermes/dream-cycle/v3-config/threads.json`
2. Optionally pre-seed the shadow store with the `seed-store` seam
   (idempotent; confined to the shadow root):

   ```
   cd $HOME/.hermes/hermes-agent
   python3 -m dream_cycle_v3 seed-store \
     --v3-root $HOME/.hermes/dream-cycle/v3-shadow \
     --as-of <current ISO-8601 UTC timestamp> \
     --registry $HOME/.hermes/dream-cycle/v3-config/projects.json \
     --threads $HOME/.hermes/dream-cycle/v3-config/threads.json
   ```

3. Install the shim (cron scripts must live inside the profile scripts
   directory):

   ```
   install -m 0755 $HOME/.hermes/hermes-agent/scripts/dream_cycle_v3_shadow_cron.sh \
     $HOME/.hermes/profiles/nagatha/scripts/
   ```

4. Run the installed shim once by hand and confirm it prints exactly one
   `dream-cycle-v3 cycle ok mode=shadow ...` line and touched nothing
   outside `$HOME/.hermes/dream-cycle/v3-shadow`.
5. Create the job through the supported profile-scoped workflow, then
   pause it in the SAME sitting. The tracked definition
   (`nagatha-shadow-cron.job.json`) is the reference to verify against;
   it is never appended raw:

   ```
   hermes -p nagatha cron create "30 6 * * *" \
     --name dream-cycle-v3-shadow-nagatha \
     --script dream_cycle_v3_shadow_cron.sh \
     --no-agent --deliver local
   hermes -p nagatha cron pause <job id printed by create>
   ```

   `-p nagatha` selects the Nagatha cron STORE for this command; it does
   not set the created job's per-job execution `profile` field, and there
   is no supported `cron create` flag that does. The created job's
   `profile` will be `null` (it runs as a plain no-agent script, so it
   needs no profile-scoped agent execution) — the tracked definition
   reflects this; do not "fix" it to `"nagatha"` without first confirming
   the CLI grew a supported flag for it.

   The create-then-immediately-pause is safe only because the schedule's
   `next_run_at` is in the future — verify explicitly that the
   `next_run_at` printed by create is a future timestamp before running
   anything else, and pause before leaving the session. If the printed
   fire time is not comfortably in the future, pause first and
   investigate before proceeding.
6. Verify the Nagatha cron store explicitly:
   - `hermes -p nagatha cron list --all` shows the new job
     `[paused]`, `no-agent`, script `dream_cycle_v3_shadow_cron.sh`,
     schedule `30 6 * * *`, matching the tracked definition.
   - The same `hermes -p nagatha cron list --all` output shows legacy v2
     job `0ec6fab53a91` and productivity job `3165dba05f75` unchanged and
     still enabled on their existing schedules and execution profiles.

## Activation (later; still long before any cutover)

7. When ready to start the shadow week, activate ONLY the new job:
   `hermes -p nagatha cron resume <job id>`. This begins nightly
   shadow cycles; nothing live changes.
8. Let it run for at least seven distinct elapsed shadow days, checking
   `$HOME/.hermes/dream-cycle/v3-shadow/reports/` for the daily cycle
   reports.
9. Produce the replay evidence and evaluate the gate (both exit nonzero
   on failure):

   ```
   cd $HOME/.hermes/hermes-agent
   python3 -m dream_cycle_v3 shadow-replay \
     --profile nagatha --owner nagatha \
     --root v2-state=$HOME/.hermes/dream-cycle/state/nagatha \
     --root v2-runs=$HOME/.hermes/dream-cycle/runs \
     --root sessions=$HOME/.hermes/profiles/nagatha/sessions \
     --v3-root $HOME/.hermes/dream-cycle/v3-replay \
     --registry $HOME/.hermes/dream-cycle/v3-config/projects.json \
     --threads $HOME/.hermes/dream-cycle/v3-config/threads.json \
     --migrate-v2-root v2-state --migrate-v2-root v2-runs \
     --smoke-message "<representative first message>" \
     --smoke-expect-project <project-id> --smoke-require-thread \
     --start-date <d0> --end-date <d0+7>

   python3 -m dream_cycle_v3 cutover-gate \
     --db $HOME/.hermes/dream-cycle/v3-shadow/continuity.db \
     --replay-summary $HOME/.hermes/dream-cycle/v3-replay/reports/historical-replay-<d0>_<d0+7>.json \
     --shadow-report $HOME/.hermes/dream-cycle/v3-shadow/reports/<latest run_id>.json
   ```

   The shadow report handed to the gate must come from a cycle with the
   retrieval smoke configured (add the `DC3_SMOKE_*` variables for one
   manual run of the shim, or pass the `--smoke-*` flags to a manual
   `run --shadow` invocation).
10. Cutover itself (promotion, v2 retirement) remains a separately
    approved decision outside this checklist; nothing here authorizes it.

## Rollback (complete cleanup, in increasing strength)

1. `hermes -p nagatha cron pause <job id>` — stops firing;
   everything else stays for inspection.
2. `hermes -p nagatha cron remove <job id>` — removes only the v3
   job from the Nagatha profile store.
3. Delete the scheduler's run output for this job:
   `$HOME/.hermes/profiles/nagatha/cron/output/<job id>/`.
4. Remove the installed shim
   `$HOME/.hermes/profiles/nagatha/scripts/dream_cycle_v3_shadow_cron.sh`.
5. Delete or archive `$HOME/.hermes/dream-cycle/v3-shadow` — all runtime
   state (lock, manifests, store, reports) lives under it.

The legacy v2 job and the Nagatha productivity cycle are never touched
at any step above.

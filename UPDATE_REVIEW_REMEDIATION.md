# Update review remediation

Validated on 2026-09-05 in `/private/tmp/hermes-update-latest-20260905T072632Z`.
Original merge: `e283bff928298453baa609716ac758f64dbe4736`.

Merge parents retained by the amendment:

- `1432c35e327734c0f3d2a32510995855e554a731`
- `f58fcc8118d9db092ad60d363d4a28520e08ac5a`

## Repairs

1. `run_one_job` resolves execution identity in order: explicit argument, job record, newly created attempt. It writes the resolved ID into the job before handoff. Existing attempts are not duplicated.
2. Manual `cronjob` runs, built-in ticker dispatch, and external provider dispatch use `cron.scheduler_profile.run_one_job_profiled`. The extracted sibling owns the restored bounded reader/writer lock and profile context; no compatibility shims were added. Execution uses the selected profile's config, secret scope, and script environment. Profile-pinned delivery does not reuse the caller's adapters or event loop. The process-wide `HERMES_HOME` is not changed. Subprocesses receive the context-local home through the existing environment builder, and scoped values pass through the existing credential scrub.
3. The job owner's cron store remains pinned for job updates, execution ledger writes, shutdown ownership tracking, and detached-worker handoff. The selected execution profile does not acquire the owner's job or execution history.
4. Scheduler tests import moved symbols from their defining siblings and patch current runtime bindings. Behavioral coverage exercises real script subprocesses and temporary profiles through manual and scheduled dispatch, with multiplexing both off and on. It checks config, credentials, child environment, unrelated-thread home/config isolation, environment restoration, delivery isolation, owner-store bookkeeping, and execution identity. Existing worker coverage now checks distinct execution and storage homes.

Related test drift was also repaired: explicit full-memory opt-in, delivery assertions based on content rather than an obsolete header, the current session DB acquisition seam, the current watchdog interface, script-timeout keyword arguments, and background-thread mock lifetime. The heartbeat grace test now permits a delayed first retry to exhaust the grace window instead of assuming a minimum number of scheduling intervals.

## Final validation

All commands below completed successfully before the merge amendment.

```sh
scripts/run_tests.sh tests/cron/test_scheduler.py tests/cron/test_scheduler_profile.py tests/cron/test_execution_ledger.py tests/cron/test_restart_safe_worker.py tests/cron/test_scheduler_provider.py tests/cron/test_cron_profile_isolation.py tests/cron/test_cron_home_target_profile_scope.py tests/cron/test_cron_multiplex_profile_route_preflight.py tests/cron/test_cron_script.py tests/cron/test_script_timeout_override.py tests/cron/test_script_claim_heartbeat.py tests/cron/test_cron_no_agent.py tests/tools/test_cronjob_tools.py tests/tools/test_cronjob_run_immediate.py tests/tools/test_cronjob_run_background.py tests/tools/test_cronjob_run_delivery_notice.py --file-retries 0 --tb=short
```

Exit 0: **16 files, 406 passed, 0 failed, 3 skipped**, 21.3 seconds, 36 workers. Retries were disabled. Two Linux-only worker tests and one Windows-only script test were skipped on this macOS host. The live managed-systemd worker test therefore remains a Linux CI validation, not a claimed local result.

The final run used approved process visibility so the live-system guard could identify and clean up the tests' own child processes. Earlier sandboxed script runs failed that guard; it was not disabled to obtain the final result.

```sh
python3 -m py_compile cron/executions.py cron/scheduler.py cron/scheduler_delivery.py cron/scheduler_profile.py cron/scheduler_provider.py cron/scheduler_script.py tests/cron/test_cron_no_agent.py tests/cron/test_restart_safe_worker.py tests/cron/test_scheduler.py tests/cron/test_scheduler_profile.py tests/cron/test_script_claim_heartbeat.py tests/cron/test_script_timeout_override.py tests/tools/test_cronjob_run_background.py tests/tools/test_cronjob_run_immediate.py tools/cronjob_tools.py
git diff --check
```

Both exit 0, no output: all **15 touched Python files compile**, and the diff has no whitespace errors. The staged diff is checked again before amendment.

Intermediate regression evidence: the owner-store shutdown assertion failed in all four manual/scheduled × multiplex cases before the ownership-tracking fix (4 failed, 4 passed). Those cases pass in the final suite. Earlier full scheduler runs exposed a stale mocked timeout loop and stale imports/expectations; the repaired scheduler file passes all **124 tests**.

## Clawpatch review

- `clawpatch doctor`: passed; provider `codex`, local CLI `0.153.4`.
- `clawpatch map --source heuristic`: completed, 1,051 features.
- Targeted review: `feat_library_c2cb84b7b1`, one feature, one review job, Codex provider.
- Finding `fnd_sig-feat-library-c2cb84b7b1-e789_69db2511bc`: process-wide profile home mutation. Removed the mutation and added unrelated-thread regression coverage.
- `clawpatch revalidate --finding fnd_sig-feat-library-c2cb84b7b1-e789_69db2511bc --provider codex`: exit 0; **fixed=1, open=0, uncertain=0**. The reviewer could not execute tests inside its own read-only sandbox; the canonical test results above were obtained separately.
- `clawpatch report`: the finding is marked **fixed**.

The first Clawpatch launch was blocked by sandbox app-server initialization; the approved retry succeeded. No provider or credential configuration was changed.

## Scope

Only remediation files and this report are included in the merge amendment. Existing reconciliation prompts/results and `.clawpatch/` remain untracked. No reset, abort, remote change, push, service restart, or OpenRouter use was performed.

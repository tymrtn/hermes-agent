---
name: governor-real-hermes-cutover
description: Implement real Governor in Hermes/OpenClaw after a security incident. Use when the user wants actual Governor enforcement, not a temporary regex firewall or invented shadow layer.
version: 1.0.0
author: Nagovernor
license: MIT
metadata:
  hermes:
    tags: [governor, hermes, openclaw, terminal, governance, security, dogfood]
    related_skills: [writing-plans, test-driven-development, systematic-debugging]
---

# Governor Real Hermes Cutover

Use this when the user wants real Governor wired into Hermes/OpenClaw terminal execution.

Do not use this skill for emergency containment heuristics. If you are about to invent a "Governor-style" guard, stop.

## Hard-won corrections

1. Do not claim a Governor integration exists unless you verified the actual files.
2. Do not invent `governor_shadow.py` or any proxy layer and call it Governor.
3. Do not build a regex firewall and present it as Governor.
4. Before integrating around any Governor feature, inspect the current Governor code and confirm it matches the user's intended operating model. Do not treat existing code paths as product truth automatically.
5. For Nagovernor specifically, default to the simpler operator model unless the user explicitly asks for more:
   - route execution through real Governor
   - allow => run
   - deny => stop and escalate to Tyler
   - do not add Hermes-side probing, ticket workflows, or other ceremony unless requested
6. Real Governor means:
   - real Governor binary/engine
   - real attribution
   - real routing semantics that match the user's intended operator flow

## What research found

### Governor is currently split across multiple models

There are 4 materially different things called Governor in the repo/ecosystem:

1. `governor2` Rust CLI/library
   - This is the real blind attribution scoring core.
   - Key files:
     - `governor2/src/bin/governor.rs`
     - `governor2/crates/governor/src/catalog.rs`
     - `governor2/crates/governor/src/routing.rs`
     - `governor2/crates/governor/src/hooks.rs`

2. `governor2/catalogs/*.yaml`
   - Useful tool-family YAML catalogs.
   - Format is flat zero-sum attribute lists.
   - OpenClaw plugin can read these.
   - The Rust CLI does **not** actually consume them today.

3. `public/governor`
   - Different Python heuristic/regex model.
   - Not the same as blind declared-attribute Governor.
   - Do not confuse this with `governor2`.

4. `openclaw-governor`
   - Dashboard/catalog tooling.
   - Not runtime enforcement by default.

### Governor Rust realities to remember

- The Rust library has real shell zones:
  - `Allow`
  - `Review`
  - `Deny`
- File: `governor2/crates/governor/src/routing.rs`
- Historically the CLI execution path still used binary allow/deny semantics.
- Historically the CLI also allowed ungoverned passthrough if there was no final `--` separator.

### Hermes realities to remember

There is no real Governor integration in Hermes by default.

Main shell execution surfaces:
- `tools/terminal_tool.py`
- `tools/environments/local.py`
- `tools/process_registry.py`
- `gateway/run.py` `/approve` replay path
- CLI quick command exec path in `cli.py`
- gateway quick command exec path in `gateway/run.py`
- persistent shell mode in `tools/environments/persistent_shell.py`
- `mini_swe_runner.py`
- `tools/code_execution_tool.py` is a broader execution bypass unless constrained/disabled

## The key product-security lesson

Even if Hermes wraps commands and sends attrs to Governor, that is not a strong security boundary if Hermes can lie about attrs.

Gemini product-security review produced these important principles:

1. Wrapping execution through Governor is directionally correct.
2. But if Hermes is the only thing deriving attrs, Hermes is still effectively the policy engine.
3. Review approval must use a real approval ticket/receipt, not a spoofable `--review-ok` style flag.
4. `execute_code` is a critical bypass if left unconstrained.
5. Persistent shells are hard to govern honestly; disabling them in governed mode is the clean initial move.
6. Allowing attr-less commands by default is acceptable for initial dogfood plumbing, but not a long-term production posture.

## Practical target architecture

### Phase 1: make Governor itself usable for Hermes dogfood

Use the real Rust Governor binary.

Preferred shape:
- Governor gets a dedicated shell-facing subcommand or equivalent governed execution path.
- It must:
  - derive sparse shell attrs internally from the raw command/context
  - return `allow/review/deny`
  - emit structured JSON
  - issue a review ticket for review-band commands
  - require that ticket for reviewed execution

Important:
- Do not rely on a plain `--review-ok` flag.
- Use a stateful review ticket that is specific to:
  - command
  - cwd/context
  - attrs
  - justification
  - expiry

### Phase 2: route Hermes terminal execution through Governor

Create a small Hermes adapter, e.g. `tools/governor_exec.py`, that:
- resolves the Governor binary path
- asks Governor for preflight decision
- wraps actual terminal execution through Governor
- passes review tickets back into Governor when approved

Then wire that into:
- `tools/terminal_tool.py`
- `/approve` replay in `gateway/run.py`

### Phase 3: close obvious bypasses for honest dogfooding

At minimum in governed mode:
- disable or govern CLI quick command exec
- disable or govern gateway quick command exec
- disable persistent shell mode unless every command still passes through Governor
- treat `execute_code` as a separate bypass and either disable it or explicitly document that terminal-only governance is not full execution governance

## Good initial shell attrs for sparse dogfood

These are useful as the first sparse shell set:
- `user_requested`
- `agent_created`
- `read_only`
- `destructive`
- `network_access`
- `local_only`
- `reversible`
- `scripted`

Useful next-wave protection attrs:
- `workspace_scoped`
- `known_safe_binary`
- `constrained_scope`
- `test_or_lint`
- `dry_run_mode`
- `owner_safe_path`
- `sensitive_path_access`
- `hidden_scope_expansion`
- `credential_hunt_pattern`
- `recursive_home_scope`
- `privileged_exec`
- `remote_exec`

## Safe dogfood threshold lesson

For initial dogfood where attribution is sparse, it is reasonable to allow score `0.0`.

A practical starting shape is:
- allow `>= 0.00`
- review `>= -0.20`
- deny `< -0.20`

Why:
- sparse coverage should not brick the operator
- obvious negative context still routes to review/deny
- later, once catalog coverage is robust, attr-less/unknown actions should likely move toward review, not allow

## OpenClaw integration lesson

OpenClaw can intercept generic tool calls with `before_tool_call`.

Important file/runtime locations found during research:
- current plugin code:
  - `openclaw-governor/index.ts`
  - `openclaw-governor/src/attribution.ts`
  - `openclaw-governor/src/tools.ts`
  - `openclaw-governor/src/routes.ts`
- installed OpenClaw runtime/types under `~/.openclaw/.../node_modules/openclaw/...`

Important limitation:
- `before_tool_call` can modify params or block
- it does **not** natively support true `review/pending approval/resume`
- so review must currently be implemented as:
  - block + persist pending review + notify operator
  - later rerun explicitly after approval
- true transparent review/resume would require an OpenClaw core extension

## What not to do again

- Do not create `tools/governor_shadow.py` and present it as real Governor.
- Do not add narrow regex deny rules and call that Governor.
- Do not say “fully governed” while `execute_code`, persistent shells, or quick exec paths still bypass the system.
- Do not forget `/approve` replay — it is a real bypass if it calls terminal with `force=True` and no Governor ticket.

## Recommended execution order

1. Research actual Governor code and contracts.
2. Write a plan before changing Hermes.
3. If resuming after a bad or overbuilt attempt, archive the dirty state first:
   - create dedicated archive branches in both repos
   - commit the full dirty tree to those branches
   - switch active worktrees back to clean `main`
   - do not keep arguing with a confused cutover in-place
4. Update Hermes to the latest upstream baseline before resuming integration work.
5. Save a continuity plan outside the repo if needed (for example under `~/.hermes/plans/`) so future sessions can recover context after self-update or context loss.
6. Get a skeptical product-security review.
7. Improve Governor binary/CLI first so it can support the intended operator model honestly.
8. Design a generic governor adapter seam in Hermes instead of hardwiring governor2-specific workflow into harness files.
9. Use Codex for substantive Hermes/Governor development, especially when touching sensitive harness files.
10. Add the Hermes adapter.
11. Route terminal tool through the adapter.
12. Fix `/approve` replay only if the chosen operator model actually requires it.
13. Disable/close obvious bypasses for dogfood mode.
14. Verify in a real session.
15. Then delegate richer catalog/scoring work to Codex or another coding agent.

## Recovery / reset pattern that worked

When an integration pass drifted away from the user's intended model, the clean recovery path was:
- archive both repos on explicit rollback branches
- commit the dirty trees so nothing is lost
- reset active worktrees to clean `main`
- fast-forward Hermes local `main` to latest `origin/main`
- write a short continuity plan capturing:
  - clean baseline commit
  - archived branch names / commits
  - next-session startup steps
  - rules for future implementation

Use this pattern again if a Governor/Hermes cutover becomes overbuilt, misaligned, or hard to reason about.

## Sensitive-files rule learned the hard way

Treat these Hermes files as sensitive harness surfaces and plan before editing them:
- `run_agent.py`
- `model_tools.py`
- `cli.py`
- `gateway/run.py`
- `tools/terminal_tool.py`
- `hermes_cli/config.py`
- `toolsets.py`

For those files:
- branch first
- keep diffs minimal
- prefer a generic adapter boundary over direct product-specific wiring
- use Codex for implementation work

## Verification checklist before claiming dogfood

Do not claim Nagovernor is fully governed unless all are true:
- terminal execution goes through Governor, not raw subprocess paths
- review-band commands require a Governor-issued ticket
- `/approve` replay still runs through Governor
- quick exec paths are disabled or governed
- persistent shell is disabled or honestly governed
- you have explicitly addressed `execute_code` as in-scope or out-of-scope
- you tested normal operation in a real governed session

## Hermes implementation details learned during dogfood

Concrete Hermes cutover work needed more than just `tools/terminal_tool.py`.

Important practical changes:
- `cli.py`
  - fix the quick-command exec branch first if prior edits left it syntactically broken
  - bridge `governor.enforce`, `governor.bin`, and `governor.dogfood` from config into env vars
  - disable quick-command `type=exec` when Governor enforcement is enabled
- `gateway/run.py`
  - bridge the same governor config values into env vars on startup
  - disable gateway quick-command `type=exec` when Governor enforcement is enabled
  - preserve Governor review semantics across `/approve`
    - if the gateway approval flow simply unblocks the already-waiting agent thread,
      that is acceptable and avoids a separate replay bypass surface
    - if there is an explicit replay path, it must call
      `terminal_tool(..., force=False, governor_review_ticket=...)`
      instead of `force=True`
- `hermes_cli/config.py`
  - add a first-class `governor` config section:
    - `enforce`
    - `bin`
    - `dogfood`
    - `allow_execute_code`
  - sync those values for code paths that still read env vars directly
- `tools/terminal_tool.py`
  - in governed mode, skip legacy regex/Tirith command gating for the terminal path so Governor owns allow/review/deny semantics
  - in governed mode, force `local_persistent=False` and `ssh_persistent=False`
    because persistent shells are an honest bypass of per-command Governor review

## Test and verification notes learned during dogfood

Additional hard-won caution:
- When delegating this cutover to Codex or another coding agent, review the diff skeptically before accepting it.
- A plausible-looking implementation may still regress to Hermes-side pattern approvals (`pattern_key`, session allowlists, or `approved=True` replay flags) instead of preserving Governor's own review-ticket semantics.
- Reject any change that:
  - reuses the old dangerous-command approval queue as the actual authorization artifact
  - treats a Governor preflight as sufficient without replaying the real Governor-issued ticket
  - synthesizes attrs/approval state in Hermes and calls that governed replay

Live verification pitfall:
- End-to-end smoke scripts that include an inline deny-case command like `rm ...` can trip Hermes' outer cron/job approval layer before the governed inner test even runs.
- For autonomous verification, probe allow/review/deny cases separately so the wrapper command itself does not get blocked before the Governor path is exercised.

Add targeted tests for:
- quick-command exec disabled in governed mode (CLI + gateway)
- `/approve` replay uses `force=False` and preserves `governor_review_ticket`
- local/SSH persistent shell config resolves to disabled when Governor is enforced
- governed tool exposure hides `execute_code` by default and hard-blocks direct `handle_function_call("execute_code", ...)` dispatch unless explicitly overridden
- terminal-tool governed allow/review behavior
- shell attribution edge cases inside Governor itself:
  - `~/.ssh` paths must not falsely trip `ssh` remote/network detection
  - `head -n 1` must not be misclassified as `dry_run_mode`
  - direct private-key reads like `cat ~/.ssh/id_rsa` must land below allow and require review

Current useful Hermes targeted bundle (local dogfood cutover):
- `python -m pytest -o addopts='' tests/tools/test_governor_mode.py tests/cli/test_quick_commands.py tests/tools/test_process_registry.py -q`
- `python -m pytest -o addopts='' tests/gateway/test_approve_deny_commands.py tests/tools/test_terminal_timeout_output.py tests/tools/test_terminal_exit_semantics.py -q`
- `python -m pytest -o addopts='' tests/test_model_tools.py -q`

In this Hermes repo, targeted pytest may need:
- `./venv/bin/python -m pytest -o addopts='' ...`

Reason:
- `pyproject.toml` sets `-n auto`, and environments without xdist will fail before tests even run.
- If you are dogfooding under a profile that already exports `HERMES_GOVERNOR_ENFORCE=1`, tests that assert default quick-command or persistent-shell behavior must explicitly clear that env var with `monkeypatch.delenv(...)` or they will fail for the wrong reason.
- For broad pytest runs from a governed profile (for example Nagovernor cron on the local machine), clear all inherited Governor env vars in `tests/conftest.py` (`HERMES_GOVERNOR_ENFORCE`, `HERMES_GOVERNOR_BIN`, `HERMES_GOVERNOR_DOGFOOD`, `HERMES_GOVERNOR_ALLOW_EXECUTE_CODE`) so the suite starts from the normal ungoverned baseline unless a test opts in.

Empirical verification that proved the path was real:
- `terminal_tool("pwd")` under Governor enforcement should return real command output and Governor metadata showing the command actually executed
- a strong negative-path proof is to temporarily point `HERMES_GOVERNOR_BIN` at a definitely missing path and confirm `terminal_tool("pwd")` fails immediately with a Governor binary `FileNotFoundError`; if the command still succeeds, Hermes is not actually routing through Governor
- note: current Governor `shell --json` execution appends a trailing JSON envelope after command stdout/stderr, so Hermes must strip/split that footer instead of returning it as raw terminal output
- important edge case: the footer may be appended immediately after stdout with no newline separator (for example `printf`), so footer splitting must scan for a trailing Governor JSON object, not just `\n{`
- important edge case: if Hermes needs output fencing/wrappers, Governor shell must support a separate execution wrapper string (for example `--exec-command`) while keeping scoring, derived attrs, and review-ticket identity bound to the raw `--command`; otherwise attribution and replay integrity drift
- critical integrity rule: Hermes must not pass its own synthesized shell attrs (`--attr ...`) into Governor for dogfood enforcement. Doing that silently turns Hermes back into the classifier and can override hardening in Governor itself (for example re-adding `local_only`/`read_only` to commands Governor would have reviewed). Hermes should send the raw command and justification, then use Governor-returned `derived_attrs`/`attributes` for UX and approval metadata.
- important edge case: Governor review tickets are context-bound to the command and cwd; local governed paths should normalize relative cwd values (for example `.`) before preflight and replay so approved review-band commands actually execute after review
- floating-point gotcha: review-band shell scores that are mathematically zero can land as tiny signed residues (for example `-5e-18`); `shell_route()` should use a small epsilon around zero so near-zero scores still route to review instead of spuriously deny/allow
- do not accidentally collapse dogfood shell routing back to pure sign-based allow/review/deny while tuning catalogs; for this cutover the intended shell thresholds are still allow `> 0`, review `>= -0.20`, deny `< -0.20`
- after changing Governor shell scoring, rebuild the binary before re-running Hermes smoke tests; otherwise Hermes may still exercise a stale debug binary and make a correct catalog/routing fix look ineffective
- direct private-key reads like `cat ~/.ssh/id_rsa` must not silently allow; add a dedicated shell attribute (for example `credential_material_access`) and tests so key-material reads drop below allow while softer config reads like `cat ~/.ssh/config` can still remain allow or review depending on weights
- a sensitive local inspection command like `find ~/.ssh -maxdepth 2 -type f` may still silently allow under sparse catalogs even after the execution-path cutover is technically complete; treat that as a catalog/scoring gap, not proof that the execution path is fake
- Governor `shell --dry-run --json` for a review-band command must emit a real `review_ticket`, because Hermes preflight depends on that ticket for honest replay through Governor
- replaying a review-band command through `terminal_tool(..., governor_review_ticket=ticket)` should return Governor metadata showing it executed after review; verify this explicitly before claiming `/approve` replay is honest
- background execution should also be verified end-to-end via `terminal_tool(..., background=True)` plus `process_registry.wait(...)`, confirming the background output is stripped of the Governor JSON footer and still carries Governor metadata
- in Hermes, local background readers (`tools/process_registry.py`) need the same footer sanitization as foreground local execution; otherwise governed background output leaks the trailing Governor JSON envelope into `poll`/`wait`/`log` results even when the execution path itself is correct
- practical implementation seam that worked in Hermes:
  - add a small adapter module (for example `tools/governor_exec.py`) that owns:
    - `HERMES_GOVERNOR_*` env/config detection
    - Governor binary resolution
    - `governor shell --json` argv construction
    - normalization of cwd before both preflight and replay
    - trailing Governor JSON envelope splitting
  - keep `terminal_tool.py` as the orchestration layer, but keep Governor-specific parsing and argv construction in the adapter
- practical foreground implementation that worked:
  - do a Governor `--dry-run --json` preflight on the raw command first
  - if `decision=deny`, stop immediately and surface Governor metadata
  - if `decision=review`, require the real Governor-issued `review_ticket`, collect user approval, then execute through Governor with that ticket
  - for actual execution, pass the raw shell command as `--command` and Hermes' wrapped/snapshot-preserving shell string as `--exec-command` so attribution stays bound to the raw command while Hermes still preserves cwd/session behavior
- practical output-cleanup lesson from the cutover:
  - if Hermes executes the wrapped shell string through Governor, Hermes must still run its usual `_update_cwd()` / marker stripping on the returned stdout before presenting the final terminal output
  - after that, governed foreground output should still pass through the normal terminal cleanup path (`strip_ansi`, secret redaction, and sudo-failure handling) so local shell integration OSC noise like `1337;RemoteHost` / `CurrentDir` does not leak into user-visible output
  - background local execution needs the same cleanup after splitting the trailing Governor JSON envelope: sanitize any residual `__HERMES_CWD_*__` markers before `poll` / `wait` / `log` surface output
  - otherwise governed success can still leak local shell integration noise or `__HERMES_CWD_*__` markers in foreground/background output, which is a polish issue rather than a fake-path issue
- practical background implementation that worked:
  - extend `process_registry.spawn_local(...)` with Governor-aware args (`governor_bin`, `governor_review_ticket`, `governor_exec_command`)
  - when governed local background mode is active, spawn the Governor process directly instead of `bash -lic ...`
  - after the process exits, split and store the trailing Governor JSON envelope once, then surface that stored Governor metadata from `poll`, `wait`, and `log`
- practical config/workflow additions that were worth making explicit:
  - add a first-class `governor` section to Hermes config with:
    - `enforce`
    - `bin`
    - a runtime-scoped interception/apply flag if still needed
    - `allow_execute_code`
  - avoid shipping the internal term `dogfood` as a distributed config key; it was understandable during setup, but it is not clear product vocabulary for other operators and does not describe the real behavior
  - if backward compatibility is needed, keep `governor.dogfood` / `HERMES_GOVERNOR_DOGFOOD` only as deprecated aliases while introducing a clearer replacement name based on actual behavior
  - bridge those config values into env vars in both `cli.py` and `gateway/run.py` so old env-reading execution surfaces stay in sync during the cutover
  - before updating Hermes to latest upstream, commit Governor integration work to a dedicated branch or durable patch series; otherwise local governed-mode changes are easy to lose during reset/rebase/update
- practical bypass closure that proved useful:
  - in governed mode, disable CLI and gateway quick-command `type=exec` paths explicitly instead of trying to silently reroute them
  - in governed mode, hide `execute_code` from model tool definitions and also hard-block direct `handle_function_call("execute_code", ...)` dispatch unless an explicit override is set
- build both the debug path used by dogfood and a release binary (`cargo build --bin governor` and `cargo build --release --bin governor`) so the cutover is not validated only against an unbuilt or stale binary path
- when verifying Nagovernor dogfood specifically, run at least one smoke using `HERMES_HOME=/Users/tylermartin/.hermes/profiles/nagovernor` so config/env bridging is proven from the actual profile, not just ad-hoc env vars
- for profile-based dogfood smokes, explicitly clear inherited `HERMES_GOVERNOR_*` env vars before importing Hermes modules, then set only `HERMES_HOME` to the target profile. Otherwise stale parent-shell env can override the profile config you think you are testing.
- important direct-helper smoke gotcha: setting only `HERMES_HOME` is not enough if you call `terminal_tool()` or `handle_function_call()` directly from Python. First call `cli.load_cli_config()` (or otherwise apply the governor config/env bridge) so the profile's `governor.*` settings actually populate `HERMES_GOVERNOR_*` env vars before importing/exercising the execution helpers.
- a practical live-verification recipe is:
  - Governor dry-run directly on the profile for allow/review/deny probes (`pwd`, `find ~/.ssh -maxdepth 2 -type f`, `cat ~/.ssh/id_rsa`)
  - Hermes foreground smoke via `terminal_tool("pwd")`
  - Hermes review-band smoke via `terminal_tool("find ~/.ssh -maxdepth 2 -type f")` with an approval callback that returns `"once"`
  - Hermes background smoke via `terminal_tool("printf hi", background=True)` followed by `process_registry.wait(...)`
- sparse shell attribution may initially send surprisingly small commands to review; treat that as a calibration gap, not proof that the execution path is fake. In the current local dogfood state, `printf hi` has been recalibrated back into allow.
- for manual review-band smoke tests in Python, any temporary approval callback must accept the prompt signature `(command, description, *, allow_permanent=True)` (or just `**kwargs`); a bare two-argument lambda will raise inside `prompt_dangerous_approval()` and produce a false local deny that looks like a Governor replay failure
- practical Hermes gotcha: `terminal_tool()` does not accept an `_approval_callback=` keyword. For local smoke tests that need scripted approval, register it first with `set_approval_callback(...)`, then call `terminal_tool(...)`. The helper lives in `tools.terminal_tool`, not `tools.approval`. Trying to import it from the wrong module or pass `_approval_callback` directly produces a misleading failure that is not a Governor replay problem.
- practical in-process smoke gotcha: when calling Hermes helpers directly from Python, normalize return values before `json.loads(...)`. Some paths return JSON strings while others may already return Python dicts in-process (for example `process_registry.wait(...)`). A tiny `normalize()` helper avoids false-negative smoke failures caused by double-decoding instead of real Governor issues.
- when running those local Python smoke tests in the Hermes repo, activate the project venv first (`source venv/bin/activate && python ...`). Using system `python3` can fail on normal Hermes imports before the Governor path is exercised, producing false negatives unrelated to the cutover.

Critical failure mode discovered during dogfood:
- If Hermes wraps commands as `governor bash -lic <cmd> -- ...` and Governor derives attrs from the wrapped argv, Governor will score `bash` instead of the raw shell command.
- This can misclassify benign commands (for example `pwd` denied because it looked merely `scripted`) and can also mis-govern sensitive commands because the attribution basis is wrong.
- Do not claim governed execution is real until Governor has a raw-shell path that accepts the original shell command string and derives attrs from that raw command, not from the shell wrapper binary.
- Hermes background paths must use the same real review-ticket semantics as foreground. One live bug found during cutover was a stale `governor_approved` background path in `tools/process_registry.py`; it must pass `governor_review_ticket` instead.
- When adding new shell attrs to Governor catalogs (for example `sensitive_path_access`), preserve the catalog's zero-sum invariant and update catalog/scoring tests at the same time. Otherwise `cargo test -p envelope-governor` will fail even if the shell-flow code is otherwise correct.

When running the full Hermes pytest suite during this cutover, distinguish cutover regressions from unrelated baseline failures. Report both:
- targeted governed-path tests that prove the cutover
- any unrelated existing failures outside the Governor terminal path
Do not overclaim “full suite green” unless it is actually green.

Additional practical note from dogfood:
- A broad `tests/ -q` run on the current Hermes baseline may fail with repo-wide resource exhaustion (for example `OSError: [Errno 24] Too many open files`) unrelated to the Governor cutover.
- Once that happens, expect noisy secondary failures too (for example sqlite/open-temp-path setup errors in unrelated gateway/tool tests). Treat those as fallout from the same baseline exhaustion issue unless the failing stack clearly touches the governed terminal path.
- Treat that as a separate baseline/test-harness issue unless the failing stack clearly touches the governed terminal path.
- Still run the targeted Governor/terminal/quick-command/process tests and report those explicitly as the empirical cutover proof.
- If a referenced continuity-plan markdown file is missing during an autonomous resume run, do not block on it; recover from actual repo diff/status plus the governed-path tests and continue.
- Practical recovery detail: if the expected plan path under the repo-local `.hermes/plans/` is empty, also search `~/.hermes/orphaned-nested-state-*/plans/` for a recovered copy before assuming the plan is gone. On this machine, a missing Governor cutover plan was recoverable from an orphaned nested-state directory even though the canonical repo-local plan path no longer existed.
- In an autonomous resume run, do not assume more coding is needed just because the worktree is dirty. First prove whether the local cutover is already functionally complete by checking git state, running targeted governed-path tests, and doing real Nagovernor profile smokes. If those pass, treat the cutover as complete and move to the next honest gap (usually shell calibration) instead of churning the harness again.
- Gateway approval tests can be mildly flaky under concurrency. If a targeted bundle shows an isolated failure in `tests/gateway/test_approve_deny_commands.py::TestBlockingApprovalE2E::test_blocking_approval_approve_once`, rerun that test in isolation and then rerun the targeted approval bundle before treating it as a real cutover regression.
- Once the local terminal cutover is proven, the next sensible step is a parallel Codex review focused on shell catalog/scoring calibration. Give Codex explicit constraints: Governor is blind attribution scoring, not permissions gating; preserve allow for benign local output/introspection, review for sensitive-path inspection, and deny for direct credential-material access.
- If targeted governed-path tests, Governor crate/bin tests, and real Nagovernor profile smokes all pass, treat the local cutover as complete even if the broad Hermes suite is still noisy. In the current local state, the next honest post-cutover gap is repo-wide pytest resource exhaustion (`OSError: [Errno 24] Too many open files`) during `tests/ -q`; report it as a separate baseline issue, then continue with calibration work rather than reopening the terminal cutover.
- The highest-leverage post-cutover calibration risk found in practice is **representation drift between raw-shell and argv/tokenized scoring paths**. In one real review, the raw path preserved the intended anchors (`pwd` allow, `printf hi` allow, `find ~/.ssh -maxdepth 2 -type f` review, `cat ~/.ssh/id_rsa` deny) while argv/path-expanded forms drifted more permissive because `agent_created` parity and sensitive-path normalization were inconsistent. Treat that as a Governor calibration problem, not a reason to reintroduce permission-style gating in Hermes.
- Concrete post-cutover calibration gaps found in later review:
  - option-led prod mutations can still drift between raw-shell deny and argv review if subcommand extraction differs
  - positive local-introspection verb tables can drift (`du`, `df`, `printenv`, `env`)
  - credential-store normalization still needs coverage for files like `~/.git-credentials` and probably stronger treatment for `~/.config/gh/hosts.yml` / `.netrc`
  - indirect credential reads inside sensitive trees (`find ... -exec cat`, `xargs cat`, regex scans for private-key markers) still need dedicated attribution
  - secret-store directory traversal is an easy under-attribution hole: parent directories like `~/.config/gh`, `~/.kube`, and `~/.docker` may allow if only exact file paths are marked sensitive
  - raw-shell vs argv parity still needs explicit tests for benign URL literals (`echo https://example.com`, `printf '%s\n' https://example.com`) so raw-string heuristics do not spuriously add `network_access`
  - grep/ripgrep-style indirect credential hunting should not stay SSH-centric; add coverage for `.gnupg`, `.aws`, and GitHub auth stores, not just `~/.ssh`
- Practical follow-up from that Codex review: if Codex updates a markdown findings note, read the file back before trusting it. One real run produced a superficially successful note update with malformed markdown/content drift (`credential_material_access=***` and a dropped bullet header), so the safe workflow is: run Codex, then verify the written note with `read_file`, and clean up any formatting/content corruption before treating it as a real artifact.

## Scope honesty after cutover

Be explicit about scope:
- The completed Hermes dogfood cutover described here is for the local terminal backend.
- The enforcement seam is keyed off `is_governed_local_backend(env_type)`, so do not imply Docker/SSH/Modal/Daytona are equally governed unless they were separately wired and verified.
- A truthful completion statement is: local Hermes terminal execution and local background execution are routed through real Governor with real review-ticket replay.

## Remaining honesty gap after terminal cutover

Do not claim full self-governance if `execute_code` still exists as an ungoverned execution surface.
For Nagovernor dogfood, the clean initial move is to hide `execute_code` from tool definitions when Governor enforcement is on, unless an explicit `HERMES_GOVERNOR_ALLOW_EXECUTE_CODE=1` override is set.
Terminal governance can be complete while overall execution governance is still incomplete.

## Good handoff instructions for Codex

When delegating catalog work, explicitly tell Codex:
- Governor is blind attribution scoring, not command-pattern gating
- do not regress to static allow/deny firewall logic
- design zero-sum attrs and weights
- focus on robust shell/terminal governance
- distinguish benign inspection from credential/config harvesting
- include tests and examples

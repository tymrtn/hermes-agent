# Hermes preservation reconciliation

The in-progress preservation merge of `upstream/main` with the local `tyler/live` history is fully resolved and staged, without a commit. The isolated worktree remains on its existing `update/preserve-latest-20260905` branch.

- Local parent: `1432c35e32`; incoming parent: `f58fcc8118`.
- All 134 originally unmerged paths were handled, including 24 obsolete skill assets whose upstream removal was retained.
- Upstream's current facade/sibling architecture, profile isolation, routing guards, persistent Python kernels, review dispatch, and delivery accounting remain the structural base.
- No merge abort, reset, remote change, push, service restart, or modification outside this worktree was performed. The pre-existing `UPDATE_RECONCILIATION_PROMPT.md` was left untouched.

Staging encountered an empty `.git/index.lock` older than 20 minutes. `lsof .git/index.lock` and `pgrep -x git` confirmed no owner or active Git process; only that stale lock was removed before `git add`. No index contents or merge state were discarded.

## Preserved behavior

| Area | Reconciliation |
| --- | --- |
| Busy controls | Preserved stop/steer/interrupt primitives, early correction handling, queue staging before acknowledgement, compact button handles, and progress-bubble anchors. Wired Telegram callbacks through the current dispatcher with profile/topic identity intact; retained upstream authorization and generation guards. |
| Clarify | Preserved mobile button labels, typed choices, free-form/Other resolution, late Telegram answers, and atomic unrelated-message handling. Retained upstream multi-select behavior. Slack scoped callback tombstones prevent a duplicate click from consuming another workspace's prompt. |
| Sessions and AFK | Retained Telegram DM/topic continuity, parent-session handoff, restart recovery, internal follow-up routing, and opt-in AFK scheduling. Wake metadata survives serialization, recovery, compression, and branching. |
| Media and output hygiene | Preserved bounded Telegram download retry, native document delivery, explicit MEDIA directives, configurable local-file attachment, secure-delivery metadata, and cleanup of bot-owned progress/background notices. Failed photo downloads remain visible to both the user and agent. |
| Kanban | Retained multiple boards, task search, dashboard board/task deeplinks, machine-wide admission counts and locking, per-profile caps, profile fallback, deliberate worker-interruption accounting, and fail-closed clean-exit diagnostics. Kept upstream review lanes, orphan reconciliation, memory-pressure checks, and dispatch locks. |
| Models and credentials | Preserved local endpoint startup, sticky local fallback and explicit primary-model restoration, Codex subscription controls, and optional OpenRouter use. Repaired credential discovery to retain upstream's file-or-keychain behavior. |
| Identity and memory | Retained Governor identity, shared user identity, hot/warm memory, and once-per-session continuity packets. Connected CLI, gateway, ACP, TUI, and API prompt paths; failed history reads and seeded conversations cannot falsely attest a new session. |
| Executor and cron | Combined the stable per-profile Python launcher and multiprocessing support with upstream's persistent kernels, RPC/security controls, and lifecycle watchdogs. Cron creation/update now persists profile and memory-mode overrides through upstream's normalizer tables. |

## Validation

All tests ran through `scripts/run_tests.sh`, using existing dependencies and temporary `HERMES_HOME`/`TMPDIR` inside this worktree. Test subprocesses and localhost servers were exercised; no live Hermes services were restarted. Initial failures drove the repairs above; the final receipts are:

| Command/check | Result |
| --- | --- |
| Final focused suite (full command below) | **225 files: 2,483 passed, 0 failed, 9 skipped**, 135.2 seconds; no pass-on-retry flakes reported. |
| `git ls-files -u` | Empty: zero unmerged index entries. |
| `git diff --check` and `git diff --cached --check` | Clean after staging. Normalized inherited whitespace in 45 incoming files. |
| Compile changed Python paths from `git diff --name-only --diff-filter=ACMR HEAD -- '*.py'`, plus the two new modules/tests, using `py_compile.compile(..., doraise=True)` | **4,020 files compiled; zero errors.** Bytecode was written only into worktree-local temporary storage. |
| Tracked-file conflict-marker scan (`<<<<<<<`, standalone `=======`, `>>>>>>>`) | No merge delimiters remain. The only raw match is the existing `Context` heading underline in `tests/tools/test_mcp_oauth_metadata.py:10`; it is inside a docstring. |
| `scripts/check_compat_pointers.py` via the test interpreter | No production/test dependency on deprecated compat pointers. Final clean-tree check repeated after removing reconciliation scratch copies. |
| `node --check plugins/kanban/dashboard/dist/index.js` | Passed. |
| `UV_CACHE_DIR="$PWD/.reconciliation/uv-cache" uv lock --offline --python /Users/tylermartin/.hermes/hermes-agent/venv/bin/python` | Passed; dependency lock unchanged. The sandboxed attempt hit a macOS uv panic; rerunning with system-configuration access succeeded, keeping cache writes inside this worktree. |

Behavior tests were repaired for current contracts rather than source layout: queue handling is acknowledged after staging; callback state is workspace-scoped; project-freshness fixtures use a fixed clock; classic CLI ordering is exercised through the staging method; kernel ownership compares actual child processes instead of a removed temporary filename. Added direct integration coverage for Telegram busy callback dispatch, progress anchors, AFK watcher registration, and cron profile/memory persistence.

Clawpatch `doctor` and mapping completed. `clawpatch review --limit 10 --since MERGE_HEAD --jobs 2` was attempted, but all provider launches failed because Codex's in-process app-server client could not initialize in the sandbox (`Operation not permitted`). It was not escalated beyond the worktree-only constraint. The resulting zero-finding report is **not** a completed semantic-review approval. Manual code/history reconciliation and the focused tests above provide the validation for this merge; the entire repository suite and live Telegram delivery were not run.

## Removed skill assets

Accepted the upstream replacement/removal of these legacy assets after checking remaining references. Current `docx_*`, `pdf_*`, PowerPoint, and `xlsx_recalc.py` workflows replace them; optional financial modeling retains its own recalculation implementation.

- `skills/productivity/docx/scripts/comment.py`
- `skills/productivity/docx/scripts/merge_runs.py`
- `skills/productivity/docx/scripts/office/helpers/pptx_theme.py`
- `skills/productivity/docx/scripts/office/schemas/ISO-IEC29500-4_2016/sml.xsd`
- `skills/productivity/docx/scripts/office/schemas/ISO-IEC29500-4_2016/xml.xsd`
- `skills/productivity/docx/scripts/office/validate.py`
- `skills/productivity/docx/scripts/office/validators/base.py`
- `skills/productivity/docx/scripts/office/validators/docx.py`
- `skills/productivity/docx/scripts/office/validators/pptx.py`
- `skills/productivity/docx/scripts/office/validators/redlining.py`
- `skills/productivity/pdf/reference.md`
- `skills/productivity/pdf/scripts/convert_pdf_to_images.py`
- `skills/productivity/pdf/scripts/create_validation_image.py`
- `skills/productivity/pdf/scripts/extract_form_field_info.py`
- `skills/productivity/pdf/scripts/fill_fillable_fields.py`
- `skills/productivity/pdf/scripts/fill_pdf_form_with_annotations.py`
- `skills/productivity/powerpoint/scripts/office/helpers/pptx_theme.py`
- `skills/productivity/powerpoint/scripts/office/validate.py`
- `skills/productivity/powerpoint/scripts/office/validators/base.py`
- `skills/productivity/powerpoint/scripts/office/validators/docx.py`
- `skills/productivity/powerpoint/scripts/office/validators/pptx.py`
- `skills/productivity/powerpoint/scripts/office/validators/redlining.py`
- `skills/productivity/powerpoint/scripts/thumbnail.py`
- `skills/productivity/xlsx/scripts/recalc.py`

## Reproducible focused test command

This is the expanded argument list of the final invocation (`scripts/run_tests.sh $(cat .reconciliation/focused-final.txt)`); reconciliation scratch files are not part of the staged merge.

```sh
scripts/run_tests.sh \
  tests/acp_adapter/test_wake_lifecycle.py \
  tests/agent/test_auxiliary_anthropic_pool_fallback_regression.py \
  tests/agent/test_auxiliary_openrouter_env_fallback.py \
  tests/agent/test_codex_app_server_event_bridge.py \
  tests/agent/test_codex_app_server_persist.py \
  tests/agent/test_codex_aux_no_progress_timeout.py \
  tests/agent/test_codex_aux_timeout_fd_ownership.py \
  tests/agent/test_codex_cloudflare_headers.py \
  tests/agent/test_codex_gpt55_autoraise_notice.py \
  tests/agent/test_codex_happy_eyeballs.py \
  tests/agent/test_codex_request_transport_diagnostics.py \
  tests/agent/test_codex_responses_adapter.py \
  tests/agent/test_codex_responses_settle_pending_tool_calls.py \
  tests/agent/test_codex_runtime_live_events.py \
  tests/agent/test_codex_ttfb_watchdog.py \
  tests/agent/test_codex_usage_attribution.py \
  tests/agent/test_compression_busy_steer_anchor.py \
  tests/agent/test_compression_fallback_budget.py \
  tests/agent/test_compression_stall_fallback_78981.py \
  tests/agent/test_context_compressor_reasoning_fallback.py \
  tests/agent/test_context_compressor_summary_continuity.py \
  tests/agent/test_gemini_fast_fallback.py \
  tests/agent/test_kanban_stop.py \
  tests/agent/test_kanban_terminal_reconciliation.py \
  tests/agent/test_local_endpoint.py \
  tests/agent/test_lock_fallback_base_semantics.py \
  tests/agent/test_reference_handoff_active_turn.py \
  tests/agent/test_shared_user_profile_compression.py \
  tests/agent/transports/test_codex_app_server_runtime.py \
  tests/agent/transports/test_codex_app_server_session.py \
  tests/agent/transports/test_codex_event_projector.py \
  tests/agent/transports/test_codex_transport.py \
  tests/agent/transports/test_meta_codex_cache.py \
  tests/agent/transports/test_router_codex_efforts.py \
  tests/cli/test_cli_codex_context_reference.py \
  tests/cli/test_continuity_wake.py \
  tests/cron/test_codex_execution_paths.py \
  tests/cron/test_cron_kanban_env_isolation.py \
  tests/cron/test_cron_profile_isolation.py \
  tests/cron/test_cron_script.py \
  tests/cron/test_media_delivery_parity.py \
  tests/cron/test_mirror_origin_fallback.py \
  tests/dream_cycle_v3/test_wake.py \
  tests/gateway/relay/test_handoff_relay_aliasing.py \
  tests/gateway/test_afk_followup.py \
  tests/gateway/test_api_server_wake.py \
  tests/gateway/test_auth_fallback.py \
  tests/gateway/test_busy_command.py \
  tests/gateway/test_busy_session_ack.py \
  tests/gateway/test_busy_session_auth_bypass.py \
  tests/gateway/test_busy_session_buttons.py \
  tests/gateway/test_busy_session_runner.py \
  tests/gateway/test_channel_continuity_hint.py \
  tests/gateway/test_clarify_active_session_bypass.py \
  tests/gateway/test_clarify_progress_leak.py \
  tests/gateway/test_clarify_send_timeout_ambiguity.py \
  tests/gateway/test_clarify_thread_followup_not_swallowed.py \
  tests/gateway/test_clarify_tool_progress.py \
  tests/gateway/test_codex_hygiene_compaction.py \
  tests/gateway/test_continuity_wake.py \
  tests/gateway/test_continuity_wake_lifecycle.py \
  tests/gateway/test_continuity_wake_multiplex.py \
  tests/gateway/test_discord_clarify_buttons.py \
  tests/gateway/test_fallback_chain_reload.py \
  tests/gateway/test_fallback_eviction.py \
  tests/gateway/test_handoff_secondary_profile_adapter.py \
  tests/gateway/test_handoff_thread_session_key.py \
  tests/gateway/test_handoff_watcher_async_db.py \
  tests/gateway/test_handoff_watcher_multiprofile.py \
  tests/gateway/test_handoff_watcher_resilience.py \
  tests/gateway/test_internal_event_never_interrupts_busy_session.py \
  tests/gateway/test_kanban_auto_decompose_live.py \
  tests/gateway/test_kanban_board_scope.py \
  tests/gateway/test_kanban_changes_requested_notifier.py \
  tests/gateway/test_kanban_notifier.py \
  tests/gateway/test_kanban_notifier_apiserver_wake.py \
  tests/gateway/test_kanban_notifier_wake_only_ordering.py \
  tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py \
  tests/gateway/test_kanban_notifier_zero_sub_gate.py \
  tests/gateway/test_kanban_reconcile_orphans.py \
  tests/gateway/test_kanban_wake_scope.py \
  tests/gateway/test_kanban_watchers_mixin.py \
  tests/gateway/test_multiplex_busy_input_mode.py \
  tests/gateway/test_post_stream_media_delivery.py \
  tests/gateway/test_reconciled_busy_wiring.py \
  tests/gateway/test_run_cleanup_progress.py \
  tests/gateway/test_run_progress_interrupt.py \
  tests/gateway/test_run_progress_topics.py \
  tests/gateway/test_session_continuity_82616.py \
  tests/gateway/test_session_db_corrupt_fallback.py \
  tests/gateway/test_session_db_replaced_fallback.py \
  tests/gateway/test_session_degraded_db_continuity.py \
  tests/gateway/test_slack_clarify_buttons.py \
  tests/gateway/test_slack_wake_external_bot_messages.py \
  tests/gateway/test_stop_phrase_matcher.py \
  tests/gateway/test_telegram_busy_controls.py \
  tests/gateway/test_telegram_clarify_buttons.py \
  tests/gateway/test_telegram_documents.py \
  tests/gateway/test_telegram_fallback_pool_release_71593.py \
  tests/gateway/test_telegram_media_download_retry.py \
  tests/gateway/test_telegram_media_read_timeout.py \
  tests/gateway/test_telegram_prune_stale_topic_binding_31501.py \
  tests/gateway/test_telegram_thread_fallback.py \
  tests/gateway/test_telegram_topic_mode.py \
  tests/gateway/test_telegram_topic_profile_isolation_76423.py \
  tests/gateway/test_telegram_topic_profile_routing_76423.py \
  tests/gateway/test_wake_delivery.py \
  tests/hermes_cli/test_auth_codex_provider.py \
  tests/hermes_cli/test_auth_codex_quota_probe.py \
  tests/hermes_cli/test_auth_codex_self_heal.py \
  tests/hermes_cli/test_auth_profile_fallback.py \
  tests/hermes_cli/test_busy_policy_invariants.py \
  tests/hermes_cli/test_codex_cli_model_picker.py \
  tests/hermes_cli/test_codex_models.py \
  tests/hermes_cli/test_codex_runtime_plugin_migration.py \
  tests/hermes_cli/test_codex_runtime_switch.py \
  tests/hermes_cli/test_copilot_catalog_oauth_fallback.py \
  tests/hermes_cli/test_fallback_cmd.py \
  tests/hermes_cli/test_fallback_config.py \
  tests/hermes_cli/test_gateway_proc_fallback.py \
  tests/hermes_cli/test_kanban_block_kinds.py \
  tests/hermes_cli/test_kanban_blocked_sticky.py \
  tests/hermes_cli/test_kanban_board_project.py \
  tests/hermes_cli/test_kanban_boards.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_cli_dispatch_passthrough.py \
  tests/hermes_cli/test_kanban_cli_exit_status.py \
  tests/hermes_cli/test_kanban_comment_queries.py \
  tests/hermes_cli/test_kanban_core_functionality.py \
  tests/hermes_cli/test_kanban_count_notify_subs.py \
  tests/hermes_cli/test_kanban_db.py \
  tests/hermes_cli/test_kanban_db_init.py \
  tests/hermes_cli/test_kanban_db_repair.py \
  tests/hermes_cli/test_kanban_decompose.py \
  tests/hermes_cli/test_kanban_decompose_db.py \
  tests/hermes_cli/test_kanban_default_assignee.py \
  tests/hermes_cli/test_kanban_diagnostics.py \
  tests/hermes_cli/test_kanban_dispatch_entry_points.py \
  tests/hermes_cli/test_kanban_dispatch_lock.py \
  tests/hermes_cli/test_kanban_dispatch_tick_hook.py \
  tests/hermes_cli/test_kanban_gateway_restart_handoff.py \
  tests/hermes_cli/test_kanban_global_concurrency.py \
  tests/hermes_cli/test_kanban_goal_mode.py \
  tests/hermes_cli/test_kanban_host_cap.py \
  tests/hermes_cli/test_kanban_init_lock_bounded.py \
  tests/hermes_cli/test_kanban_lifecycle_hooks.py \
  tests/hermes_cli/test_kanban_memory_guard.py \
  tests/hermes_cli/test_kanban_notify.py \
  tests/hermes_cli/test_kanban_parent_reopen_invalidation.py \
  tests/hermes_cli/test_kanban_per_profile_cap.py \
  tests/hermes_cli/test_kanban_project_link.py \
  tests/hermes_cli/test_kanban_promote.py \
  tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py \
  tests/hermes_cli/test_kanban_review_lifecycle.py \
  tests/hermes_cli/test_kanban_review_lifecycle_complete.py \
  tests/hermes_cli/test_kanban_review_surfaces.py \
  tests/hermes_cli/test_kanban_specify.py \
  tests/hermes_cli/test_kanban_specify_db.py \
  tests/hermes_cli/test_kanban_swarm.py \
  tests/hermes_cli/test_kanban_task_updated_hook.py \
  tests/hermes_cli/test_kanban_transfer.py \
  tests/hermes_cli/test_kanban_worker_image_extraction.py \
  tests/hermes_cli/test_kanban_worker_lifecycle_hooks.py \
  tests/hermes_cli/test_kanban_worker_session_source.py \
  tests/hermes_cli/test_kanban_worker_spawn_toolsets.py \
  tests/hermes_cli/test_kanban_worker_terminal_cwd.py \
  tests/hermes_cli/test_kanban_worktree_isolation.py \
  tests/hermes_cli/test_kanban_worktree_teardown.py \
  tests/hermes_cli/test_kanban_write_guard.py \
  tests/hermes_cli/test_kanban_write_txn_busy_retry.py \
  tests/hermes_cli/test_openai_codex_model_validation_fallback.py \
  tests/hermes_cli/test_pin_kanban_board_env.py \
  tests/hermes_cli/test_session_handoff.py \
  tests/hermes_cli/test_signal_handler_kanban_worker.py \
  tests/hermes_cli/test_terminal_menu_fallbacks.py \
  tests/hermes_cli/test_update_handoff_backend_reap.py \
  tests/hermes_cli/test_update_handoff_desktop_rebuild.py \
  tests/hermes_cli/test_update_handoff_exit.py \
  tests/plugins/image_gen/test_openai_codex_provider.py \
  tests/run_agent/test_24996_fallback_exhaustion_cooldown.py \
  tests/run_agent/test_32646_fallback_429_after_timeout.py \
  tests/run_agent/test_codex_app_server_compaction.py \
  tests/run_agent/test_codex_app_server_integration.py \
  tests/run_agent/test_codex_app_server_lifecycle.py \
  tests/run_agent/test_codex_multimodal_tool_result.py \
  tests/run_agent/test_codex_no_tools_nonetype.py \
  tests/run_agent/test_codex_sdk_transform_bypass.py \
  tests/run_agent/test_codex_silent_hang_hint.py \
  tests/run_agent/test_codex_xai_oauth_recovery.py \
  tests/run_agent/test_compress_context_fallback_shim.py \
  tests/run_agent/test_compress_focus_plugin_fallback.py \
  tests/run_agent/test_compressor_fallback_update.py \
  tests/run_agent/test_conversation_fallback_state.py \
  tests/run_agent/test_fallback_api_mode_preservation.py \
  tests/run_agent/test_fallback_credential_isolation.py \
  tests/run_agent/test_fallback_reasoning_override.py \
  tests/run_agent/test_image_rejection_fallback.py \
  tests/run_agent/test_init_fallback_on_exhausted_pool.py \
  tests/run_agent/test_local_start_on_fallback.py \
  tests/run_agent/test_nous_429_fallback_reentry.py \
  tests/run_agent/test_nous_fallback_unavailable.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_run_agent_codex_responses.py \
  tests/run_agent/test_switch_model_fallback_prune.py \
  tests/test_empty_model_fallback.py \
  tests/test_hermes_state_wal_fallback.py \
  tests/tools/test_clarify_gateway.py \
  tests/tools/test_clarify_tool.py \
  tests/tools/test_code_execution_stable_launcher.py \
  tests/tools/test_code_kernel.py \
  tests/tools/test_code_kernel_remote.py \
  tests/tools/test_continuity_tool.py \
  tests/tools/test_credential_pool_env_fallback.py \
  tests/tools/test_delegate_cron_sync_fallback.py \
  tests/tools/test_delegate_kanban_isolation.py \
  tests/tools/test_kanban_comment_injection.py \
  tests/tools/test_kanban_redaction.py \
  tests/tools/test_kanban_tools.py \
  tests/tools/test_local_cwd_permission_fallback.py \
  tests/tools/test_memory_tool.py \
  tests/tools/test_memory_tool_import_fallback.py \
  tests/tools/test_memory_tool_schema.py \
  tests/tools/test_voice_stop_phrase.py \
  tests/tui_gateway/test_codex_app_server_live_events.py \
  tests/tui_gateway/test_wake_lifecycle.py
```

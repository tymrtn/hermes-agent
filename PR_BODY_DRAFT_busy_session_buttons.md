## Summary

Adds per-message busy-session controls on top of the existing `display.busy_input_mode` infrastructure. Lets users pick **steer / interrupt / stop** on a specific follow-up message via inline keyboard buttons rather than only changing the global mode.

Three additive pieces:

1. **Inline keyboard `[/steer] [/interrupt] [/stop]`** anchored to the running tool-progress bubble (Telegram + Discord + Slack). Reuses the existing busy-ack message as the keyboard anchor when no tool bubble is live yet — no extra messages, no duplication. After a tap, the ack body rewrites to reflect what actually happened (`⏩ Steered…` / `⚡ Interrupted…` / `🛑 Stopped.`) so chat history stays accurate. A single tap acts on **every** follow-up that arrived since the agent went busy — texts joined, primitive applied once, reaction emitted on each individual user message.
2. **Multilingual halt-phrase heuristic** — 16-language exact-word matcher (en/es/fr/de/pt/it/nl/ja/ko/zh-Hans/zh-Hant/ru/ar/hi/tr/pl) with conservative function-word exclusions ("para" / "basta" / "잠깐" deliberately not included). Runs as a pre-flight inside `_handle_active_session_busy_message`; matches like `stop`, `alto`, `止まれ`, `/`, or empty msg trigger the full `_interrupt_and_clear_session` path so the chat unlocks even if the agent is wedged inside a tool.
3. **Reaction lifecycle on user follow-ups** — 👍 (steer), ⚡ (interrupt), 🙊 (halt). Wraps emoji in `ReactionTypeEmoji` to dodge python-telegram-bot's variation-selector `custom_emoji` serialization bug.

**Default change:** `display.busy_input_mode` defaults to `queue` instead of `interrupt`. Interrupt-by-default destroys partial work; queue buffers the follow-up for the next turn. The new buttons give users an explicit per-message override when they want something different. New `display.busy_buttons: true` toggle (env: `HERMES_GATEWAY_BUSY_BUTTONS=false`) for per-bot disable.

Resolves #11639. Resolves #11118. Related: #18362, #11119.

## Why not the LLM-based router originally proposed in #11639?

#11639 proposed a small classifier model deciding the primitive per inbound message. This PR takes a deterministic alternative — explicit user choice via persistent UI controls plus a literal-phrase heuristic. No model latency, no false positives on ambiguous text, no per-message token cost. The buttons are language-neutral and always available; the halt heuristic gives a fast path for natural-language stops without needing `/stop`.

## Authorization

- **Telegram:** `_is_callback_user_authorized` (PR #17775).
- **Discord:** `_component_check_auth` (mirrors `ExecApprovalView` etc.).
- **Slack:** routes through `GatewayRunner._is_user_authorized` for full org-level checks; falls back to `SLACK_ALLOWED_USERS` only when no runner is bound, and FAILS CLOSED when neither is configured.

## Telegram callback_data 64-byte cap

Long group/forum session keys (e.g. `agent:main:telegram:supergroup:-1001234567890:thread:42:user:9876543210`) overflow Telegram's 64-byte cap. `build_buttons_with_handles` switches to a stable short hash (`bs:steer:#<12-char-sha256>`) and surfaces the handle map; the platform registers the mapping at attach time and resolves it on tap.

## Test plan

- [x] `pytest tests/gateway/test_stop_phrase_matcher.py` (33/33)
- [x] `pytest tests/gateway/test_busy_session_buttons.py` (21/21)
- [x] `pytest tests/gateway/test_busy_session_runner.py` (16/16)
- [x] `pytest tests/gateway/test_busy_session_ack.py` `test_busy_session_auth_bypass.py` `test_steer_command.py` `test_queue_consumption.py` `test_telegram_reactions.py` `test_telegram_approval_buttons.py` `test_session_state_cleanup.py` `test_session_race_guard.py` `test_session_split_brain_11016.py` `test_status_command.py` `test_restart_drain.py` `test_run_progress_topics.py` (no regressions)
- [x] `codex review --base origin/main` — three passes, 14 findings resolved (Slack auth bypass, Discord adapter binding, halt-phrase full-clear, multi-anchor cleanup, callback-data cap, etc.)
- [ ] Live smoke on Telegram — slow tool call, mid-tool follow-up, button taps, halt phrases in en/es/ja
- [ ] Live smoke on Discord
- [ ] Live smoke on Slack

## Backwards-compatibility

- `display.busy_buttons: false` keeps current behavior (no buttons).
- `display.busy_input_mode: interrupt` (legacy) still works; the change is to the default only.
- All new platform methods on `BasePlatformAdapter` have no-op defaults; platforms that don't override (Matrix, Feishu, DingTalk, WhatsApp) are unaffected and inherit silently.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

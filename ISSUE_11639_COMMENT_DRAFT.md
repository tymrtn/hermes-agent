Hi — opening a PR for this from a different angle than the original sketch and wanted to flag it here in case you'd rather close this issue or steer the design before review.

The original proposal (Section "Proposed Solution") leaned on a small classifier model to pick the primitive per inbound message. After running an interrupt-by-default deployment in production for a few months, I landed on a deterministic alternative that turned out to be both more reliable and cheaper:

**Per-message buttons + multilingual halt-phrase heuristic + queue by default.**

Concretely:

1. **Inline keyboard `[/steer] [/interrupt] [/stop]`** attached to the running tool-progress bubble (and to the existing busy-ack message as the anchor when no tool bubble is live yet — no extra surface, no duplication). One tap acts on every follow-up that arrived since the agent went busy. Telegram, Discord, and Slack.
2. **Conservative 16-language halt-phrase matcher.** Short literal stop intent ("stop", "alto", "止まれ", lone `/`, empty msg) routes through the full `_interrupt_and_clear_session` path. Function words deliberately excluded ("para", "basta", "잠깐"). Runs as a pre-flight in the existing busy handler — no router agent, no model call.
3. **Default flip to `queue`.** Interrupt-by-default destroys partial work; queue buffers the follow-up. The buttons are the explicit per-message override.

Three Codex review passes; 14 findings resolved (auth bypass on Slack, Discord adapter binding, halt-phrase full-clear, callback_data cap on long forum session keys, etc.). PR coming next with full diff and tests.

Happy to fold this into the design discussion if you'd prefer the model-router approach for v1 — the deterministic side ships fine on its own. Either way, also resolves #11118 (queue vs interrupt acknowledgement distinction).

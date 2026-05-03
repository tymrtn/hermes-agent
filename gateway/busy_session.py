"""Busy-session router primitives.

When a user sends a message while the agent is mid-turn, the gateway must
decide what to do with it. This module defines the primitives, the shared
decision dataclass, and the reaction map. Both busy code paths (the adapter
busy-session callback and the inline fast-path in ``_handle_message``) call
the same router so they cannot diverge.

Primitives (user-facing):
- ``queue`` (default): store the message for delivery as the next turn
  after the current turn fully completes. Pending state shown as ⏳ on the
  user's message; cleared when bot responds.
- ``steer``: user explicitly chose to inject mid-stream via the inline
  keyboard button on the active tool bubble.
- ``interrupt``: user chose to halt + redirect — the running agent aborts
  and the user's text becomes the next turn's input.
- ``halt``: user chose to halt + idle, OR multilingual halt heuristic
  matched ("stop"/"para"/"止まれ"/lone "/"/empty msg). Agent halts; no
  replay.

Internal-only:
- ``drop``: rejected (drain-without-queue, missing adapter). Not
  user-invokable; no public reaction.

Reactions on the user's inbound message:
- ⏳ pending  (queue / awaiting button tap or turn-end)
- 👌 steer    (final state after [Steer] button)
- ↪️ interrupt (final state after [Interrupt] button)
- 🛑 halt     (final state after halt heuristic OR [Halt] button)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

# Reaction glyph constants for the user-facing primitives.
#
# Telegram constraint: bot reactions via setMessageReaction are restricted
# to the chat's allowed reactions list. The default "free" set rejects
# many semantically-perfect glyphs (⏳ ↪️ 🛑 😳 are all REACTION_INVALID).
# These constants are picked to (a) be in the Telegram free set,
# (b) convey the right vibe, (c) stay distinct from each other and from
# the existing on_processing_start/complete reactions (👀/✅/❌).
REACTION_QUEUE = "⏳"      # Hermes canonical; queue is the SILENT default —
                          # this constant is never emitted as a reaction
                          # (Telegram rejects ⏳ anyway). It only shows up
                          # in the inline tool-bubble "⏳ /queue'd: ..." line.
REACTION_STEER = "👍"      # thumbs up — "got it, doing" (Telegram sub for Hermes's ⏩)
REACTION_INTERRUPT = "⚡"   # Hermes's canonical interrupt glyph — also in Telegram whitelist
REACTION_HALT = "🙊"       # going quiet (Telegram sub for Hermes's 🛑; primitive verb is "stop")
REACTION_DROP = "🙈"       # internal-only; see-no-evil

# Backwards-compat alias — older imports/tests may reference REACTION_STOP.
REACTION_STOP = REACTION_HALT


@dataclass(frozen=True)
class BusySessionDecision:
    """Structured outcome of routing a busy-session inbound message.

    Both busy code paths return one of these; the application function then
    turns it into side-effects (queue, steer, interrupt, halt, ack, reaction).
    """

    action: Literal["queue", "steer", "interrupt", "halt", "drop"]
    reason: str
    message: Optional[str] = None
    reaction: Optional[str] = None
    merge_text: bool = False
    debounce_ack: bool = False


# Reaction map by action. Most reasons share the same reaction; reasons
# distinguish for logging/observability only.
_DEFAULT_REACTIONS: dict[str, str] = {
    "queue": REACTION_QUEUE,
    "steer": REACTION_STEER,
    "interrupt": REACTION_INTERRUPT,
    "halt": REACTION_HALT,
    "drop": REACTION_DROP,
    # Backwards-compat: callers using "stop" still resolve to halt's glyph.
    "stop": REACTION_HALT,
}


def reaction_for(action: str) -> Optional[str]:
    """Return the default reaction glyph for an action."""
    return _DEFAULT_REACTIONS.get(action)


def normalize_busy_input_mode(raw: Optional[str]) -> str:
    """Map raw config strings to the canonical busy-input mode.

    Values:
    - ``queue`` (new default) — text follow-ups wait for current turn to
      finish; user can override via inline-keyboard buttons on the tool bubble
    - ``steer`` — text follow-ups inject mid-stream automatically
    - ``interrupt`` (legacy) — text follow-ups abort the current tool

    Anything unrecognized resolves to ``queue``.
    """
    if not raw:
        return "queue"
    normalized = str(raw).strip().lower()
    if normalized in ("queue", "steer", "interrupt"):
        return normalized
    return "queue"


def normalize_busy_ack_mode(raw: Optional[str]) -> str:
    """Map raw config strings to the canonical busy-ack mode.

    Values:
    - ``reaction`` (default) — emoji reaction only, no text ack
    - ``text`` — text ack only, no reaction
    - ``both`` — both
    """
    if not raw:
        return "reaction"
    normalized = str(raw).strip().lower()
    if normalized in ("reaction", "text", "both"):
        return normalized
    return "reaction"


__all__ = [
    "BusySessionDecision",
    "REACTION_DROP",
    "REACTION_HALT",
    "REACTION_INTERRUPT",
    "REACTION_QUEUE",
    "REACTION_STEER",
    "REACTION_STOP",  # alias
    "normalize_busy_ack_mode",
    "normalize_busy_input_mode",
    "reaction_for",
]

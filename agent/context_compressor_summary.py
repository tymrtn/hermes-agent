"""Summary-hook dispatch and cancellation rollback for context compression."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent.auxiliary_client import AuxiliaryExplicitCancellation

if TYPE_CHECKING:
    from agent.context_compressor import _HandoffScan

# Opt-in per compressor instance (``reconcile_with_retained_tail``), set only by the scheduled
# nightly compression on its throwaway agent. The tail always keeps the latest user message and
# everything after it verbatim, and by contract wins over the summary — so the handoff's state
# sections lag exactly the exchange where work usually finishes, and a consumer reading them alone
# reports blockers that exchange already resolved. The summarizer sees the tail as reference so
# those sections reflect it; the tail itself is neither summarized nor removed.
_RETAINED_TAIL_BLOCK = """

NEWER TURNS KEPT VERBATIM AFTER THIS SUMMARY (reference only; do not summarize or repeat them):
{tail}

These turns are newer than everything above. Write "## Active State", "## Blocked" and any "## Active Task" as of the END of these turns: drop blockers they resolve and do not list as open anything they show as done."""


def _accepts_keyword_argument(callable_obj: Any, name: str) -> bool:
    """Return whether an inspectable callable accepts ``name`` as a keyword."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return True
    parameter = parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


class SummaryDispatchMixin:
    def _summarize_window(
        self, messages: List[Dict[str, Any]], turns_to_summarize: List[Dict[str, Any]], scan: "_HandoffScan",
        focus_topic: Optional[str], memory_context: str, bypass_cooldown: bool,
    ) -> Optional[str]:
        """Run the summary LLM; a cancellation rolls back the handoff scan's self-heal mutation first.
        A deterministic pin (repeated stall, #112420) skips the LLM: ``None`` lets Phase 3 insert the static
        fallback summary, or abort under ``abort_on_summary_failure`` exactly like a failed summary call."""
        from agent.context_compressor import take_deterministic_summary_pin
        if take_deterministic_summary_pin():
            # Surfaces through the fallback summary's reason line and the host's one-shot user warning.
            self._last_summary_error = (
                "summary model stalled again after a stall backoff; deterministic fallback summary inserted"
            )
            telemetry = getattr(self, "_active_compression_telemetry", None)
            if isinstance(telemetry, dict):
                telemetry["failure_class"] = "stall_deterministic_fallback"
            return None
        # Focus-topic derivation scans user turns; only pay when a summary is generated.
        summary_kwargs: Dict[str, Any] = {
            "focus_topic": focus_topic or self._derive_auto_focus_topic(messages),
            "memory_context": memory_context,
        }
        if _accepts_keyword_argument(self._generate_summary, "bypass_cooldown"):
            summary_kwargs["bypass_cooldown"] = bypass_cooldown
        # Held for the whole call so the main-model retry inside _generate_summary sees it too.
        retained_tail = messages[scan.tail_start:] if getattr(self, "reconcile_with_retained_tail", False) else []
        self._retained_tail_reference = (
            self._bound_summary_input(self._serialize_for_summary(retained_tail)) if retained_tail else "")
        try:
            return self._generate_summary(turns_to_summarize, **summary_kwargs)
        except AuxiliaryExplicitCancellation:
            # Cancellation is a true no-op: restore the scan's mutation before the exception escapes.
            self._previous_summary = scan.previous_summary_before
            self._summary_has_user_turn = scan.has_user_turn_before
            raise
        finally:
            self._retained_tail_reference = ""

    def _retained_tail_prompt_block(self) -> str:
        """The summary-prompt block carrying the verbatim tail as reference, or "" when not opted in."""
        reference = getattr(self, "_retained_tail_reference", "")
        return _RETAINED_TAIL_BLOCK.format(tail=reference) if reference else ""

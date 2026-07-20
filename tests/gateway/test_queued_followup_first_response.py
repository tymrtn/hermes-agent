"""Regression tests for the queued-follow-up first-response path.

History (Dream Cycle 2026-06-12): ``_run_agent`` called
``_send_first_response_before_queued_followup(event=event, ...)`` but no
``event`` name exists in that scope — the NameError fired on *argument
evaluation*, so the entire first response (text included) was silently
dropped whenever a queued message followed a completed turn.  gateway.log
shows five occurrences between 2026-06-09 and 2026-06-12:
``Failed to send first response before queued message: name 'event' is
not defined``.
"""

import asyncio
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Unit: helper must deliver the text even when no MessageEvent is available
# (leftover-/steer queued turns have no dequeued event), and must only
# attempt reply-anchored media delivery when an event exists.
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, response, metadata=None):
        self.sent.append((chat_id, response, metadata))


def _make_runner_stub():
    from gateway.run import GatewayRunner

    stub = SimpleNamespace(media_calls=[])

    async def _record_media(response, event, adapter):
        stub.media_calls.append((response, event, adapter))

    stub._deliver_media_from_response = _record_media
    stub._impl = GatewayRunner._send_first_response_before_queued_followup
    return stub


def test_first_response_sends_text_without_event():
    stub = _make_runner_stub()
    adapter = _RecordingAdapter()
    source = SimpleNamespace(chat_id="chat-1")

    asyncio.run(
        stub._impl(
            stub,
            adapter=adapter,
            source=source,
            event=None,
            response="first response text",
            metadata=None,
        )
    )

    assert adapter.sent == [("chat-1", "first response text", None)]
    assert stub.media_calls == []


def test_first_response_delivers_media_with_event():
    stub = _make_runner_stub()
    adapter = _RecordingAdapter()
    source = SimpleNamespace(chat_id="chat-2")
    event = SimpleNamespace(source=source)

    asyncio.run(
        stub._impl(
            stub,
            adapter=adapter,
            source=source,
            event=event,
            response="text with media",
            metadata={"thread": "t"},
        )
    )

    assert adapter.sent == [("chat-2", "text with media", {"thread": "t"})]
    assert stub.media_calls == [("text with media", event, adapter)]

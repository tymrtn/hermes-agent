"""Deliverables must not be stranded behind a blocking clarify prompt.

Live failure (2026-08-15, Telegram, ``interim_assistant_messages=false`` +
``streaming=true``): the agent produced a quote dossier, announced it in a
Codex ``phase=commentary`` message carrying the turn's only ``MEDIA:``
directive, then blocked on ``clarify``.  Because interim assistant messages
were suppressed there was no commentary consumer, so the directive was
dropped and the user got a poll asking about a file they had never received.

These tests pin the rescue lane: suppressed commentary that carries a valid
``MEDIA:`` deliverable releases the ATTACHMENT (plus its caption-sized note)
before the poll, ordinary suppressed chatter stays suppressed, and a path
already delivered this way is not sent twice.
"""

import asyncio
import importlib
import sys
import time
import types

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource

from tests.gateway.test_run_progress_topics import (
    ProgressCaptureAdapter,
    _make_runner,
)


def _split_into_deltas(text: str, size: int = 6) -> list:
    """Chop a response into token-sized deltas."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class ClarifyOrderingAdapter(ProgressCaptureAdapter):
    """Record text / attachment / poll sends on one ordered timeline."""

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self.events = []

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.events.append(("text", content))
        return await super().send(
            chat_id, content, reply_to=reply_to, metadata=metadata
        )

    async def send_document(
        self,
        chat_id,
        file_path,
        caption=None,
        file_name=None,
        reply_to=None,
        metadata=None,
        **kwargs,
    ) -> SendResult:
        self.events.append(("document", str(file_path)))
        return SendResult(success=True, message_id="doc-1")

    async def send_clarify(
        self, chat_id, question, choices, clarify_id, session_key, metadata=None
    ) -> SendResult:
        self.events.append(("clarify", question))
        return SendResult(success=True, message_id="clarify-1")


class FailedDocumentAdapter(ClarifyOrderingAdapter):
    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        self.events.append(("document-failed", str(file_path)))
        return SendResult(success=False, error="simulated upload failure")


class FallbackDocumentAdapter(ClarifyOrderingAdapter):
    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        return await BasePlatformAdapter.send_document(
            self, chat_id=chat_id, file_path=file_path, **kwargs
        )


class FailedNoteAdapter(ClarifyOrderingAdapter):
    async def send(self, chat_id, content, **kwargs) -> SendResult:
        if "Dossier ready" in content:
            raise RuntimeError("simulated note failure")
        return await super().send(chat_id, content, **kwargs)


class SlowDocumentAdapter(ClarifyOrderingAdapter):
    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        await asyncio.sleep(0.2)
        self.events.append(("document", str(file_path)))
        return SendResult(success=True, message_id="late-doc")


class InvalidatingDocumentAdapter(ClarifyOrderingAdapter):
    invalidate = None

    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        await asyncio.sleep(0.05)
        self.events.append(("document", str(file_path)))
        type(self).invalidate()
        return SendResult(success=True, message_id="doc-invalidated")


class SecondSlowDocumentAdapter(ClarifyOrderingAdapter):
    sends = 0

    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        type(self).sends += 1
        if type(self).sends == 2:
            await asyncio.sleep(0.2)
        self.events.append(("document", str(file_path)))
        return SendResult(success=True, message_id=f"doc-{type(self).sends}")


class FailSecondDocumentAdapter(ClarifyOrderingAdapter):
    sends = 0

    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        type(self).sends += 1
        if type(self).sends == 2:
            self.events.append(("document-failed", str(file_path)))
            return SendResult(success=False, error="second version failed")
        self.events.append(("document", str(file_path)))
        return SendResult(success=True, message_id="doc-first")


class RewriteDuringUploadAdapter(ClarifyOrderingAdapter):
    rewrite = None

    async def send_document(self, chat_id, file_path, **kwargs) -> SendResult:
        self.events.append(("document", str(file_path)))
        type(self).rewrite()
        return SendResult(success=True, message_id="doc-before-rewrite")


class AcknowledgedImageAdapter(ClarifyOrderingAdapter):
    async def send_image_file(
        self, chat_id, image_path, caption=None, reply_to=None, metadata=None
    ) -> SendResult:
        self.events.append(("image", str(image_path)))
        return SendResult(success=True, message_id="image-1")


class StreamingClarifyAdapter(ClarifyOrderingAdapter):
    """Adapter that honours the stream consumer's edit contract.

    ``edit_message`` must accept ``finalize=``/``metadata=`` — the consumer
    always passes them — and every visible frame is recorded so a streamed
    repeat of an already-sent note is detectable.
    """

    def __init__(self, platform=Platform.TELEGRAM):
        super().__init__(platform=platform)
        self._sent_ids = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.events.append(("text", content))
        self._sent_ids += 1
        return SendResult(success=True, message_id=f"stream-{self._sent_ids}")

    async def edit_message(
        self, chat_id, message_id, content, *, finalize: bool = False, metadata=None
    ) -> SendResult:
        self.events.append(("edit", content))
        return SendResult(success=True, message_id=message_id)


class ClarifyAfterSuppressedCommentaryAgent:
    """Codex-shaped turn: commentary carries the deliverable, then clarify blocks.

    Mirrors ``run_agent._emit_interim_assistant_message`` with the interim
    consumer disabled — the commentary is not delivered, it is offered to the
    deliverable rescue lane instead.
    """

    commentary = ""
    final_response = "Done."
    invoke_clarify = True
    after_clarify = None
    second_commentary = None
    before_second_clarify = None
    messages = []
    # When set, the final response is emitted as streamed deltas (the live
    # shape: streaming on, interim assistant messages off) with a pause
    # between chunks so the consumer's edit ticks land mid-stream.
    stream_final = False

    def __init__(self, **kwargs):
        self.tools = []
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.suppressed_interim_deliverable_callback = None
        self.clarify_callback = None
        self.clarify_answer = None

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.interim_assistant_callback is not None:
            self.interim_assistant_callback(type(self).commentary)
        cb = self.suppressed_interim_deliverable_callback
        if cb is not None:
            cb(type(self).commentary)
        if type(self).invoke_clarify:
            self.clarify_answer = self.clarify_callback(
                "Want the CSV too?", ["Yes", "No"]
            )
            if type(self).after_clarify is not None:
                type(self).after_clarify()
            if type(self).second_commentary is not None:
                if type(self).before_second_clarify is not None:
                    type(self).before_second_clarify()
                cb(type(self).second_commentary)
                self.clarify_callback("Want JSON too?", ["Yes", "No"])
        if type(self).stream_final and self.stream_delta_callback is not None:
            for chunk in _split_into_deltas(type(self).final_response):
                self.stream_delta_callback(chunk)
                time.sleep(0.08)
        return {
            "final_response": type(self).final_response,
            "messages": type(self).messages,
            "api_calls": 1,
        }


async def _run_clarify_turn(
    monkeypatch,
    tmp_path,
    *,
    commentary,
    final_response="Done.",
    session_id="sess-clarify-deliverable",
    adapter=None,
    invoke_clarify=True,
    after_clarify=None,
    run_generation=None,
    is_current=None,
    second_commentary=None,
    before_second_clarify=None,
    messages=None,
    interim_messages=False,
    streaming=False,
):
    import yaml

    (tmp_path / "config.yaml").write_text(
        yaml.dump(
            {
                "display": {
                    "interim_assistant_messages": interim_messages,
                    "tool_progress": "off",
                },
                "streaming": {"enabled": bool(streaming)},
            }
        ),
        encoding="utf-8",
    )

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    ClarifyAfterSuppressedCommentaryAgent.commentary = commentary
    ClarifyAfterSuppressedCommentaryAgent.final_response = final_response
    ClarifyAfterSuppressedCommentaryAgent.invoke_clarify = invoke_clarify
    ClarifyAfterSuppressedCommentaryAgent.after_clarify = after_clarify
    ClarifyAfterSuppressedCommentaryAgent.second_commentary = second_commentary
    ClarifyAfterSuppressedCommentaryAgent.before_second_clarify = before_second_clarify
    ClarifyAfterSuppressedCommentaryAgent.messages = messages or []
    ClarifyAfterSuppressedCommentaryAgent.stream_final = bool(streaming)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = ClarifyAfterSuppressedCommentaryAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    from tools import clarify_gateway

    monkeypatch.setattr(
        clarify_gateway, "wait_for_response", lambda clarify_id, timeout: "Yes"
    )

    adapter = adapter or ClarifyOrderingAdapter(platform=Platform.TELEGRAM)
    runner = _make_runner(adapter)
    if streaming:
        # Edit transport with an eager flush cadence: mid-stream frames land
        # while the response is still arriving, which is exactly when a
        # repeated note would become visible ahead of the final-response dedup.
        runner.config.streaming = StreamingConfig(
            enabled=True, transport="edit", edit_interval=0.01, buffer_threshold=1
        )
    if is_current is not None:
        monkeypatch.setattr(
            runner,
            "_is_session_run_current",
            lambda session_key, generation: bool(is_current()),
        )
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id=None,
    )
    result = await runner._run_agent(
        message="build me the dossier",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key="agent:main:telegram:group:-1001",
        run_generation=run_generation,
    )
    return adapter, result


@pytest.mark.asyncio
async def test_suppressed_commentary_media_delivers_before_clarify_prompt(
    monkeypatch, tmp_path
):
    """The stranded-deliverable regression.

    With interim messages suppressed, a commentary-only ``MEDIA:`` directive
    must still reach the user as a native attachment BEFORE the blocking
    clarify poll — otherwise the poll asks about a file the user never got.
    """
    dossier = tmp_path / "cloudflare-quote-dossier-2026-08-15.txt"
    dossier.write_text("quote 1 — https://example.invalid/a\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=(
            "Dossier is ready — 42 quotes with source links.\n\n"
            f"MEDIA:{dossier}"
        ),
    )

    kinds = [kind for kind, _payload in adapter.events]
    assert "document" in kinds, f"deliverable never sent: {adapter.events!r}"
    assert "clarify" in kinds, f"clarify prompt never sent: {adapter.events!r}"
    assert kinds.index("document") < kinds.index("clarify"), (
        f"attachment must precede the clarify poll: {adapter.events!r}"
    )
    delivered = [
        payload for kind, payload in adapter.events if kind == "document"
    ]
    assert delivered == [str(dossier)]
    # The caption-sized note that came with the file rides along, ahead of
    # the poll, so the attachment is not a context-free upload.
    prose = [
        payload
        for kind, payload in adapter.events[: kinds.index("clarify")]
        if kind == "text"
    ]
    assert any("42 quotes" in text for text in prose), (
        f"associated prose missing before the poll: {adapter.events!r}"
    )
    assert result["final_response"] == "Done."


@pytest.mark.asyncio
async def test_enabled_interim_commentary_delivers_media_before_clarify(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "interim-on.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response=f"Done.\nMEDIA:{dossier}",
        session_id="sess-clarify-interim-on",
        interim_messages=True,
    )

    kinds = [kind for kind, _ in adapter.events]
    assert kinds.index("text") < kinds.index("document") < kinds.index("clarify")
    assert sum(
        kind == "text" and "Dossier ready" in payload
        for kind, payload in adapter.events
    ) == 1
    assert str(dossier) not in result["final_response"]


@pytest.mark.asyncio
async def test_suppressed_commentary_without_media_stays_suppressed(
    monkeypatch, tmp_path
):
    """Clarify must not become a backdoor that leaks ordinary interim chatter.

    The user turned interim assistant messages OFF. Commentary carrying no
    deliverable is discarded exactly as before — only the poll is sent.
    """
    adapter, _result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary="Let me think about the trade-offs here for a moment.",
        session_id="sess-clarify-no-media",
    )

    kinds = [kind for kind, _payload in adapter.events]
    assert "clarify" in kinds, f"clarify prompt never sent: {adapter.events!r}"
    assert "document" not in kinds
    leaked = [
        payload
        for kind, payload in adapter.events[: kinds.index("clarify")]
        if kind == "text"
    ]
    assert not any("trade-offs" in text for text in leaked), (
        f"suppressed commentary leaked to the platform: {adapter.events!r}"
    )


@pytest.mark.asyncio
async def test_repeated_predelivery_note_is_not_sent_again_as_final(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "repeat-note.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    response = f"Dossier ready.\nMEDIA:{dossier}"

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=response,
        final_response=response,
        session_id="sess-clarify-repeat-note",
    )

    assert sum(
        kind == "text" and "Dossier ready" in payload
        for kind, payload in adapter.events
    ) == 1
    assert result["final_response"] == ""
    assert result["delivery_satisfied"] is True


@pytest.mark.asyncio
async def test_streamed_final_does_not_repeat_the_predelivered_note(
    monkeypatch, tmp_path
):
    """Streaming must not put the pre-poll note on screen a second time.

    With streaming ON and interim messages OFF, the note that accompanied the
    pre-delivered attachment is sent directly on the adapter. The model then
    restates it in the post-clarify answer, which streams live — the gateway's
    final-response dedup only runs once the stream has finished, so without a
    suppression the user reads the same line twice.
    """
    dossier = tmp_path / "streamed-note.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    response = f"Dossier ready — 42 quotes with source links.\nMEDIA:{dossier}"
    adapter = StreamingClarifyAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=response,
        final_response=response,
        session_id="sess-clarify-streamed-note",
        adapter=adapter,
        streaming=True,
    )

    visible = [
        payload for kind, payload in adapter.events if kind in {"text", "edit"}
    ]
    assert sum("Dossier ready" in payload for payload in visible) == 1, (
        f"predelivered note was streamed again: {adapter.events!r}"
    )
    assert sum(kind == "document" for kind, _ in adapter.events) == 1
    assert result["final_response"] == ""
    assert result["delivery_satisfied"] is True


@pytest.mark.asyncio
async def test_streamed_final_with_new_prose_still_streams(monkeypatch, tmp_path):
    """Suppression is exact-match only — a real answer still streams.

    The post-clarify answer opens by restating the note and then continues
    with new prose. That is a genuine reply, so it streams normally; only a
    response that adds nothing beyond the already-sent note is dropped.
    """
    dossier = tmp_path / "streamed-more.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = StreamingClarifyAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready — 42 quotes with source links.\nMEDIA:{dossier}",
        final_response=(
            "Dossier ready — 42 quotes with source links.\n"
            f"MEDIA:{dossier}\n"
            "Added the CSV export you asked for as well."
        ),
        session_id="sess-clarify-streamed-more",
        adapter=adapter,
        streaming=True,
    )

    visible = [
        payload for kind, payload in adapter.events if kind in {"text", "edit"}
    ]
    assert any("Added the CSV export" in payload for payload in visible), (
        f"new prose was never streamed: {adapter.events!r}"
    )
    assert "Added the CSV export" in result["final_response"]
    kinds = [kind for kind, _ in adapter.events]
    assert kinds.index("document") < kinds.index("clarify")


@pytest.mark.asyncio
async def test_streamed_deleted_unknown_media_does_not_leak_path(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "Caddyfile"
    dossier.write_text("config\n", encoding="utf-8")
    response = f"Config attached.\nMEDIA:{dossier}"
    adapter = StreamingClarifyAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=response,
        final_response=response,
        session_id="sess-clarify-streamed-deleted-unknown",
        adapter=adapter,
        streaming=True,
        after_clarify=dossier.unlink,
    )

    visible = [
        payload for kind, payload in adapter.events if kind in {"text", "edit"}
    ]
    assert sum("Config attached" in payload for payload in visible) == 1
    assert not any(str(dossier) in payload for payload in visible)
    assert result["delivery_satisfied"] is True


@pytest.mark.asyncio
async def test_media_commentary_without_clarify_stays_suppressed(
    monkeypatch, tmp_path
):
    draft = tmp_path / "draft.txt"
    draft.write_text("not final\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Draft generated.\nMEDIA:{draft}",
        final_response="Still working; no final artifact yet.",
        session_id="sess-suppressed-media-no-clarify",
        invoke_clarify=False,
    )

    assert not any(kind in {"document", "text"} for kind, _ in adapter.events)
    assert result["final_response"] == "Still working; no final artifact yet."


@pytest.mark.asyncio
async def test_predelivered_media_is_not_resent_by_the_final_response(
    monkeypatch, tmp_path
):
    """A file released ahead of the poll must not arrive twice.

    After answering the clarify the model normally restates the attachment in
    its final reply. That tag is already satisfied, so it is dropped from the
    response text before either delivery path can act on it.
    """
    dossier = tmp_path / "dossier-resend.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    other = tmp_path / "appendix.csv"
    other.write_text("a,b\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\n\nMEDIA:{dossier}",
        final_response=(
            f"Here is the dossier and the appendix.\nMEDIA:{dossier}\nMEDIA:{other}"
        ),
        session_id="sess-clarify-dedup",
    )

    delivered = [payload for kind, payload in adapter.events if kind == "document"]
    assert delivered == [str(dossier)], (
        f"pre-delivered dossier was sent more than once: {adapter.events!r}"
    )
    final = result["final_response"]
    assert str(dossier) not in final, (
        f"already-delivered MEDIA tag survived into the final response: {final!r}"
    )
    # The undelivered appendix is untouched — dedup is per-path, not blanket.
    assert f"MEDIA:{other}" in final
    assert "Here is the dossier and the appendix." in final


@pytest.mark.asyncio
async def test_failed_predelivery_remains_in_final_response_for_retry(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "retry-me.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = FailedDocumentAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\n\nMEDIA:{dossier}",
        final_response=f"Retrying the dossier.\nMEDIA:{dossier}",
        session_id="sess-clarify-failed-predelivery",
        adapter=adapter,
    )

    assert ("document-failed", str(dossier)) in adapter.events
    clarify_index = next(i for i, event in enumerate(adapter.events) if event[0] == "clarify")
    assert not any(
        kind == "text" and "Dossier ready" in payload
        for kind, payload in adapter.events[:clarify_index]
    )
    assert f"MEDIA:{dossier}" in result["final_response"]


@pytest.mark.asyncio
async def test_fallback_notice_is_not_counted_as_native_delivery(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "fallback.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = FallbackDocumentAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response=f"Retrying.\nMEDIA:{dossier}",
        session_id="sess-clarify-fallback-notice",
        adapter=adapter,
    )

    assert any(
        kind == "text" and "Couldn't deliver" in payload
        for kind, payload in adapter.events
    )
    assert not any(
        kind == "text" and "Dossier ready" in payload
        for kind, payload in adapter.events
    )
    assert f"MEDIA:{dossier}" in result["final_response"]


@pytest.mark.asyncio
async def test_rewritten_file_at_same_path_is_not_deduplicated(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "rewritten.txt"
    dossier.write_text("version one\n", encoding="utf-8")

    def rewrite():
        dossier.write_text("version two is longer\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"First version.\nMEDIA:{dossier}",
        final_response=f"Updated version.\nMEDIA:{dossier}",
        session_id="sess-clarify-rewritten",
        after_clarify=rewrite,
    )

    assert ("document", str(dossier)) in adapter.events
    assert f"MEDIA:{dossier}" in result["final_response"]


@pytest.mark.asyncio
async def test_rewrite_during_upload_keeps_new_version_for_final_delivery(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "rewrite-during-upload.txt"
    dossier.write_text("version one\n", encoding="utf-8")

    def rewrite():
        dossier.write_text("version two is longer\n", encoding="utf-8")

    adapter = RewriteDuringUploadAdapter(platform=Platform.TELEGRAM)
    RewriteDuringUploadAdapter.rewrite = rewrite
    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"First version.\nMEDIA:{dossier}",
        final_response=f"Updated version.\nMEDIA:{dossier}",
        session_id="sess-rewrite-during-upload",
        adapter=adapter,
    )

    assert f"MEDIA:{dossier}" in result["final_response"]


@pytest.mark.asyncio
async def test_failed_second_version_does_not_inherit_first_acknowledgement(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "two-version-failure.txt"
    dossier.write_text("version one\n", encoding="utf-8")
    commentary = f"Dossier ready.\nMEDIA:{dossier}"

    def rewrite():
        dossier.write_text("version two is longer\n", encoding="utf-8")

    FailSecondDocumentAdapter.sends = 0
    adapter = FailSecondDocumentAdapter(platform=Platform.TELEGRAM)
    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=commentary,
        second_commentary=commentary,
        before_second_clarify=rewrite,
        final_response="",
        session_id="sess-failed-second-version",
        adapter=adapter,
    )

    assert ("document-failed", str(dossier)) in adapter.events
    assert result["delivery_satisfied"] is False
    assert "no response was generated" in result["final_response"].lower()


@pytest.mark.asyncio
async def test_deleted_file_remains_acknowledged_after_clarify(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "deleted-after-send.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    response = f"Dossier ready.\nMEDIA:{dossier}"

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=response,
        final_response=response,
        session_id="sess-clarify-deleted-after-send",
        after_clarify=dossier.unlink,
    )

    assert ("document", str(dossier)) in adapter.events
    assert result["final_response"] == ""
    assert result["delivery_satisfied"] is True


@pytest.mark.asyncio
async def test_rewritten_acknowledged_file_satisfies_an_empty_final(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "rewritten-empty.txt"
    dossier.write_text("version one\n", encoding="utf-8")

    def rewrite():
        dossier.write_text("version two is longer\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response="",
        session_id="sess-clarify-rewritten-empty",
        after_clarify=rewrite,
    )

    assert ("document", str(dossier)) in adapter.events
    assert result["final_response"] == ""
    assert result["delivery_satisfied"] is True


@pytest.mark.asyncio
async def test_rewritten_same_path_is_redelivered_before_second_clarify(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "multi-clarify.txt"
    dossier.write_text("version one\n", encoding="utf-8")
    commentary = f"Dossier ready.\nMEDIA:{dossier}"

    def rewrite():
        dossier.write_text("version two is longer\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=commentary,
        second_commentary=commentary,
        before_second_clarify=rewrite,
        final_response=f"Latest dossier.\nMEDIA:{dossier}",
        session_id="sess-clarify-rewritten-second-poll",
    )

    assert sum(kind == "document" for kind, _ in adapter.events) == 2
    assert sum(kind == "clarify" for kind, _ in adapter.events) == 2
    assert str(dossier) not in result["final_response"]


@pytest.mark.asyncio
async def test_clarify_is_not_registered_until_predelivery_finishes(
    monkeypatch, tmp_path
):
    from tools import clarify_gateway

    dossier = tmp_path / "slow-register.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = SlowDocumentAdapter(platform=Platform.TELEGRAM)

    turn = asyncio.create_task(
        _run_clarify_turn(
            monkeypatch,
            tmp_path,
            commentary=f"Dossier ready.\nMEDIA:{dossier}",
            final_response=f"Done.\nMEDIA:{dossier}",
            session_id="sess-clarify-register-after-upload",
            adapter=adapter,
        )
    )
    await asyncio.sleep(0.05)

    assert clarify_gateway.get_pending_for_session("telegram:chat") is None
    await turn


@pytest.mark.asyncio
async def test_note_failure_preserves_upload_acknowledgement(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "note-failure.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = FailedNoteAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response=f"Dossier ready.\nMEDIA:{dossier}",
        session_id="sess-clarify-note-failure",
        adapter=adapter,
    )

    assert sum(kind == "document" for kind, _ in adapter.events) == 1
    assert str(dossier) not in result["final_response"]


@pytest.mark.asyncio
async def test_invalidated_turn_does_not_send_note_or_clarify(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "cancelled-turn.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    current = {"value": True}
    InvalidatingDocumentAdapter.invalidate = lambda: current.update(value=False)
    adapter = InvalidatingDocumentAdapter(platform=Platform.TELEGRAM)

    adapter, _result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response=f"Done.\nMEDIA:{dossier}",
        session_id="sess-clarify-invalidated",
        adapter=adapter,
        run_generation=1,
        is_current=lambda: current["value"],
    )

    assert ("document", str(dossier)) in adapter.events
    assert not any(kind == "clarify" for kind, _ in adapter.events)
    assert not any(
        kind == "text" and "Dossier ready" in payload
        for kind, payload in adapter.events
    )


@pytest.mark.asyncio
async def test_case_insensitive_media_tag_is_rescued_and_deduplicated(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "mixed-case.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMedia:{dossier}",
        final_response=f"Here it is.\nmedia:{dossier}",
        session_id="sess-clarify-mixed-case",
    )

    assert sum(kind == "document" for kind, _ in adapter.events) == 1
    assert str(dossier) not in result["final_response"]


@pytest.mark.asyncio
async def test_media_prose_does_not_suppress_tool_attachment_auto_append(
    monkeypatch, tmp_path
):
    audio = tmp_path / "summary.ogg"
    audio.write_bytes(b"ogg")
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "tts-1", "function": {"name": "text_to_speech"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tts-1",
            "content": f"[[audio_as_voice]]\nMEDIA:{audio}",
        },
    ]

    _adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary="",
        final_response="Media: your audio summary is ready.",
        session_id="sess-media-prose-auto-append",
        invoke_clarify=False,
        messages=messages,
    )

    assert f"MEDIA:{audio}" in result["final_response"]


@pytest.mark.asyncio
async def test_unsafe_final_media_does_not_suppress_valid_tool_attachment(
    monkeypatch, tmp_path
):
    audio = tmp_path / "valid-summary.ogg"
    audio.write_bytes(b"ogg")
    missing = tmp_path / "missing.mp3"
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "tts-2", "function": {"name": "text_to_speech"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tts-2",
            "content": f"[[audio_as_voice]]\nMEDIA:{audio}",
        },
    ]

    _adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary="",
        final_response=f"Media:{missing}",
        session_id="sess-unsafe-final-valid-tool",
        invoke_clarify=False,
        messages=messages,
    )

    assert f"MEDIA:{audio}" in result["final_response"]


@pytest.mark.asyncio
async def test_later_upload_timeout_preserves_earlier_acknowledgement(
    monkeypatch, tmp_path
):
    import gateway.run as gateway_run

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_SUPPRESSED_DELIVERABLE_TIMEOUT", 0.05)
    SecondSlowDocumentAdapter.sends = 0
    adapter = SecondSlowDocumentAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Files ready.\nMEDIA:{first}\nMEDIA:{second}",
        final_response=f"Files ready.\nMEDIA:{first}\nMEDIA:{second}",
        session_id="sess-clarify-partial-timeout",
        adapter=adapter,
    )
    await asyncio.sleep(0.25)

    assert ("document", str(first)) in adapter.events
    assert ("document", str(second)) not in adapter.events
    clarify_index = next(i for i, event in enumerate(adapter.events) if event[0] == "clarify")
    assert any(
        kind == "text" and "Files ready" in payload
        for kind, payload in adapter.events[:clarify_index]
    )
    assert str(first) not in result["final_response"]
    assert f"MEDIA:{second}" in result["final_response"]

    third = tmp_path / "third.txt"
    fourth = tmp_path / "fourth.txt"
    third.write_text("third\n", encoding="utf-8")
    fourth.write_text("fourth\n", encoding="utf-8")
    SecondSlowDocumentAdapter.sends = 0
    adapter2 = SecondSlowDocumentAdapter(platform=Platform.TELEGRAM)
    _adapter2, empty_result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Files ready.\nMEDIA:{third}\nMEDIA:{fourth}",
        final_response="",
        session_id="sess-clarify-partial-timeout-empty",
        adapter=adapter2,
    )

    assert empty_result["delivery_satisfied"] is False
    assert "no response was generated" in empty_result["final_response"]


@pytest.mark.asyncio
async def test_timed_out_predelivery_is_cancelled_before_clarify(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "slow.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = SlowDocumentAdapter(platform=Platform.TELEGRAM)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_SUPPRESSED_DELIVERABLE_TIMEOUT", 0.01)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\n\nMEDIA:{dossier}",
        final_response=f"Retrying the dossier.\nMEDIA:{dossier}",
        session_id="sess-clarify-timeout",
        adapter=adapter,
    )
    await asyncio.sleep(0.25)

    assert not any(kind == "document" for kind, _ in adapter.events)
    assert any(kind == "clarify" for kind, _ in adapter.events)
    assert f"MEDIA:{dossier}" in result["final_response"]


@pytest.mark.asyncio
async def test_acknowledged_image_is_counted_as_delivered(
    monkeypatch, tmp_path
):
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    adapter = AcknowledgedImageAdapter(platform=Platform.TELEGRAM)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Chart ready.\n\nMEDIA:{image}",
        final_response=f"Here is the chart.\nMEDIA:{image}",
        session_id="sess-clarify-image-dedup",
        adapter=adapter,
    )

    assert sum(kind == "image" for kind, _ in adapter.events) == 1
    assert str(image) not in result["final_response"]


@pytest.mark.asyncio
async def test_attachment_only_final_is_a_satisfied_empty_response(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "attachment-only.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")

    _adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"MEDIA:{dossier}",
        final_response=f"MEDIA:{dossier}",
        session_id="sess-clarify-attachment-only",
    )

    assert result["final_response"] == ""
    assert result["delivery_satisfied"] is True
    from gateway.run import _normalize_empty_agent_response

    assert _normalize_empty_agent_response(result, "") == ""


def test_predelivery_does_not_hide_failed_turn():
    from gateway.run import _normalize_empty_agent_response

    result = {
        "delivery_satisfied": True,
        "failed": True,
        "error": None,
        "api_calls": 1,
    }
    normalized = _normalize_empty_agent_response(result, "")

    assert normalized.startswith("The request failed:")
    assert "unknown error" in normalized


@pytest.mark.asyncio
async def test_genuinely_empty_final_after_predelivery_is_satisfied(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "empty-final.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")

    _adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response="",
        session_id="sess-clarify-empty-final",
    )

    assert result["final_response"] == ""
    assert result["delivery_satisfied"] is True


@pytest.mark.asyncio
async def test_non_telegram_adapter_keeps_final_delivery_path(
    monkeypatch, tmp_path
):
    dossier = tmp_path / "slack-final.txt"
    dossier.write_text("quote 1\n", encoding="utf-8")
    adapter = ClarifyOrderingAdapter(platform=Platform.SLACK)

    adapter, result = await _run_clarify_turn(
        monkeypatch,
        tmp_path,
        commentary=f"Dossier ready.\nMEDIA:{dossier}",
        final_response=f"Dossier ready.\nMEDIA:{dossier}",
        session_id="sess-clarify-slack-final-only",
        adapter=adapter,
    )

    assert not any(kind == "document" for kind, _ in adapter.events)
    assert f"MEDIA:{dossier}" in result["final_response"]


class TestStripMediaTagsForPaths:
    """``strip_media_tags_for_paths`` removes only already-satisfied tags."""

    def test_removes_only_the_named_path(self, tmp_path):
        sent = tmp_path / "sent.txt"
        sent.write_text("x", encoding="utf-8")
        pending = tmp_path / "pending.csv"
        pending.write_text("x", encoding="utf-8")

        text = f"Report attached.\nMEDIA:{sent}\nMEDIA:{pending}"
        out = BasePlatformAdapter.strip_media_tags_for_paths(text, {str(sent)})

        assert str(sent) not in out
        assert f"MEDIA:{pending}" in out
        assert "Report attached." in out

    def test_unknown_path_leaves_text_unchanged(self, tmp_path):
        pending = tmp_path / "pending.csv"
        pending.write_text("x", encoding="utf-8")
        text = f"Report attached.\nMEDIA:{pending}"

        assert (
            BasePlatformAdapter.strip_media_tags_for_paths(
                text, {str(tmp_path / "never-sent.txt")}
            )
            == text
        )

    def test_empty_path_set_is_a_no_op(self, tmp_path):
        text = f"MEDIA:{tmp_path / 'x.txt'}"
        assert BasePlatformAdapter.strip_media_tags_for_paths(text, set()) == text

    def test_deleted_extensionless_path_is_still_removed(self, tmp_path):
        """A path that stopped validating after its upload still dedups.

        ``Caddyfile`` has no deliverable extension, so it routes through the
        extension-less branch. Re-running delivery policy there would forget
        an acknowledged upload the moment the file is deleted — leaving the
        model's repeated tag visible as raw ``MEDIA:`` text.
        """
        sent = tmp_path / "Caddyfile"
        sent.write_text("x", encoding="utf-8")
        acknowledged = str(sent.resolve())
        sent.unlink()

        out = BasePlatformAdapter.strip_media_tags_for_paths(
            f"Config attached.\nMEDIA:{sent}", {acknowledged}
        )

        assert "MEDIA:" not in out
        assert "Config attached." in out

    def test_expired_unknown_extension_path_is_still_removed(
        self, tmp_path, monkeypatch
    ):
        """Strict-mode recency expiry must not resurrect a delivered tag."""
        import gateway.platforms.base as base_module

        sent = tmp_path / "trace.weirdext"
        sent.write_text("x", encoding="utf-8")
        acknowledged = str(sent.resolve())
        # The upload happened while the file was inside the recency window;
        # by the time the clarify answer arrives the window has closed.
        monkeypatch.setattr(base_module, "_media_delivery_strict_mode", lambda: True)
        monkeypatch.setattr(base_module, "_media_delivery_recency_seconds", lambda: 0)
        assert base_module.validate_media_delivery_path(str(sent)) is None

        out = BasePlatformAdapter.strip_media_tags_for_paths(
            f"Trace attached.\nMEDIA:{sent}", {acknowledged}
        )

        assert "MEDIA:" not in out
        assert "Trace attached." in out

    def test_deleted_docker_path_keeps_acknowledged_host_identity(
        self, tmp_path, monkeypatch
    ):
        """Container tags dedup after the translated host file is deleted."""
        from pathlib import Path
        import gateway.platforms.base as base_module

        host_root = tmp_path / "workspace"
        host_root.mkdir()
        sent = host_root / "trace.weirdext"
        sent.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            base_module,
            "_parse_docker_volume_mounts",
            lambda: [(host_root, Path("/workspace"))],
        )
        monkeypatch.setattr(base_module, "_cache_dir_container_mounts", lambda: [])
        monkeypatch.setattr(
            base_module, "_default_docker_workspace_host_root", lambda: None
        )
        monkeypatch.setattr(
            base_module, "_docker_persistent_home_host_root", lambda: None
        )

        acknowledged = base_module.validate_media_delivery_path(
            "/workspace/trace.weirdext"
        )
        assert acknowledged == str(sent.resolve())
        sent.unlink()

        out = BasePlatformAdapter.strip_media_tags_for_paths(
            "Trace attached.\nMEDIA:/workspace/trace.weirdext",
            {acknowledged},
        )

        assert "MEDIA:" not in out
        assert "Trace attached." in out

    def test_deleted_extensionless_path_with_spaces_is_fully_removed(self, tmp_path):
        """The forward-extension walk must still span the whole path."""
        sent = tmp_path / "map data.weirdext"
        sent.write_text("x", encoding="utf-8")
        acknowledged = str(sent.resolve())
        sent.unlink()

        out = BasePlatformAdapter.strip_media_tags_for_paths(
            f"Map attached.\nMEDIA:{sent}", {acknowledged}
        )

        assert "MEDIA:" not in out
        assert "data.weirdext" not in out
        assert "Map attached." in out

    def test_undelivered_extensionless_path_survives(self, tmp_path):
        """Dedup stays per-path: an unsent tag is never stripped."""
        pending = tmp_path / "Dockerfile"
        pending.write_text("x", encoding="utf-8")
        text = f"Still sending.\nMEDIA:{pending}"

        assert (
            BasePlatformAdapter.strip_media_tags_for_paths(
                text, {str(tmp_path / "Caddyfile")}
            )
            == text
        )

    def test_equivalent_symlink_path_is_removed(self, tmp_path):
        sent = tmp_path / "sent.txt"
        sent.write_text("x", encoding="utf-8")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(sent)

        out = BasePlatformAdapter.strip_media_tags_for_paths(
            f"Report attached.\nMEDIA:{alias}", {str(sent.resolve())}
        )

        assert "MEDIA:" not in out
        assert "Report attached." in out

"""
Tests for Telegram document handling in gateway/platforms/telegram.py.

Covers: document type detection, download/cache flow, size limits,
        text injection, error handling.

Note: python-telegram-bot may not be installed in the test environment.
We mock the telegram module at import time to avoid collection errors.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_VIDEO_TYPES,
)


# ---------------------------------------------------------------------------
# Mock the telegram package if it's not installed
# ---------------------------------------------------------------------------
# Now we can safely import
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers to build mock Telegram objects
# ---------------------------------------------------------------------------

def _make_file_obj(data: bytes = b"hello"):
    """Create a mock Telegram File with download_as_bytearray."""
    f = AsyncMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    f.file_path = "documents/file.pdf"
    return f


class TimedOut(Exception):
    """Stand-in for ``telegram.error.TimedOut``.

    ``TelegramAdapter._looks_like_network_error`` classifies transient
    transport failures by class name, so this stub must be named ``TimedOut``
    for the media-download retry path to treat it as transient without the real
    python-telegram-bot package installed in the test environment.
    """


def _make_document(
    file_name="report.pdf",
    mime_type="application/pdf",
    file_size=1024,
    file_obj=None,
):
    """Create a mock Telegram Document object."""
    doc = MagicMock()
    doc.file_name = file_name
    doc.mime_type = mime_type
    doc.file_size = file_size
    doc.get_file = AsyncMock(return_value=file_obj or _make_file_obj())
    return doc


def _make_message(document=None, caption=None, media_group_id=None, photo=None):
    """Build a mock Telegram Message with the given document/photo."""
    msg = MagicMock()
    msg.message_id = 42
    msg.text = caption or ""
    msg.caption = caption
    msg.date = None
    # Media flags — all None except explicit payload
    msg.photo = photo
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.sticker = None
    msg.document = document
    msg.media_group_id = media_group_id
    # Chat / user
    msg.chat = MagicMock()
    msg.chat.id = 100
    msg.chat.type = "private"
    msg.chat.title = None
    msg.chat.full_name = "Test User"
    msg.from_user = MagicMock()
    msg.from_user.id = 1
    msg.from_user.full_name = "Test User"
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    return msg


def _make_update(msg):
    """Wrap a message in a mock Update."""
    update = MagicMock()
    update.message = msg
    return update


def _make_video(file_obj=None):
    video = MagicMock()
    video.get_file = AsyncMock(return_value=file_obj or _make_file_obj(b"video-bytes"))
    return video


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    # Capture events instead of processing them
    a.handle_message = AsyncMock()
    # After PR #28494 made the empty-allowlist callback auth fail-closed
    # (and #28492 wired _is_callback_user_authorized into _should_process_message),
    # document-routing tests need to bypass the new gate so messages from fake
    # senders reach handle_message.
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    return a


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Point document/video cache to tmp_path so tests don't touch ~/.hermes."""
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    monkeypatch.setattr(
        "gateway.platforms.base.VIDEO_CACHE_DIR", tmp_path / "video_cache"
    )
    monkeypatch.setattr(
        "gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path / "audio_cache"
    )


# ---------------------------------------------------------------------------
# TestDocumentTypeDetection
# ---------------------------------------------------------------------------

class TestDocumentTypeDetection:
    @pytest.mark.asyncio
    async def test_document_detected_explicitly(self, adapter):
        doc = _make_document()
        msg = _make_message(document=doc)
        update = _make_update(msg)
        await adapter._handle_media_message(update, MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert event.message_type == MessageType.DOCUMENT


# ---------------------------------------------------------------------------
# TestDocumentDownloadBlock
# ---------------------------------------------------------------------------

def _make_photo(file_obj=None):
    photo = MagicMock()
    photo.get_file = AsyncMock(return_value=file_obj or _make_file_obj(b"photo-bytes"))
    return photo


class TestDocumentDownloadBlock:


    @pytest.mark.asyncio
    async def test_supported_txt_injects_content(self, adapter):
        content = b"Hello from a text file"
        file_obj = _make_file_obj(content)
        doc = _make_document(
            file_name="notes.txt", mime_type="text/plain",
            file_size=len(content), file_obj=file_obj,
        )
        msg = _make_message(document=doc)
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert "Hello from a text file" in event.text
        assert "[Content of notes.txt]" in event.text

    @pytest.mark.asyncio
    async def test_supported_md_injects_content(self, adapter):
        content = b"# Title\nSome markdown"
        file_obj = _make_file_obj(content)
        doc = _make_document(
            file_name="readme.md", mime_type="text/markdown",
            file_size=len(content), file_obj=file_obj,
        )
        msg = _make_message(document=doc)
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert "# Title" in event.text

    @pytest.mark.asyncio
    async def test_caption_preserved_with_injection(self, adapter):
        content = b"file text"
        file_obj = _make_file_obj(content)
        doc = _make_document(
            file_name="doc.txt", mime_type="text/plain",
            file_size=len(content), file_obj=file_obj,
        )
        msg = _make_message(document=doc, caption="Please summarize")
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert "file text" in event.text
        assert "Please summarize" in event.text


    @pytest.mark.asyncio
    async def test_text_injection_capped(self, adapter):
        """A .txt file over 100 KB should NOT have its content injected."""
        large = b"x" * (200 * 1024)  # 200 KB
        file_obj = _make_file_obj(large)
        doc = _make_document(
            file_name="big.txt", mime_type="text/plain",
            file_size=len(large), file_obj=file_obj,
        )
        msg = _make_message(document=doc)
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())
        event = adapter.handle_message.call_args[0][0]
        # File should be cached
        assert len(event.media_urls) == 1
        # Content should NOT be injected
        assert "[Content of" not in (event.text or "")


    @pytest.mark.asyncio
    async def test_document_cache_failure_replies_and_signals_agent(self, adapter):
        """A failed document download must surface on BOTH ends, not silently.

        Regression for #23045 Bug 2: a CDN download/cache failure used to log a
        warning and fall through to an empty agent turn — user thinks the file
        arrived, agent sees nothing. Now the user gets a Telegram reply AND the
        agent's event.text carries an attempted-attachment notice.
        """
        doc = _make_document(file_name="notes.md", mime_type="text/markdown", file_size=100)
        doc.get_file = AsyncMock(side_effect=RuntimeError("Telegram CDN down"))
        msg = _make_message(document=doc)
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())

        # 1. User is told the download failed, with the filename + exception type.
        msg.reply_text.assert_awaited_once()
        reply = msg.reply_text.await_args.args[0]
        assert "Couldn't download" in reply
        assert "notes.md" in reply
        assert "RuntimeError" in reply

        # 2. The agent still gets a turn, but event.text now carries a notice so
        #    it knows an attachment was attempted and failed (not a silent empty turn).
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.media_urls == []  # nothing cached
        assert "could not be downloaded" in (event.text or "")
        assert "notes.md" in (event.text or "")


    @pytest.mark.asyncio
    async def test_voice_cache_failure_replies_and_signals_agent(self, adapter):
        """A non-transient error fails immediately (no retry) and still keeps the
        fail-closed contract (#23045 Bug 2 class): reply to the user and pass an
        agent-visible failure note onward."""
        msg = _make_message()
        msg.voice = MagicMock()
        msg.voice.file_size = 100
        msg.voice.get_file = AsyncMock(side_effect=RuntimeError("CDN down"))
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())

        # RuntimeError is not a transient transport failure — no retry.
        assert msg.voice.get_file.await_count == 1
        msg.reply_text.assert_awaited_once()
        assert "voice message" in msg.reply_text.await_args.args[0]
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert "could not be downloaded" in (event.text or "")

    @pytest.mark.asyncio
    async def test_voice_download_retries_transient_get_file_timeout(self, adapter):
        """A single transient get_file() timeout retries and succeeds silently."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        file_obj = _make_file_obj(b"voice-bytes")
        msg = _make_message()
        msg.voice = MagicMock()
        msg.voice.file_size = 100
        msg.voice.get_file = AsyncMock(side_effect=[TimedOut("CDN timeout"), file_obj])
        update = _make_update(msg)

        with patch(
            "plugins.platforms.telegram.adapter.cache_audio_from_bytes_async",
            return_value="/tmp/cached-voice.ogg",
        ) as cache_mock:
            await adapter._handle_media_message(update, MagicMock())

        assert msg.voice.get_file.await_count == 2
        cache_mock.assert_called_once_with(b"voice-bytes", ext=".ogg")
        msg.reply_text.assert_not_awaited()
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args.args[0]
        assert event.media_urls == ["/tmp/cached-voice.ogg"]
        assert event.media_types == ["audio/ogg"]

    @pytest.mark.asyncio
    async def test_voice_download_retries_transient_download_byte_timeout(self, adapter):
        """A transient download_as_bytearray() timeout also retries the whole
        get_file + download step, then succeeds silently."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        file_obj = MagicMock()
        file_obj.download_as_bytearray = AsyncMock(
            side_effect=[TimedOut("CDN timeout"), bytearray(b"voice-bytes")]
        )
        msg = _make_message()
        msg.voice = MagicMock()
        msg.voice.file_size = 100
        msg.voice.get_file = AsyncMock(return_value=file_obj)
        update = _make_update(msg)

        with patch(
            "plugins.platforms.telegram.adapter.cache_audio_from_bytes_async",
            return_value="/tmp/cached-voice.ogg",
        ) as cache_mock:
            await adapter._handle_media_message(update, MagicMock())

        assert msg.voice.get_file.await_count == 2
        assert file_obj.download_as_bytearray.await_count == 2
        cache_mock.assert_called_once_with(b"voice-bytes", ext=".ogg")
        msg.reply_text.assert_not_awaited()
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_voice_download_retry_exhaustion_preserves_failure_contract(self, adapter):
        """Persistent transient failures exhaust the bound, then keep the
        user + agent failure contract from #53912."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        msg = _make_message()
        msg.voice = MagicMock()
        msg.voice.file_size = 100
        msg.voice.get_file = AsyncMock(side_effect=TimedOut("CDN timeout"))
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())

        assert msg.voice.get_file.await_count == 2
        msg.reply_text.assert_awaited_once()
        assert "voice message" in msg.reply_text.await_args.args[0]
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert "could not be downloaded" in (event.text or "")

    @pytest.mark.asyncio
    async def test_audio_download_retries_transient_timeout(self, adapter):
        """The audio site shares the same bounded transient-retry download path."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        file_obj = _make_file_obj(b"audio-bytes")
        msg = _make_message()
        msg.audio = MagicMock()
        msg.audio.file_size = 100
        msg.audio.get_file = AsyncMock(side_effect=[TimedOut("CDN timeout"), file_obj])

        with patch(
            "plugins.platforms.telegram.adapter.cache_audio_from_bytes_async",
            return_value="/tmp/cached-audio.mp3",
        ) as cache_mock:
            await adapter._handle_media_message(_make_update(msg), MagicMock())

        assert msg.audio.get_file.await_count == 2
        cache_mock.assert_called_once_with(b"audio-bytes", ext=".mp3")
        msg.reply_text.assert_not_awaited()
        event = adapter.handle_message.call_args.args[0]
        assert event.media_urls == ["/tmp/cached-audio.mp3"]
        assert event.media_types == ["audio/mp3"]

    @pytest.mark.asyncio
    async def test_media_download_retry_attempts_are_clamped(self, adapter):
        """Attempts clamp to the 1-5 bound; a negative delay clamps to no wait."""
        adapter.config.extra.update(
            {"media_download_attempts": 99, "media_download_retry_delay_seconds": -10}
        )
        media = MagicMock()
        media.get_file = AsyncMock(side_effect=TimedOut("CDN down"))

        with pytest.raises(TimedOut):
            await adapter._download_telegram_media_bytes(media, "voice message")

        assert media.get_file.await_count == 5


# ---------------------------------------------------------------------------
# TestDirectMediaDownloadRetry
# ---------------------------------------------------------------------------

class TestDirectMediaDownloadRetry:
    """Every *direct* inbound attachment shares the bounded transient-retry
    download, not just voice/audio.

    The CDN flake is per-download, so a photo or document left on the raw
    one-shot ``get_file() + download_as_bytearray()`` pair would keep failing
    the whole turn on a single timeout. One representative case per contract —
    photo (batched), document (cached + injected), sticker (vision fallback) —
    plus an exhaustion case per user-visible failure contract; the remaining
    MIME branches route through the same helper.
    """

    @pytest.mark.asyncio
    async def test_photo_download_retries_transient_timeout(self, adapter):
        """A transient photo timeout retries, then batches exactly as it would
        have without the flake — including the file_path-derived extension."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        adapter._media_batch_delay_seconds = 0
        file_obj = _make_file_obj(b"photo-bytes")
        file_obj.file_path = "photos/screenshot.png"
        photo = MagicMock()
        photo.get_file = AsyncMock(side_effect=[TimedOut("CDN timeout"), file_obj])
        msg = _make_message(photo=[photo])

        with patch(
            "plugins.platforms.telegram.adapter.cache_image_from_bytes_async",
            return_value="/tmp/cached-photo.png",
        ) as cache_mock:
            await adapter._handle_media_message(_make_update(msg), MagicMock())
            await asyncio.sleep(0.05)  # let the photo batch flush

        assert photo.get_file.await_count == 2
        cache_mock.assert_called_once_with(b"photo-bytes", ext=".png")
        msg.reply_text.assert_not_awaited()
        event = adapter.handle_message.call_args.args[0]
        assert event.media_urls == ["/tmp/cached-photo.png"]
        assert event.media_types == ["image/png"]

    @pytest.mark.asyncio
    async def test_photo_retry_exhaustion_preserves_failure_contract(self, adapter):
        """Persistent transient failures exhaust the bound, then keep the
        #23045 fail-closed contract: user reply + agent-visible note."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        photo = MagicMock()
        photo.get_file = AsyncMock(side_effect=TimedOut("CDN timeout"))
        msg = _make_message(photo=[photo])

        await adapter._handle_media_message(_make_update(msg), MagicMock())

        assert photo.get_file.await_count == 2
        msg.reply_text.assert_awaited_once()
        reply = msg.reply_text.await_args.args[0]
        assert "photo" in reply
        assert "TimedOut" in reply
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args.args[0]
        assert event.media_urls == []
        assert "could not be downloaded" in (event.text or "")

    @pytest.mark.asyncio
    async def test_document_download_retries_transient_timeout(self, adapter):
        """A transient document timeout retries, then caches and injects the
        text content exactly as it would have without the flake."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        content = b"Hello from a text file"
        doc = _make_document(
            file_name="notes.txt", mime_type="text/plain", file_size=len(content),
        )
        doc.get_file = AsyncMock(
            side_effect=[TimedOut("CDN timeout"), _make_file_obj(content)]
        )
        msg = _make_message(document=doc)

        await adapter._handle_media_message(_make_update(msg), MagicMock())

        assert doc.get_file.await_count == 2
        msg.reply_text.assert_not_awaited()
        event = adapter.handle_message.call_args.args[0]
        assert len(event.media_urls) == 1
        assert "[Content of notes.txt]" in event.text
        assert "Hello from a text file" in event.text

    @pytest.mark.asyncio
    async def test_document_retry_exhaustion_preserves_failure_contract(self, adapter):
        """The document site's exhaustion still names the file in both the user
        reply and the agent note."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        doc = _make_document(file_name="notes.md", mime_type="text/markdown", file_size=100)
        doc.get_file = AsyncMock(side_effect=TimedOut("CDN timeout"))
        msg = _make_message(document=doc)

        await adapter._handle_media_message(_make_update(msg), MagicMock())

        assert doc.get_file.await_count == 2
        reply = msg.reply_text.await_args.args[0]
        assert "notes.md" in reply
        event = adapter.handle_message.call_args.args[0]
        assert event.media_urls == []
        assert "could not be downloaded" in (event.text or "")
        assert "notes.md" in (event.text or "")

    @pytest.mark.asyncio
    async def test_sticker_download_retries_transient_timeout(self, adapter):
        """The sticker site retries too, and a recovered download still reaches
        vision analysis rather than the emoji fallback."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        msg = _make_message()
        msg.sticker = MagicMock()
        msg.sticker.emoji = "🎉"
        msg.sticker.set_name = "party"
        msg.sticker.file_unique_id = "sticker-retry-1"
        msg.sticker.is_animated = False
        msg.sticker.is_video = False
        msg.sticker.get_file = AsyncMock(
            side_effect=[TimedOut("CDN timeout"), _make_file_obj(b"webp-bytes")]
        )
        event = MessageEvent(text="")

        with (
            patch(
                "plugins.platforms.telegram.adapter.cache_image_from_bytes_async",
                return_value="/tmp/cached-sticker.webp",
            ) as cache_mock,
            patch(
                "tools.vision_tools.vision_analyze_tool",
                new=AsyncMock(return_value='{"success": true, "analysis": "confetti"}'),
            ) as vision_mock,
        ):
            await adapter._handle_sticker(msg, event)

        assert msg.sticker.get_file.await_count == 2
        cache_mock.assert_called_once_with(b"webp-bytes", ext=".webp")
        vision_mock.assert_awaited_once()
        assert "confetti" in event.text

    @pytest.mark.asyncio
    async def test_sticker_retry_exhaustion_falls_back_to_emoji(self, adapter):
        """Sticker analysis stays best-effort: exhaustion is swallowed into the
        emoji injection, never raised at the caller."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        msg = _make_message()
        msg.sticker = MagicMock()
        msg.sticker.emoji = "🎉"
        msg.sticker.set_name = "party"
        msg.sticker.file_unique_id = "sticker-retry-2"
        msg.sticker.is_animated = False
        msg.sticker.is_video = False
        msg.sticker.get_file = AsyncMock(side_effect=TimedOut("CDN timeout"))
        event = MessageEvent(text="")

        await adapter._handle_sticker(msg, event)

        assert msg.sticker.get_file.await_count == 2
        assert "🎉" in event.text
        msg.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestRepliedMediaDownloadBlock
# ---------------------------------------------------------------------------

def _reply_to_voice_message(get_file, *, text="what did they say?", file_size=100):
    """Build a text message replying to a voice note."""
    voice = MagicMock()
    voice.file_size = file_size
    voice.get_file = get_file
    replied = _make_message()
    replied.voice = voice
    msg = _make_message()
    msg.text = text
    msg.reply_to_message = replied
    return msg, voice


class TestRepliedMediaDownloadBlock:
    """A text/command turn replying to media shares the voice site's bounded
    transient-retry download, so one CDN flake no longer drops the attachment."""

    @pytest.mark.asyncio
    async def test_replied_voice_retries_transient_timeout(self, adapter):
        """A transient get_file() timeout retries, then the voice note is cached
        and annotated exactly as it would have been without the flake."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        file_obj = _make_file_obj(b"replied-voice-bytes")
        file_obj.file_path = "voice/file_1.oga"
        msg, voice = _reply_to_voice_message(
            AsyncMock(side_effect=[TimedOut("CDN timeout"), file_obj])
        )
        event = MessageEvent(text=msg.text)

        await adapter._cache_replied_media(msg, event)

        assert voice.get_file.await_count == 2
        assert len(event.media_urls) == 1
        assert event.media_types == ["audio/ogg"]
        assert event.message_type == MessageType.AUDIO
        assert "Replied-to audio" in event.text
        assert msg.text in event.text

    @pytest.mark.asyncio
    async def test_replied_voice_non_transient_failure_is_silently_omitted(self, adapter):
        """A non-transient error fails on the first attempt. The replied-to path
        has no user/agent notice — the turn proceeds without the attachment."""
        msg, voice = _reply_to_voice_message(AsyncMock(side_effect=RuntimeError("CDN down")))
        event = MessageEvent(text=msg.text)

        await adapter._cache_replied_media(msg, event)

        assert voice.get_file.await_count == 1
        assert event.media_urls == []
        assert event.media_types == []
        assert event.text == msg.text
        msg.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replied_voice_retry_exhaustion_is_silently_omitted(self, adapter):
        """Persistent transient failures exhaust the bound and keep the same
        silent-omission semantics."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        msg, voice = _reply_to_voice_message(AsyncMock(side_effect=TimedOut("CDN timeout")))
        event = MessageEvent(text=msg.text)

        await adapter._cache_replied_media(msg, event)

        assert voice.get_file.await_count == 2
        assert event.media_urls == []
        assert event.text == msg.text
        msg.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replied_media_filename_falls_back_to_downloaded_file_path(self, adapter):
        """Media with no Telegram-supplied name still names the cache entry from
        the downloaded File — the retry helper must hand back that File, not
        only its bytes."""
        file_obj = _make_file_obj(b"\x89PNG\r\n\x1a\n replied")
        file_obj.file_path = "photos/observed.png"
        photo = MagicMock()
        photo.file_size = 100
        photo.get_file = AsyncMock(return_value=file_obj)
        replied = _make_message()
        replied.photo = [photo]
        msg = _make_message()
        msg.text = "what is this?"
        msg.reply_to_message = replied
        event = MessageEvent(text=msg.text)

        with patch("gateway.platforms.base.cache_media_bytes") as cache_mock:
            cache_mock.return_value = None
            await adapter._cache_replied_media(msg, event)

        cache_mock.assert_called_once_with(
            b"\x89PNG\r\n\x1a\n replied",
            filename="observed.png",
            mime_type="",
            default_kind="image",
        )


# ---------------------------------------------------------------------------
# TestObservedMediaDownloadBlock
# ---------------------------------------------------------------------------

def _observed_group_voice_message(get_file, *, file_size=100):
    """Build an unmentioned group message carrying a voice note."""
    voice = MagicMock()
    voice.file_size = file_size
    voice.get_file = get_file
    msg = _make_message()
    msg.chat.type = "supergroup"
    msg.voice = voice
    return msg, voice


class TestObservedMediaDownloadBlock:
    """An unmentioned group attachment shares the same bounded transient-retry
    download, so one CDN flake no longer drops it from the observed transcript."""

    @pytest.mark.asyncio
    async def test_observed_voice_retries_transient_timeout(self, adapter):
        """A transient get_file() timeout retries, then the voice note is cached
        and annotated exactly as it would have been without the flake."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        file_obj = _make_file_obj(b"observed-voice-bytes")
        msg, voice = _observed_group_voice_message(
            AsyncMock(side_effect=[TimedOut("CDN timeout"), file_obj])
        )
        event = MessageEvent(text="did anyone catch that?")

        await adapter._cache_observed_media(msg, event)

        assert voice.get_file.await_count == 2
        assert len(event.media_urls) == 1
        assert event.media_types == ["audio/ogg"]
        assert event.message_type == MessageType.AUDIO
        assert "audio 'voice.ogg' saved at:" in event.text
        assert "did anyone catch that?" in event.text
        with open(event.media_urls[0], "rb") as fh:
            assert fh.read() == b"observed-voice-bytes"

    @pytest.mark.asyncio
    async def test_observed_voice_non_transient_failure_is_silently_omitted(self, adapter):
        """A non-transient error fails on the first attempt. The observed path
        has no user/agent notice — the transcript proceeds without the note."""
        msg, voice = _observed_group_voice_message(
            AsyncMock(side_effect=RuntimeError("CDN down"))
        )
        event = MessageEvent(text="did anyone catch that?")

        await adapter._cache_observed_media(msg, event)

        assert voice.get_file.await_count == 1
        assert event.media_urls == []
        assert event.media_types == []
        assert event.text == "did anyone catch that?"
        msg.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observed_voice_retry_exhaustion_is_silently_omitted(self, adapter):
        """Persistent transient failures exhaust the bound and keep the same
        silent-omission semantics."""
        adapter.config.extra.update(
            {"media_download_attempts": 2, "media_download_retry_delay_seconds": 0}
        )
        msg, voice = _observed_group_voice_message(
            AsyncMock(side_effect=TimedOut("CDN timeout"))
        )
        event = MessageEvent(text="did anyone catch that?")

        await adapter._cache_observed_media(msg, event)

        assert voice.get_file.await_count == 2
        assert event.media_urls == []
        assert event.text == "did anyone catch that?"
        msg.reply_text.assert_not_awaited()


class TestVideoDownloadBlock:
    @pytest.mark.asyncio
    async def test_native_video_is_cached(self, adapter):
        file_obj = _make_file_obj(b"fake-mp4")
        file_obj.file_path = "videos/clip.mp4"
        msg = _make_message()
        msg.video = _make_video(file_obj)
        update = _make_update(msg)

        await adapter._handle_media_message(update, MagicMock())
        event = adapter.handle_message.call_args[0][0]
        assert event.message_type == MessageType.VIDEO
        assert len(event.media_urls) == 1
        assert os.path.exists(event.media_urls[0])
        assert event.media_types == [SUPPORTED_VIDEO_TYPES[".mp4"]]


# ---------------------------------------------------------------------------
# TestMediaGroups — media group (album) buffering
# ---------------------------------------------------------------------------

class TestMediaGroups:
    @pytest.mark.asyncio
    async def test_non_album_photo_burst_is_buffered_and_combined(self, adapter):
        first_photo = _make_photo(_make_file_obj(b"first"))
        second_photo = _make_photo(_make_file_obj(b"second"))

        msg1 = _make_message(caption="two images", photo=[first_photo])
        msg2 = _make_message(photo=[second_photo])

        with patch(
            "plugins.platforms.telegram.adapter.cache_image_from_bytes_async",
            new=AsyncMock(side_effect=["/tmp/burst-one.jpg", "/tmp/burst-two.jpg"]),
        ):
            await adapter._handle_media_message(_make_update(msg1), MagicMock())
            await adapter._handle_media_message(_make_update(msg2), MagicMock())
            assert adapter.handle_message.await_count == 0
            await asyncio.sleep(adapter.MEDIA_GROUP_WAIT_SECONDS + 0.05)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "two images"
        assert event.media_urls == ["/tmp/burst-one.jpg", "/tmp/burst-two.jpg"]
        assert len(event.media_types) == 2


# ---------------------------------------------------------------------------
# TestSendVoice — outbound audio delivery
# ---------------------------------------------------------------------------

class TestSendVoice:
    """Tests for TelegramAdapter.send_voice() routing across audio formats."""

    @pytest.fixture()
    def connected_adapter(self, adapter):
        """Adapter with a mock bot attached."""
        bot = AsyncMock()
        adapter._bot = bot
        return adapter

    @pytest.mark.asyncio
    async def test_flac_falls_back_to_document(self, connected_adapter, tmp_path):
        """Telegram sendAudio does not accept FLAC — must fall back to sendDocument."""
        audio_file = tmp_path / "clip.flac"
        audio_file.write_bytes(b"fLaC" + b"\x00" * 32)

        mock_msg = MagicMock()
        mock_msg.message_id = 101
        connected_adapter._bot.send_voice = AsyncMock()
        connected_adapter._bot.send_audio = AsyncMock()
        connected_adapter._bot.send_document = AsyncMock(return_value=mock_msg)

        result = await connected_adapter.send_voice(
            chat_id="12345",
            audio_path=str(audio_file),
            caption="Audio",
        )

        assert result.success is True
        assert result.message_id == "101"
        connected_adapter._bot.send_document.assert_awaited_once()
        connected_adapter._bot.send_audio.assert_not_awaited()
        connected_adapter._bot.send_voice.assert_not_awaited()


# ---------------------------------------------------------------------------
# TestSendDocument — outbound file attachment delivery
# ---------------------------------------------------------------------------

class TestSendDocument:
    """Tests for TelegramAdapter.send_document() — sending files to users."""

    @pytest.fixture()
    def connected_adapter(self, adapter):
        """Adapter with a mock bot attached."""
        bot = AsyncMock()
        adapter._bot = bot
        return adapter

    @pytest.mark.asyncio
    async def test_send_document_success(self, connected_adapter, tmp_path):
        """A local file is sent via bot.send_document and returns success."""
        # Create a real temp file
        test_file = tmp_path / "report.pdf"
        test_file.write_bytes(b"%PDF-1.4 fake content")

        mock_msg = MagicMock()
        mock_msg.message_id = 99
        connected_adapter._bot.send_document = AsyncMock(return_value=mock_msg)

        result = await connected_adapter.send_document(
            chat_id="12345",
            file_path=str(test_file),
            caption="Here's the report",
        )

        assert result.success is True
        assert result.message_id == "99"
        connected_adapter._bot.send_document.assert_called_once()
        call_kwargs = connected_adapter._bot.send_document.call_args[1]
        assert call_kwargs["chat_id"] == 12345
        assert call_kwargs["filename"] == "report.pdf"
        assert call_kwargs["caption"] == "Here's the report"

    @pytest.mark.asyncio
    async def test_send_document_custom_filename(self, connected_adapter, tmp_path):
        """The file_name parameter overrides the basename for display."""
        test_file = tmp_path / "doc_abc123_ugly.csv"
        test_file.write_bytes(b"a,b,c\n1,2,3")

        mock_msg = MagicMock()
        mock_msg.message_id = 100
        connected_adapter._bot.send_document = AsyncMock(return_value=mock_msg)

        result = await connected_adapter.send_document(
            chat_id="12345",
            file_path=str(test_file),
            file_name="clean_data.csv",
        )

        assert result.success is True
        call_kwargs = connected_adapter._bot.send_document.call_args[1]
        assert call_kwargs["filename"] == "clean_data.csv"


    @pytest.mark.asyncio
    async def test_send_document_caption_truncated(self, connected_adapter, tmp_path):
        """Captions longer than 1024 chars are truncated."""
        test_file = tmp_path / "data.json"
        test_file.write_bytes(b"{}")

        mock_msg = MagicMock()
        mock_msg.message_id = 101
        connected_adapter._bot.send_document = AsyncMock(return_value=mock_msg)

        long_caption = "x" * 2000
        await connected_adapter.send_document(
            chat_id="12345",
            file_path=str(test_file),
            caption=long_caption,
        )

        call_kwargs = connected_adapter._bot.send_document.call_args[1]
        assert len(call_kwargs["caption"]) == 1024

    @pytest.mark.asyncio
    async def test_send_document_api_error_falls_back(self, connected_adapter, tmp_path):
        """If Telegram API raises, falls back to base class text message."""
        test_file = tmp_path / "file.pdf"
        test_file.write_bytes(b"data")

        connected_adapter._bot.send_document = AsyncMock(
            side_effect=RuntimeError("Telegram API error")
        )

        # The base fallback calls self.send() which is also on _bot, so mock it
        # to avoid cascading errors.
        connected_adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="fallback")
        )

        result = await connected_adapter.send_document(
            chat_id="12345",
            file_path=str(test_file),
        )

        # Should have fallen back to base class
        assert result.success is True
        assert result.message_id == "fallback"


class TestTelegramPhotoBatching:
    @pytest.mark.asyncio
    async def test_flush_photo_batch_does_not_drop_newer_scheduled_task(self, adapter):
        old_task = MagicMock()
        new_task = MagicMock()
        batch_key = "session:photo-burst"
        adapter._pending_photo_batch_tasks[batch_key] = new_task
        adapter._pending_photo_batches[batch_key] = MessageEvent(
            text="",
            message_type=MessageType.PHOTO,
            source=SimpleNamespace(channel_id="chat-1"),
            media_urls=["/tmp/a.jpg"],
            media_types=["image/jpeg"],
        )

        with (
            patch("plugins.platforms.telegram.adapter.asyncio.current_task", return_value=old_task),
            patch("plugins.platforms.telegram.adapter.asyncio.sleep", new=AsyncMock()),
        ):
            await adapter._flush_photo_batch(batch_key)

        assert adapter._pending_photo_batch_tasks[batch_key] is new_task


# ---------------------------------------------------------------------------
# TestSendVideo — outbound video delivery
# ---------------------------------------------------------------------------

class TestSendVideo:
    """Tests for TelegramAdapter.send_video() — sending videos to users."""

    @pytest.fixture()
    def connected_adapter(self, adapter):
        bot = AsyncMock()
        adapter._bot = bot
        return adapter

    @pytest.mark.asyncio
    async def test_send_video_success(self, connected_adapter, tmp_path):
        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        mock_msg = MagicMock()
        mock_msg.message_id = 200
        connected_adapter._bot.send_video = AsyncMock(return_value=mock_msg)

        result = await connected_adapter.send_video(
            chat_id="12345",
            video_path=str(test_file),
            caption="Check this out",
        )

        assert result.success is True
        assert result.message_id == "200"
        connected_adapter._bot.send_video.assert_called_once()


    @pytest.mark.asyncio
    async def test_send_video_thread_id(self, connected_adapter, tmp_path):
        """metadata thread_id is forwarded as message_thread_id (required for Telegram forum groups)."""
        test_file = tmp_path / "clip.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        mock_msg = MagicMock()
        mock_msg.message_id = 201
        connected_adapter._bot.send_video = AsyncMock(return_value=mock_msg)

        await connected_adapter.send_video(
            chat_id="12345",
            video_path=str(test_file),
            metadata={"thread_id": "789"},
        )

        call_kwargs = connected_adapter._bot.send_video.call_args[1]
        assert call_kwargs["message_thread_id"] == 789

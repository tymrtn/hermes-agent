"""Lifecycle of managed staging audio (inbound voice clips in the audio cache).

A voice clip is a staging artifact: once its transcript exists, the file has no reader. Hermes
retires it instead of leaving it for the 24h sweep — but ONLY its own cache copy, and only once
nothing can still need to read it back.

The race this guards: ``_transcribe_pending_audio_event_once`` is a *peek*. The interrupt monitor,
the pending drain and a button tap can all reach the same event before its transcript cache is set,
and ``merge_pending_message_event`` can drop that cache entirely and send the whole path back over
the same clips. Deleting there would yank a file out from under an in-flight read, so the peek
keeps the clips and ``_prepare_inbound_message_text`` — which runs once, after the event has left
the pending slot — retires them.
"""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms import base
from gateway.platforms.base import merge_pending_message_event, remove_managed_cached_audio
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

SOURCE = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")


@pytest.fixture
def audio_cache(tmp_path, monkeypatch):
    """Point the managed audio cache at a temp dir and hand it back."""
    cache_dir = tmp_path / "cache" / "audio"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(base, "AUDIO_CACHE_DIR", cache_dir)
    return cache_dir


def _clip(directory, name: str = "audio_deadbeef1234.ogg"):
    path = directory / name
    path.write_bytes(b"voice")
    return path


def _runner(stt_enabled: bool = True) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=stt_enabled)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    return runner


def _voice_event(path, text: str = "") -> MessageEvent:
    return MessageEvent(
        text=text, message_type=MessageType.VOICE, source=SOURCE,
        media_urls=[str(path)], media_types=["audio/ogg"],
    )


def _transcribes(mapping):
    """Patch STT to return a per-path transcript; any unlisted path fails."""
    def _call(path, *_args, **_kwargs):
        if path in mapping:
            return {"success": True, "transcript": mapping[path], "provider": "local_command"}
        return {"success": False, "transcript": "", "error": "unlisted path"}
    return patch("tools.transcription_tools.transcribe_audio", side_effect=_call)


# ---------------------------------------------------------------------------
# remove_managed_cached_audio: only ever a direct, regular child of the cache
# ---------------------------------------------------------------------------


def test_removes_a_direct_child_of_the_cache(audio_cache):
    clip = _clip(audio_cache)
    assert remove_managed_cached_audio(str(clip)) is True
    assert not clip.exists()


def test_refuses_a_path_outside_the_cache(audio_cache, tmp_path):
    outside = tmp_path / "somebodys-recording.ogg"
    outside.write_bytes(b"voice")
    assert remove_managed_cached_audio(str(outside)) is False
    assert outside.exists()


def test_refuses_traversal_back_out_of_the_cache(audio_cache, tmp_path):
    outside = tmp_path / "somebodys-recording.ogg"
    outside.write_bytes(b"voice")
    assert remove_managed_cached_audio(str(audio_cache / ".." / ".." / outside.name)) is False
    assert outside.exists()


def test_refuses_a_nested_child_of_the_cache(audio_cache):
    nested = audio_cache / "sub"
    nested.mkdir()
    clip = _clip(nested)
    assert remove_managed_cached_audio(str(clip)) is False
    assert clip.exists()


def test_refuses_a_symlink_and_spares_its_target(audio_cache):
    target = _clip(audio_cache, "audio_target00001.ogg")
    alias = audio_cache / "audio_alias000001.ogg"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert remove_managed_cached_audio(str(alias)) is False
    assert alias.is_symlink()
    assert target.exists()


def test_refuses_a_directory(audio_cache):
    (audio_cache / "audio_dir00000001.ogg").mkdir()
    assert remove_managed_cached_audio(str(audio_cache / "audio_dir00000001.ogg")) is False


@pytest.mark.parametrize("name", ["", "gone.ogg"])
def test_missing_or_empty_names_return_false_without_raising(audio_cache, name):
    assert remove_managed_cached_audio(str(audio_cache / name)) is False


# ---------------------------------------------------------------------------
# Transcription retires the clip it consumed — and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transcribed_clip_is_retired_but_an_unmanaged_one_is_not(audio_cache, tmp_path):
    managed = _clip(audio_cache)
    unmanaged = tmp_path / "outside.ogg"
    unmanaged.write_bytes(b"voice")
    runner = _runner()

    with _transcribes({str(managed): "hello", str(unmanaged): "hello"}):
        await runner._enrich_message_with_transcription("", [str(managed)])
        await runner._enrich_message_with_transcription("", [str(unmanaged)])

    assert not managed.exists()
    assert unmanaged.exists()


@pytest.mark.asyncio
async def test_a_failed_transcription_keeps_the_clip_its_note_points_at(audio_cache):
    clip = _clip(audio_cache)
    runner = _runner()
    failure = {"success": False, "transcript": "", "error": "backend failed"}

    with patch("tools.transcription_tools.transcribe_audio", return_value=failure), \
         patch("tools.transcription_tools.transcribe_audio_local_fallback", return_value=failure):
        result, transcripts = await runner._enrich_message_with_transcription("", [str(clip)])

    assert transcripts == []
    assert clip.name in result, "the failure note offers the audio, so the audio must survive"
    assert clip.exists()


@pytest.mark.asyncio
async def test_an_inaudible_clip_is_retired_since_nothing_points_at_it(audio_cache):
    """success=True with an empty transcript asks the user to resend and names no path."""
    clip = _clip(audio_cache)
    runner = _runner()

    with _transcribes({str(clip): "   \n\t"}):
        result, transcripts = await runner._enrich_message_with_transcription("", [str(clip)])

    assert transcripts == []
    assert "empty or inaudible" in result
    assert clip.name not in result
    assert not clip.exists()


@pytest.mark.asyncio
async def test_stt_disabled_never_touches_the_clip(audio_cache):
    clip = _clip(audio_cache)
    runner = _runner(stt_enabled=False)

    result, transcripts = await runner._enrich_message_with_transcription("", [str(clip)])

    assert transcripts == []
    assert str(clip) in result
    assert clip.exists()


# ---------------------------------------------------------------------------
# The peek path must not delete what a later read still needs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pending_peek_keeps_the_clip(audio_cache):
    clip = _clip(audio_cache)
    runner = _runner()
    event = _voice_event(clip)

    with _transcribes({str(clip): "hello"}):
        _text, transcripts = await runner._transcribe_pending_audio_event_once(event)

    assert transcripts == ["hello"]
    assert clip.exists(), "a peek is not the end of the clip's life"


@pytest.mark.asyncio
async def test_a_merge_can_re_read_the_first_clip_then_both_are_retired(audio_cache):
    """The whole contract in one run: peek, merge, re-peek over the SAME clip, then prepare."""
    first = _clip(audio_cache, "audio_first000001.ogg")
    second = _clip(audio_cache, "audio_second00002.ogg")
    runner = _runner()
    event, follow_up = _voice_event(first), _voice_event(second)

    with _transcribes({str(first): "first", str(second): "second"}):
        _text, transcripts = await runner._transcribe_pending_audio_event_once(event)
        assert transcripts == ["first"]
        assert first.exists()

        # A second voice note lands while the first is still queued: the merge drops the derived
        # transcript cache, so the next read goes back over BOTH clips.
        merge_pending_message_event({"session": event}, "session", follow_up)
        assert not hasattr(event, "_gateway_pending_stt_text")

        _text, transcripts = await runner._transcribe_pending_audio_event_once(event)
        assert transcripts == ["first", "second"]
        assert first.exists() and second.exists()

        prepared = await runner._prepare_inbound_message_text(event=event, source=SOURCE, history=[])

    assert '"first"' in prepared and '"second"' in prepared
    assert not first.exists()
    assert not second.exists()


@pytest.mark.asyncio
async def test_preparing_an_unpeeked_voice_event_retires_it_too(audio_cache):
    """The ordinary path (no pending peek) retires the clip inside transcription."""
    clip = _clip(audio_cache)
    runner = _runner()
    event = _voice_event(clip)

    with _transcribes({str(clip): "hello"}):
        prepared = await runner._prepare_inbound_message_text(event=event, source=SOURCE, history=[])

    assert '"hello"' in prepared
    assert not clip.exists()


@pytest.mark.asyncio
async def test_an_audio_file_attachment_survives_preparation(audio_cache):
    """MessageType.AUDIO is a file the user sent, not staging audio — never STT, never deleted."""
    attachment = _clip(audio_cache, "audio_abc123456789_my_song.mp3")
    runner = _runner()
    event = MessageEvent(
        text="", message_type=MessageType.AUDIO, source=SOURCE,
        media_urls=[str(attachment)], media_types=["audio/mpeg"],
    )

    with patch("tools.transcription_tools.transcribe_audio",
               side_effect=AssertionError("must not be called")):
        prepared = await runner._prepare_inbound_message_text(event=event, source=SOURCE, history=[])

    assert "my_song.mp3" in prepared
    assert attachment.exists()


@pytest.mark.asyncio
async def test_preparation_keeps_a_clip_stt_could_not_read(audio_cache):
    """The failure note hands the model the audio by path, so the prepared path must spare it."""
    clip = _clip(audio_cache)
    runner = _runner()
    event = _voice_event(clip)
    failure = {"success": False, "transcript": "", "error": "backend failed"}

    with patch("tools.transcription_tools.transcribe_audio", return_value=failure), \
         patch("tools.transcription_tools.transcribe_audio_local_fallback", return_value=failure):
        await runner._transcribe_pending_audio_event_once(event)
        prepared = await runner._prepare_inbound_message_text(event=event, source=SOURCE, history=[])

    assert "could not be transcribed" in prepared
    assert clip.name in prepared
    assert clip.exists(), "the model was just told where the audio is"


@pytest.mark.asyncio
async def test_preparation_retires_only_the_clips_stt_actually_read(audio_cache):
    """A mixed batch: the readable clip goes, the unreadable one its note names stays."""
    good = _clip(audio_cache, "audio_good00000001.ogg")
    bad = _clip(audio_cache, "audio_bad000000002.ogg")
    runner = _runner()
    event = MessageEvent(
        text="", message_type=MessageType.VOICE, source=SOURCE,
        media_urls=[str(good), str(bad)], media_types=["audio/ogg", "audio/ogg"],
    )
    failure = {"success": False, "transcript": "", "error": "backend failed"}

    with _transcribes({str(good): "hello"}), \
         patch("tools.transcription_tools.transcribe_audio_local_fallback", return_value=failure):
        await runner._transcribe_pending_audio_event_once(event)
        assert event._gateway_pending_stt_consumed == [str(good)]
        prepared = await runner._prepare_inbound_message_text(event=event, source=SOURCE, history=[])

    assert '"hello"' in prepared
    assert not good.exists()
    assert bad.exists()

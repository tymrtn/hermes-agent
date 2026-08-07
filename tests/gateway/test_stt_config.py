"""Gateway STT config tests — honor stt.enabled: false from config.yaml."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def test_gateway_config_stt_disabled_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"enabled": False}})
    assert config.stt_enabled is False


def test_load_gateway_config_bridges_stt_enabled_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"stt": {"enabled": False}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_returns_tuple_for_empty_content_placeholder():
    """A successful transcription whose caption is the empty-content placeholder
    must still return the ``(text, transcripts)`` tuple.

    The Discord adapter delivers a captionless voice note as the literal
    ``"(The user sent a message with no text content)"`` placeholder. When STT
    succeeds we strip that redundant placeholder and return just the transcript
    prefix — but the method's contract (and every caller, which unpacks the
    result as ``text, transcripts = ...``) requires a 2-tuple. Returning a bare
    string here raised ``ValueError: too many values to unpack`` and dropped the
    whole voice message on the floor.
    """
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "hello from a captionless voice note",
            "provider": "local_command",
        },
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "(The user sent a message with no text content)",
            ["/tmp/voice.ogg"],
        )

    # The redundant placeholder is stripped, leaving only the transcript prefix.
    assert "hello from a captionless voice note" in result
    assert "(The user sent a message with no text content)" not in result
    # Crucially, the transcripts are still surfaced so callers can echo them.
    assert transcripts == ["hello from a captionless voice note"]


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_guards_empty_transcript():
    """success=True with an empty/whitespace transcript must not emit empty
    quotes — it gets a sentinel note and is excluded from transcripts (#41603)."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "   \n\t", "provider": "local_command"},
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "empty or inaudible" in result
    assert '""' not in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_successful_voice_transcription_deletes_only_managed_cache_file(
    tmp_path, monkeypatch
):
    from gateway import run as gateway_run
    from gateway.platforms import base

    cache_dir = tmp_path / "cache" / "audio"
    cache_dir.mkdir(parents=True)
    managed_voice = cache_dir / "audio_deadbeef1234.ogg"
    managed_voice.write_bytes(b"voice")
    outside_voice = tmp_path / "outside.ogg"
    outside_voice.write_bytes(b"voice")
    monkeypatch.setattr(base, "AUDIO_CACHE_DIR", cache_dir)

    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    result = {"success": True, "transcript": "hello", "provider": "local_command"}
    with patch("tools.transcription_tools.transcribe_audio", return_value=result):
        await runner._enrich_message_with_transcription("", [str(managed_voice)])
        await runner._enrich_message_with_transcription("", [str(outside_voice)])

    assert not managed_voice.exists()
    assert outside_voice.exists()


def test_managed_audio_cleanup_rejects_symlink(tmp_path, monkeypatch):
    from gateway.platforms import base

    cache_dir = tmp_path / "cache" / "audio"
    cache_dir.mkdir(parents=True)
    target = cache_dir / "audio_target00001.ogg"
    target.write_bytes(b"voice")
    alias = cache_dir / "audio_alias000001.ogg"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    monkeypatch.setattr(base, "AUDIO_CACHE_DIR", cache_dir)

    assert base.remove_managed_cached_audio(str(alias)) is False
    assert alias.is_symlink()
    assert target.exists()


@pytest.mark.asyncio
async def test_failed_voice_transcription_never_exposes_cache_path(tmp_path):
    from gateway.run import GatewayRunner

    voice_path = tmp_path / "secret-voice.ogg"
    voice_path.write_bytes(b"voice")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False
    failure = {"success": False, "transcript": "", "error": "backend failed"}

    with patch("tools.transcription_tools.transcribe_audio", return_value=failure), patch(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        return_value=failure,
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "", [str(voice_path)]
        )

    assert str(voice_path) not in result
    assert "could not be transcribed" in result
    assert "resend" in result
    assert transcripts == []
    assert voice_path.exists()


@pytest.mark.asyncio
async def test_pending_voice_merge_defers_cleanup_until_final_enrichment(
    tmp_path, monkeypatch
):
    from gateway import run as gateway_run
    from gateway.platforms import base
    from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event

    cache_dir = tmp_path / "cache" / "audio"
    cache_dir.mkdir(parents=True)
    first_path = cache_dir / "audio_first000001.ogg"
    second_path = cache_dir / "audio_second00002.ogg"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    monkeypatch.setattr(base, "AUDIO_CACHE_DIR", cache_dir)

    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    def transcribe(path):
        return {
            "success": True,
            "transcript": "first" if path == str(first_path) else "second",
            "provider": "local_command",
        }

    first = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        media_urls=[str(first_path)],
        media_types=["audio/ogg"],
    )
    second = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        media_urls=[str(second_path)],
        media_types=["audio/ogg"],
    )

    with patch("tools.transcription_tools.transcribe_audio", side_effect=transcribe):
        _text, transcripts = await runner._transcribe_pending_audio_event_once(first)
        assert transcripts == ["first"]
        assert first_path.exists()

        pending = {"session": first}
        merge_pending_message_event(pending, "session", second)
        assert not hasattr(first, "_gateway_pending_stt_text")

        _text, transcripts = await runner._transcribe_pending_audio_event_once(first)
        assert transcripts == ["first", "second"]
        assert first_path.exists()
        assert second_path.exists()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat",
            chat_type="private",
            user_id="user",
        )
        first.source = source
        runner.adapters = {}
        runner._model = "openai/gpt-4.1-mini"
        runner._base_url = None
        prepared = await runner._prepare_inbound_message_text(
            event=first,
            source=source,
            history=[],
        )

    assert '"first"' in prepared
    assert '"second"' in prepared

    assert not first_path.exists()
    assert not second_path.exists()



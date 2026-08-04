"""Tests for Matrix outbound message length configuration (#53026)."""
import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _make_adapter(**extra):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@bot:example.org",
            **extra,
        },
    )
    return MatrixAdapter(config)


class TestMatrixMaxMessageLength:
    def test_default_limit_is_16000(self):
        adapter = _make_adapter()
        assert adapter.max_message_length == 16000

    def test_extra_override(self):
        adapter = _make_adapter(max_message_length=12000)
        assert adapter.max_message_length == 12000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "20000")
        adapter = _make_adapter()
        assert adapter.max_message_length == 20000


    def test_invalid_values_fall_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MATRIX_MAX_MESSAGE_LENGTH", "not-a-number")
        adapter = _make_adapter()
        assert adapter.max_message_length == 16000

    def test_values_are_clamped(self):
        adapter = _make_adapter(max_message_length=100)
        assert adapter.max_message_length == 500
        adapter = _make_adapter(max_message_length=999999)
        assert adapter.max_message_length == 65535

    def test_apply_yaml_config_sets_env(self, monkeypatch):
        from plugins.platforms.matrix.adapter import _apply_yaml_config

        monkeypatch.delenv("MATRIX_MAX_MESSAGE_LENGTH", raising=False)
        _apply_yaml_config({}, {"max_message_length": 12000})
        assert os.getenv("MATRIX_MAX_MESSAGE_LENGTH") == "12000"

    def test_register_uses_default_limit(self):
        from plugins.platforms.matrix.adapter import DEFAULT_MAX_MESSAGE_LENGTH, register

        ctx = MagicMock()
        register(ctx)
        kwargs = ctx.register_platform.call_args[1]
        assert kwargs["max_message_length"] == DEFAULT_MAX_MESSAGE_LENGTH

    def test_send_uses_configured_limit(self):
        adapter = _make_adapter(max_message_length=5000)
        adapter._client = MagicMock()
        adapter._client.send_message_event = AsyncMock(return_value="evt")
        long_text = "x" * 12000

        async def _run():
            result = await adapter.send("!room:example.org", long_text)
            assert result.success
            payloads = [
                call.args[2]
                for call in adapter._client.send_message_event.await_args_list
            ]
            assert len(payloads) > 1
            assert all(len(payload["body"]) <= 5000 for payload in payloads)

        asyncio.run(_run())

    @pytest.mark.parametrize(
        "text",
        [
            "🧠" * 16000,
            ("| Name | Value |\n| --- | --- |\n| **bold** | `code` |\n" * 1200),
        ],
    )
    def test_every_outbound_payload_is_within_utf8_content_budget(self, text):
        from plugins.platforms.matrix.adapter import MATRIX_EVENT_CONTENT_BYTE_BUDGET

        adapter = _make_adapter(max_message_length=16000)
        adapter._client = MagicMock()
        adapter._client.send_message_event = AsyncMock(return_value="evt")

        result = asyncio.run(
            adapter.send(
                "!room:example.org",
                text,
                reply_to="$reply:example.org",
                metadata={"thread_id": "$thread:example.org"},
            )
        )
        payloads = [
            call.args[2]
            for call in adapter._client.send_message_event.await_args_list
        ]

        assert result.success
        assert len(payloads) > 1
        assert all(len(payload["body"]) <= adapter.max_message_length for payload in payloads)
        assert all(
            len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            <= MATRIX_EVENT_CONTENT_BYTE_BUDGET
            for payload in payloads
        )

    def test_formatted_table_stays_single_when_it_fits_both_preferences(self):
        adapter = _make_adapter(max_message_length=16000)
        table = "| Name | Value |\n| --- | --- |\n" + "".join(
            f"| row {i} | **value {i}** |\n" for i in range(250)
        )

        payloads = adapter._build_outbound_text_payloads(table)

        assert len(payloads) == 1
        assert "<table>" in payloads[0]["formatted_body"]
        assert "row 0" in payloads[0]["formatted_body"]
        assert "row 249" in payloads[0]["formatted_body"]

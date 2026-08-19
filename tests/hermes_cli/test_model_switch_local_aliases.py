"""``/model local`` and ``/model primary`` — runtime aliases, not models.

The parser flags them so every surface (CLI, gateway, TUI) routes them to the
runtime handlers instead of the model-resolution pipeline, where "local" would
be looked up as a model name on the current aggregator.
"""

import pytest

from hermes_cli.model_switch import (
    RUNTIME_ALIAS_LOCAL,
    RUNTIME_ALIAS_PRIMARY,
    parse_model_switch_args,
    resolve_local_fallback_entry,
    resolve_runtime_alias,
)


LOCAL_ENTRY = {
    "provider": "custom",
    "model": "/models/local-model",
    "base_url": "http://127.0.0.1:18765/v1",
    "start_command": "/abs/serve.command",
    "health_url": "http://127.0.0.1:18765/v1/models",
    "start_timeout": 90,
}
REMOTE_ENTRY = {
    "provider": "openrouter",
    "model": "z-ai/glm-4.7",
    "base_url": "https://openrouter.ai/api/v1",
}


class TestRuntimeAliasParsing:
    @pytest.mark.parametrize("raw", ["local", "LOCAL", " local ", "Local"])
    def test_local_alias(self, raw):
        assert parse_model_switch_args(raw).runtime_alias == RUNTIME_ALIAS_LOCAL

    @pytest.mark.parametrize("raw", ["primary", "cloud", "PRIMARY", "Cloud"])
    def test_primary_aliases(self, raw):
        assert parse_model_switch_args(raw).runtime_alias == RUNTIME_ALIAS_PRIMARY

    @pytest.mark.parametrize(
        "raw", ["", "sonnet", "z-ai/glm-4.7", "local-model", "localhost", "primary-2"]
    )
    def test_model_names_are_not_aliases(self, raw):
        assert parse_model_switch_args(raw).runtime_alias == ""

    def test_explicit_provider_suppresses_the_alias(self):
        """A provider genuinely serving a model called "local" stays reachable."""
        request = parse_model_switch_args("local --provider lmstudio")
        assert request.runtime_alias == ""
        assert request.target == "local"
        assert request.explicit_provider == "lmstudio"

    def test_alias_keeps_the_parsed_target_and_flags(self):
        request = parse_model_switch_args("local --session")
        assert request.runtime_alias == RUNTIME_ALIAS_LOCAL
        assert request.target == "local"
        assert request.scope == "session"
        assert request.errors == ()

    def test_resolve_runtime_alias_directly(self):
        assert resolve_runtime_alias("local") == RUNTIME_ALIAS_LOCAL
        assert resolve_runtime_alias("cloud") == RUNTIME_ALIAS_PRIMARY
        assert resolve_runtime_alias("local", "custom") == ""
        assert resolve_runtime_alias(None) == ""
        assert resolve_runtime_alias("sonnet") == ""


class TestResolveLocalFallbackEntry:
    def test_picks_the_startable_local_entry(self):
        entry = resolve_local_fallback_entry(
            {"fallback_providers": [dict(REMOTE_ENTRY), dict(LOCAL_ENTRY)]}
        )
        assert entry is not None
        assert entry["model"] == "/models/local-model"
        # The start keys must survive config normalization or nothing can run.
        assert entry["start_command"] == "/abs/serve.command"
        assert entry["health_url"] == "http://127.0.0.1:18765/v1/models"
        assert entry["start_timeout"] == 90

    def test_falls_back_to_a_loopback_entry_without_start_command(self):
        plain_local = {
            "provider": "custom",
            "model": "mlx-model",
            "base_url": "http://localhost:1234/v1",
        }
        entry = resolve_local_fallback_entry(
            {"fallback_providers": [dict(REMOTE_ENTRY), plain_local]}
        )
        assert entry is not None
        assert entry["model"] == "mlx-model"

    def test_prefers_startable_over_plain_loopback(self):
        plain_local = {
            "provider": "custom",
            "model": "mlx-model",
            "base_url": "http://localhost:1234/v1",
        }
        entry = resolve_local_fallback_entry(
            {"fallback_providers": [plain_local, dict(LOCAL_ENTRY)]}
        )
        assert entry["model"] == "/models/local-model"

    def test_reads_the_legacy_fallback_model_key(self):
        entry = resolve_local_fallback_entry({"fallback_model": dict(LOCAL_ENTRY)})
        assert entry is not None
        assert entry["start_command"] == "/abs/serve.command"

    def test_none_when_no_local_entry_exists(self):
        assert resolve_local_fallback_entry({"fallback_providers": [dict(REMOTE_ENTRY)]}) is None
        assert resolve_local_fallback_entry({}) is None

    def test_remote_entry_with_start_command_is_not_local(self):
        """start_command alone does not make an endpoint startable — the
        target has to be loopback."""
        remote_with_command = dict(REMOTE_ENTRY, start_command="/abs/serve.command")
        assert resolve_local_fallback_entry(
            {"fallback_providers": [remote_with_command]}
        ) is None

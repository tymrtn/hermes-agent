"""Regression test for /model context-length display on provider-capped models.

Bug (April 2026): `/model` on openai-codex (ChatGPT OAuth) could show
"Context: 1,050,000 tokens" because the display code used the raw models.dev
``ModelInfo.context_window`` (which reports the direct-OpenAI API value) instead
of the provider-aware resolver. Some Codex slugs were actually running at 272K,
so the display was lying to the user.

Fix: ``resolve_display_context_length()`` prefers
``agent.model_metadata.get_model_context_length`` (which knows about Codex OAuth,
Copilot, Nous, etc.) and falls back to models.dev only if that returns nothing.
"""
from __future__ import annotations

from unittest.mock import patch

from hermes_cli.model_switch import resolve_display_context_length


class _FakeModelInfo:
    def __init__(self, ctx):
        self.context_window = ctx


class TestResolveDisplayContextLength:
    def test_codex_oauth_overrides_models_dev(self):
        """Provider-aware resolver must win for capped Codex slugs."""
        fake_mi = _FakeModelInfo(1_050_000)  # what models.dev reports
        with patch(
            "agent.model_metadata.get_model_context_length",
            return_value=272_000,  # what Codex OAuth actually enforces
        ):
            ctx = resolve_display_context_length(
                "gpt-5.4",
                "openai-codex",
                base_url="https://chatgpt.com/backend-api/codex",
                api_key="",
                model_info=fake_mi,
            )
        assert ctx == 272_000, (
            "Codex OAuth's 272K cap must win over generic models.dev values"
        )




    def test_prefers_resolver_even_when_model_info_has_larger_value(self):
        """Invariant: provider-aware resolver is authoritative, even if models.dev
        reports a bigger window."""
        fake_mi = _FakeModelInfo(2_000_000)
        with patch(
            "agent.model_metadata.get_model_context_length", return_value=128_000
        ):
            ctx = resolve_display_context_length(
                "capped-model",
                "capped-provider",
                model_info=fake_mi,
            )
        assert ctx == 128_000

    def test_custom_providers_override_honored(self):
        """Regression for #15779: /model switch onto a custom provider must
        surface the configured per-model context_length, not the 128K/256K
        fallback.
        """
        custom_provs = [
            {
                "name": "my-custom-endpoint",
                "base_url": "https://example.invalid/v1",
                "models": {"gpt-5.5": {"context_length": 1_050_000}},
            }
        ]
        # Real resolver call — no mock — so the override path is exercised
        # through agent.model_metadata.get_model_context_length.
        from unittest.mock import patch as _p
        from agent import model_metadata as _mm
        with _p.object(_mm, "get_cached_context_length", return_value=None), \
             _p.object(_mm, "fetch_endpoint_model_metadata", return_value={}), \
             _p.object(_mm, "fetch_model_metadata", return_value={}), \
             _p.object(_mm, "is_local_endpoint", return_value=False), \
             _p.object(_mm, "_is_known_provider_base_url", return_value=False):
            ctx = resolve_display_context_length(
                "gpt-5.5",
                "custom",
                base_url="https://example.invalid/v1",
                api_key="k",
                custom_providers=custom_provs,
            )
        assert ctx == 1_050_000, (
            "custom_providers[].models.gpt-5.5.context_length=1.05M must win "
            "over probe-down fallback"
        )




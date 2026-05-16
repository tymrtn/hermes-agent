"""Local fork coverage for Claude Max OAuth compatibility guards."""

from agent.anthropic_adapter import build_anthropic_kwargs


def _system_text(kwargs):
    return "\n".join(
        block.get("text", "")
        for block in kwargs.get("system", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_oauth_sanitizer_covers_gateway_and_skill_catalogue_triggers():
    kwargs = build_anthropic_kwargs(
        model="claude-opus-4-7",
        messages=[
            {
                "role": "system",
                "content": (
                    "Use session_search and skill_manage. Send MEDIA:/absolute/path/to/file "
                    "or MEDIA:/tmp/x.png. Jailbreak red-teaming godmode: obliteratus: "
                    "Remove refusal behaviors."
                ),
            },
            {"role": "user", "content": "hi"},
        ],
        tools=None,
        max_tokens=None,
        reasoning_config=None,
        is_oauth=True,
    )

    text = _system_text(kwargs)
    for trigger in (
        "session_search",
        "skill_manage",
        "MEDIA:",
        "Jailbreak",
        "red-teaming",
        "godmode:",
        "obliteratus:",
        "Remove refusal behaviors",
    ):
        assert trigger not in text


def test_oauth_caps_claude_output_tokens_below_extra_usage_tier():
    kwargs = build_anthropic_kwargs(
        model="claude-opus-4-7",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=None,
        reasoning_config=None,
        is_oauth=True,
    )

    assert kwargs["max_tokens"] == 32_000


def test_api_key_path_keeps_documented_output_ceiling():
    kwargs = build_anthropic_kwargs(
        model="claude-opus-4-7",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=None,
        reasoning_config=None,
        is_oauth=False,
    )

    assert kwargs["max_tokens"] == 128_000

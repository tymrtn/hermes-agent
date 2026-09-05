"""Tests for GH-25255: Anthropic OAuth ``mcp__`` tool-name round-trip.

Anthropic's subscription/OAuth billing classifier treats a **single-underscore**
``mcp_`` tool name as a third-party-app fingerprint and rejects the request with
HTTP 400 "Third-party apps now draw from extra usage, not plan limits".  So on
the OAuth wire NOTHING may carry a single-underscore ``mcp_`` prefix:

  * bare native tools            ``read_file``            -> ``mcp__read_file``
  * native MCP server tools      ``mcp_linear_get_issue`` -> ``mcp__linear_get_issue``

``normalize_response`` reverses the ``mcp__`` wire name back to whatever the tool
registry knows (the single-underscore ``mcp_<server>_<tool>`` form for MCP server
tools, or the bare name for native tools) so the dispatcher is unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_use_block(name: str, block_id: str = "tc_1", input_data: dict | None = None):
    """Create a fake Anthropic tool_use content block."""
    return SimpleNamespace(
        type="tool_use",
        id=block_id,
        name=name,
        input=input_data or {"query": "test"},
    )


def _make_response(*blocks, stop_reason="end_turn"):
    """Create a fake Anthropic Messages response."""
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        model="claude-sonnet-4",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class _FakeRegistry:
    """Minimal fake tool registry for testing prefix round-trip logic."""

    def __init__(self, registered_names: set[str]):
        self._names = registered_names

    def get_entry(self, name: str):
        if name in self._names:
            return SimpleNamespace(name=name)  # truthy = tool exists
        return None


# ---------------------------------------------------------------------------
# Response side: mcp__ wire name -> registry name
# ---------------------------------------------------------------------------

class TestAnthropicMcpPrefixStrip:
    """Verify strip_tool_prefix reverses the ``mcp__`` wire prefix correctly."""

    def _get_transport(self):
        from agent.transports.anthropic import AnthropicTransport
        return AnthropicTransport()

    def test_strips_prefix_for_oauth_injected_native_tool(self):
        """``mcp__read_file`` -> ``read_file`` (bare native tool)."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file", "terminal", "web_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"


    def test_no_strip_when_flag_false(self):
        """When strip_tool_prefix=False, names are never modified."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=False)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp__read_file"

    def test_both_stripped_and_full_registered_prefers_full(self):
        """Edge case: both 'foo' and 'mcp_foo' exist in registry.

        Keep 'mcp_foo' (the original name) since it's what the LLM requested.
        """
        transport = self._get_transport()
        block = _make_tool_use_block("mcp_foo")
        response = _make_response(block)

        registry = _FakeRegistry({"foo", "mcp_foo"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        # Both exist — the condition `get_entry(stripped) and not get_entry(name)`
        # is False because get_entry(name) IS truthy, so we keep the full name.
        assert result.tool_calls[0].name == "mcp_foo"


    def test_mixed_tools_same_response(self):
        """Both OAuth and native MCP tools in the same response."""
        transport = self._get_transport()
        block1 = _make_tool_use_block("mcp_read_file", block_id="tc_1")
        block2 = _make_tool_use_block("mcp_composio_SEARCH", block_id="tc_2")
        block3 = _make_tool_use_block("mcp_composio_SEARCH", block_id="tc_3")  # also registered natively
        response = _make_response(block1, block2, block3)

        registry = _FakeRegistry({
            "read_file",  # OAuth-injected
            "mcp_composio_SEARCH",  # native MCP
        })
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 3
        # OAuth tool: stripped
        assert result.tool_calls[0].name == "read_file"
        # Native MCP: preserved (both stripped and full are registered, full wins)
        assert result.tool_calls[1].name == "mcp_composio_SEARCH"
        assert result.tool_calls[2].name == "mcp_composio_SEARCH"


    def test_no_strip_when_not_mcp_prefixed(self):
        """Non-mcp_ names are untouched regardless of strip flag."""
        transport = self._get_transport()
        block = _make_tool_use_block("web_search")
        response = _make_response(block)

        registry = _FakeRegistry({"web_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "web_search"


    def test_preserves_name_when_neither_in_registry(self):
        """When neither stripped nor full name is in registry, keep full name.

        Safety fallback: if we can't determine the type, prefer the full name
        since it's what the LLM was told about.
        """
        transport = self._get_transport()
        block = _make_tool_use_block("mcp_unknown_tool")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file"})  # neither name registered
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp_unknown_tool"


    def test_preserves_native_mcp_server_tool_name(self):
        """Native MCP tools: mcp_composio_SEARCH -> mcp_composio_SEARCH (kept).

        The tool is registered with the full mcp_ prefix in the registry.
        Stripping would break registry lookup.
        """
        transport = self._get_transport()
        block = _make_tool_use_block("mcp_composio_COMPOSIO_SEARCH_TOOLS")
        response = _make_response(block)

        registry = _FakeRegistry({
            "mcp_composio_COMPOSIO_SEARCH_TOOLS",
            "mcp_composio_COMPOSIO_GET_TOOL_SCHEMAS",
            "read_file",
        })
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp_composio_COMPOSIO_SEARCH_TOOLS"


    def test_strips_hermes_namespace_for_fork_oauth_tool(self):
        """Fork OAuth tools: mcp__hermes__read_file -> read_file (stripped)."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__hermes__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file", "terminal", "web_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"


    def test_strips_prefix_for_oauth_injected_tool(self):
        """OAuth tools: mcp_read_file -> read_file (stripped).

        The tool was registered as 'read_file' in the registry.
        Anthropic sees 'mcp_read_file' because Hermes adds the prefix.
        On response, we must strip it back to 'read_file'.
        """
        transport = self._get_transport()
        block = _make_tool_use_block("mcp_read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file", "terminal", "web_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"



class TestAnthropicOAuthAliasRoundTrip:
    """#65365: session_search / memory schemas alone deterministically trip
    Anthropic's OAuth billing classifier (verified live via the
    anthropic-ratelimit-unified-* response headers, see issue comments).
    Both are aliased to neutral wire names; normalize_response must reverse
    the mapping so the dispatcher still sees the real tool."""

    def _get_transport(self):
        from agent.transports.anthropic import AnthropicTransport
        return AnthropicTransport()

    def test_oauth_session_search_alias_round_trips_to_registry_name(self):
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__chat_history_lookup")
        response = _make_response(block)

        registry = _FakeRegistry({"session_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "session_search"

    def test_oauth_memory_alias_round_trips_to_registry_name(self):
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__context_notes")
        response = _make_response(block)

        registry = _FakeRegistry({"memory"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "memory"

    def test_registered_tool_wins_over_oauth_alias(self):
        """A real tool actually registered under the wire name keeps
        GH-25255 precedence — the alias must not hijack it."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__chat_history_lookup")
        response = _make_response(block)

        registry = _FakeRegistry({"mcp_chat_history_lookup", "session_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp_chat_history_lookup"






# ---------------------------------------------------------------------------
# Request side: registry name -> mcp__ wire name (no single-underscore leaks)
# ---------------------------------------------------------------------------

class TestAnthropicOAuthOutgoingPrefix:
    """build_anthropic_kwargs must emit ZERO single-underscore ``mcp_`` names on
    the OAuth wire — bare names and MCP server names both land on ``mcp__``."""

    def _build(self, tools, is_oauth=True, messages=None, tool_choice=None):
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages or [{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            tool_choice=tool_choice,
            is_oauth=is_oauth,
        )


    def test_oauth_promotes_single_underscore_mcp_server_tool(self):
        """OAuth + ``mcp_<server>_<tool>`` -> promoted to double underscore.

        This is the gap left by the bare constant swap: MCP server tools used
        to be *skipped* and went on the wire single-underscore, still tripping
        the classifier.  They must become ``mcp__`` and NOT be double-prefixed.
        """
        kwargs = self._build([{
            "type": "function",
            "function": {
                "name": "mcp_linear_get_issue",
                "description": "x",
                "parameters": {},
            },
        }])
        names = [t["name"] for t in kwargs["tools"]]
        assert names == ["mcp__linear_get_issue"]
        # never double-prefixed
        assert not any(n.startswith("mcp__mcp_") for n in names)


    def test_oauth_no_single_underscore_mcp_on_wire(self):
        """Mixed set: every wire name is bare-free of single-underscore mcp_."""
        kwargs = self._build([
            {"type": "function", "function": {"name": "read_file",
                                              "description": "x", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp_linear_get_issue",
                                              "description": "y", "parameters": {}}},
            {"type": "function", "function": {"name": "terminal",
                                              "description": "z", "parameters": {}}},
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__linear_get_issue", "mcp__read_file", "mcp__terminal"]
        # The core invariant: NOTHING single-underscore reaches the wire.
        for n in names:
            assert not (n.startswith("mcp_") and not n.startswith("mcp__"))

    def test_non_oauth_path_untouched(self):
        """Non-OAuth requests never get the prefix — schemas pass through as-is."""
        kwargs = self._build([
            {"type": "function", "function": {"name": "read_file",
                                               "description": "x", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp_composio_SEARCH",
                                               "description": "y", "parameters": {}}},
        ], is_oauth=False)
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp_composio_SEARCH", "read_file"]


    def test_oauth_adds_prefix_to_bare_tool_name(self):
        """OAuth + bare name → prefix added (existing Claude Code convention)."""
        kwargs = self._build([{
            "type": "function",
            "function": {"name": "read_file", "description": "x", "parameters": {}},
        }])
        names = [t["name"] for t in kwargs["tools"]]
        assert names == ["mcp__hermes__read_file"]


    def test_oauth_does_not_double_prefix_native_mcp_tool(self):
        """OAuth + already-prefixed native MCP name → left alone."""
        kwargs = self._build([{
            "type": "function",
            "function": {
                "name": "mcp_composio_COMPOSIO_SEARCH_TOOLS",
                "description": "x",
                "parameters": {},
            },
        }])
        names = [t["name"] for t in kwargs["tools"]]
        # Must NOT become "mcp_mcp_composio_..." — that breaks the round-trip
        # because normalize_response only strips ONE mcp_ prefix.
        assert names == ["mcp_composio_COMPOSIO_SEARCH_TOOLS"]


    def test_oauth_mixed_native_and_bare_tools(self):
        """Mixed: native MCP preserved, bare names prefixed."""
        kwargs = self._build([
            {"type": "function", "function": {"name": "read_file",
                                               "description": "x", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp_composio_SEARCH",
                                               "description": "y", "parameters": {}}},
            {"type": "function", "function": {"name": "terminal",
                                               "description": "z", "parameters": {}}},
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__hermes__read_file", "mcp__hermes__terminal", "mcp_composio_SEARCH"]



# ---------------------------------------------------------------------------
# #65365: session_search / memory OAuth billing-classifier trigger
# ---------------------------------------------------------------------------

class TestAnthropicOAuthClassifierAlias:
    """OAuth must alias the two schemas issue #65365 isolated as independent,
    deterministic triggers (session_search alone, memory alone) — in tool
    name, tool description, system-prompt prose, and named tool_choice."""

    def _build(self, tools, is_oauth=True, messages=None, tool_choice=None):
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages or [{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            tool_choice=tool_choice,
            is_oauth=is_oauth,
        )

    def _tool(self, name, description="x"):
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {}}}

    def test_oauth_aliases_session_search_and_memory_names(self):
        kwargs = self._build([
            self._tool("session_search", "Use session_search to recall prior chats."),
            self._tool("memory", "Persist notes with memory."),
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__chat_history_lookup", "mcp__context_notes"]

    def test_oauth_aliases_session_search_in_tool_description_not_memory(self):
        """session_search is prose-safe (unambiguous token); memory is
        ordinary English and must NOT be rewritten in free text — only its
        tool name is aliased."""
        kwargs = self._build([
            self._tool("session_search", "Call session_search to recall prior chats."),
            self._tool("memory", "Persist notes; uses working memory internally."),
        ])
        by_name = {t["name"]: t["description"] for t in kwargs["tools"]}
        assert "chat_history_lookup" in by_name["mcp__chat_history_lookup"]
        assert "session_search" not in by_name["mcp__chat_history_lookup"]
        assert "memory" in by_name["mcp__context_notes"]  # untouched prose

    def test_oauth_aliases_session_search_in_system_prompt_prose(self):
        kwargs = self._build(
            [self._tool("session_search")],
            messages=[
                {"role": "system", "content": "When relevant, use session_search to recall it."},
                {"role": "user", "content": "Hi"},
            ],
        )
        system_text = "\n".join(
            b["text"] for b in kwargs["system"] if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "chat_history_lookup" in system_text
        assert "session_search" not in system_text

    def test_oauth_does_not_alias_longer_identifier_containing_token(self):
        """Word-boundary matching: a path like tools/session_search_tool.py
        must survive untouched, not become chat_history_lookup_tool.py."""
        kwargs = self._build(
            [self._tool("session_search")],
            messages=[
                {"role": "system", "content": "See tools/session_search_tool.py for details."},
                {"role": "user", "content": "Hi"},
            ],
        )
        system_text = "\n".join(
            b["text"] for b in kwargs["system"] if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "tools/session_search_tool.py" in system_text

    def test_oauth_tool_choice_named_alias_matches_wire_name(self):
        """The gap left open by prior alias work: a forced tool_choice must
        be normalized through the same alias + mcp__ prefix as tools[], or
        (a) the literal trigger string still reaches the wire and (b) the
        name no longer matches any entry in tools[]."""
        kwargs = self._build(
            [self._tool("session_search")],
            tool_choice="session_search",
        )
        assert kwargs["tool_choice"] == {"type": "tool", "name": "mcp__chat_history_lookup"}
        assert kwargs["tool_choice"]["name"] in {t["name"] for t in kwargs["tools"]}

    def test_oauth_tool_choice_bare_name_still_gets_mcp_prefix(self):
        """Non-aliased tool_choice names still need the mcp__ prefix under
        OAuth (pre-existing GH-25255 invariant, now routed consistently)."""
        kwargs = self._build(
            [self._tool("read_file")],
            tool_choice="read_file",
        )
        assert kwargs["tool_choice"] == {"type": "tool", "name": "mcp__read_file"}

    def test_oauth_skips_alias_on_wire_name_collision(self):
        """If a real (e.g. MCP server) tool already owns the alias's wire
        name, session_search must keep its own name rather than collide —
        two identical tool names is a hard 400, strictly worse than #65365."""
        kwargs = self._build([
            self._tool("session_search"),
            self._tool("mcp_chat_history_lookup"),
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__chat_history_lookup", "mcp__session_search"]

    def test_api_key_path_never_aliases(self):
        """The alias is OAuth-only — API-key requests must be byte-identical
        to before this fix."""
        kwargs = self._build(
            [self._tool("session_search", "Use session_search to recall prior chats.")],
            is_oauth=False,
        )
        assert kwargs["tools"][0]["name"] == "session_search"
        assert "session_search" in kwargs["tools"][0]["description"]

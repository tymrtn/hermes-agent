"""Regression coverage for shared-user memory during compression prompt reuse."""

from types import SimpleNamespace

from agent.conversation_compression import _cached_prompt_reflects_builtin_memory
from agent.context_breakdown import _memory_blocks


class _Store:
    def __init__(self, blocks):
        self.blocks = blocks

    def format_for_system_prompt(self, target):
        return self.blocks.get(target)


def test_shared_user_change_invalidates_cached_prompt():
    agent = SimpleNamespace(
        _memory_store=_Store({
            "memory": "",
            "shared_user": "SHARED USER PROFILE (read-only, shared across profiles)\nnew identity",
            "user": "USER PROFILE (who the user is)\nlocal delta",
        }),
        _memory_enabled=False,
        _user_profile_enabled=True,
    )
    stale = (
        "SHARED USER PROFILE (read-only, shared across profiles)\nold identity\n\n"
        "USER PROFILE (who the user is)\nlocal delta"
    )

    assert _cached_prompt_reflects_builtin_memory(agent, stale) is False


def test_shared_user_current_block_allows_cache_retention():
    shared = "SHARED USER PROFILE (read-only, shared across profiles)\nshared identity"
    local = "USER PROFILE (who the user is)\nlocal delta"
    agent = SimpleNamespace(
        _memory_store=_Store({"memory": "", "shared_user": shared, "user": local}),
        _memory_enabled=False,
        _user_profile_enabled=True,
    )

    assert _cached_prompt_reflects_builtin_memory(agent, f"{shared}\n\n{local}") is True


def test_context_breakdown_treats_shared_user_as_memory():
    shared = "SHARED USER PROFILE\nshared identity"
    local = "USER PROFILE\nlocal delta"
    agent = SimpleNamespace(
        _memory_store=_Store({"memory": "", "shared_user": shared, "user": local}),
        _memory_enabled=False,
        _user_profile_enabled=True,
    )

    assert _memory_blocks(agent) == ("", shared, local)
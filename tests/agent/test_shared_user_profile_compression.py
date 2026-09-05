"""Regression coverage for shared-user memory during compression prompt reuse."""

from types import SimpleNamespace

from agent.context_breakdown import _memory_blocks


class _Store:
    def __init__(self, blocks):
        self.blocks = blocks

    def format_for_system_prompt(self, target):
        return self.blocks.get(target)






def test_context_breakdown_treats_shared_user_as_memory():
    shared = "SHARED USER PROFILE\nshared identity"
    local = "USER PROFILE\nlocal delta"
    agent = SimpleNamespace(
        _memory_store=_Store({"memory": "", "shared_user": shared, "user": local}),
        _memory_enabled=False,
        _user_profile_enabled=True,
    )

    assert _memory_blocks(agent) == ("", shared, local)
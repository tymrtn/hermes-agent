"""Security regression tests: Discord authorization is profile-bound.

Two defects are pinned here.

1. Process-env drift under profile multiplexing (High). One gateway process
   serves many profiles. Each adapter connects under its own
   ``_profile_runtime_scope`` (gateway.run), but the message / component /
   slash / channel callbacks fire LATER, after that scope has exited. A bare
   ``os.getenv`` in those callbacks reads the process-global environment —
   which holds the *default* profile's values — so a secondary profile would
   silently inherit the default profile's allow-all flag and allowlists.
   The fix captures a profile-bound ``_DiscordAuthPolicy`` while ``connect()``
   still runs under the profile scope and uses it for later auth. Legacy /
   direct-test behavior is preserved when no policy was captured.

2. Paired role-policy DM rejected before pairing (Medium). ``_component_check_auth``
   used to fail closed on a role-configured DM ``User`` that carried no
   ``.roles`` *before* consulting the pairing store. Pairing is an independent
   OR grant, so it must be checked before the role-data fail-closed.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Trigger the shared discord mock from tests/gateway/conftest.py before
# importing the production module.
from plugins.platforms.discord.adapter import (  # noqa: E402
    DiscordAdapter,
    ExecApprovalView,
    _DiscordAuthPolicy,
    _capture_discord_auth_policy,
    _component_check_auth,
)


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch):
    for name in (
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Default-mock PairingStore so tests don't touch the filesystem.
    mock_store = MagicMock()
    mock_store.is_approved.return_value = False
    with patch("gateway.pairing.PairingStore", return_value=mock_store):
        yield


def _interaction(user_id, role_ids=None, *, drop_user=False, drop_roles=False):
    if drop_user:
        return SimpleNamespace(user=None)
    user_kwargs = {"id": user_id}
    if not drop_roles:
        user_kwargs["roles"] = [SimpleNamespace(id=r) for r in (role_ids or [])]
    return SimpleNamespace(user=SimpleNamespace(**user_kwargs))


def _policy(*, allow_all=False, gateway_users=(), allowed_channels=(), ignored_channels=()):
    return _DiscordAuthPolicy(
        allow_all=allow_all,
        gateway_allowed_user_ids=frozenset(gateway_users),
        allowed_channels=frozenset(allowed_channels),
        ignored_channels=frozenset(ignored_channels),
    )


# ---------------------------------------------------------------------------
# Finding 2: pairing is an OR grant, checked before role-data fail-closed.
# ---------------------------------------------------------------------------


def test_paired_dm_with_role_policy_and_no_roles_passes():
    """Role policy active, DM ``User`` has no ``.roles``, but the user is
    paired: pairing is an independent OR grant and must authorize."""
    store = MagicMock()
    store.is_approved.return_value = True
    with patch("gateway.pairing.PairingStore", return_value=store):
        interaction = _interaction(11111, drop_roles=True)
        assert _component_check_auth(interaction, set(), {42}) is True
    store.is_approved.assert_called_once_with("discord", "11111")


def test_unpaired_dm_with_role_policy_and_no_roles_rejected():
    """Same shape but NOT paired: still fail closed (role data absent)."""
    store = MagicMock()
    store.is_approved.return_value = False
    with patch("gateway.pairing.PairingStore", return_value=store):
        interaction = _interaction(11111, drop_roles=True)
        assert _component_check_auth(interaction, set(), {42}) is False


def test_paired_dm_with_noniterable_roles_passes():
    """A role payload whose ``.roles`` isn't iterable must not short-circuit
    the pairing OR grant either."""
    store = MagicMock()
    store.is_approved.return_value = True
    user = SimpleNamespace(id=11111, roles=object())  # not iterable
    interaction = SimpleNamespace(user=user)
    with patch("gateway.pairing.PairingStore", return_value=store):
        assert _component_check_auth(interaction, set(), {42}) is True


# ---------------------------------------------------------------------------
# Finding 1: captured policy overrides drifted process env (components).
# ---------------------------------------------------------------------------


def test_component_captured_policy_ignores_process_env_allow_all(monkeypatch):
    """A captured policy with allow_all=False rejects even when the process
    env (the default profile's leaked value) says DISCORD_ALLOW_ALL_USERS."""
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    interaction = _interaction(99999)
    assert _component_check_auth(interaction, set(), set(), policy=_policy()) is False
    # Legacy (no policy) path still honors env allow-all — behavior preserved.
    assert _component_check_auth(interaction, set(), set()) is True


def test_component_captured_policy_allow_all_grants(monkeypatch):
    """allow_all captured from the profile grants even with no env set."""
    interaction = _interaction(99999)
    assert (
        _component_check_auth(interaction, set(), set(), policy=_policy(allow_all=True))
        is True
    )


def test_component_captured_policy_gateway_users(monkeypatch):
    """Gateway allowlist comes from the captured policy, not GATEWAY_ALLOWED_USERS env."""
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "77777")  # default profile leak
    pol = _policy(gateway_users=["11111"])
    assert _component_check_auth(_interaction(11111), set(), set(), policy=pol) is True
    # The leaked env user must NOT authorize under the captured policy.
    assert _component_check_auth(_interaction(77777), set(), set(), policy=pol) is False


# ---------------------------------------------------------------------------
# Finding 1: view threading — a view built with auth_policy uses it.
# ---------------------------------------------------------------------------


def test_exec_view_prefers_captured_policy_over_env(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    view = ExecApprovalView(
        session_key="s", allowed_user_ids=set(), auth_policy=_policy()
    )
    assert view._check_auth(_interaction(99999)) is False
    # Legacy view (no policy) honors env allow-all.
    legacy = ExecApprovalView(session_key="s", allowed_user_ids=set())
    assert legacy._check_auth(_interaction(99999)) is True


# ---------------------------------------------------------------------------
# Finding 1: policy capture reads the active secret scope, not os.environ.
# ---------------------------------------------------------------------------


def test_capture_reads_profile_scope_not_process_env(monkeypatch):
    from agent import secret_scope

    # Process env holds the DEFAULT profile's (permissive) values.
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "111")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)

    # The routed (secondary) profile is stricter: no allow-all, its own channel.
    scope = {"DISCORD_ALLOWED_CHANNELS": "222", "GATEWAY_ALLOWED_USERS": "42"}
    token = secret_scope.set_secret_scope(scope)
    try:
        policy = _capture_discord_auth_policy()
    finally:
        secret_scope.reset_secret_scope(token)

    assert policy.allow_all is False, "default profile's allow-all must not leak"
    assert policy.allowed_channels == frozenset({"222"})
    assert policy.gateway_allowed_user_ids == frozenset({"42"})
    assert policy.ignored_channels == frozenset()


# ---------------------------------------------------------------------------
# Finding 1: adapter accessors and _is_allowed_user prefer the captured policy.
# ---------------------------------------------------------------------------


def test_adapter_accessors_prefer_captured_policy(monkeypatch):
    adapter = object.__new__(DiscordAdapter)
    adapter._auth_policy = _policy(
        allow_all=False, allowed_channels=["222"], ignored_channels=["999"]
    )
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "111")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "888")

    assert adapter._auth_allow_all() is False
    assert adapter._auth_allowed_channels() == frozenset({"222"})
    assert adapter._auth_ignored_channels() == frozenset({"999"})

    # No captured policy -> live process env (legacy behavior preserved).
    adapter._auth_policy = None
    assert adapter._auth_allow_all() is True
    assert adapter._auth_allowed_channels() == frozenset({"111"})
    assert adapter._auth_ignored_channels() == frozenset({"888"})


def test_is_allowed_user_ignores_env_allow_all_when_policy_denies(monkeypatch):
    adapter = object.__new__(DiscordAdapter)
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    adapter._auth_policy = _policy(allow_all=False)
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")  # default profile leak

    store = MagicMock()
    store.is_approved.return_value = False
    with patch("gateway.pairing.PairingStore", return_value=store):
        assert adapter._is_allowed_user("999", None, is_dm=True) is False

    # Legacy: no captured policy -> env allow-all is honored (unchanged).
    adapter._auth_policy = None
    with patch("gateway.pairing.PairingStore", return_value=store):
        assert adapter._is_allowed_user("999", None, is_dm=True) is True


@pytest.mark.asyncio
async def test_profile_bound_username_resolution_does_not_mutate_process_env(monkeypatch):
    """Resolving a secondary profile's username must not overwrite the
    default profile's process-global DISCORD_ALLOWED_USERS value."""
    from gateway.config import PlatformConfig

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._allowed_user_ids = {"alice"}
    adapter._auth_policy = _policy()
    member = SimpleNamespace(
        id=222,
        name="alice",
        display_name="Alice",
        global_name=None,
        discriminator="0",
    )
    guild = SimpleNamespace(
        name="guild",
        members=[member],
        member_count=1,
    )
    adapter._client = SimpleNamespace(guilds=[guild])
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "111")

    await adapter._resolve_allowed_usernames()

    assert adapter._allowed_user_ids == {"222"}
    assert os.environ["DISCORD_ALLOWED_USERS"] == "111"


# ---------------------------------------------------------------------------
# Finding 1: connect() captures the policy while under the profile scope, and
# binds the Discord user allowlist to the routed profile (not the process env).
# ---------------------------------------------------------------------------


class _FakeTree:
    def __init__(self):
        from unittest.mock import AsyncMock

        self.sync = AsyncMock(return_value=[])
        self.fetch_commands = AsyncMock(return_value=[])

    def command(self, *a, **k):
        return lambda fn: fn

    def get_commands(self, *a, **k):
        return []


class _FakeBot:
    def __init__(self, **_):
        self.user = SimpleNamespace(id=999, name="Hermes")
        self.application_id = 999
        self._events = {}
        self.tree = _FakeTree()

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    async def start(self, token):
        if "on_ready" in self._events:
            await self._events["on_ready"]()

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_connect_captures_profile_policy_under_scope(monkeypatch):
    import plugins.platforms.discord.adapter as discord_platform
    from unittest.mock import AsyncMock
    from agent import secret_scope
    from gateway.config import PlatformConfig

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))

    # Process env carries the DEFAULT profile's permissive config.
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "123456789")
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "111")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)

    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock", lambda scope, identity: None
    )
    intents = SimpleNamespace(
        message_content=False, dm_messages=False, guild_messages=False,
        members=False, voice_states=False,
    )
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: intents)
    monkeypatch.setattr(
        discord_platform.commands, "Bot", lambda **kwargs: _FakeBot(**kwargs)
    )
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    # The routed (secondary) profile's secrets: stricter, distinct values.
    scope = {"DISCORD_ALLOWED_USERS": "555", "DISCORD_ALLOWED_CHANNELS": "222"}
    token = secret_scope.set_secret_scope(scope)
    try:
        ok = await adapter.connect()
    finally:
        secret_scope.reset_secret_scope(token)

    assert ok is True
    # The Discord user allowlist is bound to the routed profile, not the env.
    assert adapter._allowed_user_ids == {"555"}
    # A policy was captured and it reflects the profile, not the leaked env.
    assert adapter._auth_policy is not None
    assert adapter._auth_policy.allow_all is False
    assert adapter._auth_allowed_channels() == frozenset({"222"})

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_connect_single_profile_leaves_policy_none(monkeypatch):
    """Single-profile (multiplex inactive): no capture -> live env reads."""
    import plugins.platforms.discord.adapter as discord_platform
    from unittest.mock import AsyncMock
    from agent import secret_scope
    from gateway.config import PlatformConfig

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "777")

    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock", lambda scope, identity: None
    )
    intents = SimpleNamespace(
        message_content=False, dm_messages=False, guild_messages=False,
        members=False, voice_states=False,
    )
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: intents)
    monkeypatch.setattr(
        discord_platform.commands, "Bot", lambda **kwargs: _FakeBot(**kwargs)
    )
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    ok = await adapter.connect()
    assert ok is True
    assert adapter._allowed_user_ids == {"777"}
    assert adapter._auth_policy is None

    await adapter.disconnect()


# ---------------------------------------------------------------------------
# 20260804 review: profile-YAML (PlatformConfig.extra) auth values must feed
# the same gates as env — on the policy-less fallback accessors AND inside the
# captured multiplex policy — so live, backfill, and component auth agree.
# ---------------------------------------------------------------------------


def test_accessor_fallback_honors_config_extra(monkeypatch):
    adapter = object.__new__(DiscordAdapter)
    adapter._auth_policy = None
    adapter._gate_env_snapshot = None
    adapter.config = SimpleNamespace(
        extra={
            "allow_all_users": "true",
            "allowed_channels": "111,222",
            "ignored_channels": "999",
        }
    )
    assert adapter._auth_allow_all() is True
    assert adapter._auth_allowed_channels() == frozenset({"111", "222"})
    assert adapter._auth_ignored_channels() == frozenset({"999"})


def test_accessor_fallback_env_beats_config_extra(monkeypatch):
    adapter = object.__new__(DiscordAdapter)
    adapter._auth_policy = None
    adapter._gate_env_snapshot = None
    adapter.config = SimpleNamespace(extra={"allowed_channels": "111"})
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "333")
    assert adapter._auth_allowed_channels() == frozenset({"333"})


def test_captured_policy_includes_config_extra_under_multiplex(monkeypatch):
    import agent.secret_scope as secret_scope

    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    adapter = object.__new__(DiscordAdapter)
    # Empty snapshot == this profile's env has no gate values set.
    adapter._gate_env_snapshot = {}
    adapter.config = SimpleNamespace(
        extra={
            "allow_all_users": "true",
            "allowed_channels": "222",
            "ignored_channels": "888",
        }
    )
    policy = adapter._maybe_capture_auth_policy()
    assert policy is not None
    assert policy.allow_all is True
    assert policy.allowed_channels == frozenset({"222"})
    assert policy.ignored_channels == frozenset({"888"})

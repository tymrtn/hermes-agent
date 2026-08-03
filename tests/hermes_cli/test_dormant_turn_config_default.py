"""Config-loading coverage for the shipped dormant-turn defaults.

Proves the merged default (not just a hand-built dict) is off and profile-safe:
the value baked into ``DEFAULT_CONFIG`` and surfaced through the real
``load_config()`` deep-merge resolves to "feature disabled". A fresh install
with no ``config.yaml`` therefore never injects dormant-turn context. (Repair
gap 1.)
"""

from hermes_cli.config import DEFAULT_CONFIG, load_config
from gateway.dormant_turn_context import resolve_config


def test_default_config_ships_safe_dormant_defaults():
    dtc = DEFAULT_CONFIG["gateway"]["dormant_turn_context"]
    assert dtc == {
        "enabled": False,
        "idle_after_seconds": 3600,
        "reorient_after_seconds": 86400,
        "scope": "verified_dm",
        "verified_user_ids": {},
        "location": {
            "city": "",
            "timezone": "",
            "updated_at": "",
            "fresh_for_seconds": 86400,
        },
    }


def test_merged_default_resolves_feature_off():
    # A fresh install (no config.yaml under the per-test HERMES_HOME) yields the
    # deep-merged DEFAULT_CONFIG; resolve_config must treat it as disabled.
    merged = load_config()
    assert (
        merged["gateway"]["dormant_turn_context"]["enabled"] is False
    )
    assert resolve_config(merged) is None


def test_merged_default_verified_ids_is_empty_mapping():
    merged = load_config()
    # The shipped shape is a per-platform mapping, not a flat list, so an
    # accidental enable can't authorize anyone until an operator adds a
    # platform-scoped entry.
    assert merged["gateway"]["dormant_turn_context"]["verified_user_ids"] == {}

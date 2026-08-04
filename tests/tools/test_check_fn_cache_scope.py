"""check_fn cache profile scoping (merge regression, 20260804 review).

``check_fn_cache_scope()`` must key the availability cache by the resolved
Hermes home OUTSIDE multiplex too: check_fns probe profile-scoped state (an
owned continuity store, per-profile credential files), so a process whose
home changes must never serve one home's verdict for another. Under
multiplex an unresolved profile identity still bypasses the cache entirely.
"""

from pathlib import Path

import pytest

import tools.registry as registry
from tools.registry import (
    CHECK_FN_CACHE_BYPASS,
    _check_fn_cached,
    check_fn_cache_scope,
    invalidate_check_fn_cache,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    invalidate_check_fn_cache()
    yield
    invalidate_check_fn_cache()


def test_scope_outside_multiplex_is_resolved_home(monkeypatch, tmp_path):
    import agent.secret_scope as secret_scope
    import hermes_constants

    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: False)
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home", lambda: Path(tmp_path) / "alpha"
    )
    assert check_fn_cache_scope() == str(Path(tmp_path) / "alpha")


def test_cached_verdicts_do_not_leak_across_homes(monkeypatch, tmp_path):
    import agent.secret_scope as secret_scope
    import hermes_constants

    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: False)
    active_home = {"value": Path(tmp_path) / "alpha"}
    monkeypatch.setattr(
        hermes_constants, "get_hermes_home", lambda: active_home["value"]
    )

    calls = []

    def probe() -> bool:
        calls.append(str(active_home["value"]))
        return True

    assert _check_fn_cached(probe) is True
    assert _check_fn_cached(probe) is True  # second call under same home: cached
    assert len(calls) == 1

    active_home["value"] = Path(tmp_path) / "beta"
    assert _check_fn_cached(probe) is True  # new home: fresh probe, no reuse
    assert len(calls) == 2
    assert calls == [str(Path(tmp_path) / "alpha"), str(Path(tmp_path) / "beta")]


def test_unresolved_multiplex_scope_bypasses_cache(monkeypatch):
    import agent.secret_scope as secret_scope
    import hermes_constants

    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home_override", lambda: "")
    assert check_fn_cache_scope() == CHECK_FN_CACHE_BYPASS

    calls = []

    def probe() -> bool:
        calls.append(1)
        return True

    assert _check_fn_cached(probe) is True
    assert _check_fn_cached(probe) is True
    # Bypass: never cached, every call re-probes.
    assert len(calls) == 2

import pytest

from dream_cycle_v3.canonical import (canonical_json, fingerprint_obj, run_id_for,
                                      stable_id)


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})


def test_stable_id_deterministic_and_distinct():
    assert stable_id("ns", "a", "bc") == stable_id("ns", "a", "bc")
    assert stable_id("ns", "a", "bc") != stable_id("ns", "ab", "c")
    assert stable_id("ns1", "a") != stable_id("ns2", "a")
    assert len(stable_id("ns", "x")) == 32


def test_stable_id_rejects_separator_smuggling():
    with pytest.raises(ValueError):
        stable_id("ns", "a\x1fb")


def test_run_id_ignores_fingerprint_order():
    fps = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    assert run_id_for("p", "s", "e", "v", fps) == run_id_for("p", "s", "e", "v", fps[::-1])
    assert run_id_for("p", "s", "e", "v", fps) != run_id_for("p", "s", "e", "v2", fps)


def test_fingerprint_obj_covers_content():
    assert fingerprint_obj({"a": 1}) != fingerprint_obj({"a": 2})
    assert fingerprint_obj({"a": 1}).startswith("sha256:")

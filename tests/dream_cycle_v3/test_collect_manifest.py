import json
import os
import shutil
import stat
from datetime import timezone

import pytest

from .conftest import AS_OF, WINDOW_END, WINDOW_START, write_tree
from dream_cycle_v3.canonical import canonical_json, run_id_for
from dream_cycle_v3.collect import CollectionBounds, collect, collect_to_manifest
from dream_cycle_v3.errors import ManifestConflictError, ManifestValidationError
from dream_cycle_v3.manifest import (compute_manifest_fingerprint, load_manifest,
                                     require_valid_manifest, validate_manifest,
                                     write_manifest)
from dream_cycle_v3.roots import CollectionRoots

BASE_FILES = {
    "sessions/a.jsonl": '{"role": "user", "content": "hello continuity"}\n'
                        '{"role": "assistant", "content": "TODO: wire retriever"}\n',
    "state/loose-threads.md": "# threads\n- Decision: keep task state external\n",
    "notes.txt": "plain note that is long enough to matter\n",
}


def _roots(tmp_path, name="src", files=BASE_FILES):
    root = write_tree(tmp_path / name, files)
    return CollectionRoots.resolve("test-profile", {"profile": root})


def _collect(roots, **kw):
    return collect(roots, window_start=WINDOW_START, window_end=WINDOW_END,
                   generated_at=AS_OF, **kw)


def test_identical_input_identical_manifest_and_run_id(tmp_path):
    roots = _roots(tmp_path)
    m1, m2 = _collect(roots), _collect(roots)
    assert m1["run_id"] == m2["run_id"]
    assert canonical_json(m1) == canonical_json(m2)  # byte-identical serialization
    assert validate_manifest(m1) == []


def test_run_id_stable_across_tree_copies(tmp_path):
    src = write_tree(tmp_path / "one", BASE_FILES)
    copy = tmp_path / "two" / "nested"
    copy.parent.mkdir()
    shutil.copytree(src, copy)  # copy2 preserves mtimes
    m1 = _collect(CollectionRoots.resolve("test-profile", {"profile": src}))
    m2 = _collect(CollectionRoots.resolve("test-profile", {"profile": copy}))
    assert m1["run_id"] == m2["run_id"]
    assert m1["manifest_fingerprint"] == m2["manifest_fingerprint"]
    assert m1["roots"] != m2["roots"]  # absolute paths differ; identity does not


def test_content_change_changes_run_id(tmp_path):
    roots = _roots(tmp_path)
    before = _collect(roots)["run_id"]
    changed = dict(BASE_FILES, **{"notes.txt": "different content entirely\n"})
    roots2 = _roots(tmp_path, "src2", changed)
    assert _collect(roots2)["run_id"] != before


def test_window_and_profile_change_run_id(tmp_path):
    roots = _roots(tmp_path)
    m = _collect(roots)
    late = collect(roots, window_start=WINDOW_START, window_end=WINDOW_END.replace(day=13),
                   generated_at=AS_OF)
    assert late["run_id"] != m["run_id"]
    other = CollectionRoots.resolve("other-profile", dict(roots.roots))
    assert _collect(other)["run_id"] != m["run_id"]


def test_secret_paths_never_read_and_never_leak(tmp_path):
    files = dict(BASE_FILES)
    files[".env"] = "CANARY_PATH=path-canary-e19\n"
    files["secrets/tokens.txt"] = "CANARY_DIR=dir-canary-a55\n"
    files["ops/deploy.md"] = ("routine text\n"
                              "ghp_SAMPLEFAKESAMPLEFAKESAMPLEFAKE000000\n"
                              "CANARY_CONTENT=content-canary-77b\n")
    roots = _roots(tmp_path, "sec", files)
    manifest = _collect(roots)
    blob = canonical_json(manifest)
    assert "path-canary-e19" not in blob
    assert "dir-canary-a55" not in blob
    assert "content-canary-77b" not in blob
    assert "SAMPLEFAKE" not in blob

    reasons = {e["location"]: e["reason"] for e in manifest["excluded"]}
    assert reasons[".env"].startswith("secret_path:")
    assert reasons["secrets"].startswith("secret_dir:")
    ids = {s["source_id"]: s for s in manifest["sources"]}
    deploy = ids["profile:ops/deploy.md"]
    assert deploy["excerpt"] is None
    assert deploy["excerpt_suppressed"].startswith("secret_content:github_token")
    assert deploy["fingerprint"].startswith("sha256:")


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("CANARY_OUTSIDE=out-canary-31c\n")
    roots = _roots(tmp_path)
    root = roots.roots["profile"]
    os.symlink(outside, root / "sneaky.md")
    os.utime(root / "sneaky.md", (1783684800, 1783684800), follow_symlinks=False)
    manifest = _collect(roots)
    blob = canonical_json(manifest)
    assert "out-canary-31c" not in blob
    assert any(e["location"] == "sneaky.md" and e["reason"] == "symlink_escape"
               for e in manifest["excluded"])


def test_internal_symlink_is_allowed(tmp_path):
    roots = _roots(tmp_path)
    root = roots.roots["profile"]
    os.symlink(root / "notes.txt", root / "alias.txt")
    manifest = _collect(roots)
    assert "profile:alias.txt" in {s["source_id"] for s in manifest["sources"]}


def test_internal_symlink_uses_target_path_for_privacy_policy(tmp_path):
    """An innocuous alias cannot downgrade the target's session/secret class."""
    roots = _roots(tmp_path)
    root = roots.roots["profile"]
    (root / "secrets").mkdir()
    (root / "secrets" / "tokens.txt").write_text("secret material\n")
    os.symlink(root / "sessions" / "a.jsonl", root / "chat-alias.jsonl")
    os.symlink(root / "secrets" / "tokens.txt", root / "notes-alias.txt")
    manifest = _collect(roots)
    sources = {s["source_id"]: s for s in manifest["sources"]}
    assert sources["profile:chat-alias.jsonl"]["source_type"] == "session"
    assert sources["profile:chat-alias.jsonl"]["excerpt"] is None
    assert "profile:notes-alias.txt" not in sources
    assert any(e["location"] == "notes-alias.txt"
               and e["reason"].startswith("secret_dir:")
               for e in manifest["excluded"])


def test_collection_window_end_is_exclusive(tmp_path):
    roots = _roots(tmp_path)
    boundary = roots.roots["profile"] / "midnight.md"
    boundary.write_text("belongs to the following window\n")
    boundary_ts = WINDOW_END.astimezone(timezone.utc).timestamp()
    os.utime(boundary, (boundary_ts, boundary_ts))
    assert "profile:midnight.md" not in {
        s["source_id"] for s in _collect(roots)["sources"]}


def test_bounds_truncation_and_budgets(tmp_path):
    files = dict(BASE_FILES)
    files["big.log"] = "x" * 100_000
    roots = _roots(tmp_path, "bounded", files)
    bounds = CollectionBounds(max_bytes_per_file=1024)
    manifest = collect(roots, window_start=WINDOW_START, window_end=WINDOW_END,
                       bounds=bounds, generated_at=AS_OF)
    big = {s["source_id"]: s for s in manifest["sources"]}["profile:big.log"]
    assert big["truncated"] is True
    assert big["bytes_read"] == 1024
    assert big["size_bytes"] == 100_000

    tight = collect(roots, window_start=WINDOW_START, window_end=WINDOW_END,
                    bounds=CollectionBounds(max_files_per_root=2), generated_at=AS_OF)
    assert len(tight["sources"]) == 2
    assert sum(1 for e in tight["excluded"]
               if e["reason"] == "max_files_per_root") == 2


def test_max_depth_prunes_and_records(tmp_path):
    files = dict(BASE_FILES)
    files["a/b/c/deep.md"] = "too deep to collect but real\n"
    roots = _roots(tmp_path, "deep", files)
    manifest = collect(roots, window_start=WINDOW_START, window_end=WINDOW_END,
                       bounds=CollectionBounds(max_depth=2), generated_at=AS_OF)
    assert not any(s["location"].startswith("a/b/c") for s in manifest["sources"])
    assert any(e["reason"] == "max_depth" for e in manifest["excluded"])


def test_window_filters_by_mtime(tmp_path):
    roots = _roots(tmp_path)
    stale = roots.roots["profile"] / "old.md"
    stale.write_text("ancient note outside the window\n")
    os.utime(stale, (1500000000, 1500000000))  # 2017
    manifest = _collect(roots)
    assert "profile:old.md" not in {s["source_id"] for s in manifest["sources"]}


def test_manifest_write_is_immutable(tmp_path):
    roots = _roots(tmp_path)
    out = tmp_path / "out"
    manifest, path = collect_to_manifest(roots, out, window_start=WINDOW_START,
                                         window_end=WINDOW_END, generated_at=AS_OF)
    assert not os.access(path, os.W_OK) or not (path.stat().st_mode & stat.S_IWUSR)
    first_bytes = path.read_bytes()
    manifest2, path2 = collect_to_manifest(roots, out, window_start=WINDOW_START,
                                           window_end=WINDOW_END, generated_at=AS_OF)
    assert path2 == path and path.read_bytes() == first_bytes

    tampered = dict(manifest)
    tampered["generated_at"] = "2026-07-11T09:00:00+00:00"
    with pytest.raises(ManifestConflictError):
        write_manifest(tampered, out / "manifests")


def test_validation_rejects_malformed(tmp_path):
    roots = _roots(tmp_path)
    manifest = _collect(roots)
    assert require_valid_manifest(manifest)

    missing = {k: v for k, v in manifest.items() if k != "run_id"}
    assert any("missing key: run_id" in e for e in validate_manifest(missing))

    tampered = json.loads(canonical_json(manifest))
    tampered["sources"][0]["fingerprint"] = "sha256:" + "0" * 64
    errs = validate_manifest(tampered)
    assert any("run_id does not match" in e for e in errs)

    unsorted = json.loads(canonical_json(manifest))
    unsorted["sources"] = list(reversed(unsorted["sources"]))
    assert any("sorted" in e for e in validate_manifest(unsorted))

    extra = json.loads(canonical_json(manifest))
    extra["surprise"] = True
    assert any("unknown key" in e for e in validate_manifest(extra))

    absolute = json.loads(canonical_json(manifest))
    absolute["sources"][0]["location"] = "/etc/passwd"
    assert any("relative path" in e for e in validate_manifest(absolute))

    assert validate_manifest("not a dict") == ["manifest must be an object"]


def test_load_manifest_rejects_tampered_file(tmp_path):
    roots = _roots(tmp_path)
    out = tmp_path / "out"
    manifest, path = collect_to_manifest(roots, out, window_start=WINDOW_START,
                                         window_end=WINDOW_END, generated_at=AS_OF)
    assert load_manifest(path)["run_id"] == manifest["run_id"]

    evil = json.loads(path.read_text())
    evil["profile"] = "attacker"
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(evil))
    with pytest.raises(ManifestValidationError):
        load_manifest(forged)


def test_naive_datetimes_are_rejected(tmp_path):
    roots = _roots(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        collect(roots, window_start=WINDOW_START.replace(tzinfo=None),
                window_end=WINDOW_END, generated_at=AS_OF)


def test_full_file_fingerprint_streams_beyond_retained_prefix(tmp_path):
    files = dict(BASE_FILES, **{"big.log": "a" * 100_000})
    roots = _roots(tmp_path, "stream", files)
    bounds = CollectionBounds(max_bytes_per_file=1024)
    m1 = collect(roots, window_start=WINDOW_START, window_end=WINDOW_END,
                 bounds=bounds, generated_at=AS_OF)

    # Flip one byte far beyond the retained prefix; preserve size and mtime.
    path = roots.roots["profile"] / "big.log"
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[50_000] = ord("b")
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    m2 = collect(roots, window_start=WINDOW_START, window_end=WINDOW_END,
                 bounds=bounds, generated_at=AS_OF)
    big1 = {s["source_id"]: s for s in m1["sources"]}["profile:big.log"]
    big2 = {s["source_id"]: s for s in m2["sources"]}["profile:big.log"]
    assert big1["bytes_read"] == big2["bytes_read"] == 1024
    assert big1["size_bytes"] == big2["size_bytes"] == 100_000
    assert big1["truncated"] and big2["truncated"]
    assert big1["fingerprint"] != big2["fingerprint"]
    assert m1["run_id"] != m2["run_id"]


def test_session_content_never_enters_manifest(tmp_path):
    files = {
        "sessions/chat.jsonl":
            '{"role": "user", "content": "COLLECT_CANARY_AA11 raw ask"}\n'
            '{"role": "assistant", "content": "COLLECT_CANARY_BB22 raw answer"}\n',
        "sessions/summary.md": "COLLECT_CANARY_CC33 inside the sessions dir\n",
        "state/notes.md": "state docs keep bounded excerpts as designed\n",
    }
    roots = _roots(tmp_path, "transcripts", files)
    manifest = _collect(roots)
    blob = canonical_json(manifest)
    for canary in ("COLLECT_CANARY_AA11", "COLLECT_CANARY_BB22",
                   "COLLECT_CANARY_CC33"):
        assert canary not in blob

    ids = {s["source_id"]: s for s in manifest["sources"]}
    for sid in ("profile:sessions/chat.jsonl", "profile:sessions/summary.md"):
        assert ids[sid]["source_type"] == "session"
        assert ids[sid]["excerpt"] is None
        assert ids[sid]["excerpt_suppressed"] == "session_transcript"
        assert ids[sid]["fingerprint"].startswith("sha256:")
    assert "bounded excerpts" in ids["profile:state/notes.md"]["excerpt"]


def test_validation_rejects_impossible_calendar_datetimes(tmp_path):
    roots = _roots(tmp_path)
    manifest = json.loads(canonical_json(_collect(roots)))
    manifest["generated_at"] = "2026-02-30T10:00:00+00:00"  # regex-valid, no calendar
    errs = validate_manifest(manifest)
    assert any("generated_at" in e for e in errs)


def test_directly_rooted_session_store_is_suppressed(tmp_path):
    """--root sessions=PATH puts transcripts at depth zero; still suppressed."""
    files = {
        "20260711-chat.jsonl":
            '{"role": "user", "content": "DIRECT_ROOT_CANARY_DD44 raw ask"}\n',
        "20260711-notes.md": "DIRECT_ROOT_CANARY_EE55 in a session store\n",
    }
    root = write_tree(tmp_path / "livesessions", files)
    roots = CollectionRoots.resolve("test-profile", {"sessions": root})
    manifest = _collect(roots)
    blob = canonical_json(manifest)
    assert "DIRECT_ROOT_CANARY_DD44" not in blob
    assert "DIRECT_ROOT_CANARY_EE55" not in blob
    for src in manifest["sources"]:
        assert src["source_type"] == "session"
        assert src["excerpt"] is None
        assert src["excerpt_suppressed"] == "session_transcript"

    # Root keys merely containing 'session' are transcript stores too.
    roots2 = CollectionRoots.resolve("test-profile", {"session-archive": root})
    manifest2 = _collect(roots2)
    assert all(s["source_type"] == "session" for s in manifest2["sources"])
    assert "DIRECT_ROOT_CANARY_DD44" not in canonical_json(manifest2)


def test_manifest_backstop_forbids_session_excerpts(tmp_path):
    """Even a self-consistent forged manifest cannot carry session excerpts."""
    from dream_cycle_v3 import COLLECTOR_VERSION
    from dream_cycle_v3.manifest import assemble_manifest

    forged = assemble_manifest(
        profile="forge-test",
        window_start="2026-07-09T00:00:00+00:00",
        window_end="2026-07-12T00:00:00+00:00",
        collector_version=COLLECTOR_VERSION,
        bounds={"max_files_per_root": 64, "max_bytes_per_file": 65536,
                "max_total_bytes": 4194304, "max_depth": 8,
                "excerpt_chars": 700, "allowed_suffixes": [".jsonl"]},
        sources=[{
            "source_type": "session", "source_id": "sessions:chat.jsonl",
            "root": "sessions", "location": "chat.jsonl", "size_bytes": 10,
            "mtime_utc": "2026-07-10T12:00:00+00:00", "bytes_read": 10,
            "truncated": False, "fingerprint": "sha256:" + "a" * 64,
            "excerpt": "user: smuggled transcript text",
            "excerpt_suppressed": None,
        }],
        excluded=[],
        roots={"sessions": "/tmp/sessions"},
        generated_at="2026-07-11T08:00:00+00:00",
    )
    errs = validate_manifest(forged)
    assert any("transcript policy" in e for e in errs)

    forged["sources"][0]["source_type"] = "file"
    forged["manifest_fingerprint"] = compute_manifest_fingerprint(forged)
    errs = validate_manifest(forged)
    assert any("transcript policy" in e for e in errs)

    forged["sources"][0]["root"] = "profile"
    forged["sources"][0]["source_id"] = "profile:sessions/chat.jsonl"
    forged["sources"][0]["location"] = "sessions/chat.jsonl"
    forged["roots"] = {"profile": "/tmp/profile"}
    forged["run_id"] = run_id_for(
        forged["profile"], forged["window"]["start"], forged["window"]["end"],
        forged["collector_version"], [forged["sources"][0]["fingerprint"]])
    forged["manifest_fingerprint"] = compute_manifest_fingerprint(forged)
    errs = validate_manifest(forged)
    assert any("transcript policy" in e for e in errs)


def test_z_suffix_datetimes_validate_and_compare_semantically(tmp_path):
    from dream_cycle_v3 import COLLECTOR_VERSION
    from dream_cycle_v3.manifest import assemble_manifest

    def make(start, end):
        return assemble_manifest(
            profile="z-test", window_start=start, window_end=end,
            collector_version=COLLECTOR_VERSION,
            bounds={"max_files_per_root": 64, "max_bytes_per_file": 65536,
                    "max_total_bytes": 4194304, "max_depth": 8,
                    "excerpt_chars": 700, "allowed_suffixes": [".md"]},
            sources=[], excluded=[], roots={"profile": "/tmp/p"},
            generated_at="2026-07-11T08:00:00Z")

    assert validate_manifest(make("2026-07-09T00:00:00Z",
                                  "2026-07-12T00:00:00Z")) == []
    # Semantically ordered but lexically reversed (offset vs Z forms):
    # 09:00+01:00 == 08:00Z, which precedes 08:30Z.
    assert validate_manifest(make("2026-07-11T09:00:00+01:00",
                                  "2026-07-11T08:30:00Z")) == []
    # Genuinely inverted windows are still refused.
    errs = validate_manifest(make("2026-07-12T00:00:00Z",
                                  "2026-07-09T00:00:00Z"))
    assert any("precede" in e for e in errs)

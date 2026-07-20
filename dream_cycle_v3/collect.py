"""Deterministic, bounded, read-only collection into an immutable manifest.

Selection criteria (window, allowed suffixes) decide what is in scope and are
recorded in the manifest as bounds; the `excluded` list records only items
that were in scope but refused (secrets, symlink escapes, depth, budgets,
unreadable files) so it stays a meaningful safety audit trail.

Determinism contract: identical trees (paths, bytes, mtimes) with identical
parameters produce byte-identical manifests and identical run IDs. Selection
prefers recent files (mtime descending) with source_id as a stable tiebreak.

Fingerprints always cover the full file content via bounded-memory streaming,
even when only a bounded prefix is retained for excerpts. Session transcripts
(source_type 'session') are never excerpted: their entries carry fingerprints
and metadata only, so no user/assistant text reaches any persisted artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import COLLECTOR_VERSION
from .manifest import assemble_manifest, write_manifest
from .roots import CollectionRoots
from .secretguard import SECRET_DIR_NAMES, classify_path, scan_content

DEFAULT_ALLOWED_SUFFIXES = (".json", ".jsonl", ".log", ".md", ".txt", ".yaml", ".yml")
_HASH_CHUNK = 1_048_576  # 1 MiB streaming window: memory-bounded full-file hashing


def _read_prefix_with_full_hash(path: Path, keep_bytes: int,
                               expected: os.stat_result | None = None
                               ) -> tuple[bytes, int, str]:
    """Return (retained prefix, full byte count, full-content fingerprint).

    The hash streams the entire file in fixed-size chunks so a change beyond
    the retained prefix still changes the fingerprint (and therefore the run
    ID), while memory stays bounded by the chunk size plus the kept prefix.
    """
    digest = hashlib.sha256()
    kept = bytearray()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("collection candidate is not a regular file")
        if (expected is not None and
                (opened.st_dev, opened.st_ino) !=
                (expected.st_dev, expected.st_ino)):
            raise OSError("collection candidate changed after scan")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                if len(kept) < keep_bytes:
                    kept.extend(chunk[: keep_bytes - len(kept)])
    finally:
        os.close(fd)
    return bytes(kept), total, "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class CollectionBounds:
    max_files_per_root: int = 64
    max_bytes_per_file: int = 65536
    max_total_bytes: int = 4_194_304
    max_depth: int = 8
    excerpt_chars: int = 700
    allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED_SUFFIXES

    def __post_init__(self) -> None:
        for field_name in ("max_files_per_root", "max_bytes_per_file",
                           "max_total_bytes", "max_depth", "excerpt_chars"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"bounds.{field_name} must be positive")
        if not self.allowed_suffixes:
            raise ValueError("bounds.allowed_suffixes must not be empty")

    def to_manifest(self) -> dict[str, Any]:
        return {
            "max_files_per_root": self.max_files_per_root,
            "max_bytes_per_file": self.max_bytes_per_file,
            "max_total_bytes": self.max_total_bytes,
            "max_depth": self.max_depth,
            "excerpt_chars": self.excerpt_chars,
            "allowed_suffixes": sorted(self.allowed_suffixes),
        }


def _require_utc(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _source_type_for(root_key: str, rel: Path, suffix: str) -> str:
    """Session typing considers the root key, not just the relative path.

    A collection rooted directly at a session store (`--root sessions=PATH`)
    has transcripts at depth zero, so any root key or directory component
    containing 'session' classifies the file as a transcript. Over-inclusion
    is the safe direction: session sources are never excerpted.
    """
    if "session" in root_key.lower():
        return "session"
    # Match the manifest validator's transcript backstop exactly: filenames
    # as well as parent components can identify session material.  Otherwise
    # a file such as ``session-audit.md`` is excerpted here and rejected only
    # after collection, preventing an otherwise safe historical replay.
    if any("session" in part.lower() for part in rel.parts):
        return "session"
    if suffix == ".log":
        return "log"
    return "file"


def _walk_candidates(root_key: str, root: Path, bounds: CollectionBounds,
                     excluded: list[dict[str, str]]) -> Iterator[tuple[Path, Path, Path, os.stat_result]]:
    """Yield (read_path, alias_rel, policy_rel, stat) for regular files.

    Prunes secret directories and over-deep directories (each recorded once),
    never follows directory symlinks, and refuses file symlinks that resolve
    outside the root.
    """
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dpath = Path(dirpath)
        rel_dir = dpath.relative_to(root)
        keep: list[str] = []
        for d in sorted(dirnames):
            rel = rel_dir / d if rel_dir.parts else Path(d)
            if d.lower() in SECRET_DIR_NAMES:
                excluded.append({"root": root_key, "location": str(rel),
                                 "reason": f"secret_dir:{d.lower()}"})
            elif len(rel.parts) >= bounds.max_depth:
                excluded.append({"root": root_key, "location": str(rel),
                                 "reason": "max_depth"})
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = rel_dir / name if rel_dir.parts else Path(name)
            path = dpath / name
            read_path = path
            policy_rel = rel
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    excluded.append({"root": root_key, "location": str(rel),
                                     "reason": "symlink_broken"})
                    continue
                if not resolved.is_relative_to(root):
                    excluded.append({"root": root_key, "location": str(rel),
                                     "reason": "symlink_escape"})
                    continue
                read_path = resolved
                policy_rel = resolved.relative_to(root)
            try:
                st = read_path.stat()
            except OSError as exc:
                excluded.append({"root": root_key, "location": str(rel),
                                 "reason": f"unreadable:{type(exc).__name__}"})
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            yield read_path, rel, policy_rel, st


def _excerpt_for(rel: Path, data: bytes, bounds: CollectionBounds) -> str:
    """v2-representative bounded preview, deterministic over content."""
    text = data.decode("utf-8", errors="ignore").replace("\x00", "")
    suffix = rel.suffix.lower()
    lines: list[str] = []
    if suffix == ".jsonl":
        for line in text.splitlines()[-80:]:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            role, content = obj.get("role"), obj.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                lines.append(f"{role}: {content.strip()[:500]}")
        if not lines:
            lines = text.splitlines()[-20:]
        text = "\n".join(lines)
    elif suffix == ".json":
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            msgs = obj.get("messages") or obj.get("conversation") or []
            if isinstance(msgs, list):
                for msg in msgs[-20:]:
                    if not isinstance(msg, dict):
                        continue
                    role, content = msg.get("role"), msg.get("content")
                    if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                        lines.append(f"{role}: {content.strip()[:500]}")
            if not lines:
                title = obj.get("title") or obj.get("session_title")
                if title:
                    lines.append(f"title: {title}")
        if not lines:
            lines = text.splitlines()[-20:]
        text = "\n".join(lines)
    return text[: bounds.excerpt_chars]


def collect(roots: CollectionRoots, *, window_start: datetime, window_end: datetime,
            bounds: CollectionBounds | None = None,
            generated_at: datetime | None = None) -> dict[str, Any]:
    """Collect bounded evidence under `roots` into a validated manifest dict."""
    bounds = bounds or CollectionBounds()
    window_start = _require_utc("window_start", window_start)
    window_end = _require_utc("window_end", window_end)
    if window_start >= window_end:
        raise ValueError("window_start must precede window_end")
    generated_at = _require_utc(
        "generated_at", generated_at or datetime.now(timezone.utc))

    sources: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    total_bytes = 0

    for root_key in sorted(roots.roots):
        root = roots.roots[root_key]
        in_window: list[tuple[float, str, Path, Path, Path, os.stat_result]] = []
        for path, rel, policy_rel, st in _walk_candidates(root_key, root, bounds, excluded):
            # Secret paths are recorded regardless of suffix/window so the
            # exclusion list stays a complete safety audit trail.
            reason = classify_path(policy_rel)
            if reason:
                excluded.append({"root": root_key, "location": rel.as_posix(),
                                 "reason": reason})
                continue
            if policy_rel.suffix.lower() not in bounds.allowed_suffixes:
                continue
            mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc)
            if not (window_start <= mtime < window_end):
                continue
            source_id = f"{root_key}:{rel.as_posix()}"
            in_window.append((-st.st_mtime, source_id, path, rel, policy_rel, st))

        in_window.sort(key=lambda t: (t[0], t[1]))
        for i, (_, source_id, path, rel, policy_rel, st) in enumerate(in_window):
            if i >= bounds.max_files_per_root:
                excluded.append({"root": root_key, "location": rel.as_posix(),
                                 "reason": "max_files_per_root"})
                continue
            if total_bytes >= bounds.max_total_bytes:
                excluded.append({"root": root_key, "location": rel.as_posix(),
                                 "reason": "max_total_bytes"})
                continue
            budget = min(bounds.max_bytes_per_file,
                         bounds.max_total_bytes - total_bytes)
            try:
                data, full_size, fingerprint = _read_prefix_with_full_hash(
                    path, budget, st)
            except OSError as exc:
                excluded.append({"root": root_key, "location": rel.as_posix(),
                                 "reason": f"unreadable:{type(exc).__name__}"})
                continue
            total_bytes += len(data)
            truncated = full_size > len(data)
            source_type = _source_type_for(root_key, policy_rel,
                                           policy_rel.suffix.lower())

            excerpt: str | None
            suppressed: str | None = None
            if source_type == "session":
                # Transcript policy: session content is fingerprinted evidence
                # only; user/assistant text never enters persisted artifacts.
                excerpt = None
                suppressed = "session_transcript"
            else:
                text = data.decode("utf-8", errors="ignore")
                hits = scan_content(text)
                if hits:
                    excerpt = None
                    suppressed = "secret_content:" + ",".join(sorted(hits))
                elif b"\x00" in data:
                    excerpt = None
                    suppressed = "binary"
                else:
                    excerpt = _excerpt_for(policy_rel, data, bounds)

            sources.append({
                "source_type": source_type,
                "source_id": source_id,
                "root": root_key,
                "location": rel.as_posix(),
                "size_bytes": full_size,
                "mtime_utc": _iso(datetime.fromtimestamp(st.st_mtime, timezone.utc)),
                "bytes_read": len(data),
                "truncated": truncated,
                "fingerprint": fingerprint,
                "excerpt": excerpt,
                "excerpt_suppressed": suppressed,
            })

    return assemble_manifest(
        profile=roots.profile,
        window_start=_iso(window_start),
        window_end=_iso(window_end),
        collector_version=COLLECTOR_VERSION,
        bounds=bounds.to_manifest(),
        sources=sources,
        excluded=excluded,
        roots={k: str(v) for k, v in roots.roots.items()},
        generated_at=_iso(generated_at),
    )


def collect_to_manifest(roots: CollectionRoots, out_dir: Path, *,
                        window_start: datetime, window_end: datetime,
                        bounds: CollectionBounds | None = None,
                        generated_at: datetime | None = None) -> tuple[dict[str, Any], Path]:
    manifest = collect(roots, window_start=window_start, window_end=window_end,
                       bounds=bounds, generated_at=generated_at)
    path = write_manifest(manifest, out_dir / "manifests")
    return manifest, path

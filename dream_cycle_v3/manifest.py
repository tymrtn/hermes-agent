"""Immutable source manifests: structure, fingerprinting, persistence, validation.

The manifest fingerprint covers the canonical core only: schema version,
run_id, profile, window, collector version, bounds, sources, excluded.
Root-relative paths keep it stable across hosts and tree copies; wall-clock
`generated_at` and absolute root paths are provenance metadata outside the
fingerprint.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import MANIFEST_SCHEMA_VERSION
from .canonical import canonical_json, fingerprint_obj, run_id_for
from .contracts import parse_iso_datetime
from .errors import ManifestConflictError, ManifestValidationError

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _is_datetime(value: Any) -> bool:
    """Shape plus calendar semantics — 2026-99-99T00:00:00Z is rejected,
    a valid trailing 'Z' is accepted on every supported interpreter."""
    if not isinstance(value, str) or not _ISO_RE.match(value):
        return False
    try:
        parse_iso_datetime(value)
    except ValueError:
        return False
    return True

SOURCE_TYPES = ("session", "task", "git", "cron", "log", "file", "user_confirmation", "live_probe")

_CORE_KEYS = ("schema_version", "run_id", "profile", "window", "collector_version",
              "bounds", "sources", "excluded")
_TOP_KEYS = _CORE_KEYS + ("roots", "generated_at", "manifest_fingerprint")

_SOURCE_KEYS = ("source_type", "source_id", "root", "location", "size_bytes",
                "mtime_utc", "bytes_read", "truncated", "fingerprint",
                "excerpt", "excerpt_suppressed")
_EXCLUDED_KEYS = ("root", "location", "reason")
_BOUNDS_KEYS = ("max_files_per_root", "max_bytes_per_file", "max_total_bytes",
                "max_depth", "excerpt_chars", "allowed_suffixes")


def manifest_core(manifest: dict[str, Any]) -> dict[str, Any]:
    return {k: manifest[k] for k in _CORE_KEYS}


def compute_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    return fingerprint_obj(manifest_core(manifest))


def assemble_manifest(*, profile: str, window_start: str, window_end: str,
                      collector_version: str, bounds: dict[str, Any],
                      sources: list[dict[str, Any]], excluded: list[dict[str, Any]],
                      roots: dict[str, str], generated_at: str) -> dict[str, Any]:
    sources = sorted(sources, key=lambda s: s["source_id"])
    excluded = sorted(excluded, key=lambda e: (e["root"], e["location"], e["reason"]))
    run_id = run_id_for(profile, window_start, window_end, collector_version,
                        [s["fingerprint"] for s in sources])
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "profile": profile,
        "window": {"start": window_start, "end": window_end},
        "collector_version": collector_version,
        "bounds": bounds,
        "sources": sources,
        "excluded": excluded,
        "roots": roots,
        "generated_at": generated_at,
    }
    manifest["manifest_fingerprint"] = compute_manifest_fingerprint(manifest)
    return manifest


def validate_manifest(manifest: Any) -> list[str]:
    """Structural validation. Returns error strings; empty list means valid."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]

    for key in _TOP_KEYS:
        if key not in manifest:
            errors.append(f"missing key: {key}")
    for key in manifest:
        if key not in _TOP_KEYS:
            errors.append(f"unknown key: {key}")
    if errors:
        return errors

    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {manifest['schema_version']!r}")
    if not (isinstance(manifest["run_id"], str) and _RUN_ID_RE.match(manifest["run_id"])):
        errors.append("run_id must be 32 lowercase hex chars")
    if not (isinstance(manifest["profile"], str) and manifest["profile"].strip()):
        errors.append("profile must be a non-empty string")

    window = manifest["window"]
    if not (isinstance(window, dict) and set(window) == {"start", "end"}):
        errors.append("window must be {start, end}")
    else:
        for k in ("start", "end"):
            if not _is_datetime(window[k]):
                errors.append(f"window.{k} must be an ISO-8601 datetime")
        if not errors and parse_iso_datetime(window["start"]) >= \
                parse_iso_datetime(window["end"]):
            errors.append("window.start must precede window.end")

    if not (isinstance(manifest["collector_version"], str) and manifest["collector_version"]):
        errors.append("collector_version must be a non-empty string")
    if not _is_datetime(manifest["generated_at"]):
        errors.append("generated_at must be an ISO-8601 datetime")

    bounds = manifest["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != set(_BOUNDS_KEYS):
        errors.append(f"bounds must have exactly keys {sorted(_BOUNDS_KEYS)}")
    else:
        for k in _BOUNDS_KEYS:
            if k == "allowed_suffixes":
                if not (isinstance(bounds[k], list) and bounds[k] == sorted(bounds[k])
                        and all(isinstance(s, str) for s in bounds[k])):
                    errors.append("bounds.allowed_suffixes must be a sorted string list")
            elif not (isinstance(bounds[k], int) and bounds[k] > 0):
                errors.append(f"bounds.{k} must be a positive integer")

    roots = manifest["roots"]
    if not (isinstance(roots, dict) and roots
            and all(isinstance(v, str) and v for v in roots.values())):
        errors.append("roots must be a non-empty mapping of key->absolute path")

    sources = manifest["sources"]
    if not isinstance(sources, list):
        errors.append("sources must be a list")
        sources = []
    seen_ids: set[str] = set()
    for i, src in enumerate(sources):
        where = f"sources[{i}]"
        if not isinstance(src, dict) or set(src) != set(_SOURCE_KEYS):
            errors.append(f"{where}: must have exactly keys {sorted(_SOURCE_KEYS)}")
            continue
        if src["source_type"] not in SOURCE_TYPES:
            errors.append(f"{where}: bad source_type {src['source_type']!r}")
        if not (isinstance(src["source_id"], str) and src["source_id"]):
            errors.append(f"{where}: source_id required")
        elif src["source_id"] in seen_ids:
            errors.append(f"{where}: duplicate source_id {src['source_id']}")
        else:
            seen_ids.add(src["source_id"])
        if isinstance(roots, dict) and src.get("root") not in roots:
            errors.append(f"{where}: root {src.get('root')!r} not declared")
        loc = src.get("location")
        if not isinstance(loc, str) or not loc or loc.startswith(("/", "..")) or ".." in Path(loc).parts:
            errors.append(f"{where}: location must be a relative path inside its root")
        if not (isinstance(src["fingerprint"], str) and _FINGERPRINT_RE.match(src["fingerprint"])):
            errors.append(f"{where}: fingerprint must match sha256:<64 hex>")
        for k in ("size_bytes", "bytes_read"):
            if not (isinstance(src[k], int) and src[k] >= 0):
                errors.append(f"{where}: {k} must be a non-negative integer")
        if not isinstance(src["truncated"], bool):
            errors.append(f"{where}: truncated must be a boolean")
        if not _is_datetime(src["mtime_utc"]):
            errors.append(f"{where}: mtime_utc must be an ISO-8601 datetime")
        excerpt, suppressed = src["excerpt"], src["excerpt_suppressed"]
        if suppressed is not None and not isinstance(suppressed, str):
            errors.append(f"{where}: excerpt_suppressed must be null or string")
        if suppressed is not None and excerpt is not None:
            errors.append(f"{where}: excerpt must be null when suppressed")
        if excerpt is not None and not isinstance(excerpt, str):
            errors.append(f"{where}: excerpt must be null or string")
        # Do not trust caller-controlled source_type alone. Declared session
        # roots and session-prefixed source IDs are independently transcripts.
        root_key = str(src.get("root", "")).lower()
        source_prefix = str(src.get("source_id", "")).split(":", 1)[0].lower()
        location_parts = {part.lower() for part in Path(str(src.get("location", ""))).parts}
        transcript_source = (src.get("source_type") == "session"
                             or "session" in root_key
                             or "session" in source_prefix
                             or any("session" in part for part in location_parts))
        if transcript_source and excerpt is not None:
            errors.append(
                f"{where}: session sources must never carry excerpts "
                "(transcript policy)")
        if isinstance(bounds, dict) and isinstance(excerpt, str) \
                and isinstance(bounds.get("excerpt_chars"), int) \
                and len(excerpt) > bounds["excerpt_chars"]:
            errors.append(f"{where}: excerpt exceeds bounds.excerpt_chars")

    if [s.get("source_id") for s in sources if isinstance(s, dict)] != \
            sorted(s.get("source_id", "") for s in sources if isinstance(s, dict)):
        errors.append("sources must be sorted by source_id")

    excluded = manifest["excluded"]
    if not isinstance(excluded, list):
        errors.append("excluded must be a list")
        excluded = []
    for i, exc in enumerate(excluded):
        if not isinstance(exc, dict) or set(exc) != set(_EXCLUDED_KEYS):
            errors.append(f"excluded[{i}]: must have exactly keys {sorted(_EXCLUDED_KEYS)}")
            continue
        if not all(isinstance(exc[k], str) and exc[k] for k in _EXCLUDED_KEYS):
            errors.append(f"excluded[{i}]: all fields must be non-empty strings")

    if not errors:
        expected_run_id = run_id_for(
            manifest["profile"], window["start"], window["end"],
            manifest["collector_version"], [s["fingerprint"] for s in sources])
        if manifest["run_id"] != expected_run_id:
            errors.append("run_id does not match its inputs")
        if manifest["manifest_fingerprint"] != compute_manifest_fingerprint(manifest):
            errors.append("manifest_fingerprint does not match manifest core")
    return errors


def require_valid_manifest(manifest: Any) -> dict[str, Any]:
    errors = validate_manifest(manifest)
    if errors:
        raise ManifestValidationError(errors)
    return manifest


def write_manifest(manifest: dict[str, Any], manifests_dir: Path) -> Path:
    """Publish `manifests/<run_id>.json` immutably and race-safely.

    The payload is written to a unique temp file, then published with
    os.link(), which atomically fails if the destination already exists —
    concurrent writers can never overwrite each other. An existing identical
    manifest is an idempotent no-op; an existing different one is a hard
    ManifestConflictError, whichever writer arrives second.
    """
    require_valid_manifest(manifest)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / f"{manifest['run_id']}.json"
    payload = canonical_json(manifest) + "\n"

    def existing_outcome() -> Path:
        existing = path.read_text(encoding="utf-8")
        if existing == payload:
            return path
        raise ManifestConflictError(
            f"manifest {path} exists with different content; refusing to overwrite")

    if path.exists():
        return existing_outcome()
    fd, tmp_name = tempfile.mkstemp(dir=manifests_dir,
                                    prefix=f".{manifest['run_id']}.",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o444)
        try:
            os.link(tmp_name, path)  # atomic claim: never overwrites
        except FileExistsError:
            return existing_outcome()
    finally:
        os.unlink(tmp_name)
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    import json

    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestValidationError([f"unreadable manifest {path}: {exc}"]) from None
    return require_valid_manifest(obj)

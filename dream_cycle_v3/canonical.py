"""Canonical serialization and stable identifiers.

Every fingerprint, run ID, and idempotency key in v3 flows through this
module so identity is defined in exactly one place.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SEP = "\x1f"  # unit separator: cannot appear in the id parts we join

# The one safe shape for any identity/metadata string that is later
# interpolated verbatim into a DC3 managed marker, receipt, journal, or
# manifest.  The character set excludes every structural delimiter that could
# reopen the marker grammar — whitespace and newlines, the HTML comment
# tokens ``<!--`` / ``-->`` (both ``<`` and ``>`` are absent), ``#`` headings,
# path separators, and NUL — so a value that matches can never forge or nest a
# region.  ``\Z`` (not ``$``) anchors the true end so a trailing newline is
# rejected.  All stable_id outputs (32 hex chars) and the human-authored ids
# used in fixtures (``candidate-…``, ``run-…``, ``sha256:…``) match.
SAFE_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


def is_safe_identity(value: Any) -> bool:
    """True iff *value* is a string safe to render into DC3 marker metadata."""
    return isinstance(value, str) and SAFE_IDENTITY_RE.match(value) is not None


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, tight separators, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def fingerprint_bytes(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


def fingerprint_obj(obj: Any) -> str:
    return "sha256:" + sha256_hex(canonical_json(obj))


def stable_id(namespace: str, *parts: str) -> str:
    """32-hex-char stable identifier over namespace + ordered parts.

    Parts are joined with a separator that cannot occur in the values, so
    ("a", "bc") and ("ab", "c") never collide.
    """
    for p in parts:
        if _SEP in p:
            raise ValueError("stable_id part contains reserved separator")
    return sha256_hex(namespace + _SEP + _SEP.join(parts))[:32]


def run_id_for(profile: str, window_start: str, window_end: str,
               collector_version: str, source_fingerprints: list[str]) -> str:
    """Design §11: run_id = hash(profile, window, collector_version, sorted fingerprints)."""
    return stable_id(
        "dream-cycle-v3-run",
        profile,
        window_start,
        window_end,
        collector_version,
        *sorted(source_fingerprints),
    )


def record_key_for(destination: str, canonical_subject: str) -> str:
    """Canonical record identity: one key per (destination, subject).

    Two candidates about the same subject aimed at the same destination are
    revisions of one record, never two records.
    """
    return stable_id("dream-cycle-v3-record", destination, canonical_subject)


def write_idempotency_key(destination: str, record_key: str,
                          content_revision: int) -> str:
    """Design §11 mutation idempotency key:
    hash(destination, canonical record identity, content revision).

    UNIQUE in write_receipts, so the database — not caller discipline —
    guarantees a given record revision is written at most once.
    """
    return stable_id("dream-cycle-v3-write", destination, record_key,
                     str(int(content_revision)))

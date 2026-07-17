"""Recursive fail-closed output sanitization for the Phase 3 read paths.

Every string the wake broker or continuity_lookup emits — titles, actions,
headings, identifiers, task refs, providers, locators, statuses, dates —
passes through here before leaving the package. Layers, in order:

1. secretguard content scan over the full pre-clip string: any hit withholds
   the whole field ("[privacy_withheld]"); partial secrets can never survive
   clipping.
2. Email/phone PII redaction (always on: continuity output is derived
   metadata, so redacting is cheap and over-exclusion is the intended
   failure direction — this matches the strictest setting of the gateway's
   privacy.redact_pii policy rather than trusting the caller to plumb it).
3. Per-field length caps, and structural whitelist validation for fields
   that must be identifiers, enums, or dates.
4. A hard cap on the final serialized payload (`cap_serialized`).

Any unexpected exception inside a sanitizer returns the withheld marker —
never the raw value.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .secretguard import scan_content

WITHHELD = "[privacy_withheld]"
INVALID = "[invalid]"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# International (+CC ...) and separator-grouped national numbers, the latter
# with an optional 1-3 digit country/trunk prefix so forms like
# todoist:1-555-123-4567 are caught without a literal '+'. The lookbehind
# still refuses a digit/'.'/'-' immediately before the whole number, so ISO
# dates (4-2-2 digit groups) and date-prefixed identifiers match neither
# alternative.
_PHONE_RE = re.compile(
    r"\+\d[\d\s().-]{7,}\d"
    r"|(?<![\d.-])(?:\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\d.-])")

# Identifiers: project/thread/candidate IDs, task refs, providers, locators,
# owners, skill ids, destinations. No whitespace, bounded charset.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.+Z-]{0,32})?$")

IDENTIFIER_CHARS = 120
FIELD_CHARS = 300


def redact_pii(text: str) -> str:
    """Redact email addresses and phone numbers (Hermes privacy policy)."""
    text = _EMAIL_RE.sub("[email_redacted]", text)
    return _PHONE_RE.sub("[phone_redacted]", text)


def has_raw_pii(text: str) -> bool:
    """True when *text* still carries an unredacted email or phone pattern.

    Backstop for assembled output that is built from already-sanitized
    parts: a hit here means some layer was bypassed, and the caller must
    withhold rather than emit. Fails closed on any error."""
    try:
        return bool(_EMAIL_RE.search(text) or _PHONE_RE.search(text))
    except Exception:
        return True


# Secret/PII tokens are far shorter than this; scanning limit+margin chars
# covers anything that could straddle the clip boundary while keeping the
# scan linear on corrupt multi-hundred-KB fields (the secretguard assignment
# pattern backtracks quadratically on long uniform runs).
_SCAN_MARGIN = 512


def sanitize_text(value: object, limit: int = FIELD_CHARS) -> str:
    """Free-text sanitizer: scan -> redact -> clip. Fail closed.

    Only the first ``limit`` chars can ever be emitted, so the scan/redact
    window is ``limit + _SCAN_MARGIN`` — bytes beyond it cannot influence
    the output at all.
    """
    try:
        text = " ".join(str(value if value is not None else "").split())
        if not text:
            return ""
        text = text[: limit + _SCAN_MARGIN]
        if scan_content(text):
            return WITHHELD
        text = redact_pii(text)
        if len(text) > limit:
            text = text[: max(limit - 1, 1)].rstrip() + "…"
        return text
    except Exception:
        return WITHHELD


def sanitize_identifier(value: object, limit: int = IDENTIFIER_CHARS) -> str:
    """Structural identifier field: bounded charset, no spaces, secret scan."""
    try:
        if value is None:
            return ""
        text = str(value)
        if len(text) > limit or not _IDENTIFIER_RE.match(text):
            return INVALID
        if scan_content(text) or _EMAIL_RE.search(text) or _PHONE_RE.search(text):
            # Identifiers skip redact_pii (a partial redaction would break
            # the grammar), so PII-shaped values are withheld outright.
            return WITHHELD
        return text
    except Exception:
        return WITHHELD


def sanitize_enum(value: object, allowed: frozenset[str] | set[str]) -> str:
    try:
        return value if isinstance(value, str) and value in allowed else INVALID
    except Exception:
        return INVALID


def sanitize_date(value: object) -> str:
    try:
        if value is None:
            return ""
        text = str(value)
        return text if _DATE_RE.match(text) else INVALID
    except Exception:
        return INVALID


def sanitize_payload(value: Any, limit: int = FIELD_CHARS) -> Any:
    """Recursive sweep: every string leaf sanitized, unknown types stringified.

    This is the backstop under the per-field sanitizers — a field added later
    without an explicit sanitizer still cannot emit a raw string.
    """
    if isinstance(value, str):
        return sanitize_text(value, limit)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: sanitize_payload(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(v, limit) for v in value]
    return sanitize_text(value, limit)


def cap_serialized(payload: dict, budget: int) -> dict:
    """Enforce a hard cap on the serialized payload size.

    Shrinks deterministically — drop list tails, then clip the longest
    strings — and marks the result truncated. If shrinking cannot get under
    budget (pathological input), returns a minimal typed stub instead of
    oversized output.
    """
    def _size(p: Any) -> int:
        return len(json.dumps(p, ensure_ascii=False, default=str))

    try:
        if _size(payload) <= budget:
            return payload
        work = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        work["truncated"] = True

        def _lists(node: Any) -> list[list]:
            found = []
            if isinstance(node, list):
                found.append(node)
                for item in node:
                    found.extend(_lists(item))
            elif isinstance(node, dict):
                for item in node.values():
                    found.extend(_lists(item))
            return found

        def _clip_longest_string(node: Any) -> bool:
            best_holder, best_key, best_len = None, None, 64
            stack = [node]
            while stack:
                cur = stack.pop()
                items = (cur.items() if isinstance(cur, dict)
                         else enumerate(cur) if isinstance(cur, list) else ())
                for key, val in items:
                    if isinstance(val, str) and len(val) > best_len:
                        best_holder, best_key, best_len = cur, key, len(val)
                    elif isinstance(val, (dict, list)):
                        stack.append(val)
            if best_holder is None:
                return False
            best_holder[best_key] = best_holder[best_key][: best_len // 2] + "…"
            return True

        for _ in range(10_000):
            if _size(work) <= budget:
                return work
            lists = [l for l in _lists(work) if l]
            if lists:
                max(lists, key=len).pop()
                continue
            if not _clip_longest_string(work):
                break
        if _size(work) <= budget:
            return work
    except Exception:
        pass
    return {"schema_version": payload.get("schema_version", 0)
            if isinstance(payload, dict) else 0,
            "kind": "error", "truncated": True,
            "error": "result_exceeded_size_budget"}

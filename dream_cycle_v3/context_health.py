"""Deterministic continuity-health audit for active project context files.

The probe deliberately reuses prompt_builder's discovery and cap resolver so a
Dream Cycle cutover cannot call a context file healthy under rules different
from the first model call that will consume it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent import prompt_builder
from .canonical import canonical_json
from .report import create_file_exclusive

_CONTEXT_PROMPT_PREFIX = (
    "# Project Context\n\n"
    "The following project context files have been loaded and should be followed:\n\n"
)


def _read_nonempty(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
        if data.decode("utf-8").strip():
            return data
    except (OSError, UnicodeDecodeError):
        return None
    return None


def active_project_context_files(cwd: Path | str) -> list[Path]:
    """Return only the project context sources prompt_builder would activate."""
    root = Path(cwd).resolve()

    hermes_md = prompt_builder._find_hermes_md(root)
    if hermes_md is not None and _read_nonempty(hermes_md) is not None:
        return [hermes_md.resolve()]

    for names in (("AGENTS.md", "agents.md"), ("CLAUDE.md", "claude.md")):
        for name in names:
            candidate = root / name
            if _read_nonempty(candidate) is not None:
                return [candidate.resolve()]

    active: list[Path] = []
    cursorrules = root / ".cursorrules"
    if _read_nonempty(cursorrules) is not None:
        active.append(cursorrules.resolve())
    rules_dir = root / ".cursor" / "rules"
    if rules_dir.is_dir():
        for candidate in sorted(rules_dir.glob("*.mdc")):
            if _read_nonempty(candidate) is not None:
                active.append(candidate.resolve())
    return active


def audit_context_health(
    cwd: Path | str,
    *,
    profile: str,
    context_length: int,
) -> dict[str, Any]:
    """Build the target profile's real project-context prompt."""
    root = Path(cwd).resolve()
    sources = active_project_context_files(root)

    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import (reset_hermes_home_override,
                                  set_hermes_home_override)
    try:
        profile_home = Path(resolve_profile_env(profile)).resolve()
    except (FileNotFoundError, ValueError) as exc:
        return {
            "schema_version": 1,
            "kind": "dream-cycle-v3-context-health",
            "profile": profile,
            "cwd": str(root),
            "context_length": context_length,
            "effective_cap_chars": None,
            "active_sources": [],
            "active_source_count": 0,
            "rendered_project_context_chars": 0,
            "truncation_warnings": [],
            "errors": [f"profile resolution failed: {exc}"],
            "pass": False,
            "remediation_required": True,
            "remediation_key": f"context-file-continuity:{profile}:{root}",
        }

    token = set_hermes_home_override(profile_home)
    try:
        cap = prompt_builder._get_context_file_max_chars(context_length)
        # Do not let a warning from an earlier prompt build contaminate this audit.
        prompt_builder.drain_truncation_warnings()
        rendered = prompt_builder.build_context_files_prompt(
            cwd=str(root),
            skip_soul=True,
            context_length=context_length,
            allow_install_tree_fallback=True,
        )
        warnings = prompt_builder.drain_truncation_warnings()
    finally:
        reset_hermes_home_override(token)

    project_payload = (
        rendered[len(_CONTEXT_PROMPT_PREFIX):]
        if rendered.startswith(_CONTEXT_PROMPT_PREFIX)
        else rendered
    )
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sources:
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        records.append({
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "chars": len(text.strip()),
            "effective_cap_chars": cap,
        })

    passed = bool(records) and not errors and not warnings and len(project_payload) <= cap
    return {
        "schema_version": 1,
        "kind": "dream-cycle-v3-context-health",
        "profile": profile,
        "profile_home": str(profile_home),
        "cwd": str(root),
        "context_length": context_length,
        "effective_cap_chars": cap,
        "active_sources": records,
        "active_source_count": len(records),
        "rendered_project_context_chars": len(project_payload),
        "truncation_warnings": warnings,
        "errors": errors,
        "pass": passed,
        "remediation_required": not passed,
        "remediation_key": f"context-file-continuity:{profile}:{root}",
    }


def write_context_health(report: dict[str, Any], path: Path | str) -> Path:
    """Write a fresh canonical JSON audit without overwriting prior evidence."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    create_file_exclusive(
        target, (canonical_json(report) + "\n").encode("utf-8")
    )
    return target

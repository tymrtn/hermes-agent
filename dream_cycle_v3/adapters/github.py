"""Read-only GitHub adapter over the `gh` CLI.

Write-incapability is structural, then double-checked:
- the only argv this module ever builds is `gh issue list --json ...`;
- `_assert_read_only` rejects any argv containing a mutating subcommand or a
  non-GET --method before execution, so a future edit cannot quietly turn
  this adapter into a writer;
- commands run with shell=False and a validated `owner/repo` (a leading `-`
  cannot be smuggled in as a flag).

`gh` missing, unauthenticated, offline, or failing degrades to a typed
'unavailable'/'error' result.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Callable, Sequence

from .base import AdapterResult, TaskItem

ADAPTER_NAME = "github"
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_ITEMS = 200

_FORBIDDEN_TOKENS = frozenset({
    "create", "edit", "close", "reopen", "delete", "merge", "comment", "label",
    "transfer", "pin", "unpin", "lock", "unlock", "review", "ready", "checkout",
    "fork", "clone", "push", "sync", "secret", "ssh-key", "gpg-key", "auth",
})
_ALLOWED_HEADS = (("issue", "list"), ("pr", "list"), ("api",))


class ReadOnlyViolation(RuntimeError):
    """Programming error: an argv that could mutate was constructed."""


def _assert_read_only(argv: Sequence[str]) -> None:
    if not argv or argv[0] != "gh":
        raise ReadOnlyViolation(f"only gh invocations are allowed: {argv!r}")
    head = tuple(argv[1:3])
    if not any(head[:len(h)] == h for h in _ALLOWED_HEADS):
        raise ReadOnlyViolation(f"subcommand not in read-only allowlist: {argv!r}")
    lowered = [a.lower() for a in argv[1:]]
    forbidden = _FORBIDDEN_TOKENS.intersection(lowered)
    if forbidden:
        raise ReadOnlyViolation(f"forbidden tokens {sorted(forbidden)} in {argv!r}")
    if "api" in head:
        if "--method" in lowered or "-x" in lowered:
            idx = lowered.index("--method") if "--method" in lowered else lowered.index("-x")
            if idx + 1 >= len(lowered) or lowered[idx + 1] != "get":
                raise ReadOnlyViolation(f"gh api must be GET: {argv!r}")
        for a in lowered:
            if a in ("-f", "--field", "--input", "--raw-field"):
                raise ReadOnlyViolation(f"gh api body fields are write-shaped: {argv!r}")


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), shell=False, capture_output=True,
                          text=True, timeout=30)


def read_github_issues(repo: str, *, runner: Runner | None = None,
                       gh_available: bool | None = None,
                       max_items: int = _MAX_ITEMS) -> AdapterResult:
    locator = repo
    if not _REPO_RE.match(repo or ""):
        return AdapterResult.unavailable(ADAPTER_NAME, locator or "<empty>",
                                         "invalid_repo_locator")
    if gh_available is None:
        gh_available = shutil.which("gh") is not None
    if not gh_available:
        return AdapterResult.unavailable(ADAPTER_NAME, locator, "gh_cli_not_found")
    argv = ["gh", "issue", "list", "--repo", repo, "--state", "all",
            "--limit", str(max_items),
            "--json", "number,title,state,updatedAt,url,assignees"]
    _assert_read_only(argv)
    try:
        proc = (runner or _default_runner)(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return AdapterResult.error(ADAPTER_NAME, locator,
                                   f"gh_invocation_failed:{type(exc).__name__}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        return AdapterResult.unavailable(
            ADAPTER_NAME, locator,
            f"gh_exit_{proc.returncode}:{stderr[0][:200] if stderr else 'no_stderr'}")
    try:
        raw = json.loads(proc.stdout or "[]")
        if not isinstance(raw, list):
            raise ValueError("expected a JSON array")
        items = []
        for entry in raw:
            # Only explicit states are trusted. An unrecognized state poisons
            # the whole snapshot as a typed error, so it can never prove
            # closure downstream (carry-forward reads 'ok' snapshots only).
            state_raw = str(entry.get("state", "")).upper()
            if state_raw == "OPEN":
                state = "open"
            elif state_raw == "CLOSED":
                state = "closed"
            else:
                return AdapterResult.error(
                    ADAPTER_NAME, locator,
                    f"unknown_issue_state:{state_raw or '<missing>'}"
                    f":#{entry.get('number', '?')}")
            items.append(TaskItem(
                item_id=str(entry["number"]),
                ref=f"github:{repo}#{entry['number']}",
                title=str(entry.get("title", "")),
                state=state,
                status_raw=state_raw,
                assignee=(entry.get("assignees") or [{}])[0].get("login")
                         if entry.get("assignees") else None,
                updated_at=entry.get("updatedAt"),
                url=entry.get("url"),
            ))
    except (ValueError, KeyError, TypeError) as exc:
        return AdapterResult.error(ADAPTER_NAME, locator,
                                   f"gh_output_parse_failed:{type(exc).__name__}:{exc}")
    return AdapterResult.ok(ADAPTER_NAME, locator, items)

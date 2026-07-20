"""Deterministic project routing (design §5 routing priority, Phase 1 tiers).

Tier order:
1. explicit external task reference in the observation text;
2. canonical path match on the evidence source id;
3. registered alias match in the observation text.

Each tier must produce exactly one project; zero matches falls through to the
next tier, more than one is ambiguity. Ambiguous or unresolved observations
abstain — the caller quarantines them. There is no default project and no
LLM tier in Phase 1 (an LLM proposal would be tier 5 per the plan and can
never auto-assign).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

TASK_REF_RE = re.compile(r"\b(kanban|github|todoist):[A-Za-z0-9_./#:-]*[A-Za-z0-9]")


@dataclass(frozen=True)
class RoutingDecision:
    project_id: str | None
    method: str            # external_task_ref | canonical_path | alias | ambiguous | unresolved
    matched: str | None = None
    detail: tuple[str, ...] = ()

    @property
    def routed(self) -> bool:
        return self.project_id is not None

    def to_payload(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "method": self.method,
                "matched": self.matched, "detail": list(self.detail)}


def _ref_matches_project(ref: str, project: dict[str, Any]) -> bool:
    provider = project["task_ssot"]["provider"]
    locator = project["task_ssot"]["locator"] or ""
    scheme, _, rest = ref.partition(":")
    if scheme != provider:
        return False
    if scheme == "kanban":
        board = rest.split(":", 1)[0]
        return bool(board) and board == locator
    if scheme == "github":
        repo = rest.split("#", 1)[0]
        return repo == locator or repo in project.get("repositories", [])
    if scheme == "todoist":
        # Task ids carry no namespace; provider-level match only.
        return True
    return False


def _path_prefix_matches(prefix: str, source_id: str) -> bool:
    """Prefix match on whole path/locator segments only.

    'profile:state' matches 'profile:state/x.md' and 'profile:state' itself,
    never 'profile:stateful/x.md' — a raw startswith would falsely route
    instead of abstaining.
    """
    if not prefix:
        return False
    if source_id == prefix:
        return True
    if prefix.endswith(("/", ":")):
        return source_id.startswith(prefix)
    return (source_id.startswith(prefix)
            and len(source_id) > len(prefix)
            and source_id[len(prefix)] in "/:")


def _single(matches: dict[str, str], method: str) -> RoutingDecision:
    if len(matches) == 1:
        (project_id, matched), = matches.items()
        return RoutingDecision(project_id=project_id, method=method,
                               matched=matched)
    return RoutingDecision(
        project_id=None, method="ambiguous", matched=None,
        detail=tuple(f"{method}:{pid}:{m}" for pid, m in sorted(matches.items())))


def route_observation(*, text: str, source_id: str,
                      registry: Iterable[dict[str, Any]]) -> RoutingDecision:
    projects = list(registry)

    # Tier 1: explicit external task reference.
    refs = [m.group(0) for m in TASK_REF_RE.finditer(text)]
    if refs:
        matches: dict[str, str] = {}
        for project in projects:
            for ref in refs:
                if _ref_matches_project(ref, project):
                    matches.setdefault(project["project_id"], ref)
        if matches:
            return _single(matches, "external_task_ref")

    # Tier 2: canonical path prefix (segment-bounded) on the evidence source id.
    path_matches: dict[str, str] = {}
    for project in projects:
        for prefix in project.get("canonical_paths", []):
            if _path_prefix_matches(prefix, source_id):
                path_matches.setdefault(project["project_id"], prefix)
    if path_matches:
        return _single(path_matches, "canonical_path")

    # Tier 3: registered alias as a whole word in the text.
    alias_matches: dict[str, str] = {}
    lowered = text.lower()
    for project in projects:
        for alias in project.get("aliases", []):
            if re.search(rf"(?<![\w-]){re.escape(alias.lower())}(?![\w-])", lowered):
                alias_matches.setdefault(project["project_id"], alias)
    if alias_matches:
        return _single(alias_matches, "alias")

    return RoutingDecision(project_id=None, method="unresolved")

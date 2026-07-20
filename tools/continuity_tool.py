"""continuity_lookup tool: read-only retrieval over the Dream Cycle v3
continuity store (wake/retrieval Phase 3).

Also home to the profile-aware path resolvers for the live continuity home —
the dream_cycle_v3 package deliberately never resolves live profile paths on
its own, so this module is where HERMES_HOME awareness lives. Every resolver
accepts an explicit ``home`` so multiplexed gateways can bind a specific
profile's paths instead of whatever HERMES_HOME happens to be ambient;
without one they derive from hermes_constants.get_hermes_home(). Nothing is
hardcoded to ~/.hermes. The explicit home is resolved once here (symlinks
above the profile home are the operator's business); everything at or below
it reached through a symlink is refused by the package's confined readers.

Kanban is the one deliberately *shared* root: boards live under the shared
Hermes root, not the profile home. ``kanban_root()`` derives that root
deterministically from the explicit profile home (``<root>/profiles/<name>``
-> ``<root>``; Docker/custom homes are their own root) — never from ambient
``HERMES_KANBAN_HOME``/``HERMES_KANBAN_DB``/current-board state, so a
worker's injected board pins can never redirect a lookup for a different
explicit ``kanban:<board>:<task>`` ref. The default-board back-compat
layout (``<root>/kanban.db``) is handled by dream_cycle_v3.kanban_layout.

The tool is gated by check_fn on the presence of an owned v3 continuity
store that resolves inside the active profile's continuity home (no symlink
crossing), so sessions on profiles without a store never see it and one
profile's store can never serve another profile's session.
"""
import logging
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.registry import (invalidate_check_fn, registry, tool_error,
                            tool_result)

logger = logging.getLogger(__name__)

CONTINUITY_DIR_NAME = "dream-cycle-v3"


def resolved_profile_home(home: Path | None = None) -> Path:
    """The explicit profile home, resolved once at this boundary so the
    package-side symlink confinement (which trusts the root as given but
    refuses links at or below it) has a canonical anchor."""
    try:
        return (home or get_hermes_home()).resolve()
    except OSError:
        return home or get_hermes_home()


def continuity_home(home: Path | None = None) -> Path:
    """Per-profile continuity home: <HERMES_HOME>/dream-cycle-v3."""
    return resolved_profile_home(home) / CONTINUITY_DIR_NAME


def continuity_store_path(home: Path | None = None) -> Path:
    return continuity_home(home) / "continuity.db"


def continuity_projects_home(home: Path | None = None) -> Path:
    """Explicit project-docs home (Phase 2 ProjectDocDestination layout:
    <home>/<project_id>/<doc>.md)."""
    return continuity_home(home) / "projects"


def continuity_skills_home(home: Path | None = None) -> Path:
    """Explicit per-profile skills home for project-context skills
    (<HERMES_HOME>/skills/[<category>/]<skill>/SKILL.md)."""
    return resolved_profile_home(home) / "skills"


def hermes_root_for_home(home: Path | None = None) -> Path:
    """Shared Hermes root derived from the explicit profile home:
    <root>/profiles/<name> -> <root>; any other home IS the root
    (standard ~/.hermes and Docker/custom deployments)."""
    home = resolved_profile_home(home)
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def kanban_root(home: Path | None = None) -> Path:
    """Read-only tracker refresh root: the shared Hermes root that anchors
    the canonical hermes_cli.kanban_db layout (named boards under
    <root>/kanban/boards/, the default board at <root>/kanban.db)."""
    return hermes_root_for_home(home)


def todoist_export_path(home: Path | None = None) -> Path | None:
    """Optional read-only Todoist export for tracker refresh. Configured by
    placing the export at <continuity_home>/todoist_export.json; absent means
    unconfigured (threads with todoist refs fall back to collector snapshots
    and are marked stale with an age warning)."""
    path = continuity_home(home) / "todoist_export.json"
    return path if path.is_file() else None


def check_continuity_requirements() -> bool:
    """Visible only when an owned v3 continuity store exists for this
    profile AND resolves below the profile's own continuity home with no
    symlink crossing. A foreign/corrupt/cross-profile file hides the tool
    rather than crashing."""
    try:
        from dream_cycle_v3.store import (assert_store_confined,
                                          inspect_store_identity)
        home = resolved_profile_home()
        store = continuity_store_path(home)
        # Anchor at the profile home so a symlinked dream-cycle-v3 dir is
        # refused, not just a symlinked db file.
        assert_store_confined(store, home)
        return inspect_store_identity(store) == "owned"
    except Exception:
        return False


# Per-home memory of the last continuity store fingerprint observed here.
# When a store appears, changes, or is removed under a profile home, the
# cached ``check_continuity_requirements`` verdict for that home is stale;
# dropping it lets the very next definitions build re-probe instead of
# serving the pre-change availability for up to the 30s check_fn TTL. Keyed
# by resolved home so one profile's store lifecycle never forces another's
# to re-probe on every call.
_UNSEEN = object()
_last_store_fp_by_home: "dict[str | None, object]" = {}


def continuity_store_fingerprint(home: Path | None = None):
    """Cheap availability fingerprint for the tool-definitions cache: the
    outer definitions memo must not pin a stale continuity verdict after a
    store is created or removed. When the fingerprint changes for this
    profile home, the continuity check_fn's TTL entry is dropped so the 30s
    check_fn cache one level below the memo re-evaluates on the next build —
    this covers both provisioning and the rollback unlink, neither of which
    routes through a store method that could invalidate on its own. Never
    raises."""
    try:
        try:
            st = continuity_store_path(home).stat()
            fp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            fp = None
        try:
            key = str(resolved_profile_home(home))
        except Exception:
            key = None
        if _last_store_fp_by_home.get(key, _UNSEEN) != fp:
            _last_store_fp_by_home[key] = fp
            invalidate_check_fn(check_continuity_requirements)
        return fp
    except Exception:
        return None


def continuity_lookup_tool(project=None, thread_id=None, query=None) -> str:
    # Everything — including the dream_cycle_v3 import — stays inside the
    # guarded block: on a platform where the package cannot import, an
    # advertised tool degrades to a typed error instead of a traceback.
    # Error text returned to the model is either a typed, path-free lookup
    # message (sanitized and capped) or a fixed string — a raw exception
    # can carry store paths and unbounded payloads, so it never passes
    # through verbatim (details go to the log).
    try:
        from dream_cycle_v3.lookup import (LookupBadRequest, LookupUnavailable,
                                           continuity_lookup)
        from dream_cycle_v3.sanitize import sanitize_text
    except Exception:
        logger.exception("continuity_lookup import failed")
        return tool_error("continuity_lookup unavailable on this platform",
                          error_type="unavailable")
    try:
        home = resolved_profile_home()
        payload = continuity_lookup(
            store_path=continuity_store_path(home),
            projects_home=continuity_projects_home(home),
            skills_home=continuity_skills_home(home),
            project=project or None,
            thread_id=thread_id or None,
            query=query or None,
            kanban_root=kanban_root(home),
            todoist_export_path=todoist_export_path(home),
            confine_root=home,
        )
    except LookupBadRequest as exc:
        return tool_error(sanitize_text(str(exc), 200),
                          error_type="bad_request")
    except LookupUnavailable as exc:
        return tool_error(sanitize_text(str(exc), 200),
                          error_type="unavailable")
    except Exception:
        logger.exception("continuity_lookup failed")
        return tool_error("continuity_lookup failed (internal error; "
                          "details logged)", error_type="internal")
    return tool_result(payload)


CONTINUITY_LOOKUP_SCHEMA = {
    "name": "continuity_lookup",
    "description": (
        "Look up bounded continuity state from the Dream Cycle v3 store "
        "(read-only). Pass exactly one of: project (registry metadata, map "
        "excerpt, project-context skill excerpt, open threads with live "
        "tracker status, durable decisions), thread_id (tracker status, "
        "owner, next action, disposition history), or query "
        "(registry/ledger search with confidence and source dates). Returns "
        "typed JSON; never transcripts, secrets, or the full backlog."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Project id, canonical name, or exact alias",
            },
            "thread_id": {
                "type": "string",
                "description": "Continuity thread id (th_...)",
            },
            "query": {
                "type": "string",
                "description": "Free-text search over projects/threads/facts",
            },
        },
        "required": [],
    },
}

registry.register(
    name="continuity_lookup",
    toolset="continuity",
    schema=CONTINUITY_LOOKUP_SCHEMA,
    handler=lambda args, **kw: continuity_lookup_tool(
        project=args.get("project"),
        thread_id=args.get("thread_id"),
        query=args.get("query"),
    ),
    check_fn=check_continuity_requirements,
    emoji="🧵",
)

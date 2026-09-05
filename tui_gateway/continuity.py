"""Session-stable identity binding, deferred until a model-bound prompt."""
from pathlib import Path


def _seed_history_present(messages) -> bool:
    return messages is not None and messages != []


def _arm_deferred_wake(session: dict) -> None:
    session["wake_pending"] = True
    session.setdefault("wake_is_new_session", not (
        _seed_history_present(session.get("history"))
        or session.get("parent_session_id") or session.get("resume_session_id")))


def _compose_ephemeral_prompt(agent, base):
    packet = getattr(agent, "_wake_packet_text", None)
    if not packet:
        agent.ephemeral_system_prompt = base or None
        return agent.ephemeral_system_prompt
    if base and packet in base:
        agent.ephemeral_system_prompt = base
        return base
    agent.ephemeral_system_prompt = "\n\n".join(part for part in (base, packet) if part)
    return agent.ephemeral_system_prompt


def _attach_wake_for_prompt(session: dict, agent, message: str) -> None:
    if not session.pop("wake_pending", False):
        return
    from gateway.continuity_wake import (
        ensure_wake_text_for_session_id, finalize_pending_wake_as_none)
    db = getattr(agent, "_session_db", None)
    key = session.get("resume_session_id") or session.get("session_key")
    if _seed_history_present(session.get("history")):
        finalize_pending_wake_as_none(db, key)
    packet = ensure_wake_text_for_session_id(
        db, key, is_new_session=bool(session.get("wake_is_new_session"))
        and not _seed_history_present(session.get("history")),
        first_message=message, workspace_path=session.get("cwd"),
        profile_home=Path(session["profile_home"]) if session.get("profile_home") else None,
        create_source=session.get("source") or "tui")
    agent._wake_packet_text = packet
    agent.ephemeral_system_prompt = _compose_ephemeral_prompt(
        agent, getattr(agent, "ephemeral_system_prompt", None))


def persist_wake_lifecycle(session: dict, db, key: str) -> None:
    from gateway.continuity_wake import (
        mark_wake_pending_for_session_id, materialize_wake_record_for_child)
    if parent := session.get("parent_session_id"):
        materialize_wake_record_for_child(db, key, parent)
    elif (session.get("wake_is_new_session")
          and not _seed_history_present(session.get("history"))
          and not db.get_messages(key)):
        mark_wake_pending_for_session_id(db, key)

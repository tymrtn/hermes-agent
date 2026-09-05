"""Dream Cycle v3 wake binding on the classic CLI surface."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cli import HermesCLI


def _cli(history=None, durable=None):
    db = MagicMock()
    db.get_messages.return_value = list(durable or [])
    cli = HermesCLI.__new__(HermesCLI)
    cli._session_db = db
    cli.session_id = "classic-cli-session"
    cli.conversation_history = list(history or [])
    return cli, db


def test_classic_cli_chat_binds_before_staging(monkeypatch):
    cli, _ = _cli()
    agent = SimpleNamespace()
    seen = []
    def attach(current_agent, message):
        seen.append((current_agent, message, list(cli.conversation_history)))
    monkeypatch.setattr(cli, "_attach_continuity_wake_for_prompt", attach)
    cli._chat_stage_user_message(agent, "first question")
    assert seen == [(agent, "first question", [])]
    assert cli.conversation_history[-1]["content"] == "first question"


def test_classic_cli_binds_first_message_ephemerally(monkeypatch):
    cli, db = _cli()
    agent = SimpleNamespace(
        session_id="classic-cli-session",
        ephemeral_system_prompt="BASE",
    )
    monkeypatch.chdir("/")

    with patch(
        "gateway.continuity_wake.ensure_wake_text_for_session_id",
        return_value="WAKE-PACKET",
    ) as ensure:
        cli._attach_continuity_wake_for_prompt(agent, "project status")
        cli._attach_continuity_wake_for_prompt(agent, "project status")

    assert agent._wake_packet_text == "WAKE-PACKET"
    assert agent.ephemeral_system_prompt == "BASE\n\nWAKE-PACKET"
    ensure.assert_called_with(
        db,
        "classic-cli-session",
        is_new_session=True,
        first_message="project status",
        workspace_path="/",
        create_source="cli",
    )


def test_classic_cli_history_bearing_session_is_not_first_message():
    cli, db = _cli(
        history=[{"role": "user", "content": "old"}],
        durable=[{"role": "user", "content": "old"}],
    )
    agent = SimpleNamespace(
        session_id="classic-cli-session",
        ephemeral_system_prompt=None,
    )

    with patch(
        "gateway.continuity_wake.ensure_wake_text_for_session_id",
        return_value=None,
    ) as ensure:
        cli._attach_continuity_wake_for_prompt(agent, "later message")

    assert ensure.call_args.kwargs["is_new_session"] is False
    assert not hasattr(agent, "_wake_packet_text")


def test_classic_cli_multimodal_evidence_excludes_image_urls():
    cli, _db = _cli()
    agent = SimpleNamespace(
        session_id="classic-cli-session",
        ephemeral_system_prompt=None,
    )
    message = [
        {"type": "text", "text": "project status"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
    ]

    with patch(
        "gateway.continuity_wake.ensure_wake_text_for_session_id",
        return_value=None,
    ) as ensure:
        cli._attach_continuity_wake_for_prompt(agent, message)

    assert ensure.call_args.kwargs["first_message"] == "project status"

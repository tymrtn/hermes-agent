from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner(mode="phone"):
    runner = object.__new__(GatewayRunner)
    runner._session_mode_skills = {"telegram:dm:u1": mode}
    return runner


def _event(text="write the report"):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            chat_type="dm",
        ),
        message_id="m1",
    )


def test_session_mode_skill_injects_on_plain_followup(monkeypatch):
    calls = []

    def fake_build(cmd_key, user_instruction="", task_id=None, runtime_note=""):
        calls.append((cmd_key, user_instruction, task_id, runtime_note))
        return f"MODE:{cmd_key}\nUSER:{user_instruction}"

    monkeypatch.setattr("agent.skill_commands.build_skill_invocation_message", fake_build)
    runner = _runner("phone")
    event = _event("make it concise")

    assert runner._maybe_apply_session_mode_skill(event, "telegram:dm:u1", "task-1") is True
    assert event.text == "MODE:/phone\nUSER:make it concise"
    assert calls == [(
        "/phone",
        "make it concise",
        "task-1",
        "Tyler previously switched this gateway session into /phone mode. Apply this mode to the current user message.",
    )]


def test_session_mode_skill_skips_slash_commands(monkeypatch):
    def fail_build(*_args, **_kwargs):  # pragma: no cover - should not run
        raise AssertionError("slash commands must not be wrapped in mode skills")

    monkeypatch.setattr("agent.skill_commands.build_skill_invocation_message", fail_build)
    runner = _runner("mac")
    event = _event("/help")

    assert runner._maybe_apply_session_mode_skill(event, "telegram:dm:u1", "task-1") is False
    assert event.text == "/help"


def test_session_mode_skill_ignores_unknown_mode(monkeypatch):
    def fail_build(*_args, **_kwargs):  # pragma: no cover - should not run
        raise AssertionError("unknown modes must not be injected")

    monkeypatch.setattr("agent.skill_commands.build_skill_invocation_message", fail_build)
    runner = _runner("desktop")
    event = _event("hello")

    assert runner._maybe_apply_session_mode_skill(event, "telegram:dm:u1", "task-1") is False
    assert event.text == "hello"

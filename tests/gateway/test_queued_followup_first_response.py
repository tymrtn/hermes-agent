"""Regression tests for the queued-follow-up first-response path.

History (Dream Cycle 2026-06-12): ``_run_agent`` called
``_send_first_response_before_queued_followup(event=event, ...)`` but no
``event`` name exists in that scope — the NameError fired on *argument
evaluation*, so the entire first response (text included) was silently
dropped whenever a queued message followed a completed turn.  gateway.log
shows five occurrences between 2026-06-09 and 2026-06-12:
``Failed to send first response before queued message: name 'event' is
not defined``.
"""

import ast
import asyncio
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

RUN_PY = Path(__file__).parent.parent.parent / "gateway" / "run.py"


# ---------------------------------------------------------------------------
# Unit: helper must deliver the text even when no MessageEvent is available
# (leftover-/steer queued turns have no dequeued event), and must only
# attempt reply-anchored media delivery when an event exists.
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, response, metadata=None):
        self.sent.append((chat_id, response, metadata))


def _make_runner_stub():
    from gateway.run import GatewayRunner

    stub = SimpleNamespace(media_calls=[])

    async def _record_media(response, event, adapter):
        stub.media_calls.append((response, event, adapter))

    stub._deliver_media_from_response = _record_media
    stub._impl = GatewayRunner._send_first_response_before_queued_followup
    return stub


def test_first_response_sends_text_without_event():
    stub = _make_runner_stub()
    adapter = _RecordingAdapter()
    source = SimpleNamespace(chat_id="chat-1")

    asyncio.run(
        stub._impl(
            stub,
            adapter=adapter,
            source=source,
            event=None,
            response="first response text",
            metadata=None,
        )
    )

    assert adapter.sent == [("chat-1", "first response text", None)]
    assert stub.media_calls == []


def test_first_response_delivers_media_with_event():
    stub = _make_runner_stub()
    adapter = _RecordingAdapter()
    source = SimpleNamespace(chat_id="chat-2")
    event = SimpleNamespace(source=source)

    asyncio.run(
        stub._impl(
            stub,
            adapter=adapter,
            source=source,
            event=event,
            response="text with media",
            metadata={"thread": "t"},
        )
    )

    assert adapter.sent == [("chat-2", "text with media", {"thread": "t"})]
    assert stub.media_calls == [("text with media", event, adapter)]


# ---------------------------------------------------------------------------
# Static scope check: every name used in the kwargs of the
# _send_first_response_before_queued_followup call inside _run_agent must be
# bound somewhere in that function (or be a module-level/builtin name).
# This is what would have caught ``event=event`` at review time.
# ---------------------------------------------------------------------------


def _collect_bound_names(func_node: ast.AST) -> set:
    bound = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            for arg_field in ("args", "posonlyargs", "kwonlyargs"):
                for a in getattr(node.args, arg_field, []):
                    bound.add(a.arg)
            for special in (node.args.vararg, node.args.kwarg):
                if special is not None:
                    bound.add(special.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def _module_level_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in ast.walk(node):
                if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
                    names.add(target.id)
    return names


def test_queued_followup_call_uses_only_bound_names():
    tree = ast.parse(RUN_PY.read_text())
    module_names = _module_level_names(tree)

    run_agent = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_agent":
            run_agent = node
            break
    assert run_agent is not None, "_run_agent not found in gateway/run.py"

    bound = _collect_bound_names(run_agent)
    allowed = bound | module_names | set(dir(builtins)) | {"self"}

    calls = [
        node
        for node in ast.walk(run_agent)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_send_first_response_before_queued_followup"
    ]
    assert calls, (
        "_send_first_response_before_queued_followup call not found in _run_agent"
    )

    for call in calls:
        for kw in call.keywords:
            for name_node in ast.walk(kw.value):
                if isinstance(name_node, ast.Name) and isinstance(
                    name_node.ctx, ast.Load
                ):
                    assert name_node.id in allowed, (
                        f"kwarg '{kw.arg}' references '{name_node.id}', which is "
                        f"not bound in _run_agent scope — this is the "
                        f"NameError-at-argument-evaluation class that silently "
                        f"dropped first responses (gateway.log 2026-06-09..12)"
                    )

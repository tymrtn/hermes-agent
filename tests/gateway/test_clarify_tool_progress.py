"""Regression coverage for clarify prompts not duplicating as gateway status."""

from gateway.run import (
    _suppress_gateway_status_action,
    _suppress_gateway_tool_progress,
)


def test_clarify_tool_progress_is_suppressed():
    assert _suppress_gateway_tool_progress("clarify") is True
    assert _suppress_gateway_tool_progress(" Clarify ") is True


def test_regular_tool_progress_is_not_suppressed():
    assert _suppress_gateway_tool_progress("read_file") is False
    assert _suppress_gateway_tool_progress(None) is False


def test_clarify_status_detail_is_suppressed():
    assert _suppress_gateway_status_action("clarify") is True
    assert _suppress_gateway_status_action("waiting for user clarify response") is True


def test_regular_status_detail_is_not_suppressed():
    assert _suppress_gateway_status_action("terminal") is False
    assert _suppress_gateway_status_action("executing tool: read_file") is False
    assert _suppress_gateway_status_action(None) is False

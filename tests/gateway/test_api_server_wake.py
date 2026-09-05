"""Phase 3 wake packet on the API-server surface (the authoritative agent
host in proxy topologies): bound once per durable session id, appended to
the ephemeral system prompt, byte-stable across requests, absent without an
owned store.
"""
import pytest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_constants import get_hermes_home

from dream_cycle_v3.store import ContinuityStore

NOW_ISO = "2026-07-11T08:00:00+00:00"


def make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(
        enabled=True, extra={"host": "127.0.0.1", "port": 0}))


def make_app(adapter) -> web.Application:
    app = web.Application()
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions",
                        adapter._handle_chat_completions)
    app.router.add_post("/api/sessions/{session_id}/chat",
                        adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream",
                        adapter._handle_session_chat_stream)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    return app


def capture_run_agent(captured: list):
    """A _run_agent double that records each run's ephemeral system prompt."""
    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    return _mock_run_agent


def seed_store():
    home = get_hermes_home()
    store_path = home / "dream-cycle-v3" / "continuity.db"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with ContinuityStore(store_path) as store:
        store.migrate(NOW_ISO)
        store.upsert_project({
            "schema_version": 1, "project_id": "api-proj",
            "canonical_name": "API project", "aliases": ["apiproj"],
            "canonical_paths": [], "repositories": [], "status": "active",
            "owner": "default",
            "task_ssot": {"provider": "none", "locator": None,
                          "write_policy": "read_only"},
            "context_skill_id": None, "memory_policy": "warm_only",
            "sensitivity_policy": "normal", "retrieval_terms": [],
            "registry_version": 1,
            "last_verified_at": "2026-07-10T00:00:00+00:00",
        }, NOW_ISO)


@pytest.mark.asyncio
async def test_wake_packet_bound_once_and_stable(tmp_path):
    seed_store()
    adapter = make_adapter()
    captured = []

    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    body = {"model": "test",
            "messages": [{"role": "user", "content": "hello there"}]}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            assert (await cli.post("/v1/chat/completions", json=body)).status == 200
            assert (await cli.post("/v1/chat/completions", json=body)).status == 200

    assert len(captured) == 2
    assert captured[0] is not None
    assert "[Continuity wake packet" in captured[0]
    # Byte-stable across requests of the same derived session.
    assert captured[0] == captured[1]
    assert captured[0].count("[Continuity wake packet") == 1


@pytest.mark.asyncio
async def test_no_store_first_call_marks_none_durably(tmp_path):
    """Empty history is not the durable first-call marker: a session whose
    one first-call attempt found no store persists an explicit
    attempted-none state, so a later identical (still empty-history)
    request can never opportunistically bind against a store created in
    between."""
    adapter = make_adapter()
    captured = []

    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    body = {"model": "test",
            "messages": [{"role": "user", "content": "hello sentinel"}]}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            assert (await cli.post("/v1/chat/completions", json=body)).status == 200
            seed_store()   # store appears AFTER the session's first call
            assert (await cli.post("/v1/chat/completions", json=body)).status == 200

    assert len(captured) == 2
    for prompt in captured:
        assert prompt is None or "[Continuity wake packet" not in prompt
    # And the durable marker is the explicit sentinel, not a NULL.
    from gateway.continuity_wake import wake_state_from_json
    from gateway.platforms.api_server import _derive_chat_session_id
    db = adapter._ensure_session_db()
    session_id = _derive_chat_session_id(None, "hello sentinel")
    state, _ = wake_state_from_json(db.get_session_wake_packet(session_id))
    assert state == "none"


@pytest.mark.asyncio
async def test_multiplex_mode_disables_api_wake(tmp_path):
    """Multiplex boundary (fail closed): the API surface has no per-profile
    routing, so with gateway.multiplex_profiles on it must not borrow the
    primary ambient profile — no packet is bound at all."""
    from agent.secret_scope import set_multiplex_active
    seed_store()
    adapter = make_adapter()
    captured = []

    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    body = {"model": "test",
            "messages": [{"role": "user", "content": "hello multiplexed"}]}
    app = make_app(adapter)
    set_multiplex_active(True)
    try:
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent",
                              side_effect=_mock_run_agent):
                assert (await cli.post("/v1/chat/completions",
                                       json=body)).status == 200
    finally:
        set_multiplex_active(False)
    assert captured[0] is None or "[Continuity wake packet" not in captured[0]


@pytest.mark.asyncio
async def test_no_store_means_no_packet(tmp_path):
    adapter = make_adapter()
    captured = []

    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            resp = await cli.post("/v1/chat/completions", json={
                "model": "test",
                "messages": [{"role": "user", "content": "hi"}]})
            assert resp.status == 200
    assert captured[0] is None or "[Continuity wake packet" not in captured[0]


@pytest.mark.asyncio
async def test_concurrent_first_requests_inject_one_packet(tmp_path):
    """Two simultaneous first requests for the same derived session must not
    build/inject competing packets: the CAS lets one build win and both
    requests carry the winner's bytes."""
    import asyncio
    seed_store()
    adapter = make_adapter()
    captured = []
    release = asyncio.Event()

    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        await release.wait()   # hold both requests in flight together
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    body = {"model": "test",
            "messages": [{"role": "user", "content": "hello racing"}]}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
            t1 = asyncio.create_task(cli.post("/v1/chat/completions",
                                              json=body))
            t2 = asyncio.create_task(cli.post("/v1/chat/completions",
                                              json=body))
            while len(captured) < 2:
                await asyncio.sleep(0.01)
            release.set()
            r1, r2 = await asyncio.gather(t1, t2)
            assert r1.status == 200 and r2.status == 200

    assert len(captured) == 2
    assert all(c and "[Continuity wake packet" in c for c in captured)
    assert captured[0] == captured[1]     # identical injected bytes
    # And both match the single persisted winner.
    from gateway.continuity_wake import load_wake_state_for_session
    from gateway.platforms.api_server import _derive_chat_session_id
    db = adapter._ensure_session_db()
    session_id = _derive_chat_session_id(None, "hello racing")
    state, binding = load_wake_state_for_session(db, session_id)
    assert state == "bound"
    assert binding["text"] in captured[0]
    assert captured[0].count("[Continuity wake packet") == 1


# -- /api/sessions/{id}/chat (sync + stream) -----------------------------------

@pytest.mark.asyncio
async def test_session_chat_binds_once_and_preserves_system_message(tmp_path):
    """The durable-session chat endpoint runs the same once-per-session wake
    lifecycle as /v1/chat/completions: bound on the first call, restored
    byte-stable on replays (the durable record, not empty history, is the
    marker), layered under the caller's system_message."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-chat-1", source="api_server")
    captured = []
    body = {"message": "hello there", "system_message": "Client prompt"}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            assert (await cli.post("/api/sessions/sess-chat-1/chat",
                                   json=body)).status == 200
            # The mocked agent persisted nothing, so the second request is
            # an empty-history replay of the same durable session.
            assert (await cli.post("/api/sessions/sess-chat-1/chat",
                                   json=body)).status == 200

    assert len(captured) == 2
    assert captured[0].startswith("Client prompt")
    assert captured[0].count("[Continuity wake packet") == 1
    assert captured[0] == captured[1]
    state, binding = load_wake_state_for_session(db, "sess-chat-1")
    assert state == "bound"
    assert binding["text"] in captured[0]


@pytest.mark.asyncio
async def test_session_chat_stream_shares_lifecycle_with_sync(tmp_path):
    """The streaming variant injects the exact same bytes as the sync
    endpoint for the same durable session — one shared helper, no drift."""
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-stream-1", source="api_server")
    captured = []
    body = {"message": "hello stream"}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            resp = await cli.post("/api/sessions/sess-stream-1/chat/stream",
                                  json=body)
            assert resp.status == 200
            await resp.text()          # drain the SSE stream to completion
            assert (await cli.post("/api/sessions/sess-stream-1/chat",
                                   json=body)).status == 200

    assert len(captured) == 2
    assert captured[0] is not None
    assert captured[0].count("[Continuity wake packet") == 1
    assert captured[0] == captured[1]


@pytest.mark.asyncio
async def test_session_chat_no_store_marks_none_durably(tmp_path):
    """A session-chat first call with no store persists the attempted-none
    sentinel; a later empty-history replay never binds against a store that
    appeared in between."""
    from gateway.continuity_wake import wake_state_from_json
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-chat-2", source="api_server")
    captured = []
    body = {"message": "hello sentinel"}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            assert (await cli.post("/api/sessions/sess-chat-2/chat",
                                   json=body)).status == 200
            seed_store()   # store appears AFTER the one first-call attempt
            assert (await cli.post("/api/sessions/sess-chat-2/chat",
                                   json=body)).status == 200

    for prompt in captured:
        assert prompt is None or "[Continuity wake packet" not in prompt
    state, _ = wake_state_from_json(db.get_session_wake_packet("sess-chat-2"))
    assert state == "none"


@pytest.mark.asyncio
async def test_session_chat_corrupt_state_fails_closed(tmp_path):
    """Present-but-corrupt durable wake state is terminal on this endpoint
    too: no packet, no rebuild, and the corrupt record is preserved."""
    seed_store()   # a rebuild WOULD produce a packet if allowed
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-chat-3", source="api_server")
    db.set_session_wake_packet("sess-chat-3", "{corrupt")
    captured = []
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            assert (await cli.post("/api/sessions/sess-chat-3/chat",
                                   json={"message": "hi"})).status == 200

    assert captured[0] is None or "[Continuity wake packet" not in captured[0]
    assert db.get_session_wake_packet("sess-chat-3") == "{corrupt"


# -- /v1/responses ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_responses_binds_once_and_never_carries_packet_forward(tmp_path):
    """/v1/responses binds on the session's first call and re-injects the
    same bytes when the conversation chains via previous_response_id — while
    the stored `instructions` stay clean, so chaining can never carry the
    packet forward and double-append it."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    captured = []
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            r1 = await cli.post("/v1/responses", json={
                "model": "test", "input": "hello responses",
                "instructions": "Pirate mode"})
            assert r1.status == 200
            data1 = await r1.json()
            session_id = r1.headers["X-Hermes-Session-Id"]
            r2 = await cli.post("/v1/responses", json={
                "model": "test", "input": "and again",
                "previous_response_id": data1["id"]})
            assert r2.status == 200
            assert r2.headers["X-Hermes-Session-Id"] == session_id

    assert len(captured) == 2
    assert captured[0].startswith("Pirate mode")
    assert captured[0].count("[Continuity wake packet") == 1
    # Chained turn: instructions carried forward clean, same packet bytes
    # appended exactly once (bound state restored, never rebuilt).
    assert captured[1].count("[Continuity wake packet") == 1
    wake0 = captured[0][captured[0].index("[Continuity wake packet"):]
    wake1 = captured[1][captured[1].index("[Continuity wake packet"):]
    assert wake0 == wake1
    stored = adapter._response_store.get(data1["id"])
    assert stored["instructions"] == "Pirate mode"
    db = adapter._ensure_session_db()
    state, binding = load_wake_state_for_session(db, session_id)
    assert state == "bound"
    assert binding["text"] == wake0


@pytest.mark.asyncio
async def test_responses_no_store_marks_none_durably(tmp_path):
    """A /v1/responses first call with no store persists the attempted-none
    sentinel under the durable session id."""
    from gateway.continuity_wake import wake_state_from_json
    adapter = make_adapter()
    captured = []
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            resp = await cli.post("/v1/responses", json={
                "model": "test", "input": "hello none"})
            assert resp.status == 200
            session_id = resp.headers["X-Hermes-Session-Id"]

    assert captured[0] is None or "[Continuity wake packet" not in captured[0]
    db = adapter._ensure_session_db()
    state, _ = wake_state_from_json(db.get_session_wake_packet(session_id))
    assert state == "none"


@pytest.mark.asyncio
async def test_multiplex_disables_wake_on_all_durable_endpoints(tmp_path):
    """The multiplex fail-closed boundary covers every durable API
    first-message endpoint, not just /v1/chat/completions."""
    from agent.secret_scope import set_multiplex_active
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-mux-1", source="api_server")
    captured = []
    app = make_app(adapter)
    set_multiplex_active(True)
    try:
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent",
                              side_effect=capture_run_agent(captured)):
                assert (await cli.post("/api/sessions/sess-mux-1/chat",
                                       json={"message": "hi"})).status == 200
                resp = await cli.post("/api/sessions/sess-mux-1/chat/stream",
                                      json={"message": "hi"})
                assert resp.status == 200
                await resp.text()
                assert (await cli.post("/v1/responses", json={
                    "model": "test", "input": "hi"})).status == 200
    finally:
        set_multiplex_active(False)

    assert len(captured) == 3
    for prompt in captured:
        assert prompt is None or "[Continuity wake packet" not in prompt


# -- Multimodal first-message evidence (all durable endpoints) ------------------

IMAGE_URL = "https://cdn.example.com/screenshots/apiproj-board.png"
DATA_URL = "data:image/png;base64,QkFTRTY0TUFSS0VSCg=="


def capture_agent_kwargs(captured: list):
    """A _run_agent double that records each run's full kwargs."""
    async def _mock_run_agent(**kwargs):
        captured.append(kwargs)
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    return _mock_run_agent


@pytest.mark.parametrize("content,expected", [
    # Plain strings pass through unchanged (the pre-multimodal contract).
    ("continue kanban:board:42", "continue kanban:board:42"),
    # Normalized text+image: only the text, image part contributes nothing.
    ([{"type": "text", "text": "continuing apiproj work"},
      {"type": "image_url", "image_url": {"url": IMAGE_URL}}],
     "continuing apiproj work"),
    # Multiple text parts keep input order.
    ([{"type": "text", "text": "first"},
      {"type": "image_url", "image_url": {"url": DATA_URL}},
      {"type": "text", "text": "second"}],
     "first\nsecond"),
    # Image-only: no evidence, and the URL never leaks.
    ([{"type": "image_url", "image_url": {"url": IMAGE_URL}}], ""),
    ([], ""),
    (None, ""),
    # Unsupported / corrupt shapes fail closed to empty evidence.
    (42, ""),
    ({"type": "text", "text": "bare dict"}, ""),
    ([["nested", "list"]], ""),
    ([{"type": "text", "text": 123}], ""),
    ([{"type": "tool_result", "content": "sneaky evidence"}], ""),
    ([{"type": "text", "text": "lead"}, "bare string in list"], ""),
])
def test_wake_evidence_text_shapes(content, expected):
    """The shared extractor derives wake evidence only from the normalized
    shapes this API server emits (str, or canonical text/image_url parts);
    everything else safely produces empty evidence."""
    from gateway.platforms.api_server import _wake_evidence_text
    assert _wake_evidence_text(content) == expected


def test_wake_evidence_text_hard_cap():
    from gateway.platforms.api_server import (MAX_WAKE_EVIDENCE_LENGTH,
                                              _wake_evidence_text)
    huge = "x" * (MAX_WAKE_EVIDENCE_LENGTH * 3)
    out = _wake_evidence_text([{"type": "text", "text": huge}])
    assert len(out) == MAX_WAKE_EVIDENCE_LENGTH
    many = [{"type": "text", "text": "y" * 1000} for _ in range(3 * (
        MAX_WAKE_EVIDENCE_LENGTH // 1000))]
    assert len(_wake_evidence_text(many)) <= MAX_WAKE_EVIDENCE_LENGTH


def test_wake_evidence_text_validates_past_the_cap():
    """Corruption AFTER the evidence cap still fails closed to empty: the
    cap bounds accumulation, never structural validation — the permanently
    durable binding must not ride evidence from a malformed turn."""
    from gateway.platforms.api_server import (MAX_WAKE_EVIDENCE_LENGTH,
                                              _wake_evidence_text)
    filler = [{"type": "text",
               "text": "x" * (MAX_WAKE_EVIDENCE_LENGTH + 1000)}]
    for bad_tail in (["nested", "list"],
                     {"type": "text", "text": 123},
                     {"type": "tool_result", "content": "sneaky evidence"},
                     "bare string in list",
                     42):
        assert _wake_evidence_text(filler + [bad_tail]) == ""
    # Well-formed parts past the cap keep the capped evidence intact.
    ok = filler + [{"type": "image_url", "image_url": {"url": IMAGE_URL}},
                   {"type": "text", "text": "tail text"}]
    assert _wake_evidence_text(ok) == "x" * MAX_WAKE_EVIDENCE_LENGTH


@pytest.mark.asyncio
async def test_chat_completions_multimodal_binds_with_text_evidence(tmp_path):
    """A multimodal first message (project text + image) binds using its
    text evidence, the image URL never enters the wake evidence or packet,
    and the full multimodal payload still reaches the agent unchanged."""
    from gateway.continuity_wake import load_wake_state_for_session
    from gateway.platforms.api_server import (_derive_chat_session_id,
                                              _normalize_multimodal_content)
    seed_store()
    adapter = make_adapter()
    captured = []
    content = [{"type": "text", "text": "continuing apiproj work"},
               {"type": "image_url", "image_url": {"url": IMAGE_URL}}]
    body = {"model": "test",
            "messages": [{"role": "user", "content": content}]}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_agent_kwargs(captured)):
            assert (await cli.post("/v1/chat/completions",
                                   json=body)).status == 200

    prompt = captured[0]["ephemeral_system_prompt"]
    assert prompt is not None and "[Continuity wake packet" in prompt
    assert "Active project: API project (api-proj)" in prompt
    assert IMAGE_URL not in prompt
    # Durable binding carries the activated project, never the URL.
    db = adapter._ensure_session_db()
    session_id = _derive_chat_session_id(
        None, _normalize_multimodal_content(content))
    state, binding = load_wake_state_for_session(db, session_id)
    assert state == "bound"
    assert binding["project_id"] == "api-proj"
    assert IMAGE_URL not in db.get_session_wake_packet(session_id)
    # The agent still receives the untouched multimodal message.
    assert captured[0]["user_message"] == _normalize_multimodal_content(content)


@pytest.mark.asyncio
async def test_session_chat_multimodal_binds_with_text_evidence(tmp_path):
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-mm-1", source="api_server")
    captured = []
    content = [{"type": "text", "text": "continuing apiproj work"},
               {"type": "image_url", "image_url": {"url": IMAGE_URL}}]
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_agent_kwargs(captured)):
            assert (await cli.post("/api/sessions/sess-mm-1/chat",
                                   json={"message": content})).status == 200

    prompt = captured[0]["ephemeral_system_prompt"]
    assert prompt is not None
    assert "Active project: API project (api-proj)" in prompt
    assert IMAGE_URL not in prompt
    state, binding = load_wake_state_for_session(db, "sess-mm-1")
    assert state == "bound"
    assert binding["project_id"] == "api-proj"
    assert IMAGE_URL not in db.get_session_wake_packet("sess-mm-1")
    sent = captured[0]["user_message"]
    assert isinstance(sent, list) and sent[1]["type"] == "image_url"
    assert sent[1]["image_url"]["url"] == IMAGE_URL
    assert sent[0] == {"type": "text", "text": "continuing apiproj work"}


@pytest.mark.asyncio
async def test_responses_multimodal_binds_with_text_evidence(tmp_path):
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    captured = []
    app = make_app(adapter)
    body = {"model": "test", "input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "continuing apiproj work"},
            {"type": "input_image", "image_url": IMAGE_URL}]}]}
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_agent_kwargs(captured)):
            resp = await cli.post("/v1/responses", json=body)
            assert resp.status == 200
            session_id = resp.headers["X-Hermes-Session-Id"]

    prompt = captured[0]["ephemeral_system_prompt"]
    assert prompt is not None
    assert "Active project: API project (api-proj)" in prompt
    assert IMAGE_URL not in prompt
    db = adapter._ensure_session_db()
    state, binding = load_wake_state_for_session(db, session_id)
    assert state == "bound"
    assert binding["project_id"] == "api-proj"
    assert IMAGE_URL not in db.get_session_wake_packet(session_id)
    # Canonicalized (input_* -> native) but multimodally intact.
    sent = captured[0]["user_message"]
    assert isinstance(sent, list) and sent[1]["type"] == "image_url"
    assert sent[1]["image_url"]["url"] == IMAGE_URL


@pytest.mark.asyncio
async def test_image_only_first_message_marks_none_without_leak(tmp_path):
    """An image-only first message with no store durably persists the
    attempted-none sentinel, and neither the URL nor the base64 payload
    ever enters the wake evidence, prompt, or durable record."""
    from gateway.continuity_wake import wake_state_from_json
    from gateway.platforms.api_server import (_derive_chat_session_id,
                                              _normalize_multimodal_content)
    adapter = make_adapter()
    captured = []
    content = [{"type": "image_url", "image_url": {"url": DATA_URL}}]
    body = {"model": "test",
            "messages": [{"role": "user", "content": content}]}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_agent_kwargs(captured)):
            assert (await cli.post("/v1/chat/completions",
                                   json=body)).status == 200

    prompt = captured[0]["ephemeral_system_prompt"]
    assert prompt is None or "[Continuity wake packet" not in prompt
    assert prompt is None or "QkFTRTY0TUFSS0VS" not in prompt
    db = adapter._ensure_session_db()
    session_id = _derive_chat_session_id(
        None, _normalize_multimodal_content(content))
    record = db.get_session_wake_packet(session_id)
    state, _ = wake_state_from_json(record)
    assert state == "none"
    assert "QkFTRTY0TUFSS0VS" not in record
    # The image itself still reaches the agent unchanged.
    assert captured[0]["user_message"] == _normalize_multimodal_content(content)


@pytest.mark.asyncio
async def test_image_url_text_never_activates_project(tmp_path):
    """A project alias appearing only inside an image URL is not evidence:
    the packet binds with no activated project (URLs/base64 are never
    stringified into the wake evidence)."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="sess-mm-2", source="api_server")
    captured = []
    content = [{"type": "image_url",
                "image_url": {"url": "https://example.com/apiproj/shot.png"}}]
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_agent_kwargs(captured)):
            assert (await cli.post("/api/sessions/sess-mm-2/chat",
                                   json={"message": content})).status == 200

    state, binding = load_wake_state_for_session(db, "sess-mm-2")
    assert state == "bound"
    assert binding["project_id"] is None
    assert "example.com" not in binding["text"]
    prompt = captured[0]["ephemeral_system_prompt"]
    assert prompt is not None and "Active project" not in prompt


@pytest.mark.asyncio
async def test_wake_lifecycle_fails_closed_on_unsupported_shapes(tmp_path):
    """Shapes the normalizer never emits (corrupt/nested/non-text) reaching
    the wake lifecycle produce empty evidence — no exception, no project
    activation — even with a live store and a matching alias inside."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    for i, weird in enumerate([
            [{"type": "text", "text": {"nested": "apiproj"}}],
            [["apiproj"]],
            object()]):
        session_id = f"sess-weird-{i}"
        db.create_session(session_id=session_id, source="api_server")
        prompt = await adapter._wake_ephemeral_prompt(
            session_id, "Base prompt", is_new_session=True,
            user_message=weird)
        assert prompt.startswith("Base prompt")
        state, binding = load_wake_state_for_session(db, session_id)
        assert state == "bound"
        assert binding["project_id"] is None
        assert "apiproj" not in binding["text"]


@pytest.mark.asyncio
async def test_multiplex_guard_failure_fails_closed(tmp_path):
    """If the multiplex guard itself raises, profile identity is unknown —
    wake binding must be disabled, not fall through to the ambient
    profile."""
    seed_store()
    adapter = make_adapter()
    captured = []

    async def _mock_run_agent(**kwargs):
        captured.append(kwargs.get("ephemeral_system_prompt"))
        return ({"final_response": "ok", "messages": [], "api_calls": 1},
                {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})

    body = {"model": "test",
            "messages": [{"role": "user", "content": "guard broken"}]}
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent), \
             patch("agent.secret_scope.is_multiplex_active",
                   side_effect=RuntimeError("guard exploded")):
            assert (await cli.post("/v1/chat/completions",
                                   json=body)).status == 200
    assert captured[0] is None or "[Continuity wake packet" not in captured[0]


# -- post-verification finding 4: evidence caps and non-string coercion ---------

def test_wake_evidence_scalar_string_is_capped():
    """Plain-string first messages ride the same hard evidence cap as
    part-list inputs — a scalar can be up to MAX_NORMALIZED_TEXT_LENGTH
    (64 KB), far past the evidence bound."""
    from gateway.platforms.api_server import (MAX_WAKE_EVIDENCE_LENGTH,
                                              _wake_evidence_text)
    huge = "x" * (MAX_WAKE_EVIDENCE_LENGTH * 3)
    out = _wake_evidence_text(huge)
    assert len(out) == MAX_WAKE_EVIDENCE_LENGTH
    # Short strings still pass through unchanged.
    assert _wake_evidence_text("continue kanban:board:42") \
        == "continue kanban:board:42"


def test_normalizer_rejects_top_level_non_string_content():
    """Re-review blocker 3: a TOP-LEVEL dict/number/bool content value fell
    through _normalize_multimodal_content's non-list fallback into
    _normalize_chat_content's str() coercion — its repr became canonical
    message content AND durable wake activation evidence. Reject loud,
    like the nested text-part case; None/str keep their passthrough."""
    from gateway.platforms.api_server import _normalize_multimodal_content
    for bad in ({"nested": "apiproj"}, 123, 4.5, True):
        with pytest.raises(ValueError) as exc:
            _normalize_multimodal_content(bad)
        assert str(exc.value).startswith("invalid_content_part:"), bad
    assert _normalize_multimodal_content(None) == ""
    assert _normalize_multimodal_content("hi") == "hi"


@pytest.mark.asyncio
async def test_top_level_non_string_content_rejected_before_wake_consumes(
        tmp_path):
    """Every endpoint that feeds the wake lifecycle rejects top-level
    dict/number/bool content with a 400 at the boundary: no agent run, no
    stringified repr as the user message, and no durable wake record built
    from it."""
    from gateway.continuity_wake import load_wake_state_for_session
    from gateway.platforms.api_server import _derive_chat_session_id
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    captured = []
    app = make_app(adapter)
    bad_contents = [{"nested": "apiproj"}, 123, True]
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            for bad in bad_contents:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "test",
                          "messages": [{"role": "user", "content": bad}]})
                assert resp.status == 400, bad
                resp = await cli.post(
                    "/v1/responses",
                    json={"input": [{"role": "user", "content": bad}]})
                assert resp.status == 400, bad
                resp = await cli.post(
                    "/v1/responses",
                    json={"input": "hello",
                          "conversation_history": [
                              {"role": "user", "content": bad}]})
                assert resp.status == 400, bad
    assert captured == []
    # The would-have-been coerced sessions never acquired wake state: the
    # old behavior derived a session id from the stringified content and
    # bound durable evidence under it.
    for bad in bad_contents:
        derived = _derive_chat_session_id(None, str(bad))
        assert load_wake_state_for_session(db, derived) == ("absent", None)
    """The normalizer must not stringify non-string text values: the repr
    of a dict/number would otherwise become canonical message content AND
    permanently durable wake activation evidence. Reject loud (400 at the
    endpoint), never coerce."""
    from gateway.platforms.api_server import _normalize_multimodal_content
    for bad in ({"nested": "apiproj"}, 123, ["list"], True):
        with pytest.raises(ValueError) as exc:
            _normalize_multimodal_content([{"type": "text", "text": bad}])
        assert str(exc.value).startswith("invalid_content_part:")
    # None text is still simply skipped (no part emitted).
    assert _normalize_multimodal_content([{"type": "text", "text": None}]) == ""


@pytest.mark.asyncio
async def test_non_string_text_part_rejected_before_wake_consumes(tmp_path):
    """A first message with a non-string text value is rejected at the
    boundary: no agent run, no coerced repr as activation evidence, and the
    session's one wake attempt is not consumed."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    session_id = "sess-coerce-1"
    db.create_session(session_id=session_id, source="api_server")
    captured = []
    app = make_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": [{"type": "text",
                                   "text": {"nested": "apiproj"}}]})
    assert resp.status == 400
    assert captured == []
    assert load_wake_state_for_session(db, session_id) == ("absent", None)


# -- re-review blocker 5: REST fork materializes the parent record --------------

@pytest.mark.asyncio
async def test_rest_fork_chain_materializes_wake_record(tmp_path):
    """POST /api/sessions/{id}/fork creates a parent-linked child; each
    fork must copy the parent chain's terminating wake record verbatim onto
    the child, so a 12-hop fork chain still resolves its binding instead of
    reaching terminal 'exhausted' past the ten-hop walk limit."""
    import hashlib
    import json as _json
    from gateway.continuity_wake import load_wake_state_for_session
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="fork-root", source="api_server")
    text = "forked packet"
    raw = _json.dumps({
        "schema_version": 1, "packet_id": "pkt-fork",
        "content_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "project_id": None, "text": text})
    db.set_session_wake_packet("fork-root", raw)

    app = make_app(adapter)
    app.router.add_post("/api/sessions/{session_id}/fork",
                        adapter._handle_fork_session)
    prev = "fork-root"
    async with TestClient(TestServer(app)) as cli:
        for i in range(1, 13):
            fork_id = f"fork-hop-{i}"
            resp = await cli.post(f"/api/sessions/{prev}/fork",
                                  json={"id": fork_id})
            assert resp.status == 201, (i, await resp.text())
            assert db.get_session_wake_packet(fork_id) == raw, i
            prev = fork_id

    state, binding = load_wake_state_for_session(db, prev)
    assert state == "bound" and binding["text"] == text


# -- post-verification finding 6: history-read failures are not attestations ----

@pytest.mark.asyncio
async def test_conversation_history_read_failure_is_flagged(tmp_path):
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    db.create_session(session_id="hist-unit-1", source="api_server")
    history, ok = await adapter._conversation_history_for_session("hist-unit-1")
    assert ok is True and history == []

    def _boom(*args, **kwargs):
        raise RuntimeError("disk error")

    original = db.get_messages_as_conversation
    db.get_messages_as_conversation = _boom
    try:
        history, ok = await adapter._conversation_history_for_session(
            "hist-unit-1"
        )
    finally:
        db.get_messages_as_conversation = original
    assert ok is False and history == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["chat", "stream"])
async def test_session_chat_history_read_failure_never_attests(tmp_path,
                                                               endpoint):
    """A failed history read must not become [] and falsely attest
    first-message eligibility for an existing transcript: no bind, no
    attempted-none, the one attempt survives for a healthy read."""
    import asyncio as _asyncio
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = make_adapter()
    db = adapter._ensure_session_db()
    session_id = f"sess-histfail-{endpoint}"
    db.create_session(session_id=session_id, source="api_server")
    db.append_message(session_id=session_id, role="user", content="old q")
    db.append_message(session_id=session_id, role="assistant",
                      content="old a")
    captured = []
    app = make_app(adapter)
    url = (f"/api/sessions/{session_id}/chat" if endpoint == "chat"
           else f"/api/sessions/{session_id}/chat/stream")

    def _boom(*args, **kwargs):
        raise RuntimeError("disk error")

    original = db.get_messages_as_conversation
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            db.get_messages_as_conversation = _boom
            try:
                resp = await cli.post(url,
                                      json={"message": "apiproj status"})
                await resp.read()
            finally:
                db.get_messages_as_conversation = original
    assert resp.status == 200
    assert captured
    assert "[Continuity wake packet" not in (captured[0] or "")
    assert load_wake_state_for_session(db, session_id) == ("absent", None)


@pytest.mark.asyncio
async def test_chat_completions_history_read_failure_never_attests(tmp_path):
    """Same contract on /v1/chat/completions session continuation
    (X-Hermes-Session-Id): a read failure is not an empty history."""
    from gateway.continuity_wake import load_wake_state_for_session
    seed_store()
    adapter = APIServerAdapter(PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": 0, "key": "secret-key"}))
    db = adapter._ensure_session_db()
    session_id = "sess-cc-histfail"
    db.create_session(session_id=session_id, source="api_server")
    db.append_message(session_id=session_id, role="user", content="old q")
    captured = []
    app = make_app(adapter)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk error")

    original = db.get_messages_as_conversation
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=capture_run_agent(captured)):
            db.get_messages_as_conversation = _boom
            try:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "test", "messages": [
                        {"role": "user", "content": "apiproj status"}]},
                    headers={"Authorization": "Bearer secret-key",
                             "X-Hermes-Session-Id": session_id})
            finally:
                db.get_messages_as_conversation = original
    assert resp.status == 200
    assert captured
    assert "[Continuity wake packet" not in (captured[0] or "")
    assert load_wake_state_for_session(db, session_id) == ("absent", None)


@pytest.fixture(autouse=True)
def stable_wake_clock(monkeypatch):
    # Project freshness is meaningful; lifecycle fixtures must not age with wall time.
    from datetime import datetime as real_datetime
    import gateway.continuity_wake as wake

    class FixtureClock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = real_datetime.fromisoformat(NOW_ISO)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(wake, "datetime", FixtureClock)

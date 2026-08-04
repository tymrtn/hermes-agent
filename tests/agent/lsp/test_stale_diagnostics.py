"""Regression tests for the "ghost diagnostics" staleness bug.

Scenario: the agent edits a TypeScript file, tsserver takes a long
time to re-check it, and the old diagnostics (for the PRE-edit
content) were reported as if they were current — the agent then
chases errors it already fixed.

The contract under test:

- ``wait_for_diagnostics`` must NOT be satisfied by diagnostics left
  over from a previous edit cycle; it returns True only when fresh
  (post-didChange) data arrived, False on timeout.
- ``diagnostics_for(fresh_only=True)`` must exclude stale stores.
- ``LSPService.get_diagnostics_sync`` must return [] ("no data")
  rather than the stale diagnostics when the server never re-checks
  within the wait budget, and must NOT mark the server broken.
- A slow-but-eventually-correct server ("slow_push") is waited on,
  honouring the configured ``lsp.wait_timeout``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agent.lsp.client import LSPClient


# These tests intentionally spawn and terminate their own mock language server.
# The canonical parallel wrapper can lose the exited child's ancestry before
# asyncio's transport delivers SIGTERM, causing the live-system guard to reject
# cleanup of that dedicated subprocess.
pytestmark = pytest.mark.live_system_guard_bypass


MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


def _client(workspace: Path, script: str, **env_extra: str) -> LSPClient:
    env = {
        "MOCK_LSP_SCRIPT": script,
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        **env_extra,
    }
    return LSPClient(
        server_id=f"mock-{script}",
        workspace_root=str(workspace),
        command=[sys.executable, MOCK_SERVER],
        env=env,
        cwd=str(workspace),
    )






@pytest.mark.asyncio
async def test_slow_push_is_waited_for(tmp_path: Path):
    """A server that re-checks slowly (but within budget) gets waited on,
    and the fresh (clean) result replaces the old error."""
    f = tmp_path / "x.py"
    f.write_text("bad code\n")

    client = _client(tmp_path, "slow_push", MOCK_LSP_PUSH_DELAY="0.8")
    await client.start()
    try:
        v0 = await client.open_file(str(f), language_id="python")
        assert await client.wait_for_diagnostics(str(f), v0, mode="document", timeout=2.0)
        assert len(client.diagnostics_for(str(f), fresh_only=True)) == 1

        f.write_text("good code\n")
        v1 = await client.open_file(str(f), language_id="python")
        fresh = await client.wait_for_diagnostics(str(f), v1, mode="document", timeout=5.0)
        assert fresh is True, "slow push within budget must satisfy the wait"
        assert client.diagnostics_for(str(f), fresh_only=True) == []
    finally:
        await client.shutdown()






@pytest.mark.asyncio
async def test_related_pull_uses_request_time_version_when_other_file_changes(
    tmp_path: Path,
):
    """A's in-flight pull must not bless stale related diagnostics for B."""
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"
    file_a.write_text("a = 1\n")
    file_b.write_text("b = 1\n")

    client = _client(tmp_path, "clean")
    await client.start()
    try:
        await client.open_file(str(file_a), language_id="python")
        await client.open_file(str(file_b), language_id="python")

        request_started = asyncio.Event()
        release_response = asyncio.Event()
        stale_related = {
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
            "severity": 1,
            "code": "RELATED_OLD_B",
            "message": "diagnostic for B before its edit",
        }

        async def delayed_pull(method, params, timeout):
            assert method == "textDocument/diagnostic"
            request_started.set()
            await release_response.wait()
            return {
                "kind": "full",
                "items": [],
                "relatedDocuments": {
                    client_path_uri: {"kind": "full", "items": [stale_related]},
                },
            }

        from agent.lsp.client import file_uri

        client_path_uri = file_uri(str(file_b))
        client._send_request_with_retry = delayed_pull
        pull = asyncio.create_task(client._pull_document_diagnostics(str(file_a)))
        await request_started.wait()

        file_b.write_text("b = 2\n")
        await client.open_file(str(file_b), language_id="python")
        release_response.set()
        await pull

        # The legacy merged view proves the related result was stored; the
        # fresh-only view must reject it because B advanced during A's pull.
        assert stale_related in client.diagnostics_for(str(file_b))
        assert stale_related not in client.diagnostics_for(
            str(file_b), fresh_only=True
        )
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_versionless_push_during_did_change_drain_remains_stale(tmp_path: Path):
    """A versionless push received while didChange drains is not trusted as new."""
    f = tmp_path / "x.py"
    f.write_text("old text\n")

    client = _client(tmp_path, "stale")
    await client.start()
    try:
        v0 = await client.open_file(str(f), language_id="python")
        assert await client.wait_for_diagnostics(str(f), v0, timeout=2.0)

        original_send = client._send_notification
        sent_change = None
        client._sync_kind = 2
        buffered_old = {
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 0, "character": 1},
            },
            "severity": 1,
            "code": "BUFFERED_OLD",
            "message": "buffered diagnostics for the previous text",
        }

        async def push_while_sending(method, params):
            nonlocal sent_change
            if method != "textDocument/didChange":
                await original_send(method, params)
                return

            sent_change = params
            # A server can flush diagnostics buffered for the old text while
            # the new didChange notification is still draining.
            await asyncio.sleep(0)
            client._handle_publish_diagnostics(
                {
                    "uri": params["textDocument"]["uri"],
                    "diagnostics": [buffered_old],
                }
            )

        client._send_notification = push_while_sending
        f.write_text("new text\n")
        v1 = await client.open_file(str(f), language_id="python")

        assert sent_change == {
            "textDocument": {"uri": sent_change["textDocument"]["uri"], "version": v1},
            "contentChanges": [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 1, "character": 0},
                    },
                    "text": "new text\n",
                }
            ],
        }
        doc = client._docs[os.path.abspath(str(f))]
        assert doc.text == "new text\n"
        assert doc.push_version == v0
        assert buffered_old in client.diagnostics_for(str(f))
        assert buffered_old not in client.diagnostics_for(str(f), fresh_only=True)
        assert not await client.wait_for_diagnostics(str(f), v1, timeout=0.05)
    finally:
        await client.shutdown()


# ---------------------------------------------------------------------------
# Service-level: stale data must surface as "no data", never as errors
# ---------------------------------------------------------------------------


def _install_mock_server(
    script: str,
    server_id: str = "pyright",
    *,
    seed_first_push: bool = False,
):
    """Replace one registered server with a wrapper spawning the mock.

    Mirrors the helper in test_service.py — reuse pyright so .py files
    route to the mock without a real toolchain.
    """
    from agent.lsp.servers import SERVERS, ServerContext, ServerDef, SpawnSpec

    target_index = next(i for i, s in enumerate(SERVERS) if s.server_id == server_id)
    original = SERVERS[target_index]

    def _spawn(root: str, ctx: ServerContext) -> SpawnSpec:
        return SpawnSpec(
            command=[sys.executable, MOCK_SERVER],
            workspace_root=root,
            cwd=root,
            env={"MOCK_LSP_SCRIPT": script},
            initialization_options={},
        )

    SERVERS[target_index] = ServerDef(
        server_id=server_id,
        extensions=original.extensions,
        resolve_root=lambda fp, ws: ws,
        build_spawn=_spawn,
        seed_first_push=seed_first_push,
        description="mock " + server_id,
    )
    return target_index, original


@pytest.fixture
def stale_repo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("")
    monkeypatch.chdir(str(repo))
    idx, original = _install_mock_server("stale")
    yield repo
    from agent.lsp.servers import SERVERS

    SERVERS[idx] = original


def test_service_reports_no_data_not_stale_errors(stale_repo):
    """When the server never re-checks the edited content in budget,
    get_diagnostics_sync must return [] and keep the server usable."""
    from agent.lsp.manager import LSPService

    f = stale_repo / "x.py"
    f.write_text("bad code\n")

    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
    )
    try:
        # First contact: didOpen gets the (real) pre-edit error push.
        first = svc.get_diagnostics_sync(str(f), delta=False)
        assert len(first) == 1

        # Edit the file — mock never re-publishes (slow tsserver model).
        f.write_text("good code\n")
        ghost = svc.get_diagnostics_sync(str(f), delta=False)
        assert ghost == [], "stale pre-edit error must not be reported as current"

        # Not marked broken: slow is not dead.
        assert svc.enabled_for(str(f))
        status = svc.get_status()
        assert status["broken"] == []
    finally:
        svc.shutdown()


def test_snapshot_baseline_accepts_only_initial_push_from_push_only_server(
    monkeypatch,
    tmp_path,
):
    """A TypeScript-style seeded first push is a usable fresh baseline."""
    from agent.lsp import client as client_module
    from agent.lsp.manager import LSPService

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("")
    source = repo / "baseline.py"
    source.write_text("bad code\n")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(client_module, "DIAGNOSTICS_DOCUMENT_WAIT", 0.2)

    idx, original = _install_mock_server("stale", seed_first_push=True)
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=0.2,
        install_strategy="manual",
    )
    try:
        svc.snapshot_baseline(str(source))
        baseline = svc._delta_baseline[os.path.abspath(str(source))]
        assert [diag["code"] for diag in baseline] == ["MOCK001"]
    finally:
        svc.shutdown()
        from agent.lsp.servers import SERVERS

        SERVERS[idx] = original

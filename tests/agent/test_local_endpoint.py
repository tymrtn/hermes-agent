"""Tests for agent.local_endpoint — start-on-demand for loopback endpoints.

No live network: the only sockets involved belong to health probes that are
either patched out or aimed at a closed loopback port (connection refused is
the answer under test).  The one real subprocess is a tiny script written
into tmp_path that touches a file, which stands in for a model server coming
up; the fake health probe reports "serving" once that file exists.
"""

import os
import stat
import threading

import pytest

from agent import local_endpoint
from agent.local_endpoint import (
    endpoint_health_url,
    ensure_local_endpoint,
    entry_start_command,
    is_local_endpoint_entry,
    is_loopback_url,
    is_startable_local_entry,
    resolve_start_timeout,
    select_local_fallback_entry,
    validate_start_command,
)


def _write_script(path, body):
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return str(path)


# ── loopback gate ────────────────────────────────────────────────────────


class TestIsLoopbackUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:18765/v1",
            "http://127.0.0.1:18765/v1/models",
            "http://localhost:8080/v1",
            "http://LOCALHOST:8080/v1",
            "http://[::1]:8080/v1",
            "http://127.5.5.5:8080/v1",
            "https://127.0.0.1/v1",
        ],
    )
    def test_loopback_urls_allowed(self, url):
        assert is_loopback_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.5:18765/v1",
            "http://example.com/v1",
            "https://api.openai.com/v1",
            "http://0.0.0.0:18765/v1",
            "http://169.254.169.254/latest",
            "",
            None,
            "not a url",
        ],
    )
    def test_non_loopback_urls_refused(self, url):
        assert is_loopback_url(url) is False

    def test_hostname_resolving_to_loopback_is_still_refused(self):
        """Only literal loopback names/IPs pass — DNS is attacker-influenceable
        and this gate decides whether a process may be spawned."""
        assert is_loopback_url("http://localtest.me:18765/v1") is False


# ── entry shape helpers ──────────────────────────────────────────────────


class TestEntryHelpers:
    def test_health_url_derived_from_base_url(self):
        entry = {"base_url": "http://127.0.0.1:18765/v1/"}
        assert endpoint_health_url(entry) == "http://127.0.0.1:18765/v1/models"

    def test_explicit_health_url_wins(self):
        entry = {
            "base_url": "http://127.0.0.1:18765/v1",
            "health_url": "http://127.0.0.1:18765/health",
        }
        assert endpoint_health_url(entry) == "http://127.0.0.1:18765/health"

    def test_health_url_empty_without_urls(self):
        assert endpoint_health_url({"provider": "custom"}) == ""
        assert endpoint_health_url(None) == ""

    def test_startable_requires_command_and_loopback(self):
        loopback = {"base_url": "http://127.0.0.1:18765/v1"}
        assert is_local_endpoint_entry(loopback) is True
        assert is_startable_local_entry(loopback) is False

        remote_with_command = {
            "base_url": "https://api.example.com/v1",
            "start_command": "/usr/bin/true",
        }
        assert is_local_endpoint_entry(remote_with_command) is False
        assert is_startable_local_entry(remote_with_command) is False

        startable = dict(loopback, start_command="/usr/bin/true")
        assert is_startable_local_entry(startable) is True
        assert entry_start_command(startable) == "/usr/bin/true"

    def test_select_prefers_startable_entry(self):
        remote = {"provider": "openai", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
        plain_local = {"provider": "custom", "model": "a", "base_url": "http://127.0.0.1:1234/v1"}
        startable = {
            "provider": "custom",
            "model": "b",
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": "/usr/bin/true",
        }
        assert select_local_fallback_entry([remote, plain_local, startable]) is startable
        assert select_local_fallback_entry([remote, plain_local]) is plain_local
        assert select_local_fallback_entry([remote]) is None
        assert select_local_fallback_entry([]) is None
        assert select_local_fallback_entry(None) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, local_endpoint.DEFAULT_START_TIMEOUT_S),
            ("nope", local_endpoint.DEFAULT_START_TIMEOUT_S),
            (30, 30.0),
            ("45", 45.0),
            (-5, 0.0),
            (99999, local_endpoint.MAX_START_TIMEOUT_S),
        ],
    )
    def test_start_timeout_bounds(self, raw, expected):
        assert resolve_start_timeout({"start_timeout": raw}) == expected


# ── start_command validation ─────────────────────────────────────────────


class TestValidateStartCommand:
    def test_absolute_executable_file_accepted(self, tmp_path):
        script = _write_script(tmp_path / "serve.command", "exit 0")
        assert validate_start_command(script) == ""

    def test_relative_path_refused(self):
        assert "absolute" in validate_start_command("serve.command")

    def test_missing_path_refused(self, tmp_path):
        assert "does not exist" in validate_start_command(str(tmp_path / "nope.command"))

    def test_non_executable_refused(self, tmp_path):
        path = tmp_path / "serve.command"
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert "not executable" in validate_start_command(str(path))

    def test_directory_refused(self, tmp_path):
        assert "does not exist" in validate_start_command(str(tmp_path))

    def test_empty_refused(self):
        assert validate_start_command("") != ""
        assert validate_start_command(None) != ""
        assert validate_start_command(["/usr/bin/true"]) != ""


# ── ensure_local_endpoint ────────────────────────────────────────────────


class TestEnsureLocalEndpoint:
    def test_healthy_endpoint_is_never_started(self, monkeypatch):
        """An answering server must not be launched a second time."""
        monkeypatch.setattr(local_endpoint, "endpoint_healthy", lambda *a, **k: True)

        def _boom(*args, **kwargs):
            raise AssertionError("must not spawn a process for a healthy endpoint")

        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _boom)

        result = ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": "/usr/bin/true",
        })
        assert result == {"started": False, "ready": True, "error": ""}

    def test_unhealthy_endpoint_runs_start_command(self, monkeypatch, tmp_path):
        """A real subprocess launch: the script touches a file, and the probe
        reports healthy only once that file exists."""
        flag = tmp_path / "serving"
        script = _write_script(tmp_path / "serve.command", f"touch {flag}")
        monkeypatch.setattr(local_endpoint, "HEALTH_POLL_INTERVAL_S", 0.02)
        monkeypatch.setattr(
            local_endpoint, "endpoint_healthy", lambda *a, **k: flag.exists()
        )

        result = ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": script,
            "start_timeout": 20,
        })

        assert result["started"] is True
        assert result["ready"] is True
        assert result["error"] == ""
        assert flag.exists()

    def test_start_command_is_not_run_through_a_shell(self, monkeypatch, tmp_path):
        """A shell one-liner in start_command is a path that does not exist —
        it is rejected, not interpreted."""
        marker = tmp_path / "pwned"
        script = _write_script(tmp_path / "serve.command", "exit 0")
        monkeypatch.setattr(local_endpoint, "endpoint_healthy", lambda *a, **k: False)

        result = ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": f"{script}; touch {marker}",
        })

        assert result["started"] is False
        assert result["ready"] is False
        assert "does not exist" in result["error"]
        assert not marker.exists()

    def test_start_command_arg_vector_is_the_bare_path(self, monkeypatch, tmp_path):
        """Popen receives ``[path]`` — no shell, no split-on-space, no args."""
        script = _write_script(tmp_path / "serve with space.command", "exit 0")
        monkeypatch.setattr(local_endpoint, "endpoint_healthy", lambda *a, **k: False)
        seen = {}

        class _FakeProc:
            pass

        def _fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _fake_popen)

        ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": script,
            "start_timeout": 0,
        })

        assert seen["argv"] == [script]
        assert seen["kwargs"]["start_new_session"] is True
        assert "shell" not in seen["kwargs"]

    def test_timeout_reports_error_and_stays_unready(self, monkeypatch, tmp_path):
        script = _write_script(tmp_path / "serve.command", "exit 0")
        monkeypatch.setattr(local_endpoint, "HEALTH_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(local_endpoint, "endpoint_healthy", lambda *a, **k: False)

        result = ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": script,
            "start_timeout": 0.05,
        })

        assert result["started"] is True
        assert result["ready"] is False
        assert "did not answer" in result["error"]

    def test_launch_failure_reports_error(self, monkeypatch, tmp_path):
        script = _write_script(tmp_path / "serve.command", "exit 0")
        monkeypatch.setattr(local_endpoint, "endpoint_healthy", lambda *a, **k: False)

        def _fail(*args, **kwargs):
            raise OSError("Exec format error")

        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _fail)

        result = ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": script,
        })
        assert result["started"] is False
        assert result["ready"] is False
        assert "Exec format error" in result["error"]

    def test_non_loopback_target_refuses_to_start(self, monkeypatch, tmp_path):
        script = _write_script(tmp_path / "serve.command", "exit 0")

        def _boom(*args, **kwargs):
            raise AssertionError("must not spawn a process for a remote endpoint")

        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _boom)

        result = ensure_local_endpoint({
            "base_url": "https://api.example.com/v1",
            "start_command": script,
        })
        assert result["ready"] is False
        assert "not loopback" in result["error"]

    def test_non_loopback_health_url_refuses_to_start(self, monkeypatch, tmp_path):
        """A loopback base_url cannot smuggle a remote health probe past the gate."""
        script = _write_script(tmp_path / "serve.command", "exit 0")

        def _boom(*args, **kwargs):
            raise AssertionError("must not spawn a process for a remote health URL")

        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _boom)

        result = ensure_local_endpoint({
            "base_url": "http://127.0.0.1:18765/v1",
            "health_url": "https://api.example.com/health",
            "start_command": script,
        })
        assert result["ready"] is False
        assert "not loopback" in result["error"]

    def test_down_endpoint_without_start_command_reports_why(self, monkeypatch):
        monkeypatch.setattr(local_endpoint, "endpoint_healthy", lambda *a, **k: False)
        result = ensure_local_endpoint({"base_url": "http://127.0.0.1:18765/v1"})
        assert result["ready"] is False
        assert "no start_command" in result["error"]

    def test_entry_without_urls_reports_why(self):
        result = ensure_local_endpoint({"provider": "custom", "model": "x"})
        assert result["ready"] is False
        assert "base_url" in result["error"]

    def test_non_mapping_entry_is_rejected(self):
        assert ensure_local_endpoint(None)["ready"] is False
        assert ensure_local_endpoint("http://127.0.0.1:18765/v1")["ready"] is False


class TestConcurrentStarts:
    """Two callers reaching a down endpoint together must start one server.

    Deterministic, not timing-based: a barrier holds both callers at the
    pre-lock health probe until each has seen "down", so the race the lock
    exists for happens on every run.
    """

    @staticmethod
    def _run(target):
        """Run *target* in a thread, re-raising whatever it raised."""
        box = {}

        def _body():
            try:
                box["result"] = target()
            except BaseException as exc:  # surfaced by _join below
                box["error"] = exc

        thread = threading.Thread(target=_body, daemon=True)
        thread.start()
        return thread, box

    @staticmethod
    def _join(thread, box, timeout=15):
        thread.join(timeout)
        assert not thread.is_alive(), "ensure_local_endpoint never returned"
        if "error" in box:
            raise box["error"]
        return box["result"]

    def test_concurrent_callers_start_the_endpoint_once(self, monkeypatch, tmp_path):
        script = _write_script(tmp_path / "serve.command", "exit 0")
        entry = {
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": script,
            "start_timeout": 5,
        }
        monkeypatch.setattr(local_endpoint, "HEALTH_POLL_INTERVAL_S", 0)

        both_probed = threading.Barrier(2, timeout=15)
        probed = threading.local()
        serving = threading.Event()
        starts = []

        def _healthy(url, timeout=None):
            if not getattr(probed, "done", False):
                # First probe of this caller: the one before the lock. Hold
                # both callers here so neither can take the lock until both
                # have decided the endpoint is down.
                probed.done = True
                both_probed.wait()
                return False
            return serving.is_set()

        def _fake_popen(argv, **kwargs):
            starts.append(argv)
            serving.set()
            return object()

        monkeypatch.setattr(local_endpoint, "endpoint_healthy", _healthy)
        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _fake_popen)

        first = self._run(lambda: ensure_local_endpoint(entry))
        second = self._run(lambda: ensure_local_endpoint(entry))
        results = [self._join(*first), self._join(*second)]

        assert starts == [[script]], "the endpoint was started more than once"
        assert [r["ready"] for r in results] == [True, True]
        assert [r["error"] for r in results] == ["", ""]
        # The loser re-probed under the lock, found the winner's server, and
        # reported no start of its own — that re-probe is what stops the
        # duplicate spawn.
        assert sorted(r["started"] for r in results) == [False, True]

    def test_a_slow_start_does_not_block_an_unrelated_endpoint(
        self, monkeypatch, tmp_path
    ):
        """The lock is per endpoint: a 90s start on one must not stall another."""
        slow_script = _write_script(tmp_path / "slow.command", "exit 0")
        fast_script = _write_script(tmp_path / "fast.command", "exit 0")
        slow_entry = {
            "base_url": "http://127.0.0.1:18765/v1",
            "start_command": slow_script,
            "start_timeout": 5,
        }
        fast_entry = {
            "base_url": "http://127.0.0.1:18766/v1",
            "start_command": fast_script,
            "start_timeout": 5,
        }
        monkeypatch.setattr(local_endpoint, "HEALTH_POLL_INTERVAL_S", 0)

        serving = set()
        in_slow_start = threading.Event()
        release_slow = threading.Event()

        def _healthy(url, timeout=None):
            return url in serving

        def _fake_popen(argv, **kwargs):
            if argv == [slow_script]:
                in_slow_start.set()
                assert release_slow.wait(15), "slow start was never released"
                serving.add(endpoint_health_url(slow_entry))
            else:
                serving.add(endpoint_health_url(fast_entry))
            return object()

        monkeypatch.setattr(local_endpoint, "endpoint_healthy", _healthy)
        monkeypatch.setattr(local_endpoint.subprocess, "Popen", _fake_popen)

        slow = self._run(lambda: ensure_local_endpoint(slow_entry))
        assert in_slow_start.wait(15), "the slow endpoint never began starting"

        fast_thread, fast_box = self._run(lambda: ensure_local_endpoint(fast_entry))
        fast_thread.join(10)
        blocked = fast_thread.is_alive()
        release_slow.set()

        assert not blocked, "an unrelated endpoint waited behind another's start"
        assert self._join(fast_thread, fast_box)["ready"] is True
        assert self._join(*slow)["ready"] is True


class TestEndpointHealthy:
    def test_remote_urls_are_never_probed(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("must not open a socket to a remote host")

        monkeypatch.setattr(local_endpoint.urllib.request, "urlopen", _boom)
        assert local_endpoint.endpoint_healthy("https://api.openai.com/v1/models") is False

    def test_closed_loopback_port_is_unhealthy(self):
        # Port 1 on loopback: nothing listens, connection refused.
        assert local_endpoint.endpoint_healthy("http://127.0.0.1:1/v1/models", timeout=0.5) is False

    def test_http_error_below_500_counts_as_serving(self, monkeypatch):
        """A 401 proves a server is listening and speaking HTTP."""
        import urllib.error

        def _raise_401(*args, **kwargs):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:18765/v1/models", 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(local_endpoint.urllib.request, "urlopen", _raise_401)
        assert local_endpoint.endpoint_healthy("http://127.0.0.1:18765/v1/models") is True

    def test_http_error_500_counts_as_down(self, monkeypatch):
        import urllib.error

        def _raise_502(*args, **kwargs):
            raise urllib.error.HTTPError(
                "http://127.0.0.1:18765/v1/models", 502, "Bad Gateway", {}, None
            )

        monkeypatch.setattr(local_endpoint.urllib.request, "urlopen", _raise_502)
        assert local_endpoint.endpoint_healthy("http://127.0.0.1:18765/v1/models") is False


def test_unknown_entry_keys_survive_config_normalization():
    """fallback_config must carry start_command / health_url / start_timeout
    through to the chain, or none of the above can ever run."""
    from hermes_cli.fallback_config import get_fallback_chain

    chain = get_fallback_chain({
        "fallback_providers": [
            {
                "provider": "custom",
                "model": "/models/local-model",
                "base_url": "http://127.0.0.1:18765/v1/",
                "api_mode": "chat_completions",
                "api_key": "local",
                "start_command": "/abs/path/serve.command",
                "health_url": "http://127.0.0.1:18765/v1/models",
                "start_timeout": 90,
            }
        ]
    })

    assert len(chain) == 1
    entry = chain[0]
    assert entry["start_command"] == "/abs/path/serve.command"
    assert entry["health_url"] == "http://127.0.0.1:18765/v1/models"
    assert entry["start_timeout"] == 90
    assert entry["base_url"] == "http://127.0.0.1:18765/v1"
    assert os.path.isabs(entry["start_command"])

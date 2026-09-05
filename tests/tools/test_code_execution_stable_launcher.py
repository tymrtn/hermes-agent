#!/usr/bin/env python3
"""Behavior tests for the stable per-profile execute_code launcher (issue #6037).

Local execute_code no longer spawns ``python <mkdtemp>/script.py`` with a fresh
path every call. It materializes ONE stable launcher under
``<HERMES_HOME>/helpers/<profile>.py`` and streams the program over stdin, so the
macOS TCC / OS-permission identity stays constant across calls.

These are behavior tests — they exercise path resolution, profile-derived
filenames, secure + idempotent + concurrent materialization, and (on POSIX) a
real local execute_code subprocess proving the code reaches stdin and reports a
stable ``__file__`` across runs. No source-text or catalog snapshots.

Run with:  scripts/run_tests.sh tests/tools/test_code_execution_stable_launcher.py
"""

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["TERMINAL_ENV"] = "local"

from tools.code_execution_tool import (  # noqa: E402
    execute_code,
    _LAUNCHER_SOURCE,
    _sanitize_helper_stem,
    _helper_launcher_stem,
    _resolve_helper_launcher_path,
    _materialize_launcher,
    SANDBOX_ALLOWED_TOOLS,
)


@pytest.fixture(autouse=True)
def _force_local_terminal(monkeypatch):
    """Keep execute_code on the local (UDS) path for every test in this file."""
    monkeypatch.setenv("TERMINAL_ENV", "local")


@contextlib.contextmanager
def throwaway_dir():
    """A throwaway directory for unittest.TestCase methods (no pytest tmp_path)."""
    d = tempfile.mkdtemp(prefix="hermes-launcher-test-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Filename stem: profile-derived, safe, with the hermes.py fallback
# ---------------------------------------------------------------------------

class TestHelperStem(unittest.TestCase):
    def test_sanitize_strips_unsafe_chars(self):
        self.assertEqual(_sanitize_helper_stem("Nagatha"), "nagatha")
        self.assertEqual(_sanitize_helper_stem("weird name!!"), "weird-name")
        self.assertEqual(_sanitize_helper_stem("../../etc/passwd"), "etc-passwd")
        self.assertEqual(_sanitize_helper_stem("--__leading"), "leading")
        # Length is capped so the filename can never blow up.
        self.assertLessEqual(len(_sanitize_helper_stem("a" * 200)), 64)

    def test_named_profile_uses_its_own_stem(self):
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="nagatha"):
            self.assertEqual(_helper_launcher_stem(), "nagatha")

    def test_default_and_custom_fall_back_to_hermes(self):
        for profile in ("default", "custom"):
            with patch("hermes_cli.profiles.get_active_profile_name", return_value=profile):
                self.assertEqual(_helper_launcher_stem(), "hermes")

    def test_profile_lookup_failure_falls_back_to_hermes(self):
        with patch("hermes_cli.profiles.get_active_profile_name",
                   side_effect=RuntimeError("boom")):
            self.assertEqual(_helper_launcher_stem(), "hermes")


# ---------------------------------------------------------------------------
# Path resolution + profile isolation
# ---------------------------------------------------------------------------

class TestResolveLauncherPath(unittest.TestCase):
    def test_path_is_under_active_home_helpers_dir(self):
        # _hermetic_environment already points HERMES_HOME at a tempdir.
        from hermes_constants import get_hermes_home
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            path = _resolve_helper_launcher_path()
        self.assertEqual(path.name, "hermes.py")
        self.assertEqual(path.parent, get_hermes_home() / "helpers")

    def test_named_profile_gets_its_own_filename(self):
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="skippy"):
            path = _resolve_helper_launcher_path()
        self.assertEqual(path.name, "skippy.py")

    def test_different_profiles_resolve_to_different_paths(self):
        """Profile isolation: two profiles under the same home never collide."""
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="nagatha"):
            nagatha = _resolve_helper_launcher_path()
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="skippy"):
            skippy = _resolve_helper_launcher_path()
        self.assertNotEqual(nagatha, skippy)
        self.assertEqual(nagatha.name, "nagatha.py")
        self.assertEqual(skippy.name, "skippy.py")

    def test_different_homes_isolate_launchers(self, ):
        """Two profile homes yield launchers in different directories."""
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"):
            with patch.dict(os.environ, {"HERMES_HOME": "/tmp/home-a"}):
                a = _resolve_helper_launcher_path()
            with patch.dict(os.environ, {"HERMES_HOME": "/tmp/home-b"}):
                b = _resolve_helper_launcher_path()
        self.assertNotEqual(a, b)
        self.assertEqual(a, Path("/tmp/home-a/helpers/hermes.py"))
        self.assertEqual(b, Path("/tmp/home-b/helpers/hermes.py"))


# ---------------------------------------------------------------------------
# Secure + idempotent materialization
# ---------------------------------------------------------------------------

class TestMaterializeLauncher(unittest.TestCase):
    def test_creates_launcher_with_expected_content(self):
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            _materialize_launcher(path)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), _LAUNCHER_SOURCE.encode("utf-8"))

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes not enforced on Windows")
    def test_secure_dir_and_file_modes(self):
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            _materialize_launcher(path)
            file_mode = stat.S_IMODE(os.stat(path).st_mode)
            dir_mode = stat.S_IMODE(os.stat(path.parent).st_mode)
            self.assertEqual(file_mode, 0o600, f"file mode {oct(file_mode)}")
            self.assertEqual(dir_mode, 0o700, f"dir mode {oct(dir_mode)}")

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes not enforced on Windows")
    def test_existing_launcher_permissions_are_repaired_without_rewrite(self):
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            _materialize_launcher(path)
            os.chmod(path.parent, 0o755)
            os.chmod(path, 0o644)
            before = os.stat(path)

            _materialize_launcher(path)

            after = os.stat(path)
            self.assertEqual(before.st_ino, after.st_ino)
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)

    @unittest.skipIf(sys.platform == "win32", "symlink behavior differs on Windows")
    def test_exact_content_symlink_is_replaced_by_regular_file(self):
        with throwaway_dir() as tmp:
            target = tmp / "target.py"
            target.write_text(_LAUNCHER_SOURCE, encoding="utf-8")
            path = tmp / "helpers" / "hermes.py"
            path.parent.mkdir(parents=True)
            path.symlink_to(target)

            _materialize_launcher(path)

            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_text(encoding="utf-8"), _LAUNCHER_SOURCE)
            self.assertEqual(target.read_text(encoding="utf-8"), _LAUNCHER_SOURCE)

    def test_idempotent_does_not_rewrite_unchanged_bytes(self):
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            _materialize_launcher(path)
            before = os.stat(path)
            _materialize_launcher(path)  # unchanged — must be a no-op
            after = os.stat(path)
            self.assertEqual(before.st_ino, after.st_ino, "launcher was replaced")
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns, "launcher was rewritten")

    def test_refreshes_when_content_drifts(self):
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            path.parent.mkdir(parents=True)
            path.write_text("# stale launcher from an older version\n", encoding="utf-8")
            _materialize_launcher(path)
            self.assertEqual(path.read_bytes(), _LAUNCHER_SOURCE.encode("utf-8"))

    def test_no_stray_temp_files_left_behind(self):
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            _materialize_launcher(path)
            leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


class TestConcurrentMaterialize(unittest.TestCase):
    def test_concurrent_materialization_is_safe(self):
        """Many processes/threads share one launcher path; the file must always
        end up complete and valid, and no writer may raise."""
        with throwaway_dir() as tmp:
            path = tmp / "helpers" / "hermes.py"
            n = 24
            start = threading.Barrier(n)
            errors: list = []

            def worker():
                try:
                    start.wait()
                    _materialize_launcher(path)
                except Exception as exc:  # noqa: BLE001 - surfaced via assert
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [], f"materialization raised under concurrency: {errors}")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), _LAUNCHER_SOURCE.encode("utf-8"))
            leftovers = [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


# ---------------------------------------------------------------------------
# Real local execute_code subprocess (E2E)
# ---------------------------------------------------------------------------

class TestExecuteCodeStableLauncherE2E(unittest.TestCase):
    """Drive the real child subprocess — no mocks in the exec path."""

    def setUp(self):
        from tools.code_kernel import shutdown_all_kernels
        shutdown_all_kernels()
        self.addCleanup(shutdown_all_kernels)

    def _run(self, code):
        raw = execute_code(
            code=code,
            task_id="test-stable-launcher",
            enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
        )
        return json.loads(raw)

    def test_code_reaches_stdin_and_file_is_stable_across_runs(self):
        code = "print('SENTINEL', __file__)"

        first = self._run(code)
        second = self._run(code)

        self.assertEqual(first["status"], "success", msg=first)
        self.assertEqual(second["status"], "success", msg=second)

        # The program only runs (and prints) if its bytes reached the launcher's
        # stdin — nothing else feeds SENTINEL to stdout.
        self.assertIn("SENTINEL", first["output"])
        file1 = first["output"].split("SENTINEL", 1)[1].strip()
        file2 = second["output"].split("SENTINEL", 1)[1].strip()

        # Stable identity: both runs report the *same* launcher path.
        self.assertEqual(os.path.realpath(file1), os.path.realpath(file2))

        expected = _resolve_helper_launcher_path()
        self.assertEqual(os.path.realpath(file1), os.path.realpath(str(expected)))
        self.assertEqual(Path(file1).name, "hermes.py")
        self.assertEqual(Path(file1).parent.name, "helpers")

    def test_read_only_home_falls_back_to_invocation_local_script(self):
        """A launcher write failure must not make local execute_code unusable."""
        with patch(
            "tools.code_execution_tool._materialize_launcher",
            side_effect=PermissionError("read-only HERMES_HOME"),
        ):
            result = self._run("print('FALLBACK_OK', __file__)")

        self.assertEqual(result["status"], "success", msg=result)
        self.assertIn("FALLBACK_OK", result["output"])
        self.assertIn("script.py", result["output"])

    def test_launcher_persists_and_is_not_rewritten_between_runs(self):
        expected = _resolve_helper_launcher_path()

        self._run("print('one')")
        self.assertTrue(expected.exists())
        self.assertEqual(expected.read_bytes(), _LAUNCHER_SOURCE.encode("utf-8"))
        ino_before = os.stat(expected).st_ino
        mtime_before = os.stat(expected).st_mtime_ns

        self._run("print('two')")
        self.assertEqual(os.stat(expected).st_ino, ino_before, "launcher replaced")
        self.assertEqual(os.stat(expected).st_mtime_ns, mtime_before, "launcher rewritten")

    def test_launcher_dir_not_on_syspath_and_tools_still_import(self):
        # The launcher strips its own directory from sys.path so a launcher named
        # hermes.py can't shadow real modules; hermes_tools still resolves from
        # the tmpdir on PYTHONPATH.
        code = (
            "import os, sys\n"
            "from hermes_tools import terminal\n"
            "print('SELFDIR_ON_PATH', os.path.dirname(__file__) in sys.path)\n"
            "print('TOOLS_OK', callable(terminal))\n"
        )
        result = self._run(code)
        self.assertEqual(result["status"], "success", msg=result)
        self.assertIn("SELFDIR_ON_PATH False", result["output"])
        self.assertIn("TOOLS_OK True", result["output"])

    def test_traceback_reports_user_source_lines(self):
        # The launcher registers the stdin program in linecache and formats
        # uncaught errors via the traceback module, so tracebacks show the user's
        # real source line — and only user frames, not the launcher wrapper.
        code = "def f():\n    raise ValueError('kaboom')\nf()\n"
        result = self._run(code)
        self.assertEqual(result["status"], "error", msg=result)
        blob = result.get("error", "") + result.get("output", "")
        self.assertIn("ValueError", blob)
        self.assertIn("kaboom", blob)
        self.assertIn("raise ValueError('kaboom')", blob,
                      "traceback should show the offending source line")
        self.assertIn("<cell>", blob,
                      "user frames should carry the synthetic <execute_code> name")
        self.assertNotIn("exec(compile", blob,
                         "launcher wrapper frame must be hidden from the traceback")

    def test_large_program_does_not_deadlock_on_stdin(self):
        # A program larger than a typical OS pipe buffer (64KB) proves the
        # dedicated stdin-writer thread can't deadlock the parent.
        filler = "\n".join(f"_v{i} = {i}" for i in range(20000))  # ~180KB of source
        code = filler + "\nprint('BIGDONE', _v19999)\n"
        result = self._run(code)
        self.assertEqual(result["status"], "success", msg=result.get("error", result))
        self.assertIn("BIGDONE 19999", result["output"])

    def test_spawn_multiprocessing_reconstructs_submitted_main(self):
        """Spawn workers must reload user code through the stable launcher.

        macOS and Windows use spawn by default.  The child re-runs the stable
        launcher with no submitted stdin, so it must recover the invocation's
        source sidecar and preserve ``__mp_main__`` rather than recursively
        entering the user's ``if __name__ == '__main__'`` block.
        """
        code = (
            "import multiprocessing as mp\n"
            "def square(value):\n"
            "    return value * value\n"
            "if __name__ == '__main__':\n"
            "    ctx = mp.get_context('spawn')\n"
            "    with ctx.Pool(1) as pool:\n"
            "        print('SPAWN_OK', pool.map(square, [3, 5]))\n"
        )
        result = self._run(code)
        self.assertEqual(result["status"], "success", msg=result)
        self.assertIn("SPAWN_OK [9, 25]", result["output"])


if __name__ == "__main__":
    unittest.main()

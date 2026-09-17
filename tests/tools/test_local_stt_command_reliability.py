"""Two ways a local Whisper run used to fail silently.

A gateway started by launchd/systemd inherits a minimal PATH. Whisper resolves by absolute path,
then shells out to a bare ``ffmpeg`` that is not there, skips the input and exits 0 — no transcript,
no error. And some successful invocations print the whole timestamped transcript instead of leaving
the ``.txt`` artifact the local-command contract reads.
"""

import os
import subprocess

import pytest

from tools import transcription_local as tl


class TestWhisperStdoutRecovery:

    def test_a_clean_timestamped_transcript_is_accepted(self):
        stdout = ("[00:00.000 --> 00:02.000]  first sentence\n"
                  "[00:02.000 --> 00:04.000]  second sentence\n")
        assert tl._extract_whisper_stdout_transcript(stdout) == "first sentence\nsecond sentence"

    def test_the_hour_form_is_accepted_too(self):
        stdout = "[01:00:00.000 --> 01:00:02.000]  an hour in\n"
        assert tl._extract_whisper_stdout_transcript(stdout) == "an hour in"

    def test_one_non_segment_line_rejects_the_whole_output(self):
        """A warning or a progress bar must never be handed back as user speech."""
        stdout = ("Warning: ffmpeg not found on PATH\n"
                  "[00:00.000 --> 00:02.000]  first sentence\n")
        assert tl._extract_whisper_stdout_transcript(stdout) == ""

    @pytest.mark.parametrize("stdout", ["", "   \n\n  ", None])
    def test_nothing_in_means_nothing_out(self, stdout):
        assert tl._extract_whisper_stdout_transcript(stdout) == ""

    def test_blank_segments_are_dropped_without_rejecting_the_run(self):
        stdout = ("[00:00.000 --> 00:02.000]  \n"
                  "[00:02.000 --> 00:04.000]  only this\n")
        assert tl._extract_whisper_stdout_transcript(stdout) == "only this"


class TestLocalCommandRun:

    @pytest.fixture()
    def run_local(self, monkeypatch, tmp_path):
        """Drive _transcribe_local_command with a stubbed child process; returns (result, seen)."""
        seen = {}

        def _factory(*, stdout="", write_txt=None):
            monkeypatch.setattr(tl, "_get_local_command_template",
                                lambda: "whisper {input_path} --model {model} "
                                        "--output_dir {output_dir} --language {language}")
            monkeypatch.setattr(tl, "_resolve_stt_language", lambda _scope: "en", raising=False)
            monkeypatch.setattr(tl, "_prepare_local_audio",
                                lambda path, out_dir: (str(path), None))

            def _fake_run(command, *, timeout, env=None):
                seen["env"] = env
                seen["command"] = command
                if write_txt is not None:
                    out_dir = command[command.index("--output_dir") + 1]
                    (tmp_path / "x").write_text("")  # keep tmp_path referenced
                    open(os.path.join(out_dir, "out.txt"), "w", encoding="utf-8").write(write_txt)
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            monkeypatch.setattr(tl, "_run_quiet", _fake_run)
            return tl._transcribe_local_command(str(tmp_path / "audio.wav"), "base"), seen

        return _factory

    def test_the_common_local_bin_roots_are_prepended_to_the_child_path(self, run_local, monkeypatch):
        monkeypatch.setattr(tl, "COMMON_LOCAL_BIN_DIRS", ("/opt/homebrew/bin", "/usr/local/bin"))
        monkeypatch.setattr("tools.environments.local.hermes_subprocess_env",
                            lambda **kw: {"PATH": "/usr/bin:/bin"})

        _result, seen = run_local(write_txt="hello")

        assert seen["env"]["PATH"].split(os.pathsep) == [
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]

    def test_a_root_already_on_path_is_not_duplicated(self, run_local, monkeypatch):
        monkeypatch.setattr(tl, "COMMON_LOCAL_BIN_DIRS", ("/opt/homebrew/bin", "/usr/local/bin"))
        monkeypatch.setattr("tools.environments.local.hermes_subprocess_env",
                            lambda **kw: {"PATH": "/usr/local/bin:/bin"})

        _result, seen = run_local(write_txt="hello")

        assert seen["env"]["PATH"].split(os.pathsep) == ["/opt/homebrew/bin", "/usr/local/bin", "/bin"]

    def test_the_txt_artifact_still_wins_when_present(self, run_local, monkeypatch):
        monkeypatch.setattr("tools.environments.local.hermes_subprocess_env", lambda **kw: {"PATH": ""})

        result, _seen = run_local(stdout="[00:00.000 --> 00:02.000]  from stdout", write_txt="from the file")

        assert result["transcript"] == "from the file"

    def test_a_missing_artifact_falls_back_to_a_clean_stdout_transcript(self, run_local, monkeypatch):
        monkeypatch.setattr("tools.environments.local.hermes_subprocess_env", lambda **kw: {"PATH": ""})

        result = run_local(stdout="[00:00.000 --> 00:02.000]  recovered\n")[0]

        assert result["success"] is True
        assert result["transcript"] == "recovered"

    def test_a_missing_artifact_with_noisy_stdout_still_reports_the_failure(self, run_local, monkeypatch):
        monkeypatch.setattr("tools.environments.local.hermes_subprocess_env", lambda **kw: {"PATH": ""})

        result = run_local(stdout="whisper: skipping input, ffmpeg unavailable\n")[0]

        assert result.get("success") is not True
        assert "did not produce a .txt transcript" in result["error"]

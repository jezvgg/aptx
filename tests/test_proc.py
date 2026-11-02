"""proc: logging, helper commands, executability probes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

from aptx import proc
from aptx.proc import human_size
from aptx.errors import UnavailableError


@pytest.fixture(autouse=True)
def reset_verbose():
    proc.set_verbose(0)
    yield
    proc.set_verbose(0)


class TestVerbosity:
    def test_level_roundtrip(self):
        assert proc.verbose_level() == 0
        proc.set_verbose(2)
        assert proc.verbose_level() == 2

    def test_log_is_silent_by_default(self, capsys):
        proc.log("hidden")
        assert capsys.readouterr().err == ""

    def test_log_prints_at_level(self, capsys):
        proc.set_verbose(1)
        proc.log("shown")
        assert capsys.readouterr().err == "aptx: shown\n"

    def test_log_respects_higher_levels(self, capsys):
        proc.set_verbose(1)
        proc.log("deep", level=2)
        assert capsys.readouterr().err == ""
        proc.set_verbose(2)
        proc.log("deep", level=2)
        assert capsys.readouterr().err == "aptx: deep\n"


class TestMessages:
    def test_warn_prefixes_and_flushes(self, capsys):
        proc.warn("careful")
        captured = capsys.readouterr()
        assert captured.err == "aptx: warning: careful\n"
        assert captured.out == ""

    def test_note_prefixes(self, capsys):
        proc.note("cowsay is installed")
        assert capsys.readouterr().err == "aptx: note: cowsay is installed\n"


class TestRun:
    def test_captures_output(self):
        cp = proc.run(["sh", "-c", "echo hi"])
        assert cp.returncode == 0
        assert cp.stdout.strip() == "hi"

    def test_does_not_raise_without_check(self):
        cp = proc.run(["sh", "-c", "exit 3"])
        assert cp.returncode == 3

    def test_check_raises_with_last_stderr_line(self):
        with pytest.raises(UnavailableError, match="boom"):
            proc.run(["sh", "-c", "echo boom >&2; exit 4"], check=True)

    def test_check_falls_back_to_exit_code(self):
        with pytest.raises(UnavailableError, match="exit 5"):
            proc.run(["sh", "-c", "exit 5"], check=True)

    def test_check_passes_when_successful(self):
        cp = proc.run(["sh", "-c", "true"], check=True)
        assert cp.returncode == 0

    def test_missing_command_becomes_aptx_error(self):
        with pytest.raises(UnavailableError, match="required command not found"):
            proc.run(["aptx-command-that-does-not-exist-xyz"])

    def test_logs_the_command_at_high_verbosity(self, capsys):
        proc.set_verbose(2)
        proc.run(["sh", "-c", "true"])
        assert "exec: sh -c true" in capsys.readouterr().err

    def test_forwards_cwd_and_env(self, tmp_path: Path):
        cp = proc.run(
            ["sh", "-c", "echo $APTX_TEST_VAR; pwd"],
            cwd=str(tmp_path),
            env={"APTX_TEST_VAR": "value", "PATH": "/usr/bin:/bin"},
        )
        out = cp.stdout.split()
        assert out[0] == "value"
        assert Path(out[1]) == tmp_path.resolve()

    def test_forwards_stdin(self):
        cp = proc.run(["cat"], input_text="payload")
        assert cp.stdout == "payload"

    def test_accepts_non_string_arguments(self):
        cp = proc.run(["sh", "-c", "echo $0", str(Path("/tmp"))])
        assert cp.returncode == 0


class TestWhich:
    def test_finds_a_real_tool(self):
        assert proc.which("sh") is not None

    def test_returns_none_for_garbage(self):
        assert proc.which("aptx-definitely-not-a-command") is None


class TestIsExecutable:
    def test_true_for_an_executable_file(self, tmp_path: Path):
        target = tmp_path / "tool"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
        assert proc.is_executable(target) is True

    def test_false_for_a_plain_file(self, tmp_path: Path):
        target = tmp_path / "data.txt"
        target.write_text("x", encoding="utf-8")
        target.chmod(0o644)
        assert proc.is_executable(target) is False

    def test_false_for_a_directory(self, tmp_path: Path):
        assert proc.is_executable(tmp_path) is False

    def test_false_for_a_missing_path(self, tmp_path: Path):
        assert proc.is_executable(tmp_path / "nope") is False

    def test_false_when_the_kernel_refuses(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "tool"
        target.write_text("x", encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("refused")

        monkeypatch.setattr(proc.os, "access", boom)
        assert proc.is_executable(target) is False


class TestStreams:
    def test_messages_go_to_stderr(self, capsys):
        proc.note("x")
        assert capsys.readouterr().out == ""
        assert sys.stderr is not None


class TestHumanSize:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (0, "0B"),
            (1023, "1023B"),
            (1024, "1.0K"),
            (1536, "1.5K"),
            (1024**2, "1.0M"),
            (1024**3, "1.0G"),
            (1024**4, "1.0T"),
            (1024**5, "1024.0T"),
        ],
    )
    def test_units(self, value, expected):
        assert human_size(value) == expected

    def test_non_numeric_is_echoed(self):
        assert human_size(cast(int, "n/a")) == "n/a"

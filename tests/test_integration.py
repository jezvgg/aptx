"""End-to-end tests: real apt, real dpkg, real sandbox.

Skipped unless ``-m integration`` is selected; they download packages and need
bubblewrap.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


def aptx(*args: str, env: dict[str, str] | None = None, stdin: str | None = None):
    merged = dict(os.environ)
    merged["PYTHONPATH"] = str(REPO_ROOT / "src")
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "aptx", *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=merged,
        check=False,
        timeout=600,
    )


@pytest.fixture(scope="session")
def cache(tmp_path_factory) -> dict[str, str]:
    """Isolate the whole integration run from the developer's real cache."""
    root = tmp_path_factory.mktemp("aptx-cache")
    return {"APTX_CACHE": str(root / "cache"), "APTX_BIN": str(root / "bin")}


@pytest.fixture(scope="session")
def stage_cowsay(cache):
    """Stage cowsay once for the whole session."""
    result = aptx("run", "--dry-run", "cowsay", "hi", env=cache)
    if result.returncode != 0:
        pytest.skip(f"cannot stage cowsay: {result.stderr.strip()[:200]}")
    return cache


@pytest.fixture
def staged(stage_cowsay):
    """Guarantee the package is present even after TestInstall removed it."""
    result = aptx("run", "--dry-run", "cowsay", "hi", env=stage_cowsay)
    assert result.returncode == 0, result.stderr
    return stage_cowsay


class TestDoctor:
    def test_reports_a_working_environment(self, cache):
        result = aptx("doctor", env=cache)
        assert "bwrap" in result.stdout
        assert result.returncode == 0, result.stdout + result.stderr


class TestRun:
    def test_prints_the_cow(self, staged):
        result = aptx("run", "cowsay", "hi", env=staged)
        assert result.returncode == 0, result.stderr
        assert "< hi >" in result.stdout
        assert "^__^" in result.stdout

    def test_package_named_binary_is_implicit(self, staged):
        """cowsay ships cowsay, cowthink and cowsay_random; cowsay is the default."""
        result = aptx("run", "cowsay", "hello", env=staged)
        assert result.returncode == 0, result.stderr
        assert "hello" in result.stdout

    def test_explicit_binary(self, staged):
        result = aptx("run", "cowsay", "cowthink", "hmm", env=staged)
        assert result.returncode == 0, result.stderr
        assert "hmm" in result.stdout

    def test_live_paths_resolve(self, staged):
        """`-f tux` reads /usr/share/cowsay/cows/tux.cow through the farm."""
        result = aptx("run", "cowsay", "-f", "tux", "hi", env=staged)
        assert result.returncode == 0, result.stderr
        assert "o_o" in result.stdout

    def test_home_is_not_touched(self, staged, tmp_path: Path):
        marker = tmp_path / "marker"
        script = f"touch {marker}; echo HOME=$HOME"
        result = aptx("shell", "cowsay", "--", "-c", script, env=staged)
        assert result.returncode == 0, result.stderr
        assert not marker.exists(), "the sandbox wrote outside its own $HOME"
        assert "HOME=/home/" in result.stdout

    def test_exit_status_propagates(self, staged):
        result = aptx("shell", "cowsay", "--", "-c", "exit 7", env=staged)
        assert result.returncode == 7

    def test_cwd_is_read_only_by_default(self, staged, tmp_path: Path):
        target = tmp_path / "out.txt"
        result = aptx("shell", "cowsay", "--", "-c", f"echo x > {target}", env=staged)
        assert result.returncode != 0
        assert not target.exists()

    def test_offline_blocks_the_network(self, staged):
        """Only loopback exists inside --offline, so there is nothing to talk to."""
        result = aptx(
            "shell", "--offline", "cowsay", "--", "-c",
            "cat /proc/net/dev 2>/dev/null | wc -l", env=staged,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() in ("2", "3")


class TestPlan:
    def test_lists_merge_points(self, staged):
        result = aptx("plan", "cowsay", env=staged)
        assert result.returncode == 0, result.stderr
        assert "merge points" in result.stdout
        assert "/usr/games" in result.stdout

    def test_does_not_bind_the_whole_usr_tree(self, staged):
        result = aptx("run", "--dry-run", "cowsay", "hi", env=staged)
        line = result.stdout
        store = staged["APTX_CACHE"]
        assert f"--ro-bind {store}/store/cowsay/usr /usr" not in line


class TestInstall:
    def test_shim_roundtrip(self, staged):
        installed = aptx("install", "cowsay", env=staged)
        assert installed.returncode == 0, installed.stderr
        shim = Path(staged["APTX_BIN"]) / "cowsay"
        assert shim.exists()
        assert os.access(shim, os.X_OK)

        run = subprocess.run([str(shim), "hi"], capture_output=True, text=True, check=False)
        assert "< hi >" in run.stdout

        removed = aptx("remove", "cowsay", env=staged)
        assert removed.returncode == 0, removed.stderr
        assert not shim.exists()


class TestList:
    def test_lists_the_staged_package(self, staged):
        result = aptx("list", env=staged)
        assert "cowsay" in result.stdout

    def test_ls_shows_executables(self, staged):
        result = aptx("ls", "cowsay", env=staged)
        assert "/usr/games/cowsay" in result.stdout

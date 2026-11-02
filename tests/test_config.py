"""config: environment-driven paths and the ephemeral override."""

from __future__ import annotations

from pathlib import Path

import pytest

from aptx import config
from aptx.errors import EnvError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("APTX_CACHE", raising=False)
    monkeypatch.delenv("APTX_BIN", raising=False)
    config.set_cache_override(None)
    yield
    config.set_cache_override(None)


class TestDefaults:
    def test_cache_dir_defaults_under_home(self):
        assert config.cache_dir() == Path.home() / ".cache" / "aptx"

    def test_bin_dir_defaults_to_local_bin(self):
        assert config.bin_dir() == Path.home() / ".local" / "bin"

    def test_store_lives_under_the_cache(self):
        assert config.store_dir() == config.cache_dir() / "store"

    def test_pins_live_under_the_cache(self):
        assert config.pins_file() == config.cache_dir() / "pins.json"


class TestEnvironment:
    def test_cache_env_is_honoured(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("APTX_CACHE", str(tmp_path / "cache"))
        assert config.cache_dir() == tmp_path / "cache"

    def test_bin_env_is_honoured(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        assert config.bin_dir() == tmp_path / "bin"

    def test_tilde_is_expanded(self, monkeypatch):
        monkeypatch.setenv("APTX_CACHE", "~/custom-aptx")
        assert config.cache_dir() == Path.home() / "custom-aptx"

    def test_empty_env_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("APTX_CACHE", "")
        assert config.cache_dir() == Path.home() / ".cache" / "aptx"


class TestOverride:
    def test_override_replaces_the_cache(self, tmp_path: Path):
        config.set_cache_override(tmp_path)
        assert config.cache_dir() == tmp_path

    def test_override_wins_over_the_environment(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("APTX_CACHE", str(tmp_path / "env"))
        config.set_cache_override(tmp_path / "override")
        assert config.cache_dir() == tmp_path / "override"

    def test_store_and_pins_follow_the_override(self, tmp_path: Path):
        config.set_cache_override(tmp_path)
        assert config.store_dir() == tmp_path / "store"
        assert config.pins_file() == tmp_path / "pins.json"

    def test_clearing_restores_the_default(self, tmp_path: Path):
        config.set_cache_override(tmp_path)
        config.set_cache_override(None)
        assert config.cache_dir() == Path.home() / ".cache" / "aptx"


class TestConstants:
    def test_sandbox_paths_are_absolute(self):
        assert config.SANDBOX_ROOT.startswith("/")
        assert config.SANDBOX_HOST.startswith("/")

    def test_default_path_contains_games(self):
        assert "/usr/games" in config.DEFAULT_PATH.split(":")

    def test_tmp_is_a_tmpfs_dir(self):
        assert "/tmp" in config.TMPFS_DIRS

    def test_gpu_devices_split_by_requirement(self):
        assert "/dev/dri" in config.GPU_HARD_BINDS
        assert "/dev/kfd" in config.GPU_SOFT_BINDS
        assert not set(config.GPU_HARD_BINDS) & set(config.GPU_SOFT_BINDS)

    def test_apt_relations(self):
        assert config.APT_RELATIONS_WANTED == {"Depends", "PreDepends"}

    def test_python_dist_regex_matches(self):
        import re

        assert re.match(config.PYTHON_DIST_RE, "/usr/lib/python3/dist-packages/x")
        assert re.match(config.PYTHON_DIST_RE, "/usr/local/lib/python3.12/site-packages")
        assert not re.match(config.PYTHON_DIST_RE, "/usr/share/x")


class TestMissingHome:
    def test_missing_home_without_overrides_is_an_env_error(self, monkeypatch):
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("APTX_CACHE", raising=False)
        monkeypatch.delenv("APTX_BIN", raising=False)
        with pytest.raises(EnvError, match=r"\$HOME is not set"):
            config.cache_dir()

    def test_missing_home_with_explicit_cache_still_works(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setenv("APTX_CACHE", str(tmp_path / "cache"))
        assert config.cache_dir() == tmp_path / "cache"

    def test_missing_home_with_explicit_bin_still_works(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        assert config.bin_dir() == tmp_path / "bin"


"""Shims in ~/.local/bin."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aptx import shim
from aptx.errors import CantCreateError, UsageError
from tests.conftest import make_entry


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "bin"
    target.mkdir()
    monkeypatch.setenv("APTX_BIN", str(target))
    return target


class TestShimText:
    def test_reenters_aptx_instead_of_symlinking_the_binary(self, tmp_path: Path):
        entry = make_entry(tmp_path)
        text = shim.shim_text(entry, "/usr/games/cowsay")
        assert text.startswith("#!/bin/sh\n")
        assert "run cowsay /usr/games/cowsay --" in text
        assert '"$@"' in text

    def test_is_marked_as_managed(self, tmp_path: Path):
        entry = make_entry(tmp_path)
        assert shim.MARKER in shim.shim_text(entry, "/usr/games/cowsay")


class TestInstall:
    def test_writes_an_executable(self, tmp_path: Path, bin_dir: Path):
        entry = make_entry(tmp_path)
        target = shim.install(entry, "/usr/games/cowsay")
        assert target == bin_dir / "cowsay"
        assert os.access(target, os.X_OK)

    def test_refuses_to_clobber_a_foreign_file(self, tmp_path: Path, bin_dir: Path):
        (bin_dir / "cowsay").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        entry = make_entry(tmp_path)
        with pytest.raises(CantCreateError, match="not created by aptx"):
            shim.install(entry, "/usr/games/cowsay")

    def test_force_overwrites(self, tmp_path: Path, bin_dir: Path):
        (bin_dir / "cowsay").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        entry = make_entry(tmp_path)
        shim.install(entry, "/usr/games/cowsay", force=True)
        assert shim.is_managed(bin_dir / "cowsay")

    def test_is_idempotent(self, tmp_path: Path, bin_dir: Path):
        entry = make_entry(tmp_path)
        first = shim.install(entry, "/usr/games/cowsay")
        second = shim.install(entry, "/usr/games/cowsay")
        assert first == second


class TestRemove:
    def test_removes_its_own_shim(self, tmp_path: Path, bin_dir: Path):
        entry = make_entry(tmp_path)
        shim.install(entry, "/usr/games/cowsay")
        assert shim.remove(entry, "/usr/games/cowsay") is True
        assert not (bin_dir / "cowsay").exists()

    def test_leaves_foreign_files_alone(self, tmp_path: Path, bin_dir: Path):
        (bin_dir / "cowsay").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        entry = make_entry(tmp_path)
        assert shim.remove(entry, "/usr/games/cowsay") is False
        assert (bin_dir / "cowsay").exists()

    def test_remove_all(self, tmp_path: Path, bin_dir: Path):
        entry = make_entry(tmp_path, files=["/usr/bin/sponge", "/usr/bin/ts"])
        for path in entry.files():
            shim.install(entry, path)
        assert sorted(shim.remove_all(entry)) == ["sponge", "ts"]
        assert list(bin_dir.iterdir()) == []


class TestOrphans:
    def test_finds_shims_for_missing_packages(self, tmp_path: Path, bin_dir: Path):
        entry = make_entry(tmp_path, files=["/usr/bin/sponge"])
        shim.install(entry, "/usr/bin/sponge")
        assert [p.name for p in shim.orphan_shims(set())] == ["sponge"]
        assert shim.orphan_shims({"cowsay"}) == []

    def test_ignores_unmanaged_files(self, tmp_path: Path, bin_dir: Path):
        (bin_dir / "mine").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        assert shim.orphan_shims(set()) == []


class TestAptxCommand:
    def test_uses_the_running_executable(self, monkeypatch, tmp_path: Path):
        binary = tmp_path / "aptx"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr(shim.sys, "argv", [str(binary)])
        assert shim.aptx_command() == str(binary)

    def test_falls_back_for_a_python_script(self, monkeypatch, tmp_path: Path):
        script = tmp_path / "aptx.py"
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(shim.sys, "argv", [str(script)])
        assert shim.aptx_command() == f"{shim.sys.executable} -m aptx"

    def test_falls_back_when_argv_is_empty(self, monkeypatch):
        monkeypatch.setattr(shim.sys, "argv", [])
        assert shim.aptx_command().endswith("-m aptx")

    def test_falls_back_when_the_executable_is_not_runnable(self, monkeypatch, tmp_path: Path):
        ghost = tmp_path / "ghost"
        monkeypatch.setattr(shim.sys, "argv", [str(ghost)])
        assert shim.aptx_command().endswith("-m aptx")


class TestShimPath:
    def test_bare_name(self, bin_dir: Path):
        assert shim.shim_path("cowsay") == bin_dir / "cowsay"

    def test_directory_components_are_dropped(self, bin_dir: Path):
        assert shim.shim_path("/usr/games/cowsay") == bin_dir / "cowsay"

    def test_traversal_is_dropped(self, bin_dir: Path):
        assert shim.shim_path("../../etc/passwd") == bin_dir / "passwd"

    @pytest.mark.parametrize("name", ["", ".", "..", "/", "../.."])
    def test_empty_names_are_rejected(self, bin_dir: Path, name: str):
        with pytest.raises(UsageError, match="invalid executable name"):
            shim.shim_path(name)


class TestIsManaged:
    def test_foreign_shell_script(self, tmp_path: Path):
        target = tmp_path / "x"
        target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        assert shim.is_managed(target) is False

    def test_non_shell_file(self, tmp_path: Path):
        target = tmp_path / "x"
        target.write_text("binary", encoding="utf-8")
        assert shim.is_managed(target) is False

    def test_missing_file(self, tmp_path: Path):
        assert shim.is_managed(tmp_path / "absent") is False

    def test_managed_file(self, bin_dir: Path, tmp_path: Path):
        entry = make_entry(tmp_path)
        target = shim.install(entry, "/usr/games/cowsay")
        assert shim.is_managed(target) is True


class TestInstallFailures:
    def test_unwritable_directory_raises(self, bin_dir: Path, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        monkeypatch.setattr(
            Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        with pytest.raises(CantCreateError, match="could not write"):
            shim.install(entry, "/usr/games/cowsay")

    def test_bin_dir_is_created(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "deep" / "bin"
        monkeypatch.setenv("APTX_BIN", str(target))
        entry = make_entry(tmp_path)
        shim.install(entry, "/usr/games/cowsay")
        assert target.is_dir()


class TestRemoveFailures:
    def test_unlink_failure_returns_false(self, bin_dir: Path, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        target = shim.install(entry, "/usr/games/cowsay")
        monkeypatch.setattr(
            Path, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        assert shim.remove(entry, "/usr/games/cowsay") is False
        assert target.exists()


class TestListShims:
    def test_lists_only_installed_shims(self, bin_dir: Path, tmp_path: Path):
        entry = make_entry(
            tmp_path, files=["/usr/games/cowsay", "/usr/bin/cowthink"]
        )
        shim.install(entry, "/usr/games/cowsay")
        assert [p.name for p in shim.list_shims(entry)] == ["cowsay"]

    def test_empty_without_any_shims(self, bin_dir: Path, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        assert shim.list_shims(entry) == []


class TestShimPackage:
    def test_reads_the_package_name(self, bin_dir: Path, tmp_path: Path):
        entry = make_entry(tmp_path)
        target = shim.install(entry, "/usr/games/cowsay")
        assert shim.shim_package(target) == "cowsay"

    def test_unreadable_file(self, tmp_path: Path):
        assert shim.shim_package(tmp_path / "absent") == ""

    def test_file_without_the_marker(self, tmp_path: Path):
        target = tmp_path / "x"
        target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        assert shim.shim_package(target) == ""


class TestOrphansExtra:
    def test_absent_bin_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "nope"))
        assert shim.orphan_shims(set()) == []

    def test_shims_for_known_packages_are_not_orphans(self, bin_dir: Path, tmp_path: Path):
        entry = make_entry(tmp_path)
        shim.install(entry, "/usr/games/cowsay")
        assert shim.orphan_shims({"cowsay"}) == []

    def test_unreadable_shims_are_skipped(self, bin_dir: Path, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        shim.install(entry, "/usr/games/cowsay")
        monkeypatch.setattr(shim, "shim_package", lambda path: "")
        assert shim.orphan_shims(set()) == []

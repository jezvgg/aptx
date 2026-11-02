"""doctor: every check, driven by fake hosts and fake subprocesses."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aptx import config, doctor
from aptx.doctor import Check


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path):
    config.set_cache_override(tmp_path / "cache")
    yield
    config.set_cache_override(None)


def required(check: Check | None) -> Check:
    """Assert a check was produced and hand it back for status assertions."""
    assert check is not None
    return check


def completed(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def fake_subprocess(monkeypatch, *, version: str = "bubblewrap 0.12.0", smoke_ok: bool = True):
    def runner(argv, **_kwargs):
        if "--version" in argv:
            return completed(version)
        return completed(returncode=0 if smoke_ok else 1)

    monkeypatch.setattr(doctor.subprocess, "run", runner)


def fake_which(monkeypatch, present: set[str]):
    monkeypatch.setattr(doctor, "which", lambda name: f"/usr/bin/{name}" if name in present else None)


ALL_TOOLS = {"bwrap", "proot", "apt-cache", "apt-get", "dpkg", "dpkg-deb"}


class TestCheck:
    def test_symbols(self):
        assert Check("a", doctor.OK, "").symbol == "+"
        assert Check("a", doctor.WARN, "").symbol == "~"
        assert Check("a", doctor.FAIL, "").symbol == "!"
        assert Check("a", "mystery", "").symbol == "?"


class TestSysctl:
    def test_reads_a_value(self, tmp_path: Path):
        target = tmp_path / "sysctl"
        target.write_text("  1234\n", encoding="utf-8")
        assert doctor._read_sysctl(target) == "1234"

    def test_missing_file(self, tmp_path: Path):
        assert doctor._read_sysctl(tmp_path / "absent") is None

    def test_userns_limit_is_parsed(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "max"
        target.write_text("481290\n", encoding="utf-8")
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", target)
        assert doctor._userns_limit() == 481290

    def test_userns_limit_missing(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", tmp_path / "absent")
        assert doctor._userns_limit() is None

    def test_userns_limit_not_a_number(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "max"
        target.write_text("lots\n", encoding="utf-8")
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", target)
        assert doctor._userns_limit() is None


class TestBwrapVersion:
    def test_parses_the_first_line(self, monkeypatch):
        fake_subprocess(monkeypatch, version="bubblewrap 0.12.0\nextra")
        assert doctor._bwrap_version() == "bubblewrap 0.12.0"

    def test_empty_output(self, monkeypatch):
        fake_subprocess(monkeypatch, version="")
        assert doctor._bwrap_version() == ""

    def test_exec_failure(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("nope")

        monkeypatch.setattr(doctor.subprocess, "run", boom)
        assert doctor._bwrap_version() == ""

    def test_timeout(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("bwrap", 10)

        monkeypatch.setattr(doctor.subprocess, "run", boom)
        assert doctor._bwrap_version() == ""


class TestVersionTuple:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("bubblewrap 0.12.0", (0, 12, 0)),
            ("0.6", (0, 6)),
            ("v1", (1,)),
            ("no digits here", ()),
        ],
    )
    def test_parse(self, text, expected):
        assert doctor._version_tuple(text) == expected


class TestBwrapSmoke:
    def test_success(self, monkeypatch):
        fake_subprocess(monkeypatch)
        assert doctor._bwrap_smoke() is True

    def test_nonzero_exit(self, monkeypatch):
        fake_subprocess(monkeypatch, smoke_ok=False)
        assert doctor._bwrap_smoke() is False

    def test_exec_failure(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("nope")

        monkeypatch.setattr(doctor.subprocess, "run", boom)
        assert doctor._bwrap_smoke() is False


class TestPythonCheck:
    def test_current_python_passes(self):
        assert doctor._python_check().status == doctor.OK

    def test_old_python_fails(self, monkeypatch):
        monkeypatch.setattr(doctor.platform, "python_version_tuple", lambda: ("3", "9", "0"))
        monkeypatch.setattr(doctor.platform, "python_version", lambda: "3.9.0")
        assert doctor._python_check().status == doctor.FAIL

    def test_unparsable_version_warns(self, monkeypatch):
        monkeypatch.setattr(doctor.platform, "python_version_tuple", lambda: ("x", "y", "z"))
        assert doctor._python_check().status == doctor.WARN


class TestBwrapCheck:
    def test_ok(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch)
        assert doctor._bwrap_check().status == doctor.OK

    def test_missing_with_proot_available(self, monkeypatch):
        fake_which(monkeypatch, {"proot"})
        check = doctor._bwrap_check()
        assert check.status == doctor.FAIL
        assert "proot" in check.detail

    def test_missing_entirely(self, monkeypatch):
        fake_which(monkeypatch, set())
        assert "not installed" in doctor._bwrap_check().detail

    def test_unparsable_version_warns(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch, version="weird")
        assert doctor._bwrap_check().status == doctor.WARN

    def test_too_old_fails(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch, version="bubblewrap 0.4.1")
        assert doctor._bwrap_check().status == doctor.FAIL


class TestUsernsCheck:
    def test_ok(self, monkeypatch, tmp_path: Path):
        limit = tmp_path / "max"
        limit.write_text("1000\n", encoding="utf-8")
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", limit)
        monkeypatch.setattr(doctor, "CLONE_SYSCTL", tmp_path / "clone")
        assert doctor._userns_check().status == doctor.OK

    def test_unreadable_warns(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", tmp_path / "absent")
        assert doctor._userns_check().status == doctor.WARN

    def test_zero_limit_fails(self, monkeypatch, tmp_path: Path):
        limit = tmp_path / "max"
        limit.write_text("0\n", encoding="utf-8")
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", limit)
        assert doctor._userns_check().status == doctor.FAIL

    def test_clone_disabled_fails(self, monkeypatch, tmp_path: Path):
        limit = tmp_path / "max"
        limit.write_text("1000\n", encoding="utf-8")
        clone = tmp_path / "clone"
        clone.write_text("0\n", encoding="utf-8")
        monkeypatch.setattr(doctor, "USERNS_SYSCTL", limit)
        monkeypatch.setattr(doctor, "CLONE_SYSCTL", clone)
        assert doctor._userns_check().status == doctor.FAIL


class TestToolsCheck:
    def test_all_present(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        assert doctor._tools_check("apt", doctor.APT_TOOLS).status == doctor.OK

    def test_some_missing(self, monkeypatch):
        fake_which(monkeypatch, {"apt-cache"})
        check = doctor._tools_check("apt", doctor.APT_TOOLS)
        assert check.status == doctor.FAIL
        assert "apt-get" in check.detail


class TestStoreCheck:
    def test_empty_store(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        assert "empty" in doctor._store_check().detail

    def test_unwritable_cache(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(
            Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        check = doctor._store_check()
        assert check.status == doctor.FAIL
        assert "ro" in check.detail

    def test_reports_sizes(self, monkeypatch):
        root = config.store_dir()
        for index in range(9):
            entry = root / f"pkg{index}"
            entry.mkdir(parents=True)
            (entry / ".ready").touch()
            (entry / "blob").write_bytes(b"x" * 100)
        check = doctor._store_check()
        assert check.status == doctor.OK
        assert "9 packages" in check.detail
        assert "(+1)" in check.detail


class TestBinDirCheck:
    def test_on_path(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "bin"
        monkeypatch.setenv("APTX_BIN", str(target))
        monkeypatch.setenv("PATH", f"{target}:/usr/bin")
        assert doctor._bin_dir_check().status == doctor.OK

    def test_not_on_path_warns(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "bin"
        monkeypatch.setenv("APTX_BIN", str(target))
        monkeypatch.setenv("PATH", "/usr/bin")
        assert doctor._bin_dir_check().status == doctor.WARN

    def test_uncreatable_fails(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        monkeypatch.setattr(
            Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        assert doctor._bin_dir_check().status == doctor.FAIL


class TestNetCheck:
    def test_lists_present(self, monkeypatch, tmp_path: Path):
        (tmp_path / "deb_Packages").write_text("", encoding="utf-8")
        monkeypatch.setattr(doctor, "LISTS_DIR", tmp_path)
        assert doctor._net_check().status == doctor.OK

    def test_no_lists_warns(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(doctor, "LISTS_DIR", tmp_path)
        check = doctor._net_check()
        assert check.status == doctor.WARN
        assert "apt-get update" in check.detail

    def test_unreadable_warns(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(doctor, "LISTS_DIR", tmp_path)
        monkeypatch.setattr(
            Path, "glob", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        assert doctor._net_check().status == doctor.WARN


class TestDeviceCheck:
    def test_present(self):
        assert doctor._device_check("d", "/").status == doctor.OK

    def test_missing_warns(self):
        assert doctor._device_check("d", "/definitely/absent").status == doctor.WARN


class TestRootCheck:
    def test_root_warns(self, monkeypatch):
        monkeypatch.setattr(doctor.os, "geteuid", lambda: 0)
        assert doctor._root_check().status == doctor.WARN

    def test_user_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor.os, "geteuid", lambda: 1000)
        assert doctor._root_check().status == doctor.OK


class TestPinCheck:
    def test_no_pins_is_omitted(self):
        assert doctor._pin_check([]) is None

    def test_pins_for_staged_packages(self, monkeypatch):
        config.pins_file().parent.mkdir(parents=True, exist_ok=True)
        config.pins_file().write_text('{"cowsay": "1.0"}', encoding="utf-8")
        entry = SimpleNamespace(pkg="cowsay")
        assert required(doctor._pin_check([entry])).status == doctor.OK

    def test_dangling_pins_warn(self, monkeypatch):
        config.pins_file().parent.mkdir(parents=True, exist_ok=True)
        config.pins_file().write_text('{"gone": "1.0"}', encoding="utf-8")
        check = doctor._pin_check([])
        assert check is not None and check.status == doctor.WARN
        assert "not staged" in check.detail


class TestShimCheck:
    def test_omitted_when_nothing_is_managed(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        assert doctor._shim_check([]) is None

    def test_consistent_when_packages_are_staged(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        check = doctor._shim_check([SimpleNamespace(pkg="cowsay")])
        assert required(check).status == doctor.OK

    def test_orphans_warn(self, monkeypatch, tmp_path: Path):
        from aptx import shim
        from tests.conftest import make_entry

        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("APTX_BIN", str(bin_dir))
        entry = make_entry(tmp_path / "entry")
        shim.install(entry, "/usr/games/cowsay")
        check = doctor._shim_check([])
        assert check is not None and check.status == doctor.WARN
        assert "dangling" in check.detail


class TestRunChecks:
    def test_full_run_on_this_host(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch)
        names = [check.name for check in doctor.run_checks()]
        assert names[0] == "aptx"
        assert "python" in names
        assert "platform" in names
        assert "apt" in names and "dpkg" in names

    def test_platform_check_fails_off_linux(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch)
        monkeypatch.setattr(doctor.platform, "system", lambda: "Darwin")
        check = next(c for c in doctor.run_checks() if c.name == "platform")
        assert check.status == doctor.FAIL

    def test_smoke_check_is_skipped_without_bwrap(self, monkeypatch):
        fake_which(monkeypatch, set())
        names = [check.name for check in doctor.run_checks()]
        assert "bwrap smoke" not in names

    def test_smoke_check_failure_is_reported(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch, smoke_ok=False)
        check = next(c for c in doctor.run_checks() if c.name == "bwrap smoke")
        assert check.status == doctor.FAIL


class TestFailed:
    def test_warnings_never_fail(self):
        assert doctor.failed([Check("python", doctor.WARN, "")]) is False

    def test_critical_failure_fails(self):
        assert doctor.failed([Check("python", doctor.FAIL, "")]) is True

    def test_non_critical_failure_is_ignored(self):
        assert doctor.failed([Check("net", doctor.FAIL, "")]) is False

    def test_clean_run(self):
        assert doctor.failed([Check("python", doctor.OK, "")]) is False


class TestRender:
    def test_columns_are_aligned(self):
        text = doctor.render([Check("a", doctor.OK, "x"), Check("long-name", doctor.WARN, "y")])
        assert text.splitlines()[0].startswith("+ a        ")
        assert text.splitlines()[1].startswith("~ long-name")

    def test_empty_list(self):
        assert doctor.render([]) == ""


class TestRunChecksOptional:
    def test_optional_checks_are_appended_when_present(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch)
        monkeypatch.setattr(doctor, "_pin_check", lambda entries: Check("pins", doctor.OK, "x"))
        monkeypatch.setattr(doctor, "_shim_check", lambda entries: Check("shims", doctor.OK, "y"))
        names = [check.name for check in doctor.run_checks()]
        assert "pins" in names and "shims" in names

    def test_optional_checks_are_skipped_when_absent(self, monkeypatch):
        fake_which(monkeypatch, ALL_TOOLS)
        fake_subprocess(monkeypatch)
        monkeypatch.setattr(doctor, "_pin_check", lambda entries: None)
        monkeypatch.setattr(doctor, "_shim_check", lambda entries: None)
        names = [check.name for check in doctor.run_checks()]
        assert "pins" not in names and "shims" not in names

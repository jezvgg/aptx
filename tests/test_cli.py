"""Argument parsing and binary selection — the parts users actually feel."""

from __future__ import annotations

from pathlib import Path

import pytest

from aptx.cli import _matches_bin, _pick_bin
from aptx.errors import NoInputError, UsageError
from tests.conftest import make_entry


class TestPickBin:
    def test_single_binary_is_implicit(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        assert _pick_bin(entry, None) == "/usr/games/cowsay"

    def test_explicit_name(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay", "/usr/games/cowthink"])
        assert _pick_bin(entry, "cowthink") == "/usr/games/cowthink"

    def test_package_named_binary_wins(self, tmp_path: Path):
        entry = make_entry(
            tmp_path,
            pkg="cowsay",
            files=["/usr/games/cowsay", "/usr/games/cowthink", "/usr/games/cowsay_random"],
        )
        assert _pick_bin(entry, None) == "/usr/games/cowsay"

    def test_ambiguous_without_a_package_named_binary(self, tmp_path: Path):
        entry = make_entry(
            tmp_path,
            pkg="moreutils",
            files=["/usr/bin/sponge", "/usr/bin/ts", "/usr/bin/vidir"],
        )
        with pytest.raises(UsageError, match="none is named moreutils"):
            _pick_bin(entry, None)

    def test_no_executables(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=[])
        with pytest.raises(NoInputError):
            _pick_bin(entry, None)

    def test_unknown_name_is_reported(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/bin/sponge"])
        with pytest.raises(NoInputError, match="no executable named 'nope'"):
            _pick_bin(entry, "nope")


class TestMatchesBin:
    def test_basename_matches(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        assert _matches_bin(entry, "cowsay")
        assert _matches_bin(entry, "/usr/games/cowsay")

    def test_plain_argument_does_not_match(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        assert not _matches_bin(entry, "hi")


# --- commands ---------------------------------------------------------------

from aptx import cli, config, sandbox, store  # noqa: E402
from aptx.errors import UnavailableError  # noqa: E402
from aptx.store import DEPS, FILES, META, PROVIDES, READY, Entry  # noqa: E402


def run(*args: str) -> int:
    """Run the CLI, which reports AptxError as an exit code rather than raising."""
    return cli.main(list(args))


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path):
    config.set_cache_override(tmp_path / "cache")
    yield
    config.set_cache_override(None)


def stage(pkg: str = "cowsay", files=("/usr/games/cowsay",), version: str = "1.0") -> Entry:
    """Create a ready store entry without touching the network."""
    target = config.store_dir() / pkg
    target.mkdir(parents=True, exist_ok=True)
    (target / PROVIDES).write_text(
        "".join(f"f\t{path}\t{pkg}\n" for path in files), encoding="utf-8"
    )
    (target / FILES).write_text("".join(f"{path}\n" for path in files), encoding="utf-8")
    (target / DEPS).write_text(f"{pkg}\t{version}\tall\tcafe\n", encoding="utf-8")
    (target / META).write_text(
        f'{{"pkg": "{pkg}", "version": "{version}"}}', encoding="utf-8"
    )
    (target / READY).touch()
    (target / ".last-used").touch()
    for path in files:
        binary = target / path.lstrip("/")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
    return Entry(pkg, target)


@pytest.fixture
def ran(monkeypatch):
    """Capture every sandbox invocation instead of spawning one."""
    calls: list[list[str]] = []

    def runner(argv, env=None):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(cli.sandbox, "run", runner)
    monkeypatch.setattr(
        cli.sandbox, "build_argv", lambda entry, plan, stage_dir, binpath, argv, opts: [
            "bwrap", binpath, *argv
        ]
    )
    monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {})
    return calls


class TestMain:
    def test_help(self, capsys):
        assert cli.main([]) == 0
        assert "Usage: aptx" in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_flags(self, capsys, flag):
        assert cli.main([flag]) == 0
        assert "Commands:" in capsys.readouterr().out

    def test_version(self, capsys):
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().out.startswith("aptx ")

    def test_unknown_option_is_not_a_package(self, capsys):
        assert cli.main(["--frobnicate"]) == 2
        assert "No such option" in capsys.readouterr().err

    @pytest.mark.parametrize("command", sorted(cli.cli.commands))
    def test_per_command_help(self, capsys, command):
        assert cli.main([command, "--help"]) == 0
        assert "Usage:" in capsys.readouterr().out

    def test_command_help_does_not_swallow_program_flags(self, ran, capsys):
        stage()
        assert cli.main(["run", "cowsay", "--help"]) == 0
        assert ran == [["bwrap", "/usr/games/cowsay", "--help"]]

    def test_global_verbose_and_quiet(self):
        from aptx import proc

        base = proc.verbose_level()
        cli.main(["-v", "list"])
        assert proc.verbose_level() == base + 1
        cli.main(["-q", "-q", "list"])
        assert proc.verbose_level() == base - 2

    def test_no_color(self):
        from aptx import errors

        cli.main(["--no-color", "--help"])
        assert errors.color_enabled() is False
        errors.set_color(True)

    def test_keyboard_interrupt_exits_130(self, monkeypatch):
        monkeypatch.setattr(
            cli, "_staged", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt)
        )
        assert cli.main(["plan", "cowsay"]) == 130

    def test_aptx_error_is_rendered(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli, "_staged", lambda *a, **kw: (_ for _ in ()).throw(NoInputError("boom"))
        )
        assert cli.main(["plan", "cowsay"]) == 66
        assert "aptx: error:" in capsys.readouterr().err

class TestPersistSpelling:
    def test_a_bare_persist_does_not_eat_the_package(self, ran, monkeypatch):
        stage()
        seen: list[sandbox.SandboxOptions] = []
        monkeypatch.setattr(
            cli.sandbox,
            "build_argv",
            lambda entry, plan, stage_dir, binpath, argv, opts: (
                seen.append(opts) or ["bwrap", binpath, *argv]
            ),
        )
        assert run("run", "--persist", "cowsay", "hi") == 0
        assert seen[0].persist == "default"

    def test_an_inline_value_still_works(self, ran, monkeypatch):
        stage()
        seen: list[sandbox.SandboxOptions] = []
        monkeypatch.setattr(
            cli.sandbox,
            "build_argv",
            lambda entry, plan, stage_dir, binpath, argv, opts: (
                seen.append(opts) or ["bwrap", binpath, *argv]
            ),
        )
        assert run("run", "--persist=shared", "cowsay", "hi") == 0
        assert seen[0].persist == "shared"

    def test_persist_after_the_package_goes_to_the_program(self, ran):
        stage()
        assert run("run", "cowsay", "--persist") == 0
        assert ran[-1] == ["bwrap", "/usr/games/cowsay", "--persist"]


class TestOptionTypos:
    def test_an_unknown_flag_before_the_package_is_not_a_package(self, capsys):
        assert run("run", "--bogus", "cowsay") == 2
        assert "no such option: --bogus" in capsys.readouterr().err


class TestDamagedStore:
    def test_a_missing_payload_is_reported_not_executed(self, capsys, tmp_path: Path):
        entry = stage("cowsay", ["/usr/games/cowsay"])
        (entry.path / "usr" / "games" / "cowsay").unlink()
        assert cli.main(["run", "cowsay", "hi"]) == 65
        err = capsys.readouterr().err
        assert "is damaged" in err
        assert "--force-stage cowsay" in err


class TestHostileBinNames:
    @pytest.mark.parametrize("name", ["../../../etc/passwd", "usr/../..", ".."])
    def test_traversal_is_a_usage_error(self, name, capsys):
        stage("cowsay", ["/usr/games/cowsay"])
        assert cli.main(["which", "cowsay", name]) == 2
        assert "invalid executable name" in capsys.readouterr().err


class TestShorthandResolution:
    """The group falls back to `run` for a bare first token."""

    def _context(self):
        import click

        return click.Context(cli.cli)

    def test_unknown_command_becomes_a_package(self):
        import click

        name, command, args = cli.cli.resolve_command(
            self._context(), ["cowsay", "hi"]
        )
        assert name == "run"
        assert command is cli.cli.commands["run"]
        assert args == ["cowsay", "hi"]

    def test_known_command_wins(self):
        name, _command, args = cli.cli.resolve_command(self._context(), ["list"])
        assert name == "list"
        assert args == []

    def test_an_option_is_not_a_package(self):
        import click

        # `aptx -x` must report an unknown command, never run a package "-x".
        with pytest.raises(click.UsageError):
            cli.cli.resolve_command(self._context(), ["-x"])


class TestMainErrors:
    def test_persist_and_ephemeral_conflict(self, capsys):
        assert cli.main(["run", "--persist", "--ephemeral", "cowsay"]) == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_click_abort_exits_130(self, monkeypatch):
        import click

        monkeypatch.setattr(
            cli, "_staged", lambda *a, **kw: (_ for _ in ()).throw(click.exceptions.Abort())
        )
        assert cli.main(["plan", "cowsay"]) == 130

    def test_keyboard_interrupt_from_click_itself_exits_130(self, monkeypatch):
        monkeypatch.setattr(
            cli.cli, "main", lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        assert cli.main(["list"]) == 130

    def test_empty_argv_shows_help(self, capsys):
        assert cli.main([]) == 0
        assert "Usage: aptx" in capsys.readouterr().out


class TestPackageShortcut:
    """`aptx cowsay hi` is `aptx run cowsay hi`."""

    def test_bare_package_runs_it(self, ran):
        stage()
        assert cli.main(["cowsay", "hi"]) == 0
        assert ran == [["bwrap", "/usr/games/cowsay", "hi"]]

    def test_program_arguments_pass_through(self, ran):
        stage()
        assert cli.main(["cowsay", "-f", "tux", "hi"]) == 0
        assert ran[0][2:] == ["-f", "tux", "hi"]

    def test_a_command_name_still_wins(self, capsys):
        assert cli.main(["list"]) == 0
        assert "no staged packages" in capsys.readouterr().out

    def test_global_flags_still_apply(self, ran):
        stage()
        assert cli.main(["--no-color", "cowsay", "hi"]) == 0
        assert ran[0][1] == "/usr/games/cowsay"

    def test_missing_package_reports_a_usage_error(self, monkeypatch):
        from aptx.errors import DataError

        monkeypatch.setattr(cli.store, "build", lambda pkg, **kw: (_ for _ in ()).throw(DataError("package not found: nope")))
        assert cli.main(["nope"]) == 65

    def test_explicit_binary_is_honoured(self, ran):
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        assert cli.main(["cowsay", "cowthink", "moo"]) == 0
        assert ran[0][1] == "/usr/games/cowthink"


class TestCmdRun:
    def test_runs_the_single_binary(self, ran):
        stage()
        assert cli.main(["run", "cowsay", "hi"]) == 0
        assert ran == [["bwrap", "/usr/games/cowsay", "hi"]]

    def test_dashdash_makes_every_token_a_program_argument(self, ran):
        # `--` is explicit: the first token is never taken as the binary.
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        assert cli.main(["run", "cowsay", "--", "cowthink", "hi"]) == 0
        assert ran[0][1] == "/usr/games/cowsay"
        assert ran[0][2:] == ["cowthink", "hi"]

    def test_dashdash_before_the_package(self, ran):
        stage()
        assert cli.main(["run", "--", "cowsay", "hi"]) == 0
        assert ran == [["bwrap", "/usr/games/cowsay", "hi"]]

    def test_dry_run_prints_instead_of_running(self, ran, capsys):
        stage()
        assert cli.main(["run", "--dry-run", "cowsay", "hi"]) == 0
        assert ran == []
        assert "bwrap" in capsys.readouterr().out

    def test_no_sandbox_passes_the_host_environment(self, monkeypatch):
        stage()
        seen: dict = {}

        def runner(argv, env=None):
            seen["env"] = env
            return 0

        monkeypatch.setattr(cli.sandbox, "run", runner)
        monkeypatch.setattr(cli.sandbox, "build_argv", lambda *a: ["/x"])
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {})
        assert cli.main(["run", "--no-sandbox", "cowsay"]) == 0
        assert seen["env"] is not None and "PATH" in seen["env"]

    def test_none_backend_passes_the_host_environment(self, monkeypatch):
        stage()
        seen: dict = {}

        def runner(argv, env=None):
            seen["env"] = env
            return 0

        monkeypatch.setattr(cli.sandbox, "run", runner)
        monkeypatch.setattr(cli.sandbox, "build_argv", lambda *a: ["/x"])
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {})
        assert cli.main(["run", "--backend", "none", "cowsay"]) == 0
        assert seen["env"] is not None

    def test_host_binary_wins_for_an_installed_package(self, monkeypatch, capsys):
        stage()
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {"cowsay": "ii "})
        monkeypatch.setattr(
            cli.proc, "which", lambda name: "/usr/bin/cowsay" if name == "cowsay" else None
        )
        calls: list = []
        monkeypatch.setattr(cli.sandbox, "run", lambda argv, env=None: calls.append(argv) or 0)
        assert cli.main(["run", "cowsay", "hi"]) == 0
        assert calls == [["/usr/bin/cowsay", "hi"]]
        assert "already installed" in capsys.readouterr().err

    def test_installed_package_with_an_explicit_binary(self, monkeypatch):
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {"cowsay": "ii "})
        monkeypatch.setattr(
            cli.proc, "which", lambda name: f"/usr/bin/{name}" if name == "cowthink" else None
        )
        calls: list = []
        monkeypatch.setattr(cli.sandbox, "run", lambda argv, env=None: calls.append(argv) or 0)
        assert cli.main(["run", "cowsay", "cowthink", "hi"]) == 0
        assert calls == [["/usr/bin/cowthink", "hi"]]

    def test_dashdash_keeps_the_package_binary(self, monkeypatch, ran):
        # With `--` the first token is never treated as a binary selector.
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {"cowsay": "ii "})
        monkeypatch.setattr(
            cli.proc, "which", lambda name: f"/usr/bin/{name}" if name == "cowsay" else None
        )
        calls: list = []
        monkeypatch.setattr(cli.sandbox, "run", lambda argv, env=None: calls.append(argv) or 0)
        assert cli.main(["run", "cowsay", "--", "cowthink", "hi"]) == 0
        assert calls == [["/usr/bin/cowsay", "cowthink", "hi"]]

    def test_installed_package_without_a_host_binary_still_stages(self, monkeypatch, ran):
        stage()
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {"cowsay": "ii "})
        monkeypatch.setattr(cli.proc, "which", lambda name: None)
        assert cli.main(["run", "cowsay", "hi"]) == 0
        assert ran[0][1] == "/usr/games/cowsay"

    def test_force_stage_skips_the_host_binary(self, monkeypatch, ran):
        stage()
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {"cowsay": "ii "})
        assert cli.main(["run", "--force-stage", "cowsay", "hi"]) == 0
        assert ran[0][1] == "/usr/games/cowsay"

    def test_installed_status_failure_is_ignored(self, monkeypatch, ran):
        stage()
        from aptx.errors import DataError

        monkeypatch.setattr(
            cli.fetch, "installed_status", lambda names: (_ for _ in ()).throw(DataError("x"))
        )
        assert cli.main(["run", "cowsay", "hi"]) == 0

    def test_ephemeral_uses_a_throwaway_cache(self, ran, monkeypatch, tmp_path: Path):
        entry = stage()
        monkeypatch.setattr(cli.store, "build", lambda pkg, **kw: entry)
        scratch = tmp_path / "ephemeral"
        scratch.mkdir()
        monkeypatch.setattr(cli.fetch, "temp_dir", lambda prefix: scratch)
        assert cli.main(["run", "--ephemeral", "cowsay", "hi"]) == 0
        assert not scratch.exists()  # thrown away afterwards
        assert ran[0][1] == "/usr/games/cowsay"

    def test_ephemeral_redirects_the_store(self, monkeypatch):
        entry = stage()
        monkeypatch.setattr(cli.store, "build", lambda pkg, **kw: entry)
        monkeypatch.setattr(cli.sandbox, "build_argv", lambda *a: ["/x"])
        monkeypatch.setattr(cli.sandbox, "run", lambda argv, env=None: 0)
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {})
        seen: list = []
        real_override = config.set_cache_override
        monkeypatch.setattr(
            config, "set_cache_override", lambda path: seen.append(path) or real_override(path)
        )
        assert cli.main(["run", "--ephemeral", "cowsay"]) == 0
        assert seen[0] is not None and seen[-1] is None

    def test_ephemeral_cleans_up_on_failure(self, monkeypatch):
        entry = stage()
        monkeypatch.setattr(cli.store, "build", lambda pkg, **kw: entry)
        monkeypatch.setattr(cli.sandbox, "build_argv", lambda *a: ["/x"])
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {})
        monkeypatch.setattr(
            cli.sandbox, "run", lambda argv, env=None: (_ for _ in ()).throw(UnavailableError("nope"))
        )
        assert cli.main(["run", "--ephemeral", "cowsay"]) == 69
        assert config.cache_dir() == config.cache_dir()

    def test_rest_is_not_treated_as_a_binary(self, ran):
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        assert cli.main(["run", "cowsay", "hello"]) == 0
        assert ran[0] == ["bwrap", "/usr/games/cowsay", "hello"]

    def test_explicit_binary_token_is_consumed(self, ran):
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        assert cli.main(["run", "cowsay", "cowthink", "moo"]) == 0
        assert ran[0][1] == "/usr/games/cowthink"
        assert ran[0][2:] == ["moo"]


class TestCmdShell:
    def test_runs_sh_interactively(self, ran):
        stage()
        assert cli.main(["shell", "cowsay"]) == 0
        assert ran[0][1] == "/bin/sh"

    def test_arguments_after_dashdash_go_to_the_shell(self, ran):
        stage()
        assert cli.main(["shell", "cowsay", "--", "-c", "echo hi"]) == 0
        assert ran[0][2:] == ["-c", "echo hi"]

    def test_dry_run(self, ran, capsys):
        stage()
        assert cli.main(["shell", "--dry-run", "cowsay"]) == 0
        assert ran == []
        assert "bwrap" in capsys.readouterr().out

    def test_no_sandbox(self, monkeypatch):
        stage()
        seen: dict = {}
        def runner(argv, env=None):
            seen["env"] = env
            return 0

        monkeypatch.setattr(cli.sandbox, "run", runner)
        monkeypatch.setattr(cli.sandbox, "build_argv", lambda *a: ["/x"])
        monkeypatch.setattr(cli.fetch, "installed_status", lambda names: {})
        assert cli.main(["shell", "--no-sandbox", "cowsay"]) == 0
        assert seen["env"] is not None

    def test_ephemeral(self, ran):
        stage()
        assert cli.main(["shell", "--ephemeral", "cowsay"]) == 0
        assert ran[0][1] == "/bin/sh"


class TestCmdInstall:
    def test_installs_a_shim(self, ran, monkeypatch, tmp_path: Path, capsys):
        stage()
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        assert cli.main(["install", "cowsay"]) == 0
        out = capsys.readouterr().out
        assert "installed cowsay" in out
        assert "remove with" in out
        assert (tmp_path / "bin" / "cowsay").exists()

    def test_installs_several_binaries(self, monkeypatch, tmp_path: Path):
        stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        assert cli.main(["install", "cowsay", "cowsay", "cowthink"]) == 0
        assert (tmp_path / "bin" / "cowthink").exists()

    def test_force_is_passed_through(self, monkeypatch, tmp_path: Path, ran):
        stage()
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "cowsay").write_text("foreign", encoding="utf-8")
        assert cli.main(["install", "cowsay"]) == 73
        assert cli.main(["install", "--force", "cowsay"]) == 0

    def test_missing_package_name(self):
        assert run("install") == 2

    def test_unknown_option(self):
        assert run("install", "--nope", "cowsay") == 2

    def test_force_stages_before_shimming(self, monkeypatch, tmp_path: Path):
        entry = stage()
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        forced: list[bool] = []
        real = cli.stage.ensure
        monkeypatch.setattr(
            cli.stage, "ensure", lambda e, p, force=False: forced.append(force) or real(e, p)
        )
        assert cli.main(["install", "--force", "cowsay"]) == 0
        assert forced == [True]
        assert entry.pkg == "cowsay"


class TestCmdRemove:
    def test_removes_a_package(self, capsys):
        stage()
        assert cli.main(["remove", "cowsay"]) == 0
        assert "removed cowsay" in capsys.readouterr().out

    def test_reports_removed_shims(self, monkeypatch, tmp_path: Path, capsys):
        stage()
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        cli.main(["install", "cowsay"])
        assert cli.main(["remove", "cowsay"]) == 0
        assert "1 shim(s): cowsay" in capsys.readouterr().out

    def test_several_packages(self, capsys):
        stage("cowsay")
        stage("moreutils", files=("/usr/bin/sponge",))
        assert cli.main(["remove", "cowsay", "moreutils"]) == 0
        assert "removed moreutils" in capsys.readouterr().out

    def test_unstaged_package(self):
        assert run("remove", "cowsay") == 66

    def test_all_removes_everything(self, capsys):
        stage()
        assert cli.main(["remove", "--all"]) == 0
        assert "removed the store" in capsys.readouterr().out

    def test_all_with_package_names_is_an_error(self):
        assert run("remove", "--all", "cowsay") == 2

    def test_no_arguments(self):
        assert run("remove") == 2

    def test_unknown_option(self):
        assert run("remove", "--nope") == 2


class TestCmdList:
    def test_empty_store(self, capsys):
        assert cli.main(["list"]) == 0
        assert "no staged packages" in capsys.readouterr().out

    def test_table_and_sections(self, monkeypatch, tmp_path: Path, capsys):
        stage()
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        cli.main(["install", "cowsay"])
        stage("moreutils", files=("/usr/bin/sponge",))
        assert cli.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "PACKAGE" in out and "SHIMS" in out
        assert "unshimmed:" in out and "moreutils" in out

    def test_long_binary_lists_are_truncated(self, capsys):
        stage(files=tuple(f"/usr/bin/b{index}" for index in range(6)))
        assert cli.main(["list"]) == 0
        assert "…" in capsys.readouterr().out

    def test_orphan_shims_are_reported(self, monkeypatch, tmp_path: Path, capsys):
        from aptx import shim
        from tests.conftest import make_entry

        entry = make_entry(tmp_path / "e")
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        shim.install(entry, "/usr/games/cowsay")
        stage("moreutils", files=("/usr/bin/sponge",))
        assert cli.main(["list"]) == 0
        assert "orphan shims:" in capsys.readouterr().out


class TestCmdLs:
    def test_prints_plan_files_and_closure(self, capsys):
        stage()
        assert cli.main(["ls", "cowsay"]) == 0
        out = capsys.readouterr().out
        assert "package: cowsay 1.0" in out
        assert "executables (1):" in out
        assert "closure (1 packages):" in out

    def test_closure_is_truncated(self, capsys):
        entry = stage()
        (entry.path / DEPS).write_text(
            "".join(f"dep{index}\t1.0\tall\tcafe\n" for index in range(25)), encoding="utf-8"
        )
        assert cli.main(["ls", "cowsay"]) == 0
        assert "… and 5 more" in capsys.readouterr().out

    def test_package_without_executables(self, capsys):
        stage(files=())
        assert cli.main(["ls", "cowsay"]) == 0
        assert "executables" not in capsys.readouterr().out

    def test_missing_name(self):
        assert run("ls") == 2


class TestCmdWhich:
    def test_prints_the_store_path(self, capsys):
        entry = stage()
        assert cli.main(["which", "cowsay"]) == 0
        assert capsys.readouterr().out.strip() == str(entry.store_path("/usr/games/cowsay"))

    def test_explicit_binary(self, capsys):
        entry = stage(files=("/usr/games/cowsay", "/usr/games/cowthink"))
        assert cli.main(["which", "cowsay", "cowthink"]) == 0
        assert capsys.readouterr().out.strip().endswith("/usr/games/cowthink")
        assert entry.pkg == "cowsay"

    def test_unstaged_package(self):
        assert run("which", "cowsay") == 66

    def test_missing_name(self):
        assert run("which") == 2


class TestCmdPlan:
    def test_prints_the_plan(self, capsys):
        stage()
        assert cli.main(["plan", "cowsay"]) == 0
        assert "merge points" in capsys.readouterr().out

    def test_missing_name(self):
        assert run("plan") == 2


class TestCmdStore:
    def test_path(self, capsys):
        assert cli.main(["store", "path"]) == 0
        assert capsys.readouterr().out.strip() == str(config.store_dir())

    def test_size(self, capsys):
        stage()
        assert cli.main(["store", "size"]) == 0
        assert capsys.readouterr().out.strip().endswith(("B", "K", "M", "G"))

    def test_du(self, capsys):
        stage()
        assert cli.main(["store", "du"]) == 0
        assert "cowsay" in capsys.readouterr().out

    def test_du_on_an_empty_store(self, capsys):
        assert cli.main(["store", "du"]) == 0
        assert "store is empty" in capsys.readouterr().out

    def test_missing_subcommand(self):
        assert run("store") == 2

    def test_unknown_subcommand(self):
        assert run("store", "nope") == 2


class TestParseDuration:
    @pytest.mark.parametrize(
        "text, seconds",
        [
            ("30m", 1800.0),
            ("24h", 86400.0),
            ("7d", 604800.0),
            ("2w", 1209600.0),
            ("90s", 90.0),
            ("7", 604800.0),
            (" 7D ", 604800.0),
            ("0.5d", 43200.0),
        ],
    )
    def test_units(self, text, seconds):
        assert cli.parse_duration(text) == seconds

    def test_empty(self):
        with pytest.raises(UsageError, match="needs a value"):
            cli.parse_duration("")

    def test_garbage(self):
        with pytest.raises(UsageError, match="expects 7d/24h/30m/2w"):
            cli.parse_duration("nonsense")


class TestCmdGc:
    def test_nothing_to_collect(self, capsys):
        assert cli.main(["gc"]) == 0
        assert "nothing to collect" in capsys.readouterr().out

    def test_removes_old_entries(self, capsys):
        import os

        entry = stage()
        old = 1_000_000
        os.utime(entry.path / ".last-used", (old, old))
        assert cli.main(["gc", "--older-than", "1d"]) == 0
        assert "removed 1 package(s): cowsay" in capsys.readouterr().out

    def test_equals_form(self, capsys):
        assert cli.main(["gc", "--older-than=1d"]) == 0
        assert "nothing to collect" in capsys.readouterr().out

    def test_dangling_shims_are_removed(self, monkeypatch, tmp_path: Path, capsys):
        from aptx import shim
        from tests.conftest import make_entry

        entry = make_entry(tmp_path / "e")
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        shim.install(entry, "/usr/games/cowsay")
        assert cli.main(["gc"]) == 0
        assert "dangling shim(s)" in capsys.readouterr().out

    def test_older_than_without_a_value(self):
        assert run("gc", "--older-than") == 2

    def test_unknown_option(self):
        assert run("gc", "--nope") == 2


class TestCmdRmAll:
    def test_removes_store_and_shims(self, monkeypatch, tmp_path: Path, capsys):
        stage()
        monkeypatch.setenv("APTX_BIN", str(tmp_path / "bin"))
        cli.main(["install", "cowsay"])
        assert cli.main(["rm-all"]) == 0
        assert "removed the store and 1 shim(s)" in capsys.readouterr().out
        assert not config.store_dir().exists()

    def test_empty(self, capsys):
        assert cli.main(["rm-all"]) == 0
        assert "removed the store and 0 shim(s)" in capsys.readouterr().out


class TestCmdPin:
    def test_pins_an_explicit_version(self, capsys):
        assert cli.main(["pin", "cowsay=1.2"]) == 0
        assert store.load_pins() == {"cowsay": "1.2"}
        assert "pinned cowsay=1.2" in capsys.readouterr().out

    def test_pins_a_separate_version_argument(self):
        assert cli.main(["pin", "cowsay", "1.3"]) == 0
        assert store.load_pins() == {"cowsay": "1.3"}

    def test_pins_the_staged_version(self, capsys):
        stage(version="2.0")
        assert cli.main(["pin", "cowsay"]) == 0
        assert store.load_pins() == {"cowsay": "2.0"}

    def test_unstaged_without_a_version(self):
        assert run("pin", "cowsay") == 66

    def test_clear(self, capsys):
        store.set_pin("cowsay", "1.0")
        assert cli.main(["pin", "cowsay", "--clear"]) == 0
        assert store.load_pins() == {}
        assert "unpinned cowsay" in capsys.readouterr().out

    def test_missing_name(self):
        assert run("pin") == 2

    def test_unknown_option(self):
        assert run("pin", "--nope") == 2

    def test_unpin_command(self, capsys):
        store.set_pin("cowsay", "1.0")
        assert cli.main(["unpin", "cowsay"]) == 0
        assert "unpinned cowsay" in capsys.readouterr().out

    def test_unpin_without_a_name(self):
        assert run("unpin") == 2


class TestCmdDoctor:
    def test_prints_checks(self, monkeypatch, capsys):
        from aptx.doctor import Check, FAIL, OK

        monkeypatch.setattr(
            cli.doctor, "run_checks", lambda: [Check("python", OK, "3.12"), Check("net", FAIL, "x")]
        )
        assert cli.main(["doctor"]) == 0
        assert "+ python" in capsys.readouterr().out

    def test_critical_failure_exits_69(self, monkeypatch):
        from aptx.doctor import Check, FAIL

        monkeypatch.setattr(cli.doctor, "run_checks", lambda: [Check("python", FAIL, "old")])
        assert run("doctor") == 69


class TestHelpers:
    def test_backend_name(self):
        assert cli._backend_name(sandbox.SandboxOptions()) == "bwrap"
        assert cli._backend_name(sandbox.SandboxOptions(backend="proot")) == "proot"
        assert cli._backend_name(sandbox.SandboxOptions(backend="none")) == "none"

    def test_staged_rebuilds_when_forced(self, monkeypatch):
        entry = stage()
        built: list = []
        monkeypatch.setattr(cli.store, "build", lambda pkg, **kw: built.append(kw) or entry)
        assert cli._staged("cowsay", False, True).pkg == "cowsay"
        assert built and built[0]["force"] is True

    def test_staged_delegates_to_build_so_the_policy_lives_in_one_place(self, monkeypatch):
        entry = stage()
        calls: list[dict] = []
        monkeypatch.setattr(cli.store, "build", lambda pkg, **kw: calls.append(kw) or entry)
        assert cli._staged("cowsay", True, False).pkg == "cowsay"
        assert calls == [{"recommends": True, "force": False, "backend": "bwrap"}]

    def test_unlink_reports_failure(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "x"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            Path, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        assert cli._unlink(target) is False


class TestModuleEntryPoint:
    def test_python_dash_m_runs_main(self, monkeypatch, capsys):
        import runpy

        monkeypatch.setattr(cli.sys, "argv", ["aptx", "--version"])
        with pytest.raises(SystemExit) as caught:
            runpy.run_module("aptx", run_name="__main__")
        assert caught.value.code == 0
        assert capsys.readouterr().out.startswith("aptx ")

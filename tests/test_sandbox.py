"""Sandbox argv construction — no process is ever spawned."""

from __future__ import annotations

import stat as stat_mod
from pathlib import Path
from types import SimpleNamespace

import pytest

from aptx import sandbox
from aptx.errors import UnavailableError, UsageError
from aptx.plan import Plan
from aptx.sandbox import SandboxOptions, build_bwrap_argv, build_proot_argv, sandbox_env
from tests.conftest import make_entry

PLAN = Plan(pkg="cowsay", version="1.0", merge_points={"/usr/games": ["cowsay"]})


def argv_for(tmp_path: Path, opts: SandboxOptions | None = None, argv=None) -> list[str]:
    entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(exist_ok=True)
    return build_bwrap_argv(
        entry, PLAN, stage_dir, "/usr/games/cowsay", argv or [], opts or SandboxOptions()
    )


def after(args: list[str], token: str) -> str:
    return args[args.index(token) + 1]


def binds(args: list[str]) -> dict[str, tuple[str, str]]:
    """Map mount target -> (flag, source) for every bind-like operation."""
    found: dict[str, tuple[str, str]] = {}
    for index, part in enumerate(args):
        if part.startswith("--") and part.endswith(("-bind", "-bind-try")):
            found[args[index + 2]] = (part, args[index + 1])
    return found


def tmpfs_dirs(args: list[str]) -> list[str]:
    return [args[i + 1] for i, part in enumerate(args) if part == "--tmpfs"]


class TestStructure:
    def test_starts_from_an_empty_root(self, tmp_path: Path):
        args = argv_for(tmp_path)
        assert args[:4] == ["bwrap", "--die-with-parent", "--tmpfs", "/"]

    def test_store_is_bound_at_its_fixed_path(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_bwrap_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", [], SandboxOptions()
        )
        assert binds(args)["/run/aptx/root"] == ("--ro-bind", str(entry.path.resolve()))

    def test_farm_is_bound_over_the_real_path(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_bwrap_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", [], SandboxOptions()
        )
        assert binds(args)["/usr"] == ("--ro-bind", str(stage_dir / "usr"))

    def test_host_directory_gets_a_flat_mount(self, tmp_path: Path):
        args = argv_for(tmp_path)
        assert "/run/aptx/host/usr" in args
        assert "/run/aptx/host/usr!games" in args

    def test_usrmerge_symlinks_are_created(self, tmp_path: Path):
        args = argv_for(tmp_path)
        assert "usr/bin" in args and "/bin" in args

    def test_proc_and_dev_are_mounted(self, tmp_path: Path):
        args = argv_for(tmp_path)
        assert after(args, "--proc") == "/proc"
        assert after(args, "--dev") == "/dev"

    def test_tmp_exists_so_mkstemp_works(self, tmp_path: Path):
        """`sponge` and friends call mkstemp() in $TMPDIR; it must exist."""
        assert "/tmp" in tmpfs_dirs(argv_for(tmp_path))

    def test_runtime_dir_is_created(self, tmp_path: Path):
        args = argv_for(tmp_path)
        assert any(part.startswith("/run/user/") for part in args)


class TestEnvironment:
    def test_environment_is_cleared_then_rebuilt(self, tmp_path: Path):
        args = argv_for(tmp_path)
        assert "--clearenv" in args
        assert after(args, "--setenv") == "PATH"

    def test_path_includes_games(self, tmp_path: Path):
        assert "/usr/games" in sandbox_env(SandboxOptions())["PATH"]

    def test_home_points_inside_the_sandbox(self, tmp_path: Path):
        env = sandbox_env(SandboxOptions())
        assert env["HOME"].startswith("/home/")

    def test_gui_only_when_asked(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        assert "DISPLAY" not in sandbox_env(SandboxOptions())
        assert sandbox_env(SandboxOptions(gui=True))["DISPLAY"] == ":0"


class TestIsolation:
    def test_home_is_a_tmpfs_by_default(self, tmp_path: Path):
        homes = [path for path in tmpfs_dirs(argv_for(tmp_path)) if path.startswith("/home/")]
        assert len(homes) == 1

    def test_persist_binds_a_state_directory(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_bwrap_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", [], SandboxOptions(persist="default")
        )
        assert "--bind" in args
        bound = [args[i + 1] for i, part in enumerate(args) if part == "--bind"]
        assert any(path.endswith("state/default") for path in bound)
        assert (entry.state_dir / "default").is_dir()

    def test_cwd_is_read_only_unless_asked(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert binds(argv_for(tmp_path))[str(tmp_path)][0] == "--ro-bind"

    def test_rw_cwd_makes_it_writable(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert binds(argv_for(tmp_path, SandboxOptions(rw_cwd=True)))[str(tmp_path)][0] == "--bind"

    def test_bare_tmp_is_never_bound(self, tmp_path: Path, monkeypatch):
        """Binding the host's /tmp over our tmpfs would leak the host scratch space."""
        monkeypatch.chdir("/tmp")
        assert "/tmp" not in binds(argv_for(tmp_path))

    def test_cwd_under_tmp_is_still_bound(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert str(tmp_path) in binds(argv_for(tmp_path))

    def test_offline_unshares_the_network(self, tmp_path: Path):
        assert "--unshare-net" not in argv_for(tmp_path)
        assert "--unshare-net" in argv_for(tmp_path, SandboxOptions(offline=True))

    def test_gpu_binds_only_when_asked(self, tmp_path: Path):
        assert "/dev/dri" not in argv_for(tmp_path)
        assert "/dev/dri" in argv_for(tmp_path, SandboxOptions(gpu=True))

    def test_extra_writable_bind(self, tmp_path: Path):
        args = argv_for(tmp_path, SandboxOptions(rw=["/srv/data:/data"]))
        assert binds(args)["/data"] == ("--bind", "/srv/data")

    def test_dev_bind(self, tmp_path: Path):
        args = argv_for(tmp_path, SandboxOptions(dev_binds=["/dev/null"]))
        assert "--dev-bind-try" in args


class TestCommandLine:
    def test_command_comes_after_the_separator(self, tmp_path: Path):
        args = argv_for(tmp_path, argv=["hi", "-f", "tux"])
        index = args.index("--")
        assert args[index + 1 :] == ["/usr/games/cowsay", "hi", "-f", "tux"]

    def test_no_sandbox_runs_the_store_binary(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        (entry.path / "usr/games").mkdir(parents=True)
        (entry.path / "usr/games/cowsay").write_text("#!/bin/sh\n", encoding="utf-8")
        args = sandbox.build_argv(
            entry, PLAN, tmp_path, "/usr/games/cowsay", ["hi"], SandboxOptions(no_sandbox=True)
        )
        assert args == [str(entry.store_path("/usr/games/cowsay")), "hi"]


class TestProot:
    def test_offline_is_rejected(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        with pytest.raises(UnavailableError, match="offline"):
            build_proot_argv(
                entry, PLAN, tmp_path, "/usr/games/cowsay", [], SandboxOptions(offline=True)
            )

    def test_overlays_the_farm(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_proot_argv(entry, PLAN, stage_dir, "/usr/games/cowsay", ["hi"], SandboxOptions())
        assert args[0] == "proot"
        assert any(part.endswith(":/usr") for part in args)


class TestDescribe:
    def test_quotes_awkward_arguments(self):
        assert sandbox.describe(["echo", "a b"]) == "echo 'a b'"


class TestWritableHome:
    def test_default_is_ephemeral_tmpfs(self):
        assert SandboxOptions().writable_home is False

    def test_persist_makes_home_writable(self):
        assert SandboxOptions(persist="work").writable_home is True

    def test_ephemeral_makes_home_writable(self):
        assert SandboxOptions(ephemeral=True).writable_home is True


class TestChooseBackend:
    def test_explicit_none(self):
        assert sandbox.choose_backend("none") == "none"

    @pytest.mark.parametrize("alias", ["no", "off"])
    def test_none_aliases(self, alias):
        assert sandbox.choose_backend(alias) == "none"

    def test_bwrap_when_present(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: f"/usr/bin/{name}")
        assert sandbox.choose_backend("bwrap") == "bwrap"

    def test_bwrap_missing(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: None)
        with pytest.raises(UnavailableError, match="bwrap not found"):
            sandbox.choose_backend("bwrap")

    def test_proot_when_present(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: f"/usr/bin/{name}")
        assert sandbox.choose_backend("proot") == "proot"

    def test_proot_missing(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: None)
        with pytest.raises(UnavailableError, match="proot not found"):
            sandbox.choose_backend("proot")

    def test_auto_prefers_bwrap(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: f"/usr/bin/{name}")
        assert sandbox.choose_backend("auto") == "bwrap"

    def test_auto_falls_back_to_proot(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: "/usr/bin/proot" if name == "proot" else None)
        assert sandbox.choose_backend("auto") == "proot"

    def test_auto_without_any_backend(self, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: None)
        with pytest.raises(UnavailableError, match="no sandbox backend"):
            sandbox.choose_backend("auto")


class TestBwrapExtras:
    def test_direct_binds_are_read_only(self, tmp_path: Path):
        plan = Plan(pkg="p", version="1", direct_binds=["/opt/tool"])
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_bwrap_argv(entry, plan, stage_dir, "/x", [], SandboxOptions())
        assert binds(args)["/opt/tool"] == ("--ro-bind", str(entry.store_path("/opt/tool")))

    def test_gui_binds_the_runtime_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        args = argv_for(tmp_path, SandboxOptions(gui=True))
        assert binds(args)["/run/user/1000"] == ("--ro-bind-try", "/run/user/1000")

    def test_gui_moves_a_foreign_runtime_dir_to_the_standard_path(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/custom-runtime")
        args = argv_for(tmp_path, SandboxOptions(gui=True))
        assert binds(args)["/run/user/1000"] == ("--ro-bind-try", "/tmp/custom-runtime")

    def test_gui_binds_the_xauthority_cookie(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XAUTHORITY", "/tmp/xauth")
        args = argv_for(tmp_path, SandboxOptions(gui=True))
        assert binds(args)["/tmp/xauth"] == ("--ro-bind-try", "/tmp/xauth")

    def test_gui_without_a_runtime_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        args = argv_for(tmp_path, SandboxOptions(gui=True))
        assert "/run/user/1000" not in binds(args)

    def test_merge_link_skipped_when_the_node_is_present(self, tmp_path: Path):
        # /lib is a farm here, so the usrmerge symlink for it must not be made.
        plan = Plan(pkg="p", version="1", merge_points={"/lib": ["x86_64-linux-gnu"]})
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_bwrap_argv(entry, plan, stage_dir, "/x", [], SandboxOptions())
        links = [args[i + 2] for i, part in enumerate(args) if part == "--symlink"]
        assert "/lib" not in links
        assert "/bin" in links

    def test_persist_directory_creation_failure(self, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        monkeypatch.setattr(
            Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        with pytest.raises(UnavailableError, match="could not create"):
            build_bwrap_argv(
                entry, PLAN, stage_dir, "/x", [], SandboxOptions(persist="work")
            )

    def test_base_bind_is_skipped_when_a_node_covers_it(self, tmp_path: Path):
        plan = Plan(pkg="p", version="1", merge_points={"/usr": ["share"]})
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = build_bwrap_argv(entry, plan, stage_dir, "/x", [], SandboxOptions())
        assert "/usr" in binds(args)
        # /usr/lib must not be bound separately: /usr is a farm already.
        assert "/usr/lib" not in binds(args)

    def test_gui_skips_a_bind_whose_destination_is_a_farm_symlink(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        (stage_dir / "usr/share").mkdir(parents=True)
        (stage_dir / "usr/share/X11").symlink_to("/run/aptx/host/usr!share/X11")
        args = build_bwrap_argv(entry, PLAN, stage_dir, "/x", [], SandboxOptions(gui=True))
        # bwrap refuses to mount over a symlink; the farm symlink already points
        # at the host copy, so the bind is both illegal and redundant.
        assert "/usr/share/X11" not in binds(args)
        assert binds(args)["/usr/share/fonts"] == ("--ro-bind-try", "/usr/share/fonts")


class TestProotExtras:
    def make_args(self, tmp_path: Path, plan: Plan, opts: SandboxOptions) -> list[str]:
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir(exist_ok=True)
        return build_proot_argv(entry, plan, stage_dir, "/usr/games/cowsay", [], opts)

    def test_direct_binds(self, tmp_path: Path):
        plan = Plan(pkg="p", version="1", direct_binds=["/opt/tool"])
        args = self.make_args(tmp_path, plan, SandboxOptions())
        assert any(":/opt/tool" in part for part in args)

    def test_gui_binds(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        args = self.make_args(tmp_path, PLAN, SandboxOptions(gui=True))
        assert "/run/user/1000:/run/user/1000" in args

    def test_gui_moves_a_foreign_runtime_dir_to_the_standard_path(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/custom-runtime")
        args = argv_for(tmp_path, SandboxOptions(gui=True))
        assert binds(args)["/run/user/1000"] == ("--ro-bind-try", "/tmp/custom-runtime")

    def test_gui_binds_the_xauthority_cookie(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XAUTHORITY", "/tmp/xauth")
        args = argv_for(tmp_path, SandboxOptions(gui=True))
        assert binds(args)["/tmp/xauth"] == ("--ro-bind-try", "/tmp/xauth")

    def test_gui_without_a_runtime_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        args = self.make_args(tmp_path, PLAN, SandboxOptions(gui=True))
        assert "/run/user/1000:/run/user/1000" not in args

    def test_gpu_binds_only_existing_devices(self, tmp_path: Path):
        args = self.make_args(tmp_path, PLAN, SandboxOptions(gpu=True))
        assert any(":/dev/dri" in part for part in args) or not Path("/dev/dri").exists()

    def test_dev_binds(self, tmp_path: Path):
        args = self.make_args(tmp_path, PLAN, SandboxOptions(dev_binds=["/dev/snd"]))
        assert "/dev/snd:/dev/snd" in args

    def test_dev_bind_with_an_explicit_target(self, tmp_path: Path):
        args = self.make_args(tmp_path, PLAN, SandboxOptions(dev_binds=["/dev/snd:/other"]))
        assert "/dev/snd:/other" in args

    def test_named_persist_binds_the_shared_home(self, tmp_path: Path):
        args = self.make_args(tmp_path, PLAN, SandboxOptions(persist="work"))
        assert any(part.endswith("/homes/work:" + str(Path.home())) for part in args)

    def test_an_invalid_persistence_name_is_rejected(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        with pytest.raises(UsageError, match="invalid persistence name"):
            sandbox.persist_home(SandboxOptions(persist="../../etc"), entry)

    def test_proot_gui_binds_the_xauthority_cookie(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XAUTHORITY", "/tmp/xauth")
        args = self.make_args(tmp_path, PLAN, SandboxOptions(gui=True))
        assert "/tmp/xauth:/tmp/xauth" in args

    def test_bare_persist_binds_a_per_package_home(self, tmp_path: Path):
        args = self.make_args(tmp_path, PLAN, SandboxOptions(persist="default"))
        assert any("/state/default:" in part for part in args)

    def test_ephemeral_state_directory_is_created(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        build_proot_argv(entry, PLAN, stage_dir, "/x", [], SandboxOptions())
        assert (entry.state_dir / ".ephemeral").is_dir()

    def test_extra_writable_binds(self, tmp_path: Path):
        args = self.make_args(tmp_path, PLAN, SandboxOptions(rw=["/tmp/scratch:/scratch"]))
        assert "/tmp/scratch:/scratch" in args

    def test_rw_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = self.make_args(tmp_path, PLAN, SandboxOptions(rw_cwd=True))
        assert f"{tmp_path}:{tmp_path}" in args

    def test_cwd_under_proc_is_not_bound(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir("/proc")
        args = self.make_args(tmp_path, PLAN, SandboxOptions(rw_cwd=True))
        assert "-w" in args
        assert args[args.index("-w") + 1] == "/"


class TestHostFallback:
    def test_host_env_prepends_store_dirs(self, tmp_path: Path):
        entry = make_entry(tmp_path)
        (entry.path / "usr" / "bin").mkdir(parents=True)
        (entry.path / "usr" / "lib").mkdir(parents=True)
        env = sandbox.host_env(entry)
        assert env["PATH"].startswith(str(entry.path / "usr/bin"))
        assert str(entry.path / "usr/lib") in env["LD_LIBRARY_PATH"]

    def test_host_env_keeps_an_inherited_ld_path(self, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        (entry.path / "usr" / "lib").mkdir(parents=True)
        monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/libs")
        assert sandbox.host_env(entry)["LD_LIBRARY_PATH"].endswith("/opt/libs")

    def test_host_env_without_ld_path(self, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
        assert sandbox.host_env(entry)["LD_LIBRARY_PATH"] == ""

    def test_host_argv_points_into_the_store(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        target = entry.store_path("/usr/games/cowsay")
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        assert sandbox.host_argv(entry, "/usr/games/cowsay", ["hi"]) == [str(target), "hi"]

    def test_host_argv_rejects_a_missing_binary(self, tmp_path: Path):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        with pytest.raises(UnavailableError, match="is not in the store entry"):
            sandbox.host_argv(entry, "/usr/games/cowsay", [])

    def test_no_sandbox_warns_and_uses_the_store_binary(self, tmp_path: Path, capsys):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        target = entry.store_path("/usr/games/cowsay")
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = sandbox.build_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", ["hi"],
            SandboxOptions(no_sandbox=True),
        )
        assert args == [str(target), "hi"]
        assert "running without a sandbox" in capsys.readouterr().err

    def test_none_backend_warns_and_uses_the_store_binary(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        target = entry.store_path("/usr/games/cowsay")
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        monkeypatch.setattr(sandbox, "which", lambda name: None)
        args = sandbox.build_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", [], SandboxOptions(backend="none")
        )
        assert args == [str(target)]
        assert "no sandbox backend available" in capsys.readouterr().err

    def test_build_argv_dispatches_to_proot(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: "/usr/bin/proot")
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = sandbox.build_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", [], SandboxOptions(backend="proot")
        )
        assert args[0] == "proot"

    def test_build_argv_dispatches_to_bwrap(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox, "which", lambda name: "/usr/bin/bwrap")
        entry = make_entry(tmp_path, files=["/usr/games/cowsay"])
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        args = sandbox.build_argv(
            entry, PLAN, stage_dir, "/usr/games/cowsay", [], SandboxOptions(backend="bwrap")
        )
        assert args[0] == "bwrap"


class TestRun:
    def test_empty_argv_is_rejected(self):
        with pytest.raises(UnavailableError, match="empty command line"):
            sandbox.run([])

    def test_returns_the_child_status(self, monkeypatch):
        import subprocess
        from types import SimpleNamespace

        monkeypatch.setattr(
            subprocess, "run", lambda argv, check, env: SimpleNamespace(returncode=7)
        )
        assert sandbox.run(["true"]) == 7

    def test_keyboard_interrupt_maps_to_130(self, monkeypatch):
        import subprocess

        def boom(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(subprocess, "run", boom)
        assert sandbox.run(["true"]) == 130

    def test_missing_program_is_wrapped(self, monkeypatch):
        import subprocess

        def boom(*_args, **_kwargs):
            raise FileNotFoundError("nope")

        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(UnavailableError, match="cannot execute"):
            sandbox.run(["/nope"])

    def test_env_is_forwarded(self, monkeypatch):
        import subprocess
        from types import SimpleNamespace

        seen: dict = {}

        def capture(argv, check, env):
            seen["env"] = env
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(subprocess, "run", capture)
        sandbox.run(["true"], env={"A": "1"})
        assert seen["env"] == {"A": "1"}


class TestHome:
    def test_user_home(self, monkeypatch):
        monkeypatch.setattr(sandbox.os, "getuid", lambda: 1000)
        assert sandbox._home("jezv") == "/home/jezv"

    def test_root_home(self, monkeypatch):
        # `sudo` sets USER=root, and root's home is /root, not /home/root.
        monkeypatch.setattr(sandbox.os, "getuid", lambda: 0)
        assert sandbox._home("root") == "/root"

    def test_root_does_not_get_a_tmpfs_over_its_own_home(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox.os, "getuid", lambda: 0)
        monkeypatch.setattr(sandbox, "_user", lambda: "root")
        args = argv_for(tmp_path, SandboxOptions(persist="work"))
        assert "--tmpfs" not in [args[index] for index, part in enumerate(args) if part == "/root"]


class TestDevicePaths:
    def test_named_char_device_is_collected(self, monkeypatch):
        monkeypatch.setattr(sandbox, "_is_device", lambda path: path == "/dev/nvme1n1")
        assert sandbox.device_paths(["/dev/nvme1n1"]) == ["/dev/nvme1n1"]

    def test_duplicates_are_dropped(self, monkeypatch):
        monkeypatch.setattr(sandbox, "_is_device", lambda path: True)
        assert sandbox.device_paths(["/dev/null", "/dev/null"]) == ["/dev/null"]

    def test_unknown_node_is_ignored(self, monkeypatch):
        monkeypatch.setattr(sandbox, "_is_device", lambda path: False)
        assert sandbox.device_paths(["/dev/nope"]) == []

    def test_non_device_paths_are_ignored(self, monkeypatch):
        monkeypatch.setattr(sandbox, "_is_device", lambda path: True)
        assert sandbox.device_paths(["/tmp/x", "cowsay"]) == []


class TestIsDevice:
    def test_char_device(self, monkeypatch):
        monkeypatch.setattr(
            sandbox.os, "stat", lambda path: SimpleNamespace(st_mode=stat_mod.S_IFCHR | 0o660)
        )
        assert sandbox._is_device("/dev/null")

    def test_block_device(self, monkeypatch):
        monkeypatch.setattr(
            sandbox.os, "stat", lambda path: SimpleNamespace(st_mode=stat_mod.S_IFBLK | 0o660)
        )
        assert sandbox._is_device("/dev/sda")

    def test_regular_file(self, monkeypatch):
        monkeypatch.setattr(
            sandbox.os, "stat", lambda path: SimpleNamespace(st_mode=stat_mod.S_IFREG | 0o644)
        )
        assert not sandbox._is_device("/dev/notes")

    def test_missing_path(self):
        assert not sandbox._is_device("/dev/definitely-not-here")


class TestDevMountOrder:
    def test_proc_and_dev_come_before_device_binds(self, tmp_path: Path):
        args = argv_for(tmp_path, SandboxOptions(gpu=True, dev_binds=["/dev/fuse"]))
        assert args.index("--proc") < args.index("/dev/fuse")
        assert args.index("--dev") < args.index("/dev/fuse")

    def test_named_device_gets_bound_when_present(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox, "_is_device", lambda path: True)
        args = argv_for(tmp_path, argv=["smart-log", "/dev/nvme1n1"])
        assert binds(args)["/dev/nvme1n1"] == ("--dev-bind-try", "/dev/nvme1n1")

    def test_whole_dev_flag_binds_the_host_dev(self, tmp_path: Path):
        args = argv_for(tmp_path, SandboxOptions(dev=True))
        assert binds(args)["/dev"] == ("--dev-bind", "/dev")
        assert "--dev" not in args

    def test_whole_dev_flag_skips_individual_devices(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox, "_is_device", lambda path: True)
        args = argv_for(tmp_path, SandboxOptions(dev=True), argv=["/dev/nvme1n1"])
        assert "/dev/nvme1n1" not in binds(args)


class TestRootSandbox:
    """As root the sandbox must keep the capabilities a device ioctl needs."""

    def test_a_normal_user_gets_a_user_namespace(self, tmp_path: Path):
        assert "--unshare-user" in argv_for(tmp_path)

    def test_root_skips_the_user_namespace(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox.os, "geteuid", lambda: 0)
        assert "--unshare-user" not in argv_for(tmp_path)

    def test_root_keeps_its_capabilities(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sandbox.os, "geteuid", lambda: 0)
        args = argv_for(tmp_path, argv=["smart-log", "/dev/nvme1n1"])
        assert after(args, "--cap-add") == "ALL"

    def test_a_normal_user_drops_capabilities(self, tmp_path: Path):
        assert "--cap-add" not in argv_for(tmp_path)



class TestOptsArgv:
    """Flags are rendered back into CLI form for the shims `install` writes."""

    def test_no_flags_means_an_empty_argv(self):
        assert sandbox.opts_argv(SandboxOptions()) == []

    def test_every_flag_is_rendered(self):
        opts = SandboxOptions(
            offline=True,
            gpu=True,
            gui=True,
            persist="qgis",
            rw_cwd=True,
            dev=True,
            recommends=True,
            backend="proot",
            no_sandbox=True,
        )
        opts.rw = ["/a:/b"]
        opts.dev_binds = ["/dev/kvm"]
        assert sandbox.opts_argv(opts) == [
            "--offline",
            "--gpu",
            "--gui",
            "--persist=qgis",
            "--rw-cwd",
            "--rw=/a:/b",
            "--dev",
            "--dev-bind=/dev/kvm",
            "--recommends",
            "--backend=proot",
            "--no-sandbox",
        ]

    def test_the_auto_backend_is_left_out(self):
        assert sandbox.opts_argv(SandboxOptions(backend="auto")) == []

    def test_a_bare_persist_is_spelled_out(self):
        # A bare `--persist` would swallow the next word if a flag ever followed.
        assert sandbox.opts_argv(SandboxOptions(persist="default")) == ["--persist=default"]

    def test_staging_only_flags_are_dropped(self):
        # A shim replaying --force-stage would re-download the package every time.
        opts = SandboxOptions(force_stage=True, dry_run=True, ephemeral=True)
        assert sandbox.opts_argv(opts) == []

    def test_the_result_is_accepted_by_the_cli(self):
        from aptx import cli

        opts = SandboxOptions(gui=True, persist="work")
        assert cli.main(["run", *sandbox.opts_argv(opts), "--dry-run", "cowsay"]) != 2

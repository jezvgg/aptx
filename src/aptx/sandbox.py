"""Building the sandbox command line.

Two backends: ``bwrap`` (preferred, real isolation) and ``proot`` (fallback, no
namespaces). ``none`` runs the binary directly and is only useful for debugging.

The argv builder is pure — it takes everything it needs as arguments and returns a
list of strings — so the whole thing is testable without spawning a process.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from aptx import config, proc
from aptx.config import (
    BASE_RO_BINDS,
    DEFAULT_PATH,
    GPU_HARD_BINDS,
    GPU_SOFT_BINDS,
    GUI_BINDS,
    SANDBOX_BASE,
    SANDBOX_HOST,
    SANDBOX_ROOT,
    TMPFS_DIRS,
)
from aptx.errors import UnavailableError, UsageError
from aptx.plan import Plan
from aptx.proc import which
from aptx.stage import host_mount, node_tree, top_nodes
from aptx.store import Entry

#: usrmerge links every Debian system needs inside the sandbox.
MERGE_LINKS: list[tuple[str, str]] = [
    ("/bin", "usr/bin"),
    ("/sbin", "usr/sbin"),
    ("/lib", "usr/lib"),
    ("/lib64", "usr/lib64"),
]

ENV_PASSTHROUGH = ("LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ")


@dataclass
class SandboxOptions:
    backend: str = "auto"
    offline: bool = False
    gpu: bool = False
    gui: bool = False
    persist: str | None = None
    ephemeral: bool = False
    rw: list[str] = field(default_factory=list)
    rw_cwd: bool = False
    dev_binds: list[str] = field(default_factory=list)
    dev: bool = False
    no_sandbox: bool = False
    interactive: bool = False
    force_stage: bool = False
    dry_run: bool = False
    recommends: bool = False

    @property
    def writable_home(self) -> bool:
        return self.persist is not None or self.ephemeral


def _user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()


def _home(user: str) -> str:
    """Where ``$HOME`` lives inside the sandbox.

    ``sudo`` sets ``USER=root``, and root's home is ``/root``, not ``/home/root``.
    """
    if os.getuid() == 0:
        return "/root"
    return f"/home/{user}"


PERSIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def opts_argv(opts: SandboxOptions) -> list[str]:
    """Render *opts* back to CLI flags, in the order `aptx run` accepts them.

    `aptx install` writes these into the shim, so an installed package keeps
    running with the flags it was installed with (`--gui`, `--persist`, ...).
    Flags that only make sense while staging are deliberately left out: a shim
    carrying `--force-stage` would re-download the package on every start.
    """
    argv: list[str] = []
    if opts.offline:
        argv.append("--offline")
    if opts.gpu:
        argv.append("--gpu")
    if opts.gui:
        argv.append("--gui")
    if opts.persist is not None:
        # Always the explicit spelling: a bare `--persist` in a shim would
        # swallow the next word if a future flag ever follows it.
        argv.append(f"--persist={opts.persist}")
    if opts.rw_cwd:
        argv.append("--rw-cwd")
    argv.extend(f"--rw={spec}" for spec in opts.rw)
    if opts.dev:
        argv.append("--dev")
    argv.extend(f"--dev-bind={spec}" for spec in opts.dev_binds)
    if opts.recommends:
        argv.append("--recommends")
    if opts.backend != "auto":
        argv.append(f"--backend={opts.backend}")
    if opts.no_sandbox:
        argv.append("--no-sandbox")
    return argv


def persist_home(opts: SandboxOptions, entry: Entry) -> Path:
    """Directory that backs ``$HOME`` under ``--persist``.

    A bare ``--persist`` keeps a private home per package; ``--persist=NAME``
    is shared by every package that names the same directory, which is the
    whole point of giving it a name.
    """
    if opts.persist is None or opts.persist == "default":
        return entry.state_dir / "default"
    if not PERSIST_NAME_RE.match(opts.persist):
        raise UsageError(f"invalid persistence name: {opts.persist!r}")
    return config.homes_dir() / opts.persist


def device_paths(argv: list[str]) -> list[str]:
    """Host device nodes named on the program's own command line.

    ``aptx nvme-cli smart-log /dev/nvme1n1`` only works if ``/dev/nvme1n1`` is
    visible inside the sandbox, and the sandbox gets a fresh ``/dev``. A path the
    user already named is exactly the access they asked for, so it is bound in
    without an extra flag.
    """
    found: list[str] = []
    for token in argv:
        if not token.startswith("/dev/") or token in found:
            continue
        if _is_device(token):
            found.append(token)
    return found


def _is_device(path: str) -> bool:
    """True when *path* is a character or block device node on the host."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISCHR(mode) or stat.S_ISBLK(mode)


def sandbox_env(opts: SandboxOptions) -> dict[str, str]:
    user = _user()
    home = _home(user)
    env = {
        "PATH": DEFAULT_PATH,
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "SHELL": "/bin/sh",
        "TERM": os.environ.get("TERM") or "xterm-256color",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "LANG": "C.UTF-8",
        "PAGER": "cat",
        "DEBIAN_FRONTEND": "noninteractive",
    }
    for key in ENV_PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    if opts.gui:
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
            value = os.environ.get(key)
            if value:
                env[key] = value
    return env


def choose_backend(requested: str) -> str:
    """Resolve ``auto``/``bwrap``/``proot``/``none`` into a concrete backend."""
    if requested in ("none", "no", "off"):
        return "none"
    if requested == "bwrap":
        if not which("bwrap"):
            raise UnavailableError(
                "bwrap not found",
                hint="install bubblewrap, or use --backend proot / --no-sandbox",
            )
        return "bwrap"
    if requested == "proot":
        if not which("proot"):
            raise UnavailableError("proot not found", hint="install proot, or use --backend bwrap")
        return "proot"
    if which("bwrap"):
        return "bwrap"
    if which("proot"):
        return "proot"
    raise UnavailableError(
        "no sandbox backend available",
        hint="install bubblewrap (recommended) or proot, or pass --no-sandbox",
    )


_EXCLUDED_EXACT = {"", "/", "/tmp", "/run", "/proc", "/dev", "/sys"}
_EXCLUDED_PREFIX = ("/proc/", "/dev/", "/sys/")


def _cwd_in_sandbox(cwd: str) -> bool:
    """Can *cwd* be bound as-is?

    Pseudo-filesystems are out, and so are the bare roots of the directories the
    sandbox replaces with its own tmpfs — binding ``/tmp`` itself would hand the
    host's scratch space to the sandbox, while a subdirectory of it is fine.
    """
    if cwd in _EXCLUDED_EXACT:
        return False
    return not cwd.startswith(_EXCLUDED_PREFIX)


def _bind(spec: str) -> tuple[str, str]:
    host, _, target = spec.partition(":")
    return host, (target or host)


# --- bwrap ------------------------------------------------------------------


def build_bwrap_argv(
    entry: Entry,
    plan: Plan,
    stage_dir: Path,
    binpath: str,
    argv: list[str],
    opts: SandboxOptions,
) -> list[str]:
    user = _user()
    home = _home(user)
    cwd = os.getcwd()
    nodes = node_tree(plan)

    args = ["bwrap", "--die-with-parent", "--tmpfs", "/"]
    for directory in TMPFS_DIRS:
        args += ["--tmpfs", directory]
    # XDG_RUNTIME_DIR points here; a missing directory breaks every program that
    # mkstemp()s next to it.
    args += ["--dir", f"/run/user/{os.getuid()}"]

    # /proc and /dev come before anything mounted inside them: `--dev /dev`
    # installs a fresh devtmpfs and hides whatever was bound there earlier, so a
    # device bind issued before this line silently does nothing.
    args += ["--proc", "/proc"]
    if opts.dev:
        args += ["--dev-bind", "/dev", "/dev"]
    else:
        args += ["--dev", "/dev"]

    # The package closure, read-only, at its fixed sandbox path.
    args += ["--ro-bind", str(entry.path.resolve()), SANDBOX_ROOT]

    # Every node: the host directory (for farm symlinks) and, for top nodes, the
    # farm itself. Nested farms live physically inside their parent.
    for node in sorted(nodes):
        if Path(node).is_dir():
            args += ["--ro-bind", node, host_mount(node)]
    for node in top_nodes(nodes):
        args += ["--ro-bind", str(stage_dir / node.lstrip("/")), node]

    def covered(path: str) -> bool:
        return any(path == node or path.startswith(node.rstrip("/") + "/") for node in nodes)

    for source in BASE_RO_BINDS:
        if covered(source):
            continue
        args += ["--ro-bind-try", source, source]

    for path in plan.direct_binds:
        args += ["--ro-bind", str(entry.store_path(path)), path]

    if opts.gui:
        for source in GUI_BINDS:
            # The farm mirrors a host directory the package does not touch as a
            # symlink into /run/aptx/host, and bwrap refuses to mount over a
            # symlink destination ("Can't mount on symlink destination"). The
            # symlink already exposes exactly the host copy this bind would add,
            # so skipping it is equivalent and keeps `--gui` working.
            if (stage_dir / source.lstrip("/")).is_symlink():
                continue
            args += ["--ro-bind-try", source, source]
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            # Wayland sockets live here; a tmpfs at /run would hide them. The
            # sandbox always sees the conventional path, whatever the host uses.
            args += ["--ro-bind-try", runtime, f"/run/user/{os.getuid()}"]

    for link, target in MERGE_LINKS:
        if link in nodes:
            continue
        args += ["--symlink", target, link]

    # $HOME: ephemeral tmpfs by default, bound directory with --persist.
    if opts.persist is not None:
        state_dir = persist_home(opts, entry)
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UnavailableError(f"could not create {state_dir}: {exc}") from exc
        args += ["--bind", str(state_dir), home]
    else:
        args += ["--tmpfs", home]
    if home != "/root":
        args += ["--tmpfs", "/root"]

    if opts.gui:
        # Bound after $HOME: a cookie usually lives there, and the tmpfs above
        # would otherwise hide the mount.
        authority = os.environ.get("XAUTHORITY")
        if authority:
            args += ["--ro-bind-try", authority, authority]

    if _cwd_in_sandbox(cwd) and cwd != home:
        args += ["--bind" if opts.rw_cwd else "--ro-bind", cwd, cwd]

    for spec in opts.rw:
        host, target = _bind(spec)
        args += ["--bind", host, target]

    if opts.gpu:
        for source in GPU_HARD_BINDS:
            args += ["--dev-bind", source, source]
        for source in GPU_SOFT_BINDS:
            args += ["--dev-bind-try", source, source]
    for spec in opts.dev_binds:
        source, target = _bind(spec)
        args += ["--dev-bind-try", source, target]
    if not opts.dev:
        # `aptx nvme smart-log /dev/nvme1n1` should see the device it names.
        for path in device_paths(argv):
            args += ["--dev-bind-try", path, path]

    args += ["--clearenv"]
    for key, value in sandbox_env(opts).items():
        args += ["--setenv", key, value]

    args += ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if os.geteuid() == 0:
        # Root keeps its capability set. A device ioctl asks capable(), which
        # checks the *initial* user namespace, so a nested one would strip the
        # privilege however root we are; and root without CAP_DAC_OVERRIDE
        # cannot traverse another user's home either. bwrap drops every
        # capability by default, so they have to be asked for back.
        args += ["--cap-add", "ALL"]
    else:
        args += ["--unshare-user"]
    if opts.offline:
        args += ["--unshare-net"]
    if not opts.interactive:
        args += ["--new-session"]

    args += ["--chdir", cwd if _cwd_in_sandbox(cwd) else "/"]
    args += ["--", binpath, *argv]
    return args


# --- proot ------------------------------------------------------------------


def build_proot_argv(
    entry: Entry,
    plan: Plan,
    stage_dir: Path,
    binpath: str,
    argv: list[str],
    opts: SandboxOptions,
) -> list[str]:
    """proot cannot hide anything, so it only overlays.

    Weaker than bwrap: ``$HOME`` isolation is a bind, ``--offline`` is impossible
    (proot has no network namespace) and nothing is hidden from the process.
    """
    if opts.offline:
        raise UnavailableError(
            "--offline needs the bwrap backend",
            hint="proot cannot create a network namespace",
        )

    user = _user()
    home = _home(user)
    cwd = os.getcwd()
    nodes = node_tree(plan)

    args = ["proot", "-r", "/", "-w", cwd if _cwd_in_sandbox(cwd) else "/"]
    args += ["-b", f"{entry.path.resolve()}:{SANDBOX_ROOT}"]
    for node in sorted(nodes):
        if Path(node).is_dir():
            args += ["-b", f"{node}:{host_mount(node)}"]
    for node in top_nodes(nodes):
        args += ["-b", f"{stage_dir / node.lstrip('/')}:{node}"]
    for path in plan.direct_binds:
        args += ["-b", f"{entry.store_path(path)}:{path}"]

    if opts.gui:
        for source in GUI_BINDS:
            args += ["-b", f"{source}:{source}"]
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            args += ["-b", f"{runtime}:/run/user/{os.getuid()}"]
        authority = os.environ.get("XAUTHORITY")
        if authority:
            args += ["-b", f"{authority}:{authority}"]

    if opts.gpu:
        for source in GPU_HARD_BINDS + GPU_SOFT_BINDS:
            if Path(source).exists():
                args += ["-b", f"{source}:{source}"]
    for spec in opts.dev_binds:
        source, target = _bind(spec)
        args += ["-b", f"{source}:{target}"]

    if opts.persist is not None:
        state_dir = persist_home(opts, entry)
        state_dir.mkdir(parents=True, exist_ok=True)
        args += ["-b", f"{state_dir}:{home}"]
    else:
        args += ["-b", f"{entry.state_dir / '.ephemeral'}:{home}"]
        (entry.state_dir / ".ephemeral").mkdir(parents=True, exist_ok=True)

    for spec in opts.rw:
        host, target = _bind(spec)
        args += ["-b", f"{host}:{target}"]

    if opts.rw_cwd and _cwd_in_sandbox(cwd) and cwd != home:
        args += ["-b", f"{cwd}:{cwd}"]

    for key, value in sandbox_env(opts).items():
        args += ["--setenv", f"{key}={value}"]
    args += ["--"]
    args += [binpath, *argv]
    return args


#: Directories inside the store that become the ``PATH`` prefix without a sandbox.
HOST_PATH_DIRS = ("usr/bin", "usr/games", "usr/sbin", "usr/local/bin", "bin", "sbin")

#: Library directories prepended to ``LD_LIBRARY_PATH`` without a sandbox.
HOST_LIB_DIRS = ("usr/lib/x86_64-linux-gnu", "usr/lib", "lib/x86_64-linux-gnu", "lib")


def host_env(entry: Entry) -> dict[str, str]:
    """Environment for the unsandboxed fallback (SPEC.md 6.6).

    Without the sandbox the binary still resolves its libraries out of the store,
    so ``PATH`` and ``LD_LIBRARY_PATH`` have to point there explicitly.
    """
    prefix = [str(entry.path / part) for part in HOST_PATH_DIRS if (entry.path / part).is_dir()]
    libs = [str(entry.path / part) for part in HOST_LIB_DIRS if (entry.path / part).is_dir()]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(prefix + [env.get("PATH", DEFAULT_PATH)])
    inherited = env.get("LD_LIBRARY_PATH")
    env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + ([inherited] if inherited else []))
    return env


def host_argv(entry: Entry, binpath: str, argv: list[str]) -> list[str]:
    """The store copy of *binpath*, to be run without any isolation."""
    target = entry.store_path(binpath)
    if not target.exists():
        raise UnavailableError(
            f"{binpath} is not in the store entry for {entry.pkg}",
            hint="rebuild it with --force-stage",
        )
    return [str(target), *argv]


def build_argv(
    entry: Entry,
    plan: Plan,
    stage_dir: Path,
    binpath: str,
    argv: list[str],
    opts: SandboxOptions,
) -> list[str]:
    if opts.no_sandbox:
        proc.warn("running without a sandbox: paths are not live, only the store is used")
        return host_argv(entry, binpath, argv)
    backend = choose_backend(opts.backend)
    if backend == "none":
        proc.warn("no sandbox backend available: paths are not live")
        return host_argv(entry, binpath, argv)
    if backend == "proot":
        return build_proot_argv(entry, plan, stage_dir, binpath, argv, opts)
    return build_bwrap_argv(entry, plan, stage_dir, binpath, argv, opts)


def run(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Exec *argv*, returning the child's exit status (128+n on signal)."""
    if not argv:
        raise UnavailableError("empty command line")
    try:
        completed = subprocess.run(argv, check=False, env=env)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError as exc:
        raise UnavailableError(f"cannot execute {argv[0]}: {exc}") from exc
    return completed.returncode


def describe(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)

"""Command line entry point.

Argument convention (SPEC.md 3.2): aptx flags go *before* the package name;
everything after it belongs to the program being run.  Click encodes this with
``allow_interspersed_args=False`` -- option parsing stops at the first
positional -- so ``aptx run cowsay --dry-run`` hands ``--dry-run`` to cowsay.
``--`` is accepted and dropped, never required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from aptx import (
    __version__,
    config,
    doctor,
    errors,
    fetch,
    plan as plan_mod,
    proc,
    sandbox,
    shim,
    stage,
    store,
)
from aptx.errors import (
    AptxError,
    DataError,
    NoInputError,
    UnavailableError,
    UsageError,
)
from aptx.store import Entry

PROG = "aptx"

#: Rich draws tables and messages.  Errors stay plain text so their shape is
#: stable in scripts, pipes and tests.
CONSOLE = Console(highlight=False)

#: Option parsing stops at the first positional argument, so everything after
#: the package name reaches the program untouched.
PASSTHROUGH = {"ignore_unknown_options": True, "allow_interspersed_args": False}


class ShorthandGroup(click.Group):
    """``aptx cowsay hi`` means ``aptx run cowsay hi``."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if not args or args[0].startswith("-"):
                raise
            return "run", self.commands["run"], args


class SandboxCommand(click.Command):
    """A command where ``--persist`` takes an optional value.

    click can express ``--persist=NAME`` or ``--persist VALUE``, but not the
    "bare flag unless written with ``=``" form that this CLI promises. Left
    alone, ``aptx run --persist cowsay`` swallows the package name, so the bare
    spelling is rewritten to ``--persist=default`` before click parses.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        return super().parse_args(ctx, _normalise_persist(self, ctx, args))


def _normalise_persist(command: click.Command, ctx: click.Context, args: list[str]) -> list[str]:
    """Rewrite a bare ``--persist`` up to the first positional argument."""
    value_options = {
        name
        for param in command.get_params(ctx)
        if isinstance(param, click.Option) and not param.is_flag and param.nargs != 0
        for name in param.opts
    }
    out: list[str] = []
    index = 0
    parsing = True
    while index < len(args):
        token = args[index]
        index += 1
        if not parsing:
            out.append(token)
            continue
        if token == "--":
            out.append(token)
            parsing = False
            continue
        if token == "--persist":
            out.append("--persist=default")
            continue
        out.append(token)
        if token in value_options and index < len(args):
            out.append(args[index])
            index += 1
            continue
        if not token.startswith("-"):
            # The package: everything after it belongs to the program.
            parsing = False
    return out


SANDBOX_OPTIONS = [
    click.option("--offline", is_flag=True, help="no network namespace"),
    click.option("--gpu", is_flag=True, help="expose /dev/dri (and /dev/kfd when present)"),
    click.option("--gui", is_flag=True, help="expose X11/Wayland sockets and fonts"),
    click.option(
        "--persist",
        is_flag=False,
        flag_value="default",
        default=None,
        metavar="[=NAME]",
        help="keep $HOME between runs",
    ),
    click.option("--ephemeral", is_flag=True, help="throwaway store and $HOME, nothing kept"),
    click.option("--rw", multiple=True, metavar="HOST[:TARGET]", help="extra writable bind"),
    click.option("--rw-cwd", is_flag=True, help="make the current directory writable"),
    click.option(
        "--dev-bind",
        "dev_binds",
        multiple=True,
        metavar="SRC[:DST]",
        help="extra device bind (repeatable)",
    ),
    click.option("--dev", is_flag=True, help="expose the whole host /dev"),
    click.option("--recommends", is_flag=True, help="follow Recommends when resolving"),
    click.option(
        "--backend",
        type=click.Choice(["auto", "bwrap", "proot", "none"]),
        default="auto",
        show_default=True,
        help="sandbox backend",
    ),
    click.option("--no-sandbox", is_flag=True, help="run the store binary directly (debugging only)"),
    click.option("--force-stage", is_flag=True, help="stage even when installed on the host"),
    click.option("--dry-run", is_flag=True, help="print the command line, run nothing"),
]


def sandbox_options(command):
    """Apply every flag of :data:`SANDBOX_OPTIONS` to *command*."""
    for option in reversed(SANDBOX_OPTIONS):
        command = option(command)
    return command


# --- shared helpers ---------------------------------------------------------


def _opts_from(params: dict) -> sandbox.SandboxOptions:
    """Build :class:`SandboxOptions` from click's collected parameters."""
    opts = sandbox.SandboxOptions()
    for name in (
        "backend",
        "offline",
        "gpu",
        "gui",
        "persist",
        "ephemeral",
        "rw_cwd",
        "dev",
        "no_sandbox",
        "force_stage",
        "dry_run",
        "recommends",
    ):
        if name in params:
            setattr(opts, name, params[name])
    opts.rw = list(params.get("rw", ()))
    opts.dev_binds = list(params.get("dev_binds", ()))
    return opts


def _run_params(
    params: dict,
) -> tuple[sandbox.SandboxOptions, str, list[str], bool]:
    """Split click's parameters into (options, package, arguments, saw_dashdash)."""
    rest = [str(token) for token in params.get("args", ())]  # type: ignore[union-attr]
    explicit = rest[:1] == ["--"]
    if explicit:
        # `aptx run cowsay -- --help`: the separator is accepted and dropped.
        rest = rest[1:]
    pkg = str(params.get("pkg", ""))
    if pkg.startswith("-"):
        # `--persist` is parsed before this point, so a leading dash here is a
        # typo in a flag, not a package name.
        raise UsageError(f"no such option: {pkg}")
    return _opts_from(params), pkg, rest, explicit


def _backend_name(opts: sandbox.SandboxOptions) -> str:
    return "bwrap" if opts.backend in ("auto", "bwrap") else opts.backend


def _prepare(entry: Entry, opts: sandbox.SandboxOptions, force: bool = False):
    built = plan_mod.build(entry)
    stage_dir = stage.ensure(entry, built, force=force or opts.force_stage)
    return built, stage_dir


def _pick_bin(entry: Entry, spec: str | None) -> str:
    files = entry.files()
    if spec is not None:
        return entry.resolve_bin(spec)
    if len(files) == 1:
        return files[0]
    if not files:
        raise NoInputError(f"{entry.pkg} provides no executables")
    # Several binaries: the one named after the package is the obvious default
    # (`cowsay` ships cowsay, cowthink and cowsay_random). Anything else would
    # be a guess, so ask instead.
    same_name = [path for path in files if Path(path).name == entry.pkg]
    if same_name:
        return store.prefer_path(same_name)
    listing = "\n".join("  " + name for name in store.names(files))
    raise UsageError(
        f"{entry.pkg} provides several executables and none is named {entry.pkg}",
        hint="pick one explicitly:\n" + listing,
    )


def _matches_bin(entry: Entry, token: str) -> bool:
    files = entry.files()
    basename = Path(token).name
    return any(path == token or Path(path).name == basename for path in files)


def _staged(pkg: str, recommends: bool, force: bool, backend: str = "bwrap") -> Entry:
    # `store.build` is already idempotent; keeping a `get()` shortcut here would
    # skip its check that the cached closure matches the requested `--recommends`.
    return store.build(pkg, recommends=recommends, force=force, backend=backend)


def _with_ephemeral(callback):
    """Run *callback* with the whole cache redirected to a throwaway directory."""
    tmp = fetch.temp_dir("aptx-ephemeral-")
    config.set_cache_override(tmp)
    try:
        return callback()
    finally:
        config.set_cache_override(None)
        fetch.rmtree(tmp)


def _installed_on_host(pkg: str) -> bool:
    try:
        return fetch.installed_status([pkg]).get(pkg, "").startswith("ii")
    except AptxError:
        return False


def _unlink(path: Path) -> bool:
    """Remove *path*, reporting whether it actually went away."""
    try:
        path.unlink()
    except OSError:
        return False
    return True


_UNITS = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0, "s": 1.0}


def parse_duration(text: str) -> float:
    """``7d``/``24h``/``30m``/``2w`` (or a bare number of days) → seconds."""
    raw = text.strip().lower()
    if not raw:
        raise UsageError("--older-than needs a value", hint="examples: 30m, 24h, 7d, 2w")
    unit = raw[-1]
    if unit in _UNITS:
        amount, factor = raw[:-1], _UNITS[unit]
    else:
        amount, factor = raw, _UNITS["d"]
    try:
        value = float(amount)
    except ValueError as exc:
        raise UsageError(f"--older-than expects 7d/24h/30m/2w, got {text!r}") from exc
    return value * factor


# --- commands ---------------------------------------------------------------


@click.group(cls=ShorthandGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", count=True, help="more logging (repeatable)")
@click.option("-q", "--quiet", count=True, help="less logging")
@click.option("--no-color", is_flag=True, help="plain output, no ANSI escapes")
@click.version_option(__version__, "--version", prog_name=PROG, message=f"{PROG} %(version)s")
def cli(verbose: int, quiet: int, no_color: bool) -> None:
    """Run Debian packages without installing them.

    A package is downloaded into a store and executed in a bubblewrap sandbox
    where its files sit at their real paths (/usr/share/<pkg>, ...). Nothing is
    installed on the host and $HOME is left alone.

    `aptx <pkg> [args...]` is a shortcut for `aptx run <pkg> [args...]`:

    \b
        aptx cowsay hi
    """
    proc.set_verbose(verbose - quiet)
    if no_color:
        CONSOLE.no_color = True
        errors.set_color(False)


@cli.command(context_settings=PASSTHROUGH, cls=SandboxCommand)
@sandbox_options
@click.argument("pkg")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def run(**params: object) -> int:
    """Download (if needed) and run a package binary in a sandbox.

    A leading BIN may be a name, a relative path or an absolute path. Without
    one aptx uses the package's only executable, or the one named after the
    package.

    Every token after PKG goes to the program; aptx flags must come before it.

    \b
    examples:
      aptx run cowsay hi
      aptx cowsay hi
      aptx run --offline curl -sI https://example.com
      aptx run --persist cowsay cowthink moo
    """
    opts, pkg, rest, explicit = _run_params(params)
    if opts.persist is not None and opts.ephemeral:
        raise UsageError("--persist and --ephemeral are mutually exclusive")
    return _run(opts, pkg, rest, explicit)


def _run(
    opts: sandbox.SandboxOptions, pkg: str, rest: list[str], explicit: bool = False
) -> int:
    if not opts.force_stage and _installed_on_host(pkg):
        # SPEC.md 3.3: an installed package runs from the host unless asked otherwise.
        name = pkg
        if rest:
            candidate = Path(rest[0]).name
            if proc.which(candidate):
                name = candidate
        host = proc.which(name)
        if host:
            proc.note(
                f"{pkg} is already installed; running the host binary"
                " (use --force-stage to override)"
            )
            if rest and Path(rest[0]).name == name:
                rest = rest[1:]
            return sandbox.run([host, *rest])

    if opts.ephemeral:
        return _with_ephemeral(lambda: _run_staged(pkg, rest, opts, explicit))
    return _run_staged(pkg, rest, opts, explicit)


def _run_staged(
    pkg: str,
    rest: list[str],
    opts: sandbox.SandboxOptions,
    explicit: bool = False,
) -> int:
    entry = _staged(pkg, opts.recommends, opts.force_stage, _backend_name(opts))

    bin_spec: str | None = None
    if rest and not explicit and _matches_bin(entry, rest[0]):
        bin_spec, rest = rest[0], rest[1:]
    binpath = _pick_bin(entry, bin_spec)
    if not entry.store_path(binpath).exists():
        raise DataError(
            f"{entry.pkg} is damaged: {binpath} is missing from the store",
            hint=f"re-stage it with `aptx run --force-stage {entry.pkg}`",
        )

    built, stage_dir = _prepare(entry, opts)
    argv = sandbox.build_argv(entry, built, stage_dir, binpath, rest, opts)

    if opts.dry_run:
        print(sandbox.describe(argv))
        return 0

    entry.touch_used()
    proc.log(f"exec {sandbox.describe(argv)}")
    if opts.no_sandbox or opts.backend == "none":
        return sandbox.run(argv, env=sandbox.host_env(entry))
    return sandbox.run(argv)


@cli.command(context_settings=PASSTHROUGH, cls=SandboxCommand)
@sandbox_options
@click.argument("pkg")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def shell(**params: object) -> int:
    """Open /bin/sh inside the package sandbox.

    Every flag of `aptx run` works here. Everything after PKG goes to the shell.

    \b
    examples:
      aptx shell cowsay
      aptx shell cowsay -- -c 'echo $HOME'
    """
    opts, pkg, rest, _explicit = _run_params(params)
    opts.interactive = not rest

    def launch() -> int:
        entry = _staged(pkg, opts.recommends, opts.force_stage, _backend_name(opts))
        built, stage_dir = _prepare(entry, opts)
        argv = sandbox.build_argv(entry, built, stage_dir, "/bin/sh", rest, opts)
        if opts.dry_run:
            print(sandbox.describe(argv))
            return 0
        entry.touch_used()
        if opts.no_sandbox or opts.backend == "none":
            return sandbox.run(argv, env=sandbox.host_env(entry))
        return sandbox.run(argv)

    if opts.ephemeral:
        return _with_ephemeral(launch)
    return launch()


@cli.command()
@click.option("--force", is_flag=True, help="overwrite a file aptx did not create")
@click.argument("pkg")
@click.argument("bins", nargs=-1)
def install(force: bool, pkg: str, bins: tuple[str, ...]) -> int:
    """Stage PKG and write a shim per binary into the bin dir.

    Each shim re-enters aptx, so the program always runs sandboxed. Without
    BIN the default binary is used; pass one name per wanted shim.

    \b
    examples:
      aptx install cowsay
      aptx install fio fio fio-genzipf
    """
    entry = _staged(pkg, False, False)
    wanted = list(bins) or [None]
    _prepare(entry, sandbox.SandboxOptions(), force=force)

    for spec in wanted:
        binpath = _pick_bin(entry, spec)
        target = shim.install(entry, binpath, force=force)
        print(f"installed {Path(binpath).name} -> {target}")
    print(f"  remove with: {PROG} remove {pkg}")
    return 0


@cli.command()
@click.option("--all", "all_", is_flag=True, help="remove every staged package")
@click.argument("pkgs", nargs=-1)
def remove(all_: bool, pkgs: tuple[str, ...]) -> int:
    """Delete a package's shims and its store entry.

    Only shim files carrying the aptx marker are touched.

    \b
    examples:
      aptx remove cowsay
      aptx remove cowsay moreutils
      aptx remove --all
    """
    if all_:
        if pkgs:
            raise UsageError("--all takes no package names")
        return _rm_all()
    if not pkgs:
        raise UsageError(f"{PROG} remove needs a package name or --all")

    for pkg in pkgs:
        entry = store.get(pkg)
        if entry is None:
            raise NoInputError(f"{pkg} is not staged")
        removed = shim.remove_all(entry)
        store.remove(pkg)
        if removed:
            print(f"removed {pkg} and {len(removed)} shim(s): {', '.join(removed)}")
        else:
            print(f"removed {pkg}")
    return 0


@cli.command("list")
def list_packages() -> int:
    """Show staged packages, orphan shims and unshimmed packages."""
    entries = store.list_entries()
    if not entries:
        print("no staged packages")
        return 0

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("PACKAGE")
    table.add_column("VERSION")
    table.add_column("SIZE", justify="right")
    table.add_column("BINS")
    table.add_column("SHIMS", justify="right")

    shims: dict[str, int] = {}
    for entry in entries:
        files = entry.files()
        names = ", ".join(Path(path).name for path in files[:4])
        if len(files) > 4:
            names += ", …"
        count = len(shim.list_shims(entry))
        shims[entry.pkg] = count
        table.add_row(
            entry.pkg, entry.version(), proc.human_size(entry.size()), names, str(count)
        )
    CONSOLE.print(table)

    orphans = shim.orphan_shims(set(shims))
    if orphans:
        print("\norphan shims:")
        for path in orphans:
            print(f"  {path.name}  (package gone; `{PROG} gc` removes it)")
    unshimmed = [pkg for pkg, count in shims.items() if count == 0]
    if unshimmed:
        print("\nunshimmed:")
        for pkg in unshimmed:
            print(f"  {pkg}  (`{PROG} install {pkg}`)")
    return 0


@cli.command()
@click.argument("pkg")
def ls(pkg: str) -> int:
    """Show the bind/merge plan and executables of PKG, staging it if needed."""
    entry = _staged(pkg, False, False)
    print(plan_mod.render(plan_mod.build(entry)))
    files = entry.files()
    if files:
        print(f"executables ({len(files)}):")
        for path in files:
            print(f"  {path}")
    closure = entry.deps()
    if closure:
        print(f"closure ({len(closure)} packages):")
        for name, version, _arch, _sha in closure[:20]:
            print(f"  {name} {version}")
        if len(closure) > 20:
            print(f"  … and {len(closure) - 20} more")
    return 0


@cli.command()
@click.argument("pkg")
@click.argument("bin_name", metavar="[BIN]", required=False)
def which(pkg: str, bin_name: str | None) -> int:
    """Print the store path of BIN inside the staged PKG. Runs nothing."""
    entry = store.get(pkg)
    if entry is None:
        raise NoInputError(f"{pkg} is not staged", hint=f"run `{PROG} ls {pkg}` to stage it")
    print(str(entry.store_path(_pick_bin(entry, bin_name))))
    return 0


@cli.command()
@click.argument("pkg")
def plan(pkg: str) -> int:
    """Show shadow directories, merge points, direct binds and conflicts."""
    entry = _staged(pkg, False, False)
    print(plan_mod.render(plan_mod.build(entry)))
    return 0


@cli.group(name="store", invoke_without_command=True, no_args_is_help=False)
@click.pass_context
def store_group(ctx: click.Context) -> None:
    """Inspect the store: path | size | du."""
    if ctx.invoked_subcommand is None:
        raise UsageError(f"{PROG} store needs a subcommand", hint="one of: path, size, du")


@store_group.command("path")
def store_path() -> None:
    """Print the store directory."""
    print(config.store_dir())


@store_group.command("size")
def store_size() -> None:
    """Print the total size of the store."""
    print(proc.human_size(sum(size for _pkg, size in store.du())))


@store_group.command("du")
def store_du() -> None:
    """Per-package sizes, largest first."""
    rows = store.du()
    if not rows:
        print("store is empty")
        return
    width = max(len(pkg) for pkg, _size in rows)
    for pkg, size in rows:
        print(f"{pkg.ljust(width)}  {proc.human_size(size).rjust(7)}")


@cli.command()
@click.option(
    "--older-than",
    default="7d",
    show_default=True,
    metavar="DURATION",
    help="drop entries unused for this long (30m, 24h, 7d, 2w)",
)
def gc(older_than: str) -> int:
    """Remove store entries not used recently, plus any dangling shims."""
    removed = store.gc(parse_duration(older_than))
    known = {entry.pkg for entry in store.list_entries()}
    orphans = [path for path in shim.orphan_shims(known) if _unlink(path)]
    if removed:
        print(f"removed {len(removed)} package(s): {', '.join(removed)}")
    if orphans:
        print(f"removed {len(orphans)} dangling shim(s)")
    if not removed and not orphans:
        print("nothing to collect")
    return 0


def _rm_all() -> int:
    known = {entry.pkg for entry in store.list_entries()}
    # Every managed shim goes, including the ones whose package already vanished.
    managed = list(shim.orphan_shims(known))
    for entry in store.list_entries():
        managed.extend(shim.list_shims(entry))
    removed = 0
    for path in sorted(set(managed)):
        if _unlink(path):
            removed += 1
    store.rm_all()
    print(f"removed the store and {removed} shim(s)")
    return 0


@cli.command("rm-all")
def rm_all() -> int:
    """Remove the whole cache (store, pins, metadata) and every aptx shim."""
    return _rm_all()


@cli.command()
@click.option("--clear", is_flag=True, help="drop the pin")
@click.argument("spec")
@click.argument("version", required=False)
def pin(clear: bool, spec: str, version: str | None) -> int:
    """Pin PKG=VERSION so staging downloads exactly that build.

    \b
    examples:
      aptx pin cowsay=3.03+dfsg2-8
      aptx pin cowsay --clear
    """
    pkg, _, inline = spec.partition("=")
    if clear:
        store.set_pin(pkg, None)
        print(f"unpinned {pkg}")
        return 0

    wanted = inline or version or ""
    if not wanted:
        entry = store.get(pkg)
        if entry is None:
            raise NoInputError(
                f"{pkg} is not staged; pass a version explicitly",
                hint=f"{PROG} pin {pkg}=<version>",
            )
        wanted = entry.version()
    store.set_pin(pkg, wanted)
    print(f"pinned {pkg}={wanted}")
    return 0


@cli.command()
@click.argument("pkg")
def unpin(pkg: str) -> int:
    """Drop the version pin for PKG (same as `aptx pin PKG --clear`)."""
    store.set_pin(pkg, None)
    print(f"unpinned {pkg}")
    return 0


@cli.command("doctor")
def check_environment() -> int:
    """Check python, bwrap, user namespaces, apt/dpkg, store, bin dir and GPU.

    A failed critical check exits with 69.
    """
    checks = doctor.run_checks()
    print(doctor.render(checks))
    if doctor.failed(checks):
        raise UnavailableError(
            "the environment cannot run aptx",
            hint="fix the checks marked '!' above",
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        # A bare `aptx` is a help request, not an error.
        args = ["--help"]
    try:
        result = cli.main(args=args, prog_name=PROG, standalone_mode=False)
    except click.exceptions.Abort:
        return 130
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except AptxError as exc:
        print(exc.render(), file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    return result if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

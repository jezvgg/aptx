"""Command shims.

``aptx install`` does not symlink the store binary into ``~/.local/bin``: a
symlink would run the binary with host paths, which is exactly the v1 behaviour
aptx exists to fix. Instead we write a tiny shell script that re-enters aptx, so
every invocation goes through the sandbox.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from aptx import proc
from aptx.config import bin_dir
from aptx.errors import CantCreateError, UsageError
from aptx.store import Entry

MARKER = "# managed by aptx"


def aptx_command() -> str:
    """How to re-enter aptx from a shim."""
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.suffix != ".py" and proc.is_executable(argv0):
        return shlex.quote(str(argv0))
    return f"{shlex.quote(sys.executable)} -m aptx"


def shim_text(entry: Entry, binpath: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{MARKER} - remove with `aptx remove {entry.pkg}`\n"
        f'exec {aptx_command()} run {shlex.quote(entry.pkg)}'
        f' {shlex.quote(binpath)} -- "$@"\n'
    )


def is_managed(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readline().startswith("#!/bin/sh") and MARKER in (
                handle.readline() + handle.readline()
            )
    except OSError:
        return False


def shim_path(name: str) -> Path:
    """Where the shim for executable *name* lives.

    Only a bare basename is ever accepted: the name comes from a package
    manifest, and a ``..`` there must never escape ``~/.local/bin``.
    """
    base = Path(name).name
    if not base or base in (".", ".."):
        raise UsageError(f"invalid executable name: {name!r}")
    return bin_dir() / base


def install(entry: Entry, binpath: str, force: bool = False) -> Path:
    target = shim_path(binpath)
    if target.exists() and not is_managed(target) and not force:
        raise CantCreateError(
            f"{target} already exists and was not created by aptx",
            hint="pass --force to overwrite it",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shim_text(entry, binpath), encoding="utf-8")
        target.chmod(0o755)
    except OSError as exc:
        raise CantCreateError(f"could not write {target}: {exc}") from exc
    return target


def remove(entry: Entry, binpath: str) -> bool:
    target = shim_path(binpath)
    if not is_managed(target):
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


def remove_all(entry: Entry) -> list[str]:
    removed = []
    for binpath in entry.files():
        if remove(entry, binpath):
            removed.append(Path(binpath).name)
    return removed


def list_shims(entry: Entry) -> list[Path]:
    found = []
    for binpath in entry.files():
        target = shim_path(binpath)
        if is_managed(target):
            found.append(target)
    return found


def shim_package(path: Path) -> str:
    """The package a shim re-enters aptx for, or ``""`` if it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if MARKER not in line:
            continue
        _head, _, tail = line.partition("aptx remove ")
        if tail:
            return tail.strip().split()[0].strip("`")
    return ""


def orphan_shims(known_packages: set[str]) -> list[Path]:
    """Shims in ``~/.local/bin`` that point at a package no longer in the store."""
    directory = bin_dir()
    if not directory.is_dir():
        return []
    orphans = []
    for child in sorted(directory.iterdir()):
        if not child.is_file() or not is_managed(child):
            continue
        pkg = shim_package(child)
        if pkg and pkg not in known_packages:
            orphans.append(child)
    return orphans

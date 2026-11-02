"""Resolving, downloading and unpacking Debian packages.

Everything here shells out to the stock apt/dpkg tooling: no root is needed,
``apt-get download`` writes into a temporary directory and ``dpkg -x`` unpacks
into ours.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from aptx import proc
from aptx.config import APT_RELATIONS_WANTED
from aptx.errors import DataError, OsError_, UnavailableError

# `  Depends: perl`, `  |Depends: foo`, `  PreDepends: libc6`
_RELATION_RE = re.compile(
    r"^\s*\|?(?P<rel>Depends|PreDepends|Recommends|Suggests|Conflicts|Breaks|Replaces|Enhances):\s*(?P<target>.+?)\s*$"
)
_ARCH_SUFFIX_RE = re.compile(r":[a-zA-Z0-9-]+$")


@dataclass(frozen=True)
class DebPath:
    """One entry of ``dpkg-deb -c``."""

    kind: str  # 'd', 'f' or 'l'
    path: str  # absolute, no leading './'
    target: str | None = None  # symlink target
    mode: str = ""  # full mode string, e.g. '-rwxr-xr-x'

    @property
    def executable(self) -> bool:
        """True when the file carries any execute bit (dpkg's ``-..x``)."""
        if self.kind != "f" or len(self.mode) < 10:
            return False
        return self.mode[3] in "xs" or self.mode[6] in "xs" or self.mode[9] in "xt"


def _strip_arch(name: str) -> str:
    return _ARCH_SUFFIX_RE.sub("", name)


def resolve_closure(pkg: str, *, recommends: bool = False) -> list[str]:
    """Return the full dependency closure of *pkg* (including *pkg* itself).

    Only ``Depends``/``PreDepends`` (and ``Recommends`` when asked) are followed:
    ``apt-cache depends --recurse`` also prints Conflicts/Breaks/Replaces, and
    pulling those in would be wrong.
    """
    argv = [
        "apt-cache",
        "depends",
        "--recurse",
        "--no-suggests",
        "--no-conflicts",
        "--no-breaks",
        "--no-replaces",
        "--no-enhances",
    ]
    if not recommends:
        argv.append("--no-recommends")
    argv.append(pkg)

    cp = proc.run(argv, check=False)
    if cp.returncode != 0:
        raise DataError(
            f"package not found: {pkg}",
            hint="check the name with `apt-cache search`, or run `apt-get update`",
        )

    wanted = set(APT_RELATIONS_WANTED)
    if recommends:
        wanted.add("Recommends")

    names: set[str] = set()
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        match = _RELATION_RE.match(line)
        if match:
            if match.group("rel") not in wanted:
                continue
            target = match.group("target").strip()
            if target.startswith("<"):  # virtual package, providers follow
                continue
            names.add(_strip_arch(target))
            continue
        if line[0].isspace():
            # 4-space indented provider of a virtual package
            provider = line.strip()
            if provider.startswith("<") or not provider:
                continue
            names.add(_strip_arch(provider))
            continue
        # column 0: block header = the package itself. Virtual packages get
        # their own block too (``<perlapi-5.40.0>``) and are never downloadable.
        header = line.strip()
        if header.startswith("<"):
            continue
        names.add(_strip_arch(header))

    names.discard("")
    if pkg not in names:
        names.add(pkg)
    return sorted(names)


def installed_status(names: list[str]) -> dict[str, str]:
    """Map package name -> dpkg status abbreviation (``ii``, ``rc``, ...)."""
    if not names:
        return {}
    cp = proc.run(
        ["dpkg-query", "-W", "-f=${db:Status-Abbrev}\\t${Package}\\n", *names],
        check=False,
    )
    result: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        status, name = parts
        result[name.strip()] = status.strip()
    return result


#: Downloads report on stderr so a piped stdout stays clean.
CONSOLE = Console(highlight=False, stderr=True)


def download_sizes(specs: list[str]) -> dict[str, int]:
    """Expected size per file for *specs*, from ``apt-get download --print-uris``.

    ``apt-get`` prints ``'<uri>' <file> <bytes> <hash>`` per package, which is
    the only cheap way to know the total before anything is transferred.
    """
    argv = ["apt-get", "download", "--print-uris", "-y", "--", *specs]
    cp = proc.run(argv, check=False)
    if cp.returncode != 0:
        return {}
    sizes: dict[str, int] = {}
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            sizes[parts[1].strip("'")] = int(parts[2])
        except ValueError:
            continue
    return sizes


def _downloaded(directory: Path) -> int:
    """Bytes already written into *directory* by a running ``apt-get``."""
    total = 0
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return 0
    for item in entries:
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


_POLL_SECONDS = 0.15


def _run_with_progress(
    argv: list[str], dest: Path, progress: Progress, task: TaskID
) -> subprocess.CompletedProcess[str]:
    """Run *argv* in *dest* while feeding *progress* with the growing size.

    ``apt-get`` writes each .deb straight into the target directory, so the
    bytes on disk are an honest measure of transfer progress.  Output goes to a
    temporary file rather than a pipe: nothing reads the pipe while we poll, and
    a full pipe would deadlock the child.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as sink:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv, cwd=str(dest), stdout=sink, stderr=subprocess.STDOUT, text=True
        )
        while child.poll() is None:
            progress.update(task, completed=_downloaded(dest))
            time.sleep(_POLL_SECONDS)
        progress.update(task, completed=_downloaded(dest))
        sink.seek(0)
        output = sink.read()
    return subprocess.CompletedProcess(argv, child.returncode, output, "")


def download(specs: list[str], dest: Path) -> None:
    """``apt-get download`` a list of ``name`` or ``name=version`` specs."""
    if not specs:
        return
    argv = ["apt-get", "download", "--", *specs]
    total = sum(download_sizes(specs).values())
    cp = _download_with_bar(argv, dest, total, len(specs))
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise UnavailableError(
            "apt-get download failed" + (f": {detail.splitlines()[-1]}" if detail else ""),
            hint="no network? run `apt-get update`, or use a package that is already staged",
        )


def _download_with_bar(
    argv: list[str], dest: Path, total: int, count: int
) -> subprocess.CompletedProcess[str]:
    """Wrap :func:`_run_with_progress` in a rich progress bar."""
    label = f"downloading {count} package{'s' if count != 1 else ''}"
    columns = [
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ]
    # No total (offline apt metadata) means an indeterminate bar is not worth it:
    # fall back to a plain call and keep the output clean.
    if total <= 0:
        return proc.run(argv, check=False, cwd=str(dest))
    with Progress(*columns, console=CONSOLE, transient=False) as progress:
        task = progress.add_task(label, total=total)
        return _run_with_progress(argv, dest, progress, task)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deb_fields(deb: Path, *fields: str) -> dict[str, str]:
    """Read control fields from a .deb.

    ``dpkg-deb -f`` prints bare values for a single field but ``Field: value``
    lines when several are asked for, so we always read the whole control block
    and parse it uniformly.
    """
    cp = proc.run(["dpkg-deb", "-f", str(deb)], check=True)
    parsed: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        parsed[key.strip()] = value.strip()
    return {field: parsed.get(field, "") for field in fields}


def deb_contents(deb: Path) -> list[DebPath]:
    """Parse ``dpkg-deb -c`` into structured entries."""
    cp = proc.run(["dpkg-deb", "-c", str(deb)], check=True)
    entries: list[DebPath] = []
    for line in cp.stdout.splitlines():
        if not line or line[0] not in "dflcbps-":
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        mode, _owner, _size, _date, _time, name = parts
        if name == "./" or name == ".":
            continue
        target = None
        if " -> " in name:
            name, target = name.rsplit(" -> ", 1)
        path = name[2:] if name.startswith("./") else name
        path = path.rstrip("/")
        if not path:
            continue
        if not path.startswith("/"):
            path = "/" + path
        kind = mode[0] if mode[0] in "dl" else "f"
        entries.append(DebPath(kind=kind, path=path, target=target, mode=mode))
    return entries


def unpack(deb: Path, root: Path) -> None:
    cp = proc.run(["dpkg", "-x", str(deb), str(root)], check=False)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise OsError_(
            f"could not unpack {deb.name}"
            + (f": {detail.splitlines()[-1]}" if detail else ""),
        )


def temp_dir(prefix: str = "aptx-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def rmtree(path: Path) -> None:
    """Remove *path* if it is there.

    Best effort on purpose: cleanup runs on error paths and after a failed
    build, where a leftover directory must not mask the real failure.
    """
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        proc.warn(f"could not remove {path}: {exc}")

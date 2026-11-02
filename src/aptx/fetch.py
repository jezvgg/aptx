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
from dataclasses import dataclass, field
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
# The `|` marks "another alternative follows", so it is captured: `a | b` must not
# stage both, they are builds of the same files (libqt5gui5t64 vs libqt5gui5-gles).
_RELATION_RE = re.compile(
    r"^\s*(?P<alt>\|)?(?P<rel>Depends|PreDepends|Recommends|Suggests|Conflicts|Breaks|Replaces|Enhances):\s*(?P<target>.+?)\s*$"
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

    # ``apt-cache depends --recurse`` prints one block per package it walked
    # (``name`` at column 0, relations indented) and one block per virtual
    # package (``<name>``, providers indented). It prints a block for *every*
    # alternative it walked too, including the branch apt would not install, so
    # collecting the lines flat would pull in e.g. libqt5gui5-gles: a second
    # build of the same files, which then shadows the host's libqt5gui5t64 and
    # breaks PyQt5 (``undefined symbol ... version Qt_5``). The closure is
    # therefore re-walked from *pkg* below, choosing one alternative per group.
    blocks: dict[str, _Block] = {}
    virtual: dict[str, set[str]] = {}
    current = ""  # the block header relations below belong to
    current_virtual = False
    pending: list[str] = []

    def block(name: str) -> _Block:
        return blocks.setdefault(name, _Block())

    def flush() -> None:
        nonlocal pending
        if pending and current:
            block(current).any.append(pending)
        pending = []

    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        match = _RELATION_RE.match(line)
        if match:
            if not current or match.group("rel") not in wanted:
                continue
            entry = block(current)
            target = match.group("target").strip()
            if target.startswith("<"):  # satisfied by the virtual's providers
                entry.virtual.add(target)
                continue
            name = _strip_arch(target)
            if match.group("alt"):  # `a | b`: b follows on the next line
                pending.append(name)
                continue
            if pending:
                entry.any.append(pending + [name])
                pending = []
            else:
                entry.need.add(name)
            continue
        if line[0].isspace():
            # A provider of the virtual package the previous line named.
            provider = _strip_arch(line.strip())
            if not provider or provider.startswith("<") or not current:
                continue
            if current_virtual:
                virtual.setdefault(current, set()).add(provider)
            else:
                block(current).providers.add(provider)
            continue
        # column 0: a block header, i.e. the package apt-cache walked into.
        flush()
        header = line.strip()
        current_virtual = header.startswith("<")
        if current_virtual:
            current = header
            virtual.setdefault(header, set())
            continue
        current = _strip_arch(header)
        block(current)
    flush()

    candidates = sorted({name for entry in blocks.values() for group in entry.any for name in group})
    status = installed_status(candidates)

    def choose(group: list[str]) -> str:
        """Pick the alternative apt itself would be satisfied by.

        The installed one wins: it is already on the host, so nothing gets bound
        over it. Falling back to the first is what ``apt-get`` does when neither
        is installed.
        """
        installed = [name for name in group if status.get(name, "").startswith("ii")]
        return installed[0] if installed else group[0]

    names: set[str] = {pkg}
    stack = [pkg]
    while stack:
        entry = blocks.get(stack.pop())
        if entry is None:
            continue
        reached: set[str] = set(entry.need) | set(entry.providers)
        for group in entry.any:
            reached.add(choose(group))
        for target in entry.virtual:
            reached |= virtual.get(target, set())
        reached -= names
        names |= reached
        stack.extend(reached)

    names.discard("")
    return sorted(names)


@dataclass
class _Block:
    """Dependencies of one package, as ``apt-cache depends`` groups them.

    ``need`` holds mandatory names, ``any`` the ``a | b`` groups (one member is
    chosen), ``virtual`` the ``<virtual>`` targets whose providers satisfy them
    and ``providers`` the providers printed under a plain package block.
    """

    need: set[str] = field(default_factory=set)
    any: list[list[str]] = field(default_factory=list)
    virtual: set[str] = field(default_factory=set)
    providers: set[str] = field(default_factory=set)


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

"""The store: one directory per package, holding an unpacked Debian root.

Layout of a store entry (see SPEC.md section 4)::

    <store>/cowsay/
        .ready          marker, written last
        .meta.json      metadata
        .deps.tsv       closure
        .provides.tsv   every path of the whole closure
        .files.tsv      executables of the requested package
        .stage-hash     symlink-farm cache key
        .last-used      mtime bumped on every use
        stage/          symlink farm cache
        state/          --persist
        usr/ etc/ ...   unpacked root
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from aptx import fetch, proc
from aptx.config import cache_dir, pins_file, store_dir
from aptx.errors import CantCreateError, DataError, NoInputError, SoftwareError, UsageError
from aptx import __version__

READY = ".ready"
META = ".meta.json"
DEPS = ".deps.tsv"
PROVIDES = ".provides.tsv"
FILES = ".files.tsv"
STAGE_HASH = ".stage-hash"
LAST_USED = ".last-used"
STAGE = "stage"
STATE = "state"

@dataclass
class Entry:
    pkg: str
    path: Path

    # -- status ------------------------------------------------------------
    @property
    def stage_dir(self) -> Path:
        return self.path / STAGE

    @property
    def state_dir(self) -> Path:
        return self.path / STATE

    # -- manifest ----------------------------------------------------------
    def meta(self) -> dict:
        try:
            return json.loads((self.path / META).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SoftwareError(f"corrupt manifest for {self.pkg}: {exc}") from exc

    def provides(self) -> list[tuple[str, str, str]]:
        """``(kind, path, package)`` for every path of the closure."""
        return _read_tsv(self.path / PROVIDES, 3)  # type: ignore[return-value]

    def files(self) -> list[str]:
        """Executables provided by the requested package."""
        return [row[0] for row in _read_tsv(self.path / FILES, 1)]

    def deps(self) -> list[tuple[str, str, str, str]]:
        return _read_tsv(self.path / DEPS, 4)  # type: ignore[return-value]

    def version(self) -> str:
        try:
            return self.meta().get("version", "?")
        except SoftwareError:
            return "?"

    def touch_used(self) -> None:
        marker = self.path / LAST_USED
        try:
            marker.touch()
        except OSError:
            pass

    def size(self) -> int:
        return dir_size(self.path)

    def store_path(self, relative: str) -> Path:
        """Absolute path of *relative* inside the unpacked package root.

        Manifests and argv both reach this, so ``..`` is rejected rather than
        normalised: a store path must never address anything outside the entry.
        """
        parts = [part for part in relative.split("/") if part not in ("", ".")]
        if ".." in parts:
            raise DataError(f"path escapes the store entry: {relative!r}")
        return self.path.joinpath(*parts)

    def resolve_bin(self, name: str) -> str:
        """Resolve a binary spec (name, relative or absolute path) to a store path."""
        files = self.files()
        if not files:
            raise NoInputError(
                f"package {self.pkg} has no executables",
                hint="run `aptx ls %s` to see what it provides" % self.pkg,
            )
        if ".." in Path(name).parts:
            raise UsageError(f"invalid executable name: {name!r}")
        candidate = name if name.startswith("/") else "/" + name.lstrip("./")
        if candidate in files:
            return candidate
        basename = Path(candidate).name
        matches = [f for f in files if Path(f).name == basename]
        if not matches:
            raise NoInputError(
                f"{self.pkg} has no executable named {name!r}",
                hint="available: " + ", ".join(names(files)),
            )
        return prefer_path(matches)


def prefer_path(paths: list[str]) -> str:
    """Pick one of *paths* deterministically: shallowest first, then alphabetical.

    ``git`` ships both ``/usr/bin/git`` and ``/usr/lib/git-core/git``; the one in a
    real ``bin`` directory is the entry point, and it is also the shorter path.
    """
    return min(paths, key=lambda path: (len(Path(path).parts), path))


def names(paths: list[str]) -> list[str]:
    """Basenames of *paths*, without duplicates and in a stable order."""
    seen: list[str] = []
    for path in paths:
        name = Path(path).name
        if name not in seen:
            seen.append(name)
    return seen


# -- helpers -----------------------------------------------------------------


def _read_tsv(path: Path, columns: int) -> list[tuple[str, ...]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[tuple[str, ...]] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < columns:
            parts += [""] * (columns - len(parts))
        rows.append(tuple(parts[:columns]))
    return rows


def _write_tsv(path: Path, rows: list[tuple[str, ...]]) -> None:
    body = "".join("\t".join(row) + "\n" for row in rows)
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise CantCreateError(f"could not write manifest {path}: {exc}") from exc



def _link_is_executable(root: Path, item) -> bool:
    """Does the symlink *item* point at an executable inside the unpacked root?"""
    target = getattr(item, "target", None)
    if not target:
        return False
    if target.startswith("/"):
        candidate = root / target.lstrip("/")
    else:
        candidate = (root / item.path.lstrip("/")).parent / target
    try:
        real = candidate.resolve()
        base = root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        real.relative_to(base)
    except ValueError:
        return False  # points outside the package: not ours to run
    return proc.is_executable(real)


def dir_size(path: Path) -> int:
    """Bytes held by the regular files under *path* (symlinks not followed)."""
    total = 0
    for item in path.rglob("*"):
        if item.is_dir():
            continue
        try:
            total += item.lstat().st_size
        except OSError:
            continue
    return total


# -- pins --------------------------------------------------------------------


def load_pins() -> dict[str, str]:
    try:
        return json.loads(pins_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_pins(pins: dict[str, str]) -> None:
    target = pins_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        raise CantCreateError(f"could not write {target}: {exc}") from exc


def set_pin(pkg: str, version: str | None) -> None:
    pins = load_pins()
    if version is None:
        pins.pop(pkg, None)
    else:
        pins[pkg] = version
    save_pins(pins)


# -- store operations --------------------------------------------------------


#: Debian package name: lowercase alphanumerics plus ``+ - .``, never a path.
_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


def entry_path(pkg: str) -> Path:
    """Store path for *pkg*.

    The name is validated first: it reaches us straight from argv, and a ``..``
    in it must not be able to address a directory outside the store.
    """
    if not _PKG_RE.match(pkg):
        raise UsageError(f"invalid package name: {pkg!r}")
    return store_dir() / pkg


def get(pkg: str) -> Entry | None:
    path = entry_path(pkg)
    if (path / READY).exists():
        return Entry(pkg, path)
    return None


def require(pkg: str) -> Entry:
    entry = get(pkg)
    if entry is None:
        raise NoInputError(
            f"{pkg} is not staged",
            hint=f"run `aptx run {pkg}` or `aptx install {pkg}` first",
        )
    return entry


def list_entries() -> list[Entry]:
    root = store_dir()
    if not root.is_dir():
        return []
    entries = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / READY).exists():
            entries.append(Entry(child.name, child))
    return entries


def _records_recommends(entry: Entry) -> bool:
    """Whether the stored metadata says Recommends were followed.

    A broken ``.meta.json`` must not trigger a download, so any read failure is
    reported as "yes" and the cached entry is kept.
    """
    try:
        return bool(entry.meta().get("recommends", False))
    except SoftwareError:
        return True


def build(
    pkg: str,
    *,
    recommends: bool = False,
    force: bool = False,
    pins: dict[str, str] | None = None,
    backend: str = "bwrap",
) -> Entry:
    """Stage *pkg* and its closure. Idempotent unless *force* is set."""
    dest = entry_path(pkg)
    existing = get(pkg)
    if existing is not None and not force:
        if not recommends or _records_recommends(existing):
            return existing
        # The cached closure was resolved without Recommends: the user asked for
        # a different closure, so rebuild instead of silently ignoring the flag.
        proc.log(f"{pkg}: cached closure has no Recommends, rebuilding", level=1)
        force = True

    if pins is None:
        pins = load_pins()

    try:
        store_dir().mkdir(parents=True, exist_ok=True)
        tmp = store_dir() / f".{pkg}.tmp.{os.getpid()}"
        if tmp.exists():
            fetch.rmtree(tmp)
        tmp.mkdir(parents=True)
    except OSError as exc:
        raise CantCreateError(f"could not create the store for {pkg}: {exc}") from exc

    try:
        _populate(pkg, tmp, recommends=recommends, pins=pins, backend=backend)
    except BaseException:
        fetch.rmtree(tmp)
        raise

    if force and dest.exists():
        fetch.rmtree(dest)
    try:
        tmp.rename(dest)
    except OSError:
        # Another builder may have renamed its own complete copy into place first
        # (``.ready`` is written inside the temp dir, so a renamed entry is never
        # half-built). Adopt it instead of deleting a store another process is
        # already reading.
        winner = get(pkg)
        if winner is not None:
            fetch.rmtree(tmp)
            return winner
        # No ready entry: ``dest`` is debris from an interrupted build.
        fetch.rmtree(dest)
        try:
            tmp.rename(dest)
        except OSError as exc:
            fetch.rmtree(tmp)
            raise CantCreateError(
                f"could not finalise store entry for {pkg}: {exc}"
            ) from exc

    entry = Entry(pkg, dest)
    entry.touch_used()
    return entry


def _populate(
    pkg: str,
    tmp: Path,
    *,
    recommends: bool,
    pins: dict[str, str],
    backend: str,
) -> None:
    names = fetch.resolve_closure(pkg, recommends=recommends)
    proc.log(f"closure of {pkg}: {len(names)} packages")

    status = fetch.installed_status(names)
    todo = [n for n in names if not status.get(n, "").startswith("ii")]
    if pkg not in todo:
        todo.append(pkg)  # the requested package is always staged
    host_skipped = len(names) - len(todo)

    specs = [f"{n}={pins[n]}" if n in pins else n for n in todo]
    proc.log(f"downloading {len(specs)} packages: {' '.join(specs[:8])}"
             + (" …" if len(specs) > 8 else ""))

    download_dir = fetch.temp_dir("aptx-debs-")
    try:
        fetch.download(specs, download_dir)
        debs = sorted(download_dir.glob("*.deb"))
        if not debs:
            raise CantCreateError(
                f"apt-get download produced no .deb files for {pkg}",
                hint="is the package name correct?",
            )

        provides: list[tuple[str, str, str]] = []
        deps: list[tuple[str, str, str, str]] = []
        requested_files: list[str] = []

        for deb in debs:
            fields = fetch.deb_fields(deb, "Package", "Version", "Architecture")
            name = fields.get("Package", "")
            entries = fetch.deb_contents(deb)
            fetch.unpack(deb, tmp)
            for item in entries:
                provides.append((item.kind, item.path, name))
            deps.append(
                (
                    name,
                    fields.get("Version", ""),
                    fields.get("Architecture", ""),
                    fetch.sha256_file(deb),
                )
            )
            if name == pkg:
                for item in entries:
                    if item.executable:
                        requested_files.append(item.path)
                    elif item.kind == "l" and _link_is_executable(tmp, item):
                        # `python3`, `vim`, `cowthink`… are symlinks to the real
                        # binary and must be runnable by name too.
                        requested_files.append(item.path)
    finally:
        fetch.rmtree(download_dir)

    provides.sort(key=lambda row: (row[1], row[2]))
    deps.sort()
    requested_files = sorted(set(requested_files))

    _write_tsv(tmp / PROVIDES, provides)
    _write_tsv(tmp / DEPS, deps)
    _write_tsv(tmp / FILES, [(path,) for path in requested_files])

    requested = next((d for d in deps if d[0] == pkg), None)
    meta = {
        "pkg": pkg,
        "version": requested[1] if requested else "?",
        "arch": requested[2] if requested else "?",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aptx_version": __version__,
        "backend": backend,
        "recommends": recommends,
        "closure": len(deps),
        "host_skipped": host_skipped,
    }
    try:
        (tmp / META).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        # .ready is written last: its absence means "incomplete, rebuild me"
        (tmp / READY).touch()
    except OSError as exc:
        raise CantCreateError(f"could not write the manifest for {pkg}: {exc}") from exc


def remove(pkg: str) -> bool:
    path = entry_path(pkg)
    if not path.exists():
        return False
    fetch.rmtree(path)
    return True


def rm_all() -> None:
    root = cache_dir()
    if root.exists():
        fetch.rmtree(root)


def gc(older_than_seconds: float) -> list[str]:
    """Remove entries unused for *older_than_seconds*. Returns removed names."""
    removed: list[str] = []
    now = time.time()
    root = store_dir()
    if not root.is_dir():
        return removed
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / READY).exists():
            fetch.rmtree(child)
            removed.append(child.name)
            continue
        marker = child / LAST_USED
        try:
            used = marker.stat().st_mtime
        except OSError:
            used = (child / READY).stat().st_mtime
        if now - used > older_than_seconds:
            fetch.rmtree(child)
            removed.append(child.name)
    return removed


def du() -> list[tuple[str, int]]:
    rows = [(entry.pkg, entry.size()) for entry in list_entries()]
    rows.sort(key=lambda row: row[1], reverse=True)
    return rows

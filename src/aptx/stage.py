"""The symlink farm: directory merging without overlayfs.

A merge point (``/usr/share``, ``/usr/bin``, …) exists on the host and the package
adds entries to it. Mount points can only be created in a writable directory, and a
read-only host bind is not writable, so we materialise our own directory: symlinks
to the host entries plus symlinks into the store. The farm is then bound over the
real path inside the sandbox.

Merge points nest — a package can add both ``/usr/lib/x86_64-linux-gnu/libx.so``
and ``/usr/share/cowsay``, which makes ``/usr`` a farm too. Nested farms are built
*physically* inside their parent so a single bind per top-level node is enough;
that is why the ancestor closure of every merge point becomes a node as well.

Farm symlinks are absolute and point at the fixed sandbox paths
``/run/aptx/host/<flat-id>`` and ``/run/aptx/root``, which keeps the farm reusable
across runs and cacheable by ``.stage-hash``.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path

from aptx import __version__, fetch
from aptx.config import SANDBOX_HOST, SANDBOX_ROOT
from aptx.errors import OsError_
from aptx.plan import Plan, host_lexists
from aptx.store import STAGE_HASH, Entry


def host_mount(merge_point: str) -> str:
    """Sandbox path where the host directory behind *merge_point* is mounted.

    Flattened (``/usr/share`` → ``/run/aptx/host/usr!share``), because nesting
    ``/run/aptx/host/usr/share`` inside a read-only bind of
    ``/run/aptx/host/usr`` would be impossible.
    """
    if merge_point == "/":
        return SANDBOX_HOST + "/_root"
    return SANDBOX_HOST + "/" + merge_point.strip("/").replace("/", "!")


def pkg_mount(merge_point: str, child: str) -> str:
    base = "" if merge_point == "/" else merge_point.rstrip("/")
    return f"{SANDBOX_ROOT}{base}/{child}"


def node_tree(plan: Plan) -> dict[str, list[str]]:
    """Merge points plus the ancestors they need, mapped to package children."""
    nodes: dict[str, list[str]] = {}
    for merge_point, children in plan.merge_points.items():
        nodes.setdefault(merge_point, [])
        nodes[merge_point] = sorted(set(nodes[merge_point]) | set(children))
        current = Path(merge_point)
        while True:
            parent = current.parent
            if parent == current or parent == Path("/"):
                break
            nodes.setdefault(str(parent), [])
            current = parent
    return nodes


def top_nodes(nodes: dict[str, list[str]]) -> list[str]:
    """Nodes that are not physically inside another node."""
    return sorted(n for n in nodes if str(Path(n).parent) not in nodes)


def is_node_path(path: str, nodes) -> bool:
    return path in nodes


def _symlink(target: str, link: Path) -> None:
    try:
        link.symlink_to(target)
    except FileExistsError:
        try:
            link.unlink()
        except OSError:
            pass
        link.symlink_to(target)
    except OSError as exc:
        raise OsError_(f"could not create symlink {link}: {exc}") from exc


def _host_children(directory: str) -> list[str]:
    try:
        return sorted(item.name for item in Path(directory).iterdir())
    except OSError:
        return []


def plan_hash(plan: Plan) -> str:
    """Cache key: package identity + node tree + the host's own children.

    The host listing is included so that a new file appearing in ``/usr/share``
    invalidates the farm.
    """
    digest = hashlib.sha256()
    digest.update(f"{__version__}\0{plan.pkg}\0{plan.version}\0".encode())
    for node, children in sorted(node_tree(plan).items()):
        digest.update(b"N" + node.encode() + b"\0")
        for child in children:
            digest.update(b"P" + child.encode() + b"\0")
        for child in _host_children(node):
            digest.update(b"H" + child.encode() + b"\0")
    return digest.hexdigest()


def build_farm(plan: Plan, stage_dir: Path) -> None:
    nodes = node_tree(plan)

    for node in nodes:
        try:
            (stage_dir / node.lstrip("/")).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OsError_(f"could not create farm directory for {node}: {exc}") from exc

    for node, children in nodes.items():
        farm = stage_dir / node.lstrip("/")
        package_children = set(children)

        for child in _host_children(node):
            if child in package_children or is_node_path(str(Path(node) / child), nodes):
                continue
            _symlink(f"{host_mount(node)}/{child}", farm / child)

        for child in sorted(package_children):
            target = str(Path(node) / child)
            if is_node_path(target, nodes):
                continue
            _symlink(pkg_mount(node, child), farm / child)
            if plan.is_etc(node) and host_lexists(target):
                # SPEC.md 5.3: the package wins, the host copy stays visible
                # next to it as <file>.host for debugging.
                _symlink(f"{host_mount(node)}/{child}", farm / (child + ".host"))


def _fresh(hash_file: Path, digest: str, stage_dir: Path, force: bool) -> bool:
    if force:
        return False
    try:
        cached = hash_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return cached == digest and stage_dir.is_dir()


def ensure(entry: Entry, plan: Plan, force: bool = False) -> Path:
    """Return the farm directory, rebuilding it only when the plan changed.

    Two aptx processes may want the same farm at the same time, so the rebuild
    holds an exclusive lock on the entry directory. Without it the loser removes
    a farm the winner is still populating and warns about a directory that is
    not empty.
    """
    stage_dir = entry.stage_dir
    hash_file = entry.path / STAGE_HASH
    digest = plan_hash(plan)

    if _fresh(hash_file, digest, stage_dir, force):
        return stage_dir

    try:
        handle = os.open(entry.path, os.O_RDONLY)
    except OSError as exc:
        raise OsError_(f"could not lock the store entry {entry.path}: {exc}") from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        # Another process may have finished the same rebuild while we waited.
        if _fresh(hash_file, digest, stage_dir, force):
            return stage_dir
        if stage_dir.exists():
            fetch.rmtree(stage_dir)  # a half-removed farm is still rebuildable
        try:
            stage_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OsError_(f"could not create stage dir {stage_dir}: {exc}") from exc

        build_farm(plan, stage_dir)
        try:
            hash_file.write_text(digest + "\n", encoding="utf-8")
        except OSError:
            pass  # a missing hash only costs a rebuild next time
        return stage_dir
    finally:
        os.close(handle)

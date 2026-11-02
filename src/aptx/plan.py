"""Computing the bind/merge plan for a staged package.

The plan answers one question: for every path the package closure provides, is it
*added* to a directory the host already has (a merge point, needs a symlink farm)
or is it a whole subtree the host does not have (a shadow, bound directly)?

See SPEC.md section 5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from aptx.config import PYTHON_DIST_RE
from aptx.store import Entry

_PY_RE = re.compile(PYTHON_DIST_RE)


def host_lexists(path: str) -> bool:
    """Does *path* exist on the host filesystem (symlinks not followed)?"""
    target = Path(path)
    return target.is_symlink() or target.exists()


def _is_dir(path: str) -> bool:
    return Path(path).is_dir()


@dataclass
class Plan:
    pkg: str
    version: str = "?"
    #: merge point -> basenames the package contributes to it
    merge_points: dict[str, list[str]] = field(default_factory=dict)
    shadow_dirs: list[str] = field(default_factory=list)
    merge_files: list[str] = field(default_factory=list)
    etc_merge: list[str] = field(default_factory=list)
    python_dirs: list[str] = field(default_factory=list)
    direct_binds: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)

    @property
    def merge_dirs(self) -> list[str]:
        return sorted(self.merge_points)

    def is_etc(self, merge_point: str) -> bool:
        return merge_point == "/etc" or merge_point.startswith("/etc/")

    def to_dict(self) -> dict:
        return {
            "pkg": self.pkg,
            "version": self.version,
            "merge_points": {mp: list(children) for mp, children in sorted(self.merge_points.items())},
            "shadow_dirs": self.shadow_dirs,
            "merge_dirs": self.merge_dirs,
            "merge_files": self.merge_files,
            "etc_merge": self.etc_merge,
            "python_dirs": self.python_dirs,
            "direct_binds": self.direct_binds,
            "conflicts": self.conflicts,
        }


def shadow_root(path: str, host=host_lexists) -> str:
    """Highest ancestor of *path* that does not exist on the host.

    For ``/usr/share/cowsay/cows/default.cow`` with ``/usr/share`` present but
    ``/usr/share/cowsay`` absent, this returns ``/usr/share/cowsay``: the whole
    subtree can come from the store in one piece.
    """
    current = Path(path)
    while True:
        parent = current.parent
        if parent == current:
            return str(current)
        if host(str(parent)):
            return str(current)
        current = parent


def build(entry: Entry, host=host_lexists, isdir=_is_dir) -> Plan:
    plan = Plan(pkg=entry.pkg, version=entry.version())
    merge_points: dict[str, set[str]] = {}
    shadow: set[str] = set()
    direct: set[str] = set()
    conflicts: list[dict] = []
    seen_conflict: set[str] = set()

    # A package also lists the directories it creates (``/usr``, ``/usr/games``).
    # Those are derived from their contents below; treating ``/usr`` itself as an
    # entry would make it a merge point at ``/`` and bind the store over the whole
    # tree, hiding every host path.
    rows = entry.provides()
    provided = {path for _kind, path, _owner in rows if path not in ("", "/")}
    derived_dirs = {
        path for path in provided if any(other.startswith(path + "/") for other in provided)
    }

    for kind, path, _owner in rows:
        if path in ("", "/") or path in derived_dirs:
            continue
        parent = str(Path(path).parent)

        if host(parent):
            merge_point, child = parent, Path(path).name
        else:
            root = shadow_root(path, host)
            merge_point = str(Path(root).parent)
            child = Path(root).name
            shadow.add(root)

        # A merge point must be a real directory on the host.
        if host(merge_point) and not isdir(merge_point):
            if path not in seen_conflict:
                seen_conflict.add(path)
                conflicts.append(
                    {
                        "path": path,
                        "host": "file",
                        "pkg": kind,
                        "reason": f"{merge_point} is a file on the host",
                    }
                )
            continue

        if merge_point == "/":
            direct.add(child)
            continue

        merge_points.setdefault(merge_point, set()).add(child)

        candidate = str(Path(merge_point) / child)
        if host(candidate) and path not in seen_conflict:
            seen_conflict.add(path)
            host_kind = "d" if isdir(candidate) else "f"
            if host_kind != kind:
                conflicts.append(
                    {"path": path, "host": host_kind, "pkg": kind, "reason": "type mismatch"}
                )

    plan.merge_points = {mp: sorted(children) for mp, children in merge_points.items()}
    plan.shadow_dirs = sorted(shadow)
    plan.direct_binds = sorted("/" + child for child in direct)
    plan.conflicts = sorted(conflicts, key=lambda c: c["path"])

    plan.merge_files = sorted(
        str(Path(mp) / child)
        for mp, children in plan.merge_points.items()
        for child in children
        if host(str(Path(mp) / child))
        and not isdir(str(entry.store_path(mp) / child))
    )
    plan.etc_merge = sorted(mp for mp in plan.merge_points if plan.is_etc(mp))
    plan.python_dirs = sorted(
        {mp for mp in plan.merge_points if _PY_RE.match(mp)}
        | {root for root in plan.shadow_dirs if _PY_RE.match(root)}
    )
    return plan


def render(plan: Plan) -> str:
    """Human-readable plan, 5-15 lines (SPEC.md 3.5)."""
    lines = [f"package: {plan.pkg} {plan.version}"]
    lines.append(f"shadow dirs ({len(plan.shadow_dirs)}):")
    lines.extend(f"  {p}" for p in plan.shadow_dirs[:20])
    if len(plan.shadow_dirs) > 20:
        lines.append(f"  … and {len(plan.shadow_dirs) - 20} more")

    lines.append(f"merge points ({len(plan.merge_points)}):")
    for mp, children in sorted(plan.merge_points.items()):
        lines.append(f"  {mp}  ({len(children)} from package)")

    lines.append(f"merge files ({len(plan.merge_files)}):")
    lines.extend(f"  {p}" for p in plan.merge_files[:20])
    if len(plan.merge_files) > 20:
        lines.append(f"  … and {len(plan.merge_files) - 20} more")

    if plan.direct_binds:
        lines.append(f"direct binds ({len(plan.direct_binds)}):")
        lines.extend(f"  {p}" for p in plan.direct_binds)
    if plan.etc_merge:
        lines.append(f"etc merge ({len(plan.etc_merge)}):")
        lines.extend(f"  {p}" for p in plan.etc_merge)
    if plan.python_dirs:
        lines.append(f"python dirs ({len(plan.python_dirs)}):")
        lines.extend(f"  {p}" for p in plan.python_dirs)
    if plan.conflicts:
        lines.append(f"conflicts ({len(plan.conflicts)}):")
        for item in plan.conflicts[:10]:
            lines.append(f"  {item['path']}  host={item['host']} pkg={item['pkg']}")
    return "\n".join(lines)

"""Environment diagnostics: ``aptx doctor`` (SPEC.md 8)."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aptx import __version__
from aptx.config import bin_dir, cache_dir, pins_file, store_dir
from aptx.proc import human_size, which
from aptx.store import list_entries, load_pins

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: Minimum bubblewrap that supports every flag aptx passes.
MIN_BWRAP = (0, 6)

#: Checks whose failure stops aptx from working at all.
CRITICAL = {"python", "bwrap", "userns", "apt", "dpkg"}

APT_TOOLS = ("apt-cache", "apt-get")
DPKG_TOOLS = ("dpkg", "dpkg-deb")

USERNS_SYSCTL = Path("/proc/sys/user/max_user_namespaces")
CLONE_SYSCTL = Path("/proc/sys/kernel/unprivileged_userns_clone")
LISTS_DIR = Path("/var/lib/apt/lists")


@dataclass
class Check:
    name: str
    status: str
    detail: str

    @property
    def symbol(self) -> str:
        return {OK: "+", WARN: "~", FAIL: "!"}.get(self.status, "?")


def _read_sysctl(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _userns_limit() -> int | None:
    raw = _read_sysctl(USERNS_SYSCTL)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bwrap_version() -> str:
    """``bwrap --version`` output, or ``""`` when it cannot be run."""
    try:
        completed = subprocess.run(
            ["bwrap", "--version"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (completed.stdout or completed.stderr or "").strip()
    return text.splitlines()[0] if text else ""


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)*)", text)
    if not match:
        return ()
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:  # pragma: no cover - the pattern only captures digits
        return ()


def _bwrap_smoke() -> bool:
    """Can bwrap actually start a sandbox here (userns allowed, no seccomp block)?"""
    try:
        completed = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--", "true"],
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _python_check() -> Check:
    version = platform.python_version()
    major, minor = platform.python_version_tuple()[:2]
    try:
        current = (int(major), int(minor))
    except ValueError:
        return Check("python", WARN, f"{version}: cannot parse the version")
    if current < (3, 11):
        return Check("python", FAIL, f"{version}: aptx needs 3.11 or newer")
    return Check("python", OK, version)


def _bwrap_check() -> Check:
    path = which("bwrap")
    if not path:
        if which("proot"):
            return Check("bwrap", FAIL, "not found; proot is only a weaker fallback")
        return Check("bwrap", FAIL, "not installed")
    version = _bwrap_version()
    parsed = _version_tuple(version)
    if not parsed:
        return Check("bwrap", WARN, f"{path}: cannot parse --version output")
    if parsed < MIN_BWRAP:
        return Check("bwrap", FAIL, f"{version}: aptx needs 0.6 or newer")
    return Check("bwrap", OK, f"{version} ({path})")


def _userns_check() -> Check:
    limit = _userns_limit()
    if limit is None:
        return Check("userns", WARN, f"cannot read {USERNS_SYSCTL}")
    if limit == 0:
        return Check("userns", FAIL, "max_user_namespaces is 0")
    clone = _read_sysctl(CLONE_SYSCTL)
    if clone == "0":
        return Check("userns", FAIL, "kernel.unprivileged_userns_clone is 0")
    return Check("userns", OK, f"max_user_namespaces={limit}")


def _tools_check(name: str, tools: tuple[str, ...]) -> Check:
    missing = [tool for tool in tools if not which(tool)]
    if missing:
        return Check(name, FAIL, "missing: " + ", ".join(missing))
    return Check(name, OK, ", ".join(tools))


def _store_check() -> Check:
    root = cache_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("store", FAIL, f"{root}: {exc}")

    entries = list_entries()
    if not entries:
        return Check("store", OK, f"writable, empty ({store_dir()})")
    total = sum(entry.size() for entry in entries)
    names = ", ".join(entry.pkg for entry in entries[:8])
    more = f" (+{len(entries) - 8})" if len(entries) > 8 else ""
    return Check("store", OK, f"{len(entries)} packages, {human_size(total)}: {names}{more}")


def _bin_dir_check() -> Check:
    target = bin_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("bin dir", FAIL, f"{target}: {exc}")
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if str(target) in path_dirs:
        return Check("bin dir", OK, f"{target} is on PATH")
    return Check("bin dir", WARN, f"{target} is not on PATH; shims will not resolve")


def _net_check() -> Check:
    try:
        indexes = list(LISTS_DIR.glob("*_Packages"))
    except OSError as exc:
        return Check("net", WARN, f"cannot read {LISTS_DIR}: {exc}")
    if not indexes:
        return Check("net", WARN, f"no package lists in {LISTS_DIR}; run `apt-get update`")
    return Check("net", OK, f"{len(indexes)} package list(s) in {LISTS_DIR}")


def _device_check(name: str, path: str) -> Check:
    if Path(path).exists():
        return Check(name, OK, path)
    return Check(name, WARN, f"{path} is missing; --gpu will not work")


def _root_check() -> Check:
    if os.geteuid() == 0:
        return Check(
            "root",
            WARN,
            "running as root: no user namespace, full capabilities, no security boundary",
        )
    return Check("root", OK, "unprivileged user")


def _shim_check(entries: list) -> Check | None:
    from aptx.shim import orphan_shims

    orphans = orphan_shims({entry.pkg for entry in entries})
    if orphans:
        return Check(
            "shims",
            WARN,
            "dangling: " + ", ".join(path.name for path in orphans) + " (run `aptx gc`)",
        )
    if pins_file().exists() or entries:
        return Check("shims", OK, "consistent")
    return None


def _pin_check(entries: list) -> Check | None:
    pins = load_pins()
    if not pins:
        return None
    staged = {entry.pkg for entry in entries}
    detail = ", ".join(f"{name}={version}" for name, version in sorted(pins.items())[:6])
    dangling = sorted(name for name in pins if name not in staged)
    if dangling:
        return Check("pins", WARN, f"{detail}; not staged: {', '.join(dangling)}")
    return Check("pins", OK, detail)


def run_checks() -> list[Check]:
    checks: list[Check] = [
        Check("aptx", OK, f"{__version__} on python {platform.python_version()}"),
        _python_check(),
    ]

    if platform.system() == "Linux":
        checks.append(Check("platform", OK, f"{platform.system()} {platform.release()}"))
    else:
        checks.append(Check("platform", FAIL, f"{platform.system()}: aptx needs Linux"))

    checks.append(_bwrap_check())
    checks.append(_userns_check())

    if which("bwrap"):
        checks.append(
            Check("bwrap smoke", OK, "sandbox starts")
            if _bwrap_smoke()
            else Check("bwrap smoke", FAIL, "bwrap cannot start a sandbox here")
        )

    checks.append(_tools_check("apt", APT_TOOLS))
    checks.append(_tools_check("dpkg", DPKG_TOOLS))
    checks.append(_store_check())
    checks.append(_bin_dir_check())
    checks.append(_net_check())
    checks.append(_device_check("/dev/dri", "/dev/dri"))
    checks.append(_device_check("/dev/kfd", "/dev/kfd"))
    checks.append(_root_check())

    entries = list_entries()
    for optional in (_pin_check(entries), _shim_check(entries)):
        if optional is not None:
            checks.append(optional)

    return checks


def failed(checks: list[Check]) -> bool:
    """True when a *critical* check failed; warnings never change the exit code."""
    return any(check.status == FAIL and check.name in CRITICAL for check in checks)


def render(checks: list[Check]) -> str:
    width = max((len(check.name) for check in checks), default=0)
    return "\n".join(f"{check.symbol} {check.name.ljust(width)}  {check.detail}" for check in checks)

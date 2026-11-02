"""Thin wrapper around :mod:`subprocess` with aptx logging."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

_VERBOSE = 0


def set_verbose(level: int) -> None:
    global _VERBOSE
    _VERBOSE = level


def verbose_level() -> int:
    return _VERBOSE


def log(message: str, level: int = 1) -> None:
    """Write a progress line to stderr when verbosity is high enough."""
    if _VERBOSE >= level:
        sys.stderr.write(f"aptx: {message}\n")
        sys.stderr.flush()


def warn(message: str) -> None:
    sys.stderr.write(f"aptx: warning: {message}\n")
    sys.stderr.flush()


def note(message: str) -> None:
    """Informational line that does not affect the exit code (SPEC.md 3.3)."""
    sys.stderr.write(f"aptx: note: {message}\n")
    sys.stderr.flush()


def run(
    argv: Sequence[str],
    *,
    check: bool = False,
    capture: bool = True,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a helper command (apt-cache, dpkg, ...)."""
    argv = [str(a) for a in argv]
    log("exec: " + " ".join(shlex.quote(a) for a in argv), level=2)
    try:
        cp = subprocess.run(
            argv,
            check=False,
            cwd=cwd,
            env=env,
            input=input_text,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError as exc:
        from aptx.errors import UnavailableError

        raise UnavailableError(
            f"required command not found: {argv[0]}",
            hint="install it (apt-get install dpkg apt) and retry",
        ) from exc
    if check and cp.returncode != 0:
        from aptx.errors import UnavailableError

        detail = (cp.stderr or cp.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {cp.returncode}"
        raise UnavailableError(
            f"{argv[0]} failed: {tail}",
            hint=f"command was: {' '.join(shlex.quote(a) for a in argv)}",
        )
    return cp


def which(name: str) -> str | None:
    return shutil.which(name)


def is_executable(path: str | os.PathLike[str]) -> bool:
    """True when *path* is a runnable regular file.

    ``os.access`` raises on a path the kernel refuses to look at, and that must
    read as "not runnable" rather than abort the command.
    """
    try:
        return Path(path).is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def human_size(num: int) -> str:
    """Bytes as ``12.3M``. Tolerates junk so callers need no guard."""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return f"{num}"
    unit = "B"
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            break
        value /= 1024
    return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"


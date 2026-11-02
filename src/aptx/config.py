"""Paths, environment and sandbox constants.

Everything that depends on the environment is exposed as a function so tests can
monkeypatch ``os.environ`` and get fresh values.
"""

from __future__ import annotations

import os
from pathlib import Path

from aptx.errors import EnvError

# --- user-visible locations -------------------------------------------------


def _home() -> Path:
    """The host home directory, or a hard error when there is none.

    ``~`` cannot be expanded without ``$HOME`` and aptx must not silently invent
    a location for the store, so an environment without it is code 64.
    """
    raw = os.environ.get("HOME")
    if not raw:
        raise EnvError(
            "$HOME is not set",
            hint="export HOME, or set APTX_CACHE and APTX_BIN explicitly",
        )
    return Path(raw)


def _path_env(name: str, default: str) -> Path:
    raw = os.environ.get(name)
    if raw:
        return Path(raw).expanduser()
    return _home() / default


#: Set by ``aptx run --ephemeral``: the whole store lives here and dies with the run.
_override: Path | None = None


def set_cache_override(path: Path | None) -> None:
    """Redirect every cache path to *path* (``None`` restores ``APTX_CACHE``)."""
    global _override
    _override = path


def cache_dir() -> Path:
    """Root of all persistent aptx state (``APTX_CACHE``)."""
    if _override is not None:
        return _override
    return _path_env("APTX_CACHE", ".cache/aptx")


def store_dir() -> Path:
    """Where unpacked package roots live."""
    return cache_dir() / "store"


def homes_dir() -> Path:
    """Root of the ``--persist=NAME`` homes that every package shares."""
    return cache_dir() / "homes"


def pins_file() -> Path:
    return cache_dir() / "pins.json"


def bin_dir() -> Path:
    """Where ``aptx install`` writes command shims (``APTX_BIN``)."""
    return _path_env("APTX_BIN", ".local/bin")


# --- sandbox layout (paths *inside* the sandbox) ----------------------------

SANDBOX_BASE = "/run/aptx"
SANDBOX_ROOT = SANDBOX_BASE + "/root"
SANDBOX_HOST = SANDBOX_BASE + "/host"

DEFAULT_PATH = (
    "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
    ":/usr/local/games:/usr/games"
)

#: Directories the sandbox root starts as empty tmpfs for.
TMPFS_DIRS = [
    "/tmp",
    "/usr",
    "/usr/bin",
    "/usr/sbin",
    "/usr/lib",
    "/usr/lib64",
    "/usr/libexec",
    "/usr/share",
    "/usr/local",
    "/usr/include",
    "/usr/src",
    "/etc",
    "/var",
    "/var/cache",
    "/var/log",
    "/var/tmp",
    "/var/lib",
    "/var/opt",
    "/run",
    "/opt",
    "/srv",
]

#: Read-only host binds that are always made (``--ro-bind-try`` where optional).
BASE_RO_BINDS: list[str] = [
    "/usr/bin",
    "/usr/sbin",
    "/usr/lib",
    "/usr/lib64",
    "/usr/libexec",
    "/usr/share/zoneinfo",
    "/usr/share/terminfo",
    "/usr/share/locale",
    "/usr/share/ca-certificates",
    "/usr/local/bin",
    "/usr/local/lib",
    "/usr/local/share",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/ssl",
    "/etc/ca-certificates.conf",
    "/etc/localtime",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/alternatives",
    "/etc/mime.types",
    "/etc/terminfo",
]

#: Binds made only when ``--gui`` is passed.
GUI_BINDS: list[str] = [
    "/tmp/.X11-unix",
    "/usr/share/fonts",
    "/usr/share/icons",
    "/usr/share/X11",
]

#: Binds made only when ``--gpu`` is passed and the device must exist.
GPU_HARD_BINDS: list[str] = [
    "/dev/dri",
]

#: Optional GPU devices (ROCm). Missing ones are not an error.
GPU_SOFT_BINDS: list[str] = [
    "/dev/kfd",
]

#: Matches ``/usr/lib/python3/dist-packages`` and friends.
PYTHON_DIST_RE = r"^/usr/(local/)?lib/python3(\.[0-9]+)?/(dist|site)-packages"

APT_RELATIONS_WANTED = {"Depends", "PreDepends"}

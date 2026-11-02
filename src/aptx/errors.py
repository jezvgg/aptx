"""Error types and exit codes.

Exit codes follow BSD ``sysexits.h`` so that they do not collide with the
arbitrary status codes of the programs aptx launches.
"""

from __future__ import annotations

import sys

EX_OK = 0
EX_USAGE = 2
EX_ENV = 64
EX_DATAERR = 65
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_OSERR = 71
EX_CANTCREAT = 73


#: Set to ``False`` by ``aptx --no-color``.
_COLOR = True


def set_color(enabled: bool) -> None:
    global _COLOR
    _COLOR = enabled


def color_enabled(stream: object | None = None) -> bool:
    target = sys.stderr if stream is None else stream
    return _COLOR and bool(getattr(target, "isatty", lambda: False)())


class AptxError(Exception):
    """An error aptx knows how to explain to the user."""

    code = EX_SOFTWARE

    def __init__(self, message: str, hint: str | None = None, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            self.code = code

    def render(self, color: bool | None = None) -> str:
        use_color = color_enabled() if color is None else color
        prefix = "\033[31maptx: error:\033[0m" if use_color else "aptx: error:"
        out = f"{prefix} {self.message}"
        if self.hint:
            out += f"\naptx: hint: {self.hint}"
        return out


class UsageError(AptxError):
    code = EX_USAGE


class EnvError(AptxError):
    code = EX_ENV


class DataError(AptxError):
    """Package not found, closure resolution failed."""

    code = EX_DATAERR


class NoInputError(AptxError):
    """Package not staged / binary not found."""

    code = EX_NOINPUT


class UnavailableError(AptxError):
    """No network, apt-get failed, sandbox backend missing."""

    code = EX_UNAVAILABLE


class SoftwareError(AptxError):
    """Broken manifest, impossible plan - an aptx bug."""

    code = EX_SOFTWARE


class OsError_(AptxError):
    """Filesystem or permission problem."""

    code = EX_OSERR


class CantCreateError(AptxError):
    """Could not create a shim or write the store."""

    code = EX_CANTCREAT

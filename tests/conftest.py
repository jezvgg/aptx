"""Shared fixtures: a real store Entry backed by a temporary directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from aptx.store import DEPS, FILES, META, PROVIDES, Entry


def make_entry(
    tmp_path: Path,
    pkg: str = "cowsay",
    provides: list[tuple[str, str, str]] | None = None,
    files: list[str] | None = None,
    version: str = "1.0",
) -> Entry:
    """Build an :class:`Entry` whose manifests are written to *tmp_path*."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    entry = Entry(pkg, tmp_path)
    rows = provides if provides is not None else [("f", "/usr/games/cowsay", pkg)]
    (tmp_path / PROVIDES).write_text(
        "".join("\t".join(row) + "\n" for row in rows), encoding="utf-8"
    )
    (tmp_path / FILES).write_text("".join(f"{f}\n" for f in (files or [])), encoding="utf-8")
    (tmp_path / DEPS).write_text(f"{pkg}\t{version}\tall\tdeadbeef\n", encoding="utf-8")
    (tmp_path / META).write_text(f'{{"pkg": "{pkg}", "version": "{version}"}}', encoding="utf-8")
    return entry


@pytest.fixture
def entry(tmp_path: Path) -> Entry:
    return make_entry(tmp_path)

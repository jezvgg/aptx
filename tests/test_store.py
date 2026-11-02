"""store: manifests, pins, staging and the gc rules."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from aptx import config, fetch, store
from aptx.errors import CantCreateError, DataError, NoInputError, SoftwareError, UsageError
from aptx.store import (
    DEPS,
    FILES,
    LAST_USED,
    META,
    PROVIDES,
    READY,
    Entry,
)

from conftest import make_entry


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path):
    config.set_cache_override(tmp_path / "cache")
    yield
    config.set_cache_override(None)


class TestEntryManifests:
    def test_meta_is_parsed(self, entry: Entry):
        assert entry.meta()["pkg"] == "cowsay"

    def test_corrupt_meta_raises_software_error(self, entry: Entry):
        (entry.path / META).write_text("{not json", encoding="utf-8")
        with pytest.raises(SoftwareError, match="corrupt manifest"):
            entry.meta()

    def test_missing_meta_raises_software_error(self, entry: Entry):
        (entry.path / META).unlink()
        with pytest.raises(SoftwareError):
            entry.meta()

    def test_version_comes_from_meta(self, entry: Entry):
        assert entry.version() == "1.0"

    def test_version_falls_back_to_question_mark(self, entry: Entry):
        (entry.path / META).unlink()
        assert entry.version() == "?"

    def test_version_defaults_when_absent(self, entry: Entry):
        (entry.path / META).write_text('{"pkg": "cowsay"}', encoding="utf-8")
        assert entry.version() == "?"

    def test_provides_rows(self, entry: Entry):
        assert entry.provides() == [("f", "/usr/games/cowsay", "cowsay")]

    def test_files_rows(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/games/cowsay\n/usr/bin/x\n", encoding="utf-8")
        assert entry.files() == ["/usr/games/cowsay", "/usr/bin/x"]

    def test_deps_rows(self, entry: Entry):
        assert entry.deps() == [("cowsay", "1.0", "all", "deadbeef")]

    def test_missing_manifest_reads_as_empty(self, entry: Entry):
        (entry.path / PROVIDES).unlink()
        assert entry.provides() == []

    def test_short_rows_are_padded(self, entry: Entry):
        (entry.path / PROVIDES).write_text("f\t/usr/bin/x\n", encoding="utf-8")
        assert entry.provides() == [("f", "/usr/bin/x", "")]

    def test_extra_columns_are_dropped(self, entry: Entry):
        (entry.path / PROVIDES).write_text("f\t/x\tp\textra\n", encoding="utf-8")
        assert entry.provides() == [("f", "/x", "p")]

    def test_blank_lines_are_skipped(self, entry: Entry):
        (entry.path / PROVIDES).write_text("\nf\t/x\tp\n\n", encoding="utf-8")
        assert entry.provides() == [("f", "/x", "p")]

    def test_stage_and_state_dirs(self, entry: Entry):
        assert entry.stage_dir == entry.path / "stage"
        assert entry.state_dir == entry.path / "state"


class TestStorePath:
    def test_joins_a_relative_path(self, entry: Entry):
        assert entry.store_path("/usr/games/cowsay") == entry.path / "usr/games/cowsay"

    def test_ignores_empty_and_dot_segments(self, entry: Entry):
        assert entry.store_path("//usr/./bin//x") == entry.path / "usr/bin/x"

    def test_rejects_dotdot(self, entry: Entry):
        with pytest.raises(DataError, match="escapes the store entry"):
            entry.store_path("/usr/../../etc/passwd")

    def test_root_resolves_to_the_entry(self, entry: Entry):
        assert entry.store_path("/") == entry.path


class TestResolveBin:
    def test_exact_absolute_path(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/games/cowsay\n", encoding="utf-8")
        assert entry.resolve_bin("/usr/games/cowsay") == "/usr/games/cowsay"

    def test_bare_name_is_matched_by_basename(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/games/cowsay\n", encoding="utf-8")
        assert entry.resolve_bin("cowsay") == "/usr/games/cowsay"

    def test_leading_dot_slash_is_normalised(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/games/cowsay\n", encoding="utf-8")
        assert entry.resolve_bin("./cowsay") == "/usr/games/cowsay"

    def test_relative_path_is_matched(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/games/cowsay\n", encoding="utf-8")
        assert entry.resolve_bin("usr/games/cowsay") == "/usr/games/cowsay"

    def test_no_executables_at_all(self, entry: Entry):
        (entry.path / FILES).write_text("", encoding="utf-8")
        with pytest.raises(NoInputError, match="has no executables"):
            entry.resolve_bin("cowsay")

    def test_unknown_name_lists_the_alternatives(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/bin/cowthink\n", encoding="utf-8")
        with pytest.raises(NoInputError, match="no executable named"):
            entry.resolve_bin("cowsay")

    def test_duplicate_basenames_pick_the_shallowest(self, entry: Entry):
        # `git` ships both /usr/bin/git and /usr/lib/git-core/git.
        (entry.path / FILES).write_text(
            "/usr/lib/git-core/git\n/usr/bin/git\n", encoding="utf-8"
        )
        assert entry.resolve_bin("git") == "/usr/bin/git"

    def test_duplicate_basenames_tie_break_alphabetically(self, entry: Entry):
        (entry.path / FILES).write_text("/usr/bin/x\n/usr/sbin/x\n", encoding="utf-8")
        assert entry.resolve_bin("x") == "/usr/bin/x"


class TestTouchAndSize:
    def test_touch_used_creates_the_marker(self, entry: Entry):
        entry.touch_used()
        assert (entry.path / LAST_USED).exists()

    def test_touch_used_is_silent_on_failure(self, entry: Entry, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("nope")

        monkeypatch.setattr(Path, "touch", boom)
        entry.touch_used()

    def test_size_counts_regular_files_only(self, entry: Entry):
        (entry.path / "usr").mkdir()
        (entry.path / "usr" / "blob").write_bytes(b"x" * 100)
        (entry.path / "usr" / "link").symlink_to("/nonexistent")
        assert entry.size() >= 100

    def test_size_survives_a_vanished_file(self, entry: Entry, monkeypatch):
        (entry.path / "usr").mkdir()
        blob = entry.path / "usr" / "blob"
        blob.write_bytes(b"x")
        monkeypatch.setattr(
            Path, "lstat", lambda self: (_ for _ in ()).throw(OSError("gone"))
        )
        assert entry.size() == 0


class TestPins:
    def test_missing_file_reads_as_empty(self):
        assert store.load_pins() == {}

    def test_corrupt_file_reads_as_empty(self):
        target = config.pins_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{oops", encoding="utf-8")
        assert store.load_pins() == {}

    def test_save_then_load_roundtrip(self):
        store.save_pins({"cowsay": "1.0"})
        assert store.load_pins() == {"cowsay": "1.0"}

    def test_save_is_sorted_and_indented(self):
        store.save_pins({"z": "1", "a": "2"})
        body = config.pins_file().read_text(encoding="utf-8")
        assert body.index('"a"') < body.index('"z"')
        assert "\n  " in body

    def test_save_failure_raises_cant_create(self, monkeypatch):
        monkeypatch.setattr(
            Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        with pytest.raises(CantCreateError, match="could not write"):
            store.save_pins({"a": "1"})

    def test_set_pin_adds(self):
        store.set_pin("cowsay", "1.0")
        assert store.load_pins() == {"cowsay": "1.0"}

    def test_set_pin_clears_with_none(self):
        store.set_pin("cowsay", "1.0")
        store.set_pin("cowsay", None)
        assert store.load_pins() == {}

    def test_set_pin_clear_of_unknown_is_a_noop(self):
        store.set_pin("absent", None)
        assert store.load_pins() == {}


class TestEntryPathValidation:
    @pytest.mark.parametrize("name", ["cowsay", "libc6", "g++", "python3.12", "a-b+c.d"])
    def test_valid_names(self, name):
        assert store.entry_path(name).name == name

    @pytest.mark.parametrize(
        "name", ["", "..", "../etc", "Cowsay", "-rf", "a/b", ".hidden", "with space"]
    )
    def test_invalid_names_raise(self, name):
        with pytest.raises(UsageError, match="invalid package name"):
            store.entry_path(name)


class TestLookup:
    def test_get_returns_none_when_nothing_is_staged(self):
        assert store.get("cowsay") is None

    def test_get_returns_none_when_not_ready(self):
        root = config.store_dir()
        (root / "cowsay").mkdir(parents=True)
        assert store.get("cowsay") is None

    def test_get_returns_the_entry(self):
        staged("cowsay")
        found = store.get("cowsay")
        assert found is not None and found.pkg == "cowsay"

    def test_require_raises_for_an_unstaged_package(self):
        with pytest.raises(NoInputError, match="is not staged"):
            store.require("cowsay")

    def test_require_returns_a_staged_entry(self):
        staged("cowsay")
        assert store.require("cowsay").pkg == "cowsay"

    def test_list_entries_is_empty_without_a_store(self):
        assert store.list_entries() == []

    def test_list_entries_skips_files_and_unready_dirs(self):
        root = config.store_dir()
        root.mkdir(parents=True)
        (root / "stray-file").write_text("x", encoding="utf-8")
        (root / "halfbuilt").mkdir()
        (root / "ready").mkdir()
        (root / "ready" / READY).touch()
        assert [e.pkg for e in store.list_entries()] == ["ready"]


def staged(pkg: str = "cowsay") -> Entry:
    """Create a ready-to-use store entry for *pkg*."""
    root = config.store_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / pkg
    target.mkdir(exist_ok=True)
    (target / READY).touch()
    (target / LAST_USED).touch()
    return Entry(pkg, target)


class TestRemove:
    def test_removes_an_entry(self):
        entry = staged()
        assert store.remove("cowsay") is True
        assert not entry.path.exists()

    def test_missing_entry_returns_false(self):
        assert store.remove("cowsay") is False

    def test_invalid_name_is_rejected(self):
        with pytest.raises(UsageError):
            store.remove("../escape")

    def test_rm_all_removes_the_whole_cache(self):
        staged()
        store.rm_all()
        assert not config.cache_dir().exists()

    def test_rm_all_on_an_absent_cache(self):
        store.rm_all()


class TestGc:
    def test_missing_store_is_a_noop(self):
        assert store.gc(0) == []

    def test_removes_unready_directories(self):
        root = config.store_dir()
        root.mkdir(parents=True)
        (root / "halfbuilt").mkdir()
        assert store.gc(10_000_000) == ["halfbuilt"]
        assert not (root / "halfbuilt").exists()

    def test_keeps_recent_entries(self):
        staged()
        assert store.gc(10_000) == []

    def test_removes_old_entries(self):
        entry = staged()
        old = 1_000_000
        os.utime(entry.path / LAST_USED, (old, old))
        assert store.gc(10) == ["cowsay"]

    def test_falls_back_to_the_ready_mtime(self):
        entry = staged()
        (entry.path / LAST_USED).unlink()
        old = 1_000_000
        os.utime(entry.path / READY, (old, old))
        assert store.gc(10) == ["cowsay"]

    def test_skips_plain_files(self):
        root = config.store_dir()
        root.mkdir(parents=True)
        (root / "stray").write_text("x", encoding="utf-8")
        assert store.gc(0) == []

    def test_du_sorts_by_size_descending(self):
        small = staged("small")
        big = staged("big")
        (big.path / "blob").write_bytes(b"x" * 5000)
        rows = store.du()
        assert [row[0] for row in rows] == ["big", "small"]
        assert rows[0][1] >= 5000
        assert small.path.exists()


class TestLinkIsExecutable:
    def make_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "root"
        (root / "usr" / "bin").mkdir(parents=True)
        real = root / "usr" / "games" / "cowsay"
        real.parent.mkdir(parents=True)
        real.write_text("#!/bin/sh\n", encoding="utf-8")
        real.chmod(0o755)
        return root

    def test_absolute_target_inside_the_root(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        item = fetch.DebPath("l", "/usr/bin/cowsay", target="/usr/games/cowsay")
        assert store._link_is_executable(root, item) is True

    def test_relative_target_inside_the_root(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        item = fetch.DebPath("l", "/usr/bin/cowsay", target="../games/cowsay")
        assert store._link_is_executable(root, item) is True

    def test_relative_target_escaping_the_root(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        escape = "../" * 12 + "etc/passwd"
        item = fetch.DebPath("l", "/usr/bin/x", target=escape)
        assert store._link_is_executable(root, item) is False

    def test_absolute_target_is_always_re_rooted(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        item = fetch.DebPath("l", "/usr/bin/x", target="/usr/bin/env")
        assert store._link_is_executable(root, item) is False

    def test_dangling_target(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        item = fetch.DebPath("l", "/usr/bin/x", target="/usr/games/absent")
        assert store._link_is_executable(root, item) is False

    def test_no_target(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        assert store._link_is_executable(root, fetch.DebPath("f", "/usr/bin/x")) is False

    def test_target_is_not_executable(self, tmp_path: Path):
        root = self.make_root(tmp_path)
        plain = root / "usr" / "share" / "doc"
        plain.parent.mkdir(parents=True)
        plain.write_text("x", encoding="utf-8")
        plain.chmod(0o644)
        item = fetch.DebPath("l", "/usr/bin/x", target="/usr/share/doc")
        assert store._link_is_executable(root, item) is False

    def test_resolve_failure_is_not_fatal(self, tmp_path: Path, monkeypatch):
        root = self.make_root(tmp_path)
        monkeypatch.setattr(
            Path, "resolve", lambda self: (_ for _ in ()).throw(OSError("loop"))
        )
        item = fetch.DebPath("l", "/usr/bin/x", target="/usr/games/cowsay")
        assert store._link_is_executable(root, item) is False


def fake_fetch(
    monkeypatch,
    pkg: str = "cowsay",
    contents=None,
    fields=None,
    closure=None,
    status=None,
    specs_seen: list[list[str]] | None = None,
    drop_deb: bool = True,
):
    """Stub out every apt/dpkg call ``_populate`` makes."""
    contents = contents or [
        fetch.DebPath("d", "/usr", mode="drwxr-xr-x"),
        fetch.DebPath("d", "/usr/games", mode="drwxr-xr-x"),
        fetch.DebPath("f", "/usr/games/cowsay", mode="-rwxr-xr-x"),
        fetch.DebPath("f", "/usr/share/doc", mode="-rw-r--r--"),
    ]
    fields = fields or {"Package": pkg, "Version": "1.0", "Architecture": "all"}

    def download(specs, dest):
        if specs_seen is not None:
            specs_seen.append(list(specs))
        if drop_deb:
            (Path(dest) / f"{pkg}.deb").write_bytes(b"deb")

    def unpack(deb, root):
        for item in contents:
            target = Path(root) / item.path.lstrip("/")
            if item.kind == "d":
                target.mkdir(parents=True, exist_ok=True)
            elif item.kind == "l":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(item.target or "/nonexistent")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("#!/bin/sh\n", encoding="utf-8")
                if item.executable:
                    target.chmod(0o755)

    monkeypatch.setattr(
        fetch, "resolve_closure", lambda name, recommends=False: closure or [pkg]
    )
    monkeypatch.setattr(fetch, "installed_status", lambda names: status or {})
    monkeypatch.setattr(fetch, "download", download)
    monkeypatch.setattr(fetch, "deb_fields", lambda deb, *f: fields)
    monkeypatch.setattr(fetch, "deb_contents", lambda deb: contents)
    monkeypatch.setattr(fetch, "unpack", unpack)
    monkeypatch.setattr(fetch, "sha256_file", lambda deb: "cafe")


class TestRecommendsInvalidation:
    def test_a_cached_closure_without_recommends_is_rebuilt(self, monkeypatch):
        seen: list[bool] = []
        fake_fetch(monkeypatch)
        monkeypatch.setattr(
            fetch, "resolve_closure", lambda name, recommends=False: seen.append(recommends) or ["cowsay"]
        )
        store.build("cowsay")
        assert seen == [False]

        again = store.build("cowsay", recommends=True)
        assert seen == [False, True]
        assert again.meta()["recommends"] is True

    def test_a_matching_closure_is_reused(self, monkeypatch):
        seen: list[bool] = []
        fake_fetch(monkeypatch)
        monkeypatch.setattr(
            fetch, "resolve_closure", lambda name, recommends=False: seen.append(recommends) or ["cowsay"]
        )
        store.build("cowsay", recommends=True)
        store.build("cowsay", recommends=True)
        assert seen == [True]

    def test_a_broken_meta_does_not_force_a_download(self, monkeypatch):
        seen: list[bool] = []
        fake_fetch(monkeypatch)
        monkeypatch.setattr(
            fetch, "resolve_closure", lambda name, recommends=False: seen.append(recommends) or ["cowsay"]
        )
        entry = store.build("cowsay")
        (entry.path / META).write_text("{not json", encoding="utf-8")
        store.build("cowsay", recommends=True)
        assert seen == [False]


class TestBuild:
    def test_build_writes_every_manifest(self, monkeypatch):
        fake_fetch(monkeypatch)
        entry = store.build("cowsay")
        assert (entry.path / READY).exists()
        assert (entry.path / LAST_USED).exists()
        meta = json.loads((entry.path / META).read_text(encoding="utf-8"))
        assert meta["pkg"] == "cowsay"
        assert meta["version"] == "1.0"
        assert meta["backend"] == "bwrap"
        assert meta["closure"] == 1
        assert meta["recommends"] is False
        assert meta["aptx_version"]
        assert entry.files() == ["/usr/games/cowsay"]
        assert ("f", "/usr/games/cowsay", "cowsay") in entry.provides()
        assert entry.deps() == [("cowsay", "1.0", "all", "cafe")]

    def test_build_is_idempotent(self, monkeypatch):
        fake_fetch(monkeypatch)
        first = store.build("cowsay")
        second = store.build("cowsay")
        assert first.path == second.path

    def test_force_rebuilds(self, monkeypatch):
        fake_fetch(monkeypatch)
        first = store.build("cowsay")
        (first.path / "marker").write_text("x", encoding="utf-8")
        second = store.build("cowsay", force=True)
        assert second.path == first.path
        assert not (second.path / "marker").exists()

    def test_pins_become_versioned_specs(self, monkeypatch):
        seen: list[list[str]] = []
        fake_fetch(monkeypatch, specs_seen=seen)
        store.build("cowsay", pins={"cowsay": "9.9"})
        assert seen == [["cowsay=9.9"]]

    def test_pins_default_to_the_saved_file(self, monkeypatch):
        seen: list[list[str]] = []
        fake_fetch(monkeypatch, specs_seen=seen)
        store.save_pins({"cowsay": "7.7"})
        store.build("cowsay")
        assert seen == [["cowsay=7.7"]]

    def test_installed_packages_are_skipped_but_the_target_is_kept(self, monkeypatch):
        seen: list[list[str]] = []
        fake_fetch(
            monkeypatch,
            closure=["cowsay", "perl"],
            status={"perl": "ii ", "cowsay": "ii "},
            specs_seen=seen,
        )
        entry = store.build("cowsay")
        assert seen == [["cowsay"]]
        assert json.loads((entry.path / META).read_text())["host_skipped"] == 1

    def test_no_debs_produced_raises(self, monkeypatch):
        fake_fetch(monkeypatch, drop_deb=False)
        with pytest.raises(CantCreateError, match="produced no .deb files"):
            store.build("cowsay")

    def test_failure_cleans_up_the_temp_directory(self, monkeypatch):
        fake_fetch(monkeypatch)
        monkeypatch.setattr(
            fetch, "unpack", lambda deb, root: (_ for _ in ()).throw(OSError("boom"))
        )
        with pytest.raises(OSError):
            store.build("cowsay")
        leftovers = [p for p in config.store_dir().iterdir() if p.name.startswith(".")]
        assert leftovers == []

    def test_unwritable_store_raises_cant_create(self, monkeypatch):
        monkeypatch.setattr(
            Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        with pytest.raises(CantCreateError, match="could not create the store"):
            store.build("cowsay")

    def test_rename_failure_without_a_winner_raises(self, monkeypatch):
        fake_fetch(monkeypatch)
        monkeypatch.setattr(
            Path, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("exists"))
        )
        with pytest.raises(CantCreateError, match="could not finalise"):
            store.build("cowsay")

    def test_rename_failure_adopts_the_winning_entry(self, monkeypatch):
        fake_fetch(monkeypatch)

        def rename(self, target):
            winner = Path(target)
            winner.mkdir(parents=True, exist_ok=True)
            (winner / READY).touch()
            raise OSError("lost the race")

        monkeypatch.setattr(Path, "rename", rename)
        entry = store.build("cowsay")
        assert entry.pkg == "cowsay"
        assert (entry.path / READY).exists()

    def test_manifest_write_failure_raises(self, monkeypatch):
        fake_fetch(monkeypatch)
        original = Path.write_text

        def guarded(self, *args, **kwargs):
            if self.name in (META, READY):
                raise OSError("ro")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", guarded)
        with pytest.raises(CantCreateError, match="could not write"):
            store.build("cowsay")

    def test_symlink_commands_are_recorded(self, monkeypatch):
        contents = [
            fetch.DebPath("f", "/usr/games/cowsay", mode="-rwxr-xr-x"),
            fetch.DebPath("l", "/usr/bin/cowsay", target="/usr/games/cowsay"),
        ]
        fake_fetch(monkeypatch, contents=contents)
        entry = store.build("cowsay")
        assert entry.files() == ["/usr/bin/cowsay", "/usr/games/cowsay"]

    def test_manifest_write_failure_raises_cant_create(self, monkeypatch):
        target = config.cache_dir() / "x.tsv"
        target.parent.mkdir(parents=True, exist_ok=True)
        original = Path.write_text

        def guarded(self, *args, **kwargs):
            if self.name == "x.tsv":
                raise OSError("ro")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", guarded)
        with pytest.raises(CantCreateError, match="could not write manifest"):
            store._write_tsv(target, [("a",)])

    def test_unknown_package_in_the_closure_gets_a_placeholder_version(self, monkeypatch):
        fake_fetch(monkeypatch, fields={"Package": "other", "Version": "2", "Architecture": "all"})
        entry = store.build("cowsay")
        meta = json.loads((entry.path / META).read_text())
        assert meta["version"] == "?"
        assert meta["arch"] == "?"

    def test_tmp_leftover_from_a_crash_is_removed(self, monkeypatch):
        fake_fetch(monkeypatch)
        tmp = config.store_dir() / f".cowsay.tmp.{os.getpid()}"
        tmp.mkdir(parents=True)
        (tmp / "junk").write_text("x", encoding="utf-8")
        entry = store.build("cowsay")
        assert entry.pkg == "cowsay"
        assert not tmp.exists()

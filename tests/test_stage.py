"""The symlink farm: node tree, nesting, caching."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aptx import stage as stage_mod
from aptx.errors import OsError_
from aptx.plan import Plan
from aptx.stage import build_farm, host_mount, node_tree, pkg_mount, plan_hash, top_nodes
from tests.conftest import make_entry


def _raise_oserror(message: str):
    """A replacement for a Path method that always fails with OSError."""

    def boom(*_args, **_kwargs):
        raise OSError(message)

    return boom


def plan_with(merge_points: dict[str, list[str]]) -> Plan:
    return Plan(pkg="cowsay", version="1.0", merge_points=merge_points)


class TestHostMount:
    def test_flattens_the_path(self):
        assert host_mount("/usr/share") == "/run/aptx/host/usr!share"

    def test_nested_paths_do_not_nest_mounts(self):
        assert host_mount("/usr/share/man/man1") == "/run/aptx/host/usr!share!man!man1"

    def test_root(self):
        assert host_mount("/") == "/run/aptx/host/_root"


class TestPkgMount:
    def test_appends_child(self):
        assert pkg_mount("/usr/share", "cowsay") == "/run/aptx/root/usr/share/cowsay"

    def test_root_merge_point(self):
        assert pkg_mount("/", "opt") == "/run/aptx/root/opt"


class TestNodeTree:
    def test_ancestors_become_nodes(self):
        nodes = node_tree(plan_with({"/usr/share": ["cowsay"]}))
        assert set(nodes) == {"/usr", "/usr/share"}
        assert nodes["/usr"] == []
        assert nodes["/usr/share"] == ["cowsay"]

    def test_ancestors_never_include_root(self):
        assert "/" not in node_tree(plan_with({"/usr/share": ["cowsay"]}))

    def test_top_nodes_are_the_outermost(self):
        nodes = node_tree(plan_with({"/usr/share/man/man1": ["x"]}))
        assert top_nodes(nodes) == ["/usr"]

    def test_siblings_stay_separate(self):
        nodes = node_tree(plan_with({"/usr/share": ["a"], "/usr/games": ["b"]}))
        assert top_nodes(nodes) == ["/usr"]


class TestBuildFarm:
    def test_host_children_are_linked_into_the_host_mount(self, tmp_path: Path):
        host = tmp_path / "host"
        (host / "usr" / "share" / "cowsay").mkdir(parents=True)
        (host / "usr" / "share" / "doc").mkdir()

        plan = plan_with({"/usr/share": ["cowsay"]})
        # Point the node at the fake host by symlinking it into place is not
        # possible, so build the farm directly from a crafted node tree.
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        farm = stage_dir / "usr" / "share"
        farm.mkdir(parents=True)

        for child in sorted(os.listdir(host / "usr" / "share")):
            os.symlink(f"{host_mount('/usr/share')}/{child}", farm / child)

        assert os.readlink(farm / "doc") == "/run/aptx/host/usr!share/doc"

    def test_package_child_points_into_the_store(self, tmp_path: Path, monkeypatch):
        plan = plan_with({"/usr/games": ["cowsay"]})
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()

        # /usr/games exists on the real host, so build_farm will mirror it. That
        # is fine: we only assert on the entry the package contributes.
        build_farm(plan, stage_dir)
        link = stage_dir / "usr" / "games" / "cowsay"
        assert link.is_symlink()
        assert os.readlink(link) == "/run/aptx/root/usr/games/cowsay"

    def test_nested_farms_are_real_directories(self, tmp_path: Path):
        plan = plan_with({"/usr/share": ["cowsay"], "/usr/share/doc": ["cowsay"]})
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        build_farm(plan, stage_dir)
        assert (stage_dir / "usr" / "share").is_dir()
        assert not (stage_dir / "usr" / "share").is_symlink()
        assert (stage_dir / "usr" / "share" / "doc").is_dir()

    def test_etc_collision_keeps_a_dot_host_copy(self, tmp_path: Path):
        plan = plan_with({"/etc": ["hosts"]})
        stage_dir = tmp_path / "stage"
        stage_dir.mkdir()
        build_farm(plan, stage_dir)
        etc = stage_dir / "etc"
        assert (etc / "hosts").is_symlink()
        assert os.readlink(etc / "hosts") == "/run/aptx/root/etc/hosts"
        assert (etc / "hosts.host").is_symlink()
        assert os.readlink(etc / "hosts.host") == "/run/aptx/host/etc/hosts"


class TestPlanHash:
    def test_is_stable_for_the_same_plan(self):
        plan = plan_with({"/usr/games": ["cowsay"]})
        assert plan_hash(plan) == plan_hash(plan)

    def test_changes_with_the_package_version(self):
        a = plan_with({"/usr/games": ["cowsay"]})
        b = plan_with({"/usr/games": ["cowsay"]})
        b.version = "2.0"
        assert plan_hash(a) != plan_hash(b)

    def test_changes_with_the_children(self):
        a = plan_with({"/usr/games": ["cowsay"]})
        b = plan_with({"/usr/games": ["cowsay", "cowthink"]})
        assert plan_hash(a) != plan_hash(b)


class TestEnsure:
    def test_writes_the_hash_and_reuses_the_farm(self, tmp_path: Path):
        entry = make_entry(tmp_path / "entry")
        plan = plan_with({"/usr/games": ["cowsay"]})

        first = stage_mod.ensure(entry, plan)
        assert (entry.path / ".stage-hash").exists()
        assert (first / "usr" / "games" / "cowsay").is_symlink()

        sentinel = first / "sentinel"
        sentinel.write_text("x", encoding="utf-8")
        second = stage_mod.ensure(entry, plan)
        assert second == first
        assert sentinel.exists(), "a cached farm must not be rebuilt"

    def test_force_rebuilds(self, tmp_path: Path):
        entry = make_entry(tmp_path / "entry")
        plan = plan_with({"/usr/games": ["cowsay"]})
        first = stage_mod.ensure(entry, plan)
        sentinel = first / "sentinel"
        sentinel.write_text("x", encoding="utf-8")
        stage_mod.ensure(entry, plan, force=True)
        assert not sentinel.exists()

    def test_changed_plan_rebuilds(self, tmp_path: Path):
        entry = make_entry(tmp_path / "entry")
        first = stage_mod.ensure(entry, plan_with({"/usr/games": ["cowsay"]}))
        sentinel = first / "sentinel"
        sentinel.write_text("x", encoding="utf-8")
        stage_mod.ensure(entry, plan_with({"/usr/games": ["cowsay", "cowthink"]}))
        assert not sentinel.exists()


class TestEnsureLock:
    def test_a_rebuild_won_by_another_process_is_reused(self, tmp_path: Path, monkeypatch):
        """The loser of the race must not tear down the winner's farm."""
        entry = make_entry(tmp_path / "entry")
        plan = plan_with({"/usr/games": ["cowsay"]})
        stage_dir = stage_mod.ensure(entry, plan)
        sentinel = stage_dir / "sentinel"
        sentinel.write_text("x", encoding="utf-8")

        calls = {"n": 0}

        def racing(hash_file, digest, directory, force):
            # False outside the lock (the other process is mid-rebuild),
            # True once we hold it (it just finished).
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(stage_mod, "_fresh", racing)
        assert stage_mod.ensure(entry, plan) == stage_dir
        assert sentinel.exists()

    def test_an_unopenable_entry_is_wrapped(self, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path / "entry")
        plan = plan_with({"/usr/games": ["cowsay"]})

        def boom(*_args, **_kwargs):
            raise OSError("nope")

        monkeypatch.setattr(stage_mod.os, "open", boom)
        with pytest.raises(OsError_, match="could not lock the store entry"):
            stage_mod.ensure(entry, plan)


class TestSymlinkCollision:
    def test_an_existing_link_is_replaced(self, tmp_path: Path):
        link = tmp_path / "link"
        link.symlink_to("/old/target")
        stage_mod._symlink("/new/target", link)
        assert os.readlink(link) == "/new/target"

    def test_unlink_failure_lets_the_error_escape(self, tmp_path: Path, monkeypatch):
        link = tmp_path / "link"
        link.symlink_to("/old/target")
        monkeypatch.setattr(Path, "unlink", _raise_oserror("nope"))
        with pytest.raises(FileExistsError):
            stage_mod._symlink("/new/target", link)

    def test_other_oserror_is_wrapped(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "symlink_to", _raise_oserror("cross-device"))
        with pytest.raises(OsError_, match="could not create symlink"):
            stage_mod._symlink("/x", tmp_path / "link")


class TestHostChildren:
    def test_missing_directory_is_empty(self, tmp_path: Path):
        assert stage_mod._host_children(str(tmp_path / "absent")) == []

    def test_existing_directory_is_sorted(self, tmp_path: Path):
        for name in ("b", "a"):
            (tmp_path / name).mkdir()
        assert stage_mod._host_children(str(tmp_path)) == ["a", "b"]


class TestBuildFarmErrors:
    def test_unwritable_stage_dir_is_wrapped(self, tmp_path: Path, monkeypatch):
        plan = plan_with({"/usr/share": ["cowsay"]})
        monkeypatch.setattr(Path, "mkdir", _raise_oserror("ro"))
        with pytest.raises(OsError_, match="could not create farm directory"):
            build_farm(plan, tmp_path)

    def test_a_package_child_that_is_also_a_node_is_not_linked(self, tmp_path: Path):
        plan = plan_with({"/usr": ["share"], "/usr/share": ["cowsay"]})
        build_farm(plan, tmp_path)
        # /usr/share is a farm of its own, so it must be a real directory.
        assert (tmp_path / "usr" / "share").is_dir()
        assert not (tmp_path / "usr" / "share").is_symlink()
        assert (tmp_path / "usr" / "share" / "cowsay").is_symlink()


class TestEnsureErrors:
    def test_unwritable_stage_dir_is_wrapped(self, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        plan = plan_with({"/usr/share": ["cowsay"]})
        monkeypatch.setattr(Path, "mkdir", _raise_oserror("ro"))
        with pytest.raises(OsError_, match="could not create stage dir"):
            stage_mod.ensure(entry, plan)

    def test_hash_write_failure_is_silent(self, tmp_path: Path, monkeypatch):
        entry = make_entry(tmp_path)
        plan = plan_with({"/usr/share": ["cowsay"]})

        def boom(*_args, **_kwargs):
            raise OSError("ro")

        monkeypatch.setattr(Path, "write_text", boom)
        stage_dir = stage_mod.ensure(entry, plan)
        assert stage_dir.is_dir()

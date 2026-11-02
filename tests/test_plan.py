"""Plan computation: shadows, merge points, direct binds."""

from __future__ import annotations

from pathlib import Path

import pytest

from aptx import plan as plan_mod
from aptx.plan import Plan, build, shadow_root
from tests.conftest import make_entry

HOST = {"/", "/usr", "/usr/games", "/usr/share", "/usr/bin", "/etc"}


def host_exists(path: str) -> bool:
    return path in HOST


class TestShadowRoot:
    def test_returns_first_missing_ancestor(self):
        assert shadow_root("/usr/share/cowsay/cows/default.cow", host_exists) == "/usr/share/cowsay"

    def test_returns_path_itself_when_parent_exists(self):
        assert shadow_root("/usr/games/cowsay", host_exists) == "/usr/games/cowsay"

    def test_walks_up_multiple_levels(self):
        assert shadow_root("/opt/a/b/c", host_exists) == "/opt"

    def test_existing_leaf_is_not_a_shadow(self):
        # /etc/passwd exists: its parent exists, so the shadow root is the leaf.
        assert shadow_root("/etc/passwd", host_exists) == "/etc/passwd"


class TestBuild:
    def test_shadow_subtree_merges_at_existing_parent(self, tmp_path: Path):
        entry = make_entry(
            tmp_path,
            provides=[("d", "/usr/share/cowsay", "cowsay"), ("f", "/usr/share/cowsay/cows/apt.cow", "cowsay")],
        )
        result = build(entry, host_exists)
        assert result.merge_points == {"/usr/share": ["cowsay"]}
        assert result.shadow_dirs == ["/usr/share/cowsay"]

    def test_existing_parent_becomes_merge_point(self, tmp_path: Path):
        entry = make_entry(tmp_path, provides=[("f", "/usr/games/cowsay", "cowsay")])
        result = build(entry, host_exists)
        assert result.merge_points == {"/usr/games": ["cowsay"]}
        assert result.shadow_dirs == []

    def test_package_directories_are_not_their_own_bind(self, tmp_path: Path):
        """A package lists /usr too; binding it would hide the whole host tree."""
        entry = make_entry(
            tmp_path,
            provides=[
                ("d", "/usr", "cowsay"),
                ("d", "/usr/games", "cowsay"),
                ("f", "/usr/games/cowsay", "cowsay"),
            ],
        )
        result = build(entry, host_exists)
        assert result.direct_binds == []
        assert "/usr" not in result.merge_points

    def test_top_level_shadow_becomes_direct_bind(self, tmp_path: Path):
        entry = make_entry(tmp_path, provides=[("f", "/opt/thing/bin/run", "thing")])
        result = build(entry, host_exists)
        assert result.direct_binds == ["/opt"]
        assert result.merge_points == {}

    def test_merge_files_lists_only_host_collisions(self, tmp_path: Path):
        host = HOST | {"/usr/games/cowthink"}
        entry = make_entry(
            tmp_path,
            provides=[
                ("f", "/usr/games/cowsay", "cowsay"),
                ("l", "/usr/games/cowthink", "cowsay"),
            ],
        )
        result = build(entry, lambda p: p in host)
        assert result.merge_files == ["/usr/games/cowthink"]

    def test_etc_merge_is_classified(self, tmp_path: Path):
        entry = make_entry(tmp_path, provides=[("f", "/etc/aptx.conf", "thing")])
        result = build(entry, host_exists)
        assert result.etc_merge == ["/etc"]

    def test_python_dist_packages_are_classified(self, tmp_path: Path):
        host = HOST | {"/usr/lib", "/usr/lib/python3", "/usr/lib/python3/dist-packages"}
        entry = make_entry(
            tmp_path,
            provides=[("f", "/usr/lib/python3/dist-packages/thing.py", "thing")],
        )
        result = build(entry, lambda p: p in host)
        assert result.python_dirs == ["/usr/lib/python3/dist-packages"]

    def test_type_mismatch_is_a_conflict(self, tmp_path: Path):
        host = HOST | {"/usr/games/cowsay"}  # host has a *file* there
        entry = make_entry(tmp_path, provides=[("d", "/usr/games/cowsay", "cowsay")])
        result = build(entry, lambda p: p in host)
        assert [c["path"] for c in result.conflicts] == ["/usr/games/cowsay"]
        assert result.conflicts[0]["reason"] == "type mismatch"

    def test_merge_point_that_is_a_host_file_is_a_conflict(self, tmp_path: Path):
        """Nothing can be merged into a path the host has as a regular file."""
        entry = make_entry(tmp_path, provides=[("f", "/usr/games/cowsay", "cowsay")])
        result = build(
            entry,
            lambda path: path in HOST,
            isdir=lambda path: path != "/usr/games",
        )
        assert [c["path"] for c in result.conflicts] == ["/usr/games/cowsay"]
        assert "is a file on the host" in result.conflicts[0]["reason"]
        assert result.merge_points == {}

    def test_version_is_read_from_meta(self, entry):
        assert build(entry, host_exists).version == "1.0"


class TestRender:
    def test_render_is_compact(self, tmp_path: Path):
        """More than 20 entries in a section must be truncated, not dumped."""
        files = {f"/usr/share/d{i}" for i in range(50)}
        entry = make_entry(
            tmp_path,
            provides=[("f", f"/usr/share/d{i}", "thing") for i in range(50)],
        )
        result = build(
            entry,
            lambda path: path == "/usr/share" or path in files,
            isdir=lambda path: path == "/usr/share",
        )
        assert len(result.merge_files) == 50
        text = plan_mod.render(result)
        assert "package: cowsay 1.0" in text
        assert "… and 30 more" in text
        assert len(text.splitlines()) < 40


class TestToDict:
    def test_shape(self, entry):
        data = build(entry, host_exists).to_dict()
        assert set(data) >= {"pkg", "version", "merge_points", "shadow_dirs", "direct_binds"}
        assert isinstance(Plan(pkg="x").to_dict()["merge_points"], dict)


@pytest.mark.parametrize("path", ["/", ""])
def test_root_entries_are_ignored(tmp_path: Path, path: str):
    entry = make_entry(tmp_path, provides=[("d", path, "cowsay")])
    result = build(entry, host_exists)
    assert result.merge_points == {}
    assert result.direct_binds == []


class TestShadowRootEdgeCases:
    def test_single_segment_under_root(self):
        assert shadow_root("/opt", lambda path: path == "/") == "/opt"

    def test_nothing_exists_walks_up_to_the_filesystem_root(self):
        # Never finds an existing parent, so the shadow root is "/" itself.
        assert shadow_root("/a/b/c", lambda path: False) == "/"
        assert shadow_root("/nope/deep", lambda path: False) == "/"


class TestRenderSections:
    def make_plan(self, **kwargs) -> Plan:
        plan = Plan(pkg="p", version="1.0")
        for key, value in kwargs.items():
            setattr(plan, key, value)
        return plan

    def test_shadow_dirs_are_truncated(self):
        plan = self.make_plan(shadow_dirs=[f"/opt/d{i}" for i in range(30)])
        text = plan_mod.render(plan)
        assert "shadow dirs (30):" in text
        assert "… and 10 more" in text

    def test_direct_binds_are_listed(self):
        plan = self.make_plan(direct_binds=["/opt/tool"])
        assert "direct binds (1):\n  /opt/tool" in plan_mod.render(plan)

    def test_etc_merge_is_listed(self):
        plan = self.make_plan(etc_merge=["/etc/thing"])
        assert "etc merge (1):\n  /etc/thing" in plan_mod.render(plan)

    def test_python_dirs_are_listed(self):
        plan = self.make_plan(python_dirs=["/usr/lib/python3/dist-packages"])
        assert "python dirs (1):" in plan_mod.render(plan)

    def test_conflicts_are_listed(self):
        plan = self.make_plan(conflicts=[{"path": "/x", "host": "d", "pkg": "f"}])
        assert "conflicts (1):\n  /x  host=d pkg=f" in plan_mod.render(plan)

    def test_conflicts_are_truncated(self):
        plan = self.make_plan(
            conflicts=[{"path": f"/x{i}", "host": "d", "pkg": "f"} for i in range(15)]
        )
        text = plan_mod.render(plan)
        assert "conflicts (15):" in text
        assert text.count("  /x") == 10

    def test_empty_optional_sections_are_omitted(self):
        text = plan_mod.render(self.make_plan())
        for heading in ("direct binds", "etc merge", "python dirs", "conflicts"):
            assert heading not in text


class TestHostLexists:
    def test_existing_directory(self, tmp_path: Path):
        assert plan_mod.host_lexists(str(tmp_path)) is True

    def test_dangling_symlink_counts(self, tmp_path: Path):
        link = tmp_path / "dangling"
        link.symlink_to(tmp_path / "absent")
        assert plan_mod.host_lexists(str(link)) is True

    def test_absent_path(self, tmp_path: Path):
        assert plan_mod.host_lexists(str(tmp_path / "absent")) is False


class TestIsDir:
    def test_directory(self, tmp_path: Path):
        assert plan_mod._is_dir(str(tmp_path)) is True

    def test_file(self, tmp_path: Path):
        target = tmp_path / "file"
        target.write_text("x", encoding="utf-8")
        assert plan_mod._is_dir(str(target)) is False

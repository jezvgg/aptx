"""fetch: apt/dpkg output parsing, without touching the real tools."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from aptx import fetch
from aptx.errors import DataError, OsError_, UnavailableError


def fake(monkeypatch, stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Replace ``fetch.proc.run`` and record the argv it was called with."""
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append([str(part) for part in argv])
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(fetch.proc, "run", runner)
    return calls


class TestDebPath:
    def test_executable_file(self):
        assert fetch.DebPath("f", "/usr/bin/x", mode="-rwxr-xr-x").executable

    def test_group_execute_bit_counts(self):
        assert fetch.DebPath("f", "/usr/bin/x", mode="-rw-r-xr--").executable

    def test_other_execute_bit_counts(self):
        assert fetch.DebPath("f", "/usr/bin/x", mode="-------r-x").executable

    def test_setuid_counts(self):
        assert fetch.DebPath("f", "/usr/bin/x", mode="-rwsr-xr-x").executable

    def test_sticky_counts(self):
        assert fetch.DebPath("f", "/usr/bin/x", mode="-rwxr-xr-t").executable

    def test_plain_file_is_not_executable(self):
        assert not fetch.DebPath("f", "/usr/share/x", mode="-rw-r--r--").executable

    def test_directories_are_never_executable(self):
        assert not fetch.DebPath("d", "/usr/bin", mode="drwxr-xr-x").executable

    def test_short_mode_is_not_executable(self):
        assert not fetch.DebPath("f", "/usr/bin/x", mode="rwx").executable


class TestResolveClosure:
    def test_follows_depends_and_headers(self, monkeypatch):
        out = (
            "cowsay\n"
            "  Depends: perl\n"
            "  Depends: libc6\n"
            "perl\n"
            "  PreDepends: dpkg\n"
        )
        fake(monkeypatch, out)
        assert fetch.resolve_closure("cowsay") == ["cowsay", "dpkg", "libc6", "perl"]

    def test_ignores_unwanted_relations(self, monkeypatch):
        out = "cowsay\n  Conflicts: nginx\n  Suggests: fortune\n  Breaks: x\n"
        fake(monkeypatch, out)
        assert fetch.resolve_closure("cowsay") == ["cowsay"]

    def test_recommends_only_when_asked(self, monkeypatch):
        out = "cowsay\n  Recommends: fortune\n"
        fake(monkeypatch, out)
        assert fetch.resolve_closure("cowsay") == ["cowsay"]
        assert fetch.resolve_closure("cowsay", recommends=True) == ["cowsay", "fortune"]

    def test_alternatives_are_parsed(self, monkeypatch):
        out = "pkg\n  |Depends: alt\n"
        fake(monkeypatch, out)
        assert "alt" in fetch.resolve_closure("pkg")

    def test_architecture_suffix_is_stripped(self, monkeypatch):
        out = "pkg\n  Depends: libc6:amd64\n"
        fake(monkeypatch, out)
        assert "libc6" in fetch.resolve_closure("pkg")

    def test_virtual_relation_target_is_skipped(self, monkeypatch):
        out = "pkg\n  Depends: <perlapi-5.40.0>\n"
        fake(monkeypatch, out)
        assert fetch.resolve_closure("pkg") == ["pkg"]

    def test_virtual_block_header_is_skipped(self, monkeypatch):
        out = "pkg\n<perlapi-5.40.0>\n  perl\n"
        fake(monkeypatch, out)
        assert "<perlapi-5.40.0>" not in fetch.resolve_closure("pkg")

    def test_indented_provider_is_kept(self, monkeypatch):
        out = "pkg\n    real-provider\n"
        fake(monkeypatch, out)
        assert "real-provider" in fetch.resolve_closure("pkg")

    def test_indented_virtual_provider_is_skipped(self, monkeypatch):
        out = "pkg\n    <virtual>\n"
        fake(monkeypatch, out)
        assert fetch.resolve_closure("pkg") == ["pkg"]

    def test_blank_lines_are_ignored(self, monkeypatch):
        fake(monkeypatch, "\n\npkg\n\n")
        assert fetch.resolve_closure("pkg") == ["pkg"]

    def test_the_package_itself_is_always_included(self, monkeypatch):
        fake(monkeypatch, "")
        assert fetch.resolve_closure("pkg") == ["pkg"]

    def test_missing_package_raises(self, monkeypatch):
        fake(monkeypatch, "E: no such package", returncode=100)
        with pytest.raises(DataError, match="package not found"):
            fetch.resolve_closure("nope")

    def test_recommends_flag_reaches_argv(self, monkeypatch):
        calls = fake(monkeypatch, "pkg\n")
        fetch.resolve_closure("pkg")
        assert "--no-recommends" in calls[0]
        calls.clear()
        fetch.resolve_closure("pkg", recommends=True)
        assert "--no-recommends" not in calls[0]


class TestInstalledStatus:
    def test_empty_list_skips_the_query(self, monkeypatch):
        calls = fake(monkeypatch)
        assert fetch.installed_status([]) == {}
        assert calls == []

    def test_parses_status_and_name(self, monkeypatch):
        fake(monkeypatch, "ii \tcowsay\nrc \told\n")
        assert fetch.installed_status(["cowsay", "old"]) == {"cowsay": "ii", "old": "rc"}

    def test_skips_blank_and_malformed_lines(self, monkeypatch):
        fake(monkeypatch, "\nnot-a-row\nii \tok\n")
        assert fetch.installed_status(["ok"]) == {"ok": "ii"}


class FakeProgress:
    """Stands in for ``rich.progress.Progress`` inside ``fetch``."""

    instances: list["FakeProgress"] = []

    def __init__(self, *columns, **kwargs):
        self.columns = columns
        self.kwargs = kwargs
        self.updates: list[tuple[int, float | None]] = []
        self.description = ""
        self.total = None
        self.closed = False
        FakeProgress.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False

    def add_task(self, description, total=None):
        self.description = description
        self.total = total
        return 7

    def update(self, task, completed=None):
        self.updates.append((task, completed))


class FakeChild:
    """A ``Popen`` stand-in that writes to its sink and finishes after 2 polls."""

    def __init__(self, argv, cwd=None, stdout=None, stderr=None, text=None):
        self.argv = argv
        self.cwd = cwd
        self.polls = 0
        if stdout is not None:
            stdout.write("Get:1 cowsay\n")

    def poll(self):
        self.polls += 1
        return None if self.polls < 3 else 0

    @property
    def returncode(self):
        return 0


@pytest.fixture
def progress(monkeypatch):
    FakeProgress.instances = []
    monkeypatch.setattr(fetch, "Progress", FakeProgress)
    monkeypatch.setattr(fetch.time, "sleep", lambda _seconds: None)
    return FakeProgress


class TestDownloadSizes:
    def test_reads_size_from_print_uris(self, monkeypatch):
        stdout = (
            "'https://mirror/debian/git.deb' git_1%3a2.47_amd64.deb 8861572 SHA256:abc\n"
            "'https://mirror/debian/x.deb' x_1.0_amd64.deb 512 SHA256:def\n"
        )
        fake(monkeypatch, stdout=stdout)
        sizes = fetch.download_sizes(["git", "x"])
        assert sizes == {"git_1%3a2.47_amd64.deb": 8861572, "x_1.0_amd64.deb": 512}

    def test_failure_gives_no_sizes(self, monkeypatch):
        fake(monkeypatch, returncode=100)
        assert fetch.download_sizes(["git"]) == {}

    def test_ignores_short_lines_and_bad_numbers(self, monkeypatch):
        fake(monkeypatch, stdout="junk\na b\na b 12\na b notanumber\n")
        assert fetch.download_sizes(["x"]) == {"b": 12}


class TestDownloaded:
    def test_sums_file_sizes(self, tmp_path: Path):
        (tmp_path / "a.deb").write_bytes(b"x" * 10)
        (tmp_path / "b.deb").write_bytes(b"y" * 5)
        assert fetch._downloaded(tmp_path) == 15

    def test_missing_directory_is_zero(self, tmp_path: Path):
        assert fetch._downloaded(tmp_path / "gone") == 0

    def test_unreadable_entry_is_skipped(self, tmp_path: Path, monkeypatch):
        (tmp_path / "a.deb").write_bytes(b"x" * 3)
        real_stat = Path.stat

        def flaky(self, *args, **kwargs):
            if self.name == "a.deb":
                raise OSError("nope")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)
        assert fetch._downloaded(tmp_path) == 0


class TestDownload:
    def test_no_specs_is_a_noop(self, monkeypatch, tmp_path: Path):
        calls = fake(monkeypatch)
        fetch.download([], tmp_path)
        assert calls == []

    def test_passes_specs_after_the_separator(self, monkeypatch, tmp_path: Path):
        calls = fake(monkeypatch)
        fetch.download(["cowsay=1.0"], tmp_path)
        assert calls[-1] == ["apt-get", "download", "--", "cowsay=1.0"]

    def test_probes_sizes_before_downloading(self, monkeypatch, tmp_path: Path):
        calls = fake(monkeypatch)
        fetch.download(["cowsay"], tmp_path)
        assert calls[0] == ["apt-get", "download", "--print-uris", "-y", "--", "cowsay"]

    def test_failure_reports_the_last_line(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, stderr="E: boom\nE: could not fetch", returncode=100)
        with pytest.raises(UnavailableError, match="could not fetch"):
            fetch.download(["cowsay"], tmp_path)

    def test_failure_without_output(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, returncode=1)
        with pytest.raises(UnavailableError, match="apt-get download failed$"):
            fetch.download(["cowsay"], tmp_path)


class TestDownloadWithBar:
    def test_known_total_drives_a_progress_bar(self, monkeypatch, tmp_path: Path, progress):
        fake(monkeypatch, stdout="'u' cowsay_1.0.deb 100 SHA256:x\n")
        monkeypatch.setattr(fetch.subprocess, "Popen", FakeChild)

        fetch.download(["cowsay"], tmp_path)

        bar = progress.instances[0]
        assert bar.description == "downloading 1 package"
        assert bar.total == 100.0
        assert bar.closed
        assert [completed for _task, completed in bar.updates] == [0, 0, 0]

    def test_plural_label_for_several_packages(self, monkeypatch, tmp_path: Path, progress):
        fake(monkeypatch, stdout="'u' a.deb 10 SHA256:x\n'u' b.deb 10 SHA256:y\n")
        monkeypatch.setattr(fetch.subprocess, "Popen", FakeChild)

        fetch.download(["a", "b"], tmp_path)

        assert progress.instances[0].description == "downloading 2 packages"

    def test_unknown_total_falls_back_to_a_plain_run(self, monkeypatch, tmp_path: Path, progress):
        calls = fake(monkeypatch)
        fetch.download(["cowsay"], tmp_path)
        assert progress.instances == []
        assert calls[-1] == ["apt-get", "download", "--", "cowsay"]

    def test_child_failure_is_raised(self, monkeypatch, tmp_path: Path, progress):
        fake(monkeypatch, stdout="'u' cowsay.deb 5 SHA256:x\n")

        class Failing(FakeChild):
            @property
            def returncode(self):
                return 100

        monkeypatch.setattr(fetch.subprocess, "Popen", Failing)
        with pytest.raises(UnavailableError, match="apt-get download failed"):
            fetch.download(["cowsay"], tmp_path)


class TestDebMetadata:
    def test_sha256(self, tmp_path: Path):
        target = tmp_path / "file.deb"
        target.write_bytes(b"payload")
        assert fetch.sha256_file(target) == hashlib.sha256(b"payload").hexdigest()

    def test_fields_are_parsed_from_the_control_block(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "Package: cowsay\nVersion: 1.0\n\nDescription: multi\n line\n")
        got = fetch.deb_fields(tmp_path / "x.deb", "Package", "Version", "Missing")
        assert got == {"Package": "cowsay", "Version": "1.0", "Missing": ""}

    def test_fields_ignore_continuation_lines(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "Package: p\n Depends: continuation\n")
        assert fetch.deb_fields(tmp_path / "x.deb", "Package") == {"Package": "p"}

    def test_fields_ignore_lines_without_a_colon(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "junk line\nPackage: p\n")
        assert fetch.deb_fields(tmp_path / "x.deb", "Package") == {"Package": "p"}


class TestDebContents:
    def test_parses_files_dirs_and_links(self, monkeypatch, tmp_path: Path):
        out = (
            "drwxr-xr-x root/root         0 2024-01-01 00:00 ./usr/\n"
            "-rwxr-xr-x root/root        10 2024-01-01 00:00 ./usr/games/cowsay\n"
            "lrwxrwxrwx root/root         0 2024-01-01 00:00 ./usr/bin/cowthink -> ../games/cowthink\n"
        )
        fake(monkeypatch, out)
        entries = fetch.deb_contents(tmp_path / "x.deb")
        assert [e.path for e in entries] == [
            "/usr",
            "/usr/games/cowsay",
            "/usr/bin/cowthink",
        ]
        assert [e.kind for e in entries] == ["d", "f", "l"]
        assert entries[2].target == "../games/cowthink"
        assert entries[1].executable is True

    def test_skips_the_root_entry(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "drwxr-xr-x root/root 0 2024-01-01 00:00 ./\n")
        assert fetch.deb_contents(tmp_path / "x.deb") == []

    def test_skips_short_lines(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "-rw-r--r-- root/root 1 2024\n")
        assert fetch.deb_contents(tmp_path / "x.deb") == []

    def test_skips_unknown_kinds(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "zrw-r--r-- root/root 1 2024-01-01 00:00 ./weird\n")
        assert fetch.deb_contents(tmp_path / "x.deb") == []

    def test_adds_a_leading_slash(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "-rw-r--r-- root/root 1 2024-01-01 00:00 usr/share/x\n")
        assert [e.path for e in fetch.deb_contents(tmp_path / "x.deb")] == ["/usr/share/x"]

    def test_all_slash_names_are_skipped(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "-rw-r--r-- root/root 1 2024-01-01 00:00 //\n")
        assert fetch.deb_contents(tmp_path / "x.deb") == []

    def test_blocks_and_pipes_count_as_files(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, "brw-r--r-- root/root 1 2024-01-01 00:00 ./dev/block\n")
        assert fetch.deb_contents(tmp_path / "x.deb")[0].kind == "f"


class TestUnpack:
    def test_success(self, monkeypatch, tmp_path: Path):
        calls = fake(monkeypatch)
        fetch.unpack(tmp_path / "x.deb", tmp_path)
        assert calls[0][:2] == ["dpkg", "-x"]

    def test_failure_raises_oserr(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, stderr="dpkg-deb: error: bad archive", returncode=2)
        with pytest.raises(OsError_, match="bad archive"):
            fetch.unpack(tmp_path / "x.deb", tmp_path)

    def test_failure_without_detail(self, monkeypatch, tmp_path: Path):
        fake(monkeypatch, returncode=2)
        with pytest.raises(OsError_, match="could not unpack x.deb$"):
            fetch.unpack(tmp_path / "x.deb", tmp_path)


class TestTempDir:
    def test_creates_a_directory_with_the_prefix(self):
        created = fetch.temp_dir("aptx-test-")
        try:
            assert created.is_dir()
            assert created.name.startswith("aptx-test-")
        finally:
            fetch.rmtree(created)


class TestRmtree:
    def test_removes_a_tree(self, tmp_path: Path):
        victim = tmp_path / "victim"
        (victim / "nested").mkdir(parents=True)
        (victim / "nested" / "file").write_text("x", encoding="utf-8")
        fetch.rmtree(victim)
        assert not victim.exists()

    def test_missing_path_is_silent(self, tmp_path: Path):
        fetch.rmtree(tmp_path / "absent")

    def test_failure_only_warns(self, monkeypatch, tmp_path: Path, capsys):
        victim = tmp_path / "victim"
        victim.mkdir()

        def boom(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(fetch.shutil, "rmtree", boom)
        fetch.rmtree(victim)
        assert "could not remove" in capsys.readouterr().err

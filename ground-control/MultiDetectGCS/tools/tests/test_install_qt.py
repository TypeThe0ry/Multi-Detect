#!/usr/bin/env python3
"""Tests for tools/setup/install_qt.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from setup import install_qt
from setup.aqt_qt611_windows_compat import qt_611_repository_extension
from setup.install_qt import (
    _run_aqt_with_retries,
    aqt_command_prefix,
    compute_cache_digest,
    find_aqt_executable,
    needs_qt_611_windows_repo_compat,
    resolve_android_qt_root,
    resolve_arch_dir,
    resolve_qt_root,
    validate_aqt_source,
)


class TestResolveArchDir:
    def test_linux_gcc_64(self) -> None:
        assert resolve_arch_dir("linux_gcc_64") == "gcc_64"

    def test_linux_arm64(self) -> None:
        assert resolve_arch_dir("linux_arm64") == "arm64"

    def test_win64_msvc2022_64(self) -> None:
        assert resolve_arch_dir("win64_msvc2022_64") == "msvc2022_64"

    def test_win64_msvc2022_arm64_cross_compiled(self) -> None:
        assert resolve_arch_dir("win64_msvc2022_arm64_cross_compiled") == "msvc2022_arm64"

    def test_clang_64_maps_to_macos(self) -> None:
        assert resolve_arch_dir("clang_64") == "macos"

    def test_android_arm64_v8a_unchanged(self) -> None:
        assert resolve_arch_dir("android_arm64_v8a") == "android_arm64_v8a"

    def test_ios_unchanged(self) -> None:
        assert resolve_arch_dir("ios") == "ios"


class TestComputeCacheDigest:
    def test_deterministic(self) -> None:
        a = compute_cache_digest("qtgraphs qtlocation", "")
        b = compute_cache_digest("qtgraphs qtlocation", "")
        assert a == b

    def test_different_modules_differ(self) -> None:
        a = compute_cache_digest("qtgraphs", "")
        b = compute_cache_digest("qtlocation", "")
        assert a != b

    def test_archives_affect_digest(self) -> None:
        a = compute_cache_digest("qtgraphs", "")
        b = compute_cache_digest("qtgraphs", "icu")
        assert a != b

    def test_returns_hex_string(self) -> None:
        d = compute_cache_digest("", "")
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)


class TestResolveQtRoot:
    def test_valid_path(self, tmp_path: Path) -> None:
        qt_root = tmp_path / "6.8.3" / "gcc_64"
        qt_root.mkdir(parents=True)
        result = resolve_qt_root(tmp_path, "6.8.3", "gcc_64")
        assert result == qt_root

    def test_missing_path_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            resolve_qt_root(tmp_path, "6.8.3", "gcc_64")


class TestResolveAndroidQtRoot:
    def test_arm64_preferred(self) -> None:
        roots = {"arm64": "/qt/arm64", "armv7": "/qt/armv7"}
        assert resolve_android_qt_root("arm64-v8a;armeabi-v7a", roots) == "/qt/arm64"

    def test_armv7_fallback(self) -> None:
        roots = {"arm64": "", "armv7": "/qt/armv7"}
        assert resolve_android_qt_root("arm64-v8a;armeabi-v7a", roots) == "/qt/armv7"

    def test_x86_64_only(self) -> None:
        roots = {"x86_64": "/qt/x86_64"}
        assert resolve_android_qt_root("x86_64", roots) == "/qt/x86_64"

    def test_x86_only(self) -> None:
        roots = {"x86": "/qt/x86"}
        assert resolve_android_qt_root("x86", roots) == "/qt/x86"

    def test_no_match_exits(self) -> None:
        with pytest.raises(SystemExit):
            resolve_android_qt_root("mips", {})

    def test_empty_root_skipped(self) -> None:
        roots = {"arm64": "", "x86_64": "/qt/x86_64"}
        assert resolve_android_qt_root("arm64-v8a;x86_64", roots) == "/qt/x86_64"

    def test_semicolon_parsing(self) -> None:
        roots = {"armv7": "/qt/armv7"}
        assert resolve_android_qt_root("armeabi-v7a", roots) == "/qt/armv7"


class TestValidateAqtSource:
    def test_empty_passes(self) -> None:
        assert validate_aqt_source("") == ""

    def test_bare_pypi_name(self) -> None:
        assert validate_aqt_source("aqtinstall") == "aqtinstall"

    def test_pinned_pypi_version(self) -> None:
        assert validate_aqt_source("aqtinstall==3.3.0") == "aqtinstall==3.3.0"

    def test_upstream_git_sha(self) -> None:
        spec = "git+https://github.com/miurahr/aqtinstall@" + "a" * 40
        assert validate_aqt_source(spec) == spec

    def test_upstream_git_with_dot_git(self) -> None:
        spec = "git+https://github.com/miurahr/aqtinstall.git@" + "f" * 40
        assert validate_aqt_source(spec) == spec

    def test_extra_index_url_rejected(self) -> None:
        with pytest.raises(SystemExit):
            validate_aqt_source("--extra-index-url https://evil aqtinstall")

    def test_attacker_git_host_rejected(self) -> None:
        with pytest.raises(SystemExit):
            validate_aqt_source("git+https://attacker.example.com/evil@main")

    def test_unpinned_git_tag_rejected(self) -> None:
        with pytest.raises(SystemExit):
            validate_aqt_source("git+https://github.com/miurahr/aqtinstall@main")

    def test_different_package_rejected(self) -> None:
        with pytest.raises(SystemExit):
            validate_aqt_source("evil-package")


class TestRunAqtWithRetries:
    @staticmethod
    def _fake_run(returncodes: list[int], calls: list[list[str]]):
        seq = iter(returncodes)

        def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
            calls.append(args)
            return subprocess.CompletedProcess(args, next(seq))

        return _run

    def test_succeeds_first_try(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(install_qt.subprocess, "run", self._fake_run([0], calls))
        _run_aqt_with_retries(["aqt", "install-qt"])
        assert len(calls) == 1

    def test_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(install_qt.subprocess, "run", self._fake_run([254, 0], calls))
        monkeypatch.setattr(install_qt.time, "sleep", lambda _s: None)
        _run_aqt_with_retries(["aqt", "install-qt"])
        assert len(calls) == 2

    def test_raises_after_exhausting_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(install_qt.subprocess, "run", self._fake_run([254] * 3, calls))
        monkeypatch.setattr(install_qt.time, "sleep", lambda _s: None)
        with pytest.raises(subprocess.CalledProcessError):
            _run_aqt_with_retries(["aqt", "install-qt"])
        assert len(calls) == install_qt._AQT_MAX_ATTEMPTS


def test_find_aqt_executable_falls_back_to_active_virtualenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    aqt = scripts / ("aqt.exe" if sys.platform == "win32" else "aqt")
    python.touch()
    aqt.touch()

    monkeypatch.setattr(install_qt.shutil, "which", lambda _name: None)
    monkeypatch.setattr(install_qt.sys, "executable", str(python))

    assert find_aqt_executable() == str(aqt)


class TestQt611WindowsRepositoryCompatibility:
    def test_enabled_only_for_qt_611_or_newer_windows_msvc_desktop(self) -> None:
        common = {
            "host": "windows",
            "target": "desktop",
            "arch": "win64_msvc2022_64",
        }

        assert needs_qt_611_windows_repo_compat(**common, version="6.11.0")
        assert needs_qt_611_windows_repo_compat(**common, version="6.12.1")
        assert not needs_qt_611_windows_repo_compat(**common, version="6.10.3")
        assert not needs_qt_611_windows_repo_compat(
            host="linux",
            target="desktop",
            version="6.11.1",
            arch="linux_gcc_64",
        )
        assert not needs_qt_611_windows_repo_compat(
            host="windows",
            target="desktop",
            version="6.11.1",
            arch="win64_mingw1310_64",
        )

    def test_repository_extension_matches_the_new_official_folder(self) -> None:
        fallback_calls: list[tuple[str, bool]] = []

        def fallback(architecture: str, is_version_ge_6: bool) -> str:
            fallback_calls.append((architecture, is_version_ge_6))
            return "fallback"

        assert qt_611_repository_extension("win64_msvc2022_64", True, fallback) == "msvc2022_64"
        assert (
            qt_611_repository_extension(
                "win64_msvc2022_arm64_cross_compiled",
                True,
                fallback,
            )
            == "msvc2022_arm64_cross_compiled"
        )
        assert qt_611_repository_extension("linux_gcc_64", True, fallback) == "fallback"
        assert fallback_calls == [("linux_gcc_64", True)]

    def test_command_uses_the_shim_only_when_required(self) -> None:
        shim = aqt_command_prefix(
            "aqt",
            host="windows",
            target="desktop",
            version="6.11.1",
            arch="win64_msvc2022_64",
        )
        normal = aqt_command_prefix(
            "aqt",
            host="windows",
            target="desktop",
            version="6.10.3",
            arch="win64_msvc2022_64",
        )

        assert shim[0] == sys.executable
        assert shim[1].endswith("aqt_qt611_windows_compat.py")
        assert normal == ["aqt"]


def test_qt_611_modules_include_tasktree_required_by_qml_asset_downloader() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = json.loads((repo_root / ".github" / "build-config.json").read_text())

    assert "qttasktree" in config["qt"]["modules"].split()

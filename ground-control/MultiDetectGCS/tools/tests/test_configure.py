#!/usr/bin/env python3
"""Tests for tools/configure.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from configure import CMakeConfig, configure, find_qt_cmake, parse_version


class TestParseVersion:
    def test_standard_version(self) -> None:
        path = Path("/home/user/Qt/6.8.0/gcc_64/bin/qt-cmake")
        assert parse_version(path) == (6, 8, 0)

    def test_double_digit_minor(self) -> None:
        path = Path("/home/user/Qt/6.10.1/gcc_64/bin/qt-cmake")
        assert parse_version(path) == (6, 10, 1)

    def test_no_version_returns_zeros(self) -> None:
        path = Path("/usr/bin/qt-cmake")
        assert parse_version(path) == (0, 0, 0)


class TestCMakeConfig:
    def test_defaults(self) -> None:
        config = CMakeConfig()
        assert config.build_type == "Debug"
        assert config.generator == "Ninja"
        assert config.testing is False
        assert config.coverage is False
        assert config.unity_build is False

    def test_custom_values(self) -> None:
        config = CMakeConfig(
            build_type="Release",
            testing=True,
            unity_build=True,
            unity_batch_size=32,
        )
        assert config.build_type == "Release"
        assert config.testing is True
        assert config.unity_batch_size == 32


class TestFindQtCmake:
    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        with (
            patch("configure.Path.home", return_value=tmp_path),
            patch.dict("os.environ", {}, clear=True),
        ):
            result = find_qt_cmake(tmp_path / "nonexistent")
        assert result is None

    def test_finds_explicit_path(self, tmp_path: Path) -> None:
        qt_cmake = tmp_path / "bin" / "qt-cmake"
        qt_cmake.parent.mkdir(parents=True)
        qt_cmake.touch(mode=0o755)
        result = find_qt_cmake(tmp_path)
        assert result is not None
        assert result.name == "qt-cmake"


class TestConfigure:
    def test_unity_is_scoped_to_qgc_targets(self, tmp_path: Path) -> None:
        config = CMakeConfig(
            source_dir=tmp_path,
            build_dir=tmp_path / "build",
            unity_build=True,
            unity_batch_size=32,
            use_qt_cmake=False,
        )
        with (
            patch("configure.subprocess.run") as run,
            patch("configure.write_github_output"),
        ):
            run.return_value.returncode = 0
            assert configure(config) == 0

        args = run.call_args.args[0]
        assert "-DCMAKE_UNITY_BUILD=OFF" in args
        assert "-DQGC_UNITY_BUILD=ON" in args
        assert "-DQGC_UNITY_BUILD_BATCH_SIZE=32" in args
        assert "-DCMAKE_UNITY_BUILD=ON" not in args

    def test_non_unity_config_clears_a_stale_qgc_cache_value(self, tmp_path: Path) -> None:
        config = CMakeConfig(
            source_dir=tmp_path,
            build_dir=tmp_path / "build",
            use_qt_cmake=False,
        )
        with (
            patch("configure.subprocess.run") as run,
            patch("configure.write_github_output"),
        ):
            run.return_value.returncode = 0
            assert configure(config) == 0

        args = run.call_args.args[0]
        assert "-DCMAKE_UNITY_BUILD=OFF" in args
        assert "-DQGC_UNITY_BUILD=OFF" in args


def test_actuator_sources_are_excluded_from_unity_namespace_collisions() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cmake = (repo_root / "src" / "Vehicle" / "Actuators" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "set(_qgc_actuator_unity_unsafe_sources" in cmake
    assert "TARGET_DIRECTORY ${CMAKE_PROJECT_NAME}" in cmake
    assert "SKIP_UNITY_BUILD_INCLUSION ON" in cmake
    for source in (
        "ActuatorActions.cc",
        "ActuatorOutputs.cc",
        "Actuators.cc",
        "ActuatorTesting.cc",
        "Common.cc",
        "GeometryImage.cc",
        "Mixer.cc",
        "MotorAssignment.cc",
    ):
        assert source in cmake


def test_qml_object_models_are_excluded_from_unity_odr_collisions() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cmake = (repo_root / "src" / "QmlControls" / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "QmlObjectListModel.cc" in cmake
    assert "QmlObjectTreeModel.cc" in cmake
    assert "TARGET_DIRECTORY ${CMAKE_PROJECT_NAME}" in cmake
    assert "SKIP_UNITY_BUILD_INCLUSION ON" in cmake


def test_tcp_and_serial_links_are_excluded_from_unity_timeout_collisions() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cmake = (repo_root / "src" / "Comms" / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "set(_qgc_comms_unity_unsafe_sources TCPLink.cc)" in cmake
    assert "list(APPEND _qgc_comms_unity_unsafe_sources SerialLink.cc)" in cmake
    assert "TARGET_DIRECTORY ${CMAKE_PROJECT_NAME}" in cmake
    assert "SKIP_UNITY_BUILD_INCLUSION ON" in cmake


def test_dataflash_parsers_are_excluded_from_unity_helper_collisions() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cmake = (repo_root / "src" / "AnalyzeView" / "LogViewer" / "CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "APMDataFlash/APMDataFlashLogParser.cc" in cmake
    assert "APMDataFlash/LogViewerDataFlashParser.cc" in cmake
    assert "TARGET_DIRECTORY ${CMAKE_PROJECT_NAME}" in cmake
    assert "SKIP_UNITY_BUILD_INCLUSION ON" in cmake


def test_autopilot_plugin_moc_sees_complete_vehicle_component_type() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    header = (repo_root / "src" / "AutoPilotPlugins" / "AutoPilotPlugin.h").read_text(
        encoding="utf-8"
    )

    assert 'Q_MOC_INCLUDE("VehicleComponent.h")' in header

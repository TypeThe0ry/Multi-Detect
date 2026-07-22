from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_firmware_plugin_subdirectories_follow_disable_options() -> None:
    cmake = (REPO_ROOT / "src/FirmwarePlugin/CMakeLists.txt").read_text(encoding="utf-8")

    apm_gate = "if(NOT QGC_DISABLE_APM_PLUGIN)\n    add_subdirectory(APM)\nendif()"
    px4_gate = "if(NOT QGC_DISABLE_PX4_PLUGIN)\n    add_subdirectory(PX4)\nendif()"

    assert apm_gate in cmake
    assert px4_gate in cmake
    assert cmake.count("add_subdirectory(APM)") == 1
    assert cmake.count("add_subdirectory(PX4)") == 1


def test_common_vehicle_code_does_not_require_apm_headers_when_disabled() -> None:
    vehicle = (REPO_ROOT / "src/Vehicle/Vehicle.cc").read_text(encoding="utf-8")

    include_guard = '#ifndef QGC_NO_ARDUPILOT_DIALECT\n#include "APM.h"\n#endif'
    assert include_guard in vehicle

    motor_interlock = vehicle[vehicle.index("void Vehicle::motorInterlock(bool enable)") :]
    motor_interlock = motor_interlock[
        : motor_interlock.index(
            "/*---------------------------------------------------------------------------*/"
        )
    ]
    assert "#ifndef QGC_NO_ARDUPILOT_DIALECT" in motor_interlock
    assert "Q_UNUSED(enable)" in motor_interlock


def test_px4_sensors_setup_receives_a_nonzero_setup_page_viewport() -> None:
    sensor_component = (
        REPO_ROOT / "src/AutoPilotPlugins/PX4/SensorsComponent.qml"
    ).read_text(encoding="utf-8")
    assert "id: sensorsPage" in sensor_component
    assert "property string sectionNameFilter" in sensor_component
    assert "return sensorsSetup.sectionVisible(name)" in sensor_component
    assert "anchors.fill:       parent" in sensor_component
    assert "sectionNameFilter:  sensorsPage.sectionNameFilter" in sensor_component
    assert "\nSetupPage {" not in sensor_component

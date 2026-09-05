from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[2]
CUSTOM_ROOT = REPO_ROOT / "custom"
PRODUCT_VERSION_MANIFEST = REPO_ROOT.parents[1] / "configs" / "product-version.json"


def _read(relative_path: str) -> str:
    return (CUSTOM_ROOT / relative_path).read_text(encoding="utf-8")


def _cmake_string_value(source: str, variable: str) -> str:
    match = re.search(rf'set\(\s*{re.escape(variable)}\s+"([^"]+)"', source)
    assert match is not None, f"missing CMake string variable {variable}"
    return match.group(1)


def test_custom_build_contains_the_required_native_qgc_surfaces() -> None:
    required = {
        "CMakeLists.txt",
        "cmake/CustomOverrides.cmake",
        "custom.qrc",
        "src/CustomPlugin.h",
        "src/CustomPlugin.cc",
        "src/MultiDetectOperatorController.h",
        "src/MultiDetectOperatorController.cc",
        "src/MultiDetectOperatorProtocol.h",
        "src/MultiDetectOperatorProtocol.cc",
        "tests/MultiDetectOperatorProtocolSelfTest.cc",
        "tests/operator_closed_loop_hil.py",
        "src/FlyViewVideo.qml",
        "src/FlyViewCustomLayer.qml",
        "src/MultiDetectConfigure.qml",
        "src/SelectViewDropdown.qml",
        "src/PlanViewRightPanel.qml",
        "res/Custom/MultiDetect/MultiDetectState.qml",
        "res/Custom/MultiDetect/MultiDetectVideoOverlay.qml",
        "res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml",
    }

    missing = sorted(path for path in required if not (CUSTOM_ROOT / path).is_file())

    assert not missing


def test_multidetect_overlay_reuses_native_qgc_surface_and_palette() -> None:
    video = _read("src/FlyViewVideo.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    # Multi-Detect is an overlay layer inside QGC's existing Fly View.  Keep
    # this contract explicit so a future feature cannot silently introduce a
    # second simulator window or a parallel control style.
    assert "PipState {" in video
    assert "FlightDisplayViewVideo {" in video
    assert "import QGroundControl.Controls" in video
    assert "QGCPalette {" in layer
    assert "ScreenTools.defaultFontPixelWidth" in layer
    assert "QGCLabel {" in layer
    assert "QGCButton {" in layer
    assert "QGCButton {" in overlay
    assert re.search(r"^\s*Button\s*{", overlay, re.MULTILINE) is None
    assert "color: qgcPal.window" in layer
    assert "QGCPalette {" in indicator
    assert "color: control._healthColor" in indicator
    assert "qgcPal.warningText" in indicator
    assert "qgcPal.colorGreen" in layer
    # Runtime identity must inherit QGC's palette inside the native toolbar
    # instead of introducing a parallel hard-coded status palette.
    assert not re.search(r"#[0-9a-fA-F]{6,8}", indicator)
    # The target card and action overlay inherit QGC's palette.
    # The only fixed colours left in the video surface are the depth false-
    # colour legend, which is data encoding rather than control styling.
    assert not re.search(r"#[0-9a-fA-F]{6,8}", layer)
    overlay_controls = overlay[:overlay.index("// Fixed depth false-colour ramp")]
    assert not re.search(r"#[0-9a-fA-F]{6,8}", overlay_controls)
    assert "Qt.rgba(qgcPal.warningText.r, qgcPal.warningText.g, qgcPal.warningText.b, 0.10)" in overlay_controls
    assert "Qt.rgba(1.0, 0.83, 0.23, 0.10)" not in overlay_controls
    assert "readonly property color _lockActionTextColor: qgcPal.colorRed" in overlay
    assert "return qgcPal.warningText" in overlay
    assert not re.search(r"\b(ApplicationWindow|Window)\s*{", video + layer + indicator)


def test_isolated_hil_grabs_only_the_unique_native_qgc_video_output() -> None:
    plugin = _read("src/CustomPlugin.cc")
    video = _read("src/FlyViewVideo.qml")

    # The export is an acceptance-only capability, not a permissive runtime
    # environment flag. A relative/empty prefix or a production launch must
    # leave both context properties entirely unexposed.
    export_start = plugin.index("const QString configuredVideoGrabPrefix")
    export_end = plugin.index("_selector = new CustomOverrideInterceptor", export_start)
    export_block = plugin[export_start:export_end]
    assert '#include <QtCore/QFileInfo>' in plugin
    assert 'contains(QStringLiteral("--isolated-hil"))' in export_block
    assert (
        'runtimeEnvironmentExplicitlyTrue("MULTIDETECT_HIL_VIDEO_GRAB_EXPORT")'
        in export_block
    )
    assert "QFileInfo(configuredVideoGrabPrefix).isAbsolute()" in export_block
    assert (
        'runtimeEnvironmentFlagEnabled("MULTIDETECT_HIL_VIDEO_GRAB_EXPORT")'
        not in plugin
    )
    guarded_properties = export_block[export_block.index("if (hilVideoGrabExport) {") :]
    assert 'QStringLiteral("multiDetectHilVideoGrabExport")' in guarded_properties
    assert 'QStringLiteral("multiDetectHilVideoGrabPrefix")' in guarded_properties

    # Discovery and the sole grab call stay inside the existing additive
    # MultiDetectVideoOverlay. They traverse the native FlightDisplayViewVideo
    # tree and fail closed unless objectName=videoContent is unique.
    overlay_start = video.index("MultiDetectVideoOverlay {")
    overlay_end = video.index("ProximityRadarVideoView", overlay_start)
    hook = video[overlay_start:overlay_end]
    assert 'candidate.objectName === "videoContent"' in hook
    assert "function _isActiveNativeVideoOutput(candidate)" in hook
    assert "current.visible !== true || current.width <= 0 || current.height <= 0" in hook
    assert "current === videoStreaming" in hook
    assert "_collectNativeVideoOutputs(videoStreaming, matches)" in hook
    assert "if (matches.length !== 1)" in hook
    assert "function _nativeOutputDevicePixelRatio(nativeOutput)" in hook
    assert "nativeOutput.window" in hook
    assert "nativeOutput.Window.window" in hook
    assert "quickWindow.devicePixelRatio" in hook
    assert "Number.isFinite(snapshot.device_pixel_ratio)" in hook
    assert hook.count(".grabToImage(") == 1
    assert video.count(".grabToImage(") == 1
    assert "QGroundControl.multiVehicleManager.activeVehicle" in hook
    assert "activeVehicle.vtol && activeVehicle.vtolInFwdFlight" in hook
    assert '_queueNativeVideoGrab("pip-swapped")' in hook
    assert '_queueNativeVideoGrab("fixed-wing")' in hook
    assert hook.index('_queueNativeVideoGrab("pip-swapped")') < hook.index(
        '_queueNativeVideoGrab("fixed-wing")'
    )
    assert "_root.pipState.state !== _root.pipState.fullState" in hook
    assert 'multiDetectHilVideoGrabPrefix + "-" + role + ".png"' in hook

    for field in (
        '"schema": "multidetect.qgc-hil-video-grab"',
        '"schema_version": 1',
        '"sequence": _hilVideoGrabSequence',
        '"recorded_at_utc": new Date().toISOString()',
        '"role": role',
        '"object_name": snapshot.object_name',
        '"native_output_match_count": snapshot.native_output_match_count',
        '"output_width": snapshot.output_width',
        '"output_height": snapshot.output_height',
        '"device_pixel_ratio": snapshot.device_pixel_ratio',
        '"decoded_width": snapshot.decoded_width',
        '"decoded_height": snapshot.decoded_height',
        '"capture_width": snapshot.capture_width',
        '"capture_height": snapshot.capture_height',
        '"decoding": snapshot.decoding',
        '"output_visible": snapshot.output_visible',
        '"video_full_state": snapshot.video_full_state',
        '"vtol_in_fwd_flight": snapshot.vtol_in_fwd_flight',
        '"queued": queued',
        '"saved": saved',
        '"path": path',
    ):
        assert field in hook
    assert "Qt.size(snapshot.grab_width, snapshot.grab_height)" in hook
    assert "result.image" not in hook
    assert 'console.info("MULTIDETECT_HIL_VIDEO_GRAB " + JSON.stringify({' in hook

    # No parallel presentation or decoder surface is accepted by this hook.
    forbidden_component = re.compile(
        r"^\s*(?:ApplicationWindow|Window|VideoOutput|MediaPlayer)\s*\{",
        re.MULTILINE,
    )
    assert forbidden_component.search(video) is None
    assert "import QtMultimedia" not in video


def test_fly_view_actions_use_qgc_native_dialog_and_slider_controls() -> None:
    layer = _read("src/FlyViewCustomLayer.qml")

    # Confirmation, authorization and evidence history stay inside QGC's
    # standard dialog lifecycle and control theme.  Raw Qt Quick Controls here
    # would silently reintroduce independent margins, focus and handle styling.
    assert re.search(r"^\s*Popup\s*{", layer, re.MULTILINE) is None
    assert re.search(r"^\s*Slider\s*{", layer, re.MULTILINE) is None
    assert layer.count("QGCPopupDialogFactory {") >= 4
    assert layer.count("QGCPopupDialog {") >= 4
    assert layer.count("QGCSlider {") == 2
    for dialog in (
        "targetEvidenceHistoryDialog",
        "payloadActionDialog",
        "approachActionDialog",
        "authorizationDialog",
    ):
        assert f"id: {dialog}Factory" in layer
        assert f"id: {dialog}Component" in layer


def test_isolated_hil_acceptance_keeps_native_close_path_and_avoids_windows_qml_teardown_race() -> None:
    application = (REPO_ROOT / "src" / "QGCApplication.cc").read_text(encoding="utf-8")
    main = (REPO_ROOT / "src" / "main.cc").read_text(encoding="utf-8")
    main_window = (REPO_ROOT / "src" / "MainWindow" / "MainWindow.qml").read_text(
        encoding="utf-8"
    )

    # The acceptance driver must still invoke QGC's production close function,
    # not invent a second shutdown path.  Windows QML/global-singleton
    # destruction is intentionally skipped only for this short-lived process;
    # otherwise a post-Exit SEH can make a passed UI gate look failed.
    assert 'QMetaObject::invokeMethod(root, "finishCloseProcess", Qt::DirectConnection)' in application
    assert 'qEnvironmentVariableIsSet("MULTIDETECT_HIL_UI_AUTO_EXIT")' in application
    assert "_qmlAppEngine->setParent(nullptr);" in application
    assert "std::_Exit(exitCode);" in main
    assert 'args.isolatedHilMode && qEnvironmentVariableIsSet("MULTIDETECT_HIL_UI_AUTO_EXIT")' in main
    assert "function finishCloseProcess()" in main_window
    assert "QGroundControl.linkManager.shutdown()" in main_window
    assert "QGroundControl.videoManager.stopVideo()" in main_window


def test_closed_loop_driver_can_require_a_real_external_sitl_vehicle() -> None:
    driver = _read("tests/operator_closed_loop_hil.py")

    assert '"--metadata-only"' in driver
    assert "MAV_TYPE_ONBOARD_CONTROLLER" in driver
    # The driver constructs both the Jetson component-191 and PX4 autopilot
    # serializers through one parameterized signed-MAVLink helper.  Keep the
    # assertion tied to the helper contract rather than a single call-site
    # formatting detail.
    assert "def _signed_serializer(key: bytes, *, system_id: int, component_id: int)" in driver
    assert "heartbeat_serializer = _signed_serializer(signing_key, system_id=1, component_id=191)" in driver
    assert "if args.metadata_only:" in driver
    assert '"autopilot_heartbeats_sent": autopilot_heartbeats_sent' in driver
    assert '"external_autopilot_required": args.metadata_only' in driver
    assert "except ConnectionResetError:" in driver
    assert '"udp_connection_resets": udp_connection_resets' in driver


def test_custom_app_uses_a_valid_cmake_target_name() -> None:
    overrides = _read("cmake/CustomOverrides.cmake")
    app_name = _cmake_string_value(overrides, "QGC_APP_NAME")

    assert re.fullmatch(r"[A-Za-z0-9_.+-]+", app_name)
    assert (
        _cmake_string_value(overrides, "QGC_APP_DESCRIPTION")
        == "Multi-Detect Ground Control Station"
    )


def test_native_telemetry_grid_uses_display_only_airspeed_noise_gate() -> None:
    core_plugin = (REPO_ROOT / "src/API/QGCCorePlugin.cc").read_text(encoding="utf-8")
    fact_header = (
        REPO_ROOT / "src/Vehicle/FactGroups/VehicleFactGroup.h"
    ).read_text(encoding="utf-8")
    fact_source = (
        REPO_ROOT / "src/Vehicle/FactGroups/VehicleFactGroup.cc"
    ).read_text(encoding="utf-8")
    grid_source = (REPO_ROOT / "src/QmlControls/FactValueGrid.cc").read_text(
        encoding="utf-8"
    )
    assert core_plugin.count('QStringLiteral("AirSpeedDisplay")') == 1
    assert 'setFact("Vehicle", "AirSpeedDisplay")' in core_plugin
    assert "Fact *airSpeedDisplay()" in fact_header
    assert "_airSpeedDisplayFact" in fact_header
    assert "airSpeed()->setRawValue(airSpeedMps);" in fact_source
    assert "airSpeedDisplay()->setRawValue(" in fact_source
    assert "kAirSpeedDisplayEnterThresholdMps = 2.0" in fact_source
    assert "kAirSpeedDisplayExitThresholdMps = 1.5" in fact_source
    assert 'QStringLiteral("TelemetryBarUserSettings")' in grid_source
    assert 'factName = QStringLiteral("AirSpeedDisplay")' in grid_source


def test_custom_app_has_numeric_and_human_facing_product_versions() -> None:
    overrides = _read("cmake/CustomOverrides.cmake")
    options = (REPO_ROOT / "cmake/CustomOptions.cmake").read_text(encoding="utf-8")
    git_cmake = (REPO_ROOT / "cmake/modules/Git.cmake").read_text(encoding="utf-8")
    install_cmake = (REPO_ROOT / "cmake/install/Install.cmake").read_text(encoding="utf-8")
    version = json.loads(PRODUCT_VERSION_MANIFEST.read_text(encoding="utf-8"))

    assert version["release_label"] == "0.3.0-pre.8"
    assert version["numeric"] == "0.3.0"
    assert version["python"] == "0.3.0rc8"
    assert version["channel"] == "prerelease"
    assert "configs/product-version.json" in overrides
    assert _cmake_string_value(overrides, "QGC_APP_VERSION_OVERRIDE") == (
        "${_multidetect_numeric_version}"
    )
    assert _cmake_string_value(overrides, "QGC_APP_RELEASE_LABEL") == (
        "${_multidetect_release_label}"
    )
    assert _cmake_string_value(overrides, "QGC_WINDOWS_INSTALLER_FILENAME") == (
        "MultiDetectGCS-v${_multidetect_release_label}-windows-amd64.exe"
    )
    assert 'set(QGC_APP_VERSION_OVERRIDE ""' in options
    assert 'set(QGC_APP_RELEASE_LABEL ""' in options
    assert "if(QGC_APP_VERSION_OVERRIDE)" in git_cmake
    assert 'set(QGC_APP_VERSION "${QGC_APP_VERSION_OVERRIDE}")' in git_cmake
    assert 'set(QGC_APP_VERSION_STR "v${QGC_APP_RELEASE_LABEL}-${QGC_GIT_HASH}")' in git_cmake
    assert "QGC_WINDOWS_INSTALLER_FILENAME" in install_cmake


def test_custom_app_has_a_unique_cross_platform_package_identifier() -> None:
    overrides = _read("cmake/CustomOverrides.cmake")
    gradle = (REPO_ROOT / "android/build.gradle").read_text(encoding="utf-8")
    logger = (
        REPO_ROOT / "android/src/org/mavlink/qgroundcontrol/QGCLogger.java"
    ).read_text(encoding="utf-8")
    activity = (
        REPO_ROOT / "android/src/org/mavlink/qgroundcontrol/QGCActivity.java"
    ).read_text(encoding="utf-8")

    assert _cmake_string_value(overrides, "QGC_PACKAGE_NAME") == "com.multidetect.gcs"
    assert _cmake_string_value(overrides, "QGC_ANDROID_PACKAGE_NAME") == "com.multidetect.gcs"
    assert "namespace androidPackageName" in gradle
    assert "applicationId androidPackageName" not in gradle
    assert "unitTests.returnDefaultValues = true" in gradle
    assert "BuildConfig.DEBUG" not in logger
    assert "ApplicationInfo.FLAG_DEBUGGABLE" in logger
    assert "QGCLogger.initialize(this);" in activity


def test_android_receivers_and_api_specific_sdl_code_are_lint_hardened() -> None:
    qgc_usb = (
        REPO_ROOT / "android/src/org/mavlink/qgroundcontrol/QGCUsbSerialManager.java"
    ).read_text(encoding="utf-8")
    sdl_hid = (
        REPO_ROOT / "android/src/org/libsdl/app/HIDDeviceManager.java"
    ).read_text(encoding="utf-8")
    sdl_controller = (
        REPO_ROOT / "android/src/org/libsdl/app/SDLControllerManager.java"
    ).read_text(encoding="utf-8")

    assert "ContextCompat.RECEIVER_NOT_EXPORTED" in qgc_usb
    assert "ContextCompat.RECEIVER_EXPORTED" in sdl_hid
    assert "@TargetApi(Build.VERSION_CODES.Q)" in sdl_controller


def test_build_summary_reports_release_ipo_from_the_config_specific_flag() -> None:
    helpers = (REPO_ROOT / "cmake/Helpers.cmake").read_text(encoding="utf-8")
    summary = (REPO_ROOT / "cmake/PrintSummary.cmake").read_text(encoding="utf-8")

    assert "set(QGC_IPO_ENABLED TRUE PARENT_SCOPE)" in helpers
    assert "if(QGC_IPO_ENABLED OR CMAKE_INTERPROCEDURAL_OPTIMIZATION)" in summary


def test_windows_installer_excludes_developer_files_and_debug_crt() -> None:
    installer = (REPO_ROOT / "deploy/windows/nullsoft_installer.nsi").read_text(encoding="utf-8")

    assert "/x include /x lib" in installer
    for debug_runtime in (
        "msvcp140d.dll",
        "msvcp140_1d.dll",
        "msvcp140_2d.dll",
        "msvcp140d_atomic_wait.dll",
        "msvcp140d_codecvt_ids.dll",
        "vcruntime140d.dll",
        "vcruntime140_1d.dll",
        "concrt140d.dll",
    ):
        assert f"/x {debug_runtime}" in installer


def test_gstreamer_summary_counts_alternate_satisfied_plugins() -> None:
    orchestrator = (REPO_ROOT / "cmake/GStreamer/Orchestrator.cmake").read_text(encoding="utf-8")

    assert "if(GST_PLUGIN_${_p}_FOUND)" in orchestrator


def test_custom_resource_overrides_only_the_intended_fly_plan_and_settings_files() -> None:
    resource_tree = ElementTree.parse(CUSTOM_ROOT / "custom.qrc")
    aliases = {
        element.attrib["alias"]
        for element in resource_tree.findall(".//file")
        if "alias" in element.attrib and element.attrib["alias"].endswith(".qml")
    }

    assert aliases == {
        "QGroundControl/FlyView/FlyViewCustomLayer.qml",
        "QGroundControl/FlyView/FlyViewVideo.qml",
        "QGroundControl/PlanView/PlanViewRightPanel.qml",
        "QGroundControl/AppSettings/MultiDetectConfigure.qml",
        "QGroundControl/Toolbar/SelectViewDropdown.qml",
    }


def test_operator_metadata_bridge_has_compile_reviewed_fail_closed_boundaries() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    implementation = "\n".join(
        _read(path)
        for path in (
            "CMakeLists.txt",
            "cmake/CustomOverrides.cmake",
            "src/CustomPlugin.cc",
            "src/MultiDetectOperatorController.cc",
            "src/MultiDetectOperatorProtocol.cc",
            "src/FlyViewVideo.qml",
            "src/FlyViewCustomLayer.qml",
            "res/Custom/MultiDetect/MultiDetectState.qml",
            "res/Custom/MultiDetect/MultiDetectVideoOverlay.qml",
        )
    )

    assert "readonly property bool fixedWingAimControlEnabled" in state
    assert "readonly property bool physicalReleaseEnabled: false" in state
    assert "MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES=0" in implementation
    assert "MULTIDETECT_PHYSICAL_RELEASE=0" in implementation
    assert "物理输出保持锁定" in state
    assert "static_assert(MULTIDETECT_QGC_DIRECT_PIXHAWK_WRITES == 0" in implementation
    assert "static_assert(MULTIDETECT_PHYSICAL_RELEASE == 0" in implementation
    assert "mavlink_msg_tunnel_pack_chan" in implementation
    assert "MAVLINK_IFLAG_SIGNED" in implementation
    assert "QMessageAuthenticationCode::hash" in implementation

    forbidden_control_paths = {
        "sendMavCommand",
        "mavlink_msg_command_long",
        "mavlink_msg_command_int",
        "MAVLINK_MSG_ID_PARAM_SET",
        "MAVLINK_MSG_ID_MISSION_ITEM",
        "QUdpSocket",
        "QSerialPort",
        "writeDatagram",
        "MAVLINK_MSG_ID_PARAM_EXT_SET",
        "MAVLINK_MSG_ID_MANUAL_CONTROL",
    }
    assert not {token for token in forbidden_control_paths if token in implementation}


def test_signed_runtime_status_uses_one_shared_fly_view_badge_and_fails_closed() -> None:
    plugin = _read("src/CustomPlugin.cc")
    protocol_header = _read("src/MultiDetectOperatorProtocol.h")
    protocol_source = _read("src/MultiDetectOperatorProtocol.cc")
    controller_header = _read("src/MultiDetectOperatorController.h")
    controller_source = _read("src/MultiDetectOperatorController.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    assert "RuntimeStatus = 23" in protocol_header
    assert 'QStringLiteral("REPLAY")' in protocol_source
    assert 'QStringLiteral("SIM")' in protocol_source
    assert 'QStringLiteral("HIL")' in protocol_source
    assert 'QStringLiteral("REAL")' in protocol_source
    assert 'QStringLiteral("backendHealth")' in protocol_source
    assert 'QStringLiteral("scenarioHash64")' in protocol_source
    assert 'QStringLiteral("airframeProfileHash64")' in protocol_source
    assert 'QStringLiteral("runHash64")' in protocol_source
    assert 'QStringLiteral("directPixhawkWrites"), false' in protocol_source
    assert 'Q_PROPERTY(QVariantMap runtimeStatus' in controller_header
    assert 'Q_PROPERTY(QString expectedRuntimeMode' in controller_header
    assert 'Q_PROPERTY(QString runtimeDisplayMode' in controller_header
    assert 'environmentValue("MULTIDETECT_EXPECTED_RUNTIME_MODE")' in controller_source
    assert 'return QStringLiteral("UNKNOWN")' in controller_source
    assert 'return QStringLiteral("MISMATCH")' in controller_source
    assert "!profileValid" in controller_source
    assert "!groundTruthIsolated || hardwareOutputsPresent" in controller_source
    assert "kRuntimeStatusStaleMs = 2000" in controller_source
    assert "readonly property string runtimeDisplayMode" in state
    assert "readonly property string runtimeRemoteMode" in state
    assert "readonly property string runtimeBackendHealth" in state
    assert "readonly property bool runtimeProfileValid" in state
    assert "readonly property bool runtimeEnvironmentHealthy" in state
    assert 'readonly property bool replayRuntimeIdentity: runtimeDisplayMode === "REPLAY"' in state
    assert "readonly property bool replayIsolationRequested" in state
    assert "readonly property bool vehicleLinkIsolationActive: vehicleLinkDisabled" in state
    assert "readonly property bool physicalActionIsolationActive" in state
    assert 'runtimeEnvironmentValue("MULTIDETECT_EXPECTED_RUNTIME_MODE")' in plugin
    assert 'runtimeEnvironmentFlagEnabled("MULTIDETECT_DISABLE_VEHICLE_LINK")' in plugin
    assert 'expectedRuntimeMode == QStringLiteral("REPLAY")' in plugin
    assert 'QStringLiteral("multiDetectVehicleLinkDisabled")' in plugin
    assert "id: runtimeIdentityBadge" not in layer
    assert 'qsTr("环境 UNKNOWN · 签名状态缺失、过期或配置无效")' in indicator
    assert 'qsTr("环境 MISMATCH · 期望 %1 / 收到 %2")' in indicator
    assert "MultiDetectState.runtimeDisplayMode" in indicator
    assert "property string runtimeDisplayMode" not in layer
    assert "MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL" not in controller_source
    assert not list(CUSTOM_ROOT.rglob("*Sim*.qml"))


def test_runtime_badge_keeps_runtime_identity_separate_from_vehicle_link_isolation() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    # Safety remains fail-closed for the same three REPLAY signals plus the
    # process-level link isolation flag, but only runtimeDisplayMode owns the
    # user-facing REPLAY identity.
    assert 'readonly property bool replayRuntimeIdentity: runtimeDisplayMode === "REPLAY"' in state
    assert 'expectedRuntimeMode === "REPLAY"' in state
    assert 'runtimeRemoteMode === "REPLAY"' in state
    assert "replayIsolationRequested || vehicleLinkIsolationActive" in state
    assert "readonly property bool replaySafetyActive: physicalActionIsolationActive" in state
    assert "function physicalActionIsolationStatusText()" in state

    badge_text = indicator[
        indicator.index("function _runtimeDetailText()") : indicator.index(
            "function _runtimeHealthShortText()"
        )
    ]
    replay_branch = badge_text.index("if (MultiDetectState.replayRuntimeIdentity)")
    vehicle_isolation_branch = badge_text.index(
        "if (MultiDetectState.vehicleLinkIsolationActive)"
    )
    assert badge_text.index('if (mode === "MISMATCH")') < replay_branch
    assert badge_text.index('if (mode === "UNKNOWN")') < replay_branch
    assert replay_branch < vehicle_isolation_branch
    assert "MultiDetectState.replaySafetyActive" not in badge_text
    assert 'qsTr("环境 REPLAY · 只读回放 · 车辆链路/执行/授权已隔离")' in badge_text
    assert 'qsTr("环境 %1 · 车辆链路/执行/授权已隔离").arg(mode)' in badge_text
    assert 'qsTr("REPLAY · 只读回放 · 车辆链路/执行/授权已隔离 · %1")' not in badge_text
    assert "MultiDetectState.replaySafetyStatusText()" not in layer + indicator
    assert layer.count("MultiDetectState.physicalActionIsolationStatusText()") >= 2

    # MISMATCH/UNKNOWN own the compact warning state, so a fail-closed process
    # flag cannot visually mask bad signed runtime identity.
    runtime_warning = indicator[
        indicator.index("readonly property bool _runtimeWarning") : indicator.index(
            "readonly property color _healthColor"
        )
    ]
    assert 'runtimeDisplayMode === "MISMATCH"' in runtime_warning
    assert 'runtimeDisplayMode === "UNKNOWN"' in runtime_warning
    assert "vehicleLinkIsolationActive" not in runtime_warning


def test_runtime_status_ack_is_type24_and_is_sent_only_after_native_adoption() -> None:
    protocol_header = _read("src/MultiDetectOperatorProtocol.h")
    controller_source = _read("src/MultiDetectOperatorController.cc")

    assert "RuntimeStatusAck = 24" in protocol_header
    invalid_direction = controller_source.index(
        "packet.type == MultiDetectOperatorProtocol::MessageType::RuntimeStatusAck"
    )
    authenticated_count = controller_source.index("_authenticatedPackets++")
    assert invalid_direction < authenticated_count

    adoption = controller_source.index("_runtimeStatus = fields;")
    timestamp = controller_source.index(
        "_lastRuntimeStatusAtMs = _lastAuthenticatedAtMs;", adoption
    )
    notify = controller_source.index("emit runtimeStatusChanged();", timestamp)
    encode = controller_source.index("_protocol.encodeRuntimeStatusAck(", notify)
    send = controller_source.index("_sendPayload(acknowledgement)", encode)
    success_log = controller_source.index(
        '"runtime status adopted and acknowledged sequence="', send
    )
    assert adoption < timestamp < notify < encode < send < success_log
    assert controller_source.index("_isNewStatus(") < adoption


def test_replay_isolates_the_persisted_vehicle_profile_and_all_physical_ui_actions() -> None:
    plugin = _read("src/CustomPlugin.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    isolation_branch = plugin[
        plugin.index("if (vehicleLinkIsolationRequested())") : plugin.index(
            "int profileIndex = -1;"
        )
    ]
    assert "isDefaultVehicleProfile" in isolation_branch
    assert "settings.remove(root);" in isolation_branch
    assert "settings.sync();" in isolation_branch
    assert "return;" in isolation_branch
    assert 'settings.setValue(root + QStringLiteral("/auto"), true);' in plugin

    assert "readonly property bool vehicleLinkDisabled" in state
    assert 'readonly property bool replayRuntimeIdentity: runtimeDisplayMode === "REPLAY"' in state
    assert "readonly property bool replayIsolationRequested" in state
    assert "readonly property bool vehicleLinkIsolationActive: vehicleLinkDisabled" in state
    assert "readonly property bool physicalActionIsolationActive" in state
    assert 'expectedRuntimeMode === "REPLAY"' in state
    assert 'runtimeRemoteMode === "REPLAY"' in state
    assert "readonly property bool replaySafetyActive" in state
    assert "readonly property bool physicalActionsAllowed: !replaySafetyActive" in state
    assert (
        "readonly property bool executionActionable: physicalActionsAllowed && vehicleArmed && "
        "lckActionable"
    ) in state
    assert 'qsTr("REPLAY 只读隔离：车辆链路、执行、授权、载荷与飞控动作均已禁用；' in state  # noqa: RUF001

    for function_name in (
        "refreshLockForExecution",
        "confirmApproachSlide",
        "confirmPayloadTargetSlide",
        "approveAuthorization",
        "denyAuthorization",
    ):
        function_start = state.index(f"function {function_name}(")
        function_body = state[function_start : function_start + 450]
        assert "if (replaySafetyActive)" in function_body
        assert "return false;" in function_body

    # Replay keeps only target-metadata interactions: selection, TRK/LCK and
    # exact-target cancellation still use the signed operator channel.
    for action in ("SELECT_TRK", "PROMOTE_LCK", "DEMOTE_TRK", "CANCEL_TRK"):
        assert f'_operator.sendTargetSelection("{action}"' in state

    assert 'qsTr("环境 REPLAY · 只读回放 · 车辆链路/执行/授权已隔离")' in indicator
    assert 'qsTr("环境 %1 · 车辆链路/执行/授权已隔离").arg(mode)' in indicator
    assert "function onReplaySafetyActiveChanged()" in layer
    assert "root._closePayloadActionDialog();" in layer
    assert "root._closeApproachActionDialog();" in layer
    assert "root._closeAuthorizationDialog();" in layer
    # Native QGCPopupDialog acceptance is fail-closed as well: both execution
    # dialogs require ARMED, while authorization requires the safety gate.
    assert layer.count("acceptButtonEnabled: !MultiDetectState.replaySafetyActive") >= 3
    assert layer.count("MultiDetectState.vehicleArmed") >= 2
    assert layer.count("MultiDetectState.safetyAllowed") >= 2


def test_depth_grid_rendering_is_coalesced_off_the_receive_path() -> None:
    controller = _read("src/MultiDetectOperatorController.cc")
    header = _read("src/MultiDetectOperatorController.h")

    assert "_depthGridRenderTimer.setInterval(200)" in controller
    assert "_pendingDepthGridFrame = frame" in controller
    assert "void MultiDetectOperatorController::_renderPendingDepthGrid()" in controller
    assert "std::optional<MultiDetectDepthGridFrame> _pendingDepthGridFrame" in header


def test_touch_selection_owns_the_pointer_only_in_explicit_selection_mode() -> None:
    video = _read("src/FlyViewVideo.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")

    # Keep QGC's native full-view pointer surface alive.  The Multi-Detect
    # overlay accepts only target hits and otherwise lets the stock
    # gimbal/camera click-drag path handle the event.
    assert "enabled:                    pipState.state === pipState.fullState" in video
    assert "enabled: root.interactionEnabled && MultiDetectState.selectionMode" in overlay
    assert "preventStealing: true" in overlay
    assert "propagateComposedEvents: false" in overlay
    assert "mouse.accepted = true" in overlay
    assert "interactionEnabled:     _root.pipState.state === _root.pipState.fullState" in video
    assert "MultiDetectVideoOverlay {" in video
    assert "MultiDetectVideoOverlay {" not in layer
    assert "root._dragStartX / root.width" in overlay
    assert "root._dragCurrentY / root.height" in overlay
    assert "if ((right - left) < 0.02 || (bottom - top) < 0.02)" in state
    assert 'trackingState = "INITIALIZING"' in state
    assert 'function onTrackStatusReceived(status)' in state
    assert "function _selectCandidateAt(pixelX, pixelY)" in overlay
    assert "function _handleStablePointerAt(pixelX, pixelY)" in overlay
    assert "function _handleTrackedActionAt(pixelX, pixelY)" in overlay
    assert "function _handleSelectedActionAt(pixelX, pixelY)" in overlay
    assert "onClicked: mouse => mouse.accepted = root._handleStablePointerAt(mouse.x, mouse.y)" in overlay
    assert "z: 115" in overlay
    assert "enabled: root.interactionEnabled && !MultiDetectState.selectionMode" in overlay
    assert "MultiDetectState.selectableTargetPoolEntries" in overlay


def test_candidate_pool_hides_generic_and_lost_boxes_before_rendering() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    assert "readonly property var selectableTargetPoolEntries" in state
    assert 'String(entry.state || "").toUpperCase() !== "LOST"' in state
    assert "_isSelectableTargetLabel(entry.label)" in state
    assert '"person", "pedestrian", "people", "firefighter"' in state
    assert '"vehicle", "car", "van", "truck", "bus", "train"' in state
    assert '"aircraft", "airplane", "aeroplane", "plane", "helicopter"' in state
    assert '"drone", "uav"' in state
    assert "const source = MultiDetectState.selectableTargetPoolEntries;" in overlay
    assert "model: renderTargetPool" in overlay
    assert "Math.max(34, Math.min(220, Math.round(1150.0 / rate)))" in overlay
    assert "A 95 ms floor made 20/30 Hz TRK/LCK snapshots" in overlay
    assert "function _entryVisualFingerprint(entry)" in overlay
    assert "stationary candidate does not restart its QML bindings/animations" in overlay
    assert "const displayFingerprint = _entryVisualFingerprint(displayEntry);" in overlay
    assert "nowMs - rangeUpdatedAtMs <= 1500" in overlay
    assert "nowMs - speedUpdatedAtMs <= 1500" in overlay
    assert 'String(rendered.fingerprint || "") !== displayFingerprint' in overlay
    assert 'renderTargetPool.setProperty(renderedIndex, "entry", displayEntry)' in overlay
    assert "MultiDetectState.selectableTargetPoolEntries.length" in indicator
    assert '"person", "pedestrian", "people", "firefighter"' in overlay
    assert '"vehicle", "car", "van", "truck", "bus", "train"' in overlay
    assert '"aircraft", "airplane", "aeroplane", "plane", "helicopter"' in overlay
    assert "return qgcPal.brandingBlue;" in overlay
    assert "MultiDetectState.isLockEligibleTarget(parent.parent.entry)" in overlay
    assert "parent.parent.modelData" not in overlay


def test_pending_selection_survives_stale_patrol_and_tracking_snapshots() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")

    patrol_handler = state[
        state.index("function onPatrolStatusReceived(status)") : state.index(
            "function onPatrolStatusChanged()"
        )
    ]
    track_handler = state[
        state.index("function onTrackStatusReceived(status)") : state.index(
            "enabled: target !== null", state.index("function onTrackStatusReceived(status)")
        )
    ]
    selection_handler = state[
        state.index("function applySelection(") : state.index("function selectCandidate(")
    ]

    assert "property bool selectionAwaitingTrackStatus: false" in state
    assert "property Timer selectionTrackStatusTimer: Timer" in state
    assert "if (root.hasPendingTargetSelection)" in patrol_handler
    assert "statusSelectionCommandId === root.selectionCommandId" in track_handler
    assert "root.selectionAwaitingTrackStatus && !correlatedSelection" in track_handler
    assert 'status.state === "INITIALIZING" && !status.bboxValid' in track_handler
    assert "selectionAwaitingTrackStatus = true" in selection_handler
    assert "selectionTrackStatusTimer.restart()" in selection_handler
    assert "selectionTrackStatusTimer.stop()" in state[state.index("function _clearTargetLocally()") :]


def test_trk_is_local_session_scoped_cancellable_and_multi_target() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")

    patrol_handler = state[
        state.index("function onPatrolStatusReceived(status)") : state.index(
            "function onPatrolStatusChanged()"
        )
    ]
    track_handler = state[
        state.index("function onTrackStatusReceived(status)") : state.index(
            "enabled: target !== null", state.index("function onTrackStatusReceived(status)")
        )
    ]
    cancel_handler = state[
        state.index("function cancelTarget()") : state.index(
            "function denyAuthorization()"
        )
    ]

    assert "property bool hasLocalSelectionSession: false" in state
    assert "if (!correlatedSelection)" in track_handler
    assert "root.targetBoxValid = status.bboxValid" not in patrol_handler
    assert "return cancelTrackedCandidate(selectedEntry)" in cancel_handler
    assert "operatorTrackedTargetPoolEntries" in state
    assert "entry.operatorTracked === true" in state
    assert "MultiDetectState.selectTrackedCandidate" in overlay
    assert "function cancelTrackedCandidate(entry)" in state
    assert '_operator.sendTargetSelection("CANCEL_TRK"' in state
    assert "pendingCancelledTargetIds" in state
    assert "function _clearCurrentTargetAfterCancellation(cancelledTargetId)" in state
    assert "_clearCurrentTargetAfterCancellation(entry.targetId)" in state
    selectable_entries = state[
        state.index("function _selectableTargetPoolEntries(entries)") : state.index(
            "function _operatorTrackedTargetPoolEntries(entries)"
        )
    ]
    assert "_arrayContains(pendingCancelledTargetIds, entry.targetId)" in selectable_entries
    assert "MultiDetectState.cancelTrackedCandidate" in overlay
    assert "MultiDetectState.lockTrackedCandidate" in overlay
    assert 'text: qsTr("取消")' in overlay
    assert 'text: "LCK"' in overlay
    assert "readonly property color _lockActionTextColor: qgcPal.colorRed" in overlay
    assert overlay.count("textColor: root._lockActionTextColor") == 3
    assert "exclusiveLockPendingTargetId" in state
    assert "property string demotedPrimaryLockTargetId" in state
    assert "function isDemotedPrimaryLockAwaitingPool(entry)" in state
    assert "_reconcileDemotedPrimaryLock(root.targetPoolEntries)" in state
    assert "readonly property bool hasConfirmedPrimaryLock" in state
    assert "confirmedPrimaryLockTargetId === targetId" in state
    assert "entry.locked === true && entry.primary === true" in state
    select_tracked_handler = state[
        state.index("function selectTrackedCandidate(entry)") : state.index(
            "function lockTrackedCandidate(entry)"
        )
    ]
    assert "promoteLock()" not in select_tracked_handler
    promote_handler = state[
        state.index("function promoteLock()") : state.index("function demoteTrack()")
    ]
    assert promote_handler.index('interactionState = "LCK"') < promote_handler.index(
        '_operator.sendTargetSelection("PROMOTE_LCK"'
    )
    assert promote_handler.index('exclusiveLockPendingTargetId = optimisticTargetId') < (
        promote_handler.index('_operator.sendTargetSelection("PROMOTE_LCK"')
    )
    assert 'parent.primaryLocked ? "LCK" : "TRK"' in overlay
    assert "MultiDetectState.isDemotedPrimaryLockAwaitingPool(entry)" in overlay
    assert 'MultiDetectState.interactionState === "LCK"' in overlay
    assert "entry.targetId === MultiDetectState.targetId" in overlay
    assert 'entry.insert(QStringLiteral("operatorTracked"), (flags & 0x20U) != 0)' in protocol
    assert 'QStringLiteral("CANCEL_TRK")' in protocol
    controller = _read("src/MultiDetectOperatorController.cc")
    pending_selection_guard = controller[
        controller.index('const bool isTrackSelection = normalizedAction == QStringLiteral("SELECT_TRK");') : controller.index(
            "if (normalizedAction != QStringLiteral(\"CANCEL\")"
        )
    ]
    assert "if (isTrackSelection)" in pending_selection_guard
    assert "if (_pendingSelection.active())" in pending_selection_guard
    assert "else if (_hasPendingTargetSelection())" in pending_selection_guard
    assert "_clearPendingTrackSelections();" in pending_selection_guard


def test_qualified_target_geolocation_renders_read_only_map_evidence() -> None:
    layer = _read("src/FlyViewCustomLayer.qml")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    store_header = _read("src/MultiDetectTargetEvidenceStore.h")
    store_source = _read("src/MultiDetectTargetEvidenceStore.cc")

    assert "import QtPositioning" in layer
    assert "readonly property bool _targetGeolocationQualified" in layer
    assert "readonly property bool _mapControlReady" in layer
    assert 'typeof mapControl !== "undefined"' in layer
    assert "readonly property bool _targetMapContextVisible" in layer
    assert "function _updateTargetMapProjection()" in layer
    assert "if (!_targetMapVisible || !_mapControlReady ||" in layer
    assert "mapControl.fromCoordinate" in layer
    assert "root.mapFromItem(mapControl" in layer
    assert "_targetMapSigmaRadiusPx" in layer
    assert 'qsTr("目标落图 · 只读")' in layer
    assert 'qsTr("目标落图 · 不可定位")' in layer
    assert "QGroundControl.copyToClipboard" in layer
    assert "multiDetectTargetEvidenceStore.saveSnapshot" in layer
    assert 'qsTr("距离：%1 m")' in layer  # noqa: RUF001
    assert 'qsTr("相对方位：%1°")' in layer  # noqa: RUF001
    assert 'qsTr("更新时间：%1 ms 前 · 源帧 %2")' in layer  # noqa: RUF001
    assert 'qsTr("仅显示与记录；不产生飞控指令")' in layer  # noqa: RUF001
    assert 'qsTr("不可定位：")' in state  # noqa: RUF001
    assert "class MultiDetectTargetEvidenceStore" in store_header
    assert "multidetect.target-evidence.v1" in store_source
    assert "QStandardPaths::AppLocalDataLocation" in store_source
    assert "kMaximumSnapshots = 500" in store_header
    assert "validSnapshotRecord" in store_source
    assert "Valid or degraded range evidence requires a distance." in store_source
    assert "Unavailable range evidence cannot contain distance or bearing." in store_source
    assert "rangeValidity = boundedText(snapshot, \"range_validity\", 48).toUpper()" in store_source


def test_target_evidence_coordinate_domain_matches_qualified_map_contract() -> None:
    """Poles are rejected consistently by storage, live map, and history copy."""

    layer = _read("src/FlyViewCustomLayer.qml")
    store = _read("src/MultiDetectTargetEvidenceStore.cc")

    qualified_block = layer[
        layer.index("readonly property bool _targetGeolocationQualified") : layer.index(
            "readonly property bool _mapControlReady"
        )
    ]
    copy_block = layer[
        layer.index("function _copySavedEvidenceCoordinates(snapshot)") : layer.index(
            "function _saveTargetEvidenceSnapshot()"
        )
    ]

    # The qualified map path and history-copy path must both reject exact poles
    # while retaining the inclusive longitude wire domain.
    assert "targetLatitudeDeg > -90.0" in qualified_block
    assert "targetLatitudeDeg < 90.0" in qualified_block
    assert "latitude <= -90.0 || latitude >= 90.0" in copy_block
    assert "longitude < -180.0 || longitude > 180.0" in copy_block

    # Both newly saved records and records reloaded from the v1 JSONL file use
    # the same strict-interior latitude policy.  Keep this source-level guard
    # because the store is compiled into QGC rather than exposed to Python.
    assert "latitudeDeg <= -90.0 || latitudeDeg >= 90.0" in store
    assert "latitude > -90.0 && latitude < 90.0" in store


def test_target_geolocation_source_age_is_fail_closed_before_map_refresh() -> None:
    """Old signed coordinates must not be kept alive by a refreshed envelope."""

    protocol_header = _read("src/MultiDetectOperatorProtocol.h")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")
    controller_header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "kMaximumTargetGeolocationSourceAgeMs = 1500" in protocol_header
    assert "kMaximumTargetGeolocationFrameHoldMs = 3000" in protocol_header
    assert "frameAgeMs > kMaximumTargetGeolocationSourceAgeMs" in protocol
    assert "target-geolocation-status source frame is stale" in protocol
    assert "_acceptTargetGeolocationSource(fields, packet.sentAtMs, _lastAuthenticatedAtMs)" in controller
    assert "target-geolocation source frame is stale or regressed" in controller
    assert "TargetGeolocationSourceState" in controller_header
    assert "_targetGeolocationSourceStates" in controller_header
    assert "sourceAgeMs < state.lastSourceAgeMs" in controller
    assert "sourceCapturedAtMs < state.lastSourceCaptureAtMs" in controller
    assert "kMaximumTargetGeolocationFrameHoldMs" in controller
    assert "_clearTargetGeolocationSourceState();" in controller
    assert "maximumTargetGeolocationSourceAgeMs: 1500.0" in state
    assert "targetGeolocationSourceFresh" in state
    assert "targetGeolocationSourceFresh" in layer


def test_range_source_age_is_fail_closed_before_evidence_snapshot() -> None:
    """Actual distance must expire with its source, not its envelope."""

    protocol_header = _read("src/MultiDetectOperatorProtocol.h")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "kMaximumRangeStatusSourceAgeMs = 1500" in protocol_header
    assert "frameAgeMs > kMaximumRangeStatusSourceAgeMs" in protocol
    assert "range-status source frame is stale" in protocol
    assert "range-status data freshness is unavailable" in protocol
    assert "range-status data freshness is stale" in protocol
    assert "maximumRangeSourceAgeMs: 1500.0" in state
    assert "maximumRangeDataFreshnessS: 1.5" in state
    assert "rangeDataFresh: Number.isFinite(rangeDataFreshnessS)" in state
    assert "rangeStatusFresh: rangeSourceFresh && rangeDataFresh" in state
    assert "targetGeolocationRangeBindingValid" in state
    assert "targetGeolocationSourceFrameId === rangeSourceFrameId" in state
    assert "const rangeFresh = root.rangeStatusFresh" in state
    assert "MultiDetectState.rangeStatusFresh" in layer
    assert "if (!MultiDetectState.rangeStatusFresh)" in layer


def test_mode3_active_aim_has_prominent_cancel_and_pilot_override_status() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")
    resources = _read("custom.qrc")

    assert "property bool approachAimControlActive: false" in state
    assert "property bool approachPilotInputCancelled: false" in state
    assert "property bool mode3ExecutionLatched: false" in state
    assert "readonly property bool fixedWingAimActive" in state
    assert "readonly property bool mode3AimUiActive" in state
    fixed_wing_active = state[
        state.index("readonly property bool fixedWingAimActive") : state.index(
            "readonly property bool mode3AimUiActive"
        )
    ]
    assert "fixedWingAimControlEnabled" in fixed_wing_active
    assert "function cancelMode3Aim()" in state
    assert "function mode3AimBannerTitleText()" in state
    assert "function mode3AimBannerDetailText()" in state
    assert "signal lckCueRequested()" in state
    assert "function _requestLckCue(targetIdToAnnounce)" in state
    assert "_requestLckCue(entry.targetId)" in state
    assert 'interactionState !== "LCK" && interactionState !== "TGT"' in state
    assert 'qsTr("遥控输入已接管 · 模式 3 瞄准已取消")' in state
    assert "id: mode3AimBanner" in layer
    assert 'qsTr("模式 3 正在瞄准")' in state
    assert 'qsTr("模式 3 瞄准状态异常")' in state
    assert 'qsTr("模式 3 瞄准状态待确认")' in state
    assert 'qsTr("模式 3 目标已确认 · 飞控输出锁定")' in state
    assert 'qsTr("模式 3 等待控制接管")' in state
    assert 'qsTr("仅显示姿态建议；不会发送姿态或模式命令")' in state  # noqa: RUF001
    assert "text: MultiDetectState.mode3AimBannerTitleText()" in layer
    assert "text: MultiDetectState.mode3AimBannerDetailText()" in layer
    assert 'qsTr("立即取消瞄准")' in layer
    assert 'qsTr("遥控器任意按钮、拨杆或右侧按钮均可立即取消")' in state
    assert "MultiDetectState.cancelMode3Aim()" in layer
    assert "SoundEffect" in layer
    assert 'source: "qrc:/Custom/audio/locked.wav"' in layer
    assert 'source: "qrc:/Custom/audio/mode3-execution.wav"' in layer
    assert 'source: "qrc:/Custom/audio/mode3-execution-loop.wav"' in layer
    assert "loops: SoundEffect.Infinite" in layer
    assert "function onLckCueRequested()" in layer
    assert "function onMode3AimUiActiveChanged()" in layer
    assert "mode3AimLoopCue.stop()" in layer
    assert 'alias="locked.wav"' in resources
    assert 'alias="mode3-execution-loop.wav"' in resources
    assert (REPO_ROOT / "resources" / "audio" / "locked.wav").is_file()
    assert (REPO_ROOT / "resources" / "audio" / "mode3-execution-loop.wav").is_file()
    assert "scale: 1.0" in layer
    assert "NumberAnimation { duration: 360; from: 1.0; to: 1.025" not in layer
    assert 'fields.insert(QStringLiteral("aimControlActive"), aimControlActive)' in protocol
    assert (
        'fields.insert(QStringLiteral("pilotInputCancelled"), pilotInputCancelled)'
        in protocol
    )


def test_authoritative_non_manual_lock_enables_execution_after_identity_rebind() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")

    observe = state[
        state.index("function _observeConfirmedPrimaryLock(entries)") : state.index(
            "function _removePendingCancelledTarget"
        )
    ]
    reconcile = state[
        state.index("function _reconcilePendingTargetActions()") : state.index(
            "function _isSelectableTargetLabel"
        )
    ]

    assert "entry.operatorTracked === true" in observe
    assert "exclusiveLockPendingTargetId = \"\"" in observe
    assert "_adoptTargetPoolEntry(entry)" in observe
    assert "confirmedExclusiveEntry" in reconcile
    assert "confirmedPrimaryLockTargetId = confirmedExclusiveEntry.targetId" in reconcile
    assert "_adoptTargetPoolEntry(confirmedExclusiveEntry)" in reconcile
    assert "entry.targetId === exclusiveLockPendingTargetId" not in reconcile


def test_lck_is_exclusive_before_a_new_manual_selection_can_begin() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    begin_selection = state[
        state.index("function beginSelection()") : state.index("function cancelTarget()")
    ]

    assert 'interactionState === "LCK" || interactionState === "TGT"' in begin_selection
    assert 'exclusiveLockPendingTargetId !== ""' in begin_selection
    assert 'statusMessage = qsTr("LCK 状态请先返回 TRK")' in begin_selection
    assert 'MultiDetectState.interactionState !== "LCK"' in layer
    assert 'MultiDetectState.interactionState !== "TGT"' in layer
    assert 'MultiDetectState.exclusiveLockPendingTargetId === ""' in layer


def test_mode3_execution_banner_is_latched_until_an_explicit_exit() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    reset = state[
        state.index("function _resetApproachStatus()") : state.index(
            "function _resetPayloadTargetStatus()"
        )
    ]
    status_handler = state[
        state.index("function onApproachStatusChanged()") : state.index(
            "function onPayloadTargetChallengeChanged()"
        )
    ]
    abort_handler = status_handler[
        status_handler.index('status.phase === "ABORT"') : status_handler.index(
            'status.phase === "AIMING"'
        )
    ]
    set_mode = state[
        state.index("function setMissionMode(mode)") : state.index(
            "function missionModeDisplayName()"
        )
    ]

    assert "mode3ExecutionLatched = false" not in reset
    assert 'root.interactionState = root.mode3ExecutionLatched ? "TGT" : "LCK"' in abort_handler
    assert "root.mode3ExecutionLatched = false" not in abort_handler
    assert "mode3ExecutionLatched = false" in set_mode
    assert "MultiDetectState.mode3AimUiActive || MultiDetectState.approachPilotInputCancelled" in layer
    assert 'visible: MultiDetectState.mode3AimUiActive' in layer


def test_mode3_challenge_expiry_and_visual_recovery_have_bounded_popup_state() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    timer = state[state.index("property Timer approachChallengeTimer") : state.index(
        "property real approachYawErrorDeg"
    )]
    prompt = state[state.index("function approachChallengePromptText(") : state.index(
        "function confirmApproachSlide("
    )]
    challenge_handler = state[state.index("function onApproachChallengeChanged()") : state.index(
        "function onApproachStatusChanged()"
    )]

    assert 'root.statusMessage = qsTr("模式 3 执行挑战已过期")' in timer
    assert "root._refreshApproachChallenge();" in challenge_handler
    refresh_handler = state[state.index("function _refreshApproachChallenge()") : state.index(
        "function _refreshPayloadTargetChallenge()"
    )]
    assert "approachChallengeTimer.restart();" in refresh_handler
    assert "!vehicleArmed" in refresh_handler
    assert "approachChallengeTimer.stop();" in refresh_handler
    assert 'approachReasons.indexOf("target_occluded") >= 0' in prompt
    assert 'qsTr("目标重捕获中")' in prompt
    assert "MultiDetectState.approachChallengePromptText(root._targetActionable," in layer


def test_execution_requires_arm_and_rebinds_selection_after_operator_reconnect() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    controller = _read("src/MultiDetectOperatorController.cc")

    assert "readonly property bool vehicleArmed" in state
    assert (
        "readonly property bool executionActionable: physicalActionsAllowed && vehicleArmed && "
        "lckActionable"
    ) in state
    assert 'statusMessage = qsTr("请先 ARM，再确认模式 3 执行")' in state  # noqa: RUF001
    assert 'statusMessage = qsTr("请先 ARM，再确认模式 2 执行")' in state  # noqa: RUF001
    assert "enabled: MultiDetectState.operatorConfigured && MultiDetectState.executionActionable" in layer
    assert "property bool reconnectSelectionPending" in state
    assert "function _resumeSelectionAfterReconnect()" in state
    assert '_operator.sendTargetSelection("SELECT_TRK"' in state
    assert "function _tryReconnectPromoteLock()" in state
    assert "if (canPromoteCurrentTarget() && promoteLock())" in state
    assert "nowMs - root.targetRangeUpdatedAtMs > 1500" in state
    assert "nowMs - _lastAuthenticatedAtMs > 5000" in controller


def test_target_boxes_show_authenticated_range_bearing_and_speed_metadata() -> None:
    protocol = _read("src/MultiDetectOperatorProtocol.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    for field in ("relativeBearingDeg", "estimatedRangeM", "targetSpeedMps"):
        assert f'entry.insert(QStringLiteral("{field}")' in protocol
        assert field in overlay
    assert "function _metricText(entry)" in overlay
    assert "Number.isFinite(entry.estimatedRangeM)" in overlay
    assert "Number.isFinite(entry.relativeBearingDeg)" in overlay
    assert "Number.isFinite(entry.targetSpeedMps)" in overlay
    assert 'qsTr("距")' in overlay
    assert 'qsTr("方")' in overlay
    assert 'qsTr("速")' in overlay
    assert "rangeMatchesCurrentTarget" in overlay
    assert "MultiDetectState.rangeTargetId === MultiDetectState.targetId" in overlay
    assert "MultiDetectState.estimatedRangeM" in overlay
    assert "MultiDetectState.relativeBearingAvailable" in overlay
    assert "property bool relativeBearingAvailable: false" in state
    assert "root.rangeTargetId === root.targetId" in state
    assert "relativeBearingDeg = relativeBearingAvailable ? entry.relativeBearingDeg : 0.0;" in state
    assert "estimatedRangeM = Number.isFinite(entry.estimatedRangeM) ? entry.estimatedRangeM : 0.0;" in state


def test_depth_display_mode_persists_independently_of_lck() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    assert "property int depthDisplayMode: 1" in state
    assert "readonly property int depthDisplayMode: MultiDetectState.persistedSettings.depthDisplayMode" in overlay
    assert "MultiDetectState.persistedSettings.depthDisplayMode =" in overlay
    assert "root.depthDisplayMode === 1 ? 2 : root.depthDisplayMode === 2 ? 0 : 1" in overlay
    full_map = overlay[overlay.index("id: fullDepthMap") : overlay.index("Rectangle {", overlay.index("id: fullDepthMap"))]
    pip = overlay[overlay.index("id: depthPip") : overlay.index("MouseArea {", overlay.index("id: depthPip"))]
    assert "interactionState" not in full_map
    assert "interactionState" not in pip
    assert "id: compactStatusPanel" in overlay
    assert "anchors.top: compactStatusPanel.bottom" in overlay
    assert "visible: root.interactionEnabled && root.depthDisplayMode === 1" in overlay
    assert 'qsTr("等待 Jetson 深度网格")' in overlay


def test_mission_status_cannot_replace_an_active_local_target() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    mission_handler = state[
        state.index("function onMissionStatusReceived(status)") : state.index(
            "function onPatrolStatusReceived(status)"
        )
    ]

    assert 'const missionTargetId = String(status.targetId || "");' in mission_handler
    assert "const canAdoptMissionTarget" in mission_handler
    assert "root.targetId === \"\" && !root.targetBoxValid" in mission_handler
    assert "!root.hasPendingTargetSelection" in mission_handler
    assert "!root.hasLocalSelectionSession" in mission_handler
    assert "!root.hasOperatorTrackedTargets" in mission_handler
    assert "const missionTargetMatchesCurrent" in mission_handler
    assert "if (missionTargetMatchesCurrent)" in mission_handler
    assert "root.targetId = status.targetId;" not in mission_handler


def test_correlated_second_trk_target_keeps_focus_until_target_pool_catches_up() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    track_handler = state[
        state.index("function onTrackStatusReceived(status)") : state.index("enabled: target !== null")
    ]
    reconcile = state[
        state.index("function _reconcileCurrentTargetFromPool()") : state.index(
            "function _clearTargetLocally()"
        )
    ]

    assert "const canAdoptCorrelatedTarget = correlatedSelection" in track_handler
    assert 'completedAction !== "CANCEL_TRK"' in track_handler
    assert "const statusTargetsCurrent = status.targetPresent" in track_handler
    assert "if (statusTargetsCurrent)" in track_handler
    assert "root.targetX1 = status.x1;" in track_handler
    assert 'pendingSelectionAction === "SELECT_TRK"' in reconcile
    assert "if (best === null && hasLocalSelectionSession && targetId !== \"\" &&" in reconcile
    assert "targetBoxValid) {\n            return;" in reconcile


def test_parallel_manual_trk_keeps_independent_delivery_and_latest_focus() -> None:
    header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    assert "Q_PROPERTY(int pendingTrackSelectionCount" in header
    assert "QHash<QString, PendingDelivery> _pendingTrackSelections;" in header
    assert "bool _hasPendingTargetSelection() const;" in header
    assert 'const bool isTrackSelection = normalizedAction == QStringLiteral("SELECT_TRK");' in controller
    assert "_pendingTrackSelections.insert(commandId, delivery);" in controller
    assert "pendingTrackSelectionCountChanged" in controller
    assert "_pendingTrackSelections.constFind(commandId)" in controller
    assert "for (auto pendingTrackIt = _pendingTrackSelections.begin();" in controller
    assert "property var localTrackSelectionCommandIds: []" in state
    assert "readonly property bool hasPendingTargetSelection:" in state
    assert "function _rememberLocalTrackSelectionCommand(commandId)" in state
    assert "const correlatedTrackSelection =" in state
    assert "if (!targetsCurrentSelection)" in state
    assert "MultiDetectState.hasPendingTargetSelection" in overlay


def test_manual_fallback_box_is_hidden_when_the_same_pool_track_arrives_under_a_new_id() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    assert "function hasPoolTrackedBoxForFallback(x1, y1, x2, y2)" in state
    assert "const intersectionArea = overlapWidth * overlapHeight;" in state
    assert "intersectionArea / unionArea >= 0.55" in state
    assert "independent manual TRK boxes remain" in state
    assert "!MultiDetectState.hasPoolTrackedBoxForFallback(" in overlay
    assert "MultiDetectState.targetX1" in overlay
    assert "MultiDetectState.targetY2" in overlay


def test_lost_selected_target_clears_only_its_visual_without_cancelling_other_trk() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    track_handler = state[
        state.index("function onTrackStatusReceived(status)") : state.index("enabled: target !== null")
    ]
    loss_reset = state[
        state.index("function _clearCurrentTargetAfterLoss()") : state.index(
            "function _adoptTargetPoolEntry(entry)"
        )
    ]
    reconcile = state[
        state.index("function _reconcileCurrentTargetFromPool()") : state.index(
            "function _clearTargetLocally()"
        )
    ]
    cancel_handler = state[
        state.index("function cancelTarget()") : state.index("function denyAuthorization()")
    ]

    assert "function _rawTargetPoolEntry(candidateTargetId)" in state
    assert "function _currentTargetIsExplicitlyLost()" in state
    assert 'String(entry.state || "").toUpperCase() === "LOST"' in state
    assert "const statusReferencesCurrent = status.targetId !== \"\"" in track_handler
    assert "if (statusReferencesCurrent)\n                    root._clearCurrentTargetAfterLoss();" in track_handler
    assert 'targetId = "";' in loss_reset
    assert "targetBoxValid = false;" in loss_reset
    assert 'trackingState = "LOST";' in loss_reset
    assert "confirmedPrimaryLockTargetId = \"\";" in loss_reset
    assert "_resetApproachStatus();" in loss_reset
    assert "if (_currentTargetIsExplicitlyLost())\n            _clearCurrentTargetAfterLoss();" in reconcile
    assert "entries.length === 1" not in cancel_handler
    assert '_operator.sendTargetSelection("CANCEL", 0.0, 0.0, 0.0, 0.0)' not in cancel_handler
    assert "未取消其它 TRK" in cancel_handler


def test_operator_commands_use_authenticated_jetson_clock_but_local_retry_deadlines() -> None:
    header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")

    assert "quint64 _operatorWireNowMs(quint64 localNowMs) const;" in header
    assert "void _observeRemoteWireTime(quint64 remoteSentAtMs, quint64 localReceivedAtMs);" in header
    assert "_observeRemoteWireTime(packet.sentAtMs, _lastAuthenticatedAtMs);" in controller
    assert "wireNowMs, ttlMs, &error" in controller
    assert "delivery.expiresAtMs = localNowMs + ttlMs;" in controller
    assert "delivery.nextAttemptAtMs = localNowMs;" in controller
    assert "_pendingTrackSelections.insert(commandId, delivery);" in controller


def test_status_uses_compact_native_toolbar_and_detailed_qgc_drawer() -> None:
    layer = _read("src/FlyViewCustomLayer.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    assert "id: statusPanel" not in layer
    assert "id: runtimeIdentityBadge" not in layer
    assert "mainWindow.showIndicatorDrawer(indicatorPage, control)" in indicator
    assert "ToolIndicatorPage {" in indicator
    assert "showExpand: true" in indicator
    assert "expandedComponent: Component {" in indicator
    assert 'objectName: "toolbar_multiDetectIndicator"' in indicator
    assert 'Accessible.name: "MultiDetect AI Status"' in indicator
    assert "Accessible.role: Accessible.Button" in indicator
    assert 'Accessible.name: "MultiDetect AI Status Drawer"' in indicator
    assert "Accessible.role: Accessible.Pane" in indicator
    assert indicator.count("SettingsGroupLayout {") >= 3
    assert indicator.count("LabelledLabel {") >= 15
    for label in (
        "模式",
        "识别",
        "目标",
        "跟踪",
        "数据",
        "场景",
    ):
        assert f'label: qsTr("{label}")' in indicator
    for forbidden in (
        "_activeVehicle",
        "_primaryBattery",
        "airSpeedDisplay",
        "groundSpeed",
        "altitudeRelative",
        "gps.count",
        "percentRemaining",
        '"label": qsTr("飞控")',
        '"label": qsTr("姿态")',
        '"label": qsTr("速度")',
        '"label": qsTr("高度")',
        '"label": qsTr("定位")',
        '"label": qsTr("电池")',
    ):
        assert forbidden not in layer + indicator
    assert "_debugExpanded" not in indicator
    assert "_debugStatusRows" not in indicator
    assert "ScreenTools.fixedFontFamily" not in indicator
    assert 'text: "AI " + MultiDetectState.runtimeDisplayMode' in indicator
    indicator_row = indicator[
        indicator.index("id: indicatorRow") : indicator.index("MouseArea {")
    ]
    assert 'text: "JETSON "' not in indicator_row
    assert "_activeAlgorithmCount()" not in indicator_row
    compact = indicator[
        indicator.index("contentComponent: Component {") : indicator.index(
            "expandedComponent: Component {"
        )
    ]
    assert compact.count("LabelledLabel {") == 5
    assert 'label: qsTr("签名链路")' in compact
    assert 'label: qsTr("算法")' in compact
    expanded = indicator[indicator.index("expandedComponent: Component {") :]
    assert 'heading: qsTr("Jetson · AI")' in expanded
    assert 'heading: qsTr("室外测距质量")' in expanded
    assert 'label: qsTr("GPS/GNSS")' in expanded
    assert 'label: qsTr("RANGE_STATUS")' in expanded
    assert "function _gpsQualityValue()" in indicator
    assert "function _rangeStatusValue()" in indicator
    assert 'return qsTr("等待数据")' in indicator
    assert "MultiDetectState.trackingMetadataRateHz" in indicator
    assert "MultiDetectState.totalTrackCount" not in indicator
    assert "MultiDetectState.lockedTrackCount" in indicator
    assert "MultiDetectState.selectableTargetPoolEntries" in indicator
    assert "MultiDetectState.targetConfidence" in indicator
    assert "MultiDetectState.trackingQuality" in indicator
    assert "MultiDetectState.sceneContextRegions" in indicator


def test_patrol_and_payload_ui_paths_keep_authorization_separate_from_selection() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert 'mode !== "PATROL" && mode !== "PAYLOAD"' in state
    assert 'missionMode === "PAYLOAD"' in state
    assert 'missionPhase = "AWAITING_AUTHORIZATION"' in state
    assert "function approveAuthorization()" in state
    assert "QGCPopupDialog {" in layer
    assert "function _openAuthorizationDialog()" in layer
    assert "MultiDetectState.approveAuthorization()" in layer
    assert "MultiDetectState.beginSelection()" in layer
    # The acceptance-only auto-exercise calls the same production function
    # before the dialog declarations.  The user-facing approval must still be
    # owned by the final native QGC authorization dialog itself.
    authorization_dialog = layer[layer.rindex("QGCPopupDialog {") :]
    assert "MultiDetectState.approveAuthorization()" in authorization_dialog


def test_patrol_status_is_authenticated_read_only_and_rendered_in_qgc_style() -> None:
    protocol_header = _read("src/MultiDetectOperatorProtocol.h")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")
    controller_header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "PatrolStatus = 9" in protocol_header
    assert 'QStringLiteral("patrol-status body has an invalid size")' in protocol
    assert 'QStringLiteral("PATROL status cannot contain a primary target")' in protocol
    assert 'QStringLiteral("patrol-status return-observe metadata is inconsistent")' in protocol
    assert 'fields.insert(QStringLiteral("operatorConfirmationRequired"), true)' in protocol
    assert 'fields.insert(QStringLiteral("sitlValidationRequired"), true)' in protocol
    assert 'fields.insert(QStringLiteral("flightControlEnabled"), false)' in protocol
    assert "Q_PROPERTY(QVariantMap patrolStatus" in controller_header
    assert "emit patrolStatusReceived(fields)" in controller
    assert "_patrolStatus.clear()" in controller
    assert "kPatrolStatusStaleMs = 2000" in controller
    assert "function onPatrolStatusReceived(status)" in state
    assert "function onPatrolStatusChanged()" in state
    assert "function _resetPatrolStatus()" in state
    assert "totalTrackCount" in state
    assert "lockedTrackCount" in state
    assert "returnObserveText()" in state
    assert "轨迹池" not in layer
    assert "复查建议" not in layer
    assert "sendMavCommand" not in protocol + controller + state + layer


def test_new_selection_and_target_switch_revoke_stale_safety_and_authorization() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")

    reset_function = state.index("function _resetSafetyAndAuthorization()")
    selection_function = state.index("function applySelection(")
    approval_function = state.index("function approveAuthorization()")
    switch_function = state.index("function switchTarget(")
    rc_update_function = state.index("function updateRcChannels(")

    assert reset_function < selection_function < approval_function
    assert "_resetSafetyAndAuthorization()" in state[selection_function:approval_function]
    assert "_resetSafetyAndAuthorization()" in state[switch_function:rc_update_function]
    assert 'authorizationState = "NONE"' in state[reset_function:selection_function]
    assert "authorizationTimer.stop()" in state[reset_function:selection_function]


def test_qml_module_and_plugin_interceptor_are_wired_into_the_custom_build() -> None:
    cmake = _read("CMakeLists.txt")
    plugin_header = _read("src/CustomPlugin.h")
    plugin = _read("src/CustomPlugin.cc")
    video = _read("src/FlyViewVideo.qml")

    assert "QT_QML_SINGLETON_TYPE TRUE" in cmake
    assert "URI Custom.MultiDetect" in cmake
    assert "MultiDetectUi" in cmake
    assert "${CMAKE_CURRENT_SOURCE_DIR}/res/Custom" not in cmake
    assert 'QStringLiteral(":/Custom%1")' in plugin
    assert 'QStringLiteral("qrc:/qml")' in plugin
    assert 'QStringLiteral("multiDetectOperator")' in plugin
    assert "import Custom.MultiDetect" in video
    assert "const QVariantList& toolBarIndicators() final;" in plugin_header
    assert "_toolBarIndicatorList = QGCCorePlugin::toolBarIndicators();" in plugin
    assert "_toolBarIndicatorList.append" in plugin
    assert 'qrc:/qml/Custom/MultiDetect/MultiDetectToolbarIndicator.qml' in plugin
    assert "QT_RESOURCE_ALIAS MultiDetectToolbarIndicator.qml" in cmake


def test_production_operator_link_has_no_simulation_or_unsigned_hil_path() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    controller = _read("src/MultiDetectOperatorController.cc")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")

    assert 'environmentValue("MULTIDETECT_OPERATOR_KEY")' in controller
    assert (
        'environmentValue("MULTIDETECT_OPERATOR_STREAM_ID", QStringLiteral("camera-main"))'
        in controller
    )
    assert "configuration->setDynamic(true)" in controller
    assert 'QStringLiteral("WAITING_FOR_SIGNED_METADATA")' in controller
    assert 'QStringLiteral("STALE")' in controller
    assert "MULTIDETECT_OPERATOR_ALLOW_UNSIGNED_HIL" not in controller
    assert "MULTIDETECT_OPERATOR_HIL_UDP_PORT" not in controller
    assert "AUTHENTICATED_HIL_UNSIGNED" not in controller
    assert "demoRunning" not in state
    assert "startDemo" not in state
    assert "stopDemo" not in state
    assert "仿真" not in state
    assert "operator-link authentication failed" in protocol
    assert "constantTimeEqual" in protocol
    assert "function onTrackStatusReceived(status)" in state
    assert "function onMissionStatusReceived(status)" in state
    assert "function onSafetyStatusReceived(status)" in state
    assert "function onAuthorizationChallengeReceived(challenge)" in state
    assert '_operator.sendTargetSelection("SELECT_TRK"' in state
    assert '_operator.sendTargetSelection("PROMOTE_LCK"' in state
    assert '_operator.sendTargetSelection("DEMOTE_TRK"' in state
    assert "_operator.sendAuthorizationDecision(true)" in state


def test_scene_context_is_authenticated_atomic_and_advisory_only() -> None:
    header = _read("src/MultiDetectOperatorProtocol.h")
    protocol = _read("src/MultiDetectOperatorProtocol.cc")
    controller_header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    assert "SceneContextStatus = 17" in header
    assert 'QStringLiteral("scene-context page coordinates are inconsistent")' in protocol
    assert 'fields.insert(QStringLiteral("confidenceAvailable"), false)' in protocol
    assert 'fields.insert(QStringLiteral("targetIdentityAuthority"), false)' in protocol
    assert 'fields.insert(QStringLiteral("flightControlEnabled"), false)' in protocol
    assert 'fields.insert(QStringLiteral("physicalReleaseEnabled"), false)' in protocol
    assert "Q_PROPERTY(QVariantList sceneContextRegions" in controller_header
    assert "_sceneContextPages.size() == _sceneContextPageCount" in controller
    assert "kSceneContextStatusStaleMs = 2000" in controller
    assert "function onSceneContextChanged()" in state
    assert 'MultiDetectState.sceneContextState === "VALID"' in overlay
    assert "modelData.label === \"road\"" in overlay
    assert "sendMavCommand" not in protocol + controller + state + overlay


def test_configure_owns_preflight_module_and_rc_release_configuration() -> None:
    plan = _read("src/PlanViewRightPanel.qml")
    configure = _read("src/MultiDetectConfigure.qml")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    settings_pages = (REPO_ROOT / "src/AppSettings/pages/SettingsPages.json").read_text(
        encoding="utf-8"
    )

    assert "MultiDetectState.setMissionMode" not in plan
    assert "MultiDetectState.setRcReleaseChannel" not in plan
    assert "MultiDetectState.setMissionMode" in configure
    assert "MultiDetectState.setRcReleaseChannel" in configure
    assert "模式 1" in configure
    assert "模式 2" in configure
    assert "模式 3" in configure
    assert "missionConfigurationLocked" in configure
    assert '"qml": "MultiDetectConfigure.qml"' not in settings_pages
    assert "function updateRcChannels(channelValues)" in state
    assert "function onRcChannelsRawChanged(channelValues)" in state
    assert "rcReleaseSwitchActive" in state
    assert "manualReleaseRequestLatched" in state
    assert layer.count('text: qsTr("执行")') >= 2
    assert "ScreenTools.isMobile" in layer
    assert layer.count("buttons: ScreenTools.isMobile ? Dialog.Cancel : (Dialog.Ok | Dialog.Cancel)") == 2
    assert "root._openPayloadActionDialog()" in layer
    assert "root._openApproachActionDialog()" in layer
    assert 'qsTr("等待 Jetson 执行挑战")' in state
    assert "sendMavCommand" not in state + plan + configure + layer


def test_mode_setting_is_a_first_class_menu_and_airframe_is_hidden() -> None:
    menu = _read("src/SelectViewDropdown.qml")
    resources = (CUSTOM_ROOT / "custom.qrc").read_text(encoding="utf-8")
    vehicle_config = (REPO_ROOT / "src/Vehicle/VehicleSetup/VehicleConfigView.qml").read_text(
        encoding="utf-8"
    )

    assert 'objectName: "toolbar_viewModeSetting"' in menu
    assert 'text: qsTr("Mode Setting")' in menu
    assert '"qrc:/qml/QGroundControl/AppSettings/MultiDetectConfigure.qml"' in menu
    assert 'alias="QGroundControl/Toolbar/SelectViewDropdown.qml"' in resources
    assert '/PX4/AirframeComponent.qml' in vehicle_config


def test_plan_area_route_drag_creates_a_native_gps_survey_without_replacing_the_plan() -> None:
    plan_view = (REPO_ROOT / "src/PlanView/PlanView.qml").read_text(encoding="utf-8")

    assert "property bool   _addAreaRouteOnDrag: false" in plan_view
    assert 'objectName: "planToolStrip_areaRouteButton"' in plan_view
    assert 'text: qsTr("区域航线")' in plan_view
    assert "enabled: _missionController.flyThroughCommandsAllowed" in plan_view
    assert "id: areaRouteSelectionArea" in plan_view
    assert "createAreaRoute(_startPoint, _currentPoint)" in plan_view
    assert 'insertComplexMissionItem("Survey", center, nextIndex, true' in plan_view
    assert "polygon.appendVertices([topLeft, topRight, bottomRight, bottomLeft])" in plan_view
    assert "polygon.verifyClockwiseWinding()" in plan_view
    assert "_missionController.removeAll" not in plan_view


def test_mode_three_video_has_fixed_reticle_and_dynamic_target_lock_box() -> None:
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    assert "id: targetBox" in overlay
    assert "id: approachReticle" in overlay
    assert 'MultiDetectState.missionMode === "OBSERVE"' in overlay
    assert "id: approachReticleCanvas" in overlay
    assert "function drawCrosshair" in overlay
    assert "ctx.arc" not in overlay
    assert "property color reticleColor: qgcPal.text" in overlay
    assert 'MultiDetectState.interactionState === "LCK"' in overlay
    assert 'MultiDetectState.interactionState === "TGT"' in overlay
    assert overlay.index('visible: MultiDetectState.missionMode === "OBSERVE"') < overlay.index(
        'property color reticleColor: qgcPal.text'
    )
    assert 'text: "+"' in overlay
    assert "MultiDetectState.selectCandidate" in overlay
    assert "MultiDetectState.promoteLock" in overlay
    assert 'MultiDetectState.interactionState === "LCK"' in overlay
    assert 'MultiDetectState.trackingState === "LOST"' in overlay
    assert "return qgcPal.warningText;" in overlay
    assert "anchors.centerIn: parent\n            color: targetBox.border.color" not in overlay


def test_trk_cancel_preempts_pending_manual_selection_and_flyview_insets_cover_overlays() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "function _cancelCurrentTrackByBox()" in state
    assert '_operator.sendTargetSelection("CANCEL_TRK", targetX1, targetY1, targetX2, targetY2)' in state
    assert 'pendingSelectionAction = "CANCEL_TRK";' in state
    assert "if (targetBoxValid)\n            return _cancelCurrentTrackByBox();" in state
    cancel_candidate = state.split("function cancelTrackedCandidate(entry)", 1)[1].split(
        "function _cancelCurrentTrackByBox()", 1
    )[0]
    assert "hasPendingTargetSelection" not in cancel_candidate
    assert "readonly property real _topHudClearance" in overlay
    assert "anchors.top: compactStatusPanel.bottom" in overlay
    assert "anchors.left: depthModeButton.left" in overlay
    assert "enabled: root.interactionEnabled" in overlay
    assert "id: toolInsets" in layer
    for item in (
        "targetGeolocationCard.x + targetGeolocationCard.width",
        "targetGeolocationCard.y + targetGeolocationCard.height",
        "mode3AimBanner.y + mode3AimBanner.height",
        "root.height - actionBar.y",
    ):
        assert item in layer
    assert "rightEdgeCenterInset: parentToolInsets.rightEdgeCenterInset" in layer
    assert "rightEdgeTopInset: parentToolInsets.rightEdgeTopInset" in layer
    assert "topEdgeRightInset: parentToolInsets.topEdgeRightInset" in layer
    assert "runtimeIdentityBadge" not in layer
    assert "statusPanel" not in layer
    assert "ScreenTools.defaultFontPixelHeight * 7.4" not in layer


def test_lck_cancel_demotes_once_and_trk_cancel_hides_the_exact_box_immediately() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")

    cancel_handler = state[
        state.index("function cancelTarget()") : state.index("function denyAuthorization()")
    ]
    fallback_cancel = state[
        state.index("function _cancelCurrentTrackByBox()") : state.index("function approveAuthorization()")
    ]

    assert 'if ((interactionState === "LCK" || interactionState === "TGT") && targetBoxValid)' in cancel_handler
    assert "return demoteTrack();" in cancel_handler
    demote_handler = state[
        state.index("function demoteTrack()") : state.index("function cancelMode3Aim()")
    ]
    assert "hasPendingTargetSelection" not in demote_handler
    assert "function isCancellationPendingTarget(entry)" in state
    assert "pendingCancelledTargetIds = hidden;" in fallback_cancel
    assert "_removePendingCancelledTarget(cancelledTargetId);" not in fallback_cancel
    assert "function onPendingCancelledTargetIdsChanged()" in overlay
    assert "readonly property bool cancellationPending: MultiDetectState.isCancellationPendingTarget(entry)" in overlay
    assert "entry.bboxValid && !cancellationPending" in overlay
    assert 'text: parent.parent.primaryLocked ? qsTr("返回 TRK") : qsTr("取消")' in overlay
    assert "MultiDetectState.demoteTrack();" in overlay


def test_mode_three_uses_production_observe_state_and_minimal_ui_copy() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    configure = _read("src/MultiDetectConfigure.qml")

    assert 'index === 2 ? "OBSERVE" : "PATROL"' in configure
    assert 'mode !== "OBSERVE"' in state
    assert 'storedMode === "APPROACH_HIL" ? "OBSERVE"' in state
    assert 'persistedSettings.selectedMissionMode = "OBSERVE"' in state
    assert 'text: MultiDetectState.approachPhase' not in layer
    assert 'selectionHintText: qsTr("框选目标")' in _read(
        "res/Custom/MultiDetect/MultiDetectVideoOverlay.qml"
    )
    assert 'text: qsTr("后台目标")' not in layer
    assert 'text: qsTr("测距质量")' not in layer
    assert 'text: qsTr("窗口依据")' not in layer
    assert 'text: qsTr("任务模块配置")' not in configure
    assert 'text: qsTr("Mode Setting")' in configure
    assert "模拟" not in state + layer + configure
    assert "模式 3 HIL" not in state + layer + configure


def test_custom_qgc_bootstraps_the_fixed_aircraft_and_camera_without_ui_setup() -> None:
    plugin_header = _read("src/CustomPlugin.h")
    plugin = _read("src/CustomPlugin.cc")

    assert "void init() final;" in plugin_header
    assert 'kDefaultCameraRtspUrl = "rtsp://192.168.144.108:554/stream=0"' in plugin
    assert 'kDefaultVehicleHost = "192.168.144.11"' in plugin
    assert "kDefaultVehiclePort = 5760" in plugin
    assert 'runtimeEnvironmentValue("MULTIDETECT_VEHICLE_TCP_HOST")' in plugin
    assert 'runtimeEnvironmentValue("MULTIDETECT_VEHICLE_TCP_PORT")' in plugin
    assert "LinkConfiguration::TypeTcp" in plugin
    assert 'settings.setValue(root + QStringLiteral("/auto"), true)' in plugin
    assert 'settings.setValue(root + QStringLiteral("/host")' in plugin
    assert 'settings.setValue(root + QStringLiteral("/port"), vehiclePort)' in plugin
    assert "configuredRtspUrl.isEmpty()" in plugin
    assert "VideoSettings::videoSourceRTSP" in plugin
    assert "video->streamEnabled()->setRawValue(true)" in plugin
    assert "video->rtspAutoReconnect()->setRawValue(true)" in plugin


def test_direct_jetson_metadata_plane_is_signed_ephemeral_and_separate_from_vehicle() -> None:
    controller = _read("src/MultiDetectOperatorController.cc")
    plugin = _read("src/CustomPlugin.cc")

    assert (
        'environmentValue("MULTIDETECT_OPERATOR_UDP_HOST", QStringLiteral("192.168.144.20"))'
        in controller
    )
    assert 'environmentInteger("MULTIDETECT_OPERATOR_UDP_PORT", 14580)' in controller
    assert 'environmentInteger("MULTIDETECT_OPERATOR_UDP_LOCAL_PORT", 14581)' in controller
    assert 'environmentValue("MULTIDETECT_OPERATOR_MAVLINK_KEY_HEX")' in controller
    assert "configuration->setDynamic(true)" in controller
    assert "configuration->addHost(_operatorUdpHost" in controller
    assert "MAVLinkSigning::UnsignedAcceptancePolicy::Strict" in controller
    assert "signing->initSigningImmediate" in controller
    assert "link->sendMessageThreadSafe(message)" in controller
    assert "_operatorConfiguration->link()" in controller
    assert 'QStringLiteral("DIRECT_SIGNED_METADATA_READY")' in controller
    assert 'runtimeEnvironmentValue("MULTIDETECT_VIDEO_RTSP_URL")' in plugin
    assert 'QStringLiteral("HKEY_CURRENT_USER\\\\Environment")' in plugin
    assert 'qputenv("PX_FORCE_CONFIG", "config-env")' in plugin
    assert 'QUrl(rtspUrl).host().toUtf8()' in plugin
    assert 'qputenv("NO_PROXY", noProxy)' in plugin
    assert 'qputenv("QGC_RTSP_FORCE_TCP", "1")' in plugin
    assert 'qputenv("QGC_RTSP_TCP_TIMEOUT_US", "20000000")' in plugin
    assert "VideoSettings::videoSourceRTSP" in plugin
    assert "video->lowLatencyMode()->setRawValue(true)" in plugin
    assert "mavlink_msg_heartbeat_pack_chan" in controller
    assert "MAV_TYPE_GCS" in controller
    assert "_nextDirectHeartbeatAtMs = nowMs + 1000" in controller
    assert 'environmentValue("MULTIDETECT_OPERATOR_KEY")' in controller
    assert 'QStringLiteral("HKEY_CURRENT_USER\\\\Environment")' in controller
    assert "delivery->attempts >= 3" in controller
    assert "acknowledgement timed out" in controller
    assert "retry budget exhausted" not in controller
    assert "_lastStatusAcceptedAtMs" in controller
    assert "kStatusEpochResetAfterMs = 1500" in controller
    assert "_isNewStatus(packet.type, packet.sequence, _lastAuthenticatedAtMs)" in controller


def test_tracking_status_is_an_implicit_selection_ack_and_video_restarts_on_pip_transition() -> None:
    controller = _read("src/MultiDetectOperatorController.cc")
    fly_video = _read("src/FlyViewVideo.qml")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")

    assert 'fields.value(QStringLiteral("selectionCommandId"))' in controller
    assert "_lastStatusSequence.remove(trackStatusKey)" in controller
    assert "_pendingSelection.clear();" in controller
    assert "emit selectionAcknowledged(acknowledgement);" in controller
    assert "onWindowAboutToOpen" in fly_video
    assert "onWindowAboutToClose" in fly_video
    assert fly_video.count("videoStartDelay.start()") == 2
    assert "onTriggered:  QGroundControl.videoManager.startVideo()" in fly_video
    assert 'status.state === "INITIALIZING"' in state


def test_authenticated_depth_grid_is_exposed_as_pip_overlay_and_click_range() -> None:
    controller_header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")
    protocol = _read("src/MultiDetectDepthGridProtocol.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    cmake = (CUSTOM_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "Q_PROPERTY(bool depthMapAvailable" in controller_header
    assert "Q_INVOKABLE double depthAtNormalized" in controller_header
    assert "if (_protocol.configured() && _depthGridUdpPort >= 1024" in controller
    assert 'environmentInteger("MULTIDETECT_DEPTH_GRID_UDP_PORT", 14582)' in controller
    assert 'environmentInteger("MULTIDETECT_DEPTH_GRID_JETSON_PORT", 14583)' in controller
    assert "QMessageAuthenticationCode::hash" in protocol
    assert "qUncompress(compressed)" in protocol
    assert "_crc32(raw)" in protocol
    assert "depthMapDataUrl" in state
    assert "depthAtNormalized" in state
    assert 'text: "DEPTH"' in overlay
    assert "root.depthDisplayMode === 1" in overlay
    assert "root.depthDisplayMode === 2" in overlay
    assert "MultiDetectState.depthMapDataUrl" in overlay
    assert "root._sampleDepth" in overlay
    assert "Network" in cmake


def test_outdoor_fusion_metadata_shows_source_distance_weight_and_log_depth_grid() -> None:
    depth_protocol = _read("src/MultiDetectDepthGridProtocol.cc")
    operator_protocol = _read("src/MultiDetectOperatorProtocol.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    layer = _read("src/FlyViewCustomLayer.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    assert 'QStringLiteral("rgb_slam")' in operator_protocol
    assert 'fields.insert(QStringLiteral("sourceContributions")' in operator_protocol
    assert 'fields.insert(QStringLiteral("vehicleProfile")' in operator_protocol
    assert "fragment->logarithmicEncoding = (flags & 0x01U) != 0" in depth_protocol
    assert "rangeSourceContributions" in state
    assert "function rangeFusionText()" in state
    assert "function rangeContributionText()" in state
    assert "MultiDetectState.rangeContributionText()" in overlay
    assert "MultiDetectState.rangeFusionText()" in indicator
    assert "function _rangeContribution(sources)" in indicator
    assert "MultiDetectState.rangeFusionText()" not in layer
    assert 'return "DEP " + depthMinimumM.toFixed(1)' in state


def test_target_evidence_history_exposes_recent_read_only_records() -> None:
    store_header = _read("src/MultiDetectTargetEvidenceStore.h")
    store_source = _read("src/MultiDetectTargetEvidenceStore.cc")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "Q_PROPERTY(QVariantList recentSnapshots READ recentSnapshots NOTIFY recentSnapshotsChanged)" in store_header
    assert "static constexpr int kRecentSnapshotLimit = 24" in store_header
    assert "void _appendRecentSnapshot(const QVariantMap& snapshot);" in store_header
    assert "_recentSnapshots.prepend(snapshot);" in store_source
    assert "while (_recentSnapshots.size() > kRecentSnapshotLimit)" in store_source
    assert "emit recentSnapshotsChanged();" in store_source
    assert "id: targetEvidenceHistoryDialogFactory" in layer
    assert "id: targetEvidenceHistoryDialogComponent" in layer
    assert "function _openTargetEvidenceHistoryDialog()" in layer
    assert "QGCPopupDialog {" in layer
    assert "multiDetectTargetEvidenceStore.recentSnapshots" in layer
    assert "function _copySavedEvidenceCoordinates(snapshot)" in layer
    assert "function _snapshotRangeValidity()" in layer
    assert '"range_m": _snapshotRangeM()' in layer
    assert '"relative_bearing_deg": _snapshotBearingDeg()' in layer
    assert 'title: qsTr("目标事件记录 · 只读")' in layer
    assert 'text: qsTr("记录只用于态势回看与核对；不会发送飞控指令")' in layer  # noqa: RUF001


def test_fly_view_shows_per_algorithm_ranging_status_and_weight_rows() -> None:
    layer = _read("src/FlyViewCustomLayer.qml")
    indicator = _read("res/Custom/MultiDetect/MultiDetectToolbarIndicator.qml")

    assert "function _rangeContribution(sources)" in indicator
    for name in ("Depth Anything V2", "VIO-SLAM", "RGB-SLAM", "PX4 地面几何"):
        assert name in indicator
    assert 'qsTr("测距中") + " · " + _percent(contribution.weight)' in indicator
    assert 'label: qsTr("融合")' in indicator
    assert "function _rangingAlgorithmActive(sources)" in indicator
    assert 'control._rangingAlgorithmValue(["vio"], qsTr("等待运动"))' in indicator
    assert "function _activeAlgorithmCount()" in indicator
    assert "visible: showForInteraction" not in indicator
    assert "function _rangeContribution(sources)" not in layer


def test_armed_execution_refreshes_the_lck_command_before_waiting_for_challenge() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "function refreshLockForExecution()" in state
    assert 'sendTargetSelection("PROMOTE_LCK", targetX1, targetY1, targetX2, targetY2)' in state
    assert 'statusMessage = qsTr("LCK 已刷新，正在获取执行确认")' in state  # noqa: RUF001
    assert "function _requestExecutionPopup(kind)" in layer
    assert 'root._requestExecutionPopup("payload")' in layer
    assert 'root._requestExecutionPopup("approach")' in layer
    assert "id: executionChallengeWaitTimer" in layer
    assert "MultiDetectState.payloadTargetChallengeActive" in layer
    assert "MultiDetectState.approachChallengeActive" in layer
    assert "if (_pendingExecutionPopup !== \"\")" in layer
    assert "root._openPayloadActionDialog();" in layer
    assert "root._openApproachActionDialog();" in layer
    assert "root._pendingExecutionPopup === \"\"" in layer


def test_fly_view_video_preserves_qgc_camera_and_pip_interactions() -> None:
    native_video = (REPO_ROOT / "src/FlyView/FlyViewVideo.qml").read_text(encoding="utf-8")
    custom_video = _read("src/FlyViewVideo.qml")

    # The custom resource is an additive overlay on the current QGC video
    # surface.  It must not fork away the native gimbal/camera tracking input
    # stack or add a second hand-drawn fixed-wing instrument theme.
    for token in (
        "PipState {",
        "FlightDisplayViewVideo {",
        "OnScreenGimbalController {",
        "OnScreenCameraTrackingController {",
        "mouseDragStart(",
        "mouseDragPositionChanged(",
        "mouseDragEnd(",
        "mouseClicked(",
        "ProximityRadarVideoView",
        "ObstacleDistanceOverlayVideo",
    ):
        assert token in native_video
        assert token in custom_video

    assert "MultiDetectVideoOverlay {" in custom_video

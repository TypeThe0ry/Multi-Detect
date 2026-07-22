from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[2]
CUSTOM_ROOT = REPO_ROOT / "custom"


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
    }

    missing = sorted(path for path in required if not (CUSTOM_ROOT / path).is_file())

    assert not missing


def test_closed_loop_driver_can_require_a_real_external_sitl_vehicle() -> None:
    driver = _read("tests/operator_closed_loop_hil.py")

    assert '"--metadata-only"' in driver
    assert "MAV_TYPE_ONBOARD_CONTROLLER" in driver
    assert "srcSystem=1, srcComponent=191" in driver
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


def test_compact_fixed_wing_instrument_uses_display_only_airspeed_noise_gate() -> None:
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
    video_hud = _read("res/Custom/MultiDetect/FixedWingVideoHud.qml")

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
    assert "root.vehicle ? root.vehicle.airSpeedDisplay : null" in video_hud


def test_custom_app_has_a_stable_semantic_product_version() -> None:
    overrides = _read("cmake/CustomOverrides.cmake")
    options = (REPO_ROOT / "cmake/CustomOptions.cmake").read_text(encoding="utf-8")
    git_cmake = (REPO_ROOT / "cmake/modules/Git.cmake").read_text(encoding="utf-8")
    install_cmake = (REPO_ROOT / "cmake/install/Install.cmake").read_text(encoding="utf-8")

    assert _cmake_string_value(overrides, "QGC_APP_VERSION_OVERRIDE") == "0.2.0"
    assert _cmake_string_value(overrides, "QGC_WINDOWS_INSTALLER_FILENAME") == (
        "MultiDetectGCS-v0.2.0-windows-amd64.exe"
    )
    assert 'set(QGC_APP_VERSION_OVERRIDE ""' in options
    assert "if(QGC_APP_VERSION_OVERRIDE)" in git_cmake
    assert 'set(QGC_APP_VERSION "${QGC_APP_VERSION_OVERRIDE}")' in git_cmake
    assert 'set(QGC_APP_VERSION_STR "v${QGC_APP_VERSION_OVERRIDE}-${QGC_GIT_HASH}")' in git_cmake
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

    assert "!MultiDetectState.selectionMode" in video
    assert "enabled: root.interactionEnabled && MultiDetectState.selectionMode" in overlay
    assert "preventStealing: true" in overlay
    assert "propagateComposedEvents: false" in overlay
    assert "mouse.accepted = true" in overlay
    assert "interactionEnabled: root.pipState.state === root.pipState.fullState" in video
    assert "z: 1000" in video
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
    layer = _read("src/FlyViewCustomLayer.qml")

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
    assert "MultiDetectState.selectableTargetPoolEntries.length" in layer
    assert '"person", "pedestrian", "people", "firefighter"' in overlay
    assert '"vehicle", "car", "van", "truck", "bus", "train"' in overlay
    assert '"aircraft", "airplane", "aeroplane", "plane", "helicopter"' in overlay
    assert 'return "#22d3ee";' in overlay
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
    assert 'readonly property color _lockActionTextColor: "#ff3b30"' in overlay
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
    assert "readonly property bool _targetMapContextVisible" in layer
    assert "function _updateTargetMapProjection()" in layer
    assert "mapControl.fromCoordinate" in layer
    assert "root.mapFromItem(mapControl" in layer
    assert "_targetMapSigmaRadiusPx" in layer
    assert 'qsTr("目标落图 · 只读")' in layer
    assert 'qsTr("目标落图 · 不可定位")' in layer
    assert "QGroundControl.copyToClipboard" in layer
    assert "multiDetectTargetEvidenceStore.saveSnapshot" in layer
    assert 'qsTr("距离：%1 m")' in layer
    assert 'qsTr("相对方位：%1°")' in layer
    assert 'qsTr("更新时间：%1 ms 前 · 源帧 %2")' in layer
    assert 'qsTr("仅显示与记录；不产生飞控指令")' in layer
    assert 'qsTr("不可定位：")' in state
    assert "class MultiDetectTargetEvidenceStore" in store_header
    assert "multidetect.target-evidence.v1" in store_source
    assert "QStandardPaths::AppLocalDataLocation" in store_source
    assert "kMaximumSnapshots = 500" in store_header


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
    assert "function cancelMode3Aim()" in state
    assert "signal lckCueRequested()" in state
    assert "function _requestLckCue(targetIdToAnnounce)" in state
    assert "_requestLckCue(entry.targetId)" in state
    assert 'interactionState !== "LCK" && interactionState !== "TGT"' in state
    assert 'qsTr("遥控输入已接管 · 模式 3 瞄准已取消")' in state
    assert "id: mode3AimBanner" in layer
    assert 'qsTr("模式 3 正在瞄准")' in layer
    assert 'qsTr("模式 3 瞄准状态异常")' in layer
    assert 'qsTr("模式 3 瞄准状态待确认")' in layer
    assert 'qsTr("立即取消瞄准")' in layer
    assert 'qsTr("遥控器任意按钮、拨杆或右侧按钮均可立即取消")' in layer
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
    assert "readonly property bool executionActionable: vehicleArmed && lckActionable" in state
    assert 'statusMessage = qsTr("请先 ARM，再确认模式 3 执行")' in state
    assert 'statusMessage = qsTr("请先 ARM，再确认模式 2 执行")' in state
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
    assert "visible: root.depthDisplayMode === 1" in overlay
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


def test_status_panel_is_large_readable_and_jetson_ai_only() -> None:
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "readonly property var _statusRows" in layer
    for label in (
        "模式",
        "识别",
        "目标",
        "跟踪",
        "数据",
        "场景",
    ):
        assert f'"label": qsTr("{label}")' in layer
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
        assert forbidden not in layer
    assert "_debugExpanded" not in layer
    assert "_debugStatusRows" not in layer
    assert 'text: root._debugExpanded ? "BASIC" : "DEBUG"' not in layer
    assert "ScreenTools.fixedFontFamily" not in layer
    assert "model: root._statusRows" in layer
    assert "ScreenTools.defaultFontPixelWidth * 40" in layer
    assert "Layout.minimumHeight: Math.max(26" in layer
    assert "font.pointSize: ScreenTools.largeFontPointSize" in layer
    assert "font.pointSize: ScreenTools.defaultFontPointSize" in layer
    assert 'text: "JETSON · AI"' in layer
    assert 'return qsTr("等待数据")' in layer
    assert "text: MultiDetectState.metadataLinkState" not in layer
    assert "MultiDetectState.trackingMetadataRateHz" in layer
    assert "MultiDetectState.totalTrackCount" not in layer
    assert "MultiDetectState.lockedTrackCount" in layer
    assert "MultiDetectState.selectableTargetPoolEntries" in layer
    assert "MultiDetectState.targetConfidence" in layer
    assert "MultiDetectState.trackingQuality" in layer
    assert "MultiDetectState.sceneContextRegions" in layer


def test_patrol_and_payload_ui_paths_keep_authorization_separate_from_selection() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert 'mode !== "PATROL" && mode !== "PAYLOAD"' in state
    assert 'missionMode === "PAYLOAD"' in state
    assert 'missionPhase = "AWAITING_AUTHORIZATION"' in state
    assert "function approveAuthorization()" in state
    assert "Popup {" in layer
    assert "MultiDetectState.approveAuthorization()" in layer
    assert "MultiDetectState.beginSelection()" in layer
    assert layer.index("MultiDetectState.approveAuthorization()") > layer.index("Popup {")


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
    assert 'text: qsTr("确认")' in layer
    assert 'text: qsTr("取消")' in layer
    assert "payloadActionPopup.open()" in layer
    assert "approachActionPopup.open()" in layer
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
    assert 'property color reticleColor: "white"' in overlay
    assert 'MultiDetectState.interactionState === "LCK"' in overlay
    assert 'MultiDetectState.interactionState === "TGT"' in overlay
    assert overlay.index('visible: MultiDetectState.missionMode === "OBSERVE"') < overlay.index(
        'property color reticleColor: "white"'
    )
    assert 'text: "+"' in overlay
    assert "MultiDetectState.selectCandidate" in overlay
    assert "MultiDetectState.promoteLock" in overlay
    assert 'MultiDetectState.interactionState === "LCK"' in overlay
    assert 'MultiDetectState.trackingState === "LOST"' in overlay
    assert 'return "#ff6b6b"' in overlay
    assert "anchors.centerIn: parent\n            color: targetBox.border.color" not in overlay


def test_trk_cancel_preempts_pending_manual_selection_and_hud_regions_do_not_overlap() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    hud = _read("res/Custom/MultiDetect/FixedWingVideoHud.qml")
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
    assert "readonly property real _headingTopClearance" in hud
    assert "anchors.topMargin: root._headingTopClearance" in hud
    assert "id: speedPanel" in hud
    assert "anchors.top: speedPanel.bottom" in hud
    assert "anchors.topMargin: 6" in hud
    assert "ScreenTools.defaultFontPixelHeight * 7.4" in layer


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


def test_tracking_status_is_an_implicit_selection_ack_and_video_starts_on_open() -> None:
    controller = _read("src/MultiDetectOperatorController.cc")
    fly_video = _read("src/FlyViewVideo.qml")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")

    assert 'fields.value(QStringLiteral("selectionCommandId"))' in controller
    assert "_lastStatusSequence.remove(trackStatusKey)" in controller
    assert "_pendingSelection.clear();" in controller
    assert "emit selectionAcknowledged(acknowledgement);" in controller
    assert "Component.onCompleted: videoStartDelay.start()" in fly_video
    assert 'status.state === "INITIALIZING"' in state


def test_fixed_rgb_camera_has_no_gimbal_or_camera_tracking_input_path() -> None:
    fly_video = _read("src/FlyViewVideo.qml")

    assert "OnScreenGimbalController" not in fly_video
    assert "OnScreenCameraTrackingController" not in fly_video
    assert "mouseDragStart" not in fly_video
    assert "mouseDragPositionChanged" not in fly_video
    assert "mouseDragEnd" not in fly_video
    assert "mouseClicked" not in fly_video
    assert "onDoubleClicked: QGroundControl.videoManager.fullScreen" in fly_video


def test_authenticated_depth_grid_is_exposed_as_pip_overlay_and_click_range() -> None:
    controller_header = _read("src/MultiDetectOperatorController.h")
    controller = _read("src/MultiDetectOperatorController.cc")
    protocol = _read("src/MultiDetectDepthGridProtocol.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    cmake = (CUSTOM_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")

    assert "Q_PROPERTY(bool depthMapAvailable" in controller_header
    assert "Q_INVOKABLE double depthAtNormalized" in controller_header
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
    controller = _read("src/MultiDetectOperatorController.cc")
    depth_protocol = _read("src/MultiDetectDepthGridProtocol.cc")
    operator_protocol = _read("src/MultiDetectOperatorProtocol.cc")
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    overlay = _read("res/Custom/MultiDetect/MultiDetectVideoOverlay.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert 'QStringLiteral("rgb_slam")' in operator_protocol
    assert 'fields.insert(QStringLiteral("sourceContributions")' in operator_protocol
    assert 'fields.insert(QStringLiteral("vehicleProfile")' in operator_protocol
    assert "fragment->logarithmicEncoding = (flags & 0x01U) != 0" in depth_protocol
    assert "rangeSourceContributions" in state
    assert "function rangeFusionText()" in state
    assert "function rangeContributionText()" in state
    assert "MultiDetectState.rangeContributionText()" in overlay
    assert "MultiDetectState.rangeFusionText()" in layer
    assert 'return "DEP " + depthMinimumM.toFixed(1)' in state


def test_fly_view_shows_per_algorithm_ranging_status_and_weight_rows() -> None:
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "function _rangeContribution(sources)" in layer
    for name in ("Depth Anything V2", "VIO-SLAM", "RGB-SLAM", "PX4 地面几何"):
        assert name in layer
    assert 'qsTr("测距中") + " · " + _percent(contribution.weight)' in layer
    assert '"label": qsTr("融合")' not in layer
    assert "telemetryRow.modelData.algorithm" in layer
    assert "function _rangingAlgorithmActive(sources)" in layer
    assert '"active": root._rangingAlgorithmActive(["vio"])' in layer
    assert 'color: telemetryRow.modelData.active ? "#28c76f" : "#df3b3b"' in layer
    assert "Layout.minimumHeight: Math.max(26" in layer
    assert "visible: showForInteraction" not in layer


def test_armed_execution_refreshes_the_lck_command_before_waiting_for_challenge() -> None:
    state = _read("res/Custom/MultiDetect/MultiDetectState.qml")
    layer = _read("src/FlyViewCustomLayer.qml")

    assert "function refreshLockForExecution()" in state
    assert 'sendTargetSelection("PROMOTE_LCK", targetX1, targetY1, targetX2, targetY2)' in state
    assert 'statusMessage = qsTr("LCK 已刷新，正在获取执行确认")' in state
    assert "function _requestExecutionPopup(kind)" in layer
    assert 'root._requestExecutionPopup("payload")' in layer
    assert 'root._requestExecutionPopup("approach")' in layer
    assert "id: executionChallengeWaitTimer" in layer
    assert "MultiDetectState.payloadTargetChallengeActive" in layer
    assert "MultiDetectState.approachChallengeActive" in layer
    assert "if (_pendingExecutionPopup !== \"\")" in layer
    assert "payloadActionPopup.open();" in layer
    assert "approachActionPopup.open();" in layer
    assert "root._pendingExecutionPopup === \"\"" in layer


def test_video_hud_moves_altitude_and_heading_clear_of_flyview_overlays() -> None:
    hud = _read("res/Custom/MultiDetect/FixedWingVideoHud.qml")

    assert "readonly property real _headingTopClearance: Math.max(96" in hud
    assert "anchors.bottom: parent.bottom" in hud
    assert "id: speedPanel" in hud
    assert "anchors.top: speedPanel.bottom" in hud
    assert "anchors.topMargin: 6" in hud

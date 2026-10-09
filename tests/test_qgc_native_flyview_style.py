from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QGC_ROOT = ROOT / "ground-control" / "MultiDetectGCS"
CUSTOM_ROOT = QGC_ROOT / "custom"
FLY_VIEW = QGC_ROOT / "src" / "FlyView" / "FlyView.qml"
UPSTREAM_FLY_VIEW_VIDEO = QGC_ROOT / "src" / "FlyView" / "FlyViewVideo.qml"
FLY_VIEW_VIDEO = CUSTOM_ROOT / "src" / "FlyViewVideo.qml"
FLY_VIEW_LAYER = CUSTOM_ROOT / "src" / "FlyViewCustomLayer.qml"
VIDEO_OVERLAY = CUSTOM_ROOT / "res" / "Custom" / "MultiDetect" / "MultiDetectVideoOverlay.qml"
MULTIDETECT_STATE = CUSTOM_ROOT / "res" / "Custom" / "MultiDetect" / "MultiDetectState.qml"
TOOLBAR_INDICATOR = (
    CUSTOM_ROOT / "res" / "Custom" / "MultiDetect" / "MultiDetectToolbarIndicator.qml"
)
CUSTOM_QRC = CUSTOM_ROOT / "custom.qrc"
CUSTOM_CMAKE = CUSTOM_ROOT / "CMakeLists.txt"
CUSTOM_PLUGIN_HEADER = CUSTOM_ROOT / "src" / "CustomPlugin.h"
CUSTOM_PLUGIN_SOURCE = CUSTOM_ROOT / "src" / "CustomPlugin.cc"
OPERATOR_CONTROLLER_SOURCE = CUSTOM_ROOT / "src" / "MultiDetectOperatorController.cc"
OPERATOR_CONTROLLER_HEADER = CUSTOM_ROOT / "src" / "MultiDetectOperatorController.h"
OPERATOR_PROTOCOL_SOURCE = CUSTOM_ROOT / "src" / "MultiDetectOperatorProtocol.cc"
OPERATOR_PROTOCOL_HEADER = CUSTOM_ROOT / "src" / "MultiDetectOperatorProtocol.h"
TARGET_EVIDENCE_STORE_SOURCE = CUSTOM_ROOT / "src" / "MultiDetectTargetEvidenceStore.cc"
TARGET_EVENT_REPLAY_STORE_SOURCE = CUSTOM_ROOT / "src" / "MultiDetectTargetEventReplayStore.cc"
TARGET_EVENT_REPLAY_STORE_HEADER = CUSTOM_ROOT / "src" / "MultiDetectTargetEventReplayStore.h"
TARGET_EVENT_REPLAY_STORE_SELFTEST = (
    CUSTOM_ROOT / "tests" / "MultiDetectTargetEventReplayStoreSelfTest.cc"
)
PRODUCER_EPOCH_DIAGNOSTICS_SOURCE = CUSTOM_ROOT / "src" / "MultiDetectProducerEpochDiagnostics.cc"
TARGET_OBSERVATION_DIAGNOSTICS_SOURCE = (
    CUSTOM_ROOT / "src" / "MultiDetectTargetObservationDiagnostics.cc"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _qml_code(path: Path) -> str:
    source = _read(path)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _imports(path: Path) -> set[str]:
    return set(
        re.findall(
            r"^\s*import\s+([A-Za-z][A-Za-z0-9_.]*)",
            _qml_code(path),
            flags=re.MULTILINE,
        )
    )


def _without_qml_component(source: str, component_name: str) -> str:
    match = re.search(rf"\b{re.escape(component_name)}\s*\{{", source)
    assert match is not None, component_name
    opening_brace = source.find("{", match.start())
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[: match.start()] + source[index + 1 :]
    raise AssertionError(f"unterminated {component_name} block")


def _normalized_qml(source: str) -> str:
    return " ".join(source.split())


def test_custom_resources_override_qgc_flyview_components_in_place() -> None:
    qresource = ET.parse(CUSTOM_QRC).getroot().find("qresource[@prefix='/Custom/qml']")
    assert qresource is not None
    aliases = {
        item.attrib["alias"]: (CUSTOM_ROOT / (item.text or "").strip()).resolve()
        for item in qresource.findall("file")
    }
    assert aliases["QGroundControl/FlyView/FlyViewVideo.qml"] == FLY_VIEW_VIDEO.resolve()
    assert aliases["QGroundControl/FlyView/FlyViewCustomLayer.qml"] == FLY_VIEW_LAYER.resolve()

    native_fly_view = _qml_code(FLY_VIEW)
    assert "FlyViewVideo {" in native_fly_view
    assert "PipView {" in native_fly_view
    assert "FlyViewCustomLayer {" in native_fly_view
    assert re.search(r"\bitem2\s*:\s*QGroundControl\.videoManager\.hasVideo", native_fly_view)
    assert re.search(r"\banchors\.fill\s*:\s*widgetLayer", native_fly_view)
    assert re.search(r"\bmapControl\s*:\s*_mapControl", native_fly_view)


def test_multidetect_overlay_uses_native_video_and_pip_surface() -> None:
    video = _qml_code(FLY_VIEW_VIDEO)
    assert {
        "Custom.MultiDetect",
        "QGroundControl.Controls",
        "QGroundControl.FlyView",
    }.issubset(_imports(FLY_VIEW_VIDEO))
    assert re.search(r"^\s*Item\s*\{", video, flags=re.MULTILINE)
    assert "PipState {" in video
    assert "FlightDisplayViewVideo {" in video
    assert "MultiDetectVideoOverlay {" in video
    assert re.search(r"\bproperty\s+Item\s+pipState\s*:\s*videoPipState", video)
    assert re.search(
        r"\binteractionEnabled\s*:\s*_root\.pipState\.state\s*===\s*"
        r"_root\.pipState\.fullState",
        video,
    )
    assert re.search(r"\bheight\s*:\s*videoStreaming\.getHeight\(\)", video)
    assert re.search(r"\bwidth\s*:\s*videoStreaming\.getWidth\(\)", video)
    assert re.search(r"\bvisible\s*:\s*videoStreaming\.visible", video)


def test_acceptance_video_export_is_native_unique_and_fail_closed() -> None:
    plugin = _read(CUSTOM_PLUGIN_SOURCE)
    video = _qml_code(FLY_VIEW_VIDEO)

    export_start = plugin.index("const QString configuredVideoGrabPrefix")
    export_end = plugin.index("_selector = new CustomOverrideInterceptor", export_start)
    export_block = plugin[export_start:export_end]
    for gate in (
        'contains(QStringLiteral("--isolated-hil"))',
        'runtimeEnvironmentExplicitlyTrue("MULTIDETECT_HIL_VIDEO_GRAB_EXPORT")',
        "QFileInfo(configuredVideoGrabPrefix).isAbsolute()",
    ):
        assert gate in export_block
    assert export_block.index("if (hilVideoGrabExport) {") < export_block.index(
        'QStringLiteral("multiDetectHilVideoGrabExport")'
    )
    assert export_block.index("if (hilVideoGrabExport) {") < export_block.index(
        'QStringLiteral("multiDetectHilVideoGrabPrefix")'
    )

    overlay_start = video.index("MultiDetectVideoOverlay {")
    overlay_end = video.index("ProximityRadarVideoView", overlay_start)
    hook = video[overlay_start:overlay_end]
    assert 'candidate.objectName === "videoContent"' in hook
    assert "function _isActiveNativeVideoOutput(candidate)" in hook
    assert "current.visible !== true || current.width <= 0 || current.height <= 0" in hook
    assert "current === videoStreaming" in hook
    assert "_collectNativeVideoOutputs(descendants[index], matches)" in hook
    assert "_collectNativeVideoOutputs(videoStreaming, matches)" in hook
    assert "if (matches.length !== 1)" in hook
    assert "function _nativeOutputDevicePixelRatio(nativeOutput)" in hook
    assert "nativeOutput.window" in hook
    assert "nativeOutput.Window.window" in hook
    assert "quickWindow.devicePixelRatio" in hook
    assert "Number.isFinite(snapshot.device_pixel_ratio)" in hook
    assert hook.count("nativeOutput.grabToImage(") == 1
    assert video.count(".grabToImage(") == 1
    assert '"role": role' in hook
    assert '"native_output_match_count": snapshot.native_output_match_count' in hook
    assert '"video_full_state": snapshot.video_full_state' in hook
    assert '"vtol_in_fwd_flight": snapshot.vtol_in_fwd_flight' in hook
    assert '"capture_width": snapshot.capture_width' in hook
    assert '"capture_height": snapshot.capture_height' in hook
    assert '"device_pixel_ratio": snapshot.device_pixel_ratio' in hook
    assert "Qt.size(snapshot.grab_width, snapshot.grab_height)" in hook
    assert '"grab_width": Number.isFinite(outputWidth)' in hook
    assert '"grab_height": Number.isFinite(outputHeight)' in hook
    assert "result.image" not in hook
    assert 'console.info("MULTIDETECT_HIL_VIDEO_GRAB " + JSON.stringify({' in hook
    assert "QGroundControl.multiVehicleManager.activeVehicle" in hook
    assert "activeVehicle.vtol && activeVehicle.vtolInFwdFlight" in hook
    assert hook.index('_queueNativeVideoGrab("pip-swapped")') < hook.index(
        '_queueNativeVideoGrab("fixed-wing")'
    )

    prohibited = re.compile(
        r"^\s*(?:ApplicationWindow|Window|VideoOutput|MediaPlayer)\s*\{",
        flags=re.MULTILINE,
    )
    assert prohibited.search(video) is None
    assert "QtMultimedia" not in _imports(FLY_VIEW_VIDEO)


def test_custom_video_preserves_native_order_and_routes_target_hits() -> None:
    video = _qml_code(FLY_VIEW_VIDEO)
    native_mouse = video.index("id:                         flyViewVideoMouseArea")
    multidetect_overlay = video.index("MultiDetectVideoOverlay {")
    proximity_radar = video.index("ProximityRadarVideoView{")
    assert native_mouse < multidetect_overlay < proximity_radar
    assert "property bool _multiDetectTargetPress: false" in video
    assert "mapToItem(multiDetectVideoOverlay, mouse.x, mouse.y)" in video
    assert "multiDetectVideoOverlay.hasStablePointerAt(" in video
    assert "multiDetectVideoOverlay.handleStablePointerAt(" in video
    assert "if (_multiDetectTargetPress)" in video


def test_native_gimbal_tracking_pointer_chain_is_preserved() -> None:
    video = _qml_code(FLY_VIEW_VIDEO)
    required_native_fragments = (
        "OnScreenGimbalController {",
        "OnScreenCameraTrackingController {",
        "property real _pressX:",
        "property real _pressY:",
        "property bool _dragging:",
        "readonly property real _dragThreshold: 10",
        "onScreenGimbalController.mouseDragStart(_pressX, _pressY)",
        "cameraTrackingController.mouseDragStart(_pressX, _pressY)",
        "onScreenGimbalController.mouseDragPositionChanged(mouse.x, mouse.y)",
        "cameraTrackingController.mouseDragPositionChanged(mouse.x, mouse.y)",
        "onScreenGimbalController.mouseDragEnd()",
        "cameraTrackingController.mouseDragEnd(mouse.x, mouse.y)",
        "onScreenGimbalController.mouseClicked(mouse.x, mouse.y)",
        "cameraTrackingController.mouseClicked(mouse.x, mouse.y)",
    )
    for fragment in required_native_fragments:
        assert fragment in video

    overlay = _qml_code(VIDEO_OVERLAY)
    assert "function _hasStablePointerHitAt(pixelX, pixelY)" in overlay
    assert "function _renderedCandidateHitAt(pixelX, pixelY)" in overlay
    assert "id: renderTargetRepeater" in overlay
    assert "renderTargetRepeater.itemAt(index)" in overlay
    assert "renderTargetPool.itemAt(index)" not in overlay
    assert "const renderedHit = _renderedCandidateHitAt(pixelX, pixelY);" in overlay
    assert "const stableHit = root._hasStablePointerHitAt(mouse.x, mouse.y);" in overlay
    assert "mouse.accepted = stableHit;" in overlay
    assert "const handled = root._handleStablePointerAt(mouse.x, mouse.y);" in overlay
    assert "mouse.accepted = handled;" in overlay
    assert 'root._emitHilTargetPointerEvent("pressed"' in overlay
    assert 'root._emitHilTargetPointerEvent("clicked"' in overlay

    assert re.search(
        r"MultiDetectVideoOverlay\s*\{[\s\S]*?id\s*:\s*multiDetectVideoOverlay"
        r"[\s\S]*?z\s*:\s*10",
        video,
    )


def test_pip_is_read_only_annotations_without_operator_chrome() -> None:
    overlay = _qml_code(VIDEO_OVERLAY)
    assert re.search(
        r"readonly\s+property\s+bool\s+annotationsOnly\s*:\s*!interactionEnabled",
        overlay,
    )
    assert "visible: root.interactionEnabled && parent.operatorTracked" in overlay
    assert (
        'visible: root.interactionEnabled && MultiDetectState.interactionState === "TRK"' in overlay
    )
    assert "visible: root.interactionEnabled && root.showCompactStatus" in overlay
    assert "visible: root.interactionEnabled && MultiDetectState.operatorConfigured" in overlay
    assert "visible: root.interactionEnabled && root.depthDisplayMode === 1" in overlay
    assert "visible: root.interactionEnabled && root.depthDisplayMode === 2" in overlay
    assert "visible: root.interactionEnabled && MultiDetectState.selectionMode" in overlay
    assert "enabled: root.interactionEnabled && MultiDetectState.selectionMode" in overlay

    # Full-state functionality stays present; PiP hides its action chrome
    # through the parent visibility gates above rather than deleting it.
    for text in ('text: qsTr("取消")', 'text: qsTr("返回 TRK")', 'text: "LCK"'):
        assert text in overlay
    assert "id: compactStatusPanel" in overlay
    assert "id: depthModeButton" in overlay
    assert "id: depthPip" in overlay
    assert "id: fullDepthMap" in overlay


def test_parallel_fixed_wing_video_hud_is_removed() -> None:
    removed_hud = CUSTOM_ROOT / "res" / "Custom" / "MultiDetect" / "FixedWingVideoHud.qml"
    assert not removed_hud.exists()
    assert "FixedWingVideoHud" not in _qml_code(FLY_VIEW_VIDEO)
    assert "FixedWingVideoHud" not in _read(CUSTOM_CMAKE)


def test_flyview_surfaces_use_qgc_controls_palette_and_screen_metrics() -> None:
    for path in (
        FLY_VIEW_VIDEO,
        FLY_VIEW_LAYER,
        VIDEO_OVERLAY,
    ):
        assert "QGroundControl.Controls" in _imports(path)

    for path in (FLY_VIEW_LAYER, VIDEO_OVERLAY):
        code = _qml_code(path)
        assert "QGCPalette {" in code
        assert "QGCButton {" in code
        assert "QGCLabel {" in code
        assert "ScreenTools." in code
        assert "qgcPal.window" in code
        assert "qgcPal.text" in code
        assert "qgcPal.warningText" in code

    layer = _qml_code(FLY_VIEW_LAYER)
    assert "QGCToolInsets {" in layer
    assert "ScreenTools.defaultBorderRadius" in layer
    assert "property var mapControl" in layer


def test_target_geolocation_is_a_qualified_read_only_flyview_map_overlay() -> None:
    layer = _qml_code(FLY_VIEW_LAYER)

    # A target is eligible for projection only when the target identity, GPS
    # coordinates, and uncertainty all come from the same qualified state
    # snapshot.  Invalid GPS must clear the projected point instead of leaving
    # a stale/fabricated marker on the map.
    assert "readonly property bool _targetGeolocationQualified" in layer
    for required_gate in (
        "MultiDetectState.targetGeolocationAvailable",
        "_gnssEvidenceQualified",
        'MultiDetectState.targetGeolocationTargetId !== ""',
        'MultiDetectState.targetId !== ""',
        "MultiDetectState.targetGeolocationTargetId === MultiDetectState.targetId",
        "Number.isFinite(MultiDetectState.targetLatitudeDeg)",
        "MultiDetectState.targetLatitudeDeg > -90.0",
        "MultiDetectState.targetLatitudeDeg < 90.0",
        "Number.isFinite(MultiDetectState.targetLongitudeDeg)",
        "MultiDetectState.targetLongitudeDeg >= -180.0",
        "MultiDetectState.targetLongitudeDeg <= 180.0",
        "Number.isFinite(MultiDetectState.targetHorizontalSigmaM)",
        "MultiDetectState.targetHorizontalSigmaM >= 0.0",
    ):
        assert required_gate in layer
    assert "readonly property bool _targetMapVisible" in layer
    assert "readonly property bool _mapControlReady" in layer
    assert 'typeof mapControl !== "undefined"' in layer
    assert "_targetMapVisible: _targetGeolocationQualified && _mapControlReady" in layer
    assert "_targetMapContextVisible: _mapControlReady" in layer
    assert 'MultiDetectState.targetId !== ""' in layer[layer.index("_targetMapContextVisible") :]
    assert "mapControl.pipState.state === mapControl.pipState.fullState" in layer

    state = _qml_code(MULTIDETECT_STATE)
    geolocation_text_start = state.index("function targetGeolocationText()")
    geolocation_text_end = state.index("function rangeFusionText", geolocation_text_start)
    geolocation_text = state[geolocation_text_start:geolocation_text_end]
    assert 'if (targetId === "")' in geolocation_text
    assert "if (targetGeolocationTargetId !== targetId)" in geolocation_text
    assert "if (!targetGeolocationGnssQualified)" in geolocation_text
    assert "readonly property bool targetGeolocationGnssQualified" in state
    assert "onTargetIdChanged" in state

    projection_start = layer.index("function _projectMapCoordinate(latitude, longitude, sigmaM)")
    projection_end = layer.index("function _requestExecutionPopup", projection_start)
    projection = layer[projection_start:projection_end]
    assert "QtPositioning.coordinate(latitude, longitude)" in projection
    assert "function _updateTargetMapProjection()" in projection
    assert "function _updateReplayMapProjection()" in projection
    assert "function _previewReplayEvent(event)" in projection
    assert "_replayMapPreviewEvent = event" in projection
    assert "gps.coordinates_included === true" in projection
    assert (
        'if (!_mapControlReady || typeof mapControl.fromCoordinate !== "function")' in projection
    )
    assert "mapControl.fromCoordinate(targetCoordinate, false)" in projection
    assert "root.mapFromItem(mapControl, targetMapPoint.x, targetMapPoint.y)" in projection
    assert "_targetMapPoint = Qt.point(-1, -1)" in projection
    assert "_targetMapSigmaRadiusPx = 0.0" in projection
    assert "_replayMapPoint = Qt.point(-1, -1)" in projection
    assert "_replayMapSigmaRadiusPx = 0.0" in projection
    assert "const metersPerDegree = _wgs84MetersPerDegree(latitude)" in projection
    assert "const northLatitude = latitude + latitudeOffsetDeg" in projection
    assert (
        "const eastLongitude = _normalizeLongitudeDeg(longitude + longitudeOffsetDeg)" in projection
    )
    assert "northLatitude <= -90.0 || northLatitude >= 90.0" in projection
    assert "function _wgs84MetersPerDegree(latitudeDeg)" in layer
    assert "function _normalizeLongitudeDeg(longitudeDeg)" in layer
    assert "_wgs84EccentricitySquared" in layer
    assert "111132.0" not in projection
    assert "111320.0" not in projection
    assert "sigmaRadiusPx = Math.max" in projection
    assert "function _snapshotRangeValidity()" in layer
    assert "function _snapshotRangeM()" in layer
    assert "function _snapshotBearingDeg()" in layer
    assert '"range_validity": _snapshotRangeValidity()' in layer
    assert '"range_m": _snapshotRangeM()' in layer
    assert '"relative_bearing_deg": _snapshotBearingDeg()' in layer

    range_handler_start = state.index("function onRangeStatusReceived(status)")
    range_handler_end = state.index(
        "function onTargetGeolocationStatusReceived", range_handler_start
    )
    range_handler = state[range_handler_start:range_handler_end]
    assert 'String(status.validity || "UNAVAILABLE").toUpperCase()' in range_handler
    assert (
        'const rangeUsable = root.rangeValidity === "VALID" || root.rangeValidity === "DEGRADED"'
    ) in range_handler
    assert "const rangeFresh = root.rangeStatusFresh" in range_handler
    assert "if ((!rangeUsable || !rangeFresh) && rangeBelongsToCurrentTarget)" in range_handler
    assert "maximumRangeSourceAgeMs: 1500.0" in state
    assert "maximumRangeDataFreshnessS: 1.5" in state
    assert "rangeDataFresh: Number.isFinite(rangeDataFreshnessS)" in state
    assert "rangeStatusFresh: rangeSourceFresh && rangeDataFresh" in state
    assert "targetGeolocationRangeBindingValid" in state
    assert "targetGeolocationSourceFrameId === rangeSourceFrameId" in state
    assert "MultiDetectState.rangeStatusFresh" in layer

    copy_saved_start = layer.index("function _copySavedEvidenceCoordinates(snapshot)")
    copy_saved_end = layer.index("function _saveTargetEvidenceSnapshot", copy_saved_start)
    copy_saved = layer[copy_saved_start:copy_saved_end]
    # Keep history copy aligned with the qualified map domain: poles have no
    # stable local longitude/map origin and are rejected consistently.
    assert "latitude <= -90.0 || latitude >= 90.0" in copy_saved
    assert "longitude < -180.0 || longitude > 180.0" in copy_saved

    indicator_start = layer.index("id: targetMapIndicator")
    indicator_end = layer.index("id: targetGeolocationCard", indicator_start)
    indicator = layer[indicator_start:indicator_end]
    assert "visible: root._targetMapVisible && root._targetMapPointInView" in indicator
    assert "id: targetUncertaintyRing" in indicator
    assert "root._targetMapSigmaRadiusPx * 2.0" in indicator
    assert 'qsTr("目标 ±%1 m (1σ)")' in indicator

    replay_indicator_start = layer.index("id: replayMapPreviewIndicator")
    replay_indicator_end = layer.index("id: targetGeolocationCard", replay_indicator_start)
    replay_indicator = layer[replay_indicator_start:replay_indicator_end]
    assert "root._replayMapPreviewQualified" in replay_indicator
    assert "root._replayMapSigmaRadiusPx * 2.0" in replay_indicator
    assert 'qsTr("回放预览 · ±%1 m (1σ)")' in replay_indicator

    card_start = layer.index("id: targetGeolocationCard")
    card = layer[card_start:]
    assert "visible: root._targetMapContextVisible" in card
    assert 'qsTr("目标落图 · 只读")' in card
    assert 'qsTr("目标落图 · 不可定位")' in card
    assert "readonly property var _recordingQualitySummary" in layer
    assert "readonly property var _recordingCurrentStatus" in layer
    assert "function _recordingQualityLevelText(level)" in layer
    assert "function _recordingQualityColor(level)" in layer
    assert "function _recordingQualityReasonText(reason)" in layer
    assert "function _recordingQualityText()" in layer
    assert 'qsTr("%1：等待样本")' in layer
    assert 'qsTr("%1：%2 · 定位率 %3%")' in layer
    assert "status.latest_gap_violation === true" in layer
    assert "text: root._recordingQualityText()" in card
    assert 'text: qsTr("复制坐标")' in card
    assert 'text: qsTr("保存快照")' in card
    assert 'text: qsTr("记录")' in card
    assert 'qsTr("仅显示与记录；不产生飞控指令")' in card
    assert "multiDetectTargetEvidenceStore.saveSnapshot" in layer
    assert "root._gnssEvidenceQualified" in card
    assert 'qsTr("不可保存：当前 GNSS 质量证据不合格或已过期")' in layer
    for field in (
        "atomic_observation",
        "producer_epoch",
        "range_status_sequence",
        "range_sent_at_ms",
        "target_geolocation_sequence",
        "target_geolocation_sent_at_ms",
        "gnss_status_sequence",
        "gnss_sent_at_ms",
        "gnss_qualified",
        "gnss_reason",
        "gnss_position_healthy",
        "gnss_fix_type",
        "gnss_satellites_visible",
        "gnss_horizontal_accuracy_m",
        "gnss_vertical_accuracy_m",
        "gnss_source_age_ms",
        "producer_epoch_health_level",
        "producer_epoch_health_reason",
        "producer_restart_count",
        "retired_epoch_reject_count",
        "epoch_transition_reject_count",
        "epoch_changed_at_ms",
        "epoch_last_incident_at_ms",
    ):
        assert f'"{field}"' in layer
    assert "multidetect.target-evidence.v6" in card
    assert "multidetect.target-evidence.v5" in card
    assert "multidetect.target-evidence.v4" in card
    assert 'qsTr("GNSS：代际绑定原子观测' in card
    assert 'qsTr("GNSS：旧版 v3 同生产帧三消息绑定")' in card
    assert 'qsTr("GNSS：旧版 v2 同期质量记录，无原子 envelope 绑定")' in card
    assert 'qsTr("GNSS：旧版 v1 记录，无独立质量证据")' in card
    evidence_store = _read(TARGET_EVIDENCE_STORE_SOURCE)
    assert 'QStringLiteral("multidetect.target-evidence.v1")' in evidence_store
    assert 'QStringLiteral("multidetect.target-evidence.v2")' in evidence_store
    assert 'QStringLiteral("multidetect.target-evidence.v3")' in evidence_store
    assert 'QStringLiteral("multidetect.target-evidence.v4")' in evidence_store
    assert 'QStringLiteral("multidetect.target-evidence.v5")' in evidence_store
    assert 'QStringLiteral("multidetect.target-evidence.v6")' in evidence_store
    assert 'QStringLiteral("producer_epoch")' in evidence_store
    assert "validEpochHealth" in evidence_store
    assert "rangeSequence != targetGeolocationSequence" in evidence_store
    assert "rangeSentAtMs != targetGeolocationSentAtMs" in evidence_store
    assert "gnssHorizontalAccuracyM > 6.0" in evidence_store
    assert "gnssVerticalAccuracyM > 10.0" in evidence_store
    assert "gnssSourceAgeMs > 1500.0" in evidence_store
    assert "MultiDetectTargetEvidenceStoreSelfTest" in _read(CUSTOM_CMAKE)
    assert "function _optionalNumberText(value, precision, suffix)" in layer
    assert 'Number.isFinite(numeric) ? numeric.toFixed(precision) + suffix : "--"' in layer
    assert 'root._optionalNumberText(parent.parent.entry.range_m, 1, " m")' in card
    assert 'root._optionalNumberText(parent.parent.entry.relative_bearing_deg, 1, "°")' in card


def test_native_target_event_replay_inspector_is_hash_bound_and_read_only() -> None:
    layer = _qml_code(FLY_VIEW_LAYER)
    plugin = _read(CUSTOM_PLUGIN_SOURCE)
    cmake = _read(CUSTOM_CMAKE)
    replay_source = _read(TARGET_EVENT_REPLAY_STORE_SOURCE)
    replay_header = _read(TARGET_EVENT_REPLAY_STORE_HEADER)
    replay_selftest = _read(TARGET_EVENT_REPLAY_STORE_SELFTEST)

    assert "multiDetectTargetEventReplayStore" in plugin
    assert "MultiDetectTargetEventReplayStore" in plugin
    assert "MultiDetectTargetEventReplayStore" in cmake
    assert "MultiDetectTargetEventReplayStoreSelfTest" in cmake
    assert "MultiDetectTargetEventReplayStoreSelfTest" in _read(
        ROOT / "scripts" / "build_multidetect_windows.ps1"
    )

    for contract in (
        'QStringLiteral("multidetect.target-event-replay.v1")',
        'QStringLiteral("multidetect.target-event.v1")',
        'QStringLiteral("bundle_hash")',
        'QStringLiteral("events.jsonl")',
        'QStringLiteral("media")',
        "duplicate JSON object key",
        "SHA-256 mismatch",
        "contains_flight_control",
        "contains_truth",
        "GPS 观测时间必须单调不回退",
        'summary->insert(QStringLiteral("sha256")',
        'summary.insert(QStringLiteral("source_frame_id")',
    ):
        assert contract in replay_source
    for property_name in (
        "loaded",
        "integrityVerified",
        "truncated",
        "manifestName",
        "bundleId",
        "sessionId",
        "eventCount",
        "displayedEventCount",
        "mediaCount",
        "locatableEventCount",
        "rangedEventCount",
        "mediaEventCount",
        "uniqueTargetCount",
        "captureDurationS",
        "mediaBytes",
        "events",
        "statusText",
        "lastError",
    ):
        assert "Q_PROPERTY" in replay_header and property_name in replay_header
    assert "valid replay bundle must load" in replay_selftest
    assert "tampered media must fail closed" in replay_selftest
    assert "duplicate JSON keys must fail closed" in replay_selftest

    dialog_start = layer.index("id: targetEventReplayDialog\n")
    dialog = layer[dialog_start:]
    for text in (
        "targetEventReplayManifestFileDialog.openForLoad()",
        "multiDetectTargetEventReplayStore.loadManifest(path)",
    ):
        assert text in layer

    for text in (
        'qsTr("目标事件回放 · 只读")',
        'qsTr("选择清单")',
        'qsTr("清除")',
        'qsTr("复制全量摘要")',
        'qsTr("清单 %1 · 开始 %2 · 事件 %3/%4 · 媒体 %5")',
        'qsTr("可定位: %1 · 有效测距: %2 · 有媒体: %3 · 目标: %4")',
        'qsTr("时长: %1 s · 媒体字节: %2 B")',
        'qsTr("完整性: %1")',
        'qsTr("全量质量：可定位 %1 · 有效测距 %2 · 有媒体 %3 · 目标 %4 · 时长 %5 s · 媒体 %6 B")',
        'qsTr("筛选：%1 · 匹配 %2 条 · 当前显示 %3 条")',
        'qsTr("✓ 可定位")',
        'qsTr("✓ 有效测距")',
        'qsTr("✓ 有媒体")',
        'qsTr("复制证据")',
        'qsTr("落图预览")',
        'qsTr("清除落图预览")',
        'qsTr("地图预览：%1 · 只读 · 不改变当前目标")',
        "SHA-256 %6",
        'qsTr("回放查看器只读取已校验证据；落图预览仅为只读核对，不选目标、发送任务或产生飞控指令")',
        "multiDetectTargetEventReplayStore.events",
    ):
        assert text in dialog
    assert "function replayFilterMatches(event)" in dialog
    assert "function eventClipboardText(event)" in dialog
    assert "function summaryClipboardText()" in dialog
    assert "QGroundControl.copyToClipboard" in dialog
    assert "sendTargetSelection" not in dialog
    assert "executePayload" not in dialog
    assert "executeApproach" not in dialog
    assert "multiDetectOperator" not in dialog


def test_isolated_hil_exports_live_target_hit_geometry_without_selecting_it() -> None:
    overlay = _qml_code(VIDEO_OVERLAY)

    assert "_hilTargetGeometryExportEnabled" in overlay
    assert 'typeof multiDetectHilUiGeometryExport !== "undefined"' in overlay
    assert "function _emitHilTargetGeometry()" in overlay
    assert '"role": "target_candidate_ready"' in overlay
    assert '"item_object_name": "multiDetectTargetCandidate"' in overlay
    assert '"selection_action": "SELECT_TRK"' in overlay
    assert "root.mapToItem(mainWindow.contentItem" in overlay
    export_start = overlay.index("function _emitHilTargetGeometry()")
    export_end = overlay.index("Timer {", export_start)
    export_body = overlay[export_start:export_end]
    assert "sendTargetSelection" not in export_body
    assert "selectCandidate(" not in export_body


def test_selection_diagnostics_are_acceptance_only_and_cover_ack_and_track_status() -> None:
    state = _qml_code(MULTIDETECT_STATE)
    overlay = _qml_code(VIDEO_OVERLAY)

    assert "_hilSelectionDiagnosticEnabled" in state
    assert '"schema": "multidetect.qgc-hil-ui-selection"' in state
    assert '"phase": "selection_acknowledged"' in state
    assert '"accepted": acknowledgement.accepted === true' in state
    assert '"reason": Number(acknowledgement.reason)' in state
    assert '"correlated": acknowledgement.correlated === true' in state
    assert '"phase": "track_status_received"' in state
    assert '"phase": "track_status_adopted"' in state
    assert '"interaction_state": root.interactionState' in state
    assert '"selection_command_id": statusSelectionCommandId' in state
    assert '"targets_current_selection": targetsCurrentSelection' in state
    assert "const candidateVisible = tracked || !MultiDetectState.targetBoxValid" in overlay
    assert "if (!candidateVisible)" in overlay
    assert '"visible": candidateVisible' in overlay


def test_repeated_equal_remote_status_timestamp_cannot_freeze_qgc_command_clock() -> None:
    controller = _read(OPERATOR_CONTROLLER_SOURCE)

    observer_start = controller.index("void MultiDetectOperatorController::_observeRemoteWireTime")
    observer = controller[observer_start:]
    assert "remoteSentAtMs > _remoteWireTimestampMs" in observer
    assert "remoteSentAtMs >= _remoteWireTimestampMs" not in observer
    assert "observationStale && remoteSentAtMs != _remoteWireTimestampMs" in observer
    assert "_remoteWireClockValid || observationStale ||" not in observer


def test_mode3_banner_never_labels_locked_output_as_active_aim() -> None:
    """Keep QGC's native Fly View truthful while live authority is unattested."""

    state = _qml_code(MULTIDETECT_STATE)
    layer = _qml_code(FLY_VIEW_LAYER)

    active_start = state.index("readonly property bool fixedWingAimActive")
    active_end = state.index("readonly property bool mode3AimUiActive", active_start)
    active = state[active_start:active_end]
    assert "fixedWingAimControlEnabled" in active
    assert "approachAimControlActive" in active

    title_start = state.index("function mode3AimBannerTitleText()")
    detail_start = state.index("function mode3AimBannerDetailText()", title_start)
    refresh_start = state.index("function _refreshApproachChallenge()", detail_start)
    title = state[title_start:detail_start]
    detail = state[detail_start:refresh_start]
    assert 'approachPhase === "ABORT"' in title
    assert '!approachStatusFresh || metadataLinkState === "STALE"' in title
    assert "!fixedWingAimControlEnabled" in title
    assert "fixedWingAimActive" in title
    assert title.index("!fixedWingAimControlEnabled") < title.index("fixedWingAimActive")
    assert 'qsTr("模式 3 目标已确认 · 飞控输出锁定")' in title
    assert 'qsTr("模式 3 等待控制接管")' in title
    assert 'qsTr("模式 3 正在瞄准")' in title
    assert 'qsTr("仅显示姿态建议；不会发送姿态或模式命令")' in detail
    assert 'qsTr("等待新鲜 Jetson 状态；不会发送飞控命令")' in detail

    status_start = state.index("function onApproachStatusChanged()")
    status_end = state.index("function onPayloadTargetChallengeChanged()", status_start)
    status_handler = state[status_start:status_end]
    assert "else if (root.fixedWingAimActive)" in status_handler
    assert "else if (root.approachAimControlActive)" not in status_handler

    banner_start = layer.index("id: mode3AimBanner")
    banner_end = layer.index("QGCPopupDialogFactory", banner_start)
    banner = layer[banner_start:banner_end]
    assert "QGCLabel {" in banner
    assert "QGCButton {" in banner
    assert "text: MultiDetectState.mode3AimBannerTitleText()" in banner
    assert "text: MultiDetectState.mode3AimBannerDetailText()" in banner


def test_target_cancel_state_machine_is_single_press_lck_to_trk_then_trk_cancel() -> None:
    """Keep the operator's two-press LCK/TRK contract regression-tested.

    The first press must demote the authoritative primary lock while keeping
    the same target selected.  The next press must issue the per-target
    ``CANCEL_TRK`` command and hide only that target's stale rectangle while
    the acknowledgement is in flight.
    """

    state = _qml_code(MULTIDETECT_STATE)
    layer = _qml_code(FLY_VIEW_LAYER)

    demote_start = state.index("function demoteTrack()")
    demote_end = state.index("function cancelMode3Aim()", demote_start)
    demote = state[demote_start:demote_end]
    assert 'sendTargetSelection("DEMOTE_TRK"' in demote
    assert 'pendingSelectionAction = "DEMOTE_TRK"' in demote
    assert 'interactionState = "TRK"' in demote
    assert 'confirmedPrimaryLockTargetId = ""' in demote

    cancel_start = state.index("function cancelTarget()")
    cancel_end = state.index("function denyAuthorization()", cancel_start)
    cancel = state[cancel_start:cancel_end]
    assert '(interactionState === "LCK" || interactionState === "TGT") && targetBoxValid' in cancel
    assert "return demoteTrack();" in cancel
    assert "return cancelTrackedCandidate(selectedEntry);" in cancel
    assert "return _cancelCurrentTrackByBox();" in cancel

    track_status_start = state.index("function onTrackStatusReceived(status)")
    track_status_end = state.index("property Settings persistedSettings", track_status_start)
    track_status = state[track_status_start:track_status_end]
    assert 'completedAction === "CANCEL_TRK" && status.state !== "CANCELLED"' in track_status
    assert 'root.statusMessage = qsTr("当前 TRK 取消中")' in track_status
    assert 'completedAction !== "CANCEL_TRK" && status.targetPresent' in track_status
    assert 'const cancelledStatusTargetId = String(status.targetId || "")' in track_status

    timeout_start = state.index("property Timer selectionTrackStatusTimer")
    timeout_end = state.index("property string statusMessage", timeout_start)
    timeout = state[timeout_start:timeout_end]
    assert 'timedOutAction === "CANCEL_TRK"' in timeout
    assert "TRK 取消未确认；目标保持隐藏" in timeout
    assert "_removePendingCancelledTarget(root.pendingCancelledTargetId)" not in timeout

    # A manual box can be cancelled before Jetson has assigned a stable target
    # id. Keep a geometry/IoU suppression record until the next authoritative
    # pool cadence so that late association cannot resurrect the rectangle.
    assert "property var pendingCancelledTargetBoxes" in state
    assert "property int targetPoolGeneration" in state
    assert "function _targetBoxIoU(first, second)" in state
    assert "function _pendingCancelledTargetBoxMatches(entry)" in state
    assert "function _releasePendingCancelledBoxesForSelection(selectionBox)" in state
    assert "_pendingCancelledTargetBoxMatches(entry)" in state
    assert "isCancellationPendingTarget(entry)" in state
    assert "operatorTracked === true" in state
    assert "poolGenerationAtCancel: targetPoolGeneration" in state
    assert "root.targetPoolGeneration += 1" in state
    assert "_releasePendingCancelledBoxesForSelection({" in state
    assert 'cancelledStatusTargetId = String(status.targetId || "")' in state
    assert "_removePendingCancelledTargetBox(statusSelectionCommandId)" in state

    action_text = layer.index("text: MultiDetectState.mode3AimUiActive")
    action_start = layer.rfind("QGCButton {", layer.index("id: actionBar"), action_text)
    assert action_start >= 0
    action_end = layer.index("QGCButton {", action_start + 1)
    action = layer[action_start:action_end]
    assert 'interactionState === "LCK"' in action
    assert 'interactionState === "TGT"' in action
    assert 'qsTr("返回 TRK")' in action
    assert "MultiDetectState.demoteTrack()" in action
    assert "MultiDetectState.cancelTarget()" in action


def test_multidetect_status_extends_the_native_qgc_toolbar_and_drawer() -> None:
    header = _read(CUSTOM_PLUGIN_HEADER)
    plugin = _read(CUSTOM_PLUGIN_SOURCE)
    cmake = _read(CUSTOM_CMAKE)
    indicator = _qml_code(TOOLBAR_INDICATOR)
    layer = _qml_code(FLY_VIEW_LAYER)

    assert "const QVariantList& toolBarIndicators() final;" in header
    assert "_toolBarIndicatorList = QGCCorePlugin::toolBarIndicators();" in plugin
    assert "_toolBarIndicatorList.append" in plugin
    assert "qrc:/qml/Custom/MultiDetect/MultiDetectToolbarIndicator.qml" in plugin
    assert "QT_RESOURCE_ALIAS MultiDetectToolbarIndicator.qml" in cmake
    assert "MultiDetectToolbarIndicator.qml" in cmake

    assert "QGroundControl.Controls" in _imports(TOOLBAR_INDICATOR)
    assert "pragma ComponentBehavior: Bound" in indicator
    for native_control in (
        "QGCPalette {",
        "QGCColoredImage {",
        "QGCLabel {",
        "ToolIndicatorPage {",
        "SettingsGroupLayout {",
        "LabelledLabel {",
        "ScreenTools.",
        "mainWindow.showIndicatorDrawer(indicatorPage, control)",
        "showExpand: true",
        "expandedComponent: Component {",
    ):
        assert native_control in indicator
    assert 'objectName: "toolbar_multiDetectIndicator"' in indicator
    assert 'source: "/qmlimages/Analyze.svg"' in indicator
    assert "ScreenTools.smallFontPointSize" in indicator
    assert 'Accessible.name: "MultiDetect AI Status"' in indicator
    assert "Accessible.role: Accessible.Button" in indicator
    assert 'Accessible.name: "MultiDetect AI Status Drawer"' in indicator
    assert "Accessible.role: Accessible.Pane" in indicator
    assert "function _emitLifecycle(" in indicator
    assert '"drawer_closed", "closed"' in indicator
    assert 'control._queueDrawerGeometry("drawer_closed", "closed")' not in indicator
    for status in (
        "MultiDetectState.runtimeDisplayMode",
        "MultiDetectState.runtimeBackendHealth",
        "MultiDetectState.metadataLinkState",
        "_activeAlgorithmCount()",
    ):
        assert status in indicator
    compact = indicator[
        indicator.index("contentComponent: Component {") : indicator.index(
            "expandedComponent: Component {"
        )
    ]
    assert compact.count("LabelledLabel {") == 5
    assert 'label: qsTr("签名链路")' in compact
    assert 'label: qsTr("算法")' in compact
    expanded = indicator[indicator.index("expandedComponent: Component {") :]
    for detail in (
        'heading: qsTr("运行环境")',
        'heading: qsTr("Jetson · AI")',
        'heading: qsTr("室外测距质量")',
        'label: qsTr("GPS/GNSS")',
        'label: qsTr("RANGE_STATUS")',
        'label: "Depth Anything V2"',
        'label: qsTr("Gazebo 深度")',
        'label: qsTr("深度图")',
        'label: qsTr("标定")',
    ):
        assert detail in expanded
    assert "function _gpsQualityValue()" in indicator
    assert "MultiDetectState.gnssStatusAvailable" in indicator
    assert "MultiDetectState.gnssQualified" in indicator
    assert "MultiDetectState.gnssReason" in indicator
    assert "function _gnssDetailValue()" in indicator
    assert 'label: qsTr("GNSS 详情")' in expanded
    assert "function _rangeStatusValue()" in indicator

    assert "id: statusPanel" not in layer
    assert "id: runtimeIdentityBadge" not in layer
    assert "id: runtimeIdentityLabel" not in layer
    assert "statusPanel." not in layer
    assert "runtimeIdentityBadge." not in layer


def test_raw_gnss_quality_is_authenticated_display_only_and_target_independent() -> None:
    protocol_header = _read(OPERATOR_PROTOCOL_HEADER)
    protocol = _read(OPERATOR_PROTOCOL_SOURCE)
    controller_header = _read(OPERATOR_CONTROLLER_HEADER)
    controller = _read(OPERATOR_CONTROLLER_SOURCE)
    diagnostics = _read(PRODUCER_EPOCH_DIAGNOSTICS_SOURCE)
    target_diagnostics = _read(TARGET_OBSERVATION_DIAGNOSTICS_SOURCE)
    state = _qml_code(MULTIDETECT_STATE)
    indicator = _qml_code(TOOLBAR_INDICATOR)
    layer = _read(FLY_VIEW_LAYER)

    assert "GnssStatus = 25" in protocol_header
    assert "TargetObservationStatus = 26" in protocol_header
    gnss_start = protocol.index("} else if (type == MessageType::GnssStatus) {")
    gnss_end = protocol.index(
        "} else if (type == MessageType::TargetObservationStatus) {", gnss_start
    )
    gnss = protocol[gnss_start:gnss_end]
    for gate in (
        "fixTypeValue < 3",
        "satellitesValue < 6",
        "horizontalAccuracy > 60",
        "verticalAccuracy > 100",
        "sourceAge > 15",
    ):
        assert gate in gnss
    assert 'QStringLiteral("gps_qualified")' in gnss
    assert 'QStringLiteral("advisoryOnly"), true' in gnss
    assert 'QStringLiteral("flightControlEnabled"), false' in gnss
    assert 'QStringLiteral("physicalReleaseEnabled"), false' in gnss
    assert "latitudeDeg" not in gnss
    assert "longitudeDeg" not in gnss

    assert (
        "Q_PROPERTY(QVariantMap gnssStatus READ gnssStatus NOTIFY gnssStatusChanged)"
        in controller_header
    )
    assert "void gnssStatusReceived(const QVariantMap& status);" in controller_header
    assert "packet.type == MultiDetectOperatorProtocol::MessageType::GnssStatus" in controller
    assert 'fields.insert(QStringLiteral("sequence"), packet.sequence)' in controller
    assert 'fields.insert(QStringLiteral("sentAtMs")' in controller
    assert "_lastGnssStatusAtMs = _lastAuthenticatedAtMs" in controller
    assert "constexpr quint64 kGnssStatusStaleMs = 1000" in controller
    assert "nowMs - _lastGnssStatusAtMs > kGnssStatusStaleMs" in controller

    assert "function onGnssStatusReceived(status)" in state
    assert "property bool gnssStatusAvailable: false" in state
    assert "property real gnssSequence: -1" in state
    assert "property real gnssSentAtMs: NaN" in state
    assert "property bool gnssAtomicObservation: false" in state
    assert "function _resetGnssStatus()" in state
    target_geo_start = controller.index(
        "packet.type == MultiDetectOperatorProtocol::MessageType::TargetGeolocationStatus"
    )
    target_geo_end = controller.index(
        "packet.type == MultiDetectOperatorProtocol::MessageType::GnssStatus", target_geo_start
    )
    target_geo = controller[target_geo_start:target_geo_end]
    assert 'fields.insert(QStringLiteral("sequence"), packet.sequence)' in target_geo
    assert 'fields.insert(QStringLiteral("sentAtMs")' in target_geo
    assert "function onTargetGeolocationStatusReceived(status)" in state
    assert "property real targetGeolocationSequence: -1" in state
    assert "property real targetGeolocationSentAtMs: NaN" in state
    assert "property bool targetGeolocationAtomicObservation: false" in state
    assert "property bool rangeAtomicObservation: false" in state
    assert "readonly property bool targetGeolocationGnssBindingValid" in state
    assert "rangeSequence === targetGeolocationSequence" in state
    assert "targetGeolocationSequence === gnssSequence" in state
    assert "rangeSentAtMs === targetGeolocationSentAtMs" in state
    assert "targetGeolocationSentAtMs === gnssSentAtMs" in state
    assert "rangeProducerEpoch === targetGeolocationProducerEpoch" in state
    assert "targetGeolocationProducerEpoch === gnssProducerEpoch" in state
    assert "MultiDetectState.targetGeolocationGnssBindingValid" in layer
    atomic_start = controller.index(
        "packet.type == MultiDetectOperatorProtocol::MessageType::TargetObservationStatus"
    )
    atomic_end = controller.index(
        "packet.type == MultiDetectOperatorProtocol::MessageType::ReleaseStatus", atomic_start
    )
    atomic = controller[atomic_start:atomic_end]
    assert 'fields.value(QStringLiteral("rangeStatus")).toMap()' in atomic
    assert 'fields.value(QStringLiteral("targetGeolocationStatus")).toMap()' in atomic
    assert 'fields.value(QStringLiteral("gnssStatus")).toMap()' in atomic
    assert atomic.count('insert(QStringLiteral("atomicObservation"), true)') == 3
    assert atomic.count('insert(QStringLiteral("producerEpoch")') == 3
    assert "_acceptTargetObservationEpoch(fields)" in controller
    assert "_targetObservationEpoch.observe(incomingEpoch)" in controller
    assert "MultiDetectObservationEpoch::Result::CapacityExhausted" in controller
    assert "_targetObservationRetiredEpochRejectCount" in controller
    assert "_targetObservationEpochTransitionRejectCount" in controller
    assert "_targetObservationEpochLastIncidentAtMs" in controller
    assert "targetObservationEpochStatsChanged" in controller
    assert 'return QStringLiteral("CRITICAL")' in controller
    assert 'return QStringLiteral("WARNING")' in controller
    assert 'return QStringLiteral("NOTICE")' in controller
    assert 'return QStringLiteral("HEALTHY")' in controller
    assert "kMaximumRetiredEpochs = 32" in _read(CUSTOM_ROOT / "src/MultiDetectObservationEpoch.h")
    assert "return Result::CapacityExhausted" in _read(
        CUSTOM_ROOT / "src/MultiDetectObservationEpoch.h"
    )
    gps_start = indicator.index("function _gpsQualityValue()")
    gps_end = indicator.index("function _gnssDetailValue()", gps_start)
    gps = indicator[gps_start:gps_end]
    assert "MultiDetectState.gnssStatusAvailable" in gps
    assert "MultiDetectState.targetId" not in gps
    assert "targetGeolocation" not in gps
    assert 'qsTr("不可定位 · 等待 GNSS_STATUS")' in gps
    assert 'text: qsTr("观测生产者")' in indicator
    assert "function _producerEpochValue()" in indicator
    assert "function _producerEpochHealthText()" in indicator
    assert "function _producerEpochHealthColor()" in indicator
    assert 'qsTr("严重：epoch 切换容量耗尽，重启 QGC")' in indicator
    assert 'qsTr("警告：已拒绝退休 epoch 旧包")' in indicator
    assert "MultiDetectState.targetObservationProducerEpoch" in indicator
    assert "MultiDetectState.targetObservationProducerRestartCount" in indicator
    assert "MultiDetectState.targetObservationRetiredEpochRejectCount" in indicator
    assert "MultiDetectState.targetObservationEpochTransitionRejectCount" in indicator
    assert "targetObservationEpochEvents READ targetObservationEpochEvents" in controller_header
    assert (
        "targetObservationEpochEventCount READ targetObservationEpochEventCount"
        in controller_header
    )
    assert (
        "targetObservationEpochDroppedEventCount READ targetObservationEpochDroppedEventCount"
        in controller_header
    )
    assert "targetObservationEpochDiagnosticsJson() const" in controller_header
    assert "maximumEvents() { return 64; }" in _read(
        CUSTOM_ROOT / "src/MultiDetectProducerEpochDiagnostics.h"
    )
    assert 'QStringLiteral("epoch_initialized")' in controller
    assert 'QStringLiteral("producer_restart")' in controller
    assert 'QStringLiteral("retired_epoch_rejected")' in controller
    assert 'QStringLiteral("transition_capacity_exhausted")' in controller
    assert 'QStringLiteral("multidetect.producer-epoch-diagnostics.v1")' in diagnostics
    assert 'document.insert(QStringLiteral("advisory_only"), true)' in diagnostics
    assert 'document.insert(QStringLiteral("flight_control_enabled"), false)' in diagnostics
    assert 'document.insert(QStringLiteral("physical_release_enabled"), false)' in diagnostics
    assert 'summary.insert(QStringLiteral("retained_event_count")' in diagnostics
    assert 'summary.insert(QStringLiteral("dropped_event_count")' in diagnostics
    assert "QSaveFile file(outputPath)" in diagnostics
    assert "saveTargetObservationEpochDiagnostics()" in controller_header
    assert "function producerEpochDiagnosticsJson()" in state
    assert "function saveProducerEpochDiagnostics()" in state
    assert "function _copyProducerEpochDiagnostics()" in layer
    assert "function _saveProducerEpochDiagnostics()" in layer
    assert 'text: qsTr("诊断事件")' in layer
    assert 'text: qsTr("复制诊断")' in layer
    assert 'text: qsTr("保存诊断")' in layer
    assert 'title: qsTr("生产者 epoch 诊断 · 只读")' in layer
    assert "MultiDetectState.targetObservationEpochEvents.length -" in layer
    assert "enabled: MultiDetectState.targetObservationEpochEventCount > 0" not in layer
    assert "MultiDetectState.targetObservationEpochDroppedEventCount" in indicator
    assert "targetObservationDiagnosticsJson() const" in controller_header
    assert "saveTargetObservationDiagnostics()" in controller_header
    assert "function targetObservationDiagnosticsJson()" in state
    assert "function saveTargetObservationDiagnostics()" in state
    assert "function _copyTargetObservationDiagnostics()" in layer
    assert "function _saveTargetObservationDiagnostics()" in layer
    assert "startTargetObservationDiagnosticsRecording()" in controller_header
    assert "stopTargetObservationDiagnosticsRecording()" in controller_header
    assert "targetObservationRecordingChanged" in controller_header
    assert (
        "targetObservationRecordingQualitySummary READ targetObservationRecordingQualitySummary"
        in controller_header
    )
    assert "function startTargetObservationDiagnosticsRecording()" in state
    assert "function stopTargetObservationDiagnosticsRecording()" in state
    assert "targetObservationRecordingQualitySummary" in state
    assert "function _toggleTargetObservationDiagnosticsRecording()" in layer
    assert 'qsTr("开始定位诊断录制")' in layer
    assert 'qsTr("停止定位诊断录制")' in layer
    assert 'text: qsTr("复制定位诊断")' in layer
    assert 'text: qsTr("查看定位诊断")' in layer
    assert 'text: qsTr("保存定位诊断")' in layer
    assert 'title: qsTr("目标定位诊断 · 只读")' in layer
    assert "JSON.parse(payload)" in layer
    assert 'qsTr("录制质量 · 实时只读")' in layer
    assert 'qsTr("质量时间轴")' in layer
    assert 'qsTr("当前录制未发现连续质量异常段")' in layer
    assert "recordingSampling.cadence_violation_count" in layer
    assert "recordingQuality.current_status" in layer
    assert "recordingCurrentStatus.current_non_locatable_run" in layer
    assert "recordingCurrentStatus.current_range_invalid_run" in layer
    assert "recordingCurrentStatus.current_gnss_unqualified_run" in layer
    assert "recordingCurrentStatus.latest_gap_ms" in layer
    assert 'qsTr("当前状态：%1")' in layer
    assert 'qsTr("良好")' in layer
    assert 'qsTr("警告")' in layer
    assert 'qsTr("严重")' in layer
    assert "recordingQuality.longest_non_locatable_run" in layer
    assert "recordingQuality.epoch_transition_count" in layer
    assert 'qsTr("不可定位 · 坐标已强制省略")' in layer
    assert 'qsTr("定位门未全部通过；本诊断不含经纬度或 1σ")' in layer
    assert 'qsTr("诊断只用于定位排错与证据记录；不会发送飞控指令或启用物理释放")' in layer
    assert 'QStringLiteral("multidetect.target-observation-diagnostics.v1")' in target_diagnostics
    assert 'readiness.insert(QStringLiteral("locatable"), locatable)' in target_diagnostics
    assert (
        'readiness.insert(QStringLiteral("coordinates_included"), locatable)' in target_diagnostics
    )
    assert "if (locatable)" in target_diagnostics
    assert 'geolocation.insert(QStringLiteral("latitudeDeg")' in target_diagnostics
    assert 'document.insert(QStringLiteral("advisory_only"), true)' in target_diagnostics
    assert 'document.insert(QStringLiteral("flight_control_enabled"), false)' in target_diagnostics
    assert (
        'document.insert(QStringLiteral("physical_release_enabled"), false)' in target_diagnostics
    )
    assert "QSaveFile file(outputPath)" in target_diagnostics


def test_operator_actions_use_native_qgc_buttons_without_a_custom_hud_card() -> None:
    layer = _read(FLY_VIEW_LAYER)

    action_start = layer.index("id: actionBar")
    action_context = layer[max(0, action_start - 260) : action_start + 2200]
    assert "Item {\n        id: actionBar" in action_context
    assert "QGCButton {" in action_context
    assert "border.color:" not in action_context[:700]
    assert "color: qgcPal.window" not in action_context[:700]
    assert "opacity: 0.92" not in action_context[:700]


def test_custom_qml_does_not_import_an_independent_quick_controls_theme() -> None:
    qml_paths = tuple(CUSTOM_ROOT.rglob("*.qml"))
    assert qml_paths
    prohibited_theme_imports = {
        "QtQuick.Controls.Material",
        "QtQuick.Controls.Universal",
        "QtQuick.Controls.Fusion",
        "QtQuick.Controls.Imagine",
    }
    for path in qml_paths:
        assert _imports(path).isdisjoint(prohibited_theme_imports), path


def test_custom_qml_uses_qgc_tokens_for_ui_colors_and_controls() -> None:
    qml_paths = tuple(CUSTOM_ROOT.rglob("*.qml"))
    assert qml_paths

    # Target/depth data may use a fixed false-colour ramp; every other UI
    # colour must come from the active QGC palette so light/dark/mobile themes
    # remain controlled by QGC rather than a private Multi-Detect theme.
    allowed_data_colors = {"#ff0000", "#00ff44", "#0000ff"}
    hex_color = re.compile(r"#[0-9a-fA-F]{6,8}")
    private_quick_control = re.compile(
        r"^\s*(?:Button|Label|TextField|ComboBox|CheckBox|Switch|Slider|"
        r"Popup|Dialog|Menu|MenuItem)\s*\{",
        flags=re.MULTILINE,
    )
    for path in qml_paths:
        code = _qml_code(path)
        unexpected_colors = sorted(
            {
                literal.lower()
                for literal in hex_color.findall(code)
                if literal.lower() not in allowed_data_colors
            }
        )
        assert not unexpected_colors, f"private UI color literals in {path}: {unexpected_colors}"
        assert private_quick_control.search(code) is None, (
            f"unqualified QtQuick Controls component in {path}"
        )


def test_custom_qml_does_not_create_a_parallel_window_or_simulator_surface() -> None:
    qml_paths = tuple((CUSTOM_ROOT / "src").glob("*.qml")) + tuple(
        (CUSTOM_ROOT / "res" / "Custom" / "MultiDetect").glob("*.qml")
    )
    assert qml_paths
    window_component = re.compile(r"^\s*(?:ApplicationWindow|Window)\s*\{", flags=re.MULTILINE)
    mode_specific_surface_name = re.compile(
        r"(?:simulator|replay|hil|real).*(?:fly|view|window)|"
        r"(?:fly|view|window).*(?:simulator|replay|hil|real)",
        flags=re.IGNORECASE,
    )
    for path in qml_paths:
        code = _qml_code(path)
        assert "QtQuick.Window" not in _imports(path), path
        assert window_component.search(code) is None, path
        assert mode_specific_surface_name.search(path.stem) is None, path


def test_replay_sim_hil_and_real_share_one_state_and_toolbar_indicator() -> None:
    state = _qml_code(MULTIDETECT_STATE)
    layer = _qml_code(FLY_VIEW_LAYER)
    video = _qml_code(FLY_VIEW_VIDEO)
    overlay = _qml_code(VIDEO_OVERLAY)
    indicator = _qml_code(TOOLBAR_INDICATOR)
    cmake = _read(CUSTOM_CMAKE)

    assert re.search(r"^\s*pragma\s+Singleton\s*$", state, flags=re.MULTILINE)
    assert re.search(
        r'\[\s*"REPLAY"\s*,\s*"SIM"\s*,\s*"HIL"\s*,\s*"REAL"\s*\]'
        r"\.indexOf\(runtimeDisplayMode\)",
        state,
    )
    assert "QT_QML_SINGLETON_TYPE TRUE" in cmake
    assert "MultiDetectState.qml" in cmake
    assert "MultiDetectVideoOverlay.qml" in cmake

    assert "MultiDetectState.runtimeDisplayMode" in indicator
    assert "MultiDetectState.runtimeBackendHealth" in indicator
    assert "MultiDetectState.metadataLinkState" in indicator
    assert "id: runtimeIdentityBadge" not in layer
    assert "id: runtimeIdentityLabel" not in layer
    assert "Custom.MultiDetect" in _imports(FLY_VIEW_VIDEO)
    assert "MultiDetectVideoOverlay {" in video
    assert "MultiDetectState" in overlay
    assert "id: runtimeIdentityBadge" not in video
    assert "id: runtimeIdentityBadge" not in overlay

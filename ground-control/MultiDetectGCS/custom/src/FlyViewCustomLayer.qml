import Custom.MultiDetect

import QGroundControl
import QGroundControl.Controls
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia
import QtPositioning

Item {
    id: root

    property real _margin: ScreenTools.defaultFontPixelWidth * 0.9
    readonly property bool _targetActionable: MultiDetectState.lckActionable
    readonly property bool _replaySafetyActive: MultiDetectState.replaySafetyActive
    // Acceptance-only driver for the native Fly View state machine.  It is
    // hosted by this already-rendered QGC layer, so the same mode selector,
    // target overlay and authorization functions are exercised without
    // creating a second window, simulator UI, or custom visual style.
    readonly property bool _hilUiAutoExercise: (typeof multiDetectHilUiAutoExercise !== "undefined") &&
                                               multiDetectHilUiAutoExercise === true
    readonly property bool _hilUiAutoExit: (typeof multiDetectHilUiAutoExit !== "undefined") &&
                                           multiDetectHilUiAutoExit === true
    readonly property int _hilUiAutoExitDelayMs:
        typeof multiDetectHilUiAutoExitDelayMs !== "undefined" ? multiDetectHilUiAutoExitDelayMs : 0
    property int _hilUiAutoExerciseStage: 0
    property var mapControl
    property var parentToolInsets
    property var totalToolInsets: toolInsets
    property string _pendingExecutionPopup: ""
    property double _pendingExecutionStartedAtMs: 0
    property var _targetEvidenceHistoryDialog: null
    property var _producerEpochDiagnosticsDialog: null
    property var _targetObservationDiagnosticsDialog: null
    property var _payloadActionDialog: null
    property var _approachActionDialog: null
    property var _authorizationDialog: null
    // Keep the map-only uncertainty projection on the same WGS-84 ellipsoid
    // as the Python geolocation backend.  These values are used solely to
    // convert metres to the two local degree offsets needed by QGC's map
    // projection; the backend's Vincenty result remains authoritative.
    readonly property real _wgs84SemiMajorAxisM: 6378137.0
    readonly property real _wgs84EccentricitySquared: 0.0066943799901413165
    readonly property bool _targetGeolocationQualified: MultiDetectState.targetGeolocationAvailable &&
                                                       MultiDetectState.targetGeolocationSourceFresh &&
                                                       MultiDetectState.targetGeolocationTargetId !== "" &&
                                                       MultiDetectState.targetId !== "" &&
                                                       MultiDetectState.targetGeolocationTargetId === MultiDetectState.targetId &&
                                                       Number.isFinite(MultiDetectState.targetLatitudeDeg) &&
                                                       MultiDetectState.targetLatitudeDeg > -90.0 &&
                                                       MultiDetectState.targetLatitudeDeg < 90.0 &&
                                                       Number.isFinite(MultiDetectState.targetLongitudeDeg) &&
                                                       MultiDetectState.targetLongitudeDeg >= -180.0 &&
                                                       MultiDetectState.targetLongitudeDeg <= 180.0 &&
                                                       Number.isFinite(MultiDetectState.targetHorizontalSigmaM) &&
                                                       MultiDetectState.targetHorizontalSigmaM >= 0.0
    readonly property bool _gnssEvidenceQualified: MultiDetectState.gnssStatusAvailable &&
                                                  MultiDetectState.targetGeolocationGnssBindingValid &&
                                                  MultiDetectState.gnssQualified &&
                                                  MultiDetectState.gnssReason === "gps_qualified" &&
                                                  MultiDetectState.gnssPositionHealthyAvailable &&
                                                  MultiDetectState.gnssPositionHealthy &&
                                                  Number.isFinite(MultiDetectState.gnssSequence) &&
                                                  MultiDetectState.gnssSequence >= 0 &&
                                                  Math.floor(MultiDetectState.gnssSequence) ===
                                                      MultiDetectState.gnssSequence &&
                                                  Number.isFinite(MultiDetectState.gnssSentAtMs) &&
                                                  MultiDetectState.gnssSentAtMs >= 0 &&
                                                  MultiDetectState.gnssFixType >= 3 &&
                                                  MultiDetectState.gnssSatellitesVisible >= 6 &&
                                                  MultiDetectState.gnssHorizontalAccuracyM >= 0.0 &&
                                                  MultiDetectState.gnssHorizontalAccuracyM <= 6.0 &&
                                                  MultiDetectState.gnssVerticalAccuracyM >= 0.0 &&
                                                  MultiDetectState.gnssVerticalAccuracyM <= 10.0 &&
                                                  Number.isFinite(MultiDetectState.gnssSourceAgeMs) &&
                                                  MultiDetectState.gnssSourceAgeMs >= 0.0 &&
                                                  MultiDetectState.gnssSourceAgeMs <= 1500.0
    // QGC creates this layer before the map assigns its control object.  An
    // uninitialised QML var is `undefined` (not `null`), so checking only
    // `mapControl !== null` would dereference `pipState` during the first
    // qualified GPS update and throw a QML TypeError.  Keep all map bindings
    // behind one readiness gate; the card can still be shown once the native
    // map control has finished initialising.
    readonly property bool _mapControlReady: mapControl !== null &&
                                              typeof mapControl !== "undefined" &&
                                              mapControl.pipState !== null &&
                                              typeof mapControl.pipState !== "undefined" &&
                                              typeof mapControl.pipState.state !== "undefined" &&
                                              typeof mapControl.pipState.fullState !== "undefined"
    readonly property bool _targetMapVisible: _targetGeolocationQualified && _mapControlReady &&
                                              mapControl.pipState.state === mapControl.pipState.fullState
    readonly property bool _targetMapContextVisible: _mapControlReady &&
                                                     mapControl.pipState.state === mapControl.pipState.fullState &&
                                                     MultiDetectState.targetId !== ""
    property point _targetMapPoint: Qt.point(-1, -1)
    property real _targetMapSigmaRadiusPx: 0.0
    property string _targetEvidenceFeedback: ""
    property bool _targetEvidenceFeedbackIsError: false
    readonly property bool _targetMapPointInView: _targetMapPoint.x >= 0.0 && _targetMapPoint.y >= 0.0 &&
                                                  _targetMapPoint.x <= width && _targetMapPoint.y <= height

    function _currentTargetRangeM() {
        if (MultiDetectState.rangeTargetId === MultiDetectState.targetId &&
                MultiDetectState.rangeStatusFresh &&
                Number.isFinite(MultiDetectState.rangeSlantM) && MultiDetectState.rangeSlantM >= 0.0) {
            return MultiDetectState.rangeSlantM;
        }
        if (Number.isFinite(MultiDetectState.estimatedRangeM) && MultiDetectState.estimatedRangeM > 0.0)
            return MultiDetectState.estimatedRangeM;
        return NaN;
    }

    function _targetRangeText() {
        const rangeM = _currentTargetRangeM();
        if (!Number.isFinite(rangeM))
            return qsTr("距离：等待合格测距");
        return qsTr("距离：%1 m").arg(rangeM.toFixed(1));
    }

    function _currentTargetBearingDeg() {
        if (MultiDetectState.rangeTargetId === MultiDetectState.targetId &&
                MultiDetectState.rangeStatusFresh &&
                MultiDetectState.rangeBearingAvailable &&
                Number.isFinite(MultiDetectState.rangeRelativeBearingDeg)) {
            return MultiDetectState.rangeRelativeBearingDeg;
        }
        if (MultiDetectState.relativeBearingAvailable &&
                Number.isFinite(MultiDetectState.relativeBearingDeg)) {
            return MultiDetectState.relativeBearingDeg;
        }
        return NaN;
    }

    // A target card may still have a visual/SLAM estimate when the qualified
    // range packet is absent.  Keep that estimate useful for display, but do
    // not persist it as "actual" distance alongside an UNAVAILABLE/INVALID
    // range state.  Snapshots follow the stricter target-event replay schema.
    function _snapshotRangeValidity() {
        if (MultiDetectState.rangeTargetId !== MultiDetectState.targetId)
            return "UNAVAILABLE";
        const validity = String(MultiDetectState.rangeValidity || "").toUpperCase();
        if (validity !== "VALID" && validity !== "DEGRADED")
            return "UNAVAILABLE";
        if (!MultiDetectState.rangeStatusFresh)
            return "UNAVAILABLE";
        if (!Number.isFinite(MultiDetectState.rangeSlantM) || MultiDetectState.rangeSlantM < 0.0)
            return "UNAVAILABLE";
        return validity;
    }

    function _snapshotRangeM() {
        return _snapshotRangeValidity() === "UNAVAILABLE" ? null : MultiDetectState.rangeSlantM;
    }

    function _snapshotBearingDeg() {
        if (_snapshotRangeValidity() === "UNAVAILABLE")
            return null;
        const bearing = _currentTargetBearingDeg();
        return Number.isFinite(bearing) ? bearing : null;
    }

    function _targetBearingText() {
        const bearingDeg = _currentTargetBearingDeg();
        return Number.isFinite(bearingDeg) ?
                    qsTr("相对方位：%1°").arg(bearingDeg.toFixed(1)) :
                    qsTr("相对方位：等待估计");
    }

    function _optionalNumberText(value, precision, suffix) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(precision) + suffix : "--";
    }

    function _targetCoordinateClipboardText() {
        return MultiDetectState.targetLatitudeDeg.toFixed(7) + ", " +
                MultiDetectState.targetLongitudeDeg.toFixed(7);
    }

    function _setTargetEvidenceFeedback(message, isError) {
        _targetEvidenceFeedback = message;
        _targetEvidenceFeedbackIsError = isError;
        targetEvidenceFeedbackTimer.restart();
    }

    function _copyTargetCoordinates() {
        if (!_targetGeolocationQualified) {
            _setTargetEvidenceFeedback(qsTr("不可定位：没有可复制的合格坐标"), true);
            return;
        }
        QGroundControl.copyToClipboard(_targetCoordinateClipboardText());
        _setTargetEvidenceFeedback(qsTr("经纬度已复制到剪贴板"), false);
    }

    function _copySavedEvidenceCoordinates(snapshot) {
        const latitude = Number(snapshot.latitude_deg);
        const longitude = Number(snapshot.longitude_deg);
        if (!Number.isFinite(latitude) || latitude <= -90.0 || latitude >= 90.0 ||
                !Number.isFinite(longitude) || longitude < -180.0 || longitude > 180.0) {
            _setTargetEvidenceFeedback(qsTr("记录没有可复制的有效坐标"), true);
            return;
        }
        QGroundControl.copyToClipboard(latitude.toFixed(7) + ", " + longitude.toFixed(7));
        _setTargetEvidenceFeedback(qsTr("历史记录坐标已复制到剪贴板"), false);
    }

    function _saveTargetEvidenceSnapshot() {
        if (!_targetGeolocationQualified) {
            _setTargetEvidenceFeedback(qsTr("不可定位：没有可保存的合格坐标"), true);
            return;
        }
        if (!_gnssEvidenceQualified) {
            _setTargetEvidenceFeedback(qsTr("不可保存：当前 GNSS 质量证据不合格或已过期"), true);
            return;
        }
        if (typeof multiDetectTargetEvidenceStore === "undefined" ||
                multiDetectTargetEvidenceStore === null) {
            _setTargetEvidenceFeedback(qsTr("本地证据存储不可用"), true);
            return;
        }
        const saved = multiDetectTargetEvidenceStore.saveSnapshot({
            "target_id": MultiDetectState.targetId,
            "target_label": MultiDetectState.targetLabel,
            "source_frame_id": MultiDetectState.targetGeolocationSourceFrameId,
            "latitude_deg": MultiDetectState.targetLatitudeDeg,
            "longitude_deg": MultiDetectState.targetLongitudeDeg,
            "horizontal_sigma_m": MultiDetectState.targetHorizontalSigmaM,
            "range_m": _snapshotRangeM(),
            "relative_bearing_deg": _snapshotBearingDeg(),
            "range_validity": _snapshotRangeValidity(),
            "source_age_ms": MultiDetectState.targetGeolocationSourceAgeMs,
            "atomic_observation": MultiDetectState.rangeAtomicObservation &&
                                  MultiDetectState.targetGeolocationAtomicObservation &&
                                  MultiDetectState.gnssAtomicObservation,
            "producer_epoch": MultiDetectState.rangeProducerEpoch,
            "range_status_sequence": MultiDetectState.rangeSequence,
            "range_sent_at_ms": MultiDetectState.rangeSentAtMs,
            "target_geolocation_sequence": MultiDetectState.targetGeolocationSequence,
            "target_geolocation_sent_at_ms": MultiDetectState.targetGeolocationSentAtMs,
            "gnss_status_sequence": MultiDetectState.gnssSequence,
            "gnss_sent_at_ms": MultiDetectState.gnssSentAtMs,
            "gnss_qualified": MultiDetectState.gnssQualified,
            "gnss_reason": MultiDetectState.gnssReason,
            "gnss_position_healthy": MultiDetectState.gnssPositionHealthy,
            "gnss_fix_type": MultiDetectState.gnssFixType,
            "gnss_satellites_visible": MultiDetectState.gnssSatellitesVisible,
            "gnss_horizontal_accuracy_m": MultiDetectState.gnssHorizontalAccuracyM,
            "gnss_vertical_accuracy_m": MultiDetectState.gnssVerticalAccuracyM,
            "gnss_source_age_ms": MultiDetectState.gnssSourceAgeMs,
            "producer_epoch_health_level": MultiDetectState.targetObservationEpochHealthLevel,
            "producer_epoch_health_reason": MultiDetectState.targetObservationEpochHealthReason,
            "producer_restart_count": MultiDetectState.targetObservationProducerRestartCount,
            "retired_epoch_reject_count": MultiDetectState.targetObservationRetiredEpochRejectCount,
            "epoch_transition_reject_count": MultiDetectState.targetObservationEpochTransitionRejectCount,
            "epoch_changed_at_ms": MultiDetectState.targetObservationEpochChangedAtMs,
            "epoch_last_incident_at_ms": MultiDetectState.targetObservationEpochLastIncidentAtMs
        });
        if (saved) {
            _setTargetEvidenceFeedback(
                qsTr("已保存本地快照 #%1").arg(multiDetectTargetEvidenceStore.snapshotCount), false);
        } else {
            _setTargetEvidenceFeedback(
                qsTr("保存失败：%1").arg(multiDetectTargetEvidenceStore.lastError), true);
        }
    }

    Timer {
        id: targetEvidenceFeedbackTimer

        interval: 3500
        repeat: false
        onTriggered: root._targetEvidenceFeedback = ""
    }

    // Preload local cues. The state singleton emits lock confirmation only
    // after the authoritative target pool reports a primary LCK target.
    SoundEffect {
        id: lockedCue

        property bool playWhenReady: false

        source: "qrc:/Custom/audio/locked.wav"
        volume: 0.92

        onStatusChanged: {
            if (status === SoundEffect.Ready && playWhenReady) {
                playWhenReady = false;
                play();
            }
        }
    }

    // Play a one-shot acknowledgement when Mode 3 execution is accepted.
    SoundEffect {
        id: mode3AimCue

        property bool playWhenReady: false

        source: "qrc:/Custom/audio/mode3-execution.wav"
        volume: 0.78

        onStatusChanged: {
            if (status === SoundEffect.Ready && playWhenReady) {
                playWhenReady = false;
                play();
            }
        }
    }

    // Keep the execution cue audible until the latched execution session ends.
    // It is driven from mode3AimUiActive, which is cleared on explicit cancel,
    // mode change, or pilot-input takeover.
    SoundEffect {
        id: mode3AimLoopCue

        property bool playWhenReady: false

        source: "qrc:/Custom/audio/mode3-execution-loop.wav"
        volume: 0.46
        loops: SoundEffect.Infinite

        onStatusChanged: {
            if (status === SoundEffect.Ready && playWhenReady &&
                    MultiDetectState.mode3AimUiActive) {
                playWhenReady = false;
                play();
            }
        }
    }

    function _syncMode3AimLoopCue() {
        if (MultiDetectState.mode3AimUiActive) {
            if (mode3AimLoopCue.status === SoundEffect.Ready) {
                mode3AimLoopCue.playWhenReady = false;
                mode3AimLoopCue.play();
            } else {
                mode3AimLoopCue.playWhenReady = true;
            }
            return;
        }

        mode3AimLoopCue.playWhenReady = false;
        mode3AimLoopCue.stop();
    }

    function _copyProducerEpochDiagnostics() {
        const diagnostics = MultiDetectState.producerEpochDiagnosticsJson();
        if (diagnostics === "") {
            _setTargetEvidenceFeedback(qsTr("观测生产者诊断不可用"), true);
            return;
        }
        QGroundControl.copyToClipboard(diagnostics);
        _setTargetEvidenceFeedback(
            qsTr("已复制生产者诊断：%1 个事件").arg(
                Math.round(MultiDetectState.targetObservationEpochEventCount)), false);
    }

    function _saveProducerEpochDiagnostics() {
        const savedPath = MultiDetectState.saveProducerEpochDiagnostics();
        if (savedPath === "") {
            const detail = MultiDetectState.operatorLastError !== "" ?
                               ": " + MultiDetectState.operatorLastError : "";
            _setTargetEvidenceFeedback(qsTr("生产者诊断保存失败") + detail, true);
            return;
        }
        _setTargetEvidenceFeedback(qsTr("生产者诊断已保存：%1").arg(savedPath), false);
    }

    function _copyTargetObservationDiagnostics() {
        const diagnostics = MultiDetectState.targetObservationDiagnosticsJson();
        if (diagnostics === "") {
            _setTargetEvidenceFeedback(qsTr("定位诊断不可用"), true);
            return;
        }
        QGroundControl.copyToClipboard(diagnostics);
        _setTargetEvidenceFeedback(
            root._targetGeolocationQualified ? qsTr("已复制定位诊断：当前可定位") :
                                               qsTr("已复制定位诊断：当前不可定位，坐标已省略"),
            false);
    }

    function _saveTargetObservationDiagnostics() {
        const savedPath = MultiDetectState.saveTargetObservationDiagnostics();
        if (savedPath === "") {
            const detail = MultiDetectState.operatorLastError !== "" ?
                               ": " + MultiDetectState.operatorLastError : "";
            _setTargetEvidenceFeedback(qsTr("定位诊断保存失败") + detail, true);
            return;
        }
        const state = root._targetGeolocationQualified ? qsTr("可定位") :
                                                         qsTr("不可定位，坐标已省略");
        _setTargetEvidenceFeedback(qsTr("定位诊断已保存（%1）：%2").arg(state).arg(savedPath), false);
    }

    function _toggleTargetObservationDiagnosticsRecording() {
        if (MultiDetectState.targetObservationRecordingActive) {
            const manifestPath = MultiDetectState.stopTargetObservationDiagnosticsRecording();
            if (manifestPath === "") {
                const detail = MultiDetectState.operatorLastError !== "" ?
                                   ": " + MultiDetectState.operatorLastError : "";
                _setTargetEvidenceFeedback(qsTr("定位诊断录制停止失败") + detail, true);
                return;
            }
            _setTargetEvidenceFeedback(qsTr("定位诊断录制已完成：%1").arg(manifestPath), false);
            return;
        }
        const sessionDirectory = MultiDetectState.startTargetObservationDiagnosticsRecording();
        if (sessionDirectory === "") {
            const detail = MultiDetectState.operatorLastError !== "" ?
                               ": " + MultiDetectState.operatorLastError : "";
            _setTargetEvidenceFeedback(qsTr("定位诊断录制启动失败") + detail, true);
            return;
        }
        _setTargetEvidenceFeedback(qsTr("定位诊断录制已启动：%1").arg(sessionDirectory), false);
    }

    function _localizationReasonText(reason) {
        const translations = {
            "range_status_missing": qsTr("缺少测距状态"),
            "target_geolocation_status_missing": qsTr("缺少目标定位状态"),
            "gnss_status_missing": qsTr("缺少 GNSS 状态"),
            "range_not_qualified": qsTr("测距质量不合格"),
            "target_geolocation_unavailable": qsTr("目标定位不可用"),
            "gnss_not_qualified": qsTr("GNSS 质量不合格"),
            "atomic_observation_required": qsTr("三路状态不是同一原子观测"),
            "atomic_identity_invalid": qsTr("原子观测身份字段无效"),
            "atomic_identity_mismatch": qsTr("epoch / sequence / 发送时间不一致"),
            "target_id_mismatch": qsTr("目标 ID 不一致"),
            "source_frame_id_mismatch": qsTr("源帧 ID 不一致"),
            "range_source_age_invalid": qsTr("测距源年龄无效"),
            "range_source_stale": qsTr("测距源数据过期"),
            "target_geolocation_source_age_invalid": qsTr("目标定位源年龄无效"),
            "target_geolocation_source_stale": qsTr("目标定位源数据过期"),
            "gnss_source_age_invalid": qsTr("GNSS 源年龄无效"),
            "gnss_source_stale": qsTr("GNSS 源数据过期"),
            "range_local_receive_invalid": qsTr("测距本地接收时间无效"),
            "range_local_receive_stale": qsTr("测距本地接收已过期"),
            "target_geolocation_local_receive_invalid": qsTr("目标定位本地接收时间无效"),
            "target_geolocation_local_receive_stale": qsTr("目标定位本地接收已过期"),
            "gnss_local_receive_invalid": qsTr("GNSS 本地接收时间无效"),
            "gnss_local_receive_stale": qsTr("GNSS 本地接收已过期"),
            "coordinates_invalid": qsTr("坐标或 1σ 无效"),
            "diagnostics_json_invalid": qsTr("诊断 JSON 无法解析")
        };
        return translations[reason] || String(reason || qsTr("未知原因"));
    }

    function _wgs84MetersPerDegree(latitudeDeg) {
        if (!Number.isFinite(latitudeDeg) || latitudeDeg <= -90.0 || latitudeDeg >= 90.0)
            return null;
        const latitudeRad = latitudeDeg * Math.PI / 180.0;
        const sineLatitude = Math.sin(latitudeRad);
        const eccentricityTerm = 1.0 - _wgs84EccentricitySquared * sineLatitude * sineLatitude;
        if (!Number.isFinite(eccentricityTerm) || eccentricityTerm <= 0.0)
            return null;
        const denominator = Math.sqrt(eccentricityTerm);
        const primeVerticalM = _wgs84SemiMajorAxisM / denominator;
        const meridionalM = _wgs84SemiMajorAxisM * (1.0 - _wgs84EccentricitySquared) /
                            Math.pow(eccentricityTerm, 1.5);
        const north = Math.PI / 180.0 * meridionalM;
        const east = Math.PI / 180.0 * primeVerticalM * Math.cos(latitudeRad);
        if (!Number.isFinite(north) || !Number.isFinite(east) || north <= 0.0 || east <= 0.0)
            return null;
        return {"north": north, "east": east};
    }

    function _normalizeLongitudeDeg(longitudeDeg) {
        return ((longitudeDeg + 180.0) % 360.0 + 360.0) % 360.0 - 180.0;
    }

    function _updateTargetMapProjection() {
        if (!_targetMapVisible || !_mapControlReady ||
                typeof mapControl.fromCoordinate !== "function") {
            _targetMapPoint = Qt.point(-1, -1);
            _targetMapSigmaRadiusPx = 0.0;
            return;
        }

        const latitude = MultiDetectState.targetLatitudeDeg;
        const longitude = MultiDetectState.targetLongitudeDeg;
        const sigmaM = Number(MultiDetectState.targetHorizontalSigmaM);
        if (!Number.isFinite(latitude) || latitude <= -90.0 || latitude >= 90.0 ||
                !Number.isFinite(longitude) || longitude < -180.0 || longitude > 180.0 ||
                !Number.isFinite(sigmaM) || sigmaM < 0.0) {
            _targetMapPoint = Qt.point(-1, -1);
            _targetMapSigmaRadiusPx = 0.0;
            return;
        }
        const targetCoordinate = QtPositioning.coordinate(latitude, longitude);
        const targetMapPoint = mapControl.fromCoordinate(targetCoordinate, false);
        const targetOverlayPoint = root.mapFromItem(mapControl, targetMapPoint.x, targetMapPoint.y);
        if (!Number.isFinite(targetOverlayPoint.x) || !Number.isFinite(targetOverlayPoint.y)) {
            _targetMapPoint = Qt.point(-1, -1);
            _targetMapSigmaRadiusPx = 0.0;
            return;
        }

        _targetMapPoint = targetOverlayPoint;
        _targetMapSigmaRadiusPx = 0.0;
        if (sigmaM === 0.0)
            return;

        // Convert the reported one-sigma horizontal uncertainty to the current
        // map scale. The circle is a display of uncertainty, not a command or a
        // navigation radius. Use both axes so it remains useful on rotated maps.
        const metersPerDegree = _wgs84MetersPerDegree(latitude);
        if (metersPerDegree === null)
            return;
        const latitudeOffsetDeg = sigmaM / metersPerDegree.north;
        const northLatitude = latitude + latitudeOffsetDeg;
        if (!Number.isFinite(northLatitude) || northLatitude <= -90.0 || northLatitude >= 90.0)
            return;
        const longitudeOffsetDeg = sigmaM / metersPerDegree.east;
        const eastLongitude = _normalizeLongitudeDeg(longitude + longitudeOffsetDeg);
        if (!Number.isFinite(eastLongitude))
            return;
        const northMapPoint = mapControl.fromCoordinate(
                    QtPositioning.coordinate(northLatitude, longitude), false);
        const eastMapPoint = mapControl.fromCoordinate(
                    QtPositioning.coordinate(latitude, eastLongitude), false);
        const northOverlayPoint = root.mapFromItem(mapControl, northMapPoint.x, northMapPoint.y);
        const eastOverlayPoint = root.mapFromItem(mapControl, eastMapPoint.x, eastMapPoint.y);
        if (!Number.isFinite(northOverlayPoint.x) || !Number.isFinite(northOverlayPoint.y) ||
                !Number.isFinite(eastOverlayPoint.x) || !Number.isFinite(eastOverlayPoint.y))
            return;
        _targetMapSigmaRadiusPx = Math.max(
                    0.0,
                    Math.abs(northOverlayPoint.y - targetOverlayPoint.y),
                    Math.abs(eastOverlayPoint.x - targetOverlayPoint.x));
    }

    function _requestExecutionPopup(kind) {
        if (_replaySafetyActive) {
            _pendingExecutionPopup = "";
            MultiDetectState.statusMessage = MultiDetectState.physicalActionIsolationStatusText();
            return;
        }
        if (_pendingExecutionPopup !== "")
            return;

        // A challenge remains valid for its signed lifetime. Reuse it instead
        // of replacing it with another PROMOTE_LCK request on a second click.
        if (kind === "payload" && MultiDetectState.payloadTargetChallengeActive) {
            _openPayloadActionDialog();
            return;
        }
        if (kind === "approach" && MultiDetectState.approachChallengeActive) {
            _openApproachActionDialog();
            return;
        }

        if (!MultiDetectState.refreshLockForExecution())
            return;
        _pendingExecutionPopup = kind;
        _pendingExecutionStartedAtMs = Date.now();
        executionChallengeWaitTimer.restart();
    }

    function _openTargetEvidenceHistoryDialog() {
        if (_targetEvidenceHistoryDialog === null)
            _targetEvidenceHistoryDialog = targetEvidenceHistoryDialogFactory.open();
    }

    function _closeTargetEvidenceHistoryDialog() {
        if (_targetEvidenceHistoryDialog !== null)
            _targetEvidenceHistoryDialog.close();
    }

    function _openProducerEpochDiagnosticsDialog() {
        if (_producerEpochDiagnosticsDialog === null)
            _producerEpochDiagnosticsDialog = producerEpochDiagnosticsDialogFactory.open();
    }

    function _closeProducerEpochDiagnosticsDialog() {
        if (_producerEpochDiagnosticsDialog !== null)
            _producerEpochDiagnosticsDialog.close();
    }

    function _openTargetObservationDiagnosticsDialog() {
        if (_targetObservationDiagnosticsDialog === null)
            _targetObservationDiagnosticsDialog = targetObservationDiagnosticsDialogFactory.open();
    }

    function _closeTargetObservationDiagnosticsDialog() {
        if (_targetObservationDiagnosticsDialog !== null)
            _targetObservationDiagnosticsDialog.close();
    }

    function _openPayloadActionDialog() {
        if (_payloadActionDialog === null)
            _payloadActionDialog = payloadActionDialogFactory.open();
    }

    function _closePayloadActionDialog() {
        if (_payloadActionDialog !== null)
            _payloadActionDialog.close();
    }

    function _openApproachActionDialog() {
        if (_approachActionDialog === null)
            _approachActionDialog = approachActionDialogFactory.open();
    }

    function _closeApproachActionDialog() {
        if (_approachActionDialog !== null)
            _approachActionDialog.close();
    }

    function _openAuthorizationDialog() {
        if (_authorizationDialog === null)
            _authorizationDialog = authorizationDialogFactory.open();
    }

    function _closeAuthorizationDialog() {
        if (_authorizationDialog !== null)
            _authorizationDialog.close();
    }

    Component.onCompleted: {
        _syncMode3AimLoopCue();
        _updateTargetMapProjection();
        if (root._hilUiAutoExercise)
            console.log("MultiDetect HIL UI auto exercise layer ready; configured=" +
                        MultiDetectState.operatorConfigured + ", linkState=" +
                        MultiDetectState.metadataLinkState);
    }

    Timer {
        id: targetMapProjectionTimer

        interval: 150
        repeat: true
        running: root._targetMapVisible

        onTriggered: root._updateTargetMapProjection()
    }

    Timer {
        id: hilUiAutoExerciseTimer

        interval: 100
        repeat: true
        running: root._hilUiAutoExercise

        onTriggered: {
            if (!root._hilUiAutoExercise) {
                stop();
                return;
            }
            if (root._hilUiAutoExerciseStage === 0) {
                if (MultiDetectState.replaySafetyActive)
                    return;
                if (MultiDetectState.missionMode !== "PAYLOAD" &&
                        !MultiDetectState.setMissionMode("PAYLOAD"))
                    return;
                root._hilUiAutoExerciseStage = 1;
                console.log("MultiDetect HIL UI auto exercise stage=1");
                return;
            }
            if (root._hilUiAutoExerciseStage === 1) {
                const metadataLinkReady = MultiDetectState.metadataLinkState === "AUTHENTICATED" ||
                        MultiDetectState.metadataLinkState === "DIRECT_SIGNED_METADATA_READY" ||
                        MultiDetectState.metadataLinkState === "WAITING_FOR_SIGNED_METADATA";
                if (MultiDetectState.operatorConfigured && metadataLinkReady &&
                        !MultiDetectState.hasPendingTargetSelection &&
                        !MultiDetectState.targetBoxValid) {
                    if (MultiDetectState.applySelection(0.30, 0.20, 0.62, 0.72))
                        root._hilUiAutoExerciseStage = 2;
                    if (root._hilUiAutoExerciseStage === 2)
                        console.log("MultiDetect HIL UI auto exercise stage=2");
                }
                return;
            }
            if (root._hilUiAutoExerciseStage === 2 &&
                    MultiDetectState.authorizationChallengeActive &&
                    MultiDetectState.safetyAllowed) {
                if (MultiDetectState.approveAuthorization()) {
                    root._hilUiAutoExerciseStage = 3;
                    console.log("MultiDetect HIL UI auto exercise stage=3");
                    if (root._hilUiAutoExit) {
                        if (root._hilUiAutoExitDelayMs > 0)
                            hilUiAutoExitTimer.start();
                        else
                            Qt.callLater(root._quitForIsolatedHilAcceptance);
                    }
                }
            }
        }
    }

    function _quitForIsolatedHilAcceptance() {
        if (typeof multiDetectHilUiExitController !== "undefined" &&
                multiDetectHilUiExitController !== null) {
            multiDetectHilUiExitController.quitForIsolatedHilAcceptance();
        }
    }

    Timer {
        id: hilUiAutoExitTimer

        interval: Math.max(1, root._hilUiAutoExitDelayMs)
        repeat: false
        onTriggered: root._quitForIsolatedHilAcceptance()
    }

    Timer {
        id: executionChallengeWaitTimer

        interval: 75
        repeat: true

        onTriggered: {
            if (root._replaySafetyActive) {
                root._pendingExecutionPopup = "";
                stop();
                return;
            }
            const elapsedMs = Date.now() - root._pendingExecutionStartedAtMs;
            if (root._pendingExecutionPopup === "payload" &&
                    MultiDetectState.payloadTargetChallengeActive) {
                root._pendingExecutionPopup = "";
                stop();
                root._openPayloadActionDialog();
                return;
            }
            if (root._pendingExecutionPopup === "approach" &&
                    MultiDetectState.approachChallengeActive) {
                root._pendingExecutionPopup = "";
                stop();
                root._openApproachActionDialog();
                return;
            }
            if (elapsedMs < 5000)
                return;
            root._pendingExecutionPopup = "";
            stop();
            MultiDetectState.statusMessage = qsTr("Jetson 未返回新的执行确认");
        }
    }

    Connections {
        target: MultiDetectState

        function onLckCueRequested() {
            if (lockedCue.status === SoundEffect.Ready)
                lockedCue.play();
            else
                lockedCue.playWhenReady = true;
        }

        function onMode3ExecutionCueRequested() {
            if (mode3AimCue.status === SoundEffect.Ready)
                mode3AimCue.play();
            else
                mode3AimCue.playWhenReady = true;
        }

        function onMode3AimUiActiveChanged() {
            root._syncMode3AimLoopCue();
        }

        function onReplaySafetyActiveChanged() {
            if (!MultiDetectState.replaySafetyActive)
                return;
            root._pendingExecutionPopup = "";
            executionChallengeWaitTimer.stop();
            root._closePayloadActionDialog();
            root._closeApproachActionDialog();
            root._closeAuthorizationDialog();
            root._syncMode3AimLoopCue();
        }

        function onTargetGeolocationAvailableChanged() {
            root._updateTargetMapProjection();
        }

        function onTargetGeolocationTargetIdChanged() {
            root._updateTargetMapProjection();
        }

        function onTargetLatitudeDegChanged() {
            root._updateTargetMapProjection();
        }

        function onTargetLongitudeDegChanged() {
            root._updateTargetMapProjection();
        }

        function onTargetHorizontalSigmaMChanged() {
            root._updateTargetMapProjection();
        }

        function onTargetIdChanged() {
            root._updateTargetMapProjection();
        }
    }

    QGCPalette {
        id: qgcPal

        colorGroupEnabled: true
    }

    QGCToolInsets {
        id: toolInsets

        bottomEdgeCenterInset: Math.max(
                                   parentToolInsets.bottomEdgeCenterInset,
                                   actionBar.visible ? root.height - actionBar.y + root._margin : 0)
        bottomEdgeLeftInset: parentToolInsets.bottomEdgeLeftInset
        bottomEdgeRightInset: parentToolInsets.bottomEdgeRightInset
        leftEdgeBottomInset: parentToolInsets.leftEdgeBottomInset
        leftEdgeCenterInset: Math.max(
                                 parentToolInsets.leftEdgeCenterInset,
                                 targetGeolocationCard.visible ?
                                     targetGeolocationCard.x + targetGeolocationCard.width + root._margin : 0)
        leftEdgeTopInset: Math.max(
                              parentToolInsets.leftEdgeTopInset,
                              targetGeolocationCard.visible ?
                                  targetGeolocationCard.x + targetGeolocationCard.width + root._margin : 0)
        rightEdgeBottomInset: parentToolInsets.rightEdgeBottomInset
        rightEdgeCenterInset: parentToolInsets.rightEdgeCenterInset
        rightEdgeTopInset: parentToolInsets.rightEdgeTopInset
        topEdgeCenterInset: Math.max(
                                parentToolInsets.topEdgeCenterInset,
                                mode3AimBanner.visible ?
                                    mode3AimBanner.y + mode3AimBanner.height + root._margin : 0)
        topEdgeLeftInset: Math.max(
                              parentToolInsets.topEdgeLeftInset,
                              targetGeolocationCard.visible ?
                                  targetGeolocationCard.y + targetGeolocationCard.height + root._margin : 0)
        topEdgeRightInset: parentToolInsets.topEdgeRightInset
    }

    // The custom layer shares the Fly View coordinate space with mapControl.
    // Project the qualified WGS84 target coordinate into that space rather than
    // creating a mission item, guided action, or editable map object.
    Item {
        id: targetMapIndicator

        height: Math.max(18, ScreenTools.defaultFontPixelHeight * 1.2)
        visible: root._targetMapVisible && root._targetMapPointInView
        width: height
        x: root._targetMapPoint.x - width * 0.5
        y: root._targetMapPoint.y - height * 0.5
        z: 15

        Rectangle {
            id: targetUncertaintyRing

            anchors.centerIn: parent
            border.color: qgcPal.warningText
            border.width: Math.max(1, ScreenTools.defaultFontPixelWidth * 0.16)
            color: Qt.transparent
            height: Math.max(2, root._targetMapSigmaRadiusPx * 2.0)
            radius: width * 0.5
            visible: root._targetMapSigmaRadiusPx > 0.0
            width: height
        }

        Rectangle {
            anchors.centerIn: parent
            border.color: qgcPal.text
            border.width: 1
            color: qgcPal.colorRed
            height: parent.height
            radius: width * 0.5
            width: height
        }

        QGCLabel {
            anchors.bottom: parent.top
            anchors.bottomMargin: ScreenTools.defaultFontPixelHeight * 0.25
            anchors.horizontalCenter: parent.horizontalCenter
            color: qgcPal.warningText
            font.bold: true
            text: qsTr("目标 ±%1 m (1σ)").arg(MultiDetectState.targetHorizontalSigmaM.toFixed(1))
        }
    }

    Rectangle {
        id: targetGeolocationCard

        anchors.left: parent.left
        anchors.leftMargin: root._margin
        anchors.top: parent.top
        anchors.topMargin: parentToolInsets.topEdgeLeftInset + root._margin
        border.color: root._targetGeolocationQualified ? qgcPal.colorGreen : qgcPal.warningText
        border.width: 1
        color: qgcPal.window
        height: targetGeolocationColumn.implicitHeight + root._margin * 1.4
        opacity: 0.92
        radius: ScreenTools.defaultBorderRadius
        visible: root._targetMapContextVisible
        width: Math.min(root.width * 0.34, ScreenTools.defaultFontPixelWidth * 43)
        z: 20

        ColumnLayout {
            id: targetGeolocationColumn

            anchors.fill: parent
            anchors.margins: root._margin * 0.7
            spacing: root._margin * 0.25

            QGCLabel {
                Layout.fillWidth: true
                color: root._targetGeolocationQualified ? qgcPal.colorGreen : qgcPal.warningText
                font.bold: true
                text: root._targetGeolocationQualified ? qsTr("目标落图 · 只读") :
                                                        qsTr("目标落图 · 不可定位")
            }

            QGCLabel {
                Layout.fillWidth: true
                color: root._targetGeolocationQualified ? qgcPal.text : qgcPal.warningText
                font.bold: true
                text: MultiDetectState.targetGeolocationText()
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("类别：") +
                      (MultiDetectState.targetLabel !== "" ? MultiDetectState.targetLabel : "--")
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                text: root._targetRangeText() + " · " + root._targetBearingText()
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("更新时间：%1 ms 前 · 源帧 %2")
                      .arg(MultiDetectState.targetGeolocationSourceAgeMs)
                      .arg(MultiDetectState.targetGeolocationSourceFrameId !== "" ?
                               MultiDetectState.targetGeolocationSourceFrameId : "--")
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root._margin * 0.35

                QGCButton {
                    Layout.fillWidth: true
                    enabled: root._targetGeolocationQualified
                    text: qsTr("复制坐标")
                    onClicked: root._copyTargetCoordinates()
                }

                QGCButton {
                    Layout.fillWidth: true
                    enabled: root._targetGeolocationQualified &&
                             root._gnssEvidenceQualified &&
                             typeof multiDetectTargetEvidenceStore !== "undefined" &&
                             multiDetectTargetEvidenceStore !== null
                    text: qsTr("保存快照")
                    onClicked: root._saveTargetEvidenceSnapshot()
                }

                QGCButton {
                    enabled: typeof multiDetectTargetEvidenceStore !== "undefined" &&
                             multiDetectTargetEvidenceStore !== null &&
                             multiDetectTargetEvidenceStore.snapshotCount > 0
                    text: qsTr("记录")
                    onClicked: root._openTargetEvidenceHistoryDialog()
                }

                QGCButton {
                    text: qsTr("诊断事件")
                    onClicked: root._openProducerEpochDiagnosticsDialog()
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root._margin * 0.35

                QGCButton {
                    Layout.fillWidth: true
                    text: qsTr("复制诊断")
                    onClicked: root._copyProducerEpochDiagnostics()
                }

                QGCButton {
                    Layout.fillWidth: true
                    text: qsTr("保存诊断")
                    onClicked: root._saveProducerEpochDiagnostics()
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root._margin * 0.35

                QGCButton {
                    Layout.fillWidth: true
                    text: qsTr("复制定位诊断")
                    onClicked: root._copyTargetObservationDiagnostics()
                }

                QGCButton {
                    Layout.fillWidth: true
                    text: qsTr("查看定位诊断")
                    onClicked: root._openTargetObservationDiagnosticsDialog()
                }

                QGCButton {
                    Layout.fillWidth: true
                    text: qsTr("保存定位诊断")
                    onClicked: root._saveTargetObservationDiagnostics()
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root._margin * 0.35

                QGCButton {
                    Layout.fillWidth: true
                    text: MultiDetectState.targetObservationRecordingActive ?
                              qsTr("停止定位诊断录制") : qsTr("开始定位诊断录制")
                    onClicked: root._toggleTargetObservationDiagnosticsRecording()
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: MultiDetectState.targetObservationRecordingActive ?
                               qgcPal.colorGreen : qgcPal.text
                    text: MultiDetectState.targetObservationRecordingActive ?
                              qsTr("录制中 · %1 个样本 · 1 Hz").arg(
                                  Math.round(MultiDetectState.targetObservationRecordingSampleCount)) :
                              qsTr("连续诊断未录制")
                    wrapMode: Text.WordWrap
                }
            }

            QGCLabel {
                Layout.fillWidth: true
                color: root._targetEvidenceFeedbackIsError ? qgcPal.warningText : qgcPal.colorGreen
                text: root._targetEvidenceFeedback
                visible: text !== ""
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                color: qgcPal.warningText
                text: qsTr("仅显示与记录；不产生飞控指令")
                wrapMode: Text.WordWrap
            }
        }
    }

    QGCPopupDialogFactory {
        id: targetObservationDiagnosticsDialogFactory

        dialogComponent: targetObservationDiagnosticsDialogComponent
    }

    Component {
        id: targetObservationDiagnosticsDialogComponent

        QGCPopupDialog {
            id: targetObservationDiagnosticsDialog

            property var snapshot: ({})
            readonly property var readiness: snapshot.readiness || ({})
            readonly property var rangeStatus: snapshot.range_status || ({})
            readonly property var geolocationStatus: snapshot.target_geolocation_status || ({})
            readonly property var gnssStatus: snapshot.gnss_status || ({})
            readonly property var localReceive: snapshot.local_receive || ({})
            readonly property var reasons: Array.isArray(readiness.reasons) ? readiness.reasons : []

            function valueOrDash(value) {
                return value === undefined || value === null || value === "" ? "--" : String(value);
            }

            function receiveAge(channel) {
                const entry = localReceive[channel] || ({});
                return entry.age_ms === undefined || entry.age_ms === null ? "--" :
                       qsTr("%1 ms").arg(Math.round(Number(entry.age_ms)));
            }

            buttons: Dialog.Close
            title: qsTr("目标定位诊断 · 只读")

            Component.onCompleted: {
                const payload = MultiDetectState.targetObservationDiagnosticsJson();
                if (payload !== "") {
                    try {
                        snapshot = JSON.parse(payload);
                    } catch (error) {
                        snapshot = ({
                            "readiness": {
                                "locatable": false,
                                "reasons": ["diagnostics_json_invalid"]
                            }
                        });
                    }
                }
            }
            Component.onDestruction: root._targetObservationDiagnosticsDialog = null

            ScrollView {
                height: Math.min(targetObservationDiagnosticsDialog.maxContentAvailableHeight,
                                 ScreenTools.defaultFontPixelHeight * 29)
                width: Math.min(targetObservationDiagnosticsDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 64)

                ColumnLayout {
                    spacing: root._margin * 0.38
                    width: parent.width

                    QGCLabel {
                        Layout.fillWidth: true
                        color: targetObservationDiagnosticsDialog.readiness.locatable === true ?
                                   qgcPal.colorGreen : qgcPal.warningText
                        font.bold: true
                        font.pointSize: ScreenTools.mediumFontPointSize
                        text: targetObservationDiagnosticsDialog.readiness.locatable === true ?
                                  qsTr("可定位 · 坐标已纳入快照") :
                                  qsTr("不可定位 · 坐标已强制省略")
                        wrapMode: Text.WordWrap
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        text: targetObservationDiagnosticsDialog.reasons.length === 0 ? qsTr("失败原因：无") :
                              qsTr("失败原因：%1").arg(
                                  targetObservationDiagnosticsDialog.reasons.map(
                                      reason => root._localizationReasonText(reason)).join("；"))
                        wrapMode: Text.WordWrap
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        color: qgcPal.windowShade
                        implicitHeight: identityColumn.implicitHeight + root._margin
                        radius: ScreenTools.defaultBorderRadius

                        ColumnLayout {
                            id: identityColumn
                            anchors.fill: parent
                            anchors.margins: root._margin * 0.5

                            QGCLabel {
                                font.bold: true
                                text: qsTr("原子观测身份")
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("匹配：%1 · epoch %2 · sequence %3 · sentAt %4")
                                      .arg(targetObservationDiagnosticsDialog.readiness.atomic_identity_matched === true ?
                                               qsTr("是") : qsTr("否"))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.rangeStatus.producerEpoch))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.rangeStatus.sequence))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.rangeStatus.sentAtMs))
                                wrapMode: Text.WordWrap
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("目标 %1 · 源帧 %2 · 最大年龄 %3 ms")
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.rangeStatus.targetId))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.rangeStatus.sourceFrameId))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.snapshot.maximum_age_ms))
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        font.bold: true
                        text: qsTr("测距")
                    }
                    QGCLabel {
                        Layout.fillWidth: true
                        text: qsTr("质量 %1 · 斜距 %2 m · 地距 %3 m · 方位 %4° · 源年龄 %5 ms · 接收年龄 %6")
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.rangeStatus.validity))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.rangeStatus.slantRangeM))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.rangeStatus.groundRangeM))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.rangeStatus.absoluteBearingDeg))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.rangeStatus.sourceAgeMs))
                              .arg(targetObservationDiagnosticsDialog.receiveAge("range"))
                        wrapMode: Text.WordWrap
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        font.bold: true
                        text: qsTr("GNSS")
                    }
                    QGCLabel {
                        Layout.fillWidth: true
                        text: qsTr("合格 %1 · 原因 %2 · fix %3 · 卫星 %4 · HAcc %5 m · VAcc %6 m · 源年龄 %7 ms · 接收年龄 %8")
                              .arg(targetObservationDiagnosticsDialog.gnssStatus.qualified === true ? qsTr("是") : qsTr("否"))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.gnssStatus.reason))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.gnssStatus.fixType))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.gnssStatus.satellitesVisible))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.gnssStatus.horizontalAccuracyM))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.gnssStatus.verticalAccuracyM))
                              .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.gnssStatus.sourceAgeMs))
                              .arg(targetObservationDiagnosticsDialog.receiveAge("gnss"))
                        wrapMode: Text.WordWrap
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        font.bold: true
                        text: qsTr("目标坐标")
                    }
                    QGCLabel {
                        Layout.fillWidth: true
                        text: targetObservationDiagnosticsDialog.readiness.coordinates_included === true ?
                                  qsTr("纬度 %1 · 经度 %2 · 1σ %3 m · 接收年龄 %4")
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.geolocationStatus.latitudeDeg))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.geolocationStatus.longitudeDeg))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(targetObservationDiagnosticsDialog.geolocationStatus.horizontalSigmaM))
                                      .arg(targetObservationDiagnosticsDialog.receiveAge("target_geolocation")) :
                                  qsTr("定位门未全部通过；本诊断不含经纬度或 1σ")
                        wrapMode: Text.WordWrap
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignRight

                        QGCButton {
                            text: qsTr("复制 JSON")
                            onClicked: root._copyTargetObservationDiagnostics()
                        }
                        QGCButton {
                            text: qsTr("保存 JSON")
                            onClicked: root._saveTargetObservationDiagnostics()
                        }
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        color: qgcPal.warningText
                        text: qsTr("诊断只用于定位排错与证据记录；不会发送飞控指令或启用物理释放")
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }

    QGCPopupDialogFactory {
        id: producerEpochDiagnosticsDialogFactory

        dialogComponent: producerEpochDiagnosticsDialogComponent
    }

    Component {
        id: producerEpochDiagnosticsDialogComponent

        QGCPopupDialog {
            id: producerEpochDiagnosticsDialog

            buttons: Dialog.Close
            title: qsTr("生产者 epoch 诊断 · 只读")

            Component.onDestruction: root._producerEpochDiagnosticsDialog = null

            ColumnLayout {
                height: Math.min(producerEpochDiagnosticsDialog.maxContentAvailableHeight,
                                 ScreenTools.defaultFontPixelHeight * 27)
                spacing: root._margin * 0.35
                width: Math.min(producerEpochDiagnosticsDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 58)

                QGCLabel {
                    Layout.fillWidth: true
                    font.bold: true
                    text: qsTr("%1 · epoch %2 · 事件 %3 · 丢弃 %4").arg(
                              MultiDetectState.targetObservationEpochHealthLevel).arg(
                              Math.round(MultiDetectState.targetObservationProducerEpoch)).arg(
                              Math.round(MultiDetectState.targetObservationEpochEventCount)).arg(
                              Math.round(MultiDetectState.targetObservationEpochDroppedEventCount))
                    wrapMode: Text.WordWrap
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.text
                    text: qsTr("原因：%1 · 最新事件优先 · 最多保留 64 条").arg(
                              MultiDetectState.targetObservationEpochHealthReason)
                    wrapMode: Text.WordWrap
                }

                ListView {
                    id: producerEpochDiagnosticsList

                    Layout.fillHeight: true
                    Layout.fillWidth: true
                    clip: true
                    model: MultiDetectState.targetObservationEpochEvents.length
                    spacing: root._margin * 0.35

                    delegate: Rectangle {
                        required property int index
                        readonly property var entry: MultiDetectState.targetObservationEpochEvents[
                                                         MultiDetectState.targetObservationEpochEvents.length -
                                                         index - 1]

                        border.color: qgcPal.button
                        border.width: 1
                        color: qgcPal.windowShade
                        height: producerEpochEventColumn.implicitHeight + root._margin
                        radius: ScreenTools.defaultBorderRadius
                        width: producerEpochDiagnosticsList.width

                        ColumnLayout {
                            id: producerEpochEventColumn

                            anchors.fill: parent
                            anchors.margins: root._margin * 0.5
                            spacing: root._margin * 0.16

                            QGCLabel {
                                Layout.fillWidth: true
                                font.bold: true
                                text: qsTr("#%1 · %2 · %3").arg(
                                          Math.round(parent.parent.entry.event_index)).arg(
                                          String(parent.parent.entry.kind || "--")).arg(
                                          Qt.formatDateTime(
                                              new Date(Number(parent.parent.entry.occurred_at_ms)),
                                              "yyyy-MM-dd hh:mm:ss.zzz"))
                                wrapMode: Text.WordWrap
                            }

                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("epoch：%1 → %2 · 当前 %3").arg(
                                          Math.round(parent.parent.entry.previous_epoch)).arg(
                                          Math.round(parent.parent.entry.incoming_epoch)).arg(
                                          Math.round(parent.parent.entry.active_epoch))
                                wrapMode: Text.WordWrap
                            }

                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("%1/%2 · 重启 %3 · 旧包拒绝 %4 · 切换拒绝 %5").arg(
                                          String(parent.parent.entry.health_level || "--")).arg(
                                          String(parent.parent.entry.health_reason || "--")).arg(
                                          Math.round(parent.parent.entry.producer_restart_count)).arg(
                                          Math.round(parent.parent.entry.retired_epoch_reject_count)).arg(
                                          Math.round(parent.parent.entry.epoch_transition_reject_count))
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    QGCLabel {
                        anchors.centerIn: parent
                        color: qgcPal.text
                        text: qsTr("尚无生产者 epoch 事件")
                        visible: producerEpochDiagnosticsList.count === 0
                    }
                }

                RowLayout {
                    Layout.alignment: Qt.AlignRight
                    spacing: root._margin * 0.35

                    QGCButton {
                        text: qsTr("复制 JSON")
                        onClicked: root._copyProducerEpochDiagnostics()
                    }

                    QGCButton {
                        text: qsTr("保存 JSON")
                        onClicked: root._saveProducerEpochDiagnostics()
                    }
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.warningText
                    text: qsTr("诊断只用于态势核对；不会发送飞控指令或启用物理释放")
                    wrapMode: Text.WordWrap
                }
            }
        }
    }

    QGCPopupDialogFactory {
        id: targetEvidenceHistoryDialogFactory

        dialogComponent: targetEvidenceHistoryDialogComponent
    }

    Component {
        id: targetEvidenceHistoryDialogComponent

        QGCPopupDialog {
            id: targetEvidenceHistoryDialog

            buttons: Dialog.Close
            title: qsTr("目标事件记录 · 只读")

            Component.onDestruction: root._targetEvidenceHistoryDialog = null

            ColumnLayout {
                height: Math.min(targetEvidenceHistoryDialog.maxContentAvailableHeight,
                                 ScreenTools.defaultFontPixelHeight * 25)
                spacing: root._margin * 0.35
                width: Math.min(targetEvidenceHistoryDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 50)

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.text
                    text: qsTr("最近 %1 条 · 本地保存").arg(
                              typeof multiDetectTargetEvidenceStore !== "undefined" &&
                              multiDetectTargetEvidenceStore !== null ?
                                  multiDetectTargetEvidenceStore.recentSnapshots.length : 0)
                }

                ListView {
                    id: targetEvidenceHistoryList

                    Layout.fillHeight: true
                    Layout.fillWidth: true
                    clip: true
                    model: typeof multiDetectTargetEvidenceStore !== "undefined" &&
                           multiDetectTargetEvidenceStore !== null ?
                               multiDetectTargetEvidenceStore.recentSnapshots : []
                    spacing: root._margin * 0.35

                    delegate: Rectangle {
                        required property var modelData

                        readonly property var entry: modelData
                        border.color: qgcPal.button
                        border.width: 1
                        color: qgcPal.windowShade
                        height: evidenceRecordColumn.implicitHeight + root._margin
                        radius: ScreenTools.defaultBorderRadius
                        width: targetEvidenceHistoryList.width

                        ColumnLayout {
                            id: evidenceRecordColumn

                            anchors.fill: parent
                            anchors.margins: root._margin * 0.5
                            spacing: root._margin * 0.16

                            QGCLabel {
                                Layout.fillWidth: true
                                font.bold: true
                                text: qsTr("%1 · %2").arg(
                                          String(parent.parent.entry.target_label || "--"),
                                          String(parent.parent.entry.saved_at_utc || "--"))
                                wrapMode: Text.WordWrap
                            }

                            QGCLabel {
                                Layout.fillWidth: true
                                text: "GPS " + root._optionalNumberText(parent.parent.entry.latitude_deg, 7, "") + ", " +
                                      root._optionalNumberText(parent.parent.entry.longitude_deg, 7, "") + " ± " +
                                      root._optionalNumberText(parent.parent.entry.horizontal_sigma_m, 1, " m") + " (1σ)"
                                wrapMode: Text.WordWrap
                            }

                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("距离：%1 · 方位：%2 · %3").arg(
                                          root._optionalNumberText(parent.parent.entry.range_m, 1, " m"),
                                          root._optionalNumberText(parent.parent.entry.relative_bearing_deg, 1, "°"),
                                          String(parent.parent.entry.range_validity || "--"))
                                wrapMode: Text.WordWrap
                            }

                            QGCLabel {
                                Layout.fillWidth: true
                                text: parent.parent.entry.schema === "multidetect.target-evidence.v6" ?
                                          qsTr("GNSS：v6 epoch %1 · %2/%3 · 重启 %4 · 旧包拒绝 %5 · 切换拒绝 %6").arg(
                                              root._optionalNumberText(parent.parent.entry.producer_epoch, 0, ""),
                                              String(parent.parent.entry.producer_epoch_health_level || "--"),
                                              String(parent.parent.entry.producer_epoch_health_reason || "--"),
                                              root._optionalNumberText(parent.parent.entry.producer_restart_count, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.retired_epoch_reject_count, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.epoch_transition_reject_count, 0, "")) :
                                      parent.parent.entry.schema === "multidetect.target-evidence.v5" ?
                                          qsTr("GNSS：代际绑定原子观测 · epoch %1 · fix %2 · 星 %3 · H/V %4/%5 m · age %6 ms · seq %7").arg(
                                              root._optionalNumberText(parent.parent.entry.producer_epoch, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_fix_type, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_satellites_visible, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_horizontal_accuracy_m, 1, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_vertical_accuracy_m, 1, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_source_age_ms, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_status_sequence, 0, "")) :
                                      parent.parent.entry.schema === "multidetect.target-evidence.v4" ?
                                          qsTr("GNSS：旧版 v4 单包原子观测，无 producer epoch 绑定") :
                                      parent.parent.entry.schema === "multidetect.target-evidence.v3" ?
                                          qsTr("GNSS：旧版 v3 同生产帧三消息绑定") :
                                      parent.parent.entry.schema === "multidetect.target-evidence.v2" ?
                                          qsTr("GNSS：旧版 v2 同期质量记录，无原子 envelope 绑定") :
                                          qsTr("GNSS：旧版 v1 记录，无独立质量证据")
                                wrapMode: Text.WordWrap
                            }

                            QGCButton {
                                Layout.alignment: Qt.AlignRight
                                text: qsTr("复制坐标")
                                onClicked: root._copySavedEvidenceCoordinates(parent.parent.entry)
                            }
                        }
                    }
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.warningText
                    text: qsTr("记录只用于态势回看与核对；不会发送飞控指令")
                    wrapMode: Text.WordWrap
                }
            }
        }
    }

    // Keep target operations as native QGCButton controls without wrapping
    // them in a second, custom-styled HUD card. The Fly View data overlays
    // remain spatial, while operational chrome inherits QGC's own controls.
    Item {
        id: actionBar

        anchors.bottom: parent.bottom
        anchors.bottomMargin: parentToolInsets.bottomEdgeCenterInset + root._margin
        anchors.horizontalCenter: parent.horizontalCenter
        height: actionRow.implicitHeight
        width: actionRow.implicitWidth
        z: 20

        RowLayout {
            id: actionRow

            anchors.centerIn: parent
            spacing: root._margin * 0.5

            QGCButton {
                checkable: true
                checked: MultiDetectState.selectionMode
                enabled: MultiDetectState.operatorConfigured &&
                         MultiDetectState.interactionState !== "LCK" &&
                         MultiDetectState.interactionState !== "TGT" &&
                         MultiDetectState.exclusiveLockPendingTargetId === ""
                text: MultiDetectState.selectionMode ? qsTr("在视频中拖动…") : qsTr("框选目标")

                onClicked: MultiDetectState.selectionMode ? MultiDetectState.cancelTarget() : MultiDetectState.beginSelection()
            }

            QGCButton {
                enabled: MultiDetectState.selectionMode ||
                         (MultiDetectState.targetBoxValid && !MultiDetectState.hasPendingTargetSelection)
                text: MultiDetectState.mode3AimUiActive ? qsTr("取消瞄准") :
                      (MultiDetectState.interactionState === "LCK" ||
                       MultiDetectState.interactionState === "TGT") ? qsTr("返回 TRK") : qsTr("取消")

                onClicked: MultiDetectState.mode3AimUiActive ? MultiDetectState.cancelMode3Aim() :
                           (MultiDetectState.interactionState === "LCK" ||
                            MultiDetectState.interactionState === "TGT") ? MultiDetectState.demoteTrack() : MultiDetectState.cancelTarget()
            }

            QGCButton {
                enabled: MultiDetectState.operatorConfigured && MultiDetectState.executionActionable &&
                         !MultiDetectState.replaySafetyActive &&
                         root._pendingExecutionPopup === ""
                primary: true
                text: qsTr("执行")
                visible: MultiDetectState.missionMode === "PAYLOAD"

                onClicked: {
                    root._requestExecutionPopup("payload");
                }
            }

            QGCButton {
                enabled: MultiDetectState.operatorConfigured && MultiDetectState.executionActionable &&
                         !MultiDetectState.replaySafetyActive &&
                         root._pendingExecutionPopup === ""
                primary: true
                text: qsTr("执行")
                visible: MultiDetectState.missionMode === "OBSERVE" &&
                         !MultiDetectState.mode3AimUiActive

                onClicked: {
                    root._requestExecutionPopup("approach");
                }
            }
        }
    }

    Rectangle {
        id: mode3AimBanner

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: parentToolInsets.topEdgeCenterInset + root._margin
        border.color: qgcPal.warningText
        border.width: 2
        color: qgcPal.windowShadeDark
        height: mode3AimBannerContent.implicitHeight + root._margin * 1.5
        opacity: 0.96
        radius: ScreenTools.defaultBorderRadius
        scale: 1.0
        visible: MultiDetectState.mode3AimUiActive || MultiDetectState.approachPilotInputCancelled
        width: Math.min(root.width - root._margin * 2,
                        Math.max(ScreenTools.defaultFontPixelWidth * 58, root.width * 0.72))
        z: 100

        RowLayout {
            id: mode3AimBannerContent

            anchors.fill: parent
            anchors.margins: root._margin * 0.75
            spacing: root._margin

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.text
                    font.bold: true
                    font.pointSize: ScreenTools.largeFontPointSize
                    text: MultiDetectState.mode3AimBannerTitleText()
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.text
                    font.bold: true
                    font.pointSize: ScreenTools.defaultFontPointSize
                    text: MultiDetectState.mode3AimBannerDetailText()
                }
            }

            QGCButton {
                Layout.minimumHeight: ScreenTools.defaultFontPixelHeight * 2.7
                Layout.minimumWidth: ScreenTools.defaultFontPixelWidth * 18
                font.bold: true
                font.pointSize: ScreenTools.largeFontPointSize
                primary: true
                text: qsTr("立即取消瞄准")
                visible: MultiDetectState.mode3AimUiActive

                onClicked: MultiDetectState.cancelMode3Aim()
            }
        }
    }

    QGCPopupDialogFactory {
        id: payloadActionDialogFactory

        dialogComponent: payloadActionDialogComponent
    }

    Component {
        id: payloadActionDialogComponent

        QGCPopupDialog {
            id: payloadActionDialog

            acceptButtonEnabled: !MultiDetectState.replaySafetyActive &&
                                 MultiDetectState.vehicleArmed &&
                                 MultiDetectState.payloadTargetChallengeActive &&
                                 root._targetActionable
            buttons: ScreenTools.isMobile ? Dialog.Cancel : (Dialog.Ok | Dialog.Cancel)
            title: qsTr("模式 2 · 执行")

            Component.onDestruction: root._payloadActionDialog = null

            onAccepted: {
                if (!MultiDetectState.confirmPayloadTargetSlide(600, 1.0, true))
                    preventClose = true;
            }

            ColumnLayout {
                spacing: root._margin
                width: Math.min(payloadActionDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 62)

                QGCLabel {
                    Layout.fillWidth: true
                    text: qsTr("当前目标：") + (MultiDetectState.targetId === "" ?
                              qsTr("等待 Jetson 确认") :
                              MultiDetectState.targetLabel + " · " + MultiDetectState.targetId)
                    wrapMode: Text.WordWrap
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: MultiDetectState.payloadTargetChallengeActive ?
                               qgcPal.warningText : qgcPal.text
                    font.bold: true
                    text: MultiDetectState.replaySafetyActive ?
                              MultiDetectState.physicalActionIsolationStatusText() :
                          !MultiDetectState.vehicleArmed ? qsTr("请先 ARM") :
                          MultiDetectState.payloadTargetChallengeActive ?
                              (ScreenTools.isMobile ? qsTr("滑动确认") : qsTr("确认执行")) +
                              " · " + MultiDetectState.payloadTargetChallengeExpiresInS + " s" :
                              MultiDetectState.payloadTargetEligibility
                    wrapMode: Text.WordWrap
                }

                QGCSlider {
                    id: payloadTargetSlide

                    property bool continuousEvidence: false
                    property double pressedAtMs: 0
                    property real previousValue: 0.0

                    Layout.fillWidth: true
                    enabled: !MultiDetectState.replaySafetyActive &&
                             MultiDetectState.vehicleArmed &&
                             MultiDetectState.payloadTargetChallengeActive
                    from: 0.0
                    live: true
                    to: 1.0
                    value: 0.0
                    visible: ScreenTools.isMobile

                    onMoved: {
                        if (value + 0.02 < previousValue)
                            continuousEvidence = false;
                        previousValue = value;
                    }
                    onPressedChanged: {
                        if (pressed) {
                            pressedAtMs = Date.now();
                            previousValue = value;
                            continuousEvidence = value <= 0.02;
                            return;
                        }
                        const completion = value;
                        const durationMs = Math.max(0, Math.round(Date.now() - pressedAtMs));
                        const valid = continuousEvidence && completion >= 0.98 && durationMs >= 600;
                        if (completion >= 0.98)
                            MultiDetectState.confirmPayloadTargetSlide(durationMs, completion, valid);
                        value = 0.0;
                        previousValue = 0.0;
                        continuousEvidence = false;
                    }
                }

                QGCLabel {
                    Layout.fillWidth: true
                    text: qsTr("手动投放：") + MultiDetectState.rcReleaseState +
                          (MultiDetectState.rcReleaseChannel > 0 ?
                               qsTr(" · 通道 %1").arg(MultiDetectState.rcReleaseChannel) : "")
                    wrapMode: Text.WordWrap
                }

                QGCButton {
                    Layout.alignment: Qt.AlignRight
                    enabled: !MultiDetectState.replaySafetyActive &&
                             MultiDetectState.safetyAllowed &&
                             MultiDetectState.authorizationChallengeActive
                    primary: true
                    text: qsTr("进入任务授权")
                    visible: MultiDetectState.authorizationChallengeActive

                    onClicked: {
                        root._closePayloadActionDialog();
                        Qt.callLater(root._openAuthorizationDialog);
                    }
                }
            }
        }
    }

    QGCPopupDialogFactory {
        id: approachActionDialogFactory

        dialogComponent: approachActionDialogComponent
    }

    Component {
        id: approachActionDialogComponent

        QGCPopupDialog {
            id: approachActionDialog

            acceptButtonEnabled: !MultiDetectState.replaySafetyActive &&
                                 MultiDetectState.vehicleArmed &&
                                 MultiDetectState.approachChallengeActive &&
                                 root._targetActionable
            buttons: ScreenTools.isMobile ? Dialog.Cancel : (Dialog.Ok | Dialog.Cancel)
            title: qsTr("载荷模块 · 执行")

            Component.onDestruction: root._approachActionDialog = null

            onAccepted: {
                if (!MultiDetectState.confirmApproachSlide(600, 1.0, true))
                    preventClose = true;
            }

            ColumnLayout {
                spacing: root._margin
                width: Math.min(approachActionDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 62)

                QGCLabel {
                    Layout.fillWidth: true
                    text: qsTr("当前目标：") + (MultiDetectState.targetId === "" ?
                              qsTr("等待 Jetson 确认") :
                              MultiDetectState.targetLabel + " · " + MultiDetectState.targetId)
                    wrapMode: Text.WordWrap
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: MultiDetectState.approachChallengeActive ?
                               qgcPal.warningText : qgcPal.text
                    font.bold: true
                    text: MultiDetectState.approachChallengePromptText(root._targetActionable,
                                                                       ScreenTools.isMobile)
                    wrapMode: Text.WordWrap
                }

                QGCSlider {
                    id: approachSlide

                    property bool continuousEvidence: false
                    property double pressedAtMs: 0
                    property real previousValue: 0.0

                    Layout.fillWidth: true
                    enabled: !MultiDetectState.replaySafetyActive &&
                             MultiDetectState.vehicleArmed &&
                             MultiDetectState.approachChallengeActive &&
                             root._targetActionable
                    from: 0.0
                    live: true
                    to: 1.0
                    value: 0.0
                    visible: ScreenTools.isMobile

                    onMoved: {
                        if (value + 0.02 < previousValue)
                            continuousEvidence = false;
                        previousValue = value;
                    }
                    onPressedChanged: {
                        if (pressed) {
                            pressedAtMs = Date.now();
                            previousValue = value;
                            continuousEvidence = value <= 0.02;
                            return;
                        }
                        const completion = value;
                        const durationMs = Math.max(0, Math.round(Date.now() - pressedAtMs));
                        const valid = continuousEvidence && completion >= 0.98 && durationMs >= 600;
                        if (completion >= 0.98)
                            MultiDetectState.confirmApproachSlide(durationMs, completion, valid);
                        value = 0.0;
                        previousValue = 0.0;
                        continuousEvidence = false;
                    }
                }
            }
        }
    }

    QGCPopupDialogFactory {
        id: authorizationDialogFactory

        dialogComponent: authorizationDialogComponent
    }

    Component {
        id: authorizationDialogComponent

        QGCPopupDialog {
            id: authorizationDialog

            acceptButtonEnabled: !MultiDetectState.replaySafetyActive &&
                                 MultiDetectState.safetyAllowed &&
                                 MultiDetectState.authorizationChallengeActive
            buttons: Dialog.Yes | Dialog.No
            rejectButtonEnabled: !MultiDetectState.replaySafetyActive
            title: qsTr("任务授权")

            Component.onDestruction: root._authorizationDialog = null

            onAccepted: {
                if (!MultiDetectState.approveAuthorization())
                    preventClose = true;
            }
            onRejected: {
                if (!MultiDetectState.denyAuthorization())
                    preventClose = true;
            }

            ColumnLayout {
                spacing: root._margin
                width: Math.min(authorizationDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 58)

                QGCLabel {
                    Layout.fillWidth: true
                    text: qsTr("目标绑定：") + MultiDetectState.authorizationBinding
                    wrapMode: Text.WordWrap
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: MultiDetectState.safetyAllowed ? qgcPal.colorGreen : qgcPal.warningText
                    text: qsTr("安全规则：") + MultiDetectState.safetyPassCount +
                          qsTr(" 通过，") + MultiDetectState.safetyDenyCount +
                          qsTr(" 拒绝，") + MultiDetectState.safetyUnknownCount + qsTr(" 未知")
                    wrapMode: Text.WordWrap
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.warningText
                    font.bold: true
                    text: qsTr("授权只返回 Jetson 任务状态机；物理投放由遥控器开关请求，当前输出仍锁定。")
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
}

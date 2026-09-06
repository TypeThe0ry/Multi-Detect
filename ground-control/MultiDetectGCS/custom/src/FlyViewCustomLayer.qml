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
            "gnss_status_sequence": MultiDetectState.gnssSequence,
            "gnss_sent_at_ms": MultiDetectState.gnssSentAtMs,
            "gnss_qualified": MultiDetectState.gnssQualified,
            "gnss_reason": MultiDetectState.gnssReason,
            "gnss_position_healthy": MultiDetectState.gnssPositionHealthy,
            "gnss_fix_type": MultiDetectState.gnssFixType,
            "gnss_satellites_visible": MultiDetectState.gnssSatellitesVisible,
            "gnss_horizontal_accuracy_m": MultiDetectState.gnssHorizontalAccuracyM,
            "gnss_vertical_accuracy_m": MultiDetectState.gnssVerticalAccuracyM,
            "gnss_source_age_ms": MultiDetectState.gnssSourceAgeMs
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
                                text: parent.parent.entry.schema === "multidetect.target-evidence.v2" ?
                                          qsTr("GNSS：fix %1 · 星 %2 · H/V %3/%4 m · age %5 ms · seq %6").arg(
                                              root._optionalNumberText(parent.parent.entry.gnss_fix_type, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_satellites_visible, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_horizontal_accuracy_m, 1, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_vertical_accuracy_m, 1, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_source_age_ms, 0, ""),
                                              root._optionalNumberText(parent.parent.entry.gnss_status_sequence, 0, "")) :
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

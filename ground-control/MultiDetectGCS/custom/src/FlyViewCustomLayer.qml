import Custom.MultiDetect

import QGroundControl
import QGroundControl.Controls
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia
import QtPositioning
import QtLocation
import "qrc:/Custom/qml" as MultiDetectPanels

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
    property string _missionOriginLatitude: ""
    property string _missionOriginLongitude: ""
    property var parentToolInsets
    property var totalToolInsets: toolInsets
    property string _pendingExecutionPopup: ""
    property double _pendingExecutionStartedAtMs: 0
    property var _targetEvidenceHistoryDialog: null
    property var _targetEventReplayDialog: null
    // Replay preview is presentation-only. It never mutates MultiDetectState
    // and therefore cannot become a live selected target or a Mission input.
    property var _replayMapPreviewEvent: null
    property point _replayMapPoint: Qt.point(-1, -1)
    property real _replayMapSigmaRadiusPx: 0.0
    property var _producerEpochDiagnosticsDialog: null
    property var _targetObservationDiagnosticsDialog: null
    property var _payloadActionDialog: null
    property var _approachActionDialog: null
    property var _authorizationDialog: null
    // Mission monitoring is intentionally rendered on the Fly view.  The
    // downlink schema is forward compatible, so these helpers tolerate both
    // nested and legacy flat fields while keeping the card useful when a
    // newer onboard sender adds fields.
    readonly property var _missionController: typeof multiDetectMission !== "undefined" ? multiDetectMission : null
    readonly property var _missionTelemetry: _missionController ? (_missionController.telemetry || ({})) : ({})
    readonly property var _missionLogs: _missionController ? (_missionController.logEntries || []) : []
    readonly property var _missionPlan: _missionController ? (_missionController.plan || {}) : ({})
    readonly property var _missionFindings: _missionController ? (_missionController.findings || []) : []
    readonly property var _missionAlerts: _missionController ? (_missionController.alerts || []) : []
    // Keep the monitor visible before the first authenticated packet so the
    // operator always has an explicit waiting state.
    readonly property bool _missionCardVisible: true
    property bool _missionCardCollapsed: false
    property int _missionPolicyCountdown: 0
    readonly property string _missionPlanOriginText: {
        const t = _missionTelemetry || {};
        const p = root._missionPlan || t.plan || t.plan_preview || {};
        const o = p.origin || t.origin || {};
        const lat = o.latitude_deg !== undefined ? o.latitude_deg : o.lat;
        const lon = o.longitude_deg !== undefined ? o.longitude_deg : o.lon;
        return Number.isFinite(Number(lat)) && Number.isFinite(Number(lon)) ?
                   Number(lat).toFixed(7) + "," + Number(lon).toFixed(7) : "";
    }
    function _missionField(names, fallback) {
        const t = _missionTelemetry || {};
        for (let i = 0; i < names.length; ++i) {
            const path = names[i].split(".");
            let v = t;
            for (let j = 0; j < path.length && v !== null && v !== undefined; ++j)
                v = v[path[j]];
            if (v !== undefined && v !== null)
                return v;
        }
        return fallback;
    }
    function _missionCount(value) {
        if (value === undefined || value === null) return "0";
        const lower = value.lower_bound === true || value.lowerBound === true;
        const n = value.count !== undefined ? value.count : value.total !== undefined ? value.total : value;
        return (lower ? qsTr("至少 ") : "") + String(n);
    }
    function _missionStateText(state) {
        const labels = {"awaiting_approval": qsTr("等待审批"), "running": qsTr("执行中"),
            "finding": qsTr("发现目标"), "alert": qsTr("告警"), "completed": qsTr("已完成"),
            "returning": qsTr("返航中"), "return_home": qsTr("返航"), "return_now": qsTr("立即返航"),
            "recovery_finished": qsTr("返航完成"), "stopped": qsTr("已停止")};
        return labels[String(state)] || String(state || qsTr("未知"));
    }
    function _missionEntryText(entry, kind) {
        const e = entry || {};
        const label = e.label || e.type || e.id || qsTr("未命名");
        const location = e.n !== undefined && e.e !== undefined ? qsTr(" · 北 %1 / 东 %2 米").arg(e.n).arg(e.e) : "";
        const note = e.note || e.reason || e.message || "";
        return qsTr("%1：%2%3%4").arg(kind).arg(String(label)).arg(location).arg(note ? qsTr(" · %1").arg(note) : "");
    }
    function _missionLinkText() {
        const fresh = _missionController && _missionController.telemetryFresh;
        const timeout = Number(_missionField(["timeout_s", "link_loss.timeout_s"], 0));
        return fresh ? qsTr("链路正常") : qsTr("链路中断 · %1 秒后%2").arg(Math.max(0, _missionPolicyCountdown || timeout)).arg(
            String(_missionField(["link_loss_action", "policy_action", "action"], "保守返航")));
    }
    function _missionRefreshCountdown() {
        const timeout = Number(_missionField(["timeout_s", "link_loss.timeout_s"], 0));
        _missionPolicyCountdown = Number.isFinite(timeout) ? Math.ceil(timeout) : 0;
    }
    function _missionPoint(entry) {
        const e = entry || {};
        const c = e.coordinate || e.gps || e.location || {};
        let lat = Number(c.latitude_deg), lon = Number(c.longitude_deg);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
            const n = Number(c.north_m !== undefined ? c.north_m : (e.north_m !== undefined ? e.north_m : e.n));
            const east = Number(c.east_m !== undefined ? c.east_m : (e.east_m !== undefined ? e.east_m : e.e));
            const o = (_missionPlan || {}).origin || _missionTelemetry.origin || {};
            const olat = Number(o.latitude_deg !== undefined ? o.latitude_deg : o.lat), olon = Number(o.longitude_deg !== undefined ? o.longitude_deg : o.lon);
            if (!Number.isFinite(n) || !Number.isFinite(east) || !Number.isFinite(olat) || !Number.isFinite(olon)) return null;
            lat = olat + n / 111320.0; lon = olon + east / (111320.0 * Math.cos(olat * Math.PI / 180));
        }
        if (!mapControl || !mapControl.fromCoordinate) return null;
        const p = mapControl.fromCoordinate(QtPositioning.coordinate(lat, lon), false);
        if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return null;
        return root.mapFromItem(mapControl, p.x, p.y);
    }
    function _missionCoordinate(n, e) {
        const o = (_missionPlan || {}).origin || _missionTelemetry.origin || {};
        const lat = Number(o.latitude_deg !== undefined ? o.latitude_deg : o.lat);
        const lon = Number(o.longitude_deg !== undefined ? o.longitude_deg : o.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
        return QtPositioning.coordinate(lat + Number(n) / 111320.0,
                                        lon + Number(e) / (111320.0 * Math.cos(lat * Math.PI / 180)));
    }
    function _hasSummaryPages() {
        return _missionController !== null &&
               (_missionController.finalSummaryComplete || _missionController.summaryPagesTotal > 0);
    }
    // Only an assembled summary is shown: pages are never displayed alone.
    function _missionSummaryLines() {
        if (!_missionController || !_missionController.finalSummaryComplete) return [];
        const s = _missionController.finalSummary || {};
        const lines = [];
        lines.push(qsTr("结束状态：%1").arg(root._missionStateText(s.status || "--")));
        if (s.terminal_action)
            lines.push(qsTr("终止动作：%1（%2）").arg(root._missionStateText(s.terminal_action)).arg(String(s.terminal_reason || "")));
        const counts = s.counts || {};
        const lower = s.count_lower_bounds || {};
        for (const key in counts) {
            const n = counts[key];
            lines.push(lower[key] === true ? qsTr("计数 %1：至少 %2").arg(key).arg(n) : qsTr("计数 %1：%2").arg(key).arg(n));
        }
        lines.push(qsTr("发现 %1 个，报警 %2 个").arg((s.findings || []).length).arg((s.alerts || []).length));
        lines.push(qsTr("步骤记录 %1 条").arg((s.history || []).length));
        return lines;
    }
    property bool _missionPlanSnapshotSent: false
    property bool _missionSummarySnapshotSent: false
    Connections {
        target: root._missionController
        function onTelemetryChanged() {
            if (!root._missionController) return
            if (root._missionPlan.requires_approval === true && !root._missionPlanSnapshotSent) {
                root._missionPlanSnapshotSent = true
                if (typeof multiDetectUiAutoApprovePlan !== "undefined" && multiDetectUiAutoApprovePlan === true)
                    missionApprovalDialog.open()
                if (typeof multiDetectUiSnapshotController !== "undefined") multiDetectUiSnapshotController.requestSnapshot("plan-dialog")
                if (typeof multiDetectUiAutoApprovePlan !== "undefined" && multiDetectUiAutoApprovePlan === true)
                    autoApprovePlanTimer.restart()
            }
            if (root._missionController.finalSummaryComplete && !root._missionSummarySnapshotSent) {
                root._missionSummarySnapshotSent = true
                if (typeof multiDetectUiAutoApprovePlan !== "undefined" && multiDetectUiAutoApprovePlan === true)
                    missionSummaryDialog.open()
                if (typeof multiDetectUiSnapshotController !== "undefined") multiDetectUiSnapshotController.requestSnapshot("final-summary-complete")
            }
        }
    }

    Timer {
        id: autoApprovePlanTimer
        interval: 1000
        repeat: false
        onTriggered: {
            if (typeof multiDetectUiAutoApprovePlan !== "undefined" && multiDetectUiAutoApprovePlan === true &&
                    root._missionController && root._missionController.planAwaitingDecision && !root._missionController.busy) {
                root._missionController.approvePlan()
                missionApprovalDialog.close()
            }
        }
    }
    // Keep the map-only uncertainty projection on the same WGS-84 ellipsoid
    // as the Python geolocation backend.  These values are used solely to
    // convert metres to the two local degree offsets needed by QGC's map
    // projection; the backend's Vincenty result remains authoritative.
    readonly property real _wgs84SemiMajorAxisM: 6378137.0
    readonly property real _wgs84EccentricitySquared: 0.0066943799901413165
    readonly property bool _targetGeolocationQualified: MultiDetectState.targetGeolocationAvailable &&
                                                       MultiDetectState.targetGeolocationSourceFresh &&
                                                       _gnssEvidenceQualified &&
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
    readonly property bool _replayMapPreviewQualified:
        _replayMapPreviewEvent !== null &&
        _mapControlReady &&
        mapControl.pipState.state === mapControl.pipState.fullState &&
        typeof multiDetectTargetEventReplayStore !== "undefined" &&
        multiDetectTargetEventReplayStore !== null &&
        multiDetectTargetEventReplayStore.loaded &&
        multiDetectTargetEventReplayStore.integrityVerified
    readonly property bool _replayMapPreviewPointInView:
        _replayMapPoint.x >= 0.0 && _replayMapPoint.y >= 0.0 &&
        _replayMapPoint.x <= width && _replayMapPoint.y <= height
    readonly property var _recordingQualitySummary: MultiDetectState.targetObservationRecordingQualitySummary || ({})
    readonly property var _recordingCurrentStatus: _recordingQualitySummary.current_status || ({})
    readonly property bool _recordingQualityHasSamples:
        Number(_recordingQualitySummary.sample_count) > 0

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

    function _recordingQualityLevelText(level) {
        if (level === "HEALTHY")
            return qsTr("良好");
        if (level === "WARNING")
            return qsTr("警告");
        if (level === "CRITICAL")
            return qsTr("严重");
        return qsTr("等待样本");
    }

    function _recordingQualityColor(level) {
        if (!_recordingQualityHasSamples)
            return qgcPal.text;
        if (level === "HEALTHY")
            return qgcPal.colorGreen;
        if (level === "CRITICAL")
            return qgcPal.colorRed;
        return qgcPal.warningText;
    }

    function _recordingQualityReasonText(reason) {
        const translations = {
            "target_not_locatable": qsTr("当前不可定位"),
            "range_invalid": qsTr("测距异常"),
            "gnss_unqualified": qsTr("GNSS 不合格"),
            "latest_sampling_gap": qsTr("最近采样断档")
        };
        return translations[String(reason || "")] || String(reason || qsTr("质量异常"));
    }

    function _recordingQualityText() {
        const summary = _recordingQualitySummary || ({});
        const status = summary.current_status || ({});
        const level = String(status.level || "WAITING");
        const sampleCount = Number(summary.sample_count);
        const prefix = summary.active === true ? qsTr("定位质量") : qsTr("上次定位质量");
        if (!Number.isFinite(sampleCount) || sampleCount <= 0)
            return qsTr("%1：等待样本").arg(prefix);

        const rate = Number(summary.locatable_rate);
        const rateText = Number.isFinite(rate) ?
                             (Math.max(0.0, Math.min(1.0, rate)) * 100.0).toFixed(0) : "--";
        let text = qsTr("%1：%2 · 定位率 %3%").arg(prefix)
                    .arg(_recordingQualityLevelText(level)).arg(rateText);
        const currentRun = Number(status.current_non_locatable_run);
        if (Number.isFinite(currentRun) && currentRun > 0)
            text += qsTr(" · 不可定位连续 %1").arg(Math.round(currentRun));
        const latestGap = Number(status.latest_gap_ms);
        if (Number.isFinite(latestGap) && latestGap > 0 && status.latest_gap_violation === true)
            text += qsTr(" · 断档 %1 ms").arg(Math.round(latestGap));
        const reasons = Array.isArray(status.reasons) ? status.reasons : [];
        if (reasons.length > 0)
            text += qsTr(" · %1").arg(_recordingQualityReasonText(reasons[0]));
        return text;
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

    function _projectMapCoordinate(latitude, longitude, sigmaM) {
        if (!_mapControlReady || typeof mapControl.fromCoordinate !== "function")
            return null;
        if (!Number.isFinite(latitude) || latitude <= -90.0 || latitude >= 90.0 ||
                !Number.isFinite(longitude) || longitude < -180.0 || longitude > 180.0 ||
                !Number.isFinite(sigmaM) || sigmaM < 0.0)
            return null;
        const targetCoordinate = QtPositioning.coordinate(latitude, longitude);
        const targetMapPoint = mapControl.fromCoordinate(targetCoordinate, false);
        if (targetMapPoint === null || typeof targetMapPoint === "undefined" ||
                !Number.isFinite(targetMapPoint.x) || !Number.isFinite(targetMapPoint.y))
            return null;
        const targetOverlayPoint = root.mapFromItem(mapControl, targetMapPoint.x, targetMapPoint.y);
        if (!Number.isFinite(targetOverlayPoint.x) || !Number.isFinite(targetOverlayPoint.y))
            return null;
        let sigmaRadiusPx = 0.0;
        if (sigmaM === 0.0)
            return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};

        // Convert the reported one-sigma horizontal uncertainty to the current
        // map scale. The circle is a display of uncertainty, not a command or a
        // navigation radius. Use both axes so it remains useful on rotated maps.
        const metersPerDegree = _wgs84MetersPerDegree(latitude);
        if (metersPerDegree === null)
            return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};
        const latitudeOffsetDeg = sigmaM / metersPerDegree.north;
        const northLatitude = latitude + latitudeOffsetDeg;
        if (!Number.isFinite(northLatitude) || northLatitude <= -90.0 || northLatitude >= 90.0)
            return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};
        const longitudeOffsetDeg = sigmaM / metersPerDegree.east;
        const eastLongitude = _normalizeLongitudeDeg(longitude + longitudeOffsetDeg);
        if (!Number.isFinite(eastLongitude))
            return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};
        const northMapPoint = mapControl.fromCoordinate(
                    QtPositioning.coordinate(northLatitude, longitude), false);
        const eastMapPoint = mapControl.fromCoordinate(
                    QtPositioning.coordinate(latitude, eastLongitude), false);
        if (northMapPoint === null || typeof northMapPoint === "undefined" ||
                eastMapPoint === null || typeof eastMapPoint === "undefined" ||
                !Number.isFinite(northMapPoint.x) || !Number.isFinite(northMapPoint.y) ||
                !Number.isFinite(eastMapPoint.x) || !Number.isFinite(eastMapPoint.y))
            return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};
        const northOverlayPoint = root.mapFromItem(mapControl, northMapPoint.x, northMapPoint.y);
        const eastOverlayPoint = root.mapFromItem(mapControl, eastMapPoint.x, eastMapPoint.y);
        if (!Number.isFinite(northOverlayPoint.x) || !Number.isFinite(northOverlayPoint.y) ||
                !Number.isFinite(eastOverlayPoint.x) || !Number.isFinite(eastOverlayPoint.y))
            return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};
        sigmaRadiusPx = Math.max(
                    0.0,
                    Math.abs(northOverlayPoint.y - targetOverlayPoint.y),
                    Math.abs(eastOverlayPoint.x - targetOverlayPoint.x));
        return {"point": targetOverlayPoint, "radiusPx": sigmaRadiusPx};
    }

    function _updateTargetMapProjection() {
        const projection = _targetMapVisible ?
                               _projectMapCoordinate(
                                   MultiDetectState.targetLatitudeDeg,
                                   MultiDetectState.targetLongitudeDeg,
                                   Number(MultiDetectState.targetHorizontalSigmaM)) : null;
        if (projection === null) {
            _targetMapPoint = Qt.point(-1, -1);
            _targetMapSigmaRadiusPx = 0.0;
            return;
        }
        _targetMapPoint = projection.point;
        _targetMapSigmaRadiusPx = projection.radiusPx;
    }

    function _updateReplayMapProjection() {
        const event = _replayMapPreviewEvent;
        const gps = event !== null ? (event.gps || ({})) : ({});
        const projection = _replayMapPreviewQualified && gps.coordinates_included === true ?
                               _projectMapCoordinate(
                                   Number(gps.latitude_deg),
                                   Number(gps.longitude_deg),
                                   Number(gps.horizontal_sigma_m)) : null;
        if (projection === null) {
            _replayMapPoint = Qt.point(-1, -1);
            _replayMapSigmaRadiusPx = 0.0;
            return;
        }
        _replayMapPoint = projection.point;
        _replayMapSigmaRadiusPx = projection.radiusPx;
    }

    function _clearReplayMapPreview() {
        _replayMapPreviewEvent = null;
        _replayMapPoint = Qt.point(-1, -1);
        _replayMapSigmaRadiusPx = 0.0;
    }

    function _previewReplayEvent(event) {
        _clearReplayMapPreview();
        if (event === null || event === undefined) {
            _setTargetEvidenceFeedback(qsTr("回放事件不可用"), true);
            return;
        }
        const gps = event.gps || ({});
        if (gps.coordinates_included !== true ||
                !Number.isFinite(Number(gps.latitude_deg)) ||
                Number(gps.latitude_deg) <= -90.0 || Number(gps.latitude_deg) >= 90.0 ||
                !Number.isFinite(Number(gps.longitude_deg)) ||
                Number(gps.longitude_deg) < -180.0 || Number(gps.longitude_deg) > 180.0 ||
                !Number.isFinite(Number(gps.horizontal_sigma_m)) ||
                Number(gps.horizontal_sigma_m) < 0.0) {
            _setTargetEvidenceFeedback(qsTr("回放事件不可定位：没有可预览的合格坐标"), true);
            return;
        }
        _replayMapPreviewEvent = event;
        _updateReplayMapProjection();
        _setTargetEvidenceFeedback(
            qsTr("回放事件已做只读落图预览：%1").arg(event.event_id || "--"), false);
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

    function _openTargetEventReplayDialog() {
        if (_targetEventReplayDialog === null)
            _targetEventReplayDialog = targetEventReplayDialogFactory.open();
    }

    function _closeTargetEventReplayDialog() {
        if (_targetEventReplayDialog !== null)
            _targetEventReplayDialog.close();
    }

    function _chooseTargetEventReplayManifest() {
        if (typeof multiDetectTargetEventReplayStore === "undefined" ||
                multiDetectTargetEventReplayStore === null) {
            _setTargetEvidenceFeedback(qsTr("目标事件回放校验器不可用"), true);
            return;
        }
        targetEventReplayManifestFileDialog.openForLoad();
    }

    function _loadTargetEventReplayManifest(path) {
        if (typeof multiDetectTargetEventReplayStore === "undefined" ||
                multiDetectTargetEventReplayStore === null) {
            _setTargetEvidenceFeedback(qsTr("目标事件回放校验器不可用"), true);
            return;
        }
        _clearReplayMapPreview();
        if (multiDetectTargetEventReplayStore.loadManifest(path)) {
            _setTargetEvidenceFeedback(
                qsTr("回放清单已验证：%1 个事件").arg(
                    multiDetectTargetEventReplayStore.eventCount), false);
            _openTargetEventReplayDialog();
            return;
        }
        const detail = multiDetectTargetEventReplayStore.lastError !== "" ?
                           ": " + multiDetectTargetEventReplayStore.lastError : "";
        _setTargetEvidenceFeedback(qsTr("回放清单校验失败") + detail, true);
    }

    function _openOrChooseTargetEventReplay() {
        if (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                multiDetectTargetEventReplayStore !== null &&
                multiDetectTargetEventReplayStore.loaded) {
            _openTargetEventReplayDialog();
            return;
        }
        _chooseTargetEventReplayManifest();
    }

    function _clearTargetEventReplay() {
        _clearReplayMapPreview();
        if (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                multiDetectTargetEventReplayStore !== null) {
            multiDetectTargetEventReplayStore.clear();
        }
        _setTargetEvidenceFeedback(qsTr("已清除回放查看器"), false);
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
        running: root._targetMapVisible || root._replayMapPreviewQualified

        onTriggered: {
            root._updateTargetMapProjection();
            root._updateReplayMapProjection();
        }
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

    Timer {
        id: missionCountdownTimer
        interval: 1000
        repeat: true
        running: root._missionCardVisible
        onTriggered: {
            if (root._missionController && !root._missionController.telemetryFresh && root._missionPolicyCountdown > 0)
                root._missionPolicyCountdown -= 1;
        }
    }

    Rectangle {
        id: missionMonitorCard
        anchors.right: parent.right
        anchors.rightMargin: root._margin
        anchors.top: parent.top
        anchors.topMargin: parentToolInsets.topEdgeRightInset + root._margin
        width: Math.min(root.width * 0.36, ScreenTools.defaultFontPixelWidth * 49)
        height: root._missionCardCollapsed ? collapseRow.implicitHeight + root._margin : monitorColumn.implicitHeight + root._margin * 1.2
        visible: root._missionCardVisible
        z: 30
        radius: ScreenTools.defaultBorderRadius
        color: qgcPal.window
        border.color: root._missionController && root._missionController.telemetryFresh ? qgcPal.colorGreen : qgcPal.warningText
        border.width: 1

        ColumnLayout {
            id: monitorColumn
            anchors.fill: parent
            anchors.margins: root._margin * 0.65
            spacing: root._margin * 0.22
            RowLayout {
                id: collapseRow
                Layout.fillWidth: true
                QGCLabel { Layout.fillWidth: true; font.bold: true; text: qsTr("任务监控 · %1").arg(root._missionController ? root._missionController.activeMissionId : qsTr("等待任务")) }
                QGCButton { text: root._missionCardCollapsed ? qsTr("展开") : qsTr("收起"); onClicked: root._missionCardCollapsed = !root._missionCardCollapsed }
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: !root._missionCardCollapsed
                QGCLabel {
                    Layout.fillWidth: true
                    text: qsTr("状态：%1 · 步骤 %2/%3 · %4\n查询：%5")
                        .arg(root._missionStateText(root._missionField(["state", "mission_state", "status"], "未知")))
                        .arg(root._missionField(["step.index", "current_step", "step_index"], "--"))
                        .arg(root._missionField(["step.total", "step_count", "total_steps"], "--"))
                        .arg(String(root._missionField(["step.skill", "skill"], "--")))
                        .arg(String(root._missionField(["query", "task_text"], "--")))
                    wrapMode: Text.WordWrap
                }
                ProgressBar {
                    Layout.fillWidth: true
                    from: 0; to: 1
                    value: Number(root._missionField(["progress", "step.progress"], 0))
                }
                QGCLabel {
                    Layout.fillWidth: true
                    text: qsTr("原语：%1 · 预算：%2\n剩余：%3 秒 · %4 瓦时\n发现：%5 · 告警：%6")
                        .arg(root._missionField(["primitives_used", "usage.primitives"], "--"))
                        .arg(root._missionField(["usage_budget", "step.budget"], "--"))
                        .arg(root._missionField(["remaining_time_s"], "--"))
                        .arg(root._missionField(["remaining_energy_wh"], "--"))
                        .arg(root._missionCount(root._missionField(["findings_total", "findings", "counts.findings", "finding_count"], root._missionFindings.length)))
                        .arg(root._missionCount(root._missionField(["alerts_total", "alerts", "counts.alerts", "alert_count"], root._missionAlerts.length)))
                    wrapMode: Text.WordWrap
                }
                QGCLabel {
                    Layout.fillWidth: true
                    color: root._missionController && root._missionController.telemetryFresh ? qgcPal.colorGreen : qgcPal.warningText
                    text: qsTr("%1\n最近身份决策：%2").arg(root._missionLinkText()).arg(String(root._missionField(["identity_decision", "last_identity_decision", "decision"], "--")))
                    wrapMode: Text.WordWrap
                }
                QGCLabel {
                    Layout.fillWidth: true
                    visible: root._missionFindings.length > 0 || root._missionAlerts.length > 0
                    text: root._missionFindings.map(e => root._missionEntryText(e, qsTr("发现"))).concat(
                              root._missionAlerts.map(e => root._missionEntryText(e, qsTr("告警")))).join("\n")
                    wrapMode: Text.WordWrap
                }
                RowLayout {
                    Layout.fillWidth: true
                    QGCButton { text: qsTr("中止"); enabled: root._missionController && root._missionController.activeMissionId !== ""; onClicked: root._missionController.abortMission() }
                    QGCButton { text: qsTr("暂停"); enabled: root._missionController && root._missionController.telemetryFresh; onClicked: root._missionController.pauseMission() }
                    QGCButton { text: qsTr("恢复"); enabled: root._missionController && root._missionController.telemetryFresh; onClicked: root._missionController.resumeMission() }
                }
                QGCLabel {
                    Layout.fillWidth: true
                    visible: root._missionController !== null && root._missionController.onboardCompiling
                    text: qsTr("机载正在编译任务包，等待最终结论…")
                    wrapMode: Text.WordWrap
                }
                QGCButton {
                    Layout.fillWidth: true
                    visible: root._missionPlan.requires_approval === true || root._missionField(["requires_approval", "plan.requires_approval"], false) === true
                    text: root._missionController && root._missionController.planAwaitingDecision ? qsTr("查看并审批计划（待批准）") : qsTr("查看计划审批状态")
                    onClicked: { missionApprovalDialog.open(); if (typeof multiDetectUiSnapshotController !== "undefined") multiDetectUiSnapshotController.requestSnapshot("plan-dialog") }
                }
                QGCButton {
                    Layout.fillWidth: true
                    visible: root._hasSummaryPages()
                    text: qsTr("查看任务最终摘要")
                    onClicked: { missionSummaryDialog.open(); if (typeof multiDetectUiSnapshotController !== "undefined") multiDetectUiSnapshotController.requestSnapshot("final-summary") }
                }
            }
        }
    }

    QGCPopupDialog {
        id: missionApprovalDialog
        title: qsTr("任务计划审批")
        buttons: Dialog.Close
        ColumnLayout {
            width: Math.min(root.width * 0.55, ScreenTools.defaultFontPixelWidth * 70)
            QGCLabel { Layout.fillWidth: true; text: qsTr("任务 %1 请求执行以下计划：").arg(String(root._missionPlan.mission_id || "--")); wrapMode: Text.WordWrap }
            QGCLabel {
                Layout.fillWidth: true
                visible: String(root._missionPlan.task_text || "") !== ""
                text: qsTr("任务描述：%1").arg(String(root._missionPlan.task_text || ""))
                wrapMode: Text.WordWrap
            }
            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("计划摘要 SHA-256：%1…").arg(String(root._missionPlan.plan_sha256 || "--").substring(0, 16))
                font.family: ScreenTools.fixedFontFamily
            }
            Repeater {
                    model: root._missionPlan.steps || root._missionField(["plan.steps", "steps"], [])
                delegate: QGCLabel {
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true
                    text: qsTr("%1. %2 · 参数：%3 · 区域：%4").arg(index + 1).arg(modelData.skill || "--").arg(JSON.stringify(modelData.params || {})).arg(JSON.stringify(modelData.region || {}))
                    wrapMode: Text.WordWrap
                }
            }
            RowLayout {
                Layout.fillWidth: true
                QGCButton {
                    text: qsTr("批准")
                    enabled: root._missionController && root._missionController.planAwaitingDecision && !root._missionController.busy
                    onClicked: { root._missionController.approvePlan(); missionApprovalDialog.close(); }
                }
                QGCButton {
                    text: qsTr("拒绝")
                    enabled: root._missionController && root._missionController.planAwaitingDecision && !root._missionController.busy
                    onClicked: { root._missionController.rejectPlan(); missionApprovalDialog.close(); }
                }
            }
            QGCLabel {
                Layout.fillWidth: true
                text: root._missionController ? String(root._missionController.planDecisionState || "") : ""
                visible: text !== ""
                wrapMode: Text.WordWrap
            }
        }
    }

    QGCPopupDialog {
        id: missionSummaryDialog
        title: qsTr("任务最终摘要")
        buttons: Dialog.Close
        ColumnLayout {
            width: Math.min(root.width * 0.62, ScreenTools.defaultFontPixelWidth * 78)
            QGCLabel { Layout.fillWidth: true; text: qsTr("完整结果（下界计数显示‘至少 N’）："); font.bold: true }
            Repeater {
                model: root._missionSummaryLines()
                delegate: QGCLabel {
                    required property var modelData
                    Layout.fillWidth: true
                    text: String(modelData)
                    wrapMode: Text.WordWrap
                }
            }
            QGCLabel {
                Layout.fillWidth: true
                color: qgcPal.warningText
                text: qsTr("摘要不完整：已收到 %1 / %2 页，等待其余分页").arg(root._missionController ? root._missionController.summaryPagesReceived : 0).arg(root._missionController ? root._missionController.summaryPagesTotal : 0)
                visible: root._missionController && !root._missionController.finalSummaryComplete
            }
            QGCLabel {
                Layout.fillWidth: true
                visible: root._missionController && root._missionController.finalSummaryComplete
                text: {
                    const controller = root._missionController;
                    const s = controller && controller.finalSummary ? controller.finalSummary : {};
                    const findings = (s.findings || []).map(e => root._missionEntryText(e, qsTr("发现")));
                    const alerts = (s.alerts || []).map(e => root._missionEntryText(e, qsTr("告警")));
                    return findings.concat(alerts).join("\n");
                }
                wrapMode: Text.WordWrap
            }
        }
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

    Repeater {
        id: missionFindingMarkers
        model: root._missionFindings.concat(root._missionAlerts)
        delegate: Item {
            required property var modelData
            readonly property var projected: root._missionPoint(modelData)
            visible: projected !== null
            x: projected ? projected.x - width / 2 : -100
            y: projected ? projected.y - height / 2 : -100
            width: ScreenTools.defaultFontPixelHeight * 1.15
            height: width
            z: 25
            Rectangle { anchors.fill: parent; radius: width / 2; color: modelData.alert === true ? qgcPal.colorRed : qgcPal.colorOrange; border.color: qgcPal.text; border.width: 1 }
            QGCLabel { anchors.bottom: parent.top; anchors.horizontalCenter: parent.horizontalCenter; color: qgcPal.text; text: String(modelData.label || modelData.finding_type || qsTr("发现")) + " · " + (modelData.confidence !== undefined ? Number(modelData.confidence).toFixed(2) : "--"); font.bold: true }
        }
    }

    // Plan geometry uses the same local N/E projection as findings.  These
    // lightweight items remain in the Fly layer and therefore follow map panning.
    Repeater {
        model: root._missionPlan.zones || []
        delegate: Item {
            required property var modelData
            readonly property var point: root._missionPoint({north_m: modelData.n, east_m: modelData.e})
            visible: point !== null
            width: Math.max(8, Number(modelData.r || modelData.radius_m || 1) * 2)
            height: width
            x: point ? point.x - width / 2 : -100
            y: point ? point.y - height / 2 : -100
            z: 20
            Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.color: qgcPal.colorGreen; border.width: 2 }
        }
    }
    Repeater {
        model: root._missionPlan.routes || []
        delegate: MapPolyline {
            parent: root.mapControl
            line.width: 4
            line.color: qgcPal.colorGreen
            z: 21
            path: (modelData.points || []).map(function (point) { return root._missionCoordinate(point[0], point[1]); }).filter(function (point) { return point !== null; })
        }
    }

    // A replay point is visually distinct from the live target and has no
    // interaction handler. It is only a map sanity-check for a hash-verified
    // event selected in the read-only replay dialog.
    Item {
        id: replayMapPreviewIndicator

        height: Math.max(18, ScreenTools.defaultFontPixelHeight * 1.2)
        visible: root._replayMapPreviewQualified && root._replayMapPreviewPointInView
        width: height
        x: root._replayMapPoint.x - width * 0.5
        y: root._replayMapPoint.y - height * 0.5
        z: 14

        Rectangle {
            anchors.centerIn: parent
            border.color: qgcPal.warningText
            border.width: Math.max(1, ScreenTools.defaultFontPixelWidth * 0.16)
            color: Qt.transparent
            height: Math.max(2, root._replayMapSigmaRadiusPx * 2.0)
            radius: width * 0.5
            visible: root._replayMapSigmaRadiusPx > 0.0
            width: height
        }

        Rectangle {
            anchors.centerIn: parent
            border.color: qgcPal.text
            border.width: 1
            color: qgcPal.colorOrange
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
            text: qsTr("回放预览 · ±%1 m (1σ)").arg(
                      root._replayMapPreviewEvent !== null ?
                          Number(root._replayMapPreviewEvent.gps.horizontal_sigma_m).toFixed(1) : "--")
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

            QGCLabel {
                Layout.fillWidth: true
                color: root._recordingQualityColor(
                          String(root._recordingCurrentStatus.level || "WAITING"))
                text: root._recordingQualityText()
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
                    text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                           multiDetectTargetEventReplayStore !== null &&
                           multiDetectTargetEventReplayStore.loaded) ?
                              qsTr("查看回放") : qsTr("载入回放")
                    onClicked: root._openOrChooseTargetEventReplay()
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.text
                    text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                           multiDetectTargetEventReplayStore !== null &&
                           multiDetectTargetEventReplayStore.loaded) ?
                              qsTr("只读 · %1 个事件已校验").arg(
                                  multiDetectTargetEventReplayStore.eventCount) :
                              qsTr("选择 manifest.json 后校验事件与媒体")
                    wrapMode: Text.WordWrap
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

    QGCFileDialog {
        id: targetEventReplayManifestFileDialog

        folder: QGroundControl.settingsManager.appSettings.telemetrySavePath
        nameFilters: [qsTr("JSON 文件 (*.json)"), qsTr("所有文件 (*)")]
        title: qsTr("选择目标事件回放 manifest.json")

        onAcceptedForLoad: function(file) {
            root._loadTargetEventReplayManifest(file);
            close();
        }
    }

    QGCPopupDialogFactory {
        id: targetEventReplayDialogFactory

        dialogComponent: targetEventReplayDialogComponent
    }

    Component {
        id: targetEventReplayDialogComponent

        QGCPopupDialog {
            id: targetEventReplayDialog

            buttons: Dialog.Close
            property string _replayFilter: "all"
            title: qsTr("目标事件回放 · 只读")

            Component.onDestruction: root._targetEventReplayDialog = null

            function valueOrDash(value) {
                return value === undefined || value === null || value === "" ? "--" : String(value);
            }

            function numberText(value, precision) {
                const numeric = Number(value);
                return Number.isFinite(numeric) ? numeric.toFixed(precision) : "--";
            }

            function replayFilterMatches(event) {
                const value = event || ({});
                if (_replayFilter === "locatable")
                    return (value.gps || ({})).coordinates_included === true;
                if (_replayFilter === "ranged")
                    return (value.ranging || ({})).available === true;
                if (_replayFilter === "media")
                    return (value.video || ({})).present === true ||
                           (value.depth || ({})).present === true;
                return true;
            }

            function replayFilterLabel() {
                if (_replayFilter === "locatable")
                    return qsTr("可定位");
                if (_replayFilter === "ranged")
                    return qsTr("有效测距");
                if (_replayFilter === "media")
                    return qsTr("有媒体");
                return qsTr("全部");
            }

            function replayVisibleCount() {
                if (typeof multiDetectTargetEventReplayStore === "undefined" ||
                        multiDetectTargetEventReplayStore === null ||
                        !multiDetectTargetEventReplayStore.loaded)
                    return 0;
                const values = multiDetectTargetEventReplayStore.events || [];
                let count = 0;
                for (let index = 0; index < values.length; ++index) {
                    if (replayFilterMatches(values[index]))
                        ++count;
                }
                return count;
            }

            function replayMatchedCount() {
                if (typeof multiDetectTargetEventReplayStore === "undefined" ||
                        multiDetectTargetEventReplayStore === null ||
                        !multiDetectTargetEventReplayStore.loaded)
                    return 0;
                if (_replayFilter === "locatable")
                    return multiDetectTargetEventReplayStore.locatableEventCount;
                if (_replayFilter === "ranged")
                    return multiDetectTargetEventReplayStore.rangedEventCount;
                if (_replayFilter === "media")
                    return multiDetectTargetEventReplayStore.mediaEventCount;
                return multiDetectTargetEventReplayStore.eventCount;
            }

            function mediaText(label, media) {
                const value = media || ({});
                if (value.present !== true)
                    return qsTr("%1：无").arg(label);
                const dimensions = value.width !== undefined && value.height !== undefined ?
                                      qsTr(" · %1×%2").arg(value.width).arg(value.height) : "";
                return qsTr("%1：%2 · %3 · %4 B%5 · SHA-256 %6")
                    .arg(label)
                    .arg(value.path || qsTr("未知路径"))
                    .arg(value.encoding || "--")
                    .arg(value.bytes === undefined ? "--" : value.bytes)
                    .arg(dimensions)
                    .arg(value.sha256 || "--");
            }

            function eventClipboardText(event) {
                const value = event || ({});
                const gps = value.gps || ({});
                const ranging = value.ranging || ({});
                const gpsLine = gps.coordinates_included === true ?
                                    qsTr("GPS: %1, %2 ±%3 m (%4)")
                                        .arg(numberText(gps.latitude_deg, 7))
                                        .arg(numberText(gps.longitude_deg, 7))
                                        .arg(numberText(gps.horizontal_sigma_m, 1))
                                        .arg(gps.freshness || "--") :
                                    qsTr("GPS: 不可定位 (%1)").arg(gps.status || "--");
                const rangeLine = ranging.available === true ?
                                      qsTr("测距: %1 m · 方位 %2° · %3")
                                          .arg(numberText(ranging.range_m, 2))
                                          .arg(numberText(ranging.bearing_deg, 2))
                                          .arg(ranging.validity || "--") :
                                      qsTr("测距: 不可用");
                return [
                    "Multi-Detect target-event replay",
                    qsTr("事件: %1 · 目标: %2 (%3)")
                        .arg(value.event_id || "--")
                        .arg(value.target_id || "--")
                        .arg(value.target_label || "--"),
                    qsTr("捕获: %1 s · 帧: %2")
                        .arg(numberText(value.captured_at_s, 3))
                        .arg(value.frame_id || "--"),
                    qsTr("来源帧: %1 · 载体高度 %2 m · 航向 %3°")
                        .arg(value.source_frame_id || "--")
                        .arg(numberText(value.vehicle_altitude_agl_m, 2))
                        .arg(numberText(value.vehicle_heading_deg, 2)),
                    gpsLine,
                    rangeLine,
                    mediaText(qsTr("视频"), value.video),
                    mediaText(qsTr("深度"), value.depth)
                ].join("\n");
            }

            function summaryClipboardText() {
                if (typeof multiDetectTargetEventReplayStore === "undefined" ||
                        multiDetectTargetEventReplayStore === null ||
                        !multiDetectTargetEventReplayStore.loaded)
                    return "";
                return [
                    "Multi-Detect target-event replay summary",
                    qsTr("清单: %1 · bundle: %2 · 会话: %3")
                        .arg(multiDetectTargetEventReplayStore.manifestName)
                        .arg(multiDetectTargetEventReplayStore.bundleId)
                        .arg(multiDetectTargetEventReplayStore.sessionId),
                    qsTr("开始: %1 · 事件: %2 · 媒体: %3")
                        .arg(multiDetectTargetEventReplayStore.startedAtUtc)
                        .arg(multiDetectTargetEventReplayStore.eventCount)
                        .arg(multiDetectTargetEventReplayStore.mediaCount),
                    qsTr("可定位: %1 · 有效测距: %2 · 有媒体: %3 · 目标: %4")
                        .arg(multiDetectTargetEventReplayStore.locatableEventCount)
                        .arg(multiDetectTargetEventReplayStore.rangedEventCount)
                        .arg(multiDetectTargetEventReplayStore.mediaEventCount)
                        .arg(multiDetectTargetEventReplayStore.uniqueTargetCount),
                    qsTr("时长: %1 s · 媒体字节: %2 B")
                        .arg(Number(multiDetectTargetEventReplayStore.captureDurationS).toFixed(3))
                        .arg(multiDetectTargetEventReplayStore.mediaBytes),
                    qsTr("完整性: %1")
                        .arg(multiDetectTargetEventReplayStore.integrityVerified ?
                                 qsTr("已验证") : qsTr("未验证"))
                ].join("\n");
            }

            function gpsText(gps) {
                const value = gps || ({});
                if (value.coordinates_included !== true)
                    return qsTr("不可定位 · 坐标已省略 · %1").arg(value.status || qsTr("未知"));
                return qsTr("可定位 · %1, %2 · 1σ %3 m · %4")
                    .arg(Number(value.latitude_deg).toFixed(7))
                    .arg(Number(value.longitude_deg).toFixed(7))
                    .arg(Number(value.horizontal_sigma_m).toFixed(1))
                    .arg(value.freshness || qsTr("未知时效"));
            }

            function rangeText(ranging) {
                const value = ranging || ({});
                if (value.available !== true)
                    return qsTr("测距：不可用");
                const range = value.range_m === undefined || value.range_m === null ? "--" :
                              Number(value.range_m).toFixed(1);
                const bearing = value.bearing_deg === undefined || value.bearing_deg === null ? "--" :
                                Number(value.bearing_deg).toFixed(1);
                return qsTr("测距：%1 m · 方位 %2° · %3")
                    .arg(range).arg(bearing).arg(value.validity || "--");
            }

            ColumnLayout {
                height: Math.min(targetEventReplayDialog.maxContentAvailableHeight,
                                 ScreenTools.defaultFontPixelHeight * 31)
                spacing: root._margin * 0.35
                width: Math.min(targetEventReplayDialog.maxContentAvailableWidth,
                                ScreenTools.defaultFontPixelWidth * 72)

                RowLayout {
                    Layout.fillWidth: true
                    spacing: root._margin * 0.35

                    QGCButton {
                        text: qsTr("选择清单")
                        onClicked: root._chooseTargetEventReplayManifest()
                    }

                    QGCButton {
                        text: qsTr("清除")
                        enabled: typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                 multiDetectTargetEventReplayStore !== null &&
                                 multiDetectTargetEventReplayStore.loaded
                        onClicked: root._clearTargetEventReplay()
                    }

                    QGCButton {
                        text: qsTr("清除落图预览")
                        enabled: root._replayMapPreviewEvent !== null
                        onClicked: root._clearReplayMapPreview()
                    }

                    QGCButton {
                        text: qsTr("复制全量摘要")
                        enabled: typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                 multiDetectTargetEventReplayStore !== null &&
                                 multiDetectTargetEventReplayStore.loaded &&
                                 multiDetectTargetEventReplayStore.integrityVerified
                        onClicked: QGroundControl.copyToClipboard(
                                       targetEventReplayDialog.summaryClipboardText())
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        color: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                multiDetectTargetEventReplayStore !== null &&
                                multiDetectTargetEventReplayStore.integrityVerified) ?
                                   qgcPal.colorGreen : qgcPal.warningText
                        font.bold: true
                        text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                               multiDetectTargetEventReplayStore !== null) ?
                                  multiDetectTargetEventReplayStore.statusText :
                                  qsTr("回放校验器不可用")
                        wrapMode: Text.WordWrap
                    }
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: root._replayMapPreviewQualified ? qgcPal.colorGreen : qgcPal.warningText
                    text: root._replayMapPreviewQualified && root._replayMapPreviewEvent !== null ?
                              qsTr("地图预览：%1 · 只读 · 不改变当前目标")
                                  .arg(root._replayMapPreviewEvent.event_id || "--") :
                              qsTr("地图预览：选择一条可定位事件后显示")
                    wrapMode: Text.WordWrap
                }

                Rectangle {
                    Layout.fillWidth: true
                    color: qgcPal.windowShade
                    implicitHeight: replaySummaryColumn.implicitHeight + root._margin
                    radius: ScreenTools.defaultBorderRadius

                    ColumnLayout {
                        id: replaySummaryColumn
                        anchors.fill: parent
                        anchors.margins: root._margin * 0.5
                        spacing: root._margin * 0.16

                        QGCLabel {
                            Layout.fillWidth: true
                            font.bold: true
                            text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                   multiDetectTargetEventReplayStore !== null &&
                                   multiDetectTargetEventReplayStore.loaded) ?
                                      qsTr("%1 · 会话 %2")
                                          .arg(multiDetectTargetEventReplayStore.bundleId)
                                          .arg(multiDetectTargetEventReplayStore.sessionId) :
                                      qsTr("尚未加载目标事件回放")
                            wrapMode: Text.WordWrap
                        }

                        QGCLabel {
                            Layout.fillWidth: true
                            text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                   multiDetectTargetEventReplayStore !== null &&
                                   multiDetectTargetEventReplayStore.loaded) ?
                                      qsTr("清单 %1 · 开始 %2 · 事件 %3/%4 · 媒体 %5")
                                          .arg(multiDetectTargetEventReplayStore.manifestName)
                                          .arg(multiDetectTargetEventReplayStore.startedAtUtc)
                                          .arg(multiDetectTargetEventReplayStore.displayedEventCount)
                                          .arg(multiDetectTargetEventReplayStore.eventCount)
                                          .arg(multiDetectTargetEventReplayStore.mediaCount) :
                                      qsTr("先选择由 create_target_event_replay.py 生成的 manifest.json")
                            wrapMode: Text.WordWrap
                        }

                        QGCLabel {
                            Layout.fillWidth: true
                            text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                   multiDetectTargetEventReplayStore !== null &&
                                   multiDetectTargetEventReplayStore.loaded) ?
                                      qsTr("全量质量：可定位 %1 · 有效测距 %2 · 有媒体 %3 · 目标 %4 · 时长 %5 s · 媒体 %6 B")
                                          .arg(multiDetectTargetEventReplayStore.locatableEventCount)
                                          .arg(multiDetectTargetEventReplayStore.rangedEventCount)
                                          .arg(multiDetectTargetEventReplayStore.mediaEventCount)
                                          .arg(multiDetectTargetEventReplayStore.uniqueTargetCount)
                                          .arg(Number(multiDetectTargetEventReplayStore.captureDurationS).toFixed(3))
                                          .arg(multiDetectTargetEventReplayStore.mediaBytes) : ""
                            visible: text !== ""
                            wrapMode: Text.WordWrap
                        }

                        QGCLabel {
                            Layout.fillWidth: true
                            text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                   multiDetectTargetEventReplayStore !== null &&
                                   multiDetectTargetEventReplayStore.loaded) ?
                                      qsTr("筛选：%1 · 匹配 %2 条 · 当前显示 %3 条")
                                          .arg(targetEventReplayDialog.replayFilterLabel())
                                          .arg(targetEventReplayDialog.replayMatchedCount())
                                          .arg(targetEventReplayDialog.replayVisibleCount()) : ""
                            visible: text !== ""
                            wrapMode: Text.WordWrap
                        }

                        QGCLabel {
                            Layout.fillWidth: true
                            color: qgcPal.warningText
                            text: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                   multiDetectTargetEventReplayStore !== null &&
                                   multiDetectTargetEventReplayStore.truncated) ?
                                      qsTr("事件过多，仅显示前 %1 条；完整数量仍已校验").arg(
                                          multiDetectTargetEventReplayStore.displayedEventCount) : ""
                            visible: text !== ""
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: root._margin * 0.2

                    QGCLabel {
                        text: qsTr("筛选")
                    }

                    QGCButton {
                        text: targetEventReplayDialog._replayFilter === "all" ? qsTr("✓ 全部") : qsTr("全部")
                        onClicked: targetEventReplayDialog._replayFilter = "all"
                    }

                    QGCButton {
                        text: targetEventReplayDialog._replayFilter === "locatable" ?
                                  qsTr("✓ 可定位") : qsTr("可定位")
                        onClicked: targetEventReplayDialog._replayFilter = "locatable"
                    }

                    QGCButton {
                        text: targetEventReplayDialog._replayFilter === "ranged" ?
                                  qsTr("✓ 有效测距") : qsTr("有效测距")
                        onClicked: targetEventReplayDialog._replayFilter = "ranged"
                    }

                    QGCButton {
                        text: targetEventReplayDialog._replayFilter === "media" ?
                                  qsTr("✓ 有媒体") : qsTr("有媒体")
                        onClicked: targetEventReplayDialog._replayFilter = "media"
                    }
                }

                ScrollView {
                    Layout.fillHeight: true
                    Layout.fillWidth: true
                    clip: true

                    ListView {
                        id: targetEventReplayList

                        clip: true
                        model: (typeof multiDetectTargetEventReplayStore !== "undefined" &&
                                multiDetectTargetEventReplayStore !== null &&
                                multiDetectTargetEventReplayStore.loaded) ?
                                   multiDetectTargetEventReplayStore.events : []
                        spacing: root._margin * 0.35

                        delegate: Rectangle {
                            required property var modelData
                            readonly property bool replayMatches:
                                targetEventReplayDialog.replayFilterMatches(modelData)

                            border.color: qgcPal.button
                            border.width: 1
                            color: qgcPal.windowShade
                            height: replayMatches ? replayEventColumn.implicitHeight + root._margin : 0
                            radius: ScreenTools.defaultBorderRadius
                            visible: replayMatches
                            width: targetEventReplayList.width

                            ColumnLayout {
                                id: replayEventColumn
                                anchors.fill: parent
                                anchors.margins: root._margin * 0.5
                                spacing: root._margin * 0.16

                                QGCLabel {
                                    Layout.fillWidth: true
                                    font.bold: true
                                    text: qsTr("#%1 · %2 · 目标 %3 (%4)")
                                          .arg(Number(parent.parent.modelData.sequence).toFixed(0))
                                          .arg(parent.parent.modelData.event_id || "--")
                                          .arg(parent.parent.modelData.target_id || "--")
                                          .arg(parent.parent.modelData.target_label || "--")
                                    wrapMode: Text.WordWrap
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    text: qsTr("捕获时间：%1 s · 帧 %2")
                                          .arg(Number(parent.parent.modelData.captured_at_s).toFixed(3))
                                          .arg(parent.parent.modelData.frame_id || "--")
                                    wrapMode: Text.WordWrap
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    color: parent.parent.modelData.gps.coordinates_included === true ?
                                               qgcPal.colorGreen : qgcPal.warningText
                                    text: targetEventReplayDialog.gpsText(parent.parent.modelData.gps)
                                    wrapMode: Text.WordWrap
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    text: targetEventReplayDialog.rangeText(
                                              parent.parent.modelData.ranging)
                                    wrapMode: Text.WordWrap
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    color: qgcPal.text
                                    text: targetEventReplayDialog.mediaText(
                                              qsTr("视频"), parent.parent.modelData.video)
                                    wrapMode: Text.WrapAnywhere
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    color: qgcPal.text
                                    text: targetEventReplayDialog.mediaText(
                                              qsTr("深度"), parent.parent.modelData.depth)
                                    wrapMode: Text.WrapAnywhere
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: root._margin * 0.35

                                    QGCButton {
                                        text: qsTr("复制证据")
                                        onClicked: QGroundControl.copyToClipboard(
                                                       targetEventReplayDialog.eventClipboardText(
                                                           parent.parent.parent.modelData))
                                    }

                                    QGCButton {
                                        enabled: parent.parent.parent.modelData.gps.coordinates_included === true
                                        text: qsTr("落图预览")
                                        onClicked: root._previewReplayEvent(
                                                       parent.parent.parent.modelData)
                                    }
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    color: qgcPal.text
                                    text: qsTr("来源帧：%1")
                                          .arg(parent.parent.modelData.source_frame_id || "--")
                                    wrapMode: Text.WordWrap
                                }

                                QGCLabel {
                                    Layout.fillWidth: true
                                    color: qgcPal.text
                                    text: qsTr("载体：高度 %1 m · 航向 %2°")
                                          .arg(targetEventReplayDialog.numberText(
                                                   parent.parent.modelData.vehicle_altitude_agl_m, 1))
                                          .arg(targetEventReplayDialog.numberText(
                                                   parent.parent.modelData.vehicle_heading_deg, 1))
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }

                        QGCLabel {
                            anchors.centerIn: parent
                            color: qgcPal.text
                            text: qsTr("尚无已校验的目标事件")
                            visible: targetEventReplayDialog.replayVisibleCount() === 0
                        }
                    }
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: qgcPal.warningText
                    text: qsTr("回放查看器只读取已校验证据；落图预览仅为只读核对，不选目标、发送任务或产生飞控指令")
                    wrapMode: Text.WordWrap
                }
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
            readonly property var recordingQuality: MultiDetectState.targetObservationRecordingQualitySummary || ({})
            readonly property var recordingSampling: recordingQuality.sampling || ({})
            readonly property var recordingCurrentStatus: recordingQuality.current_status || ({})
            readonly property var recordingSegments: recordingQuality.quality_segments || ({})
            readonly property var qualitySegmentRows: flattenQualitySegments(recordingSegments)

            function valueOrDash(value) {
                return value === undefined || value === null || value === "" ? "--" : String(value);
            }

            function receiveAge(channel) {
                const entry = localReceive[channel] || ({});
                return entry.age_ms === undefined || entry.age_ms === null ? "--" :
                       qsTr("%1 ms").arg(Math.round(Number(entry.age_ms)));
            }

            function flattenQualitySegments(segments) {
                const rows = [];
                ["non_locatable", "range_invalid", "gnss_unqualified"].forEach(function(kind) {
                    const values = Array.isArray(segments[kind]) ? segments[kind] : [];
                    values.forEach(function(segment) { rows.push(segment); });
                });
                return rows;
            }

            function qualityKindText(kind) {
                if (kind === "non_locatable")
                    return qsTr("不可定位");
                if (kind === "range_invalid")
                    return qsTr("测距异常");
                return qsTr("GNSS 不合格");
            }

            function qualityKindColor(kind) {
                if (kind === "non_locatable")
                    return qgcPal.colorRed;
                if (kind === "range_invalid")
                    return qgcPal.warningText;
                return qgcPal.colorOrange;
            }

            function qualityLevelText(level) {
                if (level === "HEALTHY")
                    return qsTr("良好");
                if (level === "WARNING")
                    return qsTr("警告");
                if (level === "CRITICAL")
                    return qsTr("严重");
                return qsTr("等待样本");
            }

            function qualityLevelColor(level) {
                if (level === "HEALTHY")
                    return qgcPal.colorGreen;
                if (level === "CRITICAL")
                    return qgcPal.colorRed;
                return qgcPal.warningText;
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

                    Rectangle {
                        Layout.fillWidth: true
                        color: qgcPal.windowShade
                        implicitHeight: recordingQualityColumn.implicitHeight + root._margin
                        radius: ScreenTools.defaultBorderRadius
                        visible: Number(targetObservationDiagnosticsDialog.recordingQuality.sample_count || 0) > 0

                        ColumnLayout {
                            id: recordingQualityColumn
                            anchors.fill: parent
                            anchors.margins: root._margin * 0.5
                            spacing: root._margin * 0.28

                            QGCLabel {
                                Layout.fillWidth: true
                                font.bold: true
                                text: targetObservationDiagnosticsDialog.recordingQuality.active === true ?
                                          qsTr("录制质量 · 实时只读") : qsTr("录制质量 · 已停止")
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                color: targetObservationDiagnosticsDialog.qualityLevelColor(
                                           targetObservationDiagnosticsDialog.recordingCurrentStatus.level)
                                font.bold: true
                                font.pointSize: ScreenTools.mediumFontPointSize
                                text: qsTr("当前状态：%1")
                                      .arg(targetObservationDiagnosticsDialog.qualityLevelText(
                                               targetObservationDiagnosticsDialog.recordingCurrentStatus.level))
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("当前连续不可定位 %1 · 测距异常 %2 · GNSS 不合格 %3 · 最近间隔 %4 ms")
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingCurrentStatus.current_non_locatable_run || 0)))
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingCurrentStatus.current_range_invalid_run || 0)))
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingCurrentStatus.current_gnss_unqualified_run || 0)))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.recordingCurrentStatus.latest_gap_ms))
                                wrapMode: Text.WordWrap
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("样本 %1 · 可定位率 %2% · 最长连续不可定位 %3 · 观测 %4 ms")
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingQuality.sample_count || 0)))
                                      .arg((Number(targetObservationDiagnosticsDialog.recordingQuality.locatable_rate || 0) * 100).toFixed(1))
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingQuality.longest_non_locatable_run || 0)))
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingQuality.observation_duration_ms || 0)))
                                wrapMode: Text.WordWrap
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("采样期望 %1 ms · 最大间隔 %2 ms · 平均间隔 %3 ms · 断档 %4")
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.recordingSampling.expected_interval_ms))
                                      .arg(targetObservationDiagnosticsDialog.valueOrDash(
                                               targetObservationDiagnosticsDialog.recordingSampling.maximum_gap_ms))
                                      .arg(targetObservationDiagnosticsDialog.recordingSampling.mean_gap_ms === undefined ||
                                           targetObservationDiagnosticsDialog.recordingSampling.mean_gap_ms === null ? "--" :
                                               Number(targetObservationDiagnosticsDialog.recordingSampling.mean_gap_ms).toFixed(1))
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingSampling.cadence_violation_count || 0)))
                                wrapMode: Text.WordWrap
                            }

                            QGCLabel {
                                Layout.fillWidth: true
                                font.bold: true
                                text: qsTr("质量时间轴")
                            }
                            Rectangle {
                                id: qualityTimeline
                                Layout.fillWidth: true
                                implicitHeight: ScreenTools.defaultFontPixelHeight * 1.35
                                color: qgcPal.window
                                border.color: qgcPal.text
                                border.width: 1
                                radius: ScreenTools.defaultBorderRadius

                                Repeater {
                                    model: targetObservationDiagnosticsDialog.qualitySegmentRows

                                    Rectangle {
                                        required property var modelData
                                        readonly property real totalSamples: Math.max(
                                                                 Number(targetObservationDiagnosticsDialog.recordingQuality.sample_count || 0), 1)
                                        x: Number(modelData.start_index || 0) / totalSamples * qualityTimeline.width
                                        y: modelData.kind === "non_locatable" ? 1 :
                                           modelData.kind === "range_invalid" ? qualityTimeline.height / 3 :
                                                                                qualityTimeline.height * 2 / 3
                                        width: Math.max(2, Number(modelData.sample_count || 0) /
                                                        totalSamples * qualityTimeline.width)
                                        height: Math.max(2, qualityTimeline.height / 3 - 1)
                                        color: targetObservationDiagnosticsDialog.qualityKindColor(modelData.kind)
                                        opacity: 0.82
                                    }
                                }
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                color: qgcPal.colorRed
                                text: qsTr("红：不可定位 · 黄：测距异常 · 橙：GNSS 不合格")
                                wrapMode: Text.WordWrap
                            }
                            Repeater {
                                model: targetObservationDiagnosticsDialog.qualitySegmentRows

                                QGCLabel {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    color: targetObservationDiagnosticsDialog.qualityKindColor(modelData.kind)
                                    text: qsTr("%1：样本 %2–%3，共 %4 个，持续 %5 ms")
                                          .arg(targetObservationDiagnosticsDialog.qualityKindText(modelData.kind))
                                          .arg(Math.round(Number(modelData.start_index)))
                                          .arg(Math.round(Number(modelData.end_index)))
                                          .arg(Math.round(Number(modelData.sample_count)))
                                          .arg(Math.round(Number(modelData.duration_ms)))
                                    wrapMode: Text.WordWrap
                                }
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                visible: targetObservationDiagnosticsDialog.qualitySegmentRows.length === 0
                                text: qsTr("当前录制未发现连续质量异常段")
                                wrapMode: Text.WordWrap
                            }
                            QGCLabel {
                                Layout.fillWidth: true
                                text: qsTr("epoch 切换 %1 次；最终正式报告仍以 manifest 绑定的离线 verifier 为准")
                                      .arg(Math.round(Number(targetObservationDiagnosticsDialog.recordingQuality.epoch_transition_count || 0)))
                                wrapMode: Text.WordWrap
                            }
                        }
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

    QGCPopupDialogFactory {
        id: missionPackageDialogFactory
        dialogComponent: missionPackageDialogComponent
    }

    Component {
        id: missionPackageDialogComponent
        MultiDetectPanels.MultiDetectMissionPanel {
            mapControl: root.mapControl
            originLatitude: root._missionOriginLatitude
            originLongitude: root._missionOriginLongitude
            onOriginLatitudeChanged: root._missionOriginLatitude = originLatitude
            onOriginLongitudeChanged: root._missionOriginLongitude = originLongitude
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
                text: qsTr("任务包")
                onClicked: missionPackageDialogFactory.open()
            }

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

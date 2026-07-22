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
    property real _panelWidth: Math.min(
        Math.max(1, width - _margin * 2),
        Math.max(
            ScreenTools.defaultFontPixelWidth * 40,
            Math.min(width * 0.38, ScreenTools.defaultFontPixelWidth * 52)
        )
    )
    readonly property bool _targetActionable: MultiDetectState.lckActionable
    readonly property string _modeCode: MultiDetectState.missionMode === "PAYLOAD" ? "M2" :
                                        MultiDetectState.missionMode === "OBSERVE" ? "M3" : "M1"
    property var mapControl
    property var parentToolInsets
    property var totalToolInsets: toolInsets
    property string _pendingExecutionPopup: ""
    property double _pendingExecutionStartedAtMs: 0
    readonly property bool _targetGeolocationQualified: MultiDetectState.targetGeolocationAvailable &&
                                                       MultiDetectState.targetGeolocationTargetId !== "" &&
                                                       MultiDetectState.targetId !== "" &&
                                                       MultiDetectState.targetGeolocationTargetId === MultiDetectState.targetId &&
                                                       Number.isFinite(MultiDetectState.targetLatitudeDeg) &&
                                                       Number.isFinite(MultiDetectState.targetLongitudeDeg) &&
                                                       MultiDetectState.targetHorizontalSigmaM >= 0.0
    readonly property bool _targetMapVisible: _targetGeolocationQualified && mapControl !== null &&
                                              mapControl.pipState.state === mapControl.pipState.fullState
    readonly property bool _targetMapContextVisible: mapControl !== null &&
                                                     mapControl.pipState.state === mapControl.pipState.fullState &&
                                                     (MultiDetectState.targetId !== "" ||
                                                      MultiDetectState.targetGeolocationTargetId !== "")
    property point _targetMapPoint: Qt.point(-1, -1)
    property real _targetMapSigmaRadiusPx: 0.0
    property string _targetEvidenceFeedback: ""
    property bool _targetEvidenceFeedbackIsError: false
    readonly property bool _targetMapPointInView: _targetMapPoint.x >= 0.0 && _targetMapPoint.y >= 0.0 &&
                                                  _targetMapPoint.x <= width && _targetMapPoint.y <= height

    function _currentTargetRangeM() {
        if (MultiDetectState.rangeTargetId === MultiDetectState.targetId &&
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

    function _targetBearingText() {
        const bearingDeg = _currentTargetBearingDeg();
        return Number.isFinite(bearingDeg) ?
                    qsTr("相对方位：%1°").arg(bearingDeg.toFixed(1)) :
                    qsTr("相对方位：等待估计");
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

    function _saveTargetEvidenceSnapshot() {
        if (!_targetGeolocationQualified) {
            _setTargetEvidenceFeedback(qsTr("不可定位：没有可保存的合格坐标"), true);
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
            "range_m": _currentTargetRangeM(),
            "relative_bearing_deg": _currentTargetBearingDeg(),
            "range_validity": MultiDetectState.rangeTargetId === MultiDetectState.targetId ?
                                  MultiDetectState.rangeValidity : "UNAVAILABLE",
            "source_age_ms": MultiDetectState.targetGeolocationSourceAgeMs
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

    function _updateTargetMapProjection() {
        if (!_targetMapVisible || mapControl === null ||
                typeof mapControl.fromCoordinate !== "function") {
            _targetMapPoint = Qt.point(-1, -1);
            _targetMapSigmaRadiusPx = 0.0;
            return;
        }

        const latitude = MultiDetectState.targetLatitudeDeg;
        const longitude = MultiDetectState.targetLongitudeDeg;
        const targetCoordinate = QtPositioning.coordinate(latitude, longitude);
        const targetMapPoint = mapControl.fromCoordinate(targetCoordinate, false);
        const targetOverlayPoint = root.mapFromItem(mapControl, targetMapPoint.x, targetMapPoint.y);
        if (!Number.isFinite(targetOverlayPoint.x) || !Number.isFinite(targetOverlayPoint.y)) {
            _targetMapPoint = Qt.point(-1, -1);
            _targetMapSigmaRadiusPx = 0.0;
            return;
        }

        // Convert the reported one-sigma horizontal uncertainty to the current
        // map scale. The circle is a display of uncertainty, not a command or a
        // navigation radius. Use both axes so it remains useful on rotated maps.
        const sigmaM = MultiDetectState.targetHorizontalSigmaM;
        const latitudeOffsetDeg = sigmaM / 111132.0;
        const longitudeDenominator = Math.max(1.0e-6,
                                              111320.0 * Math.cos(latitude * Math.PI / 180.0));
        const longitudeOffsetDeg = sigmaM / longitudeDenominator;
        const northMapPoint = mapControl.fromCoordinate(
                    QtPositioning.coordinate(latitude + latitudeOffsetDeg, longitude), false);
        const eastMapPoint = mapControl.fromCoordinate(
                    QtPositioning.coordinate(latitude, longitude + longitudeOffsetDeg), false);
        const northOverlayPoint = root.mapFromItem(mapControl, northMapPoint.x, northMapPoint.y);
        const eastOverlayPoint = root.mapFromItem(mapControl, eastMapPoint.x, eastMapPoint.y);

        _targetMapPoint = targetOverlayPoint;
        _targetMapSigmaRadiusPx = Math.max(
                    0.0,
                    Math.abs(northOverlayPoint.y - targetOverlayPoint.y),
                    Math.abs(eastOverlayPoint.x - targetOverlayPoint.x));
    }

    function _requestExecutionPopup(kind) {
        if (_pendingExecutionPopup !== "")
            return;

        // A challenge remains valid for its signed lifetime. Reuse it instead
        // of replacing it with another PROMOTE_LCK request on a second click.
        if (kind === "payload" && MultiDetectState.payloadTargetChallengeActive) {
            payloadActionPopup.open();
            return;
        }
        if (kind === "approach" && MultiDetectState.approachChallengeActive) {
            approachActionPopup.open();
            return;
        }

        if (!MultiDetectState.refreshLockForExecution())
            return;
        _pendingExecutionPopup = kind;
        _pendingExecutionStartedAtMs = Date.now();
        executionChallengeWaitTimer.restart();
    }

    Component.onCompleted: {
        _syncMode3AimLoopCue();
        _updateTargetMapProjection();
    }

    Timer {
        id: targetMapProjectionTimer

        interval: 150
        repeat: true
        running: root._targetMapVisible

        onTriggered: root._updateTargetMapProjection()
    }

    Timer {
        id: executionChallengeWaitTimer

        interval: 75
        repeat: true

        onTriggered: {
            const elapsedMs = Date.now() - root._pendingExecutionStartedAtMs;
            if (root._pendingExecutionPopup === "payload" &&
                    MultiDetectState.payloadTargetChallengeActive) {
                root._pendingExecutionPopup = "";
                stop();
                payloadActionPopup.open();
                return;
            }
            if (root._pendingExecutionPopup === "approach" &&
                    MultiDetectState.approachChallengeActive) {
                root._pendingExecutionPopup = "";
                stop();
                approachActionPopup.open();
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

    function _number(value, precision, suffix) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(precision) + suffix : "--";
    }

    function _percent(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? (numeric * 100.0).toFixed(0) + "%" : "--";
    }

    function _metadataLabel() {
        switch (MultiDetectState.metadataLinkState) {
        case "AUTHENTICATED":
            return qsTr("在线");
        case "STALE":
            return qsTr("数据超时");
        case "DIRECT_SIGNED_METADATA_READY":
            return qsTr("等待数据");
        case "WAITING_FOR_SIGNED_METADATA":
            return qsTr("等待连接");
        case "DISABLED":
        case "UNAVAILABLE":
            return qsTr("未连接");
        default:
            return qsTr("连接中");
        }
    }

    function _classLabel(label) {
        const normalized = String(label || "").toLowerCase();
        if (["flame", "fire", "hotspot", "burned_area"].indexOf(normalized) >= 0)
            return qsTr("火情");
        if (["smoke", "smoldering_area", "smolder_area"].indexOf(normalized) >= 0)
            return qsTr("烟雾");
        if (["person", "pedestrian", "people", "firefighter"].indexOf(normalized) >= 0)
            return qsTr("人员");
        if (["car", "van", "truck", "bus", "train", "motorcycle", "bicycle", "motor"].indexOf(normalized) >= 0)
            return qsTr("车辆");
        return label === "" ? qsTr("未知目标") : label;
    }

    function _trackingLabel() {
        switch (MultiDetectState.trackingState) {
        case "LOCKED":
            return qsTr("已锁定");
        case "TRACKING":
            return qsTr("跟踪中");
        case "RECOVERED":
            return qsTr("跟踪恢复");
        case "INITIALIZING":
            return qsTr("正在建立跟踪");
        case "LOST":
            return qsTr("目标丢失");
        case "CANCELLED":
            return qsTr("已取消");
        default:
            return qsTr("未跟踪");
        }
    }

    function _recognitionText() {
        if (MultiDetectState.metadataLinkState !== "AUTHENTICATED")
            return qsTr("等待 Jetson 目标数据");
        const candidateCount = MultiDetectState.selectableTargetPoolEntries ?
            MultiDetectState.selectableTargetPoolEntries.length : 0;
        return qsTr("候选 ") + candidateCount + " · " +
               qsTr("锁定 ") + MultiDetectState.lockedTrackCount;
    }

    function _targetText() {
        if (!MultiDetectState.targetBoxValid)
            return qsTr("未选择");
        return _classLabel(MultiDetectState.targetLabel) + " · " +
               _percent(MultiDetectState.targetConfidence);
    }

    function _trackingText() {
        if (!MultiDetectState.targetBoxValid)
            return qsTr("未跟踪");
        return _trackingLabel() + " · " +
               qsTr("质量 ") + _percent(MultiDetectState.trackingQuality);
    }

    function _dataText() {
        if (MultiDetectState.metadataLinkState !== "AUTHENTICATED")
            return qsTr("等待实时元数据");
        if (MultiDetectState.trackingMetadataRateHz > 0.0)
            return qsTr("目标框 ") + _number(MultiDetectState.trackingMetadataRateHz, 1, " Hz");
        if (MultiDetectState.selectableTargetPoolEntries &&
                MultiDetectState.selectableTargetPoolEntries.length > 0)
            return qsTr("实时目标池已接收");
        return qsTr("链路在线 · 等待识别结果");
    }

    function _sceneText() {
        if (MultiDetectState.fireAlert)
            return qsTr("检测到火情");
        if (MultiDetectState.sceneContextState === "VALID" &&
                MultiDetectState.sceneContextRegions &&
                MultiDetectState.sceneContextRegions.length > 0)
            return qsTr("场景区域 ") + MultiDetectState.sceneContextRegions.length;
        return qsTr("视觉识别运行中");
    }

    function _rangeContribution(sources) {
        const contributions = MultiDetectState.rangeSourceContributions || [];
        let weight = 0.0;
        let hasMeasurement = false;
        for (let index = 0; index < contributions.length; ++index) {
            const contribution = contributions[index];
            if (sources.indexOf(String(contribution.source || "")) < 0)
                continue;
            const candidateWeight = Number(contribution.weight);
            if (Number.isFinite(candidateWeight))
                weight += candidateWeight;
            hasMeasurement = true;
        }
        return {
            "hasMeasurement": hasMeasurement,
            "weight": weight
        };
    }

    function _rangingAlgorithmValue(sources, inactiveStatus) {
        const contribution = _rangeContribution(sources);
        return contribution.hasMeasurement ?
               qsTr("测距中") + " · " + _percent(contribution.weight) : inactiveStatus;
    }

    function _rangingAlgorithmActive(sources) {
        const contribution = _rangeContribution(sources);
        return contribution.hasMeasurement ||
               (sources.indexOf("monocular_metric") >= 0 &&
                MultiDetectState.depthMapAvailable);
    }

    readonly property var _statusRows: [
        {
            "label": qsTr("模式"),
            "value": root._modeCode + " · " + MultiDetectState.interactionState
        },
        {
            "label": qsTr("识别"),
            "value": _recognitionText()
        },
        {
            "label": qsTr("目标"),
            "value": _targetText()
        },
        {
            "label": qsTr("跟踪"),
            "value": _trackingText()
        },
        {
            "label": qsTr("数据"),
            "value": _dataText()
        },
        {
            "label": qsTr("测距"),
            "value": MultiDetectState.rangeFusionText()
        },
        {
            "algorithm": true,
            "label": "Depth Anything V2",
            "active": root._rangingAlgorithmActive(["monocular_metric", "monocular_size"]),
            "value": root._rangingAlgorithmValue(
                         ["monocular_metric", "monocular_size"], qsTr("等待深度")
                     )
        },
        {
            "algorithm": true,
            "label": "VIO-SLAM",
            "active": root._rangingAlgorithmActive(["vio"]),
            "value": root._rangingAlgorithmValue(["vio"], qsTr("等待运动"))
        },
        {
            "algorithm": true,
            "label": "RGB-SLAM",
            "active": root._rangingAlgorithmActive(["rgb_slam"]),
            "value": root._rangingAlgorithmValue(["rgb_slam"], qsTr("等待视差"))
        },
        {
            "algorithm": true,
            "label": "PX4 地面几何",
            "active": root._rangingAlgorithmActive(["camera_ground", "pixhawk_agl"]),
            "value": root._rangingAlgorithmValue(
                         ["camera_ground", "pixhawk_agl"], qsTr("等待高度")
                     )
        },
        {
            "label": qsTr("场景"),
            "value": _sceneText(),
            "warning": MultiDetectState.fireAlert
        }
    ]

    QGCPalette {
        id: qgcPal

        colorGroupEnabled: true
    }

    QGCToolInsets {
        id: toolInsets

        bottomEdgeCenterInset: actionBar.visible ? root.height - actionBar.y : parentToolInsets.bottomEdgeCenterInset
        bottomEdgeLeftInset: parentToolInsets.bottomEdgeLeftInset
        bottomEdgeRightInset: parentToolInsets.bottomEdgeRightInset
        leftEdgeBottomInset: parentToolInsets.leftEdgeBottomInset
        leftEdgeCenterInset: parentToolInsets.leftEdgeCenterInset
        leftEdgeTopInset: parentToolInsets.leftEdgeTopInset
        rightEdgeBottomInset: parentToolInsets.rightEdgeBottomInset
        rightEdgeCenterInset: statusPanel.visible ? root.width - statusPanel.x : parentToolInsets.rightEdgeCenterInset
        rightEdgeTopInset: parentToolInsets.rightEdgeTopInset
        topEdgeCenterInset: parentToolInsets.topEdgeCenterInset
        topEdgeLeftInset: parentToolInsets.topEdgeLeftInset
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
            border.color: "#ffd43b"
            border.width: Math.max(1, ScreenTools.defaultFontPixelWidth * 0.16)
            color: "#00ffffff"
            height: Math.max(2, root._targetMapSigmaRadiusPx * 2.0)
            radius: width * 0.5
            visible: root._targetMapSigmaRadiusPx > 0.0
            width: height
        }

        Rectangle {
            anchors.centerIn: parent
            border.color: "#ffffff"
            border.width: 1
            color: "#e63946"
            height: parent.height
            radius: width * 0.5
            width: height
        }

        QGCLabel {
            anchors.bottom: parent.top
            anchors.bottomMargin: ScreenTools.defaultFontPixelHeight * 0.25
            anchors.horizontalCenter: parent.horizontalCenter
            color: "#ffd43b"
            font.bold: true
            text: qsTr("目标 ±%1 m (1σ)").arg(MultiDetectState.targetHorizontalSigmaM.toFixed(1))
        }
    }

    Rectangle {
        id: targetGeolocationCard

        anchors.left: parent.left
        anchors.leftMargin: root._margin
        anchors.top: parent.top
        anchors.topMargin: parentToolInsets.topEdgeLeftInset +
                           Math.max(root._margin, ScreenTools.defaultFontPixelHeight * 7.4)
        border.color: root._targetGeolocationQualified ? "#ffd43b" : qgcPal.warningText
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
                color: root._targetGeolocationQualified ? "#ffd43b" : qgcPal.warningText
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
                             typeof multiDetectTargetEvidenceStore !== "undefined" &&
                             multiDetectTargetEvidenceStore !== null
                    text: qsTr("保存快照")
                    onClicked: root._saveTargetEvidenceSnapshot()
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

    Rectangle {
        id: statusPanel

        anchors.right: parent.right
        anchors.rightMargin: root._margin
        anchors.top: parent.top
        // Keep the upper centre clear for the fixed-wing heading tape.  The
        // right panel then ends above the relocated ALT/VS strip.
        anchors.topMargin: parentToolInsets.topEdgeRightInset +
                           Math.max(root._margin, ScreenTools.defaultFontPixelHeight * 7.4)
        border.color: MultiDetectState.fireAlert ? qgcPal.warningText : qgcPal.windowShadeDark
        border.width: 1
        color: qgcPal.window
        height: statusColumn.implicitHeight + root._margin * 2
        opacity: 0.92
        radius: ScreenTools.defaultBorderRadius
        width: root._panelWidth
        z: 20

        ColumnLayout {
            id: statusColumn

            anchors.fill: parent
            anchors.margins: root._margin
            spacing: root._margin * 0.3

            RowLayout {
                Layout.fillWidth: true
                spacing: root._margin * 0.5

                Rectangle {
                    color: MultiDetectState.metadataLinkState === "AUTHENTICATED" ? qgcPal.colorGreen :
                           MultiDetectState.metadataLinkState === "STALE" ? qgcPal.warningText : qgcPal.text
                    height: width
                    radius: width / 2
                    width: ScreenTools.defaultFontPixelHeight * 0.72
                }

                QGCLabel {
                    Layout.fillWidth: true
                    font.bold: true
                    font.pointSize: ScreenTools.largeFontPointSize
                    text: "JETSON · AI"
                }

                QGCLabel {
                    color: MultiDetectState.metadataLinkState === "AUTHENTICATED" ? qgcPal.colorGreen : qgcPal.text
                    font.bold: true
                    font.pointSize: ScreenTools.defaultFontPointSize
                    text: root._metadataLabel()
                }
            }

            Rectangle {
                Layout.fillWidth: true
                color: qgcPal.windowShadeDark
                height: 1
            }

            Repeater {
                model: root._statusRows

                delegate: RowLayout {
                    id: telemetryRow

                    required property var modelData

                    Layout.fillWidth: true
                    Layout.minimumHeight: Math.max(26, ScreenTools.defaultFontPixelHeight * 1.38)
                    spacing: root._margin * 0.8

                    Rectangle {
                        Layout.alignment: Qt.AlignVCenter
                        Layout.preferredHeight: ScreenTools.defaultFontPixelHeight * 0.62
                        Layout.preferredWidth: Layout.preferredHeight
                        color: telemetryRow.modelData.active ? "#28c76f" : "#df3b3b"
                        radius: width / 2
                        visible: telemetryRow.modelData.algorithm === true
                    }

                    QGCLabel {
                        Layout.preferredWidth: telemetryRow.modelData.algorithm ?
                                                   ScreenTools.defaultFontPixelWidth * 15.5 :
                                                   ScreenTools.defaultFontPixelWidth * 5.5
                        color: qgcPal.text
                        font.bold: true
                        font.pointSize: ScreenTools.defaultFontPointSize
                        text: telemetryRow.modelData.label
                    }

                    QGCLabel {
                        Layout.fillWidth: true
                        color: telemetryRow.modelData.warning ? qgcPal.warningText : qgcPal.text
                        elide: Text.ElideRight
                        font.pointSize: ScreenTools.defaultFontPointSize
                        horizontalAlignment: Text.AlignLeft
                        text: telemetryRow.modelData.value
                    }
                }
            }

            QGCButton {
                Layout.fillWidth: true
                enabled: MultiDetectState.safetyAllowed
                primary: true
                text: qsTr("确认")
                visible: MultiDetectState.authorizationChallengeActive

                onClicked: authorizationPopup.open()
            }
        }
    }

    Rectangle {
        id: actionBar

        anchors.bottom: parent.bottom
        anchors.bottomMargin: parentToolInsets.bottomEdgeCenterInset + root._margin
        anchors.horizontalCenter: parent.horizontalCenter
        border.color: qgcPal.windowShadeDark
        border.width: 1
        color: qgcPal.window
        height: actionRow.implicitHeight + root._margin * 1.25
        opacity: 0.92
        radius: ScreenTools.defaultBorderRadius
        width: actionRow.implicitWidth + root._margin * 2
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
        border.color: "white"
        border.width: 3
        color: MultiDetectState.mode3AimUiActive ? "#c62828" : "#6d1b1b"
        height: mode3AimBannerContent.implicitHeight + root._margin * 1.5
        opacity: 0.96
        radius: Math.max(ScreenTools.defaultBorderRadius, 8)
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
                    color: "white"
                    font.bold: true
                    font.pointSize: ScreenTools.largeFontPointSize + 3
                    text: MultiDetectState.mode3AimUiActive ?
                              (MultiDetectState.approachPhase === "ABORT" ?
                                   qsTr("模式 3 瞄准状态异常") :
                               !MultiDetectState.approachStatusFresh ||
                               MultiDetectState.metadataLinkState === "STALE" ?
                                   qsTr("模式 3 瞄准状态待确认") :
                                   qsTr("模式 3 正在瞄准")) :
                              qsTr("遥控输入已接管 · 瞄准已取消")
                }

                QGCLabel {
                    Layout.fillWidth: true
                    color: "white"
                    font.bold: true
                    font.pointSize: ScreenTools.defaultFontPointSize
                    text: MultiDetectState.mode3AimUiActive ?
                              qsTr("遥控器任意按钮、拨杆或右侧按钮均可立即取消") :
                              qsTr("目标保持 LCK")
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

    Popup {
        id: payloadActionPopup

        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        focus: true
        modal: true
        parent: Overlay.overlay
        width: Math.min(root.width * 0.64, ScreenTools.defaultFontPixelWidth * 62)
        x: Math.round((parent.width - width) * 0.5)
        y: Math.round((parent.height - height) * 0.5)

        background: Rectangle {
            border.color: qgcPal.windowShadeDark
            border.width: 1
            color: qgcPal.window
            radius: ScreenTools.defaultBorderRadius
        }

        contentItem: ColumnLayout {
            spacing: root._margin

            QGCLabel {
                Layout.fillWidth: true
                font.bold: true
                font.pointSize: ScreenTools.largeFontPointSize
                text: qsTr("模式 2 · 执行")
            }

            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("当前目标：") + (MultiDetectState.targetId === "" ? qsTr("等待 Jetson 确认") :
                      MultiDetectState.targetLabel + " · " + MultiDetectState.targetId)
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                color: MultiDetectState.payloadTargetChallengeActive ? qgcPal.warningText : qgcPal.text
                font.bold: true
                text: !MultiDetectState.vehicleArmed ? qsTr("请先 ARM") :
                      MultiDetectState.payloadTargetChallengeActive ?
                          (ScreenTools.isMobile ? qsTr("滑动确认") : qsTr("确认执行")) +
                          " · " + MultiDetectState.payloadTargetChallengeExpiresInS + " s" :
                          MultiDetectState.payloadTargetEligibility
                wrapMode: Text.WordWrap
            }

            Slider {
                id: payloadTargetSlide

                property bool continuousEvidence: false
                property double pressedAtMs: 0
                property real previousValue: 0.0

                Layout.fillWidth: true
                enabled: MultiDetectState.vehicleArmed &&
                         MultiDetectState.payloadTargetChallengeActive
                visible: ScreenTools.isMobile
                from: 0.0
                live: true
                to: 1.0
                value: 0.0

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

                background: Rectangle {
                    color: qgcPal.windowShadeDark
                    height: Math.max(4, ScreenTools.defaultFontPixelHeight * 0.28)
                    radius: height / 2
                    x: payloadTargetSlide.leftPadding
                    y: payloadTargetSlide.topPadding + payloadTargetSlide.availableHeight / 2 - height / 2
                    width: payloadTargetSlide.availableWidth

                    Rectangle {
                        color: qgcPal.warningText
                        height: parent.height
                        radius: parent.radius
                        width: payloadTargetSlide.visualPosition * parent.width
                    }
                }

                handle: Rectangle {
                    border.color: qgcPal.windowShadeDark
                    border.width: 1
                    color: payloadTargetSlide.enabled ? qgcPal.warningText : qgcPal.button
                    height: ScreenTools.defaultFontPixelHeight * 1.35
                    radius: height / 2
                    width: height
                    x: payloadTargetSlide.leftPadding +
                       payloadTargetSlide.visualPosition * (payloadTargetSlide.availableWidth - width)
                    y: payloadTargetSlide.topPadding + payloadTargetSlide.availableHeight / 2 - height / 2
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: !ScreenTools.isMobile

                Item { Layout.fillWidth: true }

                QGCButton {
                    text: qsTr("取消")
                    onClicked: payloadActionPopup.close()
                }

                QGCButton {
                    enabled: MultiDetectState.vehicleArmed &&
                             MultiDetectState.payloadTargetChallengeActive && root._targetActionable
                    primary: true
                    text: qsTr("确认")
                    onClicked: {
                        if (MultiDetectState.confirmPayloadTargetSlide(600, 1.0, true))
                            payloadActionPopup.close();
                    }
                }
            }

            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("手动投放：") + MultiDetectState.rcReleaseState +
                      (MultiDetectState.rcReleaseChannel > 0 ? qsTr(" · 通道 %1").arg(MultiDetectState.rcReleaseChannel) : "")
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.alignment: Qt.AlignRight

                QGCButton {
                    text: ScreenTools.isMobile ? qsTr("取消") : qsTr("关闭")
                    visible: ScreenTools.isMobile
                    onClicked: payloadActionPopup.close()
                }

                QGCButton {
                    enabled: MultiDetectState.safetyAllowed && MultiDetectState.authorizationChallengeActive
                    primary: true
                    text: qsTr("进入任务授权")
                    visible: MultiDetectState.authorizationChallengeActive

                    onClicked: {
                        payloadActionPopup.close();
                        authorizationPopup.open();
                    }
                }
            }
        }
    }

    Popup {
        id: approachActionPopup

        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        focus: true
        modal: true
        parent: Overlay.overlay
        width: Math.min(root.width * 0.64, ScreenTools.defaultFontPixelWidth * 62)
        x: Math.round((parent.width - width) * 0.5)
        y: Math.round((parent.height - height) * 0.5)

        background: Rectangle {
            border.color: qgcPal.windowShadeDark
            border.width: 1
            color: qgcPal.window
            radius: ScreenTools.defaultBorderRadius
        }

        contentItem: ColumnLayout {
            spacing: root._margin

            QGCLabel {
                Layout.fillWidth: true
                font.bold: true
                font.pointSize: ScreenTools.largeFontPointSize
                text: qsTr("载荷模块 · 执行")
            }

            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("当前目标：") + (MultiDetectState.targetId === "" ? qsTr("等待 Jetson 确认") :
                      MultiDetectState.targetLabel + " · " + MultiDetectState.targetId)
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                color: MultiDetectState.approachChallengeActive ? qgcPal.warningText : qgcPal.text
                font.bold: true
                text: MultiDetectState.approachChallengePromptText(root._targetActionable,
                                                                     ScreenTools.isMobile)
                wrapMode: Text.WordWrap
            }

            Slider {
                id: approachSlide

                property bool continuousEvidence: false
                property double pressedAtMs: 0
                property real previousValue: 0.0

                Layout.fillWidth: true
                enabled: MultiDetectState.vehicleArmed &&
                         MultiDetectState.approachChallengeActive && root._targetActionable
                visible: ScreenTools.isMobile
                from: 0.0
                live: true
                to: 1.0
                value: 0.0

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

                background: Rectangle {
                    color: qgcPal.windowShadeDark
                    height: Math.max(4, ScreenTools.defaultFontPixelHeight * 0.28)
                    radius: height / 2
                    x: approachSlide.leftPadding
                    y: approachSlide.topPadding + approachSlide.availableHeight / 2 - height / 2
                    width: approachSlide.availableWidth

                    Rectangle {
                        color: qgcPal.warningText
                        height: parent.height
                        radius: parent.radius
                        width: approachSlide.visualPosition * parent.width
                    }
                }

                handle: Rectangle {
                    border.color: qgcPal.windowShadeDark
                    border.width: 1
                    color: approachSlide.enabled ? qgcPal.warningText : qgcPal.button
                    height: ScreenTools.defaultFontPixelHeight * 1.35
                    radius: height / 2
                    width: height
                    x: approachSlide.leftPadding + approachSlide.visualPosition * (approachSlide.availableWidth - width)
                    y: approachSlide.topPadding + approachSlide.availableHeight / 2 - height / 2
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: !ScreenTools.isMobile

                Item { Layout.fillWidth: true }

                QGCButton {
                    text: qsTr("取消")
                    onClicked: approachActionPopup.close()
                }

                QGCButton {
                    enabled: MultiDetectState.vehicleArmed &&
                             MultiDetectState.approachChallengeActive && root._targetActionable
                    primary: true
                    text: qsTr("确认")
                    onClicked: {
                        if (MultiDetectState.confirmApproachSlide(600, 1.0, true))
                            approachActionPopup.close();
                    }
                }
            }

            QGCButton {
                Layout.alignment: Qt.AlignRight
                text: qsTr("取消")
                visible: ScreenTools.isMobile
                onClicked: approachActionPopup.close()
            }
        }
    }

    Popup {
        id: authorizationPopup

        closePolicy: Popup.CloseOnEscape
        focus: true
        modal: true
        parent: Overlay.overlay
        width: Math.min(root.width * 0.62, ScreenTools.defaultFontPixelWidth * 58)
        x: Math.round((parent.width - width) * 0.5)
        y: Math.round((parent.height - height) * 0.5)

        background: Rectangle {
            border.color: qgcPal.windowShadeDark
            border.width: 1
            color: qgcPal.window
            radius: ScreenTools.defaultBorderRadius
        }
        contentItem: ColumnLayout {
            spacing: root._margin

            QGCLabel {
                Layout.fillWidth: true
                font.bold: true
                font.pointSize: ScreenTools.largeFontPointSize
                text: qsTr("任务授权")
            }

            QGCLabel {
                Layout.fillWidth: true
                text: qsTr("目标绑定：") + MultiDetectState.authorizationBinding
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                color: MultiDetectState.safetyAllowed ? qgcPal.colorGreen : qgcPal.warningText
                text: qsTr("安全规则：") + MultiDetectState.safetyPassCount + qsTr(" 通过，") + MultiDetectState.safetyDenyCount + qsTr(" 拒绝，") + MultiDetectState.safetyUnknownCount + qsTr(" 未知")
                wrapMode: Text.WordWrap
            }

            QGCLabel {
                Layout.fillWidth: true
                color: qgcPal.warningText
                font.bold: true
                text: qsTr("授权只返回 Jetson 任务状态机；物理投放由遥控器开关请求，当前输出仍锁定。")
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.alignment: Qt.AlignRight

                QGCButton {
                    text: qsTr("拒绝")

                    onClicked: {
                        MultiDetectState.denyAuthorization();
                        authorizationPopup.close();
                    }
                }

                QGCButton {
                    enabled: MultiDetectState.safetyAllowed && MultiDetectState.authorizationChallengeActive
                    primary: true
                    text: qsTr("批准任务")

                    onClicked: {
                        MultiDetectState.approveAuthorization();
                        authorizationPopup.close();
                    }
                }
            }
        }
    }
}

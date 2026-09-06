pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

import Custom.MultiDetect
import QGroundControl
import QGroundControl.Controls

// Multi-Detect's persistent status belongs in QGC's native toolbar.  The Fly
// View remains reserved for spatial annotations and operator actions; details
// use the same ToolIndicatorPage drawer lifecycle as GPS, battery and telemetry.
Item {
    id: control

    objectName: "toolbar_multiDetectIndicator"
    Accessible.name: "MultiDetect AI Status"
    Accessible.description: qsTr("Open Multi-Detect runtime and perception status")
    Accessible.id: objectName
    Accessible.role: Accessible.Button
    Accessible.focusable: true
    Accessible.ignored: false
    Accessible.onPressAction: control._openIndicatorDrawer()
    activeFocusOnTab: true
    anchors.bottom: parent.bottom
    anchors.top: parent.top
    visible: showIndicator
    width: indicatorRow.implicitWidth

    property bool showIndicator: true

    // Acceptance-only native scene telemetry. It never creates or styles a
    // visual surface: records describe this existing toolbar Item and QGC's
    // existing ToolIndicatorPage Popup background. A single resident timer
    // debounces layout churn so every record represents settled QML geometry.
    property int _geometrySequence: 0
    property var _geometryPending: []
    property var _nativeIndicatorDrawer: null
    // FlyViewToolBar places this Loader below QGC's native horizontal
    // Flickable.  The Loader item's x/width can remain unchanged while the
    // Flickable's contentX changes as vehicle indicators are created or
    // hidden.  Keep the acceptance-only export bound to that scroll source so
    // screen input never consumes a stale scene rectangle.
    property var _geometryFlickable: null
    property int _geometryHeartbeatTicks: 0
    readonly property int _geometryHeartbeatLimit: 120

    readonly property bool _geometryExportEnabled: multiDetectHilUiGeometryExport

    readonly property bool _jetsonOnline: MultiDetectState.metadataLinkState === "AUTHENTICATED"
    readonly property bool _runtimeWarning: MultiDetectState.runtimeDisplayMode === "MISMATCH" ||
                                            MultiDetectState.runtimeDisplayMode === "UNKNOWN" ||
                                            MultiDetectState.runtimeBackendHealth === "UNHEALTHY" ||
                                            MultiDetectState.runtimeBackendHealth === "DEGRADED" ||
                                            MultiDetectState.runtimeBackendHealth === "UNKNOWN"
    readonly property color _healthColor: _runtimeWarning ? qgcPal.warningText :
                                          MultiDetectState.runtimeEnvironmentHealthy ? qgcPal.colorGreen :
                                          qgcPal.warningText
    readonly property string _missionModeCode: MultiDetectState.missionMode === "PAYLOAD" ? "M2" :
                                               MultiDetectState.missionMode === "OBSERVE" ? "M3" : "M1"

    QGCPalette {
        id: qgcPal

        colorGroupEnabled: true
    }

    function _number(value, precision, suffix) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(precision) + suffix : "--";
    }

    function _openIndicatorDrawer() {
        mainWindow.showIndicatorDrawer(indicatorPage, control);
    }

    function _findGeometryFlickable() {
        let current = control.parent;
        for (let depth = 0; depth < 12 && current; ++depth) {
            try {
                if (current.contentItem !== undefined &&
                        Number.isFinite(Number(current.contentX)) &&
                        Number.isFinite(Number(current.contentWidth)))
                    return current;
            } catch (error) {
                // A transient Loader parent can disappear while QGC changes
                // views. Continue walking until the native Flickable exists.
            }
            current = current.parent;
        }
        return null;
    }

    function _bindGeometryFlickable() {
        if (!_geometryFlickable)
            _geometryFlickable = _findGeometryFlickable();
    }

    function _devicePixelRatio() {
        let ratio = Number(mainWindow.devicePixelRatio);
        if (!Number.isFinite(ratio) || ratio <= 0.0)
            ratio = Number(mainWindow.screen ? mainWindow.screen.devicePixelRatio : 1.0);
        return Number.isFinite(ratio) && ratio > 0.0 ? ratio : 1.0;
    }

    function _queueGeometry(item, role, state, itemName, accessibleName,
                            accessibleRole, accessibleId) {
        if (!_geometryExportEnabled || !item)
            return;

        // Replace only the same lifecycle role. This coalesces repeated
        // width/x/scene changes without losing open and closed transitions.
        const pending = _geometryPending.slice();
        let replaced = false;
        for (let index = 0; index < pending.length; ++index) {
            if (pending[index].role === role) {
                pending[index] = {
                    "item": item,
                    "role": role,
                    "state": state,
                    "itemName": itemName,
                    "accessibleName": accessibleName,
                    "accessibleRole": accessibleRole,
                    "accessibleId": accessibleId
                };
                replaced = true;
                break;
            }
        }
        if (!replaced) {
            pending.push({
                "item": item,
                "role": role,
                "state": state,
                "itemName": itemName,
                "accessibleName": accessibleName,
                "accessibleRole": accessibleRole,
                "accessibleId": accessibleId
            });
        }
        _geometryPending = pending;
        geometryDebounce.restart();
    }

    function _queueIndicatorGeometry() {
        _queueGeometry(control, "indicator_ready", "visible", control.objectName,
                       "MultiDetect AI Status", "Button", control.objectName);
    }

    function _queueDrawerGeometry(role, state) {
        const drawer = _nativeIndicatorDrawer;
        if (!drawer || !drawer.background)
            return;
        _queueGeometry(drawer.background, role, state,
                       drawer.background.objectName || "indicatorDrawer.background",
                       "MultiDetect AI Status Drawer", "Pane",
                       "toolbar_multiDetectIndicatorDrawer");
    }

    function _emitGeometry(record) {
        if (!_geometryExportEnabled || !record || !record.item ||
                !mainWindow || !mainWindow.contentItem)
            return;

        const item = record.item;
        const width = Number(item.width);
        const height = Number(item.height);
        const sceneWidth = Number(mainWindow.contentItem.width);
        const sceneHeight = Number(mainWindow.contentItem.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) ||
                !Number.isFinite(sceneWidth) || !Number.isFinite(sceneHeight) ||
                width <= 0.0 || height <= 0.0 ||
                sceneWidth <= 0.0 || sceneHeight <= 0.0)
            return;

        const topLeft = item.mapToItem(mainWindow.contentItem, 0, 0);
        const bottomRight = item.mapToItem(mainWindow.contentItem, width, height);
        const left = Math.min(Number(topLeft.x), Number(bottomRight.x));
        const top = Math.min(Number(topLeft.y), Number(bottomRight.y));
        const right = Math.max(Number(topLeft.x), Number(bottomRight.x));
        const bottom = Math.max(Number(topLeft.y), Number(bottomRight.y));
        if (![left, top, right, bottom].every(Number.isFinite) ||
                right <= left || bottom <= top)
            return;

        _geometrySequence += 1;
        const payload = {
            "schema": "multidetect.qgc-hil-ui-geometry",
            "schema_version": 1,
            "sequence": _geometrySequence,
            "role": record.role,
            "state": record.state,
            "item_object_name": record.itemName || "",
            "visible": Boolean(item.visible),
            "enabled": Boolean(item.enabled),
            "opacity": Number(item.opacity),
            "scene_rect": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": right - left,
                "height": bottom - top
            },
            "window_scene_size": {
                "width": sceneWidth,
                "height": sceneHeight
            },
            "device_pixel_ratio": _devicePixelRatio(),
            "emitted_at_epoch_ms": Date.now(),
            "accessible_name": record.accessibleName,
            "accessible_role": record.accessibleRole,
            "accessible_id": record.accessibleId
        };
        console.info("MULTIDETECT_HIL_UI_GEOMETRY " + JSON.stringify(payload));
    }

    function _emitLifecycle(role, state, itemName, accessibleName,
                            accessibleRole, accessibleId) {
        if (!_geometryExportEnabled || !mainWindow || !mainWindow.contentItem)
            return;

        const sceneWidth = Number(mainWindow.contentItem.width);
        const sceneHeight = Number(mainWindow.contentItem.height);
        if (!Number.isFinite(sceneWidth) || !Number.isFinite(sceneHeight) ||
                sceneWidth <= 0.0 || sceneHeight <= 0.0)
            return;

        _geometrySequence += 1;
        const payload = {
            "schema": "multidetect.qgc-hil-ui-geometry",
            "schema_version": 1,
            "sequence": _geometrySequence,
            "role": role,
            "state": state,
            "item_object_name": itemName || "",
            "visible": false,
            "enabled": false,
            "opacity": 0.0,
            "scene_rect": null,
            "window_scene_size": {
                "width": sceneWidth,
                "height": sceneHeight
            },
            "device_pixel_ratio": _devicePixelRatio(),
            "emitted_at_epoch_ms": Date.now(),
            "accessible_name": accessibleName,
            "accessible_role": accessibleRole,
            "accessible_id": accessibleId
        };
        console.info("MULTIDETECT_HIL_UI_GEOMETRY " + JSON.stringify(payload));
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

    function _runtimeDetailText() {
        const mode = MultiDetectState.runtimeDisplayMode;
        if (mode === "MISMATCH") {
            return qsTr("环境 MISMATCH · 期望 %1 / 收到 %2")
                    .arg(MultiDetectState.expectedRuntimeMode || "--")
                    .arg(MultiDetectState.runtimeRemoteMode || "UNKNOWN");
        }
        if (mode === "UNKNOWN")
            return qsTr("环境 UNKNOWN · 签名状态缺失、过期或配置无效");
        if (MultiDetectState.replayRuntimeIdentity)
            return qsTr("环境 REPLAY · 只读回放 · 车辆链路/执行/授权已隔离");
        if (MultiDetectState.vehicleLinkIsolationActive)
            return qsTr("环境 %1 · 车辆链路/执行/授权已隔离").arg(mode);
        return qsTr("环境 %1 · %2").arg(mode).arg(MultiDetectState.runtimeBackendHealth);
    }

    function _runtimeHealthShortText() {
        if (MultiDetectState.runtimeDisplayMode === "MISMATCH")
            return qsTr("不匹配");
        if (MultiDetectState.runtimeDisplayMode === "UNKNOWN")
            return qsTr("未知");
        if (MultiDetectState.runtimeEnvironmentHealthy)
            return qsTr("正常");
        return MultiDetectState.runtimeBackendHealth;
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
        if (!_jetsonOnline)
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
        if (!_jetsonOnline)
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

    // Keep the GPS/range health readout in the native QGC toolbar drawer. A
    // target card is intentionally not required here: during field bring-up
    // the operator needs to see why geolocation is unavailable before a
    // target has been selected. These values are diagnostic only and never
    // open a vehicle link or enable a flight action.
    function _gpsQualityValue() {
        if (!MultiDetectState.gnssStatusAvailable)
            return qsTr("不可定位 · 等待 GNSS_STATUS");

        const ageMs = Number(MultiDetectState.gnssSourceAgeMs);
        if (MultiDetectState.gnssQualified && Number.isFinite(ageMs))
            return qsTr("合格 · 源 %1 ms").arg(Math.round(ageMs));

        const reasonLabels = {
            "gps_position_unhealthy": qsTr("位置健康异常"),
            "gps_coordinates_invalid": qsTr("坐标无效"),
            "gps_fix_unqualified": qsTr("定位类型不足"),
            "gps_satellites_insufficient": qsTr("卫星数不足"),
            "gps_horizontal_accuracy_invalid": qsTr("水平精度无效"),
            "gps_horizontal_accuracy_exceeded": qsTr("水平误差过大"),
            "gps_vertical_accuracy_invalid": qsTr("垂直精度无效"),
            "gps_vertical_accuracy_exceeded": qsTr("垂直误差过大"),
            "gps_timestamp_unavailable": qsTr("时间戳不可用"),
            "gps_timestamp_future": qsTr("时间戳超前"),
            "gps_stale": qsTr("数据过期")
        };
        const reason = String(MultiDetectState.gnssReason || "");
        return qsTr("不可定位 · ") + (reasonLabels[reason] || qsTr("等待合格 GPS"));
    }

    function _gnssDetailValue() {
        if (!MultiDetectState.gnssStatusAvailable)
            return qsTr("fix -- · 卫星 -- · H/V -- · 年龄 --");
        const fix = MultiDetectState.gnssFixType >= 0 ? MultiDetectState.gnssFixType : "--";
        const satellites = MultiDetectState.gnssSatellitesVisible >= 0 ? MultiDetectState.gnssSatellitesVisible : "--";
        const horizontal = MultiDetectState.gnssHorizontalAccuracyM >= 0.0 ?
                MultiDetectState.gnssHorizontalAccuracyM.toFixed(1) + " m" : "--";
        const vertical = MultiDetectState.gnssVerticalAccuracyM >= 0.0 ?
                MultiDetectState.gnssVerticalAccuracyM.toFixed(1) + " m" : "--";
        const age = Number.isFinite(MultiDetectState.gnssSourceAgeMs) ?
                Math.round(MultiDetectState.gnssSourceAgeMs) + " ms" : "--";
        return "fix " + fix + " · " + qsTr("卫星 ") + satellites +
                " · H/V " + horizontal + "/" + vertical + " · " + age;
    }

    function _producerEpochValue() {
        const epoch = Number(MultiDetectState.targetObservationProducerEpoch);
        if (!Number.isFinite(epoch) || epoch <= 0)
            return qsTr("等待原子目标观测");
        const changedAtMs = Number(MultiDetectState.targetObservationEpochChangedAtMs);
        const changedAt = Number.isFinite(changedAtMs) && changedAtMs > 0 ?
                new Date(changedAtMs).toLocaleTimeString(Qt.locale(), "HH:mm:ss") : "--";
        return qsTr("%1 · epoch %2 · 重启 %3 · 旧包拒绝 %4 · 切换拒绝 %5 · %6").arg(
                    _producerEpochHealthText()).arg(Math.round(epoch)).arg(
                    Math.round(MultiDetectState.targetObservationProducerRestartCount)).arg(
                    Math.round(MultiDetectState.targetObservationRetiredEpochRejectCount)).arg(
                    Math.round(MultiDetectState.targetObservationEpochTransitionRejectCount)).arg(changedAt);
    }

    function _producerEpochHealthText() {
        const level = String(MultiDetectState.targetObservationEpochHealthLevel || "WAITING").toUpperCase();
        if (level === "CRITICAL")
            return qsTr("严重：epoch 切换容量耗尽，重启 QGC");
        if (level === "WARNING")
            return qsTr("警告：已拒绝退休 epoch 旧包");
        if (level === "NOTICE")
            return qsTr("注意：已观察到生产者重启");
        if (level === "HEALTHY")
            return qsTr("正常");
        return qsTr("等待");
    }

    function _producerEpochHealthColor() {
        const level = String(MultiDetectState.targetObservationEpochHealthLevel || "WAITING").toUpperCase();
        if (level === "CRITICAL")
            return qgcPal.colorRed;
        if (level === "WARNING")
            return qgcPal.colorOrange;
        if (level === "NOTICE")
            return qgcPal.colorYellow;
        if (level === "HEALTHY")
            return qgcPal.colorGreen;
        return qgcPal.text;
    }

    function _rangeStatusValue() {
        const validity = String(MultiDetectState.rangeValidity || "UNAVAILABLE").toUpperCase();
        if (validity === "UNAVAILABLE")
            return qsTr("不可用 · 等待 RANGE_STATUS");

        const sourceAge = Number(MultiDetectState.rangeSourceAgeMs);
        const dataAge = Number(MultiDetectState.rangeDataFreshnessS);
        const freshness = MultiDetectState.rangeStatusFresh ? qsTr("新鲜") : qsTr("过期");
        return validity + " · " + freshness +
                " · 源 " + (Number.isFinite(sourceAge) && sourceAge >= 0.0 ?
                    Math.round(sourceAge) + " ms" : "--") +
                " · 数据 " + (Number.isFinite(dataAge) && dataAge >= 0.0 ?
                    dataAge.toFixed(1) + " s" : "--");
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
               (sources.indexOf("monocular_metric") >= 0 && MultiDetectState.depthMapAvailable);
    }

    function _activeAlgorithmCount() {
        let activeCount = 0;
        if (_rangingAlgorithmActive(["monocular_metric", "monocular_size"]))
            activeCount += 1;
        if (_rangingAlgorithmActive(["vio"]))
            activeCount += 1;
        if (_rangingAlgorithmActive(["rgb_slam"]))
            activeCount += 1;
        if (_rangingAlgorithmActive(["camera_ground", "pixhawk_agl"]))
            activeCount += 1;
        return activeCount;
    }

    Timer {
        id: geometryDebounce

        interval: 100
        repeat: false
        onTriggered: {
            const pending = control._geometryPending;
            control._geometryPending = [];
            for (let index = 0; index < pending.length; ++index)
                control._emitGeometry(pending[index]);
        }
    }

    Connections {
        target: control._nativeIndicatorDrawer
        enabled: control._geometryExportEnabled && target !== null
        ignoreUnknownSignals: true

        function onOpened() {
            control._queueDrawerGeometry("drawer_open", "visible");
        }

        function onClosed() {
            // MainWindow clears the ToolIndicatorPage Loader in its own
            // Popup.onClosed handler. Emit the close lifecycle synchronously
            // so it cannot be lost when the native Popup background shrinks.
            control._emitLifecycle(
                        "drawer_closed", "closed",
                        "indicatorDrawer.background",
                        "MultiDetect AI Status Drawer", "Pane",
                        "toolbar_multiDetectIndicatorDrawer");
        }

        function onXChanged() {
            if (target.visible)
                control._queueDrawerGeometry("drawer_open", "visible");
        }

        function onYChanged() {
            if (target.visible)
                control._queueDrawerGeometry("drawer_open", "visible");
        }

        function onWidthChanged() {
            if (target.visible)
                control._queueDrawerGeometry("drawer_open", "visible");
        }

        function onHeightChanged() {
            if (target.visible)
                control._queueDrawerGeometry("drawer_open", "visible");
        }

        function onVisibleChanged() {
            if (target.visible)
                control._queueDrawerGeometry("drawer_open", "visible");
        }
    }

    Connections {
        target: mainWindow.contentItem
        enabled: control._geometryExportEnabled
        ignoreUnknownSignals: true

        function onWidthChanged() {
            control._queueIndicatorGeometry();
        }

        function onHeightChanged() {
            control._queueIndicatorGeometry();
        }
    }

    Connections {
        target: mainWindow
        enabled: control._geometryExportEnabled
        ignoreUnknownSignals: true

        function onDevicePixelRatioChanged() {
            control._queueIndicatorGeometry();
        }
    }

    // `contentX` is a transform on an ancestor rather than a property change
    // on this Loader item.  Observe it explicitly and keep the export
    // fail-closed when the native toolbar is horizontally re-laid out.
    Connections {
        target: control._geometryFlickable
        enabled: control._geometryExportEnabled && target !== null
        ignoreUnknownSignals: true

        function onContentXChanged() {
            control._queueIndicatorGeometry();
        }

        function onContentWidthChanged() {
            control._queueIndicatorGeometry();
        }

        function onWidthChanged() {
            control._queueIndicatorGeometry();
        }
    }

    // Acceptance runs enable the read-only geometry export through an
    // environment opt-in. Emit a bounded heartbeat while QGC settles its
    // native toolbar. This covers parent transforms that do not propagate an
    // `xChanged` signal to the Loader item (notably Flickable contentX).
    Timer {
        id: geometryHeartbeat

        interval: 200
        repeat: true
        running: control._geometryExportEnabled && control.visible &&
                 control._geometryHeartbeatTicks < control._geometryHeartbeatLimit

        onTriggered: {
            control._geometryHeartbeatTicks += 1;
            control._bindGeometryFlickable();
            control._queueIndicatorGeometry();
        }
    }

    Component.onCompleted: {
        _bindGeometryFlickable();
        _queueIndicatorGeometry();
    }
    onXChanged: _queueIndicatorGeometry()
    onYChanged: _queueIndicatorGeometry()
    onWidthChanged: _queueIndicatorGeometry()
    onHeightChanged: _queueIndicatorGeometry()
    onVisibleChanged: {
        if (visible)
            _geometryHeartbeatTicks = 0;
        _queueIndicatorGeometry();
    }
    onEnabledChanged: _queueIndicatorGeometry()
    onOpacityChanged: _queueIndicatorGeometry()

    RowLayout {
        id: indicatorRow

        anchors.bottom: parent.bottom
        anchors.top: parent.top
        spacing: ScreenTools.defaultFontPixelWidth / 2

        // Use the same first-party icon and sizing convention as the stock
        // QGC toolbar indicators (GPS/FlightMode/Telemetry).  The previous
        // status dot was a private visual language; keeping the health color
        // on the icon/text preserves the signal without introducing a second
        // theme.
        QGCColoredImage {
            id: aiIcon

            Layout.alignment: Qt.AlignVCenter
            Layout.preferredHeight: ScreenTools.defaultFontPixelHeight
            Layout.preferredWidth: ScreenTools.defaultFontPixelWidth * 3
            color: control._healthColor
            fillMode: Image.PreserveAspectFit
            mipmap: true
            source: "/qmlimages/Analyze.svg"
            sourceSize.height: height
        }

        ColumnLayout {
            Layout.alignment: Qt.AlignVCenter
            spacing: 0

            QGCLabel {
                color: qgcPal.text
                font.pointSize: ScreenTools.smallFontPointSize
                text: "AI " + MultiDetectState.runtimeDisplayMode
            }

            QGCLabel {
                color: control._healthColor
                font.pointSize: ScreenTools.smallFontPointSize
                text: control._runtimeHealthShortText()
            }
        }
    }

    MouseArea {
        anchors.fill: parent
        onClicked: control._openIndicatorDrawer()
    }

    Component {
        id: indicatorPage

        ToolIndicatorPage {
            Accessible.name: "MultiDetect AI Status Drawer"
            Accessible.id: "toolbar_multiDetectIndicatorDrawer"
            Accessible.role: Accessible.Pane
            Accessible.ignored: false
            showExpand: true

            onDrawerChanged: {
                if (!drawer)
                    return;
                control._nativeIndicatorDrawer = drawer;
                // MainWindow creates ToolIndicatorPage from Popup.onOpened,
                // therefore this binding can arrive after Popup.opened. Emit
                // from the bound native Popup.background once settled instead
                // of waiting for a signal which has already fired.
                if (drawer.visible)
                    control._queueDrawerGeometry("drawer_open", "visible");
            }

            contentComponent: Component {
                SettingsGroupLayout {
                    heading: qsTr("Multi-Detect 状态")
                    showDividers: true

                    QGCLabel {
                        Layout.fillWidth: true
                        color: control._healthColor
                        font.bold: true
                        text: control._runtimeDetailText()
                        wrapMode: Text.WordWrap
                    }

                    LabelledLabel {
                        label: qsTr("签名链路")
                        labelText: control._metadataLabel()
                    }

                    LabelledLabel {
                        label: qsTr("目标")
                        labelText: control._targetText()
                    }

                    LabelledLabel {
                        label: qsTr("跟踪")
                        labelText: control._trackingText()
                    }

                    LabelledLabel {
                        label: qsTr("融合")
                        labelText: MultiDetectState.rangeFusionText()
                    }

                    LabelledLabel {
                        label: qsTr("算法")
                        labelText: qsTr("%1 / 4 工作中").arg(control._activeAlgorithmCount())
                    }
                }
            }

            expandedComponent: Component {
                RowLayout {
                    spacing: ScreenTools.defaultFontPixelWidth

                    ColumnLayout {
                        Layout.alignment: Qt.AlignTop
                        spacing: ScreenTools.defaultFontPixelHeight * 0.5

                        SettingsGroupLayout {
                            heading: qsTr("运行环境")
                            showDividers: true

                            LabelledLabel {
                                label: qsTr("模式")
                                labelText: MultiDetectState.runtimeDisplayMode
                            }

                            LabelledLabel {
                                label: qsTr("后端健康")
                                labelText: MultiDetectState.runtimeBackendHealth
                            }

                            LabelledLabel {
                                label: qsTr("运行档案")
                                labelText: MultiDetectState.runtimeProfileValid ? qsTr("有效") : qsTr("无效")
                            }

                            LabelledLabel {
                                label: qsTr("任务状态")
                                labelText: control._missionModeCode + " · " + MultiDetectState.interactionState +
                                           " · " + MultiDetectState.missionPhase
                            }

                            LabelledLabel {
                                label: qsTr("物理动作")
                                labelText: MultiDetectState.physicalActionIsolationActive ?
                                               qsTr("已隔离") : qsTr("按授权门控")
                            }
                        }

                        SettingsGroupLayout {
                            heading: qsTr("Jetson · AI")
                            showDividers: true

                            LabelledLabel {
                                label: qsTr("识别")
                                labelText: control._recognitionText()
                            }

                            LabelledLabel {
                                label: qsTr("数据")
                                labelText: control._dataText()
                            }

                            LabelledLabel {
                                label: qsTr("场景")
                                labelText: control._sceneText()
                            }

                            LabelledLabel {
                                label: qsTr("Jetson 状态")
                                labelText: MultiDetectState.statusMessage
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.alignment: Qt.AlignTop
                        spacing: ScreenTools.defaultFontPixelHeight * 0.5

                        SettingsGroupLayout {
                            heading: qsTr("室外测距质量")
                            showDividers: true

                            LabelledLabel {
                                label: qsTr("GPS/GNSS")
                                labelText: control._gpsQualityValue()
                            }

                            LabelledLabel {
                                label: qsTr("GNSS 详情")
                                labelText: control._gnssDetailValue()
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: ScreenTools.defaultFontPixelWidth * 2

                                QGCLabel {
                                    Layout.fillWidth: true
                                    text: qsTr("观测生产者")
                                }

                                QGCLabel {
                                    color: control._producerEpochHealthColor()
                                    font.bold: MultiDetectState.targetObservationEpochHealthLevel === "CRITICAL" ||
                                               MultiDetectState.targetObservationEpochHealthLevel === "WARNING"
                                    text: control._producerEpochValue()
                                }
                            }

                            LabelledLabel {
                                label: qsTr("RANGE_STATUS")
                                labelText: control._rangeStatusValue()
                            }

                            LabelledLabel {
                                label: "Depth Anything V2"
                                labelText: control._rangingAlgorithmValue(
                                               ["monocular_metric", "monocular_size"], qsTr("等待深度"))
                            }

                            LabelledLabel {
                                label: "VIO-SLAM"
                                labelText: control._rangingAlgorithmValue(["vio"], qsTr("等待运动"))
                            }

                            LabelledLabel {
                                label: "RGB-SLAM"
                                labelText: control._rangingAlgorithmValue(["rgb_slam"], qsTr("等待视差"))
                            }

                            LabelledLabel {
                                label: qsTr("PX4 地面几何")
                                labelText: control._rangingAlgorithmValue(
                                               ["camera_ground", "pixhawk_agl"], qsTr("等待高度"))
                            }

                            LabelledLabel {
                                label: qsTr("深度图")
                                labelText: MultiDetectState.depthMapAvailable ?
                                               qsTr("%1×%2 · %3 ms")
                                                   .arg(MultiDetectState.depthGridWidth)
                                                   .arg(MultiDetectState.depthGridHeight)
                                                   .arg(Math.max(0, Math.round(MultiDetectState.depthMapAgeMs))) :
                                               qsTr("不可用")
                            }

                            LabelledLabel {
                                label: qsTr("标定")
                                labelText: MultiDetectState.rangeCalibrationId !== "" ?
                                               MultiDetectState.rangeCalibrationId : qsTr("等待标定")
                            }
                        }
                    }
                }
            }
        }
    }
}

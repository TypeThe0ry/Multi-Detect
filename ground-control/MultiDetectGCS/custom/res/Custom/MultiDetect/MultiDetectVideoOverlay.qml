import QGroundControl
import QGroundControl.Controls
import QtQuick
import QtQuick.Controls

Item {
    id: root

    QGCPalette {
        id: qgcPal
    }

    Button {
        id: cancelActionSizer

        text: qsTr("取消")
        visible: false
    }

    QGCButton {
        id: lockActionSizer

        text: "LCK"
        textColor: root._lockActionTextColor
        visible: false
    }

    property real _dragCurrentX: 0
    property real _dragCurrentY: 0
    property real _dragStartX: 0
    property real _dragStartY: 0
    property bool _dragging: false
    property bool interactionEnabled: true
    property string selectionHintText: qsTr("框选目标")
    property bool showCompactStatus: true
    readonly property int depthDisplayMode: MultiDetectState.persistedSettings.depthDisplayMode // 0=off, 1=picture-in-picture, 2=overlay
    property real depthSampleX: -1
    property real depthSampleY: -1
    property string depthSampleText: ""
    readonly property real _actionButtonHeight: Math.max(26, ScreenTools.defaultFontPixelHeight * 1.55)
    readonly property real _actionSpacing: Math.max(2, ScreenTools.defaultFontPixelWidth * 0.3)
    readonly property real _cancelActionWidth: Math.max(54, cancelActionSizer.implicitWidth)
    readonly property real _lockActionWidth: Math.max(48, lockActionSizer.implicitWidth)
    // Reserve the top band for the native QGC toolbar and the fixed-wing
    // heading tape.  The Jetson panel owns upper-right; depth owns upper-left.
    readonly property real _topHudClearance: Math.max(72, ScreenTools.defaultFontPixelHeight * 4.6)
    // Red is reserved for the LCK action and the confirmed primary lock.  It
    // must not inherit the neutral Qt Button text colour, otherwise the action
    // is too easy to confuse with the nearby TRK/cancel controls.
    readonly property color _lockActionTextColor: "#ff3b30"
    // Keep the visual box cadence independent from the metadata cadence.  The
    // Jetson emits a complete target-pool snapshot at a bounded rate, while a
    // 720p RTSP frame can still arrive a little later than the advertised
    // cadence.  Filter against the measured target-pool rate and deliberately
    // run slightly past one source interval so the overlay remains continuous
    // instead of stopping between fresh boxes.
    readonly property real _boxMetadataRateHz: {
        const poolRate = Number(MultiDetectState.targetPoolMetadataRateHz);
        const trackRate = Number(MultiDetectState.trackingMetadataRateHz);
        const validPoolRate = Number.isFinite(poolRate) && poolRate > 0.0 ? poolRate : 0.0;
        const validTrackRate = Number.isFinite(trackRate) && trackRate > 0.0 ? trackRate : 0.0;
        return Math.max(validPoolRate, validTrackRate);
    }
    readonly property int _targetSmoothingDurationMs: {
        const rate = _boxMetadataRateHz;
        if (!Number.isFinite(rate) || rate <= 0.0)
            return 150;
        // A 95 ms floor made 20/30 Hz TRK/LCK snapshots chase two or three
        // historical boxes. Keep the easing just beyond one measured source
        // interval: it stays continuous at a low DET cadence but reaches the
        // latest high-rate LCK box before the next packet arrives.
        return Math.max(34, Math.min(220, Math.round(1150.0 / rate)));
    }

    // The controller replaces its QVariantList on every authenticated target
    // pool update.  Repeating that list directly destroys/recreates delegates,
    // which prevents QML behaviours from interpolating their geometry.  Keep a
    // small ID-stable render model so a target's Item survives its next box.
    ListModel {
        id: renderTargetPool
    }

    function _renderTargetIndex(targetId) {
        const expected = String(targetId || "");
        for (let index = 0; index < renderTargetPool.count; ++index) {
            if (String(renderTargetPool.get(index).targetId || "") === expected)
                return index;
        }
        return -1;
    }

    function _entryNumberFingerprint(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(6) : "--";
    }

    function _entryVisualFingerprint(entry) {
        if (entry === undefined || entry === null)
            return "";
        // TargetPoolStatus replaces the QVariantMap for every complete
        // snapshot.  Ignore fields that do not affect this overlay so a
        // stationary candidate does not restart its QML bindings/animations
        // at the 10/20/30 Hz transport cadence.
        return [
            String(entry.targetId || ""),
            String(entry.label || ""),
            String(entry.state || ""),
            entry.bboxValid === true ? "1" : "0",
            entry.operatorTracked === true ? "1" : "0",
            entry.locked === true ? "1" : "0",
            entry.primary === true ? "1" : "0",
            entry.actionable === true ? "1" : "0",
            _entryNumberFingerprint(entry.x1),
            _entryNumberFingerprint(entry.y1),
            _entryNumberFingerprint(entry.x2),
            _entryNumberFingerprint(entry.y2),
            _entryNumberFingerprint(entry.confidence),
            _entryNumberFingerprint(entry.trackingQuality),
            _entryNumberFingerprint(entry.estimatedRangeM),
            _entryNumberFingerprint(entry.relativeBearingDeg),
            _entryNumberFingerprint(entry.targetSpeedMps)
        ].join("\u001f");
    }

    function _syncRenderTargetPool() {
        const source = MultiDetectState.selectableTargetPoolEntries;
        const desired = [];
        if (source !== undefined && source !== null) {
            for (let index = 0; index < source.length; ++index) {
                const entry = source[index];
                if (entry === undefined || entry === null ||
                        String(entry.targetId || "") === "" ||
                        MultiDetectState.isDemotedPrimaryLockAwaitingPool(entry))
                    continue;
                desired.push({
                    targetId: String(entry.targetId),
                    entry: entry
                });
            }
        }

        for (let index = renderTargetPool.count - 1; index >= 0; --index) {
            const renderedId = String(renderTargetPool.get(index).targetId || "");
            let stillPresent = false;
            for (let desiredIndex = 0; desiredIndex < desired.length; ++desiredIndex) {
                if (desired[desiredIndex].targetId === renderedId) {
                    stillPresent = true;
                    break;
                }
            }
            if (!stillPresent)
                renderTargetPool.remove(index);
        }

        for (let index = 0; index < desired.length; ++index) {
            const desiredEntry = desired[index];
            const targetId = desiredEntry.targetId;
            const renderedIndex = _renderTargetIndex(targetId);
            const nowMs = Date.now();
            const incoming = desiredEntry.entry;
            const incomingRangeValid = Number.isFinite(Number(incoming.estimatedRangeM));
            const incomingSpeedValid = Number.isFinite(Number(incoming.targetSpeedMps));
            let displayEntry = incoming;
            let rangeUpdatedAtMs = incomingRangeValid ? nowMs : 0;
            let speedUpdatedAtMs = incomingSpeedValid ? nowMs : 0;
            if (renderedIndex >= 0) {
                const priorRendered = renderTargetPool.get(renderedIndex);
                const priorEntry = priorRendered.entry;
                rangeUpdatedAtMs = incomingRangeValid ? nowMs : Number(priorRendered.rangeUpdatedAtMs || 0);
                speedUpdatedAtMs = incomingSpeedValid ? nowMs : Number(priorRendered.speedUpdatedAtMs || 0);
                const keepRange = !incomingRangeValid && priorEntry !== undefined &&
                        Number.isFinite(Number(priorEntry.estimatedRangeM)) &&
                        nowMs - rangeUpdatedAtMs <= 1500;
                const keepSpeed = !incomingSpeedValid && priorEntry !== undefined &&
                        Number.isFinite(Number(priorEntry.targetSpeedMps)) &&
                        nowMs - speedUpdatedAtMs <= 1500;
                if (keepRange || keepSpeed) {
                    const merged = {};
                    for (const key in incoming)
                        merged[key] = incoming[key];
                    if (keepRange)
                        merged.estimatedRangeM = priorEntry.estimatedRangeM;
                    if (keepSpeed)
                        merged.targetSpeedMps = priorEntry.targetSpeedMps;
                    displayEntry = merged;
                }
            }
            const displayFingerprint = _entryVisualFingerprint(displayEntry);
            if (renderedIndex < 0)
                renderTargetPool.append({
                    targetId: targetId,
                    entry: displayEntry,
                    fingerprint: displayFingerprint,
                    rangeUpdatedAtMs: rangeUpdatedAtMs,
                    speedUpdatedAtMs: speedUpdatedAtMs
                });
            else {
                const rendered = renderTargetPool.get(renderedIndex);
                renderTargetPool.setProperty(renderedIndex, "rangeUpdatedAtMs", rangeUpdatedAtMs);
                renderTargetPool.setProperty(renderedIndex, "speedUpdatedAtMs", speedUpdatedAtMs);
                if (String(rendered.fingerprint || "") !== displayFingerprint) {
                    renderTargetPool.setProperty(renderedIndex, "fingerprint", displayFingerprint);
                    renderTargetPool.setProperty(renderedIndex, "entry", displayEntry);
                }
            }
        }
    }

    Connections {
        target: MultiDetectState

        function onTargetPoolEntriesChanged() {
            root._syncRenderTargetPool();
        }

        function onInteractionStateChanged() {
            root._syncRenderTargetPool();
        }

        function onTargetIdChanged() {
            root._syncRenderTargetPool();
        }

        function onExclusiveLockPendingTargetIdChanged() {
            root._syncRenderTargetPool();
        }

        function onPendingCancelledTargetIdsChanged() {
            root._syncRenderTargetPool();
        }

        function onHasLocalSelectionSessionChanged() {
            root._syncRenderTargetPool();
        }
    }

    Component.onCompleted: _syncRenderTargetPool()

    function _classColor(label) {
        const normalized = String(label || "").toLowerCase();
        if (["flame", "fire", "hotspot", "burned_area"].indexOf(normalized) >= 0)
            return "#ff6b35";
        if (["smoke", "smoldering_area", "smolder_area"].indexOf(normalized) >= 0)
            return "#b48efd";
        if (["person", "pedestrian", "people", "firefighter"].indexOf(normalized) >= 0)
            return "#4ea1ff";
        if (["vehicle", "car", "van", "truck", "bus", "train",
             "motorcycle", "bicycle", "motor", "boat"].indexOf(normalized) >= 0)
            return "#45d483";
        if (["aircraft", "airplane", "aeroplane", "plane", "helicopter",
             "drone", "uav"].indexOf(normalized) >= 0)
            return "#22d3ee";
        if (["building", "road", "power_line", "tank"].indexOf(normalized) >= 0)
            return "#f6c85f";
        return "#e8eaed";
    }

    function _trackColor() {
        if (MultiDetectState.interactionState === "LCK" || MultiDetectState.interactionState === "TGT")
            return "#ff3b30";
        if (MultiDetectState.trackingState === "TRACKING") {
            return _classColor(MultiDetectState.targetLabel);
        }
        if (MultiDetectState.trackingState === "LOST") {
            return "#ff6b6b";
        }
        if (MultiDetectState.trackingState === "CANCELLED") {
            return "#9aa0a6";
        }
        return "#ffd43b";
    }

    function _metricText(entry) {
        const rangeText = entry !== undefined && Number.isFinite(entry.estimatedRangeM) ?
                          entry.estimatedRangeM.toFixed(1) + "m" : "--";
        let bearingText = "--";
        if (entry !== undefined && Number.isFinite(entry.relativeBearingDeg)) {
            const bearing = Number(entry.relativeBearingDeg);
            const direction = Math.abs(bearing) < 0.05 ? qsTr("中") :
                              bearing < 0.0 ? qsTr("左") : qsTr("右");
            bearingText = direction + " " + Math.abs(bearing).toFixed(1) + "°";
        }
        const speedText = entry !== undefined && Number.isFinite(entry.targetSpeedMps) ?
                          entry.targetSpeedMps.toFixed(1) + "m/s" : "--";
        return qsTr("距") + " " + rangeText + "  " + qsTr("方") + " " + bearingText +
               "  " + qsTr("速") + " " + speedText;
    }

    function _selectedMetricText() {
        // A RangeStatus packet belongs to one target. Do not render a late
        // packet for an earlier target on the current box; fall back to the
        // current TrackStatus/target-pool visual estimate instead.
        const rangeMatchesCurrentTarget = MultiDetectState.targetId !== "" &&
                                          MultiDetectState.rangeTargetId === MultiDetectState.targetId;
        const rangeText = rangeMatchesCurrentTarget && MultiDetectState.rangeSlantM >= 0.0 ?
                          MultiDetectState.rangeSlantM.toFixed(1) + "m" :
                          Number.isFinite(MultiDetectState.estimatedRangeM) &&
                          MultiDetectState.estimatedRangeM > 0.0 ?
                          MultiDetectState.estimatedRangeM.toFixed(1) + "m" : "--";
        const bearingAvailable = rangeMatchesCurrentTarget && MultiDetectState.rangeBearingAvailable;
        const bearing = bearingAvailable ? MultiDetectState.rangeRelativeBearingDeg :
                        MultiDetectState.relativeBearingDeg;
        const bearingText = (bearingAvailable || MultiDetectState.relativeBearingAvailable) ?
                            (Math.abs(bearing) < 0.05 ? qsTr("中") :
                             bearing < 0.0 ? qsTr("左") : qsTr("右")) +
                            " " + Math.abs(bearing).toFixed(1) + "°" : "--";
        const speedText = Number.isFinite(MultiDetectState.targetSpeedMps) ?
                          MultiDetectState.targetSpeedMps.toFixed(1) + "m/s" : "--";
        return qsTr("距") + " " + rangeText + "  " + qsTr("方") + " " + bearingText +
               "  " + qsTr("速") + " " + speedText;
    }

    function _selectCandidateAt(pixelX, pixelY) {
        const entries = MultiDetectState.selectableTargetPoolEntries;
        if (entries === undefined || entries === null)
            return false;
        const hitRadius = Math.max(24, ScreenTools.defaultFontPixelHeight * 1.8);
        const maximumDistanceSquared = hitRadius * hitRadius;
        let bestEntry = null;
        let bestDistanceSquared = maximumDistanceSquared;
        for (let index = 0; index < entries.length; ++index) {
            const entry = entries[index];
            const tracked = MultiDetectState.isOperatorTrackedTarget(entry);
            const insideTrackedBox = tracked && pixelX >= entry.x1 * root.width &&
                                     pixelX <= entry.x2 * root.width &&
                                     pixelY >= entry.y1 * root.height &&
                                     pixelY <= entry.y2 * root.height;
            const centerX = (entry.x1 + entry.x2) * 0.5 * root.width;
            const centerY = (entry.y1 + entry.y2) * 0.5 * root.height;
            const dx = centerX - pixelX;
            const dy = centerY - pixelY;
            const distanceSquared = insideTrackedBox ? 0 : dx * dx + dy * dy;
            if (distanceSquared <= bestDistanceSquared) {
                bestDistanceSquared = distanceSquared;
                bestEntry = entry;
            }
        }
        if (bestEntry === null)
            return false;
        return MultiDetectState.isOperatorTrackedTarget(bestEntry) ?
               MultiDetectState.selectTrackedCandidate(bestEntry) :
               MultiDetectState.selectCandidate(bestEntry);
    }

    // The pool model is replaced several times per second. A Button living in
    // a delegate can disappear between press and release, which made LCK look
    // dead. Hit-test every per-box action on this stable root MouseArea while
    // keeping the delegate buttons as the visual representation.
    function _actionGeometry(x1, y1, x2, lockVisible) {
        const boxX = x1 * root.width;
        const boxY = y1 * root.height;
        const boxRight = x2 * root.width;
        const rowWidth = root._cancelActionWidth +
                         (lockVisible ? root._actionSpacing + root._lockActionWidth : 0);
        const rowX = boxRight + rowWidth + 4 <= root.width ? boxRight + 4 : boxX - rowWidth - 4;
        return {
            x: rowX,
            y: boxY,
            width: rowWidth,
            height: root._actionButtonHeight,
            cancelRight: rowX + root._cancelActionWidth,
            lockLeft: rowX + root._cancelActionWidth + root._actionSpacing,
        };
    }

    function _insideActionRow(pixelX, pixelY, geometry) {
        return pixelX >= geometry.x && pixelX <= geometry.x + geometry.width &&
               pixelY >= geometry.y && pixelY <= geometry.y + geometry.height;
    }

    function _handleTrackedActionAt(pixelX, pixelY) {
        const entries = MultiDetectState.operatorTrackedTargetPoolEntries;
        if (entries === undefined || entries === null)
            return false;
        for (let index = entries.length - 1; index >= 0; --index) {
            const entry = entries[index];
            if (!entry.bboxValid)
                continue;
            const primaryLocked = (entry.locked && entry.primary &&
                                   MultiDetectState.confirmedPrimaryLockTargetId === entry.targetId) ||
                                  (MultiDetectState.interactionState === "LCK" &&
                                   entry.targetId === MultiDetectState.targetId);
            const lockVisible = MultiDetectState.missionMode !== "PATROL" &&
                                !primaryLocked && MultiDetectState.interactionState !== "LCK";
            const geometry = _actionGeometry(entry.x1, entry.y1, entry.x2, lockVisible);
            if (!_insideActionRow(pixelX, pixelY, geometry))
                continue;
            if (pixelX <= geometry.cancelRight)
                MultiDetectState.cancelTrackedCandidate(entry);
            else if (lockVisible && pixelX >= geometry.lockLeft &&
                     !MultiDetectState.hasPendingTargetSelection)
                MultiDetectState.lockTrackedCandidate(entry);
            return true;
        }
        return false;
    }

    function _handleSelectedActionAt(pixelX, pixelY) {
        if (!MultiDetectState.targetBoxValid ||
                MultiDetectState.hasPoolTrackedTarget(MultiDetectState.targetId) ||
                MultiDetectState.interactionState !== "TRK")
            return false;
        const lockVisible = MultiDetectState.missionMode !== "PATROL";
        const geometry = _actionGeometry(
            MultiDetectState.targetX1,
            MultiDetectState.targetY1,
            MultiDetectState.targetX2,
            lockVisible
        );
        if (!_insideActionRow(pixelX, pixelY, geometry))
            return false;
        if (pixelX <= geometry.cancelRight)
            MultiDetectState.cancelTarget();
        else if (lockVisible && pixelX >= geometry.lockLeft &&
                 !MultiDetectState.hasPendingTargetSelection)
            MultiDetectState.promoteLock();
        return true;
    }

    function _handleStablePointerAt(pixelX, pixelY) {
        return _handleTrackedActionAt(pixelX, pixelY) ||
               _handleSelectedActionAt(pixelX, pixelY) ||
               _selectCandidateAt(pixelX, pixelY);
    }

    function _sampleDepth(normalizedX, normalizedY) {
        const nx = Math.max(0.0, Math.min(1.0, normalizedX));
        const ny = Math.max(0.0, Math.min(1.0, normalizedY));
        const depth = MultiDetectState.depthAtNormalized(nx, ny);
        depthSampleX = nx;
        depthSampleY = ny;
        depthSampleText = Number.isFinite(depth) ? depth.toFixed(1) + " m" : "--";
    }

    Image {
        id: fullDepthMap

        anchors.fill: parent
        fillMode: Image.Stretch
        opacity: 0.46
        smooth: true
        source: MultiDetectState.depthMapDataUrl
        visible: root.depthDisplayMode === 2 && MultiDetectState.depthMapAvailable
        z: 20

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton
            propagateComposedEvents: true

            onClicked: mouse => {
                root._sampleDepth(mouse.x / width, mouse.y / height);
                mouse.accepted = true;
            }
        }
    }

    Rectangle {
        color: "transparent"
        height: Math.max(18, ScreenTools.defaultFontPixelHeight * 1.2)
        visible: fullDepthMap.visible && root.depthSampleX >= 0 && root.depthSampleY >= 0
        width: height
        x: root.depthSampleX * root.width - width * 0.5
        y: root.depthSampleY * root.height - height * 0.5
        z: 24

        Rectangle {
            anchors.centerIn: parent
            border.color: "white"
            border.width: 2
            color: "transparent"
            height: parent.height * 0.62
            radius: height * 0.5
            width: height
        }

        QGCLabel {
            anchors.left: parent.right
            anchors.leftMargin: 4
            anchors.verticalCenter: parent.verticalCenter
            color: "white"
            font.bold: true
            style: Text.Outline
            styleColor: "#cc000000"
            text: root.depthSampleText
        }
    }

    Repeater {
        model: renderTargetPool

        delegate: Item {
            required property var entry
            required property string targetId

            readonly property bool operatorTracked: MultiDetectState.isOperatorTrackedTarget(entry)
            readonly property bool primaryLocked: operatorTracked &&
                                                    ((entry.locked && entry.primary &&
                                                      MultiDetectState.confirmedPrimaryLockTargetId === entry.targetId) ||
                                                     (MultiDetectState.interactionState === "LCK" &&
                                                      entry.targetId === MultiDetectState.targetId))
            readonly property color displayColor: primaryLocked ? "#ff3b30" : root._classColor(entry.label)
            readonly property real markerDiameter: Math.max(34, ScreenTools.defaultFontPixelHeight * 2.2)
            readonly property bool candidateVisible: entry.bboxValid &&
                                                      (operatorTracked || !MultiDetectState.targetBoxValid ||
                                                       entry.targetId !== MultiDetectState.targetId)
            height: operatorTracked ? (entry.y2 - entry.y1) * root.height : markerDiameter
            visible: candidateVisible
            width: operatorTracked ? (entry.x2 - entry.x1) * root.width : markerDiameter
            x: operatorTracked ? entry.x1 * root.width :
                                 ((entry.x1 + entry.x2) * 0.5 * root.width) - width * 0.5
            y: operatorTracked ? entry.y1 * root.height :
                                 ((entry.y1 + entry.y2) * 0.5 * root.height) - height * 0.5
            z: operatorTracked ? 110 : 90

            Behavior on height {
                NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
            }

            Behavior on width {
                NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
            }

            Behavior on x {
                NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
            }

            Behavior on y {
                NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
            }

            Rectangle {
                anchors.fill: parent
                border.color: parent.displayColor
                border.width: Math.max(2, ScreenTools.defaultFontPixelWidth * 0.25)
                color: "#00000000"
                visible: parent.operatorTracked

                Rectangle {
                    anchors.bottom: parent.top
                    anchors.left: parent.left
                    color: Qt.rgba(0, 0, 0, 0.72)
                    height: poolTrackLabel.contentHeight + ScreenTools.defaultFontPixelHeight * 0.35
                    width: poolTrackLabel.contentWidth + ScreenTools.defaultFontPixelWidth * 2

                    QGCLabel {
                        id: poolTrackLabel

                        anchors.centerIn: parent
                        color: parent.parent.parent.displayColor
                        font.bold: true
                        font.pointSize: ScreenTools.smallFontPointSize
                        text: (parent.parent.parent.primaryLocked ? "LCK" : "TRK") + "  " +
                               parent.parent.parent.entry.label + "\n" +
                               root._metricText(parent.parent.parent.entry)
                    }
                }
            }

            QGCLabel {
                anchors.centerIn: parent
                color: parent.displayColor
                font.bold: true
                font.pixelSize: parent.height * 0.9
                style: Text.Outline
                styleColor: "#aa000000"
                text: "+"
                visible: !parent.operatorTracked
            }

            Row {
                spacing: root._actionSpacing
                visible: parent.operatorTracked
                x: parent.x + parent.width + implicitWidth <= root.width ?
                   parent.width + 4 : -implicitWidth - 4
                y: 0
                z: 20

                Button {
                    enabled: root.interactionEnabled
                    height: root._actionButtonHeight
                    text: qsTr("取消")
                    width: root._cancelActionWidth

                    onClicked: MultiDetectState.cancelTrackedCandidate(parent.parent.entry)
                }

                QGCButton {
                    enabled: root.interactionEnabled && !MultiDetectState.hasPendingTargetSelection &&
                              MultiDetectState.isLockEligibleTarget(parent.parent.entry)
                    height: root._actionButtonHeight
                    text: "LCK"
                    textColor: root._lockActionTextColor
                    visible: MultiDetectState.missionMode !== "PATROL" &&
                             !parent.parent.primaryLocked &&
                             MultiDetectState.interactionState !== "LCK"
                    width: root._lockActionWidth

                    onClicked: MultiDetectState.lockTrackedCandidate(parent.parent.entry)
                }
            }

            HoverHandler {
                cursorShape: Qt.PointingHandCursor
            }
        }
    }

    // Keep all target and per-box action clicks on a stable item. It sits above
    // both tracked delegates (z 110) and the fallback target box (z 100).
    MouseArea {
        acceptedButtons: Qt.LeftButton
        anchors.fill: parent
        enabled: root.interactionEnabled && !MultiDetectState.selectionMode
        propagateComposedEvents: true
        z: 115

        onClicked: mouse => mouse.accepted = root._handleStablePointerAt(mouse.x, mouse.y)
        onDoubleClicked: mouse => mouse.accepted = false
    }

    Rectangle {
        anchors.fill: parent
        border.color: "#ffd43b"
        border.width: MultiDetectState.selectionMode ? 2 : 0
        color: "#00000000"
    }

    Repeater {
        model: MultiDetectState.sceneContextState === "VALID" ? MultiDetectState.sceneContextRegions : []

        delegate: Rectangle {
            required property var modelData

            readonly property color contextColor: modelData.label === "road" ? qgcPal.colorGreen : qgcPal.warningText

            border.color: contextColor
            border.width: Math.max(1, ScreenTools.defaultFontPixelWidth * 0.16)
            color: Qt.rgba(contextColor.r, contextColor.g, contextColor.b, 0.08)
            height: (modelData.y2 - modelData.y1) * root.height
            width: (modelData.x2 - modelData.x1) * root.width
            x: modelData.x1 * root.width
            y: modelData.y1 * root.height
            z: 50

            QGCLabel {
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                color: parent.contextColor
                font.pointSize: ScreenTools.smallFontPointSize
                text: parent.modelData.label === "road" ? qsTr("道路") : qsTr("建筑")
            }
        }
    }

    Rectangle {
        id: targetBox

        border.color: root._trackColor()
        border.width: Math.max(2, ScreenTools.defaultFontPixelWidth * 0.25)
        color: "#00000000"
        height: (MultiDetectState.targetY2 - MultiDetectState.targetY1) * root.height
        visible: MultiDetectState.targetBoxValid &&
                 !MultiDetectState.hasPoolTrackedBoxForFallback(
                         MultiDetectState.targetX1,
                         MultiDetectState.targetY1,
                         MultiDetectState.targetX2,
                         MultiDetectState.targetY2)
        width: (MultiDetectState.targetX2 - MultiDetectState.targetX1) * root.width
        x: MultiDetectState.targetX1 * root.width
        y: MultiDetectState.targetY1 * root.height
        z: 100

        Behavior on height {
            NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
        }

        Behavior on width {
            NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
        }

        Behavior on x {
            NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
        }

        Behavior on y {
            NumberAnimation { duration: root._targetSmoothingDurationMs; easing.type: Easing.OutCubic }
        }

        Rectangle {
            anchors.bottom: parent.top
            anchors.left: parent.left
            color: Qt.rgba(0, 0, 0, 0.72)
            height: targetLabel.contentHeight + ScreenTools.defaultFontPixelHeight * 0.35
            width: targetLabel.contentWidth + ScreenTools.defaultFontPixelWidth * 2

            QGCLabel {
                id: targetLabel

                anchors.centerIn: parent
                color: targetBox.border.color
                font.bold: true
                font.pointSize: ScreenTools.smallFontPointSize
                text: MultiDetectState.interactionState + "  " + MultiDetectState.targetLabel +
                      "\n" + root._selectedMetricText() +
                      "\n" + MultiDetectState.targetGeolocationText()
            }
        }

        Row {
            spacing: root._actionSpacing
            visible: MultiDetectState.interactionState === "TRK"
            x: targetBox.x + targetBox.width + implicitWidth <= root.width ?
               targetBox.width + 4 : -implicitWidth - 4
            y: 0

            Button {
                enabled: root.interactionEnabled
                height: root._actionButtonHeight
                text: qsTr("取消")
                width: root._cancelActionWidth

                onClicked: MultiDetectState.cancelTarget()
            }

            QGCButton {
                enabled: root.interactionEnabled && !MultiDetectState.hasPendingTargetSelection &&
                         MultiDetectState.canPromoteCurrentTarget()
                height: root._actionButtonHeight
                text: "LCK"
                textColor: root._lockActionTextColor
                visible: MultiDetectState.missionMode !== "PATROL"
                width: root._lockActionWidth

                onClicked: MultiDetectState.promoteLock()
            }
        }
    }

    Item {
        id: approachReticle

        anchors.centerIn: parent
        height: Math.max(72, ScreenTools.defaultFontPixelHeight * 4.5)
        visible: MultiDetectState.missionMode === "OBSERVE"
                 && (MultiDetectState.interactionState === "LCK" || MultiDetectState.interactionState === "TGT")
                 && !MultiDetectState.selectionMode
        width: height
        z: 120

        Canvas {
            id: approachReticleCanvas

            anchors.fill: parent
            property color reticleColor: "white"

            onReticleColorChanged: requestPaint()
            onVisibleChanged: requestPaint()
            onPaint: {
                const ctx = getContext("2d");
                const w = width;
                const h = height;
                const cx = w / 2;
                const cy = h / 2;
                const arm = w * 0.42;

                function drawCrosshair(strokeColor, strokeWidth) {
                    ctx.strokeStyle = strokeColor;
                    ctx.lineWidth = strokeWidth;
                    ctx.lineCap = "round";
                    ctx.beginPath();
                    ctx.moveTo(cx, cy - arm);
                    ctx.lineTo(cx, cy + arm);
                    ctx.moveTo(cx - arm, cy);
                    ctx.lineTo(cx + arm, cy);
                    ctx.stroke();
                }

                ctx.clearRect(0, 0, w, h);
                drawCrosshair("#99000000", Math.max(4, w * 0.07));
                drawCrosshair(reticleColor, Math.max(2, w * 0.03));
            }
        }
    }

    Rectangle {
        border.color: "#ffd43b"
        border.width: 2
        color: Qt.rgba(1.0, 0.83, 0.23, 0.10)
        height: Math.abs(root._dragCurrentY - root._dragStartY)
        visible: root._dragging
        width: Math.abs(root._dragCurrentX - root._dragStartX)
        x: Math.min(root._dragStartX, root._dragCurrentX)
        y: Math.min(root._dragStartY, root._dragCurrentY)
    }

    Rectangle {
        id: compactStatusPanel

        anchors.left: parent.left
        anchors.leftMargin: ScreenTools.defaultFontPixelWidth
        anchors.top: parent.top
        anchors.topMargin: root._topHudClearance
        color: Qt.rgba(0, 0, 0, 0.68)
        height: compactStatus.contentHeight + ScreenTools.defaultFontPixelHeight * 0.6
        radius: 3
        visible: root.showCompactStatus
        width: compactStatus.contentWidth + ScreenTools.defaultFontPixelWidth * 2

        QGCLabel {
            id: compactStatus

            anchors.centerIn: parent
            color: MultiDetectState.fireAlert ? "#ff6b6b" : "white"
            font.bold: MultiDetectState.fireAlert
            text: (MultiDetectState.missionMode === "PAYLOAD" ? "M2" :
                   MultiDetectState.missionMode === "OBSERVE" ? "M3" : "M1") +
                  " · " + MultiDetectState.interactionState
        }
    }

    Rectangle {
        anchors.bottom: parent.bottom
        anchors.bottomMargin: ScreenTools.defaultFontPixelHeight
        anchors.horizontalCenter: parent.horizontalCenter
        color: Qt.rgba(0, 0, 0, 0.78)
        height: selectionHint.contentHeight + ScreenTools.defaultFontPixelHeight
        radius: 4
        visible: MultiDetectState.selectionMode
        width: selectionHint.contentWidth + ScreenTools.defaultFontPixelWidth * 3

        QGCLabel {
            id: selectionHint

            anchors.centerIn: parent
            color: "#ffd43b"
            text: root.selectionHintText
        }
    }

    QGCButton {
        id: depthModeButton

        anchors.left: parent.left
        anchors.leftMargin: ScreenTools.defaultFontPixelWidth
        anchors.top: compactStatusPanel.bottom
        anchors.topMargin: 6
        enabled: MultiDetectState.operatorConfigured
        text: "DEPTH"
        textColor: root.depthDisplayMode === 0 ? "white" : "#22d3ee"
        visible: MultiDetectState.operatorConfigured
        z: 190

        onClicked: MultiDetectState.persistedSettings.depthDisplayMode =
                root.depthDisplayMode === 1 ? 2 : root.depthDisplayMode === 2 ? 0 : 1
    }

    Rectangle {
        id: depthPip

        anchors.left: depthModeButton.left
        anchors.top: depthModeButton.bottom
        anchors.topMargin: 6
        border.color: "#d8e1e8"
        border.width: 1
        color: "#d9000000"
        height: width * 9 / 16
        radius: 3
        visible: root.depthDisplayMode === 1
        width: Math.max(240, Math.min(root.width * 0.28, 360))
        z: 185

        Image {
            id: depthPipImage

            anchors.fill: parent
            anchors.margins: 2
            fillMode: Image.Stretch
            smooth: true
            source: MultiDetectState.depthMapDataUrl
            visible: MultiDetectState.depthMapAvailable

            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton

                onClicked: mouse => root._sampleDepth(mouse.x / width, mouse.y / height)
            }
        }

        QGCLabel {
            id: depthHeader

            anchors.left: parent.left
            anchors.leftMargin: 7
            anchors.top: parent.top
            anchors.topMargin: 4
            color: "white"
            font.bold: true
            style: Text.Outline
            styleColor: "#cc000000"
            text: "DEPTH  " + MultiDetectState.depthMinimumM.toFixed(1) + "–" +
                  MultiDetectState.depthMaximumM.toFixed(1) + " m"
            visible: MultiDetectState.depthMapAvailable
        }

        QGCLabel {
            anchors.centerIn: parent
            color: "white"
            font.bold: true
            style: Text.Outline
            styleColor: "#cc000000"
            text: qsTr("等待 Jetson 深度网格")
            visible: !MultiDetectState.depthMapAvailable
        }

        QGCLabel {
            anchors.left: depthHeader.left
            anchors.right: parent.right
            anchors.rightMargin: 7
            anchors.top: depthHeader.bottom
            anchors.topMargin: 1
            color: "white"
            elide: Text.ElideRight
            font.pixelSize: Math.max(10, ScreenTools.defaultFontPixelHeight * 0.72)
            style: Text.Outline
            styleColor: "#cc000000"
            text: MultiDetectState.rangeContributionText()
        }

        Rectangle {
            color: "transparent"
            height: Math.max(16, ScreenTools.defaultFontPixelHeight)
            visible: root.depthSampleX >= 0 && root.depthSampleY >= 0
            width: height
            x: root.depthSampleX * parent.width - width * 0.5
            y: root.depthSampleY * parent.height - height * 0.5

            Rectangle {
                anchors.centerIn: parent
                border.color: "white"
                border.width: 2
                color: "transparent"
                height: parent.height * 0.62
                radius: height * 0.5
                width: height
            }

            QGCLabel {
                anchors.left: parent.right
                anchors.leftMargin: 3
                anchors.verticalCenter: parent.verticalCenter
                color: "white"
                font.bold: true
                style: Text.Outline
                styleColor: "#cc000000"
                text: root.depthSampleText
            }
        }

        Rectangle {
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 5
            anchors.horizontalCenter: parent.horizontalCenter
            border.color: "#bbffffff"
            border.width: 1
            height: Math.max(5, ScreenTools.defaultFontPixelHeight * 0.32)
            width: parent.width * 0.52

            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "#ff0000" }
                GradientStop { position: 0.5; color: "#00ff44" }
                GradientStop { position: 1.0; color: "#0000ff" }
            }
        }
    }

    MouseArea {
        acceptedButtons: Qt.LeftButton
        anchors.fill: parent
        cursorShape: enabled ? Qt.CrossCursor : Qt.ArrowCursor
        enabled: root.interactionEnabled && MultiDetectState.selectionMode
        hoverEnabled: true
        preventStealing: true
        propagateComposedEvents: false
        z: 1000

        onCanceled: root._dragging = false
        onPositionChanged: mouse => {
            mouse.accepted = true;
            if (root._dragging) {
                root._dragCurrentX = Math.max(0, Math.min(root.width, mouse.x));
                root._dragCurrentY = Math.max(0, Math.min(root.height, mouse.y));
            }
        }
        onPressed: mouse => {
            mouse.accepted = true;
            root._dragStartX = mouse.x;
            root._dragStartY = mouse.y;
            root._dragCurrentX = mouse.x;
            root._dragCurrentY = mouse.y;
            root._dragging = true;
        }
        onReleased: mouse => {
            mouse.accepted = true;
            if (!root._dragging) {
                return;
            }
            root._dragCurrentX = Math.max(0, Math.min(root.width, mouse.x));
            root._dragCurrentY = Math.max(0, Math.min(root.height, mouse.y));
            root._dragging = false;
            if (root.width <= 0 || root.height <= 0) {
                return;
            }
            MultiDetectState.applySelection(root._dragStartX / root.width, root._dragStartY / root.height, root._dragCurrentX / root.width, root._dragCurrentY / root.height);
        }
    }
}

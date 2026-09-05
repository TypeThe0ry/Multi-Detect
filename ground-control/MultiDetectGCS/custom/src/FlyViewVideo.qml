import QtQuick

import Custom.MultiDetect
import QGroundControl
import QGroundControl.Controls
import QGroundControl.FlyView

Item {
    id: _root

    property Item pipView
    property Item pipState: videoPipState

    PipState {
        id:         videoPipState
        pipView:    _root.pipView
        isDark:     true

        onWindowAboutToOpen: {
            QGroundControl.videoManager.stopVideo()
            videoStartDelay.start()
        }

        onWindowAboutToClose: {
            QGroundControl.videoManager.stopVideo()
            videoStartDelay.start()
        }

        onStateChanged: {
            if (pipState.state !== pipState.fullState) {
                QGroundControl.videoManager.fullScreen = false
            }
        }
    }

    Timer {
        id:           videoStartDelay
        interval:     2000;
        running:      false
        repeat:       false
        onTriggered:  QGroundControl.videoManager.startVideo()
    }

    //-- Video Streaming
    FlightDisplayViewVideo {
        id:             videoStreaming
        anchors.fill:   parent
        useSmallFont:   _root.pipState.state !== _root.pipState.fullState
        visible:        QGroundControl.videoManager.isStreamSource || QGroundControl.videoManager.isUvc
    }

    QGCLabel {
        text: qsTr("Double-click to exit full screen")
        font.pointSize: ScreenTools.largeFontPointSize
        visible: QGroundControl.videoManager.fullScreen
        anchors.centerIn: parent

        onVisibleChanged: {
            if (visible) {
                labelAnimation.start()
            }
        }

        PropertyAnimation on opacity {
            id: labelAnimation
            duration: 10000
            from: 1.0
            to: 0.0
            easing.type: Easing.InExpo
        }
    }

    OnScreenGimbalController {
        id:                      onScreenGimbalController
        anchors.fill:            parent
        cameraTrackingEnabled:   !!(videoStreaming._camera && videoStreaming._camera.trackingEnabled)
    }

    OnScreenCameraTrackingController {
        id:                      cameraTrackingController
        anchors.fill:            parent
        camera:                  videoStreaming._camera
        videoWidth:              videoStreaming.getWidth()
        videoHeight:             videoStreaming.getHeight()
    }

    MouseArea {
        id:                         flyViewVideoMouseArea
        anchors.fill:               parent
        enabled:                    pipState.state === pipState.fullState

        property real _pressX:      0
        property real _pressY:      0
        property bool _dragging:    false
        readonly property real _dragThreshold: 10

        onDoubleClicked: QGroundControl.videoManager.fullScreen = !QGroundControl.videoManager.fullScreen

        onPressed: (mouse) => {
            _pressX = mouse.x
            _pressY = mouse.y
            _dragging = false
        }

        onPositionChanged: (mouse) => {
            if (!_dragging && (Math.abs(mouse.x - _pressX) >= _dragThreshold || Math.abs(mouse.y - _pressY) >= _dragThreshold)) {
                _dragging = true
                onScreenGimbalController.mouseDragStart(_pressX, _pressY)
                cameraTrackingController.mouseDragStart(_pressX, _pressY)
            }
            if (_dragging) {
                onScreenGimbalController.mouseDragPositionChanged(mouse.x, mouse.y)
                cameraTrackingController.mouseDragPositionChanged(mouse.x, mouse.y)
            }
        }

        onReleased: (mouse) => {
            if (_dragging) {
                onScreenGimbalController.mouseDragEnd()
                cameraTrackingController.mouseDragEnd(mouse.x, mouse.y)
            } else {
                onScreenGimbalController.mouseClicked(mouse.x, mouse.y)
                cameraTrackingController.mouseClicked(mouse.x, mouse.y)
            }
            _dragging = false
        }
    }

    // Preserve QGC's native video-input chain. The overlay rejects presses
    // that are not Multi-Detect target operations, allowing the stock
    // click/drag/release path to retain gimbal and camera tracking control.
    MultiDetectVideoOverlay {
        id:                     multiDetectVideoOverlay
        anchors.centerIn:       parent
        height:                 videoStreaming.getHeight()
        interactionEnabled:     _root.pipState.state === _root.pipState.fullState
        visible:                videoStreaming.visible
        width:                  videoStreaming.getWidth()

        // Acceptance-only native video evidence. This code deliberately owns
        // no player or VideoOutput: it recursively discovers QGC's existing
        // objectName="videoContent" below FlightDisplayViewVideo and grabs it
        // only when exactly one native output is present.
        readonly property bool _hilVideoGrabEnabled:
            typeof multiDetectHilVideoGrabExport !== "undefined" &&
            multiDetectHilVideoGrabExport === true &&
            typeof multiDetectHilVideoGrabPrefix !== "undefined" &&
            multiDetectHilVideoGrabPrefix.length > 0
        property int _hilVideoGrabSequence: 0
        property bool _nativeVideoGrabPending: false
        property bool _pipSwappedGrabFinished: false
        property bool _fixedWingGrabFinished: false

        function _isActiveNativeVideoOutput(candidate) {
            let current = candidate
            while (current !== null && current !== undefined) {
                if (current.visible !== true || current.width <= 0 || current.height <= 0) {
                    return false
                }
                if (current === videoStreaming) {
                    return true
                }
                current = current.parent
            }
            return false
        }

        function _collectNativeVideoOutputs(candidate, matches) {
            if (candidate === null || candidate === undefined) {
                return
            }
            if (candidate.objectName === "videoContent" &&
                    _isActiveNativeVideoOutput(candidate)) {
                matches.push(candidate)
            }
            const descendants = candidate.children
            if (descendants === null || descendants === undefined) {
                return
            }
            for (let index = 0; index < descendants.length; ++index) {
                _collectNativeVideoOutputs(descendants[index], matches)
            }
        }

        function _activeVehicleInFixedWingFlight() {
            const activeVehicle = QGroundControl.multiVehicleManager.activeVehicle
            return !!(activeVehicle && activeVehicle.vtol && activeVehicle.vtolInFwdFlight)
        }

        function _nativeOutputDevicePixelRatio(nativeOutput) {
            // grabToImage() returns physical pixels while QQuickItem width and
            // height are logical pixels. Bind the conversion to the existing
            // VideoOutput's own QQuickWindow; never guess or default the DPR.
            let quickWindow = null
            try {
                quickWindow = nativeOutput ? nativeOutput.window : null
            } catch (error) {
                quickWindow = null
            }
            if (!quickWindow && nativeOutput) {
                quickWindow = nativeOutput.Window.window
            }
            const ratio = Number(quickWindow ? quickWindow.devicePixelRatio : NaN)
            return Number.isFinite(ratio) && ratio > 0.0 ? ratio : NaN
        }

        function _grabSnapshot(nativeOutput, matchCount) {
            const decodedSize = QGroundControl.videoManager.videoSize
            const outputWidth = Number(nativeOutput.width)
            const outputHeight = Number(nativeOutput.height)
            const devicePixelRatio = _nativeOutputDevicePixelRatio(nativeOutput)
            return {
                "object_name": nativeOutput.objectName,
                "native_output_match_count": matchCount,
                "output_width": outputWidth,
                "output_height": outputHeight,
                "device_pixel_ratio": devicePixelRatio,
                // QQuickItem::grabToImage interprets targetSize in logical
                // item pixels and applies the window DPR internally. Keep
                // the physical dimensions above for evidence, but retain the
                // logical request separately so Qt does not scale twice.
                "grab_width": Number.isFinite(outputWidth) ? Math.round(outputWidth) : 0,
                "grab_height": Number.isFinite(outputHeight) ? Math.round(outputHeight) : 0,
                "capture_width": Number.isFinite(outputWidth) &&
                                 Number.isFinite(devicePixelRatio) ?
                                 Math.round(Math.round(outputWidth) * devicePixelRatio) : 0,
                "capture_height": Number.isFinite(outputHeight) &&
                                  Number.isFinite(devicePixelRatio) ?
                                  Math.round(Math.round(outputHeight) * devicePixelRatio) : 0,
                "decoded_width": decodedSize ? Math.round(decodedSize.width) : 0,
                "decoded_height": decodedSize ? Math.round(decodedSize.height) : 0,
                "decoding": QGroundControl.videoManager.decoding === true,
                "output_visible": nativeOutput.visible === true,
                "video_full_state": _root.pipState.state === _root.pipState.fullState,
                "vtol_in_fwd_flight": _activeVehicleInFixedWingFlight()
            }
        }

        function _recordNativeVideoGrab(role, path, snapshot, queued, saved) {
            ++_hilVideoGrabSequence
            console.info("MULTIDETECT_HIL_VIDEO_GRAB " + JSON.stringify({
                "schema": "multidetect.qgc-hil-video-grab",
                "schema_version": 1,
                "sequence": _hilVideoGrabSequence,
                "recorded_at_utc": new Date().toISOString(),
                "role": role,
                "object_name": snapshot.object_name,
                "native_output_match_count": snapshot.native_output_match_count,
                "output_width": snapshot.output_width,
                "output_height": snapshot.output_height,
                "device_pixel_ratio": snapshot.device_pixel_ratio,
                "decoded_width": snapshot.decoded_width,
                "decoded_height": snapshot.decoded_height,
                "capture_width": snapshot.capture_width,
                "capture_height": snapshot.capture_height,
                "decoding": snapshot.decoding,
                "output_visible": snapshot.output_visible,
                "video_full_state": snapshot.video_full_state,
                "vtol_in_fwd_flight": snapshot.vtol_in_fwd_flight,
                "queued": queued,
                "saved": saved,
                "path": path
            }))
        }

        function _finishNativeVideoGrab(role) {
            _nativeVideoGrabPending = false
            if (role === "pip-swapped") {
                _pipSwappedGrabFinished = true
            } else if (role === "fixed-wing") {
                _fixedWingGrabFinished = true
            }
        }

        function _queueNativeVideoGrab(role) {
            const matches = []
            _collectNativeVideoOutputs(videoStreaming, matches)
            if (matches.length !== 1) {
                return false
            }

            const nativeOutput = matches[0]
            const snapshot = _grabSnapshot(nativeOutput, matches.length)
            if (!snapshot.video_full_state || !snapshot.decoding ||
                    !snapshot.output_visible || snapshot.output_width <= 0 ||
                    snapshot.output_height <= 0 ||
                    !Number.isFinite(snapshot.device_pixel_ratio) ||
                    snapshot.device_pixel_ratio <= 0.0 || snapshot.capture_width <= 0 ||
                    snapshot.capture_height <= 0 || snapshot.decoded_width <= 0 ||
                    snapshot.decoded_height <= 0) {
                return false
            }
            if (role === "fixed-wing" && !snapshot.vtol_in_fwd_flight) {
                return false
            }

            const path = multiDetectHilVideoGrabPrefix + "-" + role + ".png"
            _nativeVideoGrabPending = true
            let queued = false
            try {
                queued = nativeOutput.grabToImage(function(result) {
                    const saved = result.saveToFile(path)
                    _recordNativeVideoGrab(role, path, snapshot, true, saved)
                    _finishNativeVideoGrab(role)
                }, Qt.size(snapshot.grab_width, snapshot.grab_height))
            } catch (error) {
                queued = false
            }
            if (!queued) {
                _recordNativeVideoGrab(role, path, snapshot, false, false)
                _finishNativeVideoGrab(role)
            }
            return queued
        }

        Timer {
            interval: 200
            repeat: true
            running: multiDetectVideoOverlay._hilVideoGrabEnabled &&
                     !multiDetectVideoOverlay._fixedWingGrabFinished
            onTriggered: {
                if (multiDetectVideoOverlay._nativeVideoGrabPending ||
                        _root.pipState.state !== _root.pipState.fullState) {
                    return
                }
                if (!multiDetectVideoOverlay._pipSwappedGrabFinished) {
                    multiDetectVideoOverlay._queueNativeVideoGrab("pip-swapped")
                    return
                }
                if (!multiDetectVideoOverlay._fixedWingGrabFinished &&
                        multiDetectVideoOverlay._activeVehicleInFixedWingFlight()) {
                    multiDetectVideoOverlay._queueNativeVideoGrab("fixed-wing")
                }
            }
        }
    }

    ProximityRadarVideoView{
        anchors.fill:   parent
        vehicle:        QGroundControl.multiVehicleManager.activeVehicle
    }

    ObstacleDistanceOverlayVideo {
        id: obstacleDistance
        showText: pipState.state === pipState.fullState
    }
}

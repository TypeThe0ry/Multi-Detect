import Custom.MultiDetect

import QGroundControl
import QGroundControl.Controls
import QGroundControl.FlyView
import QtQuick

Item {
    id: root

    property Item pipState: videoPipState
    property Item pipView

    clip: true

    Component.onCompleted: videoStartDelay.start()

    PipState {
        id: videoPipState

        isDark: true
        pipView: root.pipView

        onStateChanged: {
            if (root.pipState.state !== root.pipState.fullState) {
                QGroundControl.videoManager.fullScreen = false;
            }
        }
        onWindowAboutToClose: {
            QGroundControl.videoManager.stopVideo();
            videoStartDelay.start();
        }
        onWindowAboutToOpen: {
            QGroundControl.videoManager.stopVideo();
            videoStartDelay.start();
        }
    }

    Timer {
        id: videoStartDelay

        interval: 2000
        repeat: false

        onTriggered: QGroundControl.videoManager.startVideo()
    }

    FlightDisplayViewVideo {
        id: videoStreaming

        anchors.fill: parent
        useSmallFont: root.pipState.state !== root.pipState.fullState
        visible: QGroundControl.videoManager.isStreamSource || QGroundControl.videoManager.isUvc
    }

    // The HUD binds directly to QGC's MAVLink-backed Vehicle Facts.  It is
    // intentionally outside videoStreaming so heading, attitude, speed, and
    // altitude remain visible on this video surface when only telemetry is
    // connected and the camera stream is absent.
    FixedWingVideoHud {
        anchors.fill: parent
        compact: root.pipState.state !== root.pipState.fullState
        vehicle: QGroundControl.multiVehicleManager.activeVehicle
        z: 850
    }

    MultiDetectVideoOverlay {
        id: multiDetectVideoOverlay

        anchors.centerIn: parent
        height: videoStreaming.getHeight()
        interactionEnabled: root.pipState.state === root.pipState.fullState
        visible: videoStreaming.visible
        width: videoStreaming.getWidth()
        z: 1000
    }

    QGCLabel {
        anchors.centerIn: parent
        font.pointSize: ScreenTools.largeFontPointSize
        text: qsTr("Double-click to exit full screen")
        visible: QGroundControl.videoManager.fullScreen
        z: 1010

        PropertyAnimation on opacity {
            id: labelAnimation

            duration: 10000
            easing.type: Easing.InExpo
            from: 1.0
            to: 0.0
        }

        onVisibleChanged: {
            if (visible) {
                labelAnimation.start();
            }
        }
    }

    MouseArea {
        id: flyViewVideoMouseArea

        anchors.fill: parent
        enabled: root.pipState.state === root.pipState.fullState && !MultiDetectState.selectionMode

        onDoubleClicked: QGroundControl.videoManager.fullScreen = !QGroundControl.videoManager.fullScreen
    }

    ProximityRadarVideoView {
        anchors.fill: parent
        vehicle: QGroundControl.multiVehicleManager.activeVehicle
    }

    ObstacleDistanceOverlayVideo {
        showText: root.pipState.state === root.pipState.fullState
    }
}

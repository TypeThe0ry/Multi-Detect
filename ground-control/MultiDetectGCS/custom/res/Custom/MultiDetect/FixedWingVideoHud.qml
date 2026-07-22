import QtQuick

import QGroundControl
import QGroundControl.Controls

// Read-only fixed-wing flight HUD for the video surface.  Every value below
// comes from QGC's active Vehicle Facts, so it remains available when the
// MAVLink telemetry link is present even if a video stream is unavailable.
Item {
    id: root

    property var vehicle: null
    property bool compact: false

    readonly property bool telemetryAvailable: vehicle !== null && !vehicle.communicationLost
    readonly property bool _smallLayout: compact || width < 640 || height < 390
    readonly property color _hudColor: "#f6f8fb"
    readonly property color _referenceColor: "#ffd166"
    readonly property color _outlineColor: "#9a000000"
    readonly property real _heading: _factNumber(vehicle ? vehicle.heading : null, 0.0)
    readonly property real _roll: _factNumber(vehicle ? vehicle.roll : null, 0.0)
    readonly property real _pitch: _factNumber(vehicle ? vehicle.pitch : null, 0.0)
    readonly property real _headingBase: Math.floor(_heading / 10.0) * 10.0
    readonly property real _headingPixelsPerDegree: Math.max(2.2, headingTape.width / 115.0)
    readonly property real _pitchPixelsPerDegree: Math.max(4.0, attitudeViewport.height / 22.0)
    readonly property int _labelPixelSize: Math.max(12, Math.round(ScreenTools.defaultFontPixelHeight * (_smallLayout ? 0.78 : 0.94)))
    readonly property int _valuePixelSize: Math.max(14, Math.round(ScreenTools.defaultFontPixelHeight * (_smallLayout ? 0.96 : 1.15)))
    // The native FlyView toolbar occupies the first video rows on desktop.
    // Place the heading tape below it rather than letting the translucent bar
    // wash out the direction labels.
    readonly property real _headingTopClearance: Math.max(96, _labelPixelSize * 7.2)

    function _factNumber(fact, fallback) {
        if (fact === null || fact === undefined)
            return fallback;
        const value = Number(fact.rawValue);
        return Number.isFinite(value) ? value : fallback;
    }

    function _factText(fact) {
        if (fact === null || fact === undefined)
            return "--";
        const value = String(fact.valueString || "");
        return value === "" || value === "NaN" ? "--" : value;
    }

    function _normalizedHeading(value) {
        const normalized = ((Math.round(value) % 360) + 360) % 360;
        return normalized;
    }

    function _headingLabel(value) {
        const heading = _normalizedHeading(value);
        if (heading === 0)
            return "N";
        if (heading === 90)
            return "E";
        if (heading === 180)
            return "S";
        if (heading === 270)
            return "W";
        return String(heading);
    }

    visible: vehicle !== null

    // Heading tape.  It is deliberately transparent so the camera image and
    // target boxes remain the primary visual layer.
    Item {
        id: headingTape

        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: parent.top
        anchors.topMargin: root._headingTopClearance
        clip: true
        height: Math.max(38, root._labelPixelSize * 2.7)
        width: Math.min(parent.width * 0.68, root._smallLayout ? 360 : 560)

        Rectangle {
            anchors.fill: parent
            color: Qt.rgba(0, 0, 0, 0.32)
            radius: 3
        }

        Repeater {
            model: 13

            delegate: Item {
                readonly property real rawDegree: root._headingBase + (index - 6) * 10.0
                readonly property int shownHeading: root._normalizedHeading(rawDegree)

                height: parent.height
                width: Math.max(26, root._headingPixelsPerDegree * 10.0)
                x: parent.width / 2.0 + (rawDegree - root._heading) * root._headingPixelsPerDegree - width / 2.0

                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    color: root._hudColor
                    height: parent.shownHeading % 30 === 0 ? 10 : 6
                    opacity: 0.9
                    width: 1
                }

                QGCLabel {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: parent.shownHeading % 30 === 0 ? 12 : 8
                    color: root._hudColor
                    font.bold: parent.shownHeading % 30 === 0
                    font.pixelSize: root._labelPixelSize
                    style: Text.Outline
                    styleColor: root._outlineColor
                    text: parent.shownHeading % 30 === 0 ? root._headingLabel(parent.rawDegree) : ""
                }
            }
        }

        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            color: root._referenceColor
            height: 15
            width: 2
        }

        QGCLabel {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 2
            color: root._referenceColor
            font.bold: true
            font.pixelSize: root._labelPixelSize
            style: Text.Outline
            styleColor: root._outlineColor
            text: root._headingLabel(root._heading)
        }
    }

    // Pitch ladder follows the MAVLink roll/pitch Facts.  The view itself is
    // clipped and transparent; no simulated attitude values are introduced.
    Item {
        id: attitudeViewport

        anchors.centerIn: parent
        anchors.verticalCenterOffset: root._smallLayout ? 8 : 0
        clip: true
        height: Math.min(parent.height * 0.54, root._smallLayout ? 210 : 315)
        width: Math.min(parent.width * 0.60, root._smallLayout ? 360 : 610)

        Item {
            id: pitchLadder

            anchors.horizontalCenter: parent.horizontalCenter
            height: attitudeViewport.height * 3.4
            rotation: -root._roll
            transformOrigin: Item.Center
            width: attitudeViewport.width * 1.8
            y: (attitudeViewport.height - height) / 2.0 + root._pitch * root._pitchPixelsPerDegree

            Behavior on rotation {
                NumberAnimation { duration: 90; easing.type: Easing.OutCubic }
            }

            Behavior on y {
                NumberAnimation { duration: 90; easing.type: Easing.OutCubic }
            }

            Repeater {
            model: 25

            delegate: Item {
                    readonly property int pitchMark: (index - 12) * 5

                    anchors.horizontalCenter: parent.horizontalCenter
                    height: Math.max(12, root._labelPixelSize + 2)
                    width: parent.width
                    y: parent.height / 2.0 - pitchMark * root._pitchPixelsPerDegree - height / 2.0

                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.verticalCenter: parent.verticalCenter
                        color: parent.pitchMark === 0 ? root._referenceColor : root._hudColor
                        height: parent.pitchMark === 0 ? 2 : 1
                        opacity: parent.pitchMark === 0 ? 1.0 : 0.86
                        width: parent.pitchMark === 0 ? parent.width * 0.48 :
                                                     (Math.abs(parent.pitchMark) % 10 === 0 ? parent.width * 0.34 : parent.width * 0.20)
                    }

                    QGCLabel {
                        anchors.right: parent.horizontalCenter
                        anchors.rightMargin: parent.width * 0.20
                        anchors.verticalCenter: parent.verticalCenter
                        color: root._hudColor
                        font.bold: true
                        font.pixelSize: root._labelPixelSize
                        style: Text.Outline
                        styleColor: root._outlineColor
                        text: parent.pitchMark !== 0 && Math.abs(parent.pitchMark) % 10 === 0 ? String(Math.abs(parent.pitchMark)) : ""
                    }

                    QGCLabel {
                        anchors.left: parent.horizontalCenter
                        anchors.leftMargin: parent.width * 0.20
                        anchors.verticalCenter: parent.verticalCenter
                        color: root._hudColor
                        font.bold: true
                        font.pixelSize: root._labelPixelSize
                        style: Text.Outline
                        styleColor: root._outlineColor
                        text: parent.pitchMark !== 0 && Math.abs(parent.pitchMark) % 10 === 0 ? String(Math.abs(parent.pitchMark)) : ""
                    }
                }
            }
        }
    }

    // Flight-reference wings use amber horizontal segments rather than a
    // target crosshair, keeping this HUD distinct from the white M3 lock cue.
    Item {
        id: flightReference

        anchors.centerIn: parent
        height: Math.max(24, root._labelPixelSize * 1.7)
        width: Math.max(100, Math.min(parent.width * 0.24, 180))

        Rectangle {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            color: root._referenceColor
            height: 2
            width: parent.width * 0.40
        }

        Rectangle {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            color: root._referenceColor
            height: 2
            width: parent.width * 0.40
        }

        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.verticalCenter: parent.verticalCenter
            color: root._referenceColor
            height: 7
            rotation: 45
            width: 2
        }

        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.verticalCenter: parent.verticalCenter
            color: root._referenceColor
            height: 7
            rotation: -45
            width: 2
        }
    }

    Rectangle {
        id: speedPanel

        anchors.left: parent.left
        anchors.leftMargin: root._smallLayout ? 10 : 18
        anchors.verticalCenter: parent.verticalCenter
        color: Qt.rgba(0, 0, 0, 0.34)
        height: speedColumn.implicitHeight + root._labelPixelSize
        radius: 3
        width: Math.max(root._smallLayout ? 96 : 118, speedColumn.implicitWidth + root._labelPixelSize)

        Column {
            id: speedColumn

            anchors.centerIn: parent
            spacing: 1

            QGCLabel {
                color: root._hudColor
                font.bold: true
                font.pixelSize: root._labelPixelSize
                style: Text.Outline
                styleColor: root._outlineColor
                // Keep the raw AirSpeed fact available for diagnostics, while
                // the flight-video HUD uses its display-filtered companion so
                // zero-wind pitot noise does not read as aircraft motion.
                text: "AS  " + root._factText(root.vehicle ? root.vehicle.airSpeedDisplay : null)
            }

            QGCLabel {
                color: root._hudColor
                font.pixelSize: root._labelPixelSize
                style: Text.Outline
                styleColor: root._outlineColor
                text: "GS  " + root._factText(root.vehicle ? root.vehicle.groundSpeed : null)
            }
        }
    }

    Rectangle {
        anchors.left: parent.left
        anchors.leftMargin: root._smallLayout ? 10 : 18
        anchors.top: speedPanel.bottom
        anchors.topMargin: 6
        color: Qt.rgba(0, 0, 0, 0.34)
        height: altitudeColumn.implicitHeight + root._labelPixelSize
        radius: 3
        width: Math.max(root._smallLayout ? 100 : 122, altitudeColumn.implicitWidth + root._labelPixelSize)

        Column {
            id: altitudeColumn

            anchors.centerIn: parent
            spacing: 1

            QGCLabel {
                color: root._hudColor
                font.bold: true
                font.pixelSize: root._labelPixelSize
                style: Text.Outline
                styleColor: root._outlineColor
                text: "ALT  " + root._factText(root.vehicle ? root.vehicle.altitudeRelative : null)
            }

            QGCLabel {
                color: root._hudColor
                font.pixelSize: root._labelPixelSize
                style: Text.Outline
                styleColor: root._outlineColor
                text: "VS   " + root._factText(root.vehicle ? root.vehicle.climbRate : null)
            }
        }
    }

    Rectangle {
        anchors.bottom: parent.bottom
        anchors.bottomMargin: root._smallLayout ? 8 : 15
        anchors.horizontalCenter: parent.horizontalCenter
        color: Qt.rgba(0, 0, 0, 0.38)
        height: modeLabel.implicitHeight + root._labelPixelSize * 0.65
        radius: 3
        visible: root.telemetryAvailable
        width: modeLabel.implicitWidth + root._labelPixelSize * 1.6

        QGCLabel {
            id: modeLabel

            anchors.centerIn: parent
            color: root.vehicle && root.vehicle.armed ? "#56d364" : "#ff6b6b"
            font.bold: true
            font.pixelSize: root._valuePixelSize
            style: Text.Outline
            styleColor: root._outlineColor
            text: (root.vehicle && root.vehicle.armed ? "ARMED" : "DISARMED") +
                  (root.vehicle && root.vehicle.flightMode ? "  ·  " + root.vehicle.flightMode : "")
        }
    }

    QGCLabel {
        anchors.centerIn: parent
        color: "#ff6b6b"
        font.bold: true
        font.pixelSize: root._valuePixelSize
        style: Text.Outline
        styleColor: root._outlineColor
        text: "TELEMETRY LOST"
        visible: root.vehicle !== null && !root.telemetryAvailable
    }
}

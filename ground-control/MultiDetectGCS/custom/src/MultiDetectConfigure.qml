import Custom.MultiDetect

import QGroundControl
import QGroundControl.AppSettings
import QGroundControl.Controls
import QtQuick
import QtQuick.Layouts

SettingsPage {
    id: root

    objectName: "settingsPage_MultiDetectConfigure"

    QGCPalette {
        id: qgcPal
        colorGroupEnabled: true
    }

    QGCLabel {
        Layout.fillWidth: true
        font.bold: true
        font.pointSize: ScreenTools.largeFontPointSize
        text: qsTr("Mode Setting")
    }

    SettingsGroupLayout {
        Layout.fillWidth: true
        heading: qsTr("Mode")

        LabelledComboBox {
            id: missionModeCombo

            Layout.fillWidth: true
            enabled: !MultiDetectState.missionConfigurationLocked
            label: qsTr("模式")
            model: [qsTr("模式 1"), qsTr("模式 2"), qsTr("模式 3")]

            Component.onCompleted: currentIndex = MultiDetectState.missionMode === "PAYLOAD" ? 1 :
                                                   MultiDetectState.missionMode === "OBSERVE" ? 2 : 0
            onActivated: index => MultiDetectState.setMissionMode(index === 1 ? "PAYLOAD" :
                                                                  index === 2 ? "OBSERVE" : "PATROL")
        }

        QGCLabel {
            Layout.fillWidth: true
            color: qgcPal.warningText
            font.bold: true
            text: qsTr("LOCKED")
            visible: MultiDetectState.missionConfigurationLocked
        }

    }

    SettingsGroupLayout {
        Layout.fillWidth: true
        heading: qsTr("模式 2")
        visible: MultiDetectState.missionMode === "PAYLOAD"

        LabelledComboBox {
            id: rcChannelCombo

            Layout.fillWidth: true
            enabled: !MultiDetectState.missionConfigurationLocked
            label: qsTr("RC")
            model: [qsTr("OFF"), "CH5", "CH6", "CH7", "CH8", "CH9", "CH10", "CH11", "CH12", "CH13", "CH14", "CH15", "CH16", "CH17", "CH18"]

            Component.onCompleted: currentIndex = MultiDetectState.rcReleaseChannel === 0 ? 0 :
                                                   MultiDetectState.rcReleaseChannel - 4
            onActivated: index => MultiDetectState.setRcReleaseChannel(index === 0 ? 0 : index + 4)
        }

        QGCLabel {
            Layout.fillWidth: true
            color: MultiDetectState.rcReleaseSwitchActive ? qgcPal.warningText : qgcPal.text
            text: MultiDetectState.rcReleaseState +
                  (MultiDetectState.rcSignalAvailable ? " · PWM " + MultiDetectState.rcReleasePwm : "")
        }
    }
}

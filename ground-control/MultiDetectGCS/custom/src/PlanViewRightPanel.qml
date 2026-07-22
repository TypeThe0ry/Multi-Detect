import QGroundControl
import QGroundControl.Controls
import QGroundControl.PlanView
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    property var _missionController: planMasterController.missionController
    required property var editorMap
    required property var planMasterController

    signal editingLayerChangeRequested(int layer)

    function selectLayer(nodeType) {
        if (!panelOpenCloseButton._expanded) {
            root.anchors.left = undefined;
            root.anchors.right = root.parent.right;
        }
        planTreeView.selectLayer(nodeType);
    }

    function selectNextNotReady() {
        for (var i = 0; i < _missionController.visualItems.count; i++) {
            var vmi = _missionController.visualItems.get(i);
            if (vmi.readyForSaveState === VisualMissionItem.NotReadyForSaveData) {
                _missionController.setCurrentPlanViewSeqNum(vmi.sequenceNumber, true);
                break;
            }
        }
    }

    QGCPalette {
        id: qgcPal
    }

    Rectangle {
        id: rightPanelBackground

        anchors.fill: parent
        color: qgcPal.window
        opacity: 0.85
    }

    Item {
        id: panelOpenCloseButton

        property bool _expanded: root.anchors.right === root.parent.right

        anchors.right: parent.left
        anchors.verticalCenter: parent.verticalCenter
        clip: true
        height: toggleButtonRect.height
        width: toggleButtonRect.width - toggleButtonRect.radius

        Rectangle {
            id: toggleButtonRect

            color: rightPanelBackground.color
            height: width * 3
            opacity: rightPanelBackground.opacity
            radius: ScreenTools.defaultBorderRadius
            width: ScreenTools.defaultFontPixelWidth * 2.25

            QGCLabel {
                anchors.centerIn: parent
                color: qgcPal.buttonText
                text: panelOpenCloseButton._expanded ? ">" : "<"
            }
        }

        QGCMouseArea {
            anchors.fill: parent

            onClicked: {
                if (panelOpenCloseButton._expanded) {
                    root.anchors.right = undefined;
                    root.anchors.left = root.parent.right;
                } else {
                    root.anchors.left = undefined;
                    root.anchors.right = root.parent.right;
                }
            }
        }
    }

    Item {
        anchors.fill: rightPanelBackground

        DeadMouseArea {
            anchors.fill: parent
        }

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            PlanTreeView {
                id: planTreeView

                Layout.fillHeight: true
                Layout.fillWidth: true
                editorMap: root.editorMap
                planMasterController: root.planMasterController

                onEditingLayerChangeRequested: layer => root.editingLayerChangeRequested(layer)
            }
        }
    }
}

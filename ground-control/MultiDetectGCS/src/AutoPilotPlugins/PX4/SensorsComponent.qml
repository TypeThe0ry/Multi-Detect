import QtQuick

import QGroundControl
import QGroundControl.AutoPilotPlugins.PX4

// SensorsSetup already owns the complete calibration layout. Loading it through
// a second SetupPage/Loader layer left the inner Item at 0x0 on the custom
// sectioned VehicleConfigView. Keep a single Loader boundary and size the real
// calibration surface directly from the outer vehicle panel.
Item {
    id: sensorsPage

    property string sectionNameFilter: ""

    function sectionVisible(name) {
        return sensorsSetup.sectionVisible(name)
    }

    SensorsSetup {
        id:                 sensorsSetup
        anchors.fill:       parent
        sectionNameFilter:  sensorsPage.sectionNameFilter
    }
}

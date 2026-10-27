import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents

PlasmoidItem {
    id: root

    // ── data source: runs nvidia-smi every 2 sec ──
    PlasmaCore.DataSource {
        id: nvidiaSource
        engine: "executable"
        connectedSources: []
        onNewData: {
            var raw = data["stdout"] || "";
            var parts = raw.trim().split(",");
            if (parts.length >= 2) {
                pstate.text = parts[0].trim();
                power.text = parts[1].trim() + " W";
            }
        }
    }

    Timer {
        interval: 2000
        running: true
        repeat: true
        onTriggered: {
            nvidiaSource.connectSource(
                "nvidia-smi --query-gpu=pstate,power.draw --format=csv,noheader,nounits"
            );
        }
    }

    // ══ compact representation — shown in the panel ══
    compactRepresentation: Item {
        id: compactRoot
        property int iconSize: PlasmaCore.Units.iconSizes.smallMedium

        Layout.minimumWidth: col.implicitWidth + PlasmaCore.Units.smallSpacing * 2
        Layout.minimumHeight: PlasmaCore.Units.iconSizes.small + PlasmaCore.Units.smallSpacing * 2

        ColumnLayout {
            id: col
            anchors.centerIn: parent
            spacing: 0

            PlasmaComponents.Label {
                id: pstate
                text: "—"
                font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.85
                font.bold: true
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
                color: PlasmaCore.Theme.textColor
            }

            PlasmaComponents.Label {
                id: power
                text: "—"
                font.pointSize: PlasmaCore.Theme.defaultFont.pointSize * 0.65
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
                opacity: 0.8
                color: PlasmaCore.Theme.textColor
            }
        }
    }
}

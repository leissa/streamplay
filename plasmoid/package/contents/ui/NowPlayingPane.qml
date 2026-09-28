/* Large artwork and track details for whatever is playing right now. */

import QtQuick
import QtQuick.Layouts

import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

import "Formatting.js" as Fmt

Item {
    id: pane

    readonly property var client: root.client
    readonly property var track: client.track

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Kirigami.Units.largeSpacing
        spacing: Kirigami.Units.largeSpacing
        visible: !!pane.track

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Kirigami.Units.gridUnit * 6

            Kirigami.Icon {
                anchors.centerIn: parent
                width: Math.min(parent.width, parent.height)
                height: width
                source: "media-optical-audio"
                visible: art.status !== Image.Ready
                opacity: 0.4
            }

            Image {
                id: art
                anchors.centerIn: parent
                width: Math.min(parent.width, parent.height)
                height: width
                source: client.itemCover(pane.track, 512)
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                cache: true
                smooth: true
                visible: status === Image.Ready
            }

            PlasmaComponents.BusyIndicator {
                anchors.centerIn: parent
                running: client.playback.buffering === true
                visible: running
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PlasmaExtras.Heading {
                Layout.fillWidth: true
                level: 3
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                text: Fmt.numbered(pane.track, Plasmoid.configuration.showTrackNumbersNowPlaying)
            }

            PlasmaComponents.Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
                text: pane.track ? (pane.track.artist || "") : ""
            }

            PlasmaExtras.DescriptiveLabel {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
                text: pane.track ? (pane.track.album || "") : ""
            }

            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: Kirigami.Units.smallSpacing

                SourceChip {
                    source: pane.track ? pane.track.source : ""
                    visible: client.sources.length > 1
                }

                PlasmaExtras.DescriptiveLabel {
                    visible: client.outputs.length > 1 && !!client.outputName
                    text: i18nc("@info where audio is coming out",
                                "on %1", client.outputName)
                }
            }
        }
    }

    PlasmaExtras.PlaceholderMessage {
        anchors.centerIn: parent
        width: parent.width - Kirigami.Units.gridUnit * 4
        visible: !pane.track && client.online
        iconName: "media-playback-start"
        text: client.linked ? i18n("Nothing is playing")
                            : i18n("No music server is connected")
        explanation: client.linked
            ? i18n("Pick something from the Library tab to get started.")
            : i18n("Add a music server in the settings.")
    }
}

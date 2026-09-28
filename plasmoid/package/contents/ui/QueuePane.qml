/* The shared queue; reordering is committed to the daemon on drop, not on hover. */

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

    /* The daemon says why an entry cannot be played, on the chosen output or at
       all; the source check keeps up between its queue pushes. */
    function playable(track) {
        return !track || (!track.unavailable
            && (!track.source
                || client.connectedSources.some(s => s.id === track.source)));
    }

    function reason(track) {
        return track.unavailable
            || i18n("%1 is not connected", client.sourceName(track.source));
    }

    function unplayableCount() {
        let n = 0;
        for (let i = 0; i < client.queueTracks.length; ++i) {
            if (!playable(client.queueTracks[i])) {
                ++n;
            }
        }
        return n;
    }

    function totalDuration() {
        let total = 0;
        for (let i = 0; i < client.queueTracks.length; ++i) {
            total += client.queueTracks[i].duration || 0;
        }
        return total;
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.smallSpacing
            spacing: Kirigami.Units.smallSpacing

            PlasmaExtras.DescriptiveLabel {
                Layout.fillWidth: true
                elide: Text.ElideRight
                text: {
                    if (client.queueTracks.length === 0) {
                        return "";
                    }
                    const summary = i18np("%1 track, %2", "%1 tracks, %2",
                                          client.queueTracks.length,
                                          Fmt.duration(pane.totalDuration()));
                    const stranded = pane.unplayableCount();
                    return stranded === 0 ? summary
                        : i18nc("@info:status queue summary",
                                "%1 · %2 unavailable", summary, stranded);
                }
            }

            PlasmaComponents.ToolButton {
                icon.name: "edit-clear-all"
                display: PlasmaComponents.AbstractButton.IconOnly
                enabled: client.queueTracks.length > 0
                text: i18n("Clear Queue")
                onClicked: client.clearQueue()

                PlasmaComponents.ToolTip.text: text
                PlasmaComponents.ToolTip.visible: hovered
                PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
            }
        }

        PlasmaComponents.ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
                id: view

                /* Index the dragged row would land on, or -1 while not dragging. */
                property int dropIndex: -1
                property int dragIndex: -1

                model: client.queueTracks
                clip: true
                spacing: 0
                currentIndex: client.queueIndex
                highlightMoveDuration: Kirigami.Units.shortDuration

                delegate: DropArea {
                    id: slot

                    required property int index
                    required property var modelData

                    width: view.width
                    height: row.implicitHeight
                    keys: ["streamplay/queue-item"]

                    onEntered: view.dropIndex = index
                    onExited: if (view.dropIndex === index) view.dropIndex = -1

                    // Where the row would be inserted, drawn above the delegate.
                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        height: Math.max(2, Kirigami.Units.smallSpacing / 2)
                        radius: height / 2
                        color: Kirigami.Theme.highlightColor
                        visible: view.dropIndex === slot.index
                                 && view.dragIndex !== slot.index
                    }

                    PlasmaComponents.ItemDelegate {
                        id: row

                        readonly property bool unavailable:
                            !pane.playable(slot.modelData)

                        width: parent.width
                        highlighted: slot.index === client.queueIndex
                        opacity: Drag.active ? 0.6 : 1
                        onDoubleClicked: client.playAt(slot.index)

                        Drag.active: dragHandler.active
                        Drag.source: row
                        Drag.keys: ["streamplay/queue-item"]
                        Drag.hotSpot.x: width / 2
                        Drag.hotSpot.y: height / 2

                        states: State {
                            when: dragHandler.active
                            ParentChange { target: row; parent: view }
                            AnchorChanges {
                                target: row
                                anchors.horizontalCenter: undefined
                                anchors.verticalCenter: undefined
                            }
                        }

                        contentItem: RowLayout {
                            spacing: Kirigami.Units.smallSpacing
                            opacity: row.unavailable ? 0.45 : 1

                            Item {
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 1.5
                                Layout.preferredHeight: Kirigami.Units.gridUnit

                                PlasmaComponents.Label {
                                    anchors.centerIn: parent
                                    visible: slot.index !== client.queueIndex
                                             && !row.unavailable
                                    opacity: 0.6
                                    font: Kirigami.Theme.smallFont
                                    text: slot.index + 1
                                }

                                Kirigami.Icon {
                                    anchors.centerIn: parent
                                    width: Kirigami.Units.iconSizes.small
                                    height: width
                                    visible: slot.index === client.queueIndex
                                             || row.unavailable
                                    source: row.unavailable
                                            ? "dialog-warning"
                                            : (client.playback.status === "playing"
                                               ? "media-playback-start"
                                               : "media-playback-pause")

                                    HoverHandler { id: markHover }
                                    PlasmaComponents.ToolTip.text:
                                        pane.reason(slot.modelData)
                                    PlasmaComponents.ToolTip.visible:
                                        row.unavailable && markHover.hovered
                                    PlasmaComponents.ToolTip.delay:
                                        Kirigami.Units.toolTipDelay
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 0

                                PlasmaComponents.Label {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    maximumLineCount: 1
                                    text: Fmt.numbered(slot.modelData,
                                                      Plasmoid.configuration.showTrackNumbers,
                                                      client.queueMultiDisc)
                                }

                                PlasmaExtras.DescriptiveLabel {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    maximumLineCount: 1
                                    font: Kirigami.Theme.smallFont
                                    text: Fmt.subtitle(slot.modelData)
                                }
                            }

                            SourceChip {
                                visible: client.sources.length > 1
                                source: slot.modelData.source || ""
                            }

                            PlasmaComponents.Label {
                                opacity: 0.7
                                font: Kirigami.Theme.smallFont
                                text: Fmt.duration(slot.modelData.duration)
                            }

                            PlasmaComponents.ToolButton {
                                icon.name: "list-remove"
                                display: PlasmaComponents.AbstractButton.IconOnly
                                opacity: row.hovered ? 1 : 0
                                text: i18n("Remove from Queue")
                                onClicked: client.removeAt(slot.index)

                                PlasmaComponents.ToolTip.text: text
                                PlasmaComponents.ToolTip.visible: hovered
                                PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
                            }
                        }

                        DragHandler {
                            id: dragHandler
                            target: row
                            yAxis.enabled: true
                            xAxis.enabled: false

                            onActiveChanged: {
                                if (active) {
                                    view.dragIndex = slot.index;
                                    view.dropIndex = -1;
                                    return;
                                }
                                const from = view.dragIndex;
                                const to = view.dropIndex;
                                view.dragIndex = -1;
                                view.dropIndex = -1;
                                row.Drag.drop();
                                if (from >= 0 && to >= 0 && from !== to) {
                                    client.moveItem(from, to);
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    PlasmaExtras.PlaceholderMessage {
        anchors.centerIn: parent
        width: parent.width - Kirigami.Units.gridUnit * 4
        visible: client.queueTracks.length === 0 && client.online
        iconName: "view-media-playlist"
        text: i18n("The queue is empty")
        explanation: i18n("Tracks you add from any connected library end up here, "
                        + "side by side.")
    }
}

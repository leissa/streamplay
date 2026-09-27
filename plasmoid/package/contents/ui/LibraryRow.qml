/* One line in the library browser: artist, album, genre, playlist or track. */

import QtQuick
import QtQuick.Layouts

import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

import "Formatting.js" as Fmt

Item {
    id: rowItem

    property var entry: null
    property bool highlighted: false

    signal activated()
    signal playRequested()
    signal queueRequested()

    readonly property var item: entry ? entry.item : null
    readonly property string kind: entry ? entry.kind : ""
    readonly property bool isHeader: kind === "header"

    implicitHeight: isHeader ? header.implicitHeight : delegate.implicitHeight

    PlasmaExtras.ListSectionHeader {
        id: header
        width: parent.width
        visible: rowItem.isHeader
        text: rowItem.item ? rowItem.item.name : ""
    }

    PlasmaComponents.ItemDelegate {
        id: delegate
        width: parent.width
        visible: !rowItem.isHeader
        highlighted: rowItem.highlighted
        onClicked: rowItem.activated()

        contentItem: RowLayout {
            spacing: Kirigami.Units.smallSpacing

            // Tracks get a plain icon; everything else can carry artwork.
            Item {
                Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                Layout.preferredHeight: Kirigami.Units.iconSizes.medium

                Kirigami.Icon {
                    anchors.fill: parent
                    visible: cover.status !== Image.Ready
                    source: rowItem.kind === "artist" ? "view-media-artist"
                          : rowItem.kind === "genre" ? "view-media-genre"
                          : rowItem.kind === "playlist" ? "view-media-playlist"
                          : rowItem.kind === "track" ? "audio-x-generic"
                                                     : "media-optical-audio"
                }

                Image {
                    id: cover
                    anchors.fill: parent
                    source: rowItem.kind === "genre" || rowItem.kind === "track"
                            ? "" : root.client.itemCover(rowItem.item, 64)
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: true
                    visible: status === Image.Ready
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    maximumLineCount: 1
                    text: {
                        if (!rowItem.item) {
                            return "";
                        }
                        return rowItem.kind === "track" ? (rowItem.item.title || "")
                                                        : (rowItem.item.name || "");
                    }
                }

                PlasmaExtras.DescriptiveLabel {
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    maximumLineCount: 1
                    font: Kirigami.Theme.smallFont
                    visible: text.length > 0
                    text: {
                        const item = rowItem.item;
                        if (!item) {
                            return "";
                        }
                        switch (rowItem.kind) {
                        case "track":
                            return Fmt.subtitle(item);
                        case "album":
                            return item.year ? (item.artist || "") + " · " + item.year
                                             : (item.artist || "");
                        case "artist":
                            return item.albumCount
                                ? i18np("%1 album", "%1 albums", item.albumCount) : "";
                        case "playlist":
                            return item.trackCount
                                ? i18np("%1 track", "%1 tracks", item.trackCount) : "";
                        }
                        return "";
                    }
                }
            }

            SourceChip {
                visible: root.client.sources.length > 1 && rowItem.kind !== "genre"
                source: rowItem.item ? (rowItem.item.source || "") : ""
            }

            PlasmaComponents.Label {
                visible: rowItem.kind === "track"
                opacity: 0.7
                font: Kirigami.Theme.smallFont
                text: rowItem.item ? Fmt.duration(rowItem.item.duration) : ""
            }

            PlasmaComponents.ToolButton {
                icon.name: "media-playback-start"
                display: PlasmaComponents.AbstractButton.IconOnly
                visible: rowItem.kind !== "genre"
                opacity: delegate.hovered ? 1 : 0
                text: i18n("Play Now")
                onClicked: rowItem.playRequested()

                PlasmaComponents.ToolTip.text: i18nc("@info:tooltip action and its keyboard shortcut", "%1 (%2)", text, i18nc("@info:shortcut", "Ctrl+Enter"))
                PlasmaComponents.ToolTip.visible: hovered
                PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
            }

            PlasmaComponents.ToolButton {
                icon.name: "list-add"
                display: PlasmaComponents.AbstractButton.IconOnly
                visible: rowItem.kind !== "genre"
                opacity: delegate.hovered ? 1 : 0
                text: i18n("Add to Queue")
                onClicked: rowItem.queueRequested()

                PlasmaComponents.ToolTip.text: i18nc("@info:tooltip action and its keyboard shortcut", "%1 (%2)", text, i18nc("@info:shortcut", "Shift+Enter"))
                PlasmaComponents.ToolTip.visible: hovered
                PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
            }
        }
    }
}

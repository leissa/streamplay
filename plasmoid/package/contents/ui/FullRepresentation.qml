/* The popup: what is playing, the shared queue, and the merged library. */

import QtQuick
import QtQuick.Layouts

import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasmoid
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

Item {
    id: full

    readonly property var client: root.client

    /* In the order of the panes in the StackLayout. */
    readonly property var paneKeys: ["playing", "queue", "library", "lyrics"]

    /* The tabs, in the order and selection the user chose. */
    readonly property var tabList: {
        const known = {
            playing: { key: "playing", icon: "media-playback-start",
                       label: i18nc("@title:tab", "Playing"),
                       shown: Plasmoid.configuration.showPlayingTab },
            queue: { key: "queue", icon: "view-media-playlist",
                     label: i18nc("@title:tab queue of upcoming tracks", "Queue"),
                     shown: Plasmoid.configuration.showQueueTab },
            library: { key: "library", icon: "view-media-album-cover",
                       label: i18nc("@title:tab", "Library"),
                       shown: Plasmoid.configuration.showLibraryTab },
            lyrics: { key: "lyrics", icon: "view-media-lyrics",
                      label: i18nc("@title:tab", "Lyrics"),
                      shown: Plasmoid.configuration.showLyricsTab },
        };

        const kept = [];
        const seen = {};
        for (const key of Plasmoid.configuration.tabOrder || []) {
            if (known[key] && !seen[key]) {
                seen[key] = true;
                if (known[key].shown) {
                    kept.push(known[key]);
                }
            }
        }
        for (const key of paneKeys) {
            if (!seen[key] && known[key].shown) {
                kept.push(known[key]);
            }
        }
        return kept.length > 0 ? kept : [known.playing];
    }
    readonly property var tabKeys: tabList.map(tab => tab.key)

    /* The tab last chosen; it may since have been switched off. */
    property string currentKey: tabKeys[0]
    readonly property string shownKey:
        tabKeys.indexOf(currentKey) >= 0 ? currentKey : tabKeys[0]

    function showTab(index) {
        currentKey = tabKeys[Math.max(0, Math.min(tabKeys.length - 1, index))];
    }

    Layout.minimumWidth: Kirigami.Units.gridUnit * 20
    Layout.minimumHeight: Kirigami.Units.gridUnit * 24
    Layout.preferredWidth: Kirigami.Units.gridUnit * 26
    Layout.preferredHeight: Kirigami.Units.gridUnit * 30

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.smallSpacing

        HeaderBar {
            Layout.fillWidth: true
        }

        PlasmaComponents.TabBar {
            id: tabs
            Layout.fillWidth: true
            visible: full.tabList.length > 1

            Repeater {
                model: full.tabList

                PlasmaComponents.TabButton {
                    required property var modelData
                    required property int index

                    // TabBar's own split, made explicit; rebuilt buttons otherwise loop on implicitWidth.
                    width: (tabs.availableWidth - (tabs.count - 1) * tabs.spacing) / tabs.count
                    icon.name: modelData.icon
                    text: modelData.label
                    onClicked: full.currentKey = modelData.key

                    PlasmaComponents.ToolTip.text: i18nc("@info:tooltip tab and its keyboard shortcut", "%1 (%2)", text, i18nc("@info:shortcut", "Ctrl+%1", index + 1))
                    PlasmaComponents.ToolTip.visible: hovered
                    PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
                }
            }
        }

        // Reasserted whenever the tabs change, which a plain binding would not survive a click.
        Binding {
            target: tabs
            property: "currentIndex"
            value: full.tabKeys.indexOf(full.shownKey)
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: full.paneKeys.indexOf(full.shownKey)

            NowPlayingPane {}
            QueuePane {}
            LibraryPane { id: library }
            LyricsPane {}
        }

        Shortcut {
            sequence: "Ctrl+H"
            onActivated: full.showTab(full.tabKeys.indexOf(full.shownKey) - 1)
        }
        Shortcut {
            sequence: "Ctrl+L"
            onActivated: full.showTab(full.tabKeys.indexOf(full.shownKey) + 1)
        }
        Shortcut {
            sequence: "Ctrl+1"
            enabled: full.tabKeys.length > 0
            onActivated: full.showTab(0)
        }
        Shortcut {
            sequence: "Ctrl+2"
            enabled: full.tabKeys.length > 1
            onActivated: full.showTab(1)
        }
        Shortcut {
            sequence: "Ctrl+3"
            enabled: full.tabKeys.length > 2
            onActivated: full.showTab(2)
        }
        Shortcut {
            sequence: "Ctrl+4"
            enabled: full.tabKeys.length > 3
            onActivated: full.showTab(3)
        }

        // The search field's own Ctrl+F is live only while it is visible; both at once would be ambiguous.
        Shortcut {
            sequences: [StandardKey.Find]
            enabled: full.shownKey !== "library" && full.tabKeys.indexOf("library") >= 0
            onActivated: {
                full.currentKey = "library";
                Qt.callLater(library.focusSearch);
            }
        }

        // The daemon reports these in response to something the user just did.
        PlasmaComponents.Label {
            id: notice
            Layout.fillWidth: true
            visible: opacity > 0
            opacity: 0
            wrapMode: Text.WordWrap
            maximumLineCount: 2
            elide: Text.ElideRight
            color: Kirigami.Theme.negativeTextColor
            font: Kirigami.Theme.smallFont

            Behavior on opacity { NumberAnimation { duration: 150 } }

            Timer {
                id: noticeTimer
                interval: 6000
                onTriggered: notice.opacity = 0
            }

            Connections {
                target: full.client
                function onErrorReported(message) {
                    notice.text = message;
                    notice.opacity = 1;
                    noticeTimer.restart();
                }
            }
        }

        TransportBar {
            Layout.fillWidth: true
        }
    }

    // Nothing useful can be shown until the daemon answers.
    PlasmaExtras.PlaceholderMessage {
        id: offline

        readonly property var service: root.service

        anchors.centerIn: parent
        width: parent.width - Kirigami.Units.gridUnit * 4
        visible: !full.client.online
        iconName: service.missing.length > 0 ? "dialog-warning" : "network-disconnect"
        text: service.missing.length > 0 ? i18n("Some software is missing")
                                         : i18n("The streamplay service is not running")
        explanation: {
            if (!service.bundled) {
                return i18n("Start it with: systemctl --user start streamplay");
            }
            if (service.missing.length > 0) {
                return i18n("Install these with your package manager: %1",
                            service.missing.join(", "));
            }
            if (service.error) {
                return service.error;
            }
            return i18n("Playback runs in a background service, so music keeps "
                      + "going when the panel restarts. Starting it also starts "
                      + "it automatically from now on.");
        }
        helpfulAction: Kirigami.Action {
            enabled: !offline.service.busy
            visible: offline.service.bundled
            icon.name: offline.service.missing.length > 0 ? "view-refresh"
                                                            : "media-playback-start"
            text: offline.service.missing.length > 0 ? i18n("Check Again")
                                                       : i18n("Start Service")
            onTriggered: {
                if (offline.service.missing.length > 0) {
                    offline.service.check();
                } else {
                    Plasmoid.configuration.startService = true;
                    offline.service.start();
                }
            }
        }
    }
}

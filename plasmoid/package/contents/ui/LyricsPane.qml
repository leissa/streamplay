/* Lyrics for whatever is playing, following along when they are timed. */

import QtQuick
import QtQuick.Layouts

import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami

Item {
    id: pane

    readonly property var client: root.client
    readonly property var track: client.track
    readonly property string trackKey: track ? track.source + "\n" + track.id : ""

    property var lyrics: ({})
    property string loadedKey: ""
    property bool loading: false

    readonly property var synced: lyrics.synced || []
    readonly property bool found: synced.length > 0 || !!lyrics.plain
    readonly property int currentLine: {
        const position = client.displayPosition;
        let lo = 0;
        let hi = synced.length - 1;
        let at = -1;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (synced[mid].time <= position) {
                at = mid;
                lo = mid + 1;
            } else {
                hi = mid - 1;
            }
        }
        return at;
    }

    // Only a visible pane asks, so the lookup services hear only about tracks the user wanted lyrics for.
    function load() {
        if (!visible || !client.online || trackKey === loadedKey) {
            return;
        }
        const key = trackKey;
        loadedKey = key;
        lyrics = {};
        if (!track) {
            loading = false;
            return;
        }
        loading = true;
        client.call("lyrics.get", {
            title: track.title, artist: track.artist,
            album: track.album || "", duration: track.duration || 0
        }, function (result, error) {
            if (key !== pane.loadedKey) {
                return;
            }
            pane.loading = false;
            pane.lyrics = result || {};
            if (error) {
                pane.loadedKey = "";
            }
        });
    }

    onTrackKeyChanged: load()
    onVisibleChanged: load()
    Connections {
        target: pane.client
        function onOnlineChanged() {
            pane.loadedKey = "";
            pane.load();
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.smallSpacing
        visible: pane.found && !pane.loading

        PlasmaComponents.ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: pane.synced.length > 0

            ListView {
                id: timed

                // A hand scroll holds the view still for a moment before it follows the song again.
                property bool following: true

                model: pane.synced
                clip: true
                topMargin: height / 3
                bottomMargin: height / 3

                onMovementStarted: {
                    following = false;
                    resume.stop();
                }
                onMovementEnded: resume.restart()
                onModelChanged: {
                    following = true;
                    Qt.callLater(follow, false);
                }
                onHeightChanged: Qt.callLater(follow, false)
                onFollowingChanged: follow(true)

                Connections {
                    target: pane
                    function onCurrentLineChanged() { timed.follow(true); }
                }

                function follow(animated) {
                    if (!following || pane.currentLine < 0 || count === 0) {
                        return;
                    }
                    scroll.stop();
                    const from = contentY;
                    positionViewAtIndex(pane.currentLine, ListView.Center);
                    if (animated) {
                        scroll.to = contentY;
                        contentY = from;
                        scroll.restart();
                    }
                }

                NumberAnimation {
                    id: scroll
                    target: timed
                    property: "contentY"
                    duration: Kirigami.Units.longDuration
                    easing.type: Easing.InOutQuad
                }

                Timer {
                    id: resume
                    interval: 4000
                    onTriggered: timed.following = true
                }

                delegate: PlasmaComponents.ItemDelegate {
                    id: line

                    required property int index
                    required property var modelData

                    readonly property bool current: index === pane.currentLine

                    width: ListView.view.width
                    hoverEnabled: true
                    onClicked: {
                        timed.following = true;
                        pane.client.seek(modelData.time);
                    }

                    contentItem: PlasmaComponents.Label {
                        text: line.modelData.text || "♪"
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        font.bold: line.current
                        opacity: line.current ? 1 : line.index < pane.currentLine ? 0.45 : 0.7

                        Behavior on opacity {
                            NumberAnimation { duration: Kirigami.Units.shortDuration }
                        }
                    }
                }
            }
        }

        PlasmaComponents.ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: pane.synced.length === 0
            contentWidth: availableWidth

            PlasmaComponents.Label {
                width: parent.width
                padding: Kirigami.Units.largeSpacing
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: pane.lyrics.plain || ""
            }
        }

        PlasmaExtras.DescriptiveLabel {
            Layout.alignment: Qt.AlignHCenter
            font: Kirigami.Theme.smallFont
            text: i18nc("@info lyrics provider", "Lyrics from %1", pane.lyrics.provider || "")
        }
    }

    PlasmaComponents.BusyIndicator {
        anchors.centerIn: parent
        running: pane.loading
        visible: running
    }

    PlasmaExtras.PlaceholderMessage {
        anchors.centerIn: parent
        width: parent.width - Kirigami.Units.gridUnit * 4
        visible: !pane.loading && !pane.found && client.online
        iconName: pane.lyrics.instrumental ? "view-media-genre" : "view-media-lyrics"
        text: !pane.track ? i18n("Nothing is playing")
            : pane.lyrics.instrumental ? i18n("Instrumental")
            : i18n("No lyrics found")
        explanation: pane.track && !pane.lyrics.instrumental
            ? i18n("Neither LRCLIB nor lyrics.ovh knows this track.") : ""
    }
}

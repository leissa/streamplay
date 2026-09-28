import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts

import org.kde.kcmutils as KCM
import org.kde.kirigami as Kirigami

KCM.SimpleKCM {
    id: page

    property alias cfg_daemonHost: hostField.text
    property alias cfg_daemonPort: portField.value
    property alias cfg_startService: startService.checked
    property alias cfg_showTrackInPanel: trackInPanel.checked
    property alias cfg_panelTextLength: textLength.value
    property alias cfg_useAlbumArtIcon: albumArtIcon.checked
    property alias cfg_wheelChangesVolume: wheelVolume.checked
    property alias cfg_showTrackNumbers: trackNumbers.checked
    property alias cfg_showTrackNumbersNowPlaying: trackNumbersNowPlaying.checked
    property string cfg_albumSort: "alphabetical"
    property bool cfg_showAlbums: true
    property bool cfg_showArtists: true
    property bool cfg_showGenres: true
    property bool cfg_showPlaylists: true
    property var cfg_sectionOrder: ["albums", "artists", "genres", "playlists"]

    readonly property var sectionLabels: ({
        albums: i18n("Albums"),
        artists: i18n("Artists"),
        genres: i18n("Genres"),
        playlists: i18n("Playlists"),
    })

    /* The stored order, repaired: duplicates and unknown names dropped, and
       anything missing appended so every section stays reachable. */
    readonly property var orderedKeys: {
        const all = ["albums", "artists", "genres", "playlists"];
        const out = [];
        for (const key of cfg_sectionOrder || []) {
            if (all.indexOf(key) >= 0 && out.indexOf(key) < 0) {
                out.push(key);
            }
        }
        for (const key of all) {
            if (out.indexOf(key) < 0) {
                out.push(key);
            }
        }
        return out;
    }

    readonly property bool nothingShown:
        !cfg_showAlbums && !cfg_showArtists && !cfg_showGenres
        && !cfg_showPlaylists

    function isShown(key) {
        switch (key) {
        case "albums":    return cfg_showAlbums;
        case "artists":   return cfg_showArtists;
        case "genres":    return cfg_showGenres;
        default:          return cfg_showPlaylists;
        }
    }

    function setShown(key, value) {
        switch (key) {
        case "albums":    cfg_showAlbums = value; break;
        case "artists":   cfg_showArtists = value; break;
        case "genres":    cfg_showGenres = value; break;
        default:          cfg_showPlaylists = value; break;
        }
    }

    function moveSection(from, to) {
        if (to < 0 || to >= orderedKeys.length) {
            return;
        }
        const list = orderedKeys.slice();
        const moved = list.splice(from, 1)[0];
        list.splice(to, 0, moved);
        // A fresh array, so the change is actually noticed.
        cfg_sectionOrder = list;
    }

    Kirigami.FormLayout {
        anchors.left: parent.left
        anchors.right: parent.right

        Item { Kirigami.FormData.isSection: true
               Kirigami.FormData.label: i18n("Service") }

        QQC2.TextField {
            id: hostField
            Kirigami.FormData.label: i18n("Host:")
            Layout.fillWidth: true
        }

        QQC2.SpinBox {
            id: portField
            Kirigami.FormData.label: i18n("Port:")
            from: 1
            to: 65535
            editable: true
            // A plain number, not a formatted one; ports have no thousands mark.
            textFromValue: value => value.toString()
            valueFromText: text => parseInt(text, 10)
        }

        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            font: Kirigami.Theme.smallFont
            text: i18n("The streamplay service does the playing and holds the "
                     + "queue. Leave this at 127.0.0.1 unless you run it on "
                     + "another machine.")
        }

        QQC2.CheckBox {
            id: startService
            text: i18n("Start the bundled service when it is not running")
        }

        Item { Kirigami.FormData.isSection: true
               Kirigami.FormData.label: i18n("Panel") }

        QQC2.CheckBox {
            id: trackInPanel
            Kirigami.FormData.label: i18n("Show:")
            text: i18n("Track title next to the icon")
        }

        QQC2.SpinBox {
            id: textLength
            Kirigami.FormData.label: i18n("Shorten title to:")
            from: 8
            to: 80
            enabled: trackInPanel.checked
            textFromValue: value => i18np("%1 character", "%1 characters", value)
            valueFromText: text => parseInt(text, 10)
        }

        QQC2.CheckBox {
            id: albumArtIcon
            text: i18n("Use album art as the panel icon")
        }

        QQC2.CheckBox {
            id: wheelVolume
            text: i18n("Scrolling over the icon changes the volume")
        }

        Item { Kirigami.FormData.isSection: true
               Kirigami.FormData.label: i18n("Library") }

        QQC2.ComboBox {
            id: sortBox
            Kirigami.FormData.label: i18n("Sort albums by:")
            textRole: "label"
            valueRole: "value"
            model: [
                { value: "alphabetical", label: i18n("Title") },
                { value: "artist",       label: i18n("Artist") },
                { value: "newest",       label: i18n("Recently added") },
                { value: "recent",       label: i18n("Recently played") },
                { value: "frequent",     label: i18n("Most played") },
                { value: "byYear",       label: i18n("Year (oldest first)") },
                { value: "byYearDesc",   label: i18n("Year (newest first)") },
                { value: "random",       label: i18n("Random") },
            ]
            onActivated: page.cfg_albumSort = currentValue
            Component.onCompleted: currentIndex = indexOfValue(page.cfg_albumSort)
        }

        QQC2.CheckBox {
            id: trackNumbers
            Kirigami.FormData.label: i18n("Track and disc numbers:")
            text: i18n("In lists")
        }

        QQC2.CheckBox {
            id: trackNumbersNowPlaying
            text: i18n("In Now Playing")
        }

        ColumnLayout {
            Kirigami.FormData.label: i18n("Sections shown:")
            Kirigami.FormData.labelAlignment: Qt.AlignTop
            spacing: 0

            Repeater {
                model: page.orderedKeys

                RowLayout {
                    required property string modelData
                    required property int index

                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    QQC2.CheckBox {
                        text: page.sectionLabels[modelData]
                        checked: page.isShown(modelData)
                        onToggled: page.setShown(modelData, checked)
                    }

                    Item { Layout.fillWidth: true }

                    QQC2.ToolButton {
                        icon.name: "arrow-up"
                        enabled: index > 0
                        QQC2.ToolTip.text: i18n("Move up")
                        QQC2.ToolTip.visible: hovered
                        onClicked: page.moveSection(index, index - 1)
                    }

                    QQC2.ToolButton {
                        icon.name: "arrow-down"
                        enabled: index < page.orderedKeys.length - 1
                        QQC2.ToolTip.text: i18n("Move down")
                        QQC2.ToolTip.visible: hovered
                        onClicked: page.moveSection(index, index + 1)
                    }
                }
            }

            QQC2.Label {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
                wrapMode: Text.WordWrap
                font: Kirigami.Theme.smallFont
                visible: page.nothingShown
                color: Kirigami.Theme.negativeTextColor
                text: i18n("At least one section has to stay switched on; "
                         + "Albums will be used otherwise.")
            }
        }
    }
}

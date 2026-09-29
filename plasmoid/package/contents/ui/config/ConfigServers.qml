/* Manage the music servers, which live in the daemon rather than in applet settings. */

import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts

import org.kde.kcmutils as KCM
import org.kde.kirigami as Kirigami

import ".." as Sp
import "../Formatting.js" as Fmt

KCM.SimpleKCM {
    id: page

    // Read-only here; edited on the General page. The config system fills them in.
    property string cfg_daemonHost: "127.0.0.1"
    property int cfg_daemonPort: 8760

    /* The profile being edited, or null while showing the list. */
    property var draft: null
    property string status: ""
    property bool statusIsError: false

    readonly property var serverTypes: [
        { type: "emby", label: i18n("Emby"), address: "url", login: true, tls: true,
          placeholder: "https://emby.example.org" },
        { type: "jellyfin", label: i18n("Jellyfin"), address: "url", login: true,
          tls: true, placeholder: "https://jellyfin.example.org" },
        { type: "kodi", label: i18n("Kodi"), address: "host", login: true, tls: true },
        { type: "lyrion", label: i18n("Lyrion Music Server"), address: "host",
          login: true, tls: false, port: 9000 },
        { type: "mpd", label: i18n("MPD"), address: "host", login: false, tls: false,
          port: 6600 },
        { type: "plex", label: i18n("Plex"), address: "url", login: false, tls: true,
          placeholder: "http://192.168.1.20:32400" },
        { type: "subsonic", label: i18n("Subsonic"), address: "url", login: true,
          tls: true, placeholder: "https://music.example.org" },
        { type: "upnp", label: i18n("UPnP / DLNA players"), address: "", login: false,
          tls: false },
    ]

    function addServer(type) {
        page.draft = page.blankProfile(type);
        page.status = "";
    }

    function serverType(type) {
        return page.serverTypes.find(t => t.type === type) || {};
    }

    function blankProfile(type) {
        switch (type) {
        case "kodi":
            return { type: "kodi", name: i18n("Kodi"), host: "", port: 8080,
                     wsPort: 9090, username: "", password: "", useTls: false,
                     enabled: true };
        case "jellyfin":
        case "emby":
        case "plex":
            return { type: type, name: page.serverType(type).label, url: "",
                     username: "", password: "", verifyTls: true, enabled: true };
        case "mpd":
            return { type: "mpd", name: i18n("MPD"), host: "127.0.0.1",
                     port: page.serverType(type).port, password: "", musicDirectory: "",
                     enabled: true };
        case "lyrion":
            return { type: "lyrion", name: i18n("Lyrion"), host: "",
                     port: page.serverType(type).port, username: "", password: "",
                     enabled: true };
        case "upnp":
            return { type: "upnp", name: i18n("Network players"), renderers: [],
                     enabled: true };
        default:
            return { type: "subsonic", name: i18n("Subsonic"), url: "",
                     username: "", password: "", legacyAuth: false,
                     verifyTls: true, enabled: true };
        }
    }

    readonly property var draftType: page.draft ? page.serverType(page.draft.type) : ({})

    /* Three ports are configured on this page and they differ only in which
       field they write to, so the plumbing is written once. */
    component PortField: QQC2.SpinBox {
        property string field: ""
        property int fallback: 0

        from: 1
        to: 65535
        editable: true
        value: page.draft && page.draft[field] ? page.draft[field] : fallback
        textFromValue: value => value.toString()
        valueFromText: text => parseInt(text, 10)
        onValueModified: page.draft[field] = value
    }

    function editExisting(id) {
        for (let i = 0; i < client.profileList.length; ++i) {
            if (client.profileList[i].id === id) {
                // A copy, so Cancel really cancels.
                page.draft = JSON.parse(JSON.stringify(client.profileList[i]));
                page.status = "";
                return;
            }
        }
    }

    /* Only send a password when one was typed; blank means "keep the old one". */
    function draftForWire() {
        const out = JSON.parse(JSON.stringify(page.draft));
        if (!out.password) {
            delete out.password;
        }
        delete out.hasPassword;
        return out;
    }

    function report(message, isError) {
        page.status = message;
        page.statusIsError = !!isError;
    }

    Sp.Client {
        id: client
        host: page.cfg_daemonHost
        port: page.cfg_daemonPort
    }

    ColumnLayout {
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: Kirigami.Units.largeSpacing


        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: !client.online
            type: Kirigami.MessageType.Warning
            text: i18n("Cannot reach the streamplay service on %1:%2. "
                     + "Start it with: systemctl --user start streamplay",
                       page.cfg_daemonHost, page.cfg_daemonPort)
        }

        ColumnLayout {
            Layout.fillWidth: true
            visible: page.draft === null
            spacing: Kirigami.Units.smallSpacing

            Repeater {
                model: client.sources

                QQC2.Frame {
                    required property var modelData
                    Layout.fillWidth: true

                    RowLayout {
                        anchors.fill: parent
                        spacing: Kirigami.Units.largeSpacing

                        Kirigami.Icon {
                            implicitWidth: Kirigami.Units.iconSizes.medium
                            implicitHeight: Kirigami.Units.iconSizes.medium
                            source: Fmt.serverIcon(modelData.type)
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0

                            QQC2.Label {
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                                text: modelData.name
                            }

                            QQC2.Label {
                                Layout.fillWidth: true
                                elide: Text.ElideRight
                                font: Kirigami.Theme.smallFont
                                color: modelData.state === "error"
                                       ? Kirigami.Theme.negativeTextColor
                                       : Kirigami.Theme.disabledTextColor
                                text: {
                                    switch (modelData.state) {
                                    case "connected":  return i18n("Connected");
                                    case "connecting": return i18n("Connecting…");
                                    case "error":      return modelData.message
                                                           || i18n("Connection failed");
                                    default:           return i18n("Not connected");
                                    }
                                }
                            }
                        }

                        QQC2.Switch {
                            checked: modelData.enabled
                            enabled: client.online
                            QQC2.ToolTip.text: i18n("Connect to this server")
                            QQC2.ToolTip.visible: hovered
                            onToggled: {
                                client.send(checked ? "sources.connect"
                                                    : "sources.disconnect",
                                            { id: modelData.id });
                                // Clicking breaks the binding, and the daemon may fail to connect.
                                checked = Qt.binding(() => modelData.enabled);
                            }
                        }

                        QQC2.Button {
                            icon.name: "document-edit"
                            display: QQC2.AbstractButton.IconOnly
                            text: i18n("Edit")
                            visible: !modelData.builtin
                            QQC2.ToolTip.text: text
                            QQC2.ToolTip.visible: hovered
                            onClicked: page.editExisting(modelData.id)
                        }

                        QQC2.Button {
                            icon.name: "edit-delete"
                            display: QQC2.AbstractButton.IconOnly
                            text: i18n("Remove")
                            visible: !modelData.builtin
                            QQC2.ToolTip.text: text
                            QQC2.ToolTip.visible: hovered
                            onClicked: {
                                removeDialog.profileId = modelData.id;
                                removeDialog.profileName = modelData.name;
                                removeDialog.open();
                            }
                        }
                    }
                }
            }

            QQC2.Label {
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.largeSpacing
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: client.usedSources.length === 0
                opacity: 0.7
                text: i18n("No music servers yet. Add a Subsonic-compatible "
                         + "server, Jellyfin, Emby, Plex, Kodi, MPD, Lyrion "
                         + "or UPnP players.")
            }

            RowLayout {
                Layout.topMargin: Kirigami.Units.smallSpacing

                QQC2.Button {
                    id: addButton
                    icon.name: "list-add"
                    text: i18n("Add Music Server…")
                    onClicked: addMenu.popup(addButton, 0, addButton.height)

                    QQC2.Menu {
                        id: addMenu

                        Repeater {
                            model: page.serverTypes.filter(t => t.type !== "upnp")

                            QQC2.MenuItem {
                                required property var modelData
                                text: modelData.label
                                icon.name: Fmt.serverIcon(modelData.type)
                                onTriggered: page.addServer(modelData.type)
                            }
                        }

                        QQC2.MenuSeparator {}

                        QQC2.MenuItem {
                            text: page.serverType("upnp").label
                            icon.name: Fmt.serverIcon("upnp")
                            onTriggered: page.addServer("upnp")
                        }
                    }
                }

                Item { Layout.fillWidth: true }
            }
        }


        ColumnLayout {
            Layout.fillWidth: true
            visible: page.draft !== null
            spacing: Kirigami.Units.smallSpacing

            Kirigami.FormLayout {
                Layout.fillWidth: true

                QQC2.TextField {
                    Kirigami.FormData.label: i18n("Name:")
                    Layout.fillWidth: true
                    text: page.draft ? (page.draft.name || "") : ""
                    onTextEdited: page.draft.name = text
                }

                // -- Subsonic, Jellyfin, Emby, Plex --------------------------

                QQC2.TextField {
                    Kirigami.FormData.label: i18n("Server address:")
                    Layout.fillWidth: true
                    visible: page.draftType.address === "url"
                    placeholderText: page.draftType.placeholder || ""
                    text: page.draft ? (page.draft.url || "") : ""
                    onTextEdited: page.draft.url = text
                }

                // -- Kodi ----------------------------------------------------

                QQC2.TextField {
                    Kirigami.FormData.label: i18n("Host:")
                    Layout.fillWidth: true
                    visible: page.draftType.address === "host"
                    placeholderText: page.draft && page.draft.type === "mpd"
                                     ? "127.0.0.1" : "192.168.1.20"
                    text: page.draft ? (page.draft.host || "") : ""
                    onTextEdited: page.draft.host = text
                }

                PortField {
                    Kirigami.FormData.label: i18n("Web interface port:")
                    visible: page.draft && page.draft.type === "kodi"
                    field: "port"
                    fallback: 8080
                }

                PortField {
                    Kirigami.FormData.label: i18n("Event port:")
                    visible: page.draft && page.draft.type === "kodi"
                    field: "wsPort"
                    fallback: 9090
                }

                // -- MPD, Lyrion ---------------------------------------------

                PortField {
                    Kirigami.FormData.label: i18n("Port:")
                    visible: !!page.draftType.port
                    field: "port"
                    fallback: page.draftType.port || 0
                }

                ColumnLayout {
                    Kirigami.FormData.label: i18n("Music folder:")
                    Layout.fillWidth: true
                    visible: page.draft && page.draft.type === "mpd"
                    spacing: Kirigami.Units.smallSpacing

                    QQC2.TextField {
                        Layout.fillWidth: true
                        placeholderText: "/var/lib/mpd/music"
                        text: page.draft ? (page.draft.musicDirectory || "") : ""
                        onTextEdited: page.draft.musicDirectory = text
                    }

                    QQC2.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        font: Kirigami.Theme.smallFont
                        opacity: 0.7
                        text: i18n("Where MPD keeps its files, as this computer "
                                 + "sees them. MPD serves no audio itself, so "
                                 + "without this its music can only play on MPD.")
                    }
                }

                ColumnLayout {
                    Kirigami.FormData.label: i18n("Player addresses:")
                    Layout.fillWidth: true
                    visible: page.draft && page.draft.type === "upnp"
                    spacing: Kirigami.Units.smallSpacing

                    QQC2.TextArea {
                        Layout.fillWidth: true
                        placeholderText: "http://192.168.1.30:1400/xml/device_description.xml"
                        text: page.draft && page.draft.renderers
                              ? [].concat(page.draft.renderers).join("\n") : ""
                        onTextChanged: {
                            if (page.draft && page.draft.type === "upnp") {
                                page.draft.renderers = text.split(/\s+/).filter(u => u);
                            }
                        }
                    }

                    QQC2.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        font: Kirigami.Theme.smallFont
                        opacity: 0.7
                        text: i18n("Players on this network are found by themselves. "
                                 + "List a player's description address here only "
                                 + "if it does not show up.")
                    }
                }

                // -- shared --------------------------------------------------

                QQC2.TextField {
                    Kirigami.FormData.label: i18n("Username:")
                    Layout.fillWidth: true
                    visible: !!page.draftType.login
                    text: page.draft ? (page.draft.username || "") : ""
                    onTextEdited: page.draft.username = text
                }

                Kirigami.PasswordField {
                    id: passwordField
                    Kirigami.FormData.label: page.draft && page.draft.type === "plex"
                                             ? i18n("Token:") : i18n("Password:")
                    Layout.fillWidth: true
                    visible: page.draft && page.draft.type !== "upnp"
                    placeholderText: page.draft && page.draft.hasPassword
                                     ? i18n("Unchanged") : ""
                    text: ""
                    onTextEdited: page.draft.password = text
                }

                QQC2.Label {
                    Layout.fillWidth: true
                    visible: page.draft && page.draft.type === "plex"
                    wrapMode: Text.WordWrap
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    text: i18n("In Plex Web, open any item, choose Get Info → View XML, "
                             + "and copy the X-Plex-Token value from the address bar.")
                }

                QQC2.CheckBox {
                    visible: page.draft && page.draft.type === "kodi"
                    text: i18n("Connect over HTTPS")
                    checked: page.draft ? !!page.draft.useTls : false
                    onToggled: page.draft.useTls = checked
                }

                QQC2.CheckBox {
                    visible: page.draft && page.draft.type === "subsonic"
                    text: i18n("Send the password in the old plain format")
                    checked: page.draft ? !!page.draft.legacyAuth : false
                    onToggled: page.draft.legacyAuth = checked
                }

                QQC2.CheckBox {
                    visible: !!page.draftType.tls
                    text: i18n("Check the TLS certificate")
                    checked: page.draft ? page.draft.verifyTls !== false : true
                    onToggled: page.draft.verifyTls = checked
                }
            }

            QQC2.Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                visible: page.status.length > 0
                color: page.statusIsError ? Kirigami.Theme.negativeTextColor
                                          : Kirigami.Theme.positiveTextColor
                text: page.status
            }

            RowLayout {
                Layout.topMargin: Kirigami.Units.smallSpacing

                QQC2.Button {
                    icon.name: "network-connect"
                    text: i18n("Test Connection")
                    enabled: client.online
                    onClicked: {
                        page.report(i18n("Testing…"), false);
                        client.call("profiles.test",
                                    { profile: page.draftForWire() },
                                    function (result, error) {
                            page.report(error ? error
                                              : i18n("The server answered."),
                                        !!error);
                        });
                    }
                }

                Item { Layout.fillWidth: true }

                QQC2.Button {
                    text: i18n("Cancel")
                    onClicked: {
                        page.draft = null;
                        page.status = "";
                        passwordField.text = "";
                    }
                }

                QQC2.Button {
                    icon.name: "document-save"
                    text: i18n("Save and Connect")
                    enabled: client.online
                    onClicked: {
                        client.call("profiles.save",
                                    { profile: page.draftForWire(), connect: true },
                                    function (result, error) {
                            if (error) {
                                page.report(error, true);
                                return;
                            }
                            if (result && result.connectError) {
                                page.report(i18n("Saved, but connecting failed: %1",
                                                 result.connectError), true);
                                return;
                            }
                            page.draft = null;
                            page.status = "";
                            passwordField.text = "";
                        });
                    }
                }
            }
        }
    }

    Kirigami.PromptDialog {
        id: removeDialog

        property string profileId: ""
        property string profileName: ""

        title: i18n("Remove music server")
        subtitle: i18n("Remove “%1”? Its tracks stay in the queue but will not "
                     + "play until you add the server again.", profileName)
        standardButtons: Kirigami.Dialog.Cancel

        customFooterActions: Kirigami.Action {
            text: i18n("Remove")
            icon.name: "edit-delete"
            onTriggered: {
                client.send("profiles.delete", { id: removeDialog.profileId });
                removeDialog.close();
            }
        }
    }
}

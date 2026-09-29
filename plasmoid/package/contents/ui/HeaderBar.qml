/* Where the queue is playing, and how the connected services are doing. */

import QtQml
import QtQuick
import QtQuick.Layouts

import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasmoid
import org.kde.kirigami as Kirigami

import "Formatting.js" as Fmt

RowLayout {
    id: header

    readonly property var client: root.client
    readonly property var enabledSources: client.sources.filter(s => s.enabled)
    readonly property int failed:
        enabledSources.filter(s => s.state === "error").length
    readonly property string summary: {
        if (!client.online) {
            return i18n("The streamplay service is not running");
        }
        if (client.usedSources.length === 0) {
            return i18n("No music server has been set up yet");
        }
        const connected = client.connectedSources.length;
        const total = header.enabledSources.length;
        let text = i18np("%1 of %2 music server connected",
                         "%1 of %2 music servers connected", connected, total);
        for (let i = 0; i < header.enabledSources.length; ++i) {
            const source = header.enabledSources[i];
            if (source.state === "error") {
                text += "\n" + source.name + ": " + (source.message || i18n("failed"));
            }
        }
        return text;
    }

    spacing: Kirigami.Units.smallSpacing

    Rectangle {
        Layout.alignment: Qt.AlignVCenter
        implicitWidth: Kirigami.Units.gridUnit * 0.6
        implicitHeight: implicitWidth
        radius: width / 2
        color: !client.online ? Kirigami.Theme.disabledTextColor
             : header.failed > 0 ? Kirigami.Theme.negativeTextColor
             : client.linked ? Kirigami.Theme.positiveTextColor
                             : Kirigami.Theme.neutralTextColor

        HoverHandler { id: dotHover }

        PlasmaComponents.ToolTip.text: header.summary
        PlasmaComponents.ToolTip.visible: dotHover.hovered
        PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
    }

    PlasmaComponents.Label {
        text: i18nc("@label playback destination", "Play on")
        opacity: 0.8
        visible: client.outputs.length > 1
    }

    PlasmaComponents.ComboBox {
        id: outputBox
        Layout.fillWidth: true
        visible: client.outputs.length > 1
        model: client.outputs
        textRole: "name"
        enabled: client.online

        function syncToActive() {
            for (let i = 0; i < client.outputs.length; ++i) {
                if (client.outputs[i].active) {
                    currentIndex = i;
                    return;
                }
            }
            currentIndex = -1;
        }

        onActivated: index => {
            const output = client.outputs[index];
            if (output && !output.active) {
                client.setOutput(output.id);
            }
        }

        Component.onCompleted: syncToActive()

        Connections {
            target: client
            // The daemon decides which output is live.
            function onOutputsChanged() { outputBox.syncToActive(); }
        }
    }

    // With a single output there is nothing to choose, so just name the state.
    PlasmaComponents.Label {
        Layout.fillWidth: true
        visible: client.outputs.length <= 1
        elide: Text.ElideRight
        opacity: 0.8
        text: client.online ? header.summary.split("\n")[0]
                            : i18n("Service not running")
    }

    // Switch individual services on and off without opening the settings.
    PlasmaComponents.ToolButton {
        id: serversButton

        icon.name: "server-database"
        display: PlasmaComponents.AbstractButton.IconOnly
        enabled: client.online && client.sources.length > 0
        checkable: true
        checked: serversMenu.opened
        text: i18n("Music Servers")

        onToggled: {
            if (checked) {
                serversMenu.open();
            } else {
                serversMenu.close();
            }
        }

        PlasmaComponents.ToolTip.text: header.summary
        PlasmaComponents.ToolTip.visible: hovered && !serversMenu.opened
        PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay

        PlasmaComponents.Menu {
            id: serversMenu
            y: serversButton.height

            Instantiator {
                model: client.sources

                onObjectAdded: (index, object) => serversMenu.insertItem(index, object)
                onObjectRemoved: (index, object) => serversMenu.removeItem(object)

                delegate: PlasmaComponents.MenuItem {
                    required property var modelData

                    checkable: true
                    checked: modelData.enabled
                    enabled: client.online
                    icon.name: modelData.enabled && modelData.state === "error"
                               ? "dialog-error"
                               : Fmt.serverIcon(modelData.type)
                    text: {
                        switch (modelData.state) {
                        case "connecting":
                            return i18nc("@item:inmenu music server",
                                         "%1 (connecting…)", modelData.name);
                        case "error":
                            return i18nc("@item:inmenu music server",
                                         "%1 (failed)", modelData.name);
                        default:
                            return modelData.name;
                        }
                    }

                    onTriggered: {
                        client.send(checked ? "sources.connect"
                                            : "sources.disconnect",
                                    { id: modelData.id });
                        // Ticking breaks the binding.
                        checked = Qt.binding(() => modelData.enabled);
                    }
                }
            }
        }
    }

    PlasmaComponents.ToolButton {
        icon.name: "view-refresh"
        display: PlasmaComponents.AbstractButton.IconOnly
        enabled: client.online && client.sources.length > 0
        text: i18n("Reconnect All")
        onClicked: client.send("sources.reconnectAll", {})

        PlasmaComponents.ToolTip.text: text
        PlasmaComponents.ToolTip.visible: hovered
        PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
    }

    PlasmaComponents.ToolButton {
        icon.name: "configure"
        display: PlasmaComponents.AbstractButton.IconOnly
        text: i18n("Configure Music Servers…")
        onClicked: Plasmoid.internalAction("configure").trigger()

        PlasmaComponents.ToolTip.text: text
        PlasmaComponents.ToolTip.visible: hovered
        PlasmaComponents.ToolTip.delay: Kirigami.Units.toolTipDelay
    }
}

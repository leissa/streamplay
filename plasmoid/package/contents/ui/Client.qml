/* Connection to the streamplay daemon: the whole WebSocket transport. */

import QtQuick
import QtWebSockets

QtObject {
    id: client

    property string host: "127.0.0.1"
    property int port: 8760

    readonly property bool online: socket.status === WebSocket.Open
    readonly property bool connecting: socket.status === WebSocket.Connecting
    property string socketError: ""

    property var playback: ({ status: "stopped", track: null, position: 0,
                              duration: 0, volume: 0, shuffle: false,
                              repeat: "none", index: -1, queueLength: 0,
                              canNext: false, canPrevious: false,
                              capabilities: ({}) })
    property var queueTracks: []
    property int queueIndex: -1
    /* Every configured service and whether it is currently reachable. */
    property var sources: []
    /* Everywhere the queue can be played: this computer, plus any Kodi box. */
    property var outputs: []
    property var profileList: []
    property var daemonSettings: ({})
    property var daemonInfo: ({})

    readonly property var connectedSources:
        sources.filter(source => source.state === "connected")
    // UPnP renderers are only outputs.
    readonly property var libraries:
        connectedSources.filter(source => source.hasLibrary !== false)
    readonly property bool linked: libraries.length > 0
    readonly property var track: playback.track || null
    readonly property string outputName: playback.outputName || ""

    /* Interpolated between the daemon's ~1 Hz position updates so the progress
       bar moves smoothly. Reset whenever the daemon tells us where we are. */
    property real displayPosition: 0
    property bool scrubbing: false

    signal reloaded()
    signal profilesUpdated()
    signal errorReported(string message)

    property int _nextId: 0
    property var _pending: ({})

    function call(method, params, callback) {
        if (socket.status !== WebSocket.Open) {
            if (callback) {
                callback(null, i18n("Not connected to the streamplay service"));
            }
            return;
        }
        const id = ++_nextId;
        if (callback) {
            _pending[id] = callback;
        }
        socket.sendTextMessage(JSON.stringify({
            id: id, method: method, params: params || {}
        }));
    }

    /* Fire-and-forget, but surface failures so the user is not left guessing. */
    function send(method, params) {
        call(method, params, function (result, error) {
            if (error) {
                client.errorReported(error);
            }
        });
    }

    function coverUrl(source, coverId, size) {
        if (!coverId || !source) {
            return "";
        }
        return "http://" + host + ":" + port
             + "/cover?src=" + encodeURIComponent(source)
             + "&id=" + encodeURIComponent(coverId)
             + "&size=" + (size || 0);
    }

    /* Cover for any library item (track, album or artist). */
    function itemCover(item, size) {
        return item ? coverUrl(item.source, item.coverId, size) : "";
    }

    function sourceName(id) {
        for (let i = 0; i < sources.length; ++i) {
            if (sources[i].id === id) {
                return sources[i].name;
            }
        }
        return id || "";
    }

    function setOutput(id) { send("outputs.set", { id: id }); }

    function playPause() { send("player.playPause", {}); }
    function stop()      { send("player.stop", {}); }
    function next()      { send("player.next", {}); }
    function previous()  { send("player.previous", {}); }
    function seek(pos)   { send("player.seek", { position: pos }); }
    function nudge(off)  { send("player.seekRelative", { offset: off }); }
    function setVolume(v) {
        playback = Object.assign({}, playback, { volume: v });
        send("player.setVolume", { volume: v });
    }
    function toggleShuffle() {
        send("player.setShuffle", { shuffle: !playback.shuffle });
    }
    function cycleRepeat() {
        const order = ["none", "all", "one"];
        const at = Math.max(0, order.indexOf(playback.repeat));
        send("player.setRepeat", { mode: order[(at + 1) % order.length] });
    }

    function enqueue(spec, mode, play) {
        const params = Object.assign({}, spec, { mode: mode || "append" });
        if (play) {
            params.play = true;
        }
        send("queue.add", params);
    }
    function removeAt(index)  { send("queue.remove", { indexes: [index] }); }
    function moveItem(from, to) { send("queue.move", { from: from, to: to }); }
    function clearQueue()     { send("queue.clear", {}); }
    function playAt(index)    { send("queue.playIndex", { index: index }); }

    function refresh() {
        call("hello", {}, function (result, error) {
            if (error || !result) {
                return;
            }
            _applySnapshot(result);
            client.reloaded();
        });
    }

    function _applySnapshot(snapshot) {
        if (snapshot.state) {
            _applyState(snapshot.state);
        }
        if (snapshot.queue) {
            queueTracks = snapshot.queue.tracks || [];
            queueIndex = snapshot.queue.index;
        }
        if (snapshot.sources) {
            _adopt("sources", snapshot.sources);
        }
        if (snapshot.outputs) {
            _adopt("outputs", snapshot.outputs);
        }
        if (snapshot.profiles) {
            profileList = snapshot.profiles;
            client.profilesUpdated();
        }
        if (snapshot.settings) {
            daemonSettings = snapshot.settings;
        }
        if (snapshot.daemon) {
            daemonInfo = snapshot.daemon;
        }
    }

    function _applyState(next) {
        playback = next;
        // Only a state push says the current track moved on; the queue event is for edits.
        queueIndex = next.index;
        // The daemon repeats these every state push, and reassigning reloads the browser.
        if (next.sources) {
            _adopt("sources", next.sources);
        }
        if (next.outputs) {
            _adopt("outputs", next.outputs);
        }
        if (!scrubbing) {
            displayPosition = next.position || 0;
        }
    }

    function _adopt(name, value) {
        if (JSON.stringify(client[name]) !== JSON.stringify(value)) {
            client[name] = value;
        }
    }

    property WebSocket _socket: WebSocket {
        id: socket
        url: "ws://" + client.host + ":" + client.port + "/"
        active: true

        onStatusChanged: newStatus => {
            if (newStatus === WebSocket.Open) {
                client.socketError = "";
                client.refresh();
            } else if (newStatus === WebSocket.Error) {
                client.socketError = socket.errorString;
            } else if (newStatus === WebSocket.Closed) {
                client.playback = Object.assign({}, client.playback,
                                                { status: "stopped" });
            }
        }

        onTextMessageReceived: message => {
            let msg;
            try {
                msg = JSON.parse(message);
            } catch (e) {
                return;
            }

            if (msg.id !== undefined) {
                const callback = client._pending[msg.id];
                delete client._pending[msg.id];
                if (callback) {
                    callback(msg.ok ? msg.result : null,
                             msg.ok ? null : (msg.error || i18n("Request failed")));
                }
                return;
            }

            const data = msg.data || {};
            switch (msg.event) {
            case "state":
                client._applyState(data);
                break;
            case "position":
                if (!client.scrubbing) {
                    client.displayPosition = data.position || 0;
                }
                client.playback = Object.assign({}, client.playback, {
                    position: data.position, duration: data.duration
                });
                break;
            case "seeked":
                client.displayPosition = data.position || 0;
                break;
            case "queue":
                client.queueTracks = data.tracks || [];
                client.queueIndex = data.index;
                break;
            case "sources":
                client._adopt("sources", data);
                break;
            case "profiles":
                client.profileList = data.profiles || [];
                client.profilesUpdated();
                break;
            }
        }
    }

    /* Keep trying to reach the daemon; it may start after plasmashell. */
    property Timer _retry: Timer {
        interval: 4000
        repeat: true
        running: socket.status !== WebSocket.Open
                 && socket.status !== WebSocket.Connecting
        onTriggered: {
            socket.active = false;
            socket.active = true;
        }
    }

    property Timer _tick: Timer {
        interval: 250
        repeat: true
        running: client.playback.status === "playing" && client.online
                 && !client.scrubbing
        onTriggered: {
            const duration = client.playback.duration || 0;
            const next = client.displayPosition + 0.25;
            client.displayPosition = duration > 0 ? Math.min(next, duration) : next;
        }
    }
}

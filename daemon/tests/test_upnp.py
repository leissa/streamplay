"""Drives the UPnP output against a scripted renderer over HTTP, so no hardware or multicast is needed.
Run with ``python3 tests/test_upnp.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay.backends import upnp
from streamplay.backends.base import BackendError, StreamTarget
from streamplay.models import Track

FAILURES: list[str] = []

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:ZonePlayer:1</deviceType>
    <friendlyName>Outer box</friendlyName>
    <UDN>uuid:outer</UDN>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Kitchen Speaker</friendlyName>
        <UDN>uuid:fake-1</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <controlURL>/av</controlURL>
          </service>
          <service>
            <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
            <controlURL>rc</controlURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>"""

SSDP_REPLY = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n"
              b"LOCATION: http://192.168.1.9:1400/xml/device_description.xml\r\n"
              b"ST: urn:schemas-upnp-org:service:AVTransport:1\r\n"
              b"USN: uuid:RINCON_1::urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n")


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class Renderer:
    def __init__(self) -> None:
        self.online = True
        self.state = "NO_MEDIA_PRESENT"
        self.status = "OK"
        self.rel_time = "0:00:00"
        self.duration = "0:00:00"
        self.volume = 30
        self.calls: list[tuple[str, dict[str, str]]] = []


RENDERER = Renderer()


class FakeRenderer(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not RENDERER.online or self.path != "/desc.xml":
            return self._reply(404, "")
        self._reply(200, DESCRIPTION)

    def do_POST(self) -> None:
        service, _, action = self.headers["SOAPACTION"].strip('"').partition("#")
        body = ElementTree.fromstring(self.rfile.read(int(self.headers["Content-Length"])))
        call = next(e for e in body.iter() if e.tag == f"{{{service}}}{action}")
        args = {child.tag: child.text or "" for child in call}
        RENDERER.calls.append((action, args))

        out: dict[str, str] = {}
        if action == "Play":
            RENDERER.state = "PLAYING"
        elif action == "Stop":
            RENDERER.state = "STOPPED"
        elif action == "Pause":
            RENDERER.state = "PAUSED_PLAYBACK"
        elif action == "SetAVTransportURI":
            RENDERER.state = "STOPPED"
            RENDERER.rel_time = "0:00:00"
        elif action == "GetTransportInfo":
            out = {"CurrentTransportState": RENDERER.state,
                   "CurrentTransportStatus": RENDERER.status, "CurrentSpeed": "1"}
        elif action == "GetPositionInfo":
            out = {"RelTime": RENDERER.rel_time, "TrackDuration": RENDERER.duration}
        elif action == "GetVolume":
            out = {"CurrentVolume": str(RENDERER.volume)}
        elif action == "SetVolume":
            RENDERER.volume = int(args["DesiredVolume"])
        elif action == "Seek":
            pass
        else:
            return self._reply(500, "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>"
                                    "<s:Body><s:Fault><detail><UPnPError xmlns='urn:schemas-upnp-org:control-1-0'>"
                                    "<errorCode>401</errorCode><errorDescription>Invalid Action</errorDescription>"
                                    "</UPnPError></detail></s:Fault></s:Body></s:Envelope>")
        fields = "".join(f"<{k}>{v}</{k}>" for k, v in out.items())
        self._reply(200, "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'><s:Body>"
                         f"<u:{action}Response xmlns:u='{service}'>{fields}</u:{action}Response>"
                         "</s:Body></s:Envelope>")


def last(action: str) -> dict[str, str] | None:
    for name, args in reversed(RENDERER.calls):
        if name == action:
            return args
    return None


async def main() -> None:
    headers = upnp.parse_ssdp(SSDP_REPLY)
    check("an SSDP answer yields its location",
          headers is not None
          and headers["location"] == "http://192.168.1.9:1400/xml/device_description.xml")
    check("an SSDP search from someone else is ignored",
          upnp.parse_ssdp(b"M-SEARCH * HTTP/1.1\r\nST: ssdp:all\r\n\r\n") is None)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRenderer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    location = f"http://127.0.0.1:{server.server_address[1]}/desc.xml"

    async def no_multicast(wait: float = 0.0) -> set[str]:
        return set()
    upnp.discover = no_multicast

    backend = upnp.UpnpBackend({"id": "upnp-test", "name": "Network",
                                "type": "upnp", "renderers": [location]})
    announced: list[int] = []
    backend.watch_sinks(lambda: announced.append(len(backend.sinks())))
    try:
        await backend.connect()
        sinks = backend.sinks()
        check("the configured renderer is found", len(sinks) == 1 and announced == [1])
        sink = sinks[0]
        check("the embedded renderer is used, not its parent",
              (sink.id, sink.name) == ("upnp:upnp-test:uuid:fake-1", "Kitchen Speaker"))
        check("relative control URLs resolve against the description",
              sink.renderer.rc_url == location.rsplit("/", 1)[0] + "/rc")
        check("the backend has no library", not backend.has_library
              and await backend.albums() == [])

        ended: list[str] = []

        async def on_ended(reason: str) -> None:
            ended.append(reason)
        sink.wire(on_ended, lambda: None)
        await sink.start()
        check("start reads the volume", abs(sink.state.volume - 0.3) < 1e-9)

        track = Track(id="1", title="Rock & <Roll>", artist="A \"B\" C",
                      album="Live", duration=180.0, backend="subsonic", source="nav")
        await sink.play(StreamTarget(url="http://music/1.flac?a=1&b=2", source="nav"), track)
        args = last("SetAVTransportURI") or {}
        didl = ElementTree.fromstring(args.get("CurrentURIMetaData", "<x/>"))
        ns = {"dc": "http://purl.org/dc/elements/1.1/",
              "d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"}
        res = didl.find("d:item/d:res", ns)
        check("the stream URL arrives intact",
              args.get("CurrentURI") == "http://music/1.flac?a=1&b=2"
              and res is not None and res.text == "http://music/1.flac?a=1&b=2")
        check("metadata survives escaping",
              didl.findtext("d:item/dc:title", namespaces=ns) == "Rock & <Roll>"
              and didl.findtext("d:item/dc:creator", namespaces=ns) == 'A "B" C')
        check("the mime type is guessed from the extension",
              res is not None and res.get("protocolInfo") == "http-get:*:audio/flac:*")
        check("play reports playing", sink.state.status == "playing" and ended == [])

        RENDERER.rel_time, RENDERER.duration = "0:00:05", "0:03:00"
        await sink._sync()
        RENDERER.state = "STOPPED"
        await sink._sync()
        check("a stop from elsewhere is no eof",
              sink.state.status == "stopped" and ended == [])

        await sink.play(StreamTarget(url="http://music/1.flac", source="nav"), track)
        RENDERER.rel_time = "0:02:59"
        await sink._sync()
        RENDERER.state, RENDERER.rel_time = "NO_MEDIA_PRESENT", "0:00:00"
        await sink._sync()
        check("stopping at the end is eof", ended == ["eof"])

        ended.clear()
        await sink.play(StreamTarget(url="http://music/1.flac", source="nav"), track)
        await sink.stop()
        await sink._sync()
        check("our own stop is no eof", ended == [] and last("Stop") is not None)

        await sink.play(StreamTarget(url="http://music/1.flac", source="nav"), track)
        await sink.pause()
        check("pause", sink.state.status == "paused")
        await sink.resume()
        await sink.seek(75.5)
        check("resume and seek", sink.state.status == "playing"
              and (last("Seek") or {}).get("Target") == "0:01:15")

        try:
            await sink.play(StreamTarget(url="file:///music/1.flac", source="mpd"), track)
            check("a local file is refused", False)
        except BackendError:
            check("a local file is refused", True)
        check("MPD tracks are not offered to a renderer",
              not sink.plays(Track(id="x", title="x", backend="mpd"))
              and sink.plays(Track(id="x", title="x", backend="jellyfin")))

        await sink.set_volume(0.55)
        check("volume", RENDERER.volume == 55 and sink.capabilities()["volume"])

        RENDERER.online = False
        await backend.refresh()
        check("one missed round keeps the renderer", len(backend.sinks()) == 1)
        await backend.refresh()
        check("a renderer that stops answering is dropped",
              backend.sinks() == [] and announced == [1, 0])
        RENDERER.online = True
        await backend.refresh()
        check("it comes back", len(backend.sinks()) == 1 and announced == [1, 0, 1])

        await sink.close()
    finally:
        await backend.close()
        server.shutdown()

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    asyncio.run(main())

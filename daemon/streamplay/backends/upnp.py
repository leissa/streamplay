"""UPnP/DLNA media renderers as outputs: :class:`UpnpBackend` finds them, each is a :class:`UpnpSink`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

import requests

from ..models import Album, Artist, Track
from .base import Backend, BackendError, Sink, StreamTarget

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
#: Some renderers answer only a search for the device type.
SEARCH_TARGETS = (AVTRANSPORT, "urn:schemas-upnp-org:device:MediaRenderer:1")

DISCOVERY_WAIT = 3.0
REDISCOVER_INTERVAL = 60.0
#: Rounds a known renderer may miss before it is dropped, so one lost packet does not cut playback.
MISSES_BEFORE_DROP = 2

EOF_SLACK = 2.0
#: A renderer may sit in STOPPED for a moment after Play before it moves on.
START_GRACE = 8.0

MIME_TYPES = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
    "oga": "audio/ogg", "opus": "audio/ogg", "m4a": "audio/mp4",
    "mp4": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav",
}

SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body><u:{action} xmlns:u="{service}">{args}</u:{action}></s:Body>'
    '</s:Envelope>'
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _hms(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _seconds(value: str) -> float | None:
    try:
        parts = [float(p) for p in value.strip().split(":")]
    except ValueError:
        return None
    if len(parts) != 3:
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_ssdp(data: bytes) -> dict[str, str] | None:
    """Headers of an M-SEARCH answer, keys lower-cased, or None for anything else."""
    lines = data.decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0].upper().startswith("HTTP/1.1 200"):
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return headers if headers.get("location") else None


class _SsdpListener(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.locations: set[str] = set()

    def datagram_received(self, data: bytes, addr: Any) -> None:
        headers = parse_ssdp(data)
        if headers is not None:
            self.locations.add(headers["location"])


async def discover(wait: float = DISCOVERY_WAIT) -> set[str]:
    """Device description URLs of the renderers answering an SSDP search."""
    loop = asyncio.get_running_loop()
    listener = _SsdpListener()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: listener, local_addr=("0.0.0.0", 0), family=socket.AF_INET)
    try:
        sock = transport.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for target in SEARCH_TARGETS:
            message = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 2\r\n"
                f"ST: {target}\r\n\r\n"
            )
            transport.sendto(message.encode("ascii"), SSDP_ADDR)
        await asyncio.sleep(wait)
    finally:
        transport.close()
    return listener.locations


@dataclass
class Renderer:
    udn: str
    name: str
    location: str
    av_url: str
    av_type: str
    rc_url: str | None = None
    rc_type: str | None = None


def parse_description(xml_text: str | bytes, location: str) -> Renderer | None:
    """The (possibly embedded) device offering AVTransport, or None."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None
    base = _child_text(root, "URLBase") or location
    for device in root.iter():
        if _local(device.tag) != "device":
            continue
        services: dict[str, tuple[str, str]] = {}
        for service_list in device:
            if _local(service_list.tag) != "serviceList":
                continue
            for service in service_list:
                kind = _child_text(service, "serviceType")
                control = _child_text(service, "controlURL")
                if kind and control:
                    name = kind.split(":")[-2] if kind.count(":") >= 4 else kind
                    services[name] = (urljoin(base, control), kind)
        if "AVTransport" not in services:
            continue
        av_url, av_type = services["AVTransport"]
        rc_url, rc_type = services.get("RenderingControl", (None, None))
        udn = _child_text(device, "UDN") or location
        return Renderer(udn=udn, name=_child_text(device, "friendlyName") or udn,
                        location=location, av_url=av_url, av_type=av_type,
                        rc_url=rc_url, rc_type=rc_type)
    return None


class UpnpBackend(Backend):
    """Finds renderers on the network; there is no library to browse."""

    kind = "upnp"
    has_library = False

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        configured = profile.get("renderers") or []
        if isinstance(configured, str):
            configured = configured.split()
        self.configured = [str(url).strip() for url in configured if str(url).strip()]
        self.session = requests.Session()
        self._sinks: dict[str, UpnpSink] = {}
        self._misses: dict[str, int] = {}
        self._task: asyncio.Task | None = None


    def _describe_sync(self, location: str) -> Renderer | None:
        try:
            resp = self.session.get(location, timeout=(3, 5))
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.debug("no description at %s: %s", location, exc)
            return None
        return parse_description(resp.content, location)

    def soap_sync(self, url: str, service: str, action: str,
                  args: list[tuple[str, str]]) -> dict[str, str]:
        body = SOAP_ENVELOPE.format(
            action=action, service=service,
            args="".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args))
        try:
            resp = self.session.post(url, data=body.encode("utf-8"), timeout=(3, 10), headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{service}#{action}"',
            })
        except requests.RequestException as exc:
            raise BackendError(f"{action}: {exc}") from exc
        try:
            root = ElementTree.fromstring(resp.content)
        except ElementTree.ParseError as exc:
            raise BackendError(f"{action}: malformed SOAP reply") from exc
        if resp.status_code != 200:
            code = description = ""
            for element in root.iter():
                if _local(element.tag) == "errorCode":
                    code = (element.text or "").strip()
                elif _local(element.tag) == "errorDescription":
                    description = (element.text or "").strip()
            raise BackendError(f"{action} failed: {description or 'UPnP error'} {code}".strip())
        for element in root.iter():
            if _local(element.tag) == action + "Response":
                return {_local(child.tag): (child.text or "") for child in element}
        return {}

    async def soap(self, url: str, service: str, action: str,
                   *args: tuple[str, str]) -> dict[str, str]:
        return await asyncio.to_thread(self.soap_sync, url, service, action, list(args))


    async def connect(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._rediscover_loop(),
                                         name=f"upnp-discover-{self.source}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await asyncio.to_thread(self.session.close)

    async def _rediscover_loop(self) -> None:
        while True:
            await asyncio.sleep(REDISCOVER_INTERVAL)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s discovery: %s", self.name, exc)

    async def refresh(self) -> None:
        """Search the network again and reconcile the renderers with what answered."""
        try:
            found = await discover()
        except OSError as exc:
            log.info("%s: SSDP search failed: %s", self.name, exc)
            found = set()
        known = {sink.renderer.location for sink in self._sinks.values()}
        locations = sorted(found | set(self.configured) | known)
        described = await asyncio.gather(
            *(asyncio.to_thread(self._describe_sync, loc) for loc in locations))

        changed = False
        answered: set[str] = set()
        for renderer in described:
            if renderer is None or renderer.udn in answered:
                continue
            answered.add(renderer.udn)
            self._misses.pop(renderer.udn, None)
            sink = self._sinks.get(renderer.udn)
            if sink is None:
                self._sinks[renderer.udn] = UpnpSink(self, renderer)
                log.info("%s: found renderer %s", self.name, renderer.name)
                changed = True
            else:
                sink.renderer = renderer

        for udn in [u for u in self._sinks if u not in answered]:
            self._misses[udn] = self._misses.get(udn, 0) + 1
            if self._misses[udn] >= MISSES_BEFORE_DROP:
                log.info("%s: renderer %s is gone", self.name, self._sinks[udn].name)
                del self._sinks[udn]
                del self._misses[udn]
                changed = True
        if changed:
            self.sinks_changed()

    def sinks(self) -> list[Sink]:
        return list(self._sinks.values())


    async def artists(self) -> list[Artist]:
        return []

    async def artist_albums(self, artist_id: str) -> list[Album]:
        return []

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        return []

    async def album_tracks(self, album_id: str) -> list[Track]:
        return []

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        return {"artists": [], "albums": [], "tracks": []}

    async def stream_target(self, track: Track) -> StreamTarget:
        raise BackendError(f"{self.name} has no music of its own")


class UpnpSink(Sink):
    """Plays one track at a time on a renderer through AVTransport."""

    POLL_INTERVAL = 1.0

    def __init__(self, backend: UpnpBackend, renderer: Renderer) -> None:
        super().__init__()
        self.backend = backend
        self.renderer = renderer
        self.source = backend.source
        self.id = f"upnp:{backend.source}:{renderer.udn}"
        self.name = renderer.name
        #: Set while we stop or replace the track, when the renderer passes through STOPPED.
        self._changing = False
        #: Whether the renderer has reported PLAYING since our last play.
        self._seen_playing = False
        self._played_at = 0.0
        self._position_at = 0.0
        self._poll_task: asyncio.Task | None = None

    async def _av(self, action: str, *args: tuple[str, str]) -> dict[str, str]:
        return await self.backend.soap(self.renderer.av_url, self.renderer.av_type,
                                       action, ("InstanceID", "0"), *args)

    async def _rc(self, action: str, *args: tuple[str, str]) -> dict[str, str]:
        return await self.backend.soap(self.renderer.rc_url, self.renderer.rc_type,
                                       action, ("InstanceID", "0"),
                                       ("Channel", "Master"), *args)

    async def start(self) -> None:
        if self.renderer.rc_url:
            try:
                reply = await self._rc("GetVolume")
                self.state.volume = max(0.0, min(1.0, int(reply.get("CurrentVolume") or 0) / 100))
            except (BackendError, ValueError) as exc:
                log.debug("%s volume: %s", self.name, exc)
        self._poll_task = asyncio.create_task(self._poll_loop(),
                                              name=f"upnp-poll-{self.renderer.udn}")

    async def close(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None


    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                if self.state.status != "stopped":
                    await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s poll: %s", self.name, exc)

    async def _sync(self) -> None:
        try:
            transport = await self._av("GetTransportInfo")
            position = await self._av("GetPositionInfo")
        except BackendError as exc:
            self.state.error = str(exc)
            self._changed()
            return
        if self._changing:
            return

        was, near_end = self.state.status, self._near_end()
        current = transport.get("CurrentTransportState", "")
        if current == "PLAYING":
            self._seen_playing = True
        starting = (not self._seen_playing
                    and time.monotonic() - self._played_at < START_GRACE)
        self.state.buffering = current == "TRANSITIONING" or (
            starting and current == "STOPPED")

        if current == "PLAYING" or self.state.buffering:
            self.state.status = "playing"
        elif current.startswith("PAUSED"):
            self.state.status = "paused"
        else:
            self.state.status = "stopped"
        elapsed = _seconds(position.get("RelTime", ""))
        if elapsed is not None:
            self._note_position(elapsed)
        duration = _seconds(position.get("TrackDuration", ""))
        if duration:
            self.state.duration = duration

        if transport.get("CurrentTransportStatus") == "ERROR_OCCURRED":
            self.state.error = f"{self.name} could not play the track"
            self.state.status = "stopped"
            await self._ended("error")
            return
        self.state.error = None

        if self.state.status == "stopped" and was == "playing" and self._seen_playing:
            self._note_position(0.0)
            if near_end:
                await self._ended("eof")
                return
            # Otherwise someone stopped the renderer from its own remote.
        self._changed()

    def _note_position(self, position: float) -> None:
        self.state.position = position
        self._position_at = time.monotonic()

    def _near_end(self) -> bool:
        if self.state.duration <= 0:
            return True
        position = self.state.position
        if self.state.status == "playing":
            position += max(0.0, time.monotonic() - self._position_at)
        slack = min(EOF_SLACK, self.state.duration / 2)
        return position >= self.state.duration - slack


    def plays(self, track: Track) -> bool:
        return track.backend != "mpd"

    def _mime(self, url: str) -> str:
        parts = urlsplit(url)
        ext = parts.path.rsplit(".", 1)[-1].lower() if "." in parts.path.rsplit("/", 1)[-1] else ""
        fmt = (parse_qs(parts.query).get("format") or [""])[0].lower()
        if ext in MIME_TYPES or fmt in MIME_TYPES:
            return MIME_TYPES.get(ext) or MIME_TYPES[fmt]
        try:
            with self.backend.session.get(url, stream=True, timeout=(3, 5)) as resp:
                kind = resp.headers.get("Content-Type", "").split(";")[0].strip()
        except requests.RequestException:
            kind = ""
        return kind if kind.startswith("audio/") else "*"

    @staticmethod
    def _didl(track: Track, url: str, mime: str) -> str:
        fields = [
            ("dc:title", track.title),
            ("dc:creator", track.artist),
            ("upnp:artist", track.artist),
            ("upnp:album", track.album),
        ]
        body = "".join(f"<{tag}>{escape(value)}</{tag}>" for tag, value in fields if value)
        duration = f" duration={quoteattr(_hms(track.duration) + '.000')}" if track.duration else ""
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"{body}<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f"<res protocolInfo={quoteattr(f'http-get:*:{mime}:*')}{duration}>"
            f"{escape(url)}</res></item></DIDL-Lite>"
        )

    async def play(self, target: StreamTarget, track: Track) -> None:
        url = target.url or ""
        if not url.startswith(("http://", "https://")):
            raise BackendError(f"{self.name} can only play web streams")
        mime = await asyncio.to_thread(self._mime, url)

        self._changing = True
        try:
            if self.state.status != "stopped":
                try:
                    await self._av("Stop")
                except BackendError as exc:
                    log.debug("%s stop: %s", self.name, exc)
            await self._av("SetAVTransportURI", ("CurrentURI", url),
                           ("CurrentURIMetaData", self._didl(track, url, mime)))
            await self._av("Play", ("Speed", "1"))
        finally:
            self._changing = False
        self._seen_playing = False
        self._played_at = time.monotonic()
        self.state.status = "playing"
        self.state.buffering = True
        self._note_position(0.0)
        self.state.duration = track.duration
        self.state.error = None
        self._changed()
        await self._sync()

    async def resume(self) -> None:
        await self._av("Play", ("Speed", "1"))
        await self._sync()

    async def pause(self) -> None:
        if self.state.status == "stopped":
            return
        await self._av("Pause")
        await self._sync()

    async def stop(self) -> None:
        self._changing = True
        try:
            await self._av("Stop")
        except BackendError as exc:
            log.debug("%s stop: %s", self.name, exc)
        finally:
            self._changing = False
        self.state.status = "stopped"
        self.state.buffering = False
        self._note_position(0.0)
        self._changed()

    async def seek(self, position: float) -> None:
        try:
            await self._av("Seek", ("Unit", "REL_TIME"), ("Target", _hms(position)))
        except BackendError as exc:
            log.debug("%s seek: %s", self.name, exc)
            return
        self._note_position(max(0.0, position))
        self._changed()

    async def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        if self.renderer.rc_url:
            try:
                await self._rc("SetVolume", ("DesiredVolume", str(round(volume * 100))))
            except BackendError as exc:
                log.debug("%s volume: %s", self.name, exc)
                return
        self.state.volume = volume
        self._changed()

    def capabilities(self) -> dict[str, bool]:
        return {"seek": True, "volume": bool(self.renderer.rc_url)}

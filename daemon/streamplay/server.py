"""Localhost control surface for the applet: a WebSocket plus an HTTP route for
cover art, both on one loopback port.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from .backends import BACKEND_TYPES, BackendError, create_backend
from .hub import Hub

log = logging.getLogger(__name__)

Handler = Callable[[Hub, dict[str, Any]], Awaitable[Any]]
METHODS: dict[str, Handler] = {}

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".img": "application/octet-stream",
}


def method(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        METHODS[name] = fn
        return fn
    return register


def _source(params: dict[str, Any]) -> str | None:
    value = params.get("source")
    return str(value) if value else None


def _sort_albums(albums: list, sort: str) -> list:
    """Order an album list the way the user asked, since a merged list has no order.

    Sorts needing data we do not carry per album are left to the backend.
    """
    if sort == "alphabetical":
        albums.sort(key=lambda a: a.name.lower())
    elif sort == "artist":
        albums.sort(key=lambda a: (a.artist.lower(), a.year or 0))
    elif sort == "byYear":
        # Undated albums go last either way, rather than heading the list.
        albums.sort(key=lambda a: (a.year is None, a.year or 0, a.name.lower()))
    elif sort == "byYearDesc":
        albums.sort(key=lambda a: (a.year is None, -(a.year or 0),
                                   a.name.lower()))
    return albums



@method("hello")
async def _hello(hub: Hub, params: dict) -> Any:
    return hub.snapshot()


@method("system.status")
async def _status(hub: Hub, params: dict) -> Any:
    return {"sources": hub.sources_json(), "outputs": hub.outputs_json(),
            "backendTypes": sorted(BACKEND_TYPES)}



def _announce_profiles(hub: Hub) -> None:
    hub.emit("profiles", {"profiles": hub.config.redacted_profiles()})
    hub.emit("sources", hub.sources_json())


@method("profiles.list")
async def _profiles_list(hub: Hub, params: dict) -> Any:
    return {"profiles": hub.config.redacted_profiles()}


@method("profiles.save")
async def _profiles_save(hub: Hub, params: dict) -> Any:
    saved = hub.config.upsert(dict(params.get("profile") or params))
    _announce_profiles(hub)
    if params.get("connect", True) and saved.get("enabled", True):
        try:
            await hub.connect_source(saved.id)
        except BackendError as exc:
            return {"profile": saved.redacted(), "connectError": str(exc)}
    return {"profile": saved.redacted()}


@method("profiles.delete")
async def _profiles_delete(hub: Hub, params: dict) -> Any:
    profile_id = str(params.get("id") or "")
    removed = hub.config.delete(profile_id)
    if removed:
        await hub.disconnect_source(profile_id)
    _announce_profiles(hub)
    return {"removed": removed}


@method("profiles.test")
async def _profiles_test(hub: Hub, params: dict) -> Any:
    """Dry-run a profile without disturbing any live connection."""
    submitted = dict(params.get("profile") or params)
    stored = hub.config.profiles.get(str(submitted.get("id") or ""))
    if stored and not submitted.get("password"):
        submitted["password"] = stored.get("password", "")

    backend = create_backend(submitted)
    try:
        await backend.connect()
    finally:
        await backend.close()
    return {"ok": True}



@method("sources.list")
async def _sources_list(hub: Hub, params: dict) -> Any:
    return {"sources": hub.sources_json()}


@method("sources.connect")
async def _sources_connect(hub: Hub, params: dict) -> Any:
    profile_id = str(params.get("id") or "")
    profile = hub.config.profiles.get(profile_id)
    if profile is not None and not profile.get("enabled", True):
        hub.config.upsert({"id": profile_id, "enabled": True})
    await hub.connect_source(profile_id)
    return {"sources": hub.sources_json()}


@method("sources.disconnect")
async def _sources_disconnect(hub: Hub, params: dict) -> Any:
    profile_id = str(params.get("id") or "")
    if params.get("remember", True):
        hub.config.upsert({"id": profile_id, "enabled": False})
    await hub.disconnect_source(profile_id)
    return {"sources": hub.sources_json()}


@method("sources.reconnectAll")
async def _sources_reconnect(hub: Hub, params: dict) -> Any:
    await hub.reconnect_all()
    return {"sources": hub.sources_json()}



@method("outputs.list")
async def _outputs_list(hub: Hub, params: dict) -> Any:
    return {"outputs": hub.outputs_json()}


@method("outputs.set")
async def _outputs_set(hub: Hub, params: dict) -> Any:
    await hub.set_output(str(params.get("id") or "local"))
    return {"outputs": hub.outputs_json()}



def _simple(name: str, attr: str) -> None:
    async def handler(hub: Hub, params: dict) -> Any:
        await getattr(hub.player, attr)()
        return {"ok": True}
    METHODS[name] = handler


for _name, _attr in (
    ("player.play", "play"),
    ("player.pause", "pause"),
    ("player.playPause", "play_pause"),
    ("player.stop", "stop"),
    ("player.next", "next"),
    ("player.previous", "previous"),
):
    _simple(_name, _attr)


@method("player.seek")
async def _seek(hub: Hub, params: dict) -> Any:
    await hub.player.seek(float(params.get("position") or 0.0))
    return {"ok": True}


@method("player.seekRelative")
async def _seek_relative(hub: Hub, params: dict) -> Any:
    await hub.player.seek_relative(float(params.get("offset") or 0.0))
    return {"ok": True}


@method("player.setVolume")
async def _set_volume(hub: Hub, params: dict) -> Any:
    await hub.player.set_volume(float(params.get("volume") or 0.0))
    return {"ok": True}


@method("player.setShuffle")
async def _set_shuffle(hub: Hub, params: dict) -> Any:
    await hub.player.set_shuffle(bool(params.get("shuffle")))
    return {"ok": True}


@method("player.setRepeat")
async def _set_repeat(hub: Hub, params: dict) -> Any:
    await hub.player.set_repeat(str(params.get("mode") or "none"))
    return {"ok": True}



@method("queue.get")
async def _queue_get(hub: Hub, params: dict) -> Any:
    return hub.player.queue()


@method("queue.add")
async def _queue_add(hub: Hub, params: dict) -> Any:
    tracks = await hub.tracks_for(params)
    if not tracks:
        return {"added": 0}
    await hub.player.enqueue(
        tracks,
        mode=str(params.get("mode") or "append"),
        start=bool(params.get("play")),
    )
    return {"added": len(tracks)}


@method("queue.remove")
async def _queue_remove(hub: Hub, params: dict) -> Any:
    indexes = params.get("indexes")
    if indexes is None and params.get("index") is not None:
        indexes = [params["index"]]
    await hub.player.remove(int(i) for i in indexes or [])
    return {"ok": True}


@method("queue.move")
async def _queue_move(hub: Hub, params: dict) -> Any:
    await hub.player.move(int(params.get("from", 0)), int(params.get("to", 0)))
    return {"ok": True}


@method("queue.clear")
async def _queue_clear(hub: Hub, params: dict) -> Any:
    await hub.player.clear()
    return {"ok": True}


@method("queue.playIndex")
async def _queue_play_index(hub: Hub, params: dict) -> Any:
    await hub.player.play_index(int(params.get("index", 0)))
    return {"ok": True}


# Every library call takes an optional ``source``; without one it fans out and merges.

@method("library.artists")
async def _artists(hub: Hub, params: dict) -> Any:
    artists = await hub.gather(_source(params), lambda b: b.artists())
    artists.sort(key=lambda a: a.name.lower())
    return {"artists": [a.to_json() for a in artists]}


@method("library.artistAlbums")
async def _artist_albums(hub: Hub, params: dict) -> Any:
    backend = hub.backend(_source(params))
    albums = await backend.artist_albums(str(params.get("id") or ""))
    # A discography reads best in year order, whatever the album list uses.
    sort = str(params.get("sort") or "byYear")
    return {"albums": [a.to_json() for a in _sort_albums(albums, sort)]}


@method("library.albums")
async def _albums(hub: Hub, params: dict) -> Any:
    sort = str(params.get("sort") or "alphabetical")
    offset = int(params.get("offset") or 0)
    limit = int(params.get("limit") or 100)
    albums = await hub.gather(
        _source(params), lambda b: b.albums(sort, offset, limit))
    return {"albums": [a.to_json() for a in _sort_albums(albums, sort)]}


@method("library.albumTracks")
async def _album_tracks(hub: Hub, params: dict) -> Any:
    backend = hub.backend(_source(params))
    tracks = await backend.album_tracks(str(params.get("id") or ""))
    return {"tracks": [t.to_json() for t in tracks]}


@method("library.search")
async def _search(hub: Hub, params: dict) -> Any:
    query = str(params.get("query") or "")
    limit = int(params.get("limit") or 40)
    if not query.strip():
        return {"artists": [], "albums": [], "tracks": []}

    backends = hub.selected(_source(params))
    results = await asyncio.gather(
        *(b.search(query, limit) for b in backends), return_exceptions=True)

    merged: dict[str, list] = {"artists": [], "albums": [], "tracks": []}
    for backend, result in zip(backends, results):
        if isinstance(result, Exception):
            log.info("search on %s failed: %s", backend.name, result)
            continue
        for key in merged:
            merged[key].extend(result.get(key) or [])
    return {key: [item.to_json() for item in value]
            for key, value in merged.items()}


@method("library.genres")
async def _genres(hub: Hub, params: dict) -> Any:
    names = await hub.gather(_source(params), lambda b: b.genres())
    return {"genres": sorted({n for n in names if n}, key=str.lower)}


@method("library.genreAlbums")
async def _genre_albums(hub: Hub, params: dict) -> Any:
    genre = str(params.get("genre") or "")
    offset = int(params.get("offset") or 0)
    limit = int(params.get("limit") or 100)
    sort = str(params.get("sort") or "alphabetical")
    albums = await hub.gather(
        _source(params), lambda b: b.genre_albums(genre, offset, limit))
    return {"albums": [a.to_json() for a in _sort_albums(albums, sort)]}


@method("library.playlists")
async def _playlists(hub: Hub, params: dict) -> Any:
    playlists = await hub.gather(_source(params), lambda b: b.playlists())
    return {"playlists": playlists}


@method("library.playlistTracks")
async def _playlist_tracks(hub: Hub, params: dict) -> Any:
    backend = hub.backend(_source(params))
    tracks = await backend.playlist_tracks(str(params.get("id") or ""))
    return {"tracks": [t.to_json() for t in tracks]}



@method("lyrics.get")
async def _lyrics_get(hub: Hub, params: dict) -> Any:
    return await hub.lyrics.fetch(
        str(params.get("title") or ""), str(params.get("artist") or ""),
        str(params.get("album") or ""), float(params.get("duration") or 0.0))



@method("settings.set")
async def _settings_set(hub: Hub, params: dict) -> Any:
    for key, value in (params.get("settings") or {}).items():
        hub.config.set_setting(key, value)
    return {"settings": hub.config.settings}



class ControlServer:
    def __init__(self, hub: Hub, host: str = "127.0.0.1", port: int = 8760) -> None:
        self.hub = hub
        self.host = host
        self.port = port
        self._server = None

    async def serve_forever(self) -> None:
        self._server = await serve(
            self._handle, self.host, self.port,
            process_request=self._http,
            ping_interval=20, ping_timeout=20,
            max_size=4 * 1024 * 1024,
        )
        log.info("control server listening on %s:%d", self.host, self.port)
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


    async def _http(self, connection: ServerConnection,
                    request: Request) -> Response | None:
        parsed = urlparse(request.path)
        if parsed.path in ("/", "/ws"):
            return None  # let the WebSocket handshake proceed
        if parsed.path == "/health":
            return _json_response(200, {"ok": True,
                                        "sources": self.hub.sources_json()})
        if parsed.path == "/cover":
            return await self._serve_cover(parse_qs(parsed.query))
        return _json_response(404, {"error": "not found"})

    async def _serve_cover(self, query: dict[str, list[str]]) -> Response:
        cover_id = (query.get("id") or [""])[0]
        source = (query.get("src") or [""])[0]
        try:
            size = int((query.get("size") or ["0"])[0])
        except ValueError:
            size = 0

        backend = self.hub.sources.get(source)
        if not cover_id or backend is None:
            return _json_response(404, {"error": "no cover"})

        path = await self.hub.covers.fetch(backend, source, cover_id, size)
        if path is None or not path.exists():
            return _json_response(404, {"error": "no cover"})

        body = await asyncio.to_thread(path.read_bytes)
        headers = Headers({
            "Content-Type": MIME_BY_SUFFIX.get(path.suffix, "image/jpeg"),
            "Content-Length": str(len(body)),
            "Cache-Control": "max-age=86400",
        })
        return Response(200, "OK", headers, body)


    async def _handle(self, connection: ServerConnection) -> None:
        loop = asyncio.get_running_loop()
        outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=256)

        def push(event: str, data: dict) -> None:
            payload = json.dumps({"event": event, "data": data})
            try:
                outbox.put_nowait(payload)
            except asyncio.QueueFull:
                log.debug("dropping %s for a slow client", event)

        self.hub.subscribe(push)
        writer = loop.create_task(self._writer(connection, outbox))
        try:
            async for raw in connection:
                await self._on_message(connection, raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.hub.unsubscribe(push)
            writer.cancel()

    @staticmethod
    async def _writer(connection: ServerConnection,
                      outbox: asyncio.Queue[str]) -> None:
        try:
            while True:
                await connection.send(await outbox.get())
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            pass

    async def _on_message(self, connection: ServerConnection, raw: Any) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        call_id = message.get("id")
        name = str(message.get("method") or "")
        params = message.get("params") or {}

        handler = METHODS.get(name)
        if handler is None:
            await self._reply(connection, call_id, error=f"Unknown method {name!r}")
            return

        try:
            result = await handler(self.hub, params)
        except BackendError as exc:
            await self._reply(connection, call_id, error=str(exc))
        except Exception as exc:
            log.exception("method %s failed", name)
            await self._reply(connection, call_id, error=f"{type(exc).__name__}: {exc}")
        else:
            await self._reply(connection, call_id, result=result)

    @staticmethod
    async def _reply(connection: ServerConnection, call_id: Any,
                     result: Any = None, error: str | None = None) -> None:
        if call_id is None:
            return
        payload = {"id": call_id, "ok": error is None}
        if error is None:
            payload["result"] = result
        else:
            payload["error"] = error
        try:
            await connection.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            pass


def _json_response(status: int, payload: dict) -> Response:
    body = json.dumps(payload).encode("utf-8")
    headers = Headers({
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    })
    return Response(status, "OK" if status == 200 else "Error", headers, body)

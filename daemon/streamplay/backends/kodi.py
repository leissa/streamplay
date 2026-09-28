"""Kodi over JSON-RPC, as both a library (:class:`KodiBackend`) and an output
(:class:`KodiSink`).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any
from urllib.parse import quote

import requests
import websockets

from ..models import Album, Artist, Track
from .base import Backend, BackendError, Sink, StreamTarget

log = logging.getLogger(__name__)

SONG_PROPERTIES = [
    "title", "artist", "artistid", "album", "albumid", "duration",
    "track", "disc", "year", "genre", "thumbnail", "file",
]
ALBUM_PROPERTIES = [
    "title", "artist", "artistid", "year", "genre", "thumbnail",
]
ARTIST_PROPERTIES = ["thumbnail"]

ALBUM_SORTS = {
    "alphabetical": ("album", "ascending"),
    "artist": ("artist", "ascending"),
    "newest": ("dateadded", "descending"),
    "recent": ("lastplayed", "descending"),
    "frequent": ("playcount", "descending"),
    "random": ("random", "ascending"),
    "byYear": ("year", "ascending"),
    "byYearDesc": ("year", "descending"),
}


def _hms(seconds: float) -> dict[str, int]:
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    return {
        "hours": whole // 3600,
        "minutes": (whole % 3600) // 60,
        "seconds": whole % 60,
        "milliseconds": int(round((seconds - whole) * 1000)),
    }


def _seconds(time_obj: dict[str, Any] | None) -> float:
    if not time_obj:
        return 0.0
    return (
        time_obj.get("hours", 0) * 3600
        + time_obj.get("minutes", 0) * 60
        + time_obj.get("seconds", 0)
        + time_obj.get("milliseconds", 0) / 1000.0
    )


def _first(value: Any) -> str:
    """Kodi returns artist/genre as a list; flatten it for display."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v)
    return str(value or "")


class KodiBackend(Backend):
    kind = "kodi"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        host = str(profile.get("host") or "").strip()
        if not host:
            raise BackendError("No Kodi host configured")
        scheme = "https" if profile.get("useTls") else "http"
        self.scheme = scheme
        self.host = host
        self.port = int(profile.get("port") or 8080)
        self.ws_port = int(profile.get("wsPort") or 9090)
        self.origin = f"{scheme}://{host}:{self.port}"
        self.rpc_url = f"{self.origin}/jsonrpc"
        self.verify_tls = profile.get("verifyTls", True)

        self.username = str(profile.get("username") or "")
        self.password = str(profile.get("password") or "")
        self._session = requests.Session()
        if self.username:
            self._session.auth = (self.username, self.password)
        self._session.headers["Content-Type"] = "application/json"
        self._ids = itertools.count(1)
        self._sink = KodiSink(self)

    def sinks(self) -> list[Sink]:
        return [self._sink]


    def _call_sync(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or {},
        }
        try:
            resp = self._session.post(
                self.rpc_url, data=json.dumps(payload),
                timeout=(5, 20), verify=self.verify_tls,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise BackendError(f"{self.name}: malformed JSON-RPC reply") from exc

        if "error" in body:
            err = body["error"]
            raise BackendError(err.get("message") or f"Kodi error {err.get('code')}")
        return body.get("result")

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._call_sync, method, params)


    async def connect(self) -> None:
        result = await self.call("JSONRPC.Version")
        version = (result or {}).get("version") or {}
        if version.get("major", 0) < 6:
            raise BackendError("Kodi JSON-RPC version 6 or newer is required")

    async def close(self) -> None:
        await asyncio.to_thread(self._session.close)


    def _track(self, song: dict[str, Any]) -> Track:
        song_id = song.get("songid", song.get("id"))
        artist_ids = song.get("artistid") or []
        return Track(
            id=str(song_id),
            title=song.get("title") or song.get("label") or "Unknown",
            artist=_first(song.get("artist")),
            album=song.get("album") or "",
            duration=float(song.get("duration") or 0),
            backend=self.kind,
            source=self.source,
            artist_id=str(artist_ids[0]) if artist_ids else None,
            album_id=str(song["albumid"]) if song.get("albumid") else None,
            track_no=song.get("track") or None,
            disc_no=song.get("disc") or None,
            year=song.get("year") or None,
            genre=_first(song.get("genre")) or None,
            cover_id=song.get("thumbnail") or None,
            extra={"songid": song_id, "file": song.get("file")},
        )

    def _album(self, album: dict[str, Any]) -> Album:
        artist_ids = album.get("artistid") or []
        return Album(
            id=str(album.get("albumid")),
            name=album.get("title") or album.get("label") or "Unknown",
            artist=_first(album.get("artist")),
            source=self.source,
            artist_id=str(artist_ids[0]) if artist_ids else None,
            year=album.get("year") or None,
            genre=_first(album.get("genre")) or None,
            cover_id=album.get("thumbnail") or None,
        )

    def _artist(self, artist: dict[str, Any]) -> Artist:
        return Artist(
            id=str(artist.get("artistid")),
            name=artist.get("artist") or artist.get("label") or "Unknown",
            source=self.source,
            cover_id=artist.get("thumbnail") or None,
        )


    async def artists(self) -> list[Artist]:
        result = await self.call("AudioLibrary.GetArtists", {
            "properties": ARTIST_PROPERTIES,
            "sort": {"method": "artist", "order": "ascending"},
            "albumartistsonly": True,
        })
        return [self._artist(a) for a in (result or {}).get("artists") or []]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        result = await self.call("AudioLibrary.GetAlbums", {
            "properties": ALBUM_PROPERTIES,
            "filter": {"artistid": int(artist_id)},
            "sort": {"method": "year", "order": "ascending"},
        })
        return [self._album(a) for a in (result or {}).get("albums") or []]

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        method, order = ALBUM_SORTS.get(sort, ALBUM_SORTS["alphabetical"])
        result = await self.call("AudioLibrary.GetAlbums", {
            "properties": ALBUM_PROPERTIES,
            "sort": {"method": method, "order": order},
            "limits": {"start": offset, "end": offset + limit},
        })
        return [self._album(a) for a in (result or {}).get("albums") or []]

    async def album_tracks(self, album_id: str) -> list[Track]:
        result = await self.call("AudioLibrary.GetSongs", {
            "properties": SONG_PROPERTIES,
            "filter": {"albumid": int(album_id)},
            "sort": {"method": "track", "order": "ascending"},
        })
        return [self._track(s) for s in (result or {}).get("songs") or []]

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        limits = {"start": 0, "end": limit}
        artists, albums, songs = await asyncio.gather(
            self.call("AudioLibrary.GetArtists", {
                "properties": ARTIST_PROPERTIES, "limits": limits,
                "filter": {"field": "artist", "operator": "contains", "value": query},
            }),
            self.call("AudioLibrary.GetAlbums", {
                "properties": ALBUM_PROPERTIES, "limits": limits,
                "filter": {"field": "album", "operator": "contains", "value": query},
            }),
            self.call("AudioLibrary.GetSongs", {
                "properties": SONG_PROPERTIES, "limits": limits,
                "filter": {"field": "title", "operator": "contains", "value": query},
            }),
            return_exceptions=True,
        )

        def unwrap(result: Any, key: str) -> list:
            if isinstance(result, Exception):
                log.debug("kodi search %s failed: %s", key, result)
                return []
            return (result or {}).get(key) or []

        return {
            "artists": [self._artist(a) for a in unwrap(artists, "artists")],
            "albums": [self._album(a) for a in unwrap(albums, "albums")],
            "tracks": [self._track(s) for s in unwrap(songs, "songs")],
        }

    async def genres(self) -> list[str]:
        result = await self.call("AudioLibrary.GetGenres", {
            "sort": {"method": "label", "order": "ascending"},
        })
        return [g.get("label") for g in (result or {}).get("genres") or []
                if g.get("label")]

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        result = await self.call("AudioLibrary.GetAlbums", {
            "properties": ALBUM_PROPERTIES,
            "filter": {"field": "genre", "operator": "is", "value": genre},
            "sort": {"method": "album", "order": "ascending"},
            "limits": {"start": offset, "end": offset + limit},
        })
        return [self._album(a) for a in (result or {}).get("albums") or []]


    async def stream_target(self, track: Track) -> StreamTarget:
        """Ask Kodi to expose the song over HTTP so any sink can play it."""
        native = None
        song_id = track.extra.get("songid")
        if song_id is not None:
            native = {"songid": int(song_id)}

        path = track.extra.get("file")
        if not path and song_id is not None:
            # The applet round-trips only public fields, so look the path up again.
            details = await self.call("AudioLibrary.GetSongDetails", {
                "songid": int(song_id), "properties": ["file"],
            })
            path = ((details or {}).get("songdetails") or {}).get("file")

        url = None
        if path:
            result = await self.call("Files.PrepareDownload", {"path": path})
            relative = ((result or {}).get("details") or {}).get("path")
            if relative:
                url = f"{self.origin}/{relative.lstrip('/')}"
                if self.username:
                    # mpv needs the credentials inline for Kodi's web server.
                    credentials = (f"{quote(self.username, safe='')}:"
                                   f"{quote(self.password, safe='')}@")
                    url = url.replace("://", "://" + credentials, 1)

        if url is None and native is None:
            raise BackendError(f"{self.name}: cannot play {track.title}")
        return StreamTarget(url=url, native=native, source=self.source)

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        if not cover_id:
            return None
        # Kodi's image endpoint takes the percent-encoded image:// URL.
        return f"{self.origin}/image/{quote(cover_id, safe='')}", {}, {}

    @property
    def http_session(self) -> requests.Session:
        return self._session


class KodiSink(Sink):
    """Plays one track at a time on a Kodi instance, leaving its own playlist alone.
    """

    POLL_INTERVAL = 1.0

    def __init__(self, backend: KodiBackend) -> None:
        super().__init__()
        self.backend = backend
        self.source = backend.source
        self.id = f"kodi:{backend.source}"
        self.name = backend.name
        self._player_id: int | None = None
        self._expect_stop = False
        self._notify_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._notify_task = asyncio.create_task(
            self._notification_loop(), name=f"kodi-notify-{self.backend.source}")
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"kodi-poll-{self.backend.source}")
        await self._refresh_volume()

    async def close(self) -> None:
        for task in (self._notify_task, self._poll_task):
            if task:
                task.cancel()
        self._notify_task = self._poll_task = None


    async def _notification_loop(self) -> None:
        url = f"ws://{self.backend.host}:{self.backend.ws_port}/jsonrpc"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    url, ping_interval=30, open_timeout=10
                ) as socket:
                    log.info("following Kodi notifications on %s", url)
                    backoff = 1.0
                    async for raw in socket:
                        try:
                            await self._on_notification(json.loads(raw))
                        except ValueError:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Kodi notification socket: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _on_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method") or ""
        data = (message.get("params") or {}).get("data") or {}

        if method == "Player.OnStop":
            if self._expect_stop:
                self._expect_stop = False
                return
            # ``end`` distinguishes "the track finished" from "someone hit stop".
            if data.get("end"):
                self.state.status = "stopped"
                await self._ended("eof")
            else:
                self.state.status = "stopped"
                self.state.position = 0.0
                self._changed()
        elif method in ("Player.OnPlay", "Player.OnResume", "Player.OnAVStart"):
            # Opening a file on an idle Kodi sends no OnStop to consume the flag.
            self._expect_stop = False
            self.state.status = "playing"
            await self._sync()
        elif method == "Player.OnPause":
            self.state.status = "paused"
            self._changed()
        elif method == "Player.OnSeek":
            await self._sync()
        elif method == "Application.OnVolumeChanged":
            self.state.volume = max(0.0, min(1.0, float(data.get("volume", 100)) / 100))
            self._changed()

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.POLL_INTERVAL)
                if self.state.status == "playing":
                    await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("kodi poll: %s", exc)

    async def _active_player(self) -> int | None:
        players = await self.backend.call("Player.GetActivePlayers") or []
        for player in players:
            if player.get("type") == "audio":
                return player.get("playerid")
        return players[0].get("playerid") if players else None

    async def _sync(self) -> None:
        try:
            self._player_id = await self._active_player()
            if self._player_id is None:
                if self.state.status != "stopped":
                    self.state.status = "stopped"
                    self._changed()
                return
            props = await self.backend.call("Player.GetProperties", {
                "playerid": self._player_id,
                "properties": ["speed", "time", "totaltime", "canseek"],
            }) or {}
        except BackendError as exc:
            self.state.error = str(exc)
            self._changed()
            return

        self.state.status = "playing" if props.get("speed") else "paused"
        self.state.position = _seconds(props.get("time"))
        self.state.duration = _seconds(props.get("totaltime"))
        self.state.error = None
        self._changed()

    async def _refresh_volume(self) -> None:
        try:
            app = await self.backend.call("Application.GetProperties", {
                "properties": ["volume"],
            }) or {}
            self.state.volume = max(0.0, min(1.0, float(app.get("volume", 100)) / 100))
            self._changed()
        except BackendError:
            pass


    def plays(self, track: Track) -> bool:
        return track.source == self.backend.source

    async def play(self, target: StreamTarget, track: Track, start: float = 0.0) -> None:
        if target.native and target.source == self.backend.source:
            item = dict(target.native)
        elif target.url:
            item = {"file": target.url}
        else:
            raise BackendError(f"{self.name} cannot play {track.title}")

        # We advance the queue ourselves, so Kodi must not do it as well.
        options: dict[str, Any] = {"repeat": "off", "shuffled": False}
        if start > 0:
            options["resume"] = _hms(start)
        self._expect_stop = True
        try:
            await self.backend.call("Player.Open", {"item": item, "options": options})
        except BackendError:
            self._expect_stop = False
            raise
        self.state.status = "playing"
        self.state.position = start
        self.state.duration = track.duration
        self.state.error = None
        self._changed()
        await self._sync()

    async def resume(self) -> None:
        if self._player_id is None:
            self._player_id = await self._active_player()
        if self._player_id is None:
            return
        await self.backend.call("Player.PlayPause",
                                {"playerid": self._player_id, "play": True})
        await self._sync()

    async def pause(self) -> None:
        if self._player_id is None:
            return
        await self.backend.call("Player.PlayPause",
                                {"playerid": self._player_id, "play": False})
        await self._sync()

    async def stop(self) -> None:
        if self._player_id is None:
            self._player_id = await self._active_player()
        if self._player_id is None:
            self.state.status = "stopped"
            self._changed()
            return
        self._expect_stop = True
        try:
            await self.backend.call("Player.Stop", {"playerid": self._player_id})
        except BackendError:
            self._expect_stop = False
        self.state.status = "stopped"
        self.state.position = 0.0
        self._changed()

    async def seek(self, position: float) -> None:
        if self._player_id is None:
            return
        await self.backend.call("Player.Seek", {
            "playerid": self._player_id, "value": {"time": _hms(position)},
        })
        self.state.position = position
        self._changed()

    async def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        await self.backend.call("Application.SetVolume",
                                {"volume": int(round(volume * 100))})
        self.state.volume = volume
        self._changed()

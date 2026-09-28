"""Lyrion Music Server (formerly Logitech Media Server) over JSON-RPC, as a library
(:class:`LyrionBackend`) and one output per player (:class:`LyrionSink`).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any
from urllib.parse import quote

import requests

from ..models import Album, Artist, Track
from .base import Backend, BackendError, PolledSink, Sink, StreamTarget

log = logging.getLogger(__name__)

#: LMS has no play history or favourites to sort by, and sorts years only upwards.
ALBUM_SORTS = {
    "alphabetical": "album",
    "artist": "artistalbum",
    "newest": "new",
    "random": "random",
    "byYear": "yearalbum",
}

ALBUM_TAGS = "tags:lyjaS"
TRACK_TAGS = "tags:acdegilstyJ"

#: Everything asked of LMS in one request, since it pages rather than streams.
PAGE = 10_000

PLAYER_POLL = 10.0


def _int(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number or None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _id(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


class LyrionBackend(Backend):
    kind = "lyrion"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        host = str(profile.get("host") or "").strip()
        if not host:
            raise BackendError("No Lyrion host configured")
        self.host = host
        self.port = int(profile.get("port") or 9000)
        self.origin = f"http://{host}:{self.port}"
        self.username = str(profile.get("username") or "")
        self.password = str(profile.get("password") or "")

        self._session = requests.Session()
        if self.username:
            self._session.auth = (self.username, self.password)
        self._ids = itertools.count(1)
        self._players: dict[str, LyrionSink] = {}
        self._genre_ids: dict[str, str] = {}
        self._poll_task: asyncio.Task | None = None


    def _call_sync(self, command: list[Any], player: str = "") -> dict[str, Any]:
        payload = {
            "id": next(self._ids),
            "method": "slim.request",
            "params": [player, [str(part) for part in command]],
        }
        try:
            resp = self._session.post(f"{self.origin}/jsonrpc.js", json=payload,
                                      timeout=(5, 20))
            if resp.status_code == 401:
                raise BackendError(f"{self.name}: the server refused the login")
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise BackendError(f"{self.name}: malformed JSON-RPC reply") from exc
        if body.get("error"):
            raise BackendError(f"{self.name}: {body['error']}")
        return body.get("result") or {}

    async def call(self, *command: Any, player: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self._call_sync, list(command), player)


    async def connect(self) -> None:
        await self._refresh_players()
        self._poll_task = asyncio.create_task(
            self._player_loop(), name=f"lyrion-players-{self.source}")

    async def close(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        await asyncio.to_thread(self._session.close)

    def sinks(self) -> list[Sink]:
        return list(self._players.values())

    @property
    def http_session(self) -> requests.Session:
        return self._session

    async def _refresh_players(self) -> bool:
        """Follow the connected players; True if the set of outputs changed."""
        result = await self.call("players", 0, 999)
        seen: dict[str, str] = {}
        for player in result.get("players_loop") or []:
            player_id = _id(player.get("playerid"))
            if player_id and _int(player.get("connected")):
                seen[player_id] = str(player.get("name") or player_id)

        changed = seen.keys() != self._players.keys()
        players: dict[str, LyrionSink] = {}
        for player_id, name in seen.items():
            sink = self._players.get(player_id) or LyrionSink(self, player_id, name)
            changed |= sink.name != name
            sink.name = name
            players[player_id] = sink
        self._players = players
        return changed

    async def _player_loop(self) -> None:
        while True:
            await asyncio.sleep(PLAYER_POLL)
            try:
                if await self._refresh_players():
                    self.sinks_changed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s players: %s", self.name, exc)


    def _track(self, item: dict[str, Any]) -> Track:
        return Track(
            id=str(item.get("id")),
            title=item.get("title") or "Unknown",
            artist=item.get("artist") or "",
            album=item.get("album") or "",
            duration=_float(item.get("duration")),
            backend=self.kind,
            source=self.source,
            artist_id=_id(item.get("artist_id")),
            album_id=_id(item.get("album_id")),
            track_no=_int(item.get("tracknum")),
            disc_no=_int(item.get("disc")),
            year=_int(item.get("year")),
            genre=item.get("genre") or None,
            cover_id=_id(item.get("coverid")) or _id(item.get("artwork_track_id")),
        )

    def _album(self, item: dict[str, Any]) -> Album:
        return Album(
            id=str(item.get("id")),
            name=item.get("album") or "Unknown",
            artist=item.get("artist") or "",
            source=self.source,
            artist_id=_id(item.get("artist_id")),
            year=_int(item.get("year")),
            cover_id=_id(item.get("artwork_track_id")),
        )

    def _artist(self, item: dict[str, Any]) -> Artist:
        return Artist(id=str(item.get("id")), name=item.get("artist") or "Unknown",
                      source=self.source)

    async def _loop(self, key: str, *command: Any) -> list[dict[str, Any]]:
        return (await self.call(*command)).get(key) or []


    async def artists(self) -> list[Artist]:
        items = await self._loop("artists_loop", "artists", 0, PAGE)
        return [self._artist(a) for a in items]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        items = await self._loop("albums_loop", "albums", 0, PAGE,
                                 f"artist_id:{artist_id}", "sort:yearalbum", ALBUM_TAGS)
        return [self._album(a) for a in items]

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        items = await self._loop("albums_loop", "albums", offset, min(limit, 500),
                                 f"sort:{ALBUM_SORTS.get(sort, 'album')}", ALBUM_TAGS)
        return [self._album(a) for a in items]

    async def album_tracks(self, album_id: str) -> list[Track]:
        items = await self._loop("titles_loop", "titles", 0, PAGE,
                                 f"album_id:{album_id}", "sort:tracknum", TRACK_TAGS)
        tracks = [self._track(t) for t in items]
        tracks.sort(key=lambda t: (t.disc_no or 0, t.track_no or 0))
        return tracks

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        term = f"search:{query}"
        artists, albums, tracks = await asyncio.gather(
            self._loop("artists_loop", "artists", 0, limit, term),
            self._loop("albums_loop", "albums", 0, limit, term, ALBUM_TAGS),
            self._loop("titles_loop", "titles", 0, limit, term, TRACK_TAGS),
        )
        return {
            "artists": [self._artist(a) for a in artists],
            "albums": [self._album(a) for a in albums],
            "tracks": [self._track(t) for t in tracks],
        }

    async def genres(self) -> list[str]:
        items = await self._loop("genres_loop", "genres", 0, PAGE)
        self._genre_ids = {g["genre"]: str(g["id"]) for g in items
                           if g.get("genre") and g.get("id") is not None}
        return list(self._genre_ids)

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        if genre not in self._genre_ids:
            await self.genres()
        genre_id = self._genre_ids.get(genre)
        if genre_id is None:
            return []
        items = await self._loop("albums_loop", "albums", offset, min(limit, 500),
                                 f"genre_id:{genre_id}", ALBUM_TAGS)
        return [self._album(a) for a in items]

    async def playlists(self) -> list[dict[str, Any]]:
        items = await self._loop("playlists_loop", "playlists", 0, PAGE)
        return [{"id": str(p.get("id")), "source": self.source,
                 "name": p.get("playlist") or ""} for p in items]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        items = await self._loop("playlisttracks_loop", "playlists", "tracks", 0, PAGE,
                                 f"playlist_id:{playlist_id}", TRACK_TAGS)
        return [self._track(t) for t in items]


    async def stream_target(self, track: Track) -> StreamTarget:
        origin = self.origin
        if self.username:
            credentials = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}"
            origin = f"http://{credentials}@{self.host}:{self.port}"
        return StreamTarget(url=f"{origin}/music/{quote(track.id)}/download",
                            native={"track_id": track.id}, source=self.source)

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        name = f"cover_{size}x{size}_o" if size else "cover"
        return f"{self.origin}/music/{quote(cover_id)}/{name}", {}, {}


class LyrionSink(PolledSink):
    """Plays one track at a time on a Lyrion player, replacing its playlist."""

    IDLE_POLL_INTERVAL = 3.0
    #: A player reports elapsed time only roughly.
    EOF_SLACK = 2.0
    web_streams_only = True

    def __init__(self, backend: LyrionBackend, player_id: str, name: str) -> None:
        super().__init__()
        self.backend = backend
        self.player_id = player_id
        self.source = backend.source
        self.id = f"lyrion:{backend.source}:{player_id}"
        self.name = name
        self._has_mixer = True

    async def call(self, *command: Any) -> dict[str, Any]:
        return await self.backend.call(*command, player=self.player_id)

    async def start(self) -> None:
        await self._sync()
        self._start_polling()

    def _poll_interval(self) -> float:
        return (self.POLL_INTERVAL if self.state.status == "playing"
                else self.IDLE_POLL_INTERVAL)

    def _should_poll(self) -> bool:
        return True

    async def _sync(self) -> None:
        try:
            status = await self.call("status", "-", 1, "tags:")
        except BackendError as exc:
            self.state.error = str(exc)
            self._changed()
            return
        if self._changing:
            return

        was, near_end = self.state.status, self._near_end()
        self.state.buffering = bool(_int(status.get("waitingToPlay")))
        mode = "stop" if str(status.get("power", 1)) == "0" else status.get("mode")
        self.state.status = {"play": "playing", "pause": "paused"}.get(mode, "stopped")
        if self.state.buffering and was == "playing":
            self.state.status = "playing"
        self._note_position(_float(status.get("time")))
        self.state.duration = _float(status.get("duration")) or self.state.duration
        self.state.error = None

        volume = status.get("mixer volume")
        self._has_mixer = volume is not None
        if self._has_mixer:
            # A negative volume is a muted player.
            self.state.volume = max(0.0, min(1.0, _float(volume) / 100.0))

        if self.state.status == "stopped" and was == "playing":
            self._note_position(0.0)
            if near_end:
                await self._ended("eof")
                return
        self._changed()


    def _command_for(self, target: StreamTarget, track: Track) -> list[Any]:
        if target.native and target.source == self.backend.source:
            return ["playlistcontrol", "cmd:load", f"track_id:{target.native['track_id']}"]
        if target.url and target.url.startswith(("http://", "https://")):
            return ["playlist", "play", target.url, track.title]
        raise BackendError(f"{self.name} can only play its own library and web streams")

    async def play(self, target: StreamTarget, track: Track, start: float = 0.0) -> None:
        command = self._command_for(target, track)
        with self._transition():
            await self.call("power", 1)
            await self.call("playlist", "repeat", 0)
            await self.call("playlist", "shuffle", 0)
            await self.call(*command)
            await self.call("play")
        await self._started(track, start)

    async def resume(self) -> None:
        await self.call("pause", 0)
        await self._sync()

    async def pause(self) -> None:
        if self.state.status == "stopped":
            return
        await self.call("pause", 1)
        await self._sync()

    async def stop(self) -> None:
        await self._stop_with(self.call("stop"))

    async def seek(self, position: float) -> None:
        position = max(0.0, position)
        try:
            await self.call("time", f"{position:.1f}")
        except BackendError as exc:
            log.debug("%s seek: %s", self.name, exc)
            return
        self._note_position(position)
        self._changed()

    async def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        if self._has_mixer:
            try:
                await self.call("mixer", "volume", int(round(volume * 100)))
            except BackendError as exc:
                log.debug("%s volume: %s", self.name, exc)
                return
        self.state.volume = volume
        self._changed()

    def capabilities(self) -> dict[str, bool]:
        return {"seek": True, "volume": self._has_mixer}

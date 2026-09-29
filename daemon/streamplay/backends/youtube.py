"""YouTube Music: searched with ytmusicapi, streamed through the URLs yt-dlp resolves.

There is no collection to browse without an account, so only search, artists and albums reach it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..models import Album, Artist, Track
from .base import Backend, BackendError, StreamTarget

try:
    from ytmusicapi import YTMusic
except ImportError:
    YTMusic = None
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

log = logging.getLogger(__name__)

WATCH_URL = "https://music.youtube.com/watch?v="
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
RESIZABLE_HOST = "googleusercontent.com"
COVER_HOSTS = (RESIZABLE_HOST, "ytimg.com")
#: Resolve again this long before YouTube's own expiry, so a queued track does not start on a dead URL.
EXPIRY_MARGIN = 1800
DEFAULT_COVER_SIZE = 544


def _year(value: Any) -> int | None:
    return int(value) if str(value or "").isdigit() else None


def _cover(thumbnails: list[dict] | None) -> str | None:
    return thumbnails[-1].get("url") if thumbnails else None


def _names(artists: list[dict] | None) -> str:
    return ", ".join(a.get("name") or "" for a in artists or [] if a.get("name"))


def _first_id(artists: list[dict] | None) -> str | None:
    return next((a["id"] for a in artists or [] if a.get("id")), None)


class YouTubeBackend(Backend):
    kind = "youtube"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        self._music = None
        self._ydl = None
        #: A YoutubeDL is not thread-safe, and building one per track loses its player cache.
        self._ydl_lock = threading.Lock()
        #: videoId -> (url, time after which it is resolved again).
        self._streams: dict[str, tuple[str, float]] = {}
        #: Resolves under way, so a preload and the play that follows share one.
        self._resolving: dict[str, asyncio.Future] = {}

    async def connect(self) -> None:
        missing = [name for name, module in (("ytmusicapi", YTMusic), ("yt-dlp", yt_dlp))
                   if module is None]
        if missing:
            raise BackendError("Install " + " and ".join(missing) + " to use YouTube")
        self._music = await asyncio.to_thread(YTMusic)
        self._ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                      "noplaylist": True, "format": AUDIO_FORMAT})
        await self._call("get_search_suggestions", "music")

    async def close(self) -> None:
        if self._ydl is not None:
            await asyncio.to_thread(self._ydl.close)
            self._ydl = None

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._music is None:
            raise BackendError(f"{self.name} is not connected")
        try:
            return await asyncio.to_thread(getattr(self._music, method), *args, **kwargs)
        except Exception as exc:
            raise BackendError(f"{self.name}: {exc}") from exc


    def _track(self, song: dict[str, Any], album: dict[str, Any] | None = None,
               album_id: str | None = None) -> Track:
        album = album or {}
        song_album = song.get("album")
        if isinstance(song_album, dict):
            album_name, album_id = song_album.get("name") or "", song_album.get("id")
        else:
            album_name = song_album or album.get("title") or ""
        artists = song.get("artists") or album.get("artists")
        return Track(
            id=str(song.get("videoId")),
            title=song.get("title") or "Unknown",
            artist=_names(artists),
            album=album_name,
            duration=float(song.get("duration_seconds") or 0),
            backend=self.kind,
            source=self.source,
            artist_id=_first_id(artists),
            album_id=album_id,
            track_no=song.get("trackNumber"),
            year=_year(song.get("year") or album.get("year")),
            cover_id=_cover(song.get("thumbnails")) or _cover(album.get("thumbnails")),
        )

    def _album(self, album: dict[str, Any], artist: str = "",
               artist_id: str | None = None) -> Album:
        artists = album.get("artists")
        return Album(
            id=str(album.get("browseId")),
            name=album.get("title") or "Unknown",
            artist=_names(artists) or artist,
            source=self.source,
            artist_id=_first_id(artists) or artist_id,
            year=_year(album.get("year")),
            cover_id=_cover(album.get("thumbnails")),
        )

    def _artist(self, artist: dict[str, Any]) -> Artist:
        return Artist(
            id=str(artist.get("browseId")),
            name=artist.get("artist") or "Unknown",
            source=self.source,
            cover_id=_cover(artist.get("thumbnails")),
        )


    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        songs, albums, artists = await asyncio.gather(
            self._call("search", query, filter="songs", limit=limit),
            self._call("search", query, filter="albums", limit=limit),
            self._call("search", query, filter="artists", limit=limit),
        )
        return {
            "artists": [self._artist(a) for a in artists[:limit] if a.get("browseId")],
            "albums": [self._album(a) for a in albums[:limit] if a.get("browseId")],
            "tracks": [self._track(s) for s in songs[:limit] if s.get("videoId")],
        }

    async def artist_albums(self, artist_id: str) -> list[Album]:
        info = await self._call("get_artist", artist_id)
        shelves = await asyncio.gather(*(self._shelf(info.get(kind) or {})
                                         for kind in ("albums", "singles")))
        name = info.get("name") or ""
        return [self._album(a, name, artist_id) for shelf in shelves for a in shelf
                if a.get("browseId")]

    async def _shelf(self, shelf: dict[str, Any]) -> list[dict[str, Any]]:
        """All of an artist's albums or singles; the artist page shows only the first few."""
        if shelf.get("params") and shelf.get("browseId"):
            return await self._call("get_artist_albums", shelf["browseId"],
                                    shelf["params"], limit=None)
        return shelf.get("results") or []

    async def album_tracks(self, album_id: str) -> list[Track]:
        album = await self._call("get_album", album_id)
        return [self._track(t, album, album_id) for t in album.get("tracks") or []
                if t.get("videoId") and t.get("isAvailable", True)]


    async def stream_target(self, track: Track) -> StreamTarget:
        cached = self._streams.get(track.id)
        if cached and cached[1] > time.time():
            return StreamTarget(url=cached[0], source=self.source)
        pending = self._resolving.get(track.id)
        if pending is None:
            pending = asyncio.ensure_future(self._fetch(track.id))
            self._resolving[track.id] = pending
            pending.add_done_callback(lambda _: self._resolving.pop(track.id, None))
        url = await asyncio.shield(pending)
        return StreamTarget(url=url, source=self.source)

    async def _fetch(self, video_id: str) -> str:
        url = await asyncio.to_thread(self._resolve, video_id)
        expire = parse_qs(urlsplit(url).query).get("expire", [""])[0]
        if expire.isdigit():
            now = time.time()
            self._streams = {k: v for k, v in self._streams.items() if v[1] > now}
            self._streams[video_id] = (url, int(expire) - EXPIRY_MARGIN)
        return url

    def _resolve(self, video_id: str) -> str:
        if self._ydl is None:
            raise BackendError(f"{self.name} is not connected")
        try:
            with self._ydl_lock:
                info = self._ydl.extract_info(WATCH_URL + video_id, download=False)
        except Exception as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        url = (info or {}).get("url")
        if not url:
            raise BackendError(f"{self.name}: no audio stream for {video_id}")
        return url

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        host = urlsplit(cover_id).hostname or ""
        known = next((h for h in COVER_HOSTS if host == h or host.endswith("." + h)), None)
        if known is None:
            return None
        if known == RESIZABLE_HOST and "=" in cover_id:
            side = size or DEFAULT_COVER_SIZE
            cover_id = cover_id.rsplit("=", 1)[0] + f"=w{side}-h{side}-l90-rj"
        return cover_id, {}, {}

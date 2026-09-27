"""Subsonic-API client, for Subsonic itself and every server speaking its API.

Only the library half lives here; mpv streams the URLs :meth:`SubsonicBackend.stream_url` builds.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlencode, urljoin

import requests

from ..models import Album, Artist, Track
from .base import Backend, BackendError, StreamTarget

log = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "streamplay"

#: Maps our sort keys onto getAlbumList2 ``type`` values.
ALBUM_SORTS = {
    "alphabetical": "alphabeticalByName",
    "artist": "alphabeticalByArtist",
    "newest": "newest",
    "recent": "recent",
    "frequent": "frequent",
    "random": "random",
    "starred": "starred",
    "byYear": "byYear",
    "byYearDesc": "byYear",
}

#: getAlbumList2's byYear needs a range, reversed to ask for the newest first.
#: It starts at 1 so undated albums do not fill the whole first page.
YEAR_RANGE = {
    "byYear": {"fromYear": 1, "toYear": 3000},
    "byYearDesc": {"fromYear": 3000, "toYear": 1},
}


class SubsonicBackend(Backend):
    kind = "subsonic"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        url = str(profile.get("url") or "").strip()
        if not url:
            raise BackendError("No server URL configured")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.base = url.rstrip("/") + "/rest/"
        self.username = str(profile.get("username") or "")
        self.password = str(profile.get("password") or "")
        self.legacy_auth = bool(profile.get("legacyAuth"))
        self.verify_tls = profile.get("verifyTls", True)
        self.max_bitrate = int(profile.get("maxBitrate") or 0)
        self.stream_format = str(profile.get("streamFormat") or "raw")

        self._session = requests.Session()
        self._session.headers["User-Agent"] = f"{CLIENT_NAME}/1.0"


    def auth_params(self) -> dict[str, str]:
        params = {
            "u": self.username,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        if self.legacy_auth:
            # Some very old servers only understand the hex-encoded password.
            params["p"] = "enc:" + self.password.encode("utf-8").hex()
        else:
            salt = secrets.token_hex(8)
            token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
            params["t"] = token
            params["s"] = salt
        return params

    def _url(self, endpoint: str) -> str:
        return urljoin(self.base, endpoint + ".view")

    def _get_sync(self, endpoint: str, **params: Any) -> dict[str, Any]:
        query = self.auth_params()
        query.update({k: v for k, v in params.items() if v is not None})
        try:
            resp = self._session.get(
                self._url(endpoint), params=query,
                timeout=(5, 20), verify=self.verify_tls,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise BackendError(f"{self.name}: server did not return JSON") from exc

        body = payload.get("subsonic-response") or {}
        if body.get("status") != "ok":
            err = body.get("error") or {}
            raise BackendError(
                err.get("message") or f"Subsonic error {err.get('code', '?')}"
            )
        return body

    async def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_sync, endpoint, **params)


    async def connect(self) -> None:
        if not self.username or not self.password:
            raise BackendError("Username and password are required")
        await self._get("ping")

    async def close(self) -> None:
        await asyncio.to_thread(self._session.close)


    def _track(self, song: dict[str, Any]) -> Track:
        return Track(
            id=str(song.get("id")),
            title=song.get("title") or "Unknown",
            artist=song.get("artist") or "",
            album=song.get("album") or "",
            duration=float(song.get("duration") or 0),
            backend=self.kind,
            source=self.source,
            artist_id=song.get("artistId"),
            album_id=song.get("albumId"),
            track_no=song.get("track"),
            disc_no=song.get("discNumber"),
            year=song.get("year"),
            genre=song.get("genre"),
            cover_id=song.get("coverArt") or song.get("id"),
        )

    def _album(self, album: dict[str, Any]) -> Album:
        return Album(
            id=str(album.get("id")),
            name=album.get("name") or album.get("album") or "Unknown",
            artist=album.get("artist") or "",
            source=self.source,
            artist_id=album.get("artistId"),
            year=album.get("year"),
            track_count=int(album.get("songCount") or 0),
            duration=float(album.get("duration") or 0),
            genre=album.get("genre"),
            cover_id=album.get("coverArt") or album.get("id"),
        )

    def _artist(self, artist: dict[str, Any]) -> Artist:
        return Artist(
            id=str(artist.get("id")),
            name=artist.get("name") or "Unknown",
            source=self.source,
            album_count=int(artist.get("albumCount") or 0),
            cover_id=artist.get("coverArt"),
        )


    async def artists(self) -> list[Artist]:
        body = await self._get("getArtists")
        out: list[Artist] = []
        for index in (body.get("artists") or {}).get("index") or []:
            for artist in index.get("artist") or []:
                out.append(self._artist(artist))
        return out

    async def artist_albums(self, artist_id: str) -> list[Album]:
        body = await self._get("getArtist", id=artist_id)
        albums = (body.get("artist") or {}).get("album") or []
        return [self._album(a) for a in albums]

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        body = await self._get(
            "getAlbumList2",
            type=ALBUM_SORTS.get(sort, "alphabeticalByName"),
            size=min(limit, 500), offset=offset,
            **YEAR_RANGE.get(sort, {}),
        )
        albums = (body.get("albumList2") or {}).get("album") or []
        return [self._album(a) for a in albums]

    async def album_tracks(self, album_id: str) -> list[Track]:
        body = await self._get("getAlbum", id=album_id)
        songs = (body.get("album") or {}).get("song") or []
        return [self._track(s) for s in songs]

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        body = await self._get(
            "search3", query=query,
            artistCount=limit, albumCount=limit, songCount=limit,
        )
        result = body.get("searchResult3") or {}
        return {
            "artists": [self._artist(a) for a in result.get("artist") or []],
            "albums": [self._album(a) for a in result.get("album") or []],
            "tracks": [self._track(s) for s in result.get("song") or []],
        }

    async def genres(self) -> list[str]:
        body = await self._get("getGenres")
        entries = (body.get("genres") or {}).get("genre") or []
        return [g.get("value") for g in entries if g.get("value")]

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        body = await self._get(
            "getAlbumList2", type="byGenre", genre=genre,
            size=min(limit, 500), offset=offset,
        )
        albums = (body.get("albumList2") or {}).get("album") or []
        return [self._album(a) for a in albums]

    async def playlists(self) -> list[dict[str, Any]]:
        body = await self._get("getPlaylists")
        entries = (body.get("playlists") or {}).get("playlist") or []
        return [
            {
                "id": str(p.get("id")),
                "source": self.source,
                "name": p.get("name") or "",
                "trackCount": int(p.get("songCount") or 0),
                "duration": float(p.get("duration") or 0),
                "coverId": p.get("coverArt"),
            }
            for p in entries
        ]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        body = await self._get("getPlaylist", id=playlist_id)
        songs = (body.get("playlist") or {}).get("entry") or []
        return [self._track(s) for s in songs]


    async def stream_target(self, track: Track) -> StreamTarget:
        return StreamTarget(url=self.stream_url(track), source=self.source)

    def stream_url(self, track: Track) -> str:
        params = self.auth_params()
        params["id"] = track.id
        if self.max_bitrate:
            params["maxBitRate"] = str(self.max_bitrate)
        if self.stream_format and self.stream_format != "raw":
            params["format"] = self.stream_format
        return self._url("stream") + "?" + urlencode(params)

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        params = self.auth_params()
        params["id"] = cover_id
        if size:
            params["size"] = str(size)
        return self._url("getCoverArt"), params, {}

    async def scrobble(self, track: Track, submission: bool) -> None:
        try:
            await self._get("scrobble", id=track.id,
                            submission="true" if submission else "false")
        except BackendError as exc:
            log.debug("scrobble failed: %s", exc)

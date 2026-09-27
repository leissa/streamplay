"""Jellyfin client, the music half only; mpv streams the URLs :meth:`JellyfinBackend.stream_url` builds.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
from typing import Any
from urllib.parse import quote, urlencode

import requests

from .. import __version__
from ..models import Album, Artist, Track
from .base import Backend, BackendError, StreamTarget

log = logging.getLogger(__name__)

CLIENT_NAME = "streamplay"

#: Jellyfin durations are in ticks of 100 ns.
TICKS = 10_000_000

#: Maps our sort keys onto ``(SortBy, SortOrder, Filters)``.
ALBUM_SORTS = {
    "alphabetical": ("SortName", "Ascending", None),
    "artist": ("AlbumArtist,SortName", "Ascending", None),
    "newest": ("DateCreated,SortName", "Descending", None),
    "recent": ("DatePlayed,SortName", "Descending", "IsPlayed"),
    "frequent": ("PlayCount,SortName", "Descending", "IsPlayed"),
    "random": ("Random", "Ascending", None),
    "starred": ("SortName", "Ascending", "IsFavorite"),
    "byYear": ("ProductionYear,SortName", "Ascending", None),
    "byYearDesc": ("ProductionYear,SortName", "Descending", None),
}

#: Transcoding containers, where the codec name is not also a container.
CONTAINERS = {"opus": "ogg", "vorbis": "ogg"}

ALBUM_FIELDS = "Genres,ChildCount"
TRACK_FIELDS = "Genres"


def _seconds(ticks: Any) -> float:
    return float(ticks or 0) / TICKS


def _first_id(items: Any) -> str | None:
    for item in items or []:
        if item.get("Id"):
            return str(item["Id"])
    return None


def _primary(item: dict[str, Any]) -> str | None:
    return str(item["Id"]) if (item.get("ImageTags") or {}).get("Primary") else None


class JellyfinBackend(Backend):
    kind = "jellyfin"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        url = str(profile.get("url") or "").strip()
        if not url:
            raise BackendError("No server URL configured")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.base = url.rstrip("/")
        self.username = str(profile.get("username") or "")
        self.password = str(profile.get("password") or "")
        self.verify_tls = profile.get("verifyTls", True)
        self.max_bitrate = int(profile.get("maxBitrate") or 0)
        self.stream_format = str(profile.get("streamFormat") or "raw")

        # Jellyfin revokes the token of any earlier login with the same device id.
        self.device_id = f"{CLIENT_NAME}-{self.source}-{secrets.token_hex(4)}"
        self.token = ""
        self.user_id = ""

        self._session = requests.Session()
        self._session.headers["User-Agent"] = f"{CLIENT_NAME}/{__version__}"


    def _authorization(self) -> str:
        fields = {
            "Client": CLIENT_NAME,
            "Device": socket.gethostname() or CLIENT_NAME,
            "DeviceId": self.device_id,
            "Version": __version__,
        }
        if self.token:
            fields["Token"] = self.token
        return "MediaBrowser " + ", ".join(
            f'{k}="{quote(v, safe=" ")}"' for k, v in fields.items())

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization()}

    def _request_sync(self, method: str, path: str, params: dict | None = None,
                      body: dict | None = None) -> Any:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._session.request(
                method, self.base + path, params=query, json=body,
                headers=self._auth_headers(),
                timeout=(5, 20), verify=self.verify_tls,
            )
            if resp.status_code == 401:
                raise BackendError(f"{self.name}: the server refused the login")
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.RequestException as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise BackendError(f"{self.name}: server did not return JSON") from exc

    async def _request(self, method: str, path: str, params: dict | None = None,
                       body: dict | None = None) -> Any:
        return await asyncio.to_thread(self._request_sync, method, path, params, body)

    async def _items(self, path: str = "/Items", **params: Any) -> list[dict[str, Any]]:
        params.setdefault("userId", self.user_id)
        body = await self._request("GET", path, params)
        return body.get("Items") or []


    async def connect(self) -> None:
        if not self.username:
            raise BackendError("A username is required")
        body = await self._request("POST", "/Users/AuthenticateByName",
                                   body={"Username": self.username,
                                         "Pw": self.password})
        self.token = str(body.get("AccessToken") or "")
        self.user_id = str((body.get("User") or {}).get("Id") or "")
        if not self.token or not self.user_id:
            raise BackendError(f"{self.name}: the server sent no access token")

    async def close(self) -> None:
        if self.token:
            try:
                await self._request("POST", "/Sessions/Logout")
            except BackendError as exc:
                log.debug("logout failed: %s", exc)
            self.token = ""
        await asyncio.to_thread(self._session.close)


    def _track(self, item: dict[str, Any]) -> Track:
        album_artists = item.get("AlbumArtists") or []
        cover = (str(item["AlbumId"]) if item.get("AlbumId")
                 and item.get("AlbumPrimaryImageTag") else _primary(item))
        return Track(
            id=str(item.get("Id")),
            title=item.get("Name") or "Unknown",
            artist=", ".join(item.get("Artists") or []) or item.get("AlbumArtist") or "",
            album=item.get("Album") or "",
            duration=_seconds(item.get("RunTimeTicks")),
            backend=self.kind,
            source=self.source,
            artist_id=_first_id(item.get("ArtistItems")) or _first_id(album_artists),
            album_id=item.get("AlbumId"),
            track_no=item.get("IndexNumber"),
            disc_no=item.get("ParentIndexNumber"),
            year=item.get("ProductionYear"),
            genre=(item.get("Genres") or [None])[0],
            cover_id=cover,
        )

    def _album(self, item: dict[str, Any]) -> Album:
        return Album(
            id=str(item.get("Id")),
            name=item.get("Name") or "Unknown",
            artist=item.get("AlbumArtist") or ", ".join(item.get("Artists") or []),
            source=self.source,
            artist_id=_first_id(item.get("AlbumArtists")),
            year=item.get("ProductionYear"),
            track_count=int(item.get("ChildCount") or 0),
            duration=_seconds(item.get("RunTimeTicks")),
            genre=(item.get("Genres") or [None])[0],
            cover_id=_primary(item),
        )

    def _artist(self, item: dict[str, Any]) -> Artist:
        return Artist(
            id=str(item.get("Id")),
            name=item.get("Name") or "Unknown",
            source=self.source,
            album_count=int(item.get("AlbumCount") or 0),
            cover_id=_primary(item),
        )


    async def artists(self) -> list[Artist]:
        items = await self._items("/Artists/AlbumArtists", SortBy="SortName")
        return [self._artist(a) for a in items]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        items = await self._items(
            IncludeItemTypes="MusicAlbum", Recursive="true",
            ArtistIds=artist_id, SortBy="ProductionYear,SortName",
            Fields=ALBUM_FIELDS)
        return [self._album(a) for a in items]

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        sort_by, order, filters = ALBUM_SORTS.get(sort, ALBUM_SORTS["alphabetical"])
        items = await self._items(
            IncludeItemTypes="MusicAlbum", Recursive="true",
            SortBy=sort_by, SortOrder=order, Filters=filters,
            StartIndex=offset, Limit=min(limit, 500), Fields=ALBUM_FIELDS)
        return [self._album(a) for a in items]

    async def album_tracks(self, album_id: str) -> list[Track]:
        items = await self._items(
            ParentId=album_id, IncludeItemTypes="Audio", Recursive="true",
            SortBy="ParentIndexNumber,IndexNumber,SortName", Fields=TRACK_FIELDS)
        return [self._track(t) for t in items]

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        artists, albums, tracks = await asyncio.gather(
            self._items("/Artists", searchTerm=query, Limit=limit),
            self._items(searchTerm=query, IncludeItemTypes="MusicAlbum",
                        Recursive="true", Limit=limit, Fields=ALBUM_FIELDS),
            self._items(searchTerm=query, IncludeItemTypes="Audio",
                        Recursive="true", Limit=limit, Fields=TRACK_FIELDS),
        )
        return {
            "artists": [self._artist(a) for a in artists],
            "albums": [self._album(a) for a in albums],
            "tracks": [self._track(t) for t in tracks],
        }

    async def genres(self) -> list[str]:
        items = await self._items("/Genres", IncludeItemTypes="MusicAlbum",
                                  SortBy="SortName")
        return [g["Name"] for g in items if g.get("Name")]

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        items = await self._items(
            IncludeItemTypes="MusicAlbum", Recursive="true", Genres=genre,
            SortBy="SortName", StartIndex=offset, Limit=min(limit, 500),
            Fields=ALBUM_FIELDS)
        return [self._album(a) for a in items]

    async def playlists(self) -> list[dict[str, Any]]:
        items = await self._items(
            IncludeItemTypes="Playlist", MediaTypes="Audio", Recursive="true",
            SortBy="SortName", Fields="ChildCount")
        return [
            {
                "id": str(p.get("Id")),
                "source": self.source,
                "name": p.get("Name") or "",
                "trackCount": int(p.get("ChildCount") or 0),
                "duration": _seconds(p.get("RunTimeTicks")),
                "coverId": _primary(p),
            }
            for p in items
        ]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        items = await self._items(f"/Playlists/{quote(playlist_id)}/Items",
                                  Fields=TRACK_FIELDS)
        return [self._track(t) for t in items]


    async def stream_target(self, track: Track) -> StreamTarget:
        return StreamTarget(url=self.stream_url(track), source=self.source)

    def stream_url(self, track: Track) -> str:
        params = {"api_key": self.token, "UserId": self.user_id,
                  "DeviceId": self.device_id}
        fmt = "" if self.stream_format == "raw" else self.stream_format
        if not fmt and not self.max_bitrate:
            params["static"] = "true"
            return f"{self.base}/Audio/{quote(track.id)}/stream?{urlencode(params)}"
        codec = fmt or "mp3"
        params.update({
            "Container": fmt or "flac,mp3,opus,ogg,m4a,aac,wav",
            "AudioCodec": codec,
            "TranscodingContainer": CONTAINERS.get(codec, codec),
            "TranscodingProtocol": "http",
        })
        if self.max_bitrate:
            params["MaxStreamingBitrate"] = str(self.max_bitrate * 1000)
        return f"{self.base}/Audio/{quote(track.id)}/universal?{urlencode(params)}"

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        params = {"quality": "90"}
        if size:
            params["maxWidth"] = params["maxHeight"] = str(size)
        return (f"{self.base}/Items/{quote(cover_id)}/Images/Primary", params,
                self._auth_headers())

    async def scrobble(self, track: Track, submission: bool) -> None:
        # Jellyfin counts a play, and scrobbler plugins submit it, when playback stops near the end.
        if submission:
            path, ticks = "/Sessions/Playing/Stopped", int(track.duration * TICKS)
        else:
            path, ticks = "/Sessions/Playing", 0
        try:
            await self._request("POST", path, body={
                "ItemId": track.id, "PositionTicks": ticks,
                "CanSeek": True, "PlayMethod": "DirectStream"})
        except BackendError as exc:
            log.debug("scrobble failed: %s", exc)

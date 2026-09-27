"""Plex Media Server, the music half only; mpv streams the part URLs :meth:`PlexBackend.stream_target` builds.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests

from .. import __version__
from ..models import Album, Artist, Track
from .base import Backend, BackendError, StreamTarget

log = logging.getLogger(__name__)

CLIENT_NAME = "streamplay"
DEFAULT_PORT = 32400
LIBRARY_ID = "com.plexapp.plugins.library"

#: Plex's metadata ``type`` numbers.
ARTIST, ALBUM, TRACK = 8, 9, 10

#: Maps our sort keys onto ``(sort, extra filter)``; the filter is Plex's ``field>>=value`` syntax.
ALBUM_SORTS = {
    "alphabetical": ("album.titleSort", {}),
    "artist": ("artist.titleSort,album.titleSort", {}),
    "newest": ("album.addedAt:desc", {}),
    "recent": ("album.lastViewedAt:desc", {"album.viewCount>>": 0}),
    "frequent": ("album.viewCount:desc", {"album.viewCount>>": 0}),
    "random": ("random", {}),
    "starred": ("album.titleSort", {"album.userRating>>": 0}),
    "byYear": ("album.year,album.titleSort", {"album.year>>": 0}),
    "byYearDesc": ("album.year:desc,album.titleSort", {}),
}


def _seconds(ms: Any) -> float:
    return float(ms or 0) / 1000


def _key(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _genre(item: dict[str, Any]) -> str | None:
    for tag in item.get("Genre") or []:
        if tag.get("tag"):
            return tag["tag"]
    return None


def _genre_id(directory: dict[str, Any]) -> str:
    """A genre's filter id, which some servers send only inside a link."""
    for link in (directory.get("fastKey"), directory.get("key")):
        genre = parse_qs(urlsplit(str(link or "")).query).get("genre")
        if genre:
            return genre[0]
    return str(directory.get("key") or "")


class PlexBackend(Backend):
    kind = "plex"

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        url = str(profile.get("url") or "").strip()
        if not url:
            raise BackendError("No server URL configured")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
            if urlsplit(url).port is None:
                url = url.rstrip("/") + f":{DEFAULT_PORT}"
        self.base = url.rstrip("/")
        self.token = str(profile.get("password") or "")
        self.verify_tls = profile.get("verifyTls", True)

        self.sections: list[str] = []
        #: ``section -> genre name -> id``, since Plex filters by a per-section tag id.
        self._genre_ids: dict[str, dict[str, str]] = {}
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": f"{CLIENT_NAME}/{__version__}",
            "X-Plex-Client-Identifier": f"{CLIENT_NAME}-{self.source}",
            "X-Plex-Product": CLIENT_NAME,
            "X-Plex-Version": __version__,
        })
        if self.token:
            self._session.headers["X-Plex-Token"] = self.token


    def _get_sync(self, path: str, params: dict | None = None) -> dict[str, Any]:
        try:
            resp = self._session.get(
                self.base + path, params=params,
                timeout=(5, 20), verify=self.verify_tls,
            )
            if resp.status_code == 401:
                raise BackendError(f"{self.name}: the server refused the token")
            resp.raise_for_status()
            body = resp.json() if resp.content else {}
        except requests.RequestException as exc:
            raise BackendError(f"{self.name}: {exc}") from exc
        except ValueError as exc:
            raise BackendError(f"{self.name}: server did not return JSON") from exc
        return body.get("MediaContainer") or {}

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_sync, path, params)

    async def _metadata(self, path: str, **params: Any) -> list[dict[str, Any]]:
        return (await self._get(path, **params)).get("Metadata") or []

    async def _paged(self, sections: dict[str, dict], offset: int,
                     limit: int) -> list[dict[str, Any]]:
        """Page through the sections in ``sections`` as if they were one list, in order."""
        out: list[dict[str, Any]] = []
        for section, params in sections.items():
            if len(out) >= limit:
                break
            path = f"/library/sections/{section}/all"
            if offset and len(self.sections) > 1:
                total = int((await self._get(path, **params, **{
                    "X-Plex-Container-Start": 0,
                    "X-Plex-Container-Size": 0})).get("totalSize") or 0)
                if offset >= total:
                    offset -= total
                    continue
            out.extend(await self._metadata(path, **params, **{
                "X-Plex-Container-Start": offset,
                "X-Plex-Container-Size": limit - len(out)}))
            offset = 0
        return out


    async def connect(self) -> None:
        body = await self._get("/library/sections")
        self.sections = [str(d["key"]) for d in body.get("Directory") or []
                         if d.get("type") == "artist" and d.get("key")]
        if not self.sections:
            raise BackendError(f"{self.name}: the server has no music library")

    async def close(self) -> None:
        await asyncio.to_thread(self._session.close)


    def _track(self, item: dict[str, Any]) -> Track:
        media = (item.get("Media") or [{}])[0]
        part = (media.get("Part") or [{}])[0]
        return Track(
            id=str(item.get("ratingKey")),
            title=item.get("title") or "Unknown",
            artist=item.get("originalTitle") or item.get("grandparentTitle") or "",
            album=item.get("parentTitle") or "",
            duration=_seconds(item.get("duration")),
            backend=self.kind,
            source=self.source,
            artist_id=_key(item.get("grandparentRatingKey")),
            album_id=_key(item.get("parentRatingKey")),
            track_no=item.get("index"),
            disc_no=item.get("parentIndex"),
            year=item.get("parentYear") or item.get("year"),
            genre=_genre(item),
            cover_id=item.get("parentThumb") or item.get("thumb"),
            extra={"part": part["key"]} if part.get("key") else {},
        )

    def _album(self, item: dict[str, Any]) -> Album:
        return Album(
            id=str(item.get("ratingKey")),
            name=item.get("title") or "Unknown",
            artist=item.get("parentTitle") or "",
            source=self.source,
            artist_id=_key(item.get("parentRatingKey")),
            year=item.get("year"),
            track_count=int(item.get("leafCount") or 0),
            duration=_seconds(item.get("duration")),
            genre=_genre(item),
            cover_id=item.get("thumb"),
        )

    def _artist(self, item: dict[str, Any]) -> Artist:
        return Artist(
            id=str(item.get("ratingKey")),
            name=item.get("title") or "Unknown",
            source=self.source,
            album_count=int(item.get("childCount") or 0),
            cover_id=item.get("thumb"),
        )


    async def artists(self) -> list[Artist]:
        sections = await asyncio.gather(*(
            self._metadata(f"/library/sections/{section}/all", type=ARTIST, sort="titleSort")
            for section in self.sections))
        return [self._artist(a) for items in sections for a in items]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        items = await self._metadata(f"/library/metadata/{quote(artist_id)}/children")
        return [self._album(a) for a in items if a.get("type") == "album"]

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        order, filters = ALBUM_SORTS.get(sort, ALBUM_SORTS["alphabetical"])
        items = await self._paged(
            {section: {"type": ALBUM, "sort": order, **filters} for section in self.sections},
            offset, min(limit, 500))
        return [self._album(a) for a in items]

    async def album_tracks(self, album_id: str) -> list[Track]:
        items = await self._metadata(f"/library/metadata/{quote(album_id)}/children")
        return [self._track(t) for t in items]

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        body = await self._get("/hubs/search", query=query, limit=limit)
        found: dict[str, list] = {"artists": [], "albums": [], "tracks": []}
        for hub in body.get("Hub") or []:
            for item in hub.get("Metadata") or []:
                if str(item.get("librarySectionID")) not in self.sections:
                    continue
                kind = item.get("type")
                if kind == "artist":
                    found["artists"].append(self._artist(item))
                elif kind == "album":
                    found["albums"].append(self._album(item))
                elif kind == "track":
                    found["tracks"].append(self._track(item))
        return found

    async def genres(self) -> list[str]:
        bodies = await asyncio.gather(*(
            self._get(f"/library/sections/{section}/genre", type=ALBUM)
            for section in self.sections))
        self._genre_ids = {
            section: {d["title"]: _genre_id(d) for d in body.get("Directory") or []
                      if d.get("title")}
            for section, body in zip(self.sections, bodies)}
        names = {name for ids in self._genre_ids.values() for name in ids}
        return sorted(names, key=str.casefold)

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        if not any(genre in ids for ids in self._genre_ids.values()):
            await self.genres()
        params = {section: {"type": ALBUM, "sort": "album.titleSort", "genre": ids[genre]}
                  for section, ids in self._genre_ids.items() if genre in ids}
        items = await self._paged(params, offset, min(limit, 500))
        return [self._album(a) for a in items]

    async def playlists(self) -> list[dict[str, Any]]:
        items = await self._metadata("/playlists", playlistType="audio")
        return [
            {
                "id": str(p.get("ratingKey")),
                "source": self.source,
                "name": p.get("title") or "",
                "trackCount": int(p.get("leafCount") or 0),
                "duration": _seconds(p.get("duration")),
                "coverId": p.get("composite") or p.get("thumb"),
            }
            for p in items
        ]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        items = await self._metadata(f"/playlists/{quote(playlist_id)}/items")
        return [self._track(t) for t in items]


    async def stream_target(self, track: Track) -> StreamTarget:
        part = track.extra.get("part")
        if not part:
            items = await self._metadata(f"/library/metadata/{quote(track.id)}")
            part = self._track(items[0]).extra.get("part") if items else None
        if not part:
            raise BackendError(f"{self.name}: {track.title} has no playable file")
        url = self.base + part
        if self.token:
            url += "?" + urlencode({"X-Plex-Token": self.token})
        return StreamTarget(url=url, source=self.source)

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        params = {"X-Plex-Token": self.token} if self.token else {}
        if not size:
            return self.base + cover_id, params, {}
        params.update({"url": cover_id, "width": str(size), "height": str(size),
                       "minSize": "1", "upscale": "1"})
        return self.base + "/photo/:/transcode", params, {}

    async def scrobble(self, track: Track, submission: bool) -> None:
        try:
            if submission:
                await self._get("/:/scrobble", key=track.id, identifier=LIBRARY_ID)
            else:
                await self._get("/:/timeline", ratingKey=track.id,
                                key=f"/library/metadata/{track.id}",
                                identifier=LIBRARY_ID, state="playing", time=0,
                                duration=int(track.duration * 1000))
        except BackendError as exc:
            log.debug("scrobble failed: %s", exc)

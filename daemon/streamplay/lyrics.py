"""Lyrics from LRCLIB, falling back to lyrics.ovh, cached on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from . import __version__
from .config import CACHE_DIR

log = logging.getLogger(__name__)

LYRICS_DIR = CACHE_DIR / "lyrics"

LRCLIB = "https://lrclib.net/api"
LYRICS_OVH = "https://api.lyrics.ovh/v1"

#: LRCLIB asks every client to identify itself.
USER_AGENT = f"streamplay {__version__} (https://github.com/leissa/streamplay)"

#: Seconds before a track nobody had lyrics for is asked about again.
MISS_TTL = 7 * 24 * 3600

#: A search hit whose length differs by more than this is another recording.
DURATION_SLACK = 5.0

_TIMESTAMP = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text: str) -> list[dict[str, Any]]:
    lines = []
    for raw in text.splitlines():
        stamps = []
        rest = raw
        while match := _TIMESTAMP.match(rest):
            seconds = match.group(2).replace(":", ".")
            stamps.append(int(match.group(1)) * 60 + float(seconds))
            rest = rest[match.end():]
        lines.extend({"time": t, "text": rest.strip()} for t in stamps)
    lines.sort(key=lambda line: line["time"])
    return lines


def _result(record: dict[str, Any], provider: str) -> dict[str, Any]:
    synced = parse_lrc(record.get("syncedLyrics") or "")
    out: dict[str, Any] = {"provider": provider,
                           "plain": (record.get("plainLyrics") or "").strip()}
    if synced:
        out["synced"] = synced
    if record.get("instrumental"):
        out["instrumental"] = True
    return out


class LyricsCache:
    def __init__(self, directory: Path = LYRICS_DIR, lrclib: str = LRCLIB,
                 lyrics_ovh: str = LYRICS_OVH) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lrclib = lrclib
        self.lyrics_ovh = lyrics_ovh
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._inflight: dict[str, asyncio.Future] = {}

    async def fetch(self, title: str, artist: str, album: str = "",
                    duration: float = 0.0) -> dict[str, Any]:
        """Return ``{}`` when no provider knows the track."""
        if not title or not artist:
            return {}
        raw = f"{artist}\0{title}\0{album}\0{round(duration)}".lower()
        key = hashlib.sha1(raw.encode("utf-8")).hexdigest()

        cached = self._lookup(key)
        if cached is not None:
            return cached

        inflight = self._inflight.get(key)
        if inflight is not None:
            return await asyncio.shield(inflight)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await asyncio.to_thread(
                self._retrieve, title, artist, album, duration)
            self._store(key, result)
        except Exception as exc:
            log.info("lyrics lookup failed for %s - %s: %s", artist, title, exc)
            result = {}
        finally:
            self._inflight.pop(key, None)
            if not future.done():
                future.set_result(result)
        return result

    def _lookup(self, key: str) -> dict[str, Any] | None:
        path = self.dir / (key + ".json")
        try:
            entry = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not entry.get("lyrics") and time.time() - entry.get("at", 0) > MISS_TTL:
            return None
        return entry.get("lyrics") or {}

    def _store(self, key: str, lyrics: dict[str, Any]) -> None:
        path = self.dir / (key + ".json")
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps({"at": time.time(), "lyrics": lyrics}), "utf-8")
        tmp.replace(path)

    def _retrieve(self, title: str, artist: str, album: str,
                  duration: float) -> dict[str, Any]:
        return (self._lrclib_get(title, artist, album, duration)
                or self._lrclib_search(title, artist, duration)
                or self._ovh(title, artist))

    def _get(self, url: str, params: dict | None = None) -> Any:
        resp = self._session.get(url, params=params, timeout=(5, 15))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _lrclib_get(self, title: str, artist: str, album: str,
                    duration: float) -> dict[str, Any]:
        if not album or duration <= 0:
            return {}
        record = self._get(self.lrclib + "/get", {
            "track_name": title, "artist_name": artist,
            "album_name": album, "duration": round(duration)})
        return _result(record, "LRCLIB") if record else {}

    def _lrclib_search(self, title: str, artist: str,
                       duration: float) -> dict[str, Any]:
        records = self._get(self.lrclib + "/search",
                            {"track_name": title, "artist_name": artist}) or []
        if duration > 0:
            records = [r for r in records
                       if abs((r.get("duration") or 0) - duration) <= DURATION_SLACK]
        # Prefer a synced hit to a plain one wherever it ranks.
        records.sort(key=lambda r: not r.get("syncedLyrics"))
        for record in records:
            if record.get("syncedLyrics") or record.get("plainLyrics"):
                return _result(record, "LRCLIB")
        return {}

    def _ovh(self, title: str, artist: str) -> dict[str, Any]:
        record = self._get(f"{self.lyrics_ovh}/{quote(artist, safe='')}"
                           f"/{quote(title, safe='')}")
        plain = ((record or {}).get("lyrics") or "").replace("\r\n", "\n").strip()
        return {"provider": "lyrics.ovh", "plain": plain} if plain else {}

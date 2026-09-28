"""Drives the lyrics lookup against a scripted LRCLIB and lyrics.ovh, so no network is needed.
Run with ``python3 tests/test_lyrics.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay.lyrics import LyricsCache, parse_lrc

FAILURES: list[str] = []

SYNCED = "[ar:Neon]\n[00:01.50]First line\n[00:10.00][01:05.25]Chorus\n[00:20.00]\n"


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakeLyrics(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, payload=None) -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = unquote(url.path)
        self.requests.append((path, query))

        if "streamplay" not in (self.headers.get("User-Agent") or ""):
            return self._reply(403)
        if path == "/api/get":
            if query.get("track_name") == "Exact" and query.get("duration") == "180":
                return self._reply(200, {"trackName": "Exact", "duration": 180.0,
                                         "plainLyrics": "plain", "syncedLyrics": SYNCED})
            return self._reply(404, {"message": "not found"})
        if path == "/api/search":
            if query.get("track_name") == "Searched":
                return self._reply(200, [
                    {"duration": 400.0, "syncedLyrics": "[00:01.00]Live version"},
                    {"duration": 201.0, "plainLyrics": "Studio plain"},
                    {"duration": 199.0, "plainLyrics": "Studio",
                     "syncedLyrics": "[00:02.00]Studio synced"},
                ])
            if query.get("track_name") == "Instrumental":
                return self._reply(200, [{"duration": 60.0, "instrumental": True,
                                          "plainLyrics": None, "syncedLyrics": None}])
            return self._reply(200, [])
        if path == "/ovh/Neon/Only OVH":
            return self._reply(200, {"lyrics": "Line one\r\nLine two\r\n"})
        return self._reply(404, {"error": "No lyrics found"})


async def run(base: str, cache_dir: pathlib.Path) -> None:
    lyrics = LyricsCache(cache_dir, lrclib=base + "/api", lyrics_ovh=base + "/ovh")

    parsed = parse_lrc(SYNCED)
    check("lrc: tags dropped, repeated stamps expanded, sorted",
          [line["time"] for line in parsed] == [1.5, 10.0, 20.0, 65.25]
          and parsed[3]["text"] == "Chorus" and parsed[2]["text"] == "")

    exact = await lyrics.fetch("Exact", "Neon", "Night Drive", 180.2)
    check("get: synced and plain from the exact match",
          exact.get("provider") == "LRCLIB" and exact.get("plain") == "plain"
          and len(exact.get("synced") or []) == 4)

    searched = await lyrics.fetch("Searched", "Neon", "Night Drive", 200)
    check("search: another recording's length is skipped, synced preferred",
          searched.get("synced") == [{"time": 2.0, "text": "Studio synced"}])

    check("search: an instrumental with no text is not a hit",
          await lyrics.fetch("Instrumental", "Neon", "", 60) == {})

    ovh = await lyrics.fetch("Only OVH", "Neon", "", 0)
    check("lyrics.ovh: plain fallback with line endings normalised",
          ovh == {"provider": "lyrics.ovh", "plain": "Line one\nLine two"})

    check("missing title or artist asks nobody",
          await lyrics.fetch("", "Neon") == {} and await lyrics.fetch("X", "") == {})

    FakeLyrics.requests.clear()
    again = await lyrics.fetch("Exact", "Neon", "Night Drive", 180.2)
    miss = await lyrics.fetch("Nowhere", "Neon", "", 0)
    asked = len(FakeLyrics.requests)
    await lyrics.fetch("Nowhere", "Neon", "", 0)
    check("hits and misses come from the cache",
          again == exact and miss == {} and len(FakeLyrics.requests) == asked)

    both = await asyncio.gather(*(lyrics.fetch("Concurrent", "Neon", "", 200)
                                  for _ in range(3)))
    searches = [r for r in FakeLyrics.requests
                if r[0] == "/api/search" and r[1].get("track_name") == "Concurrent"]
    check("concurrent lookups share one request", len(searches) == 1
          and all(r == {} for r in both))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLyrics)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(f"http://127.0.0.1:{server.server_port}",
                            pathlib.Path(tmp)))
    finally:
        server.shutdown()
    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

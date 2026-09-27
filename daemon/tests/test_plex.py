"""Drives the Plex backend against a scripted HTTP server, so no Plex is needed.
Run with ``python3 tests/test_plex.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay.backends.base import BackendError
from streamplay.backends.plex import PlexBackend

FAILURES: list[str] = []

TOKEN = "plex-token"

ALBUMS = {
    "1": [{"ratingKey": "al1", "type": "album", "title": "Night Drive",
           "parentTitle": "Neon", "parentRatingKey": "ar1", "year": 2020,
           "leafCount": 2, "Genre": [{"tag": "Synthwave"}],
           "thumb": "/library/metadata/al1/thumb/1", "librarySectionID": 1}],
    "3": [{"ratingKey": "al2", "type": "album", "title": "Quiet", "parentTitle": "Mira",
           "parentRatingKey": "ar2", "leafCount": 1}],
}
TRACKS = [
    {"ratingKey": "t1", "type": "track", "title": "One", "originalTitle": "Neon feat. Guest",
     "grandparentTitle": "Neon", "grandparentRatingKey": "ar1", "parentTitle": "Night Drive",
     "parentRatingKey": "al1", "index": 1, "parentIndex": 1, "duration": 150000,
     "parentYear": 2020, "parentThumb": "/library/metadata/al1/thumb/1",
     "Media": [{"Part": [{"key": "/library/parts/11/123/file.flac"}]}]},
    {"ratingKey": "t2", "type": "track", "title": "Two", "grandparentTitle": "Neon",
     "parentRatingKey": "al1", "index": 2, "duration": 150000,
     "Media": [{"Part": [{"key": "/library/parts/12/123/file.flac"}]}]},
]


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakePlex(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict, dict]] = []

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, container=None) -> None:
        data = json.dumps({"MediaContainer": container}).encode() if container is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append((url.path, query, dict(self.headers)))
        if self.headers.get("X-Plex-Token") != TOKEN:
            return self._reply(401)
        path = url.path

        if path == "/library/sections":
            return self._reply(200, {"Directory": [
                {"key": "1", "type": "artist", "title": "Music"},
                {"key": "2", "type": "movie", "title": "Films"},
                {"key": "3", "type": "artist", "title": "More music"}]})
        if path.startswith("/library/sections/") and path.endswith("/genre"):
            section = path.split("/")[3]
            genre = {"1": [{"key": "101", "title": "Synthwave"}],
                     "3": [{"fastKey": "/library/sections/3/all?genre=301",
                            "key": "/library/sections/3/all?genre=301",
                            "title": "Ambient"}]}[section]
            return self._reply(200, {"Directory": genre})
        if path.startswith("/library/sections/") and path.endswith("/all"):
            section = path.split("/")[3]
            if query.get("type") == "8":
                items = [{"ratingKey": "ar1", "title": "Neon", "thumb": "/a/1"}] if section == "1" else []
            else:
                items = ALBUMS[section]
                if "genre" in query:
                    items = items if query["genre"] in ("101", "301") else []
            start = int(query.get("X-Plex-Container-Start", 0))
            size = int(query.get("X-Plex-Container-Size", 1000))
            return self._reply(200, {"totalSize": len(items),
                                     "Metadata": items[start:start + size]})
        if path == "/library/metadata/al1/children":
            return self._reply(200, {"Metadata": TRACKS})
        if path == "/library/metadata/ar1/children":
            return self._reply(200, {"Metadata": ALBUMS["1"]})
        if path == "/library/metadata/t2":
            return self._reply(200, {"Metadata": [TRACKS[1]]})
        if path == "/playlists":
            return self._reply(200, {"Metadata": [
                {"ratingKey": "pl1", "title": "Drive", "leafCount": 2,
                 "duration": 300000, "composite": "/playlists/pl1/composite/1"}]})
        if path == "/playlists/pl1/items":
            return self._reply(200, {"Metadata": TRACKS[::-1]})
        if path == "/hubs/search":
            return self._reply(200, {"Hub": [
                {"type": "album", "Metadata": ALBUMS["1"]},
                {"type": "movie", "Metadata": [{"type": "movie", "title": "Neon",
                                                "librarySectionID": 2}]}]})
        if path in ("/:/scrobble", "/:/timeline"):
            return self._reply(200)
        self._reply(404)


def last(path: str) -> dict | None:
    for p, query, _ in reversed(FakePlex.requests):
        if p == path:
            return query
    return None


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakePlex)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    profile = {"id": "px", "type": "plex", "url": f"127.0.0.1:{port}",
               "password": TOKEN}

    check("a bare host gets http and the default port",
          PlexBackend({"url": "nas"}).base == "http://nas:32400")

    try:
        bad = PlexBackend(dict(profile, password="wrong"))
        try:
            await bad.connect()
            check("a wrong token is refused", False)
        except BackendError:
            check("a wrong token is refused", True)
        await bad.close()

        px = PlexBackend(profile)
        await px.connect()
        check("only music sections are used", px.sections == ["1", "3"])
        headers = FakePlex.requests[-1][2]
        check("client identification is sent",
              headers.get("X-Plex-Client-Identifier") == "streamplay-px"
              and headers.get("Accept") == "application/json")

        artists = await px.artists()
        check("artists", [(a.id, a.name, a.cover_id, a.source) for a in artists]
              == [("ar1", "Neon", "/a/1", "px")])

        albums = await px.albums(sort="starred")
        query = last("/library/sections/3/all")
        check("album sort and filter reach the server",
              query.get("sort") == "album.titleSort"
              and query.get("album.userRating>>") == "0" and query.get("type") == "9")
        album = albums[0]
        check("albums merge across sections", [a.id for a in albums] == ["al1", "al2"])
        check("album fields", (album.name, album.artist, album.artist_id, album.year,
                               album.track_count, album.genre, album.cover_id)
              == ("Night Drive", "Neon", "ar1", 2020, 2, "Synthwave",
                  "/library/metadata/al1/thumb/1"))
        check("paging continues into the next section",
              [a.id for a in await px.albums(offset=1, limit=5)] == ["al2"])

        one, two = await px.album_tracks("al1")
        check("track fields", (one.title, one.artist, one.album, one.album_id,
                               one.artist_id, one.track_no, one.disc_no, one.duration,
                               one.year, one.cover_id, one.backend)
              == ("One", "Neon feat. Guest", "Night Drive", "al1", "ar1", 1, 1,
                  150.0, 2020, "/library/metadata/al1/thumb/1", "plex"))
        check("track falls back to the album artist", two.artist == "Neon")
        check("artist albums", [a.id for a in await px.artist_albums("ar1")] == ["al1"])

        check("genres merge across sections", await px.genres() == ["Ambient", "Synthwave"])
        await px.genre_albums("Ambient")
        check("genre albums filter by the section's tag id",
              last("/library/sections/3/all").get("genre") == "301")

        playlists = await px.playlists()
        check("playlists", playlists == [{"id": "pl1", "source": "px", "name": "Drive",
                                          "trackCount": 2, "duration": 300.0,
                                          "coverId": "/playlists/pl1/composite/1"}]
              and last("/playlists").get("playlistType") == "audio")
        check("playlist tracks keep their order",
              [t.id for t in await px.playlist_tracks("pl1")] == ["t2", "t1"])

        found = await px.search("neon")
        check("search keeps only music sections",
              [len(found[k]) for k in ("artists", "albums", "tracks")] == [0, 1, 0])

        target = await px.stream_target(one)
        check("stream is the part with the token",
              target.url == f"http://127.0.0.1:{port}/library/parts/11/123/file.flac"
                            f"?X-Plex-Token={TOKEN}" and target.source == "px")
        two.extra.clear()
        target = await px.stream_target(two)
        check("a track from the wire looks its part up",
              target.url.split("?")[0].endswith("/library/parts/12/123/file.flac"))

        url, params, _ = px.cover_request("/library/metadata/al1/thumb/1", 256)
        check("cover goes through the transcoder",
              url.endswith("/photo/:/transcode") and params.get("width") == "256"
              and params.get("url") == "/library/metadata/al1/thumb/1"
              and params.get("X-Plex-Token") == TOKEN)

        await px.scrobble(one, submission=False)
        query = last("/:/timeline")
        check("now playing is reported",
              query.get("ratingKey") == "t1" and query.get("state") == "playing"
              and query.get("duration") == "150000")
        await px.scrobble(one, submission=True)
        check("a play is scrobbled",
              last("/:/scrobble") == {"key": "t1", "identifier": "com.plexapp.plugins.library"})
        await px.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")

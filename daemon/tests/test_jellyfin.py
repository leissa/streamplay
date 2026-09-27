"""Drives the Jellyfin backend against a scripted HTTP server, so no Jellyfin is needed.
Run with ``python3 tests/test_jellyfin.py`` from ``daemon``.
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

from streamplay.backends import BackendError, create_backend

FAILURES: list[str] = []

TOKEN = "tok-123"
USER = "user-1"

ALBUM = {
    "Id": "al1", "Name": "Night Drive", "AlbumArtist": "Neon",
    "AlbumArtists": [{"Id": "ar1", "Name": "Neon"}], "ProductionYear": 2020,
    "ChildCount": 2, "RunTimeTicks": 3_000_000_000, "Genres": ["Synthwave"],
    "ImageTags": {"Primary": "abc"},
}
TRACKS = [
    {"Id": "t1", "Name": "One", "Artists": ["Neon", "Guest"], "Album": "Night Drive",
     "AlbumId": "al1", "AlbumPrimaryImageTag": "abc",
     "ArtistItems": [{"Id": "ar1"}], "IndexNumber": 1, "ParentIndexNumber": 1,
     "RunTimeTicks": 1_500_000_000, "ProductionYear": 2020},
    {"Id": "t2", "Name": "Two", "Artists": [], "AlbumArtist": "Neon",
     "AlbumArtists": [{"Id": "ar1"}], "IndexNumber": 2, "RunTimeTicks": 1_500_000_000},
]


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakeJellyfin(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict, dict]] = []

    def log_message(self, *args) -> None:
        pass

    def _reply(self, status: int, payload=None) -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        self.requests.append((method, url.path, query, body))

        auth = self.headers.get("Authorization") or ""
        if url.path == "/Users/AuthenticateByName":
            if not auth.startswith("MediaBrowser ") or body.get("Pw") != "secret":
                return self._reply(401)
            return self._reply(200, {"AccessToken": TOKEN, "User": {"Id": USER}})
        if f'Token="{TOKEN}"' not in auth:
            return self._reply(401)
        if method == "POST":
            return self._reply(204)

        if url.path == "/Artists/AlbumArtists":
            items = [{"Id": "ar1", "Name": "Neon", "ImageTags": {}}]
        elif url.path == "/Playlists/pl1/Items":
            items = TRACKS[::-1]
        elif url.path == "/Genres":
            items = [{"Name": "Synthwave"}, {"Name": ""}]
        elif query.get("IncludeItemTypes") == "Playlist":
            items = [{"Id": "pl1", "Name": "Drive", "ChildCount": 2,
                      "RunTimeTicks": 3_000_000_000}]
        elif query.get("IncludeItemTypes") == "MusicAlbum":
            items = [ALBUM]
        elif query.get("ParentId") == "al1":
            items = TRACKS
        else:
            items = []
        self._reply(200, {"Items": items, "TotalRecordCount": len(items)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def last(method: str, path: str) -> tuple[dict, dict] | None:
    for m, p, query, body in reversed(FakeJellyfin.requests):
        if (m, p) == (method, path):
            return query, body
    return None


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJellyfin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    profile = {"id": "jf", "type": "jellyfin", "url": url,
               "username": "roland", "password": "secret"}

    try:
        bad = create_backend(dict(profile, password="wrong"))
        try:
            await bad.connect()
            check("a wrong password is refused", False)
        except BackendError:
            check("a wrong password is refused", True)
        await bad.close()

        jf = create_backend(profile)
        await jf.connect()
        check("login yields token and user", (jf.token, jf.user_id) == (TOKEN, USER))

        artists = await jf.artists()
        check("artists", [(a.id, a.name, a.source, a.cover_id) for a in artists]
              == [("ar1", "Neon", "jf", None)])

        albums = await jf.albums(sort="recent", offset=5, limit=1000)
        query, _ = last("GET", "/Items")
        check("album sort, filter and paging reach the server",
              query.get("SortBy") == "DatePlayed,SortName"
              and query.get("SortOrder") == "Descending"
              and query.get("Filters") == "IsPlayed"
              and query.get("StartIndex") == "5" and query.get("Limit") == "500"
              and query.get("userId") == USER)
        album = albums[0]
        check("album fields", (album.name, album.artist, album.artist_id, album.year,
                               album.track_count, album.duration, album.genre,
                               album.cover_id)
              == ("Night Drive", "Neon", "ar1", 2020, 2, 300.0, "Synthwave", "al1"))

        tracks = await jf.album_tracks("al1")
        one, two = tracks
        check("track fields", (one.title, one.artist, one.album_id, one.track_no,
                               one.disc_no, one.duration, one.cover_id, one.backend)
              == ("One", "Neon, Guest", "al1", 1, 1, 150.0, "al1", "jellyfin"))
        check("track falls back to the album artist",
              (two.artist, two.artist_id, two.cover_id) == ("Neon", "ar1", None))

        playlists = await jf.playlists()
        check("playlists", playlists == [{"id": "pl1", "source": "jf", "name": "Drive",
                                          "trackCount": 2, "duration": 300.0,
                                          "coverId": None}])
        check("playlist tracks keep their order",
              [t.id for t in await jf.playlist_tracks("pl1")] == ["t2", "t1"])
        check("genres", await jf.genres() == ["Synthwave"])

        found = await jf.search("neon")
        check("search covers all three kinds",
              [len(found[k]) for k in ("artists", "albums", "tracks")] == [0, 1, 0])

        target = await jf.stream_target(one)
        stream = urlsplit(target.url)
        params = {k: v[0] for k, v in parse_qs(stream.query).items()}
        check("raw stream is static and authorised",
              stream.path == "/Audio/t1/stream" and params.get("static") == "true"
              and params.get("api_key") == TOKEN and target.source == "jf")

        jf.max_bitrate, jf.stream_format = 192, "opus"
        stream = urlsplit(jf.stream_url(one))
        params = {k: v[0] for k, v in parse_qs(stream.query).items()}
        check("transcoded stream asks for the codec and bitrate",
              stream.path == "/Audio/t1/universal"
              and params.get("AudioCodec") == "opus"
              and params.get("TranscodingContainer") == "ogg"
              and params.get("MaxStreamingBitrate") == "192000")

        cover_url, cover_params, headers = jf.cover_request("al1", 256)
        check("cover request", cover_url == url.rstrip("/") + "/Items/al1/Images/Primary"
              and cover_params.get("maxWidth") == "256"
              and TOKEN in headers.get("Authorization", ""))

        await jf.scrobble(one, submission=False)
        _, body = last("POST", "/Sessions/Playing")
        check("now playing is reported", body.get("ItemId") == "t1")
        await jf.scrobble(one, submission=True)
        _, body = last("POST", "/Sessions/Playing/Stopped")
        check("a play is reported as stopped at the end",
              body.get("ItemId") == "t1" and body.get("PositionTicks") == 1_500_000_000)

        await jf.close()
        check("close logs out", last("POST", "/Sessions/Logout") is not None)
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")

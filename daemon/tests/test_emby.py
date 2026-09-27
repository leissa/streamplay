"""Drives the Emby backend against a scripted HTTP server, so no Emby is needed.
Run with ``python3 tests/test_emby.py`` from ``daemon``.
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
from streamplay.backends.emby import EmbyBackend

FAILURES: list[str] = []

TOKEN = "emby-tok"
USER = "emby-user"


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakeEmby(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []

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
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        self.requests.append((method, url.path, body))

        if not url.path.startswith("/emby/") or self.headers.get("Authorization"):
            return self._reply(404)
        path = url.path[len("/emby"):]
        auth = self.headers.get("X-Emby-Authorization") or ""
        if path == "/Users/AuthenticateByName":
            if not auth.startswith("MediaBrowser ") or body.get("Pw") != "secret":
                return self._reply(401)
            return self._reply(200, {"AccessToken": TOKEN, "User": {"Id": USER}})
        if self.headers.get("X-Emby-Token") != TOKEN:
            return self._reply(401)
        if method == "POST":
            return self._reply(204)
        if path == "/Artists/AlbumArtists":
            return self._reply(200, {"Items": [{"Id": "ar1", "Name": "Neon"}]})
        self._reply(200, {"Items": [{"Id": "t1", "Name": "One", "AlbumId": "al1",
                                     "RunTimeTicks": 1_200_000_000}]})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmby)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    profile = {"id": "emby", "type": "emby", "url": root,
               "username": "roland", "password": "secret"}

    try:
        check("the /emby path is added once",
              EmbyBackend(dict(profile, url=root + "/emby/")).base == root + "/emby")

        bad = EmbyBackend(dict(profile, password="wrong"))
        try:
            await bad.connect()
            check("a wrong password is refused", False)
        except BackendError:
            check("a wrong password is refused", True)
        await bad.close()

        emby = EmbyBackend(profile)
        await emby.connect()
        check("login yields token and user", (emby.token, emby.user_id) == (TOKEN, USER))
        check("artists", [a.name for a in await emby.artists()] == ["Neon"])
        tracks = await emby.album_tracks("al1")
        check("tracks", [(t.id, t.duration, t.backend) for t in tracks]
              == [("t1", 120.0, "emby")])

        stream = urlsplit(emby.stream_url(tracks[0]))
        check("stream goes through /emby with the token",
              stream.path == "/emby/Audio/t1/stream"
              and parse_qs(stream.query).get("api_key") == [TOKEN])

        cover_url, _, headers = emby.cover_request("al1", 128)
        check("cover request carries Emby's headers",
              cover_url == root + "/emby/Items/al1/Images/Primary"
              and headers.get("X-Emby-Token") == TOKEN
              and "Authorization" not in headers)

        await emby.scrobble(tracks[0], submission=True)
        check("a play is reported", ("POST", "/emby/Sessions/Playing/Stopped")
              in [(m, p) for m, p, _ in FakeEmby.requests])
        await emby.close()
        check("close logs out", ("POST", "/emby/Sessions/Logout")
              in [(m, p) for m, p, _ in FakeEmby.requests])
    finally:
        server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")

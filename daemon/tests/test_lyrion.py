"""Drives the Lyrion library and its player outputs against a scripted JSON-RPC server,
so no Lyrion is needed. Run with ``python3 tests/test_lyrion.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay.backends.base import BackendError, StreamTarget
from streamplay.backends.lyrion import LyrionBackend
from streamplay.models import Track

FAILURES: list[str] = []

ALBUMS = [{"id": 7, "album": "Night Drive", "year": 2020, "artwork_track_id": "c7",
           "artist": "Neon", "artist_id": 3}]
TITLES = [
    {"id": 12, "title": "Two", "artist": "Neon", "album": "Night Drive", "album_id": 7,
     "artist_id": 3, "tracknum": "2", "disc": "1", "duration": "150.5", "year": "2020",
     "genre": "Synthwave", "coverid": "c7"},
    {"id": 11, "title": "One", "artist": "Neon", "album": "Night Drive", "album_id": 7,
     "artist_id": 3, "tracknum": "1", "disc": "1", "duration": 90, "year": 0},
]


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakeLyrion(BaseHTTPRequestHandler):
    commands: list[tuple[str, list[str]]] = []
    players = [{"playerid": "aa:01", "name": "Kitchen", "connected": 1},
               {"playerid": "aa:02", "name": "Offline", "connected": 0}]
    status: dict = {"mode": "stop", "power": 1, "mixer volume": 40}

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        if self.headers.get("Authorization") is None:
            self.send_response(401)
            self.end_headers()
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        player, command = request["params"]
        self.commands.append((player, command))
        data = json.dumps({"id": request["id"], "result": self.answer(command)}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def answer(self, command: list[str]) -> dict:
        head = command[0]
        if head == "players":
            return {"count": len(self.players), "players_loop": self.players}
        if head == "artists":
            return {"artists_loop": [{"id": 3, "artist": "Neon"}]}
        if head == "albums":
            return {"albums_loop": ALBUMS}
        if head == "titles":
            return {"titles_loop": TITLES}
        if head == "genres":
            return {"genres_loop": [{"id": 5, "genre": "Synthwave"}]}
        if command[:2] == ["playlists", "tracks"]:
            return {"playlisttracks_loop": TITLES[::-1]}
        if head == "playlists":
            return {"playlists_loop": [{"id": 9, "playlist": "Drive"}]}
        if head == "status":
            return dict(self.status)
        return {}


def last(player: str, head: str) -> list[str] | None:
    for p, command in reversed(FakeLyrion.commands):
        if p == player and command[0] == head:
            return command
    return None


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLyrion)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = {"id": "lms", "name": "Lyrion", "host": "127.0.0.1",
               "port": server.server_address[1], "username": "u", "password": "p w"}

    try:
        try:
            await LyrionBackend(dict(profile, username="")).connect()
            check("a missing login is refused", False)
        except BackendError:
            check("a missing login is refused", True)

        lms = LyrionBackend(profile)
        await lms.connect()
        await library(lms)
        await discovery(lms)
        await playback(lms)
        await lms.close()
    finally:
        server.shutdown()


async def library(lms: LyrionBackend) -> None:
    album = (await lms.albums(sort="newest", offset=10))[0]
    check("album sort and paging reach the server",
          last("", "albums")[1:4] == ["10", "100", "sort:new"])
    check("album fields", (album.id, album.name, album.artist, album.artist_id, album.year,
                           album.cover_id, album.source)
          == ("7", "Night Drive", "Neon", "3", 2020, "c7", "lms"))

    one, two = await lms.album_tracks("7")
    check("album tracks in disc and track order", [one.id, two.id] == ["11", "12"])
    check("track fields", (two.title, two.duration, two.track_no, two.genre, two.cover_id,
                           two.backend, one.year)
          == ("Two", 150.5, 2, "Synthwave", "c7", "lyrion", None))

    check("artists", [(a.id, a.name) for a in await lms.artists()] == [("3", "Neon")])
    check("genres", await lms.genres() == ["Synthwave"])
    await lms.genre_albums("Synthwave")
    check("genre albums go by genre id", "genre_id:5" in last("", "albums"))
    check("playlists", await lms.playlists()
          == [{"id": "9", "source": "lms", "name": "Drive"}])
    check("playlist tracks", [t.id for t in await lms.playlist_tracks("9")] == ["11", "12"])
    found = await lms.search("neon")
    check("search asks each query", [len(found[k]) for k in ("artists", "albums", "tracks")]
          == [1, 1, 2] and "search:neon" in last("", "titles"))

    target = await lms.stream_target(one)
    check("stream target", target.url.endswith("/music/11/download")
          and "u:p%20w@" in target.url and target.native == {"track_id": "11"})
    url, _, headers = lms.cover_request("c7", 300)
    check("cover request", url.endswith("/music/c7/cover_300x300_o")
          and headers["Authorization"].startswith("Basic "))


async def discovery(lms: LyrionBackend) -> None:
    calls: list[int] = []
    lms.watch_sinks(lambda: calls.append(1))
    kitchen = lms.sinks()
    check("only connected players are outputs",
          [(s.id, s.name, s.source) for s in kitchen] == [("lyrion:lms:aa:01", "Kitchen", "lms")])

    FakeLyrion.players = FakeLyrion.players + [
        {"playerid": "aa:03", "name": "Study", "connected": 1}]
    if await lms._refresh_players():
        lms.sinks_changed()
    ids = [s.id for s in lms.sinks()]
    check("a new player appears", calls == [1] and ids == ["lyrion:lms:aa:01", "lyrion:lms:aa:03"])
    check("a known player keeps its sink", lms.sinks()[0] is kitchen[0])

    check("an unchanged list changes nothing", not await lms._refresh_players())

    FakeLyrion.players = [p for p in FakeLyrion.players if p["playerid"] != "aa:01"]
    changed = await lms._refresh_players()
    check("a vanished player goes", changed and [s.id for s in lms.sinks()] == ["lyrion:lms:aa:03"])


async def playback(lms: LyrionBackend) -> None:
    sink = lms.sinks()[0]
    ended: list[str] = []

    async def on_ended(reason: str) -> None:
        ended.append(reason)

    sink.wire(on_ended, lambda: None)
    await sink.start()
    check("volume read from the player", abs(sink.state.volume - 0.4) < 1e-6)

    own = Track(id="11", title="One", source="lms", backend="lyrion", duration=90)
    FakeLyrion.status = {"mode": "play", "time": 1.0, "duration": 90, "power": 1,
                         "mixer volume": 40}
    await sink.play(await lms.stream_target(own), own)
    check("own tracks load natively",
          last("aa:03", "playlistcontrol") == ["playlistcontrol", "cmd:load", "track_id:11"])
    check("repeat and shuffle are forced off",
          ["playlist", "repeat", "0"] in [c for p, c in FakeLyrion.commands if p == "aa:03"]
          and ["playlist", "shuffle", "0"] in [c for p, c in FakeLyrion.commands if p == "aa:03"])

    foreign = Track(id="x", title="Web", source="subsonic", backend="subsonic", duration=90)
    await sink.play(StreamTarget(url="http://music/x.flac", source="subsonic"), foreign)
    check("other services play by URL",
          last("aa:03", "playlist") == ["playlist", "play", "http://music/x.flac", "Web"])

    mpd = Track(id="a.flac", title="Local", source="mpd", backend="mpd")
    check("MPD tracks are refused", not sink.plays(mpd) and sink.plays(foreign))
    try:
        await sink.play(StreamTarget(url="file:///a.flac", source="mpd"), mpd)
        check("a file URL is refused", False)
    except BackendError:
        check("a file URL is refused", True)

    FakeLyrion.status = dict(FakeLyrion.status, mode="play", time=40.0)
    await sink.play(await lms.stream_target(own), own)
    FakeLyrion.status = dict(FakeLyrion.status, mode="stop", time=0)
    await sink._sync()
    check("a stop mid-track is a user's stop",
          ended == [] and sink.state.status == "stopped")

    FakeLyrion.status = dict(FakeLyrion.status, mode="play", time=89.0)
    await sink.play(await lms.stream_target(own), own)
    FakeLyrion.status = dict(FakeLyrion.status, mode="stop", time=0)
    await sink._sync()
    check("a stop at the end is eof", ended == ["eof"])

    FakeLyrion.status = dict(FakeLyrion.status, mode="stop", waitingToPlay=1, time=0)
    await sink.play(await lms.stream_target(own), own)
    check("a player still buffering counts as playing",
          sink.state.status == "playing" and sink.state.buffering and ended == ["eof"])

    FakeLyrion.status = {"mode": "play", "power": 0, "time": 5, "duration": 90}
    await sink._sync()
    check("a powered-off player is stopped and has no mixer",
          sink.state.status == "stopped" and not sink.capabilities()["volume"])

    await sink.seek(12.5)
    await sink.set_volume(0.5)
    check("seek", last("aa:03", "time") == ["time", "12.5"])
    await sink.close()


if __name__ == "__main__":
    asyncio.run(main())
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        sys.exit(1)
    print("\nall checks passed")

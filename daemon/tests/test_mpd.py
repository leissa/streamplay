"""Exercises the MPD library and output against ``fake_mpd``, so nothing has to be
installed. Run with ``python3 tests/test_mpd.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fake_mpd import ART, EMBEDDED, FakeMpd
from streamplay.backends.base import BackendError, StreamTarget
from streamplay.backends.mpd import ID_SEP, MpdBackend, MpdSink
from streamplay.models import Track

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


def profile(server: FakeMpd, **extra) -> dict:
    return {"id": "mpd-test", "name": "Test MPD", "type": "mpd",
            "host": "127.0.0.1", "port": server.port, **extra}


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll until something becomes true, so the tests never hang for long."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()



async def test_library() -> None:
    server = FakeMpd()
    await server.start()
    backend = MpdBackend(profile(server))
    await backend.connect()

    check("the greeting gives the server version", backend.client.version == "0.24.0")

    artists = await backend.artists()
    by_name = {a.name: a for a in artists}
    check("every album artist is listed once",
          sorted(by_name) == ["Alba Nova", "Cobalt Choir", "The Odds"])
    check("artists carry an album count", by_name["Alba Nova"].album_count == 2)
    check("artists are stamped with their source",
          all(a.source == "mpd-test" for a in artists))

    albums = await backend.albums()
    named = {a.name: a for a in albums}
    check("all four albums come back", len(albums) == 4)
    check("an album knows its artist", named["First Light"].artist == "Alba Nova")
    check("a full date is reduced to a year", named["First Light"].year == 1998)
    check("an album id carries artist and title",
          named["First Light"].id == "Alba Nova" + ID_SEP + "First Light")

    discography = await backend.artist_albums("Alba Nova")
    check("an artist's albums are just theirs",
          sorted(a.name for a in discography) == ["First Light", "Second Wind"])

    tracks = await backend.album_tracks(named["First Light"].id)
    check("an album's tracks come back in track order",
          [t.title for t in tracks] == ["Dawn", "Meridian", "Dusk"])
    check("a track number written as 3/3 means three", tracks[2].track_no == 3)
    check("a track id is the path, so it survives a round trip",
          tracks[0].id == "Alba Nova/First Light/1 - Dawn.flac")
    check("a track carries its duration", tracks[0].duration == 0.8)
    check("a track carries its genre", tracks[0].genre == "Ambient")

    # A backslash and an apostrophe must survive both layers of quoting.
    odd = named["Rock'n'Roll \\ Forever"]
    odd_tracks = await backend.album_tracks(odd.id)
    check("an album whose name needs escaping can still be opened",
          [t.title for t in odd_tracks] == ["Slash"])

    check("genres are listed", await backend.genres() ==
          ["Ambient", "Electronic", "Rock"])
    ambient = await backend.genre_albums("Ambient")
    check("a genre lists only its albums",
          sorted(a.name for a in ambient) == ["First Light", "Second Wind"])
    check("and the albums are stamped with it",
          all(a.genre == "Ambient" for a in ambient))

    check("stored playlists are listed",
          [p["name"] for p in await backend.playlists()] == ["Blue Mood"])
    check("a stored playlist gives up its tracks",
          [t.title for t in await backend.playlist_tracks("Blue Mood")] == ["Azure"])

    found = await backend.search("deep")
    check("search ignores case when matching an album",
          [a.name for a in found["albums"]] == ["Deep Blue"])
    found = await backend.search("alba")
    check("search ignores case when matching an artist",
          [a.name for a in found["artists"]] == ["Alba Nova"])
    check("searching an artist also finds their albums",
          sorted(a.name for a in found["albums"]) == ["First Light", "Second Wind"])
    found = await backend.search("DUSK")
    check("search ignores case when matching a title",
          [t.title for t in found["tracks"]] == ["Dusk"])

    await backend.close()
    await server.stop()



async def test_connecting() -> None:
    server = FakeMpd(password="hunter2")
    await server.start()

    backend = MpdBackend(profile(server))
    try:
        await backend.connect()
        check("a server wanting a password refuses an anonymous client", False)
    except BackendError as exc:
        check("a server wanting a password refuses an anonymous client",
              "permission" in str(exc).lower())
    await backend.close()

    backend = MpdBackend(profile(server, password="wrong"))
    try:
        await backend.connect()
        check("a wrong password is reported as such", False)
    except BackendError as exc:
        check("a wrong password is reported as such",
              "password" in str(exc).lower())
    await backend.close()

    backend = MpdBackend(profile(server, password="hunter2"))
    await backend.connect()
    check("the right password gets in", bool(await backend.artists()))

    # Pull the socket away: the next call should redial rather than fail.
    await backend.client.close()
    check("a dropped connection is re-established on the next call",
          len(await backend.artists()) == 3)
    await backend.close()
    await server.stop()

    wrong = FakeMpd(greeting="HTTP/1.1 400 Bad Request")
    await wrong.start()
    backend = MpdBackend(profile(wrong))
    try:
        await backend.connect()
        check("something that is not MPD is rejected", False)
    except BackendError as exc:
        check("something that is not MPD is rejected", "not an MPD" in str(exc))
    await backend.close()
    await wrong.stop()

    backend = MpdBackend({"id": "x", "name": "Nobody", "type": "mpd",
                          "host": "127.0.0.1", "port": 1})
    try:
        await backend.connect()
        check("a server that is not there is reported, not raised raw", False)
    except BackendError as exc:
        check("a server that is not there is reported, not raised raw",
              "Nobody" in str(exc))
    await backend.close()



async def test_stream_targets(tmp: pathlib.Path) -> None:
    server = FakeMpd()
    await server.start()

    backend = MpdBackend(profile(server))
    await backend.connect()
    track = (await backend.album_tracks("Alba Nova" + ID_SEP + "First Light"))[0]

    target = await backend.stream_target(track)
    check("MPD's own player always gets the path it knows",
          target.native == {"uri": "Alba Nova/First Light/1 - Dawn.flac"})
    check("without a music folder there is no URL for anyone else",
          target.url is None)
    await backend.close()

    song = tmp / "Alba Nova" / "First Light"
    song.mkdir(parents=True)
    (song / "1 - Dawn.flac").write_bytes(b"not really a flac")

    backend = MpdBackend(profile(server, musicDirectory=str(tmp)))
    await backend.connect()
    target = await backend.stream_target(track)
    check("with a music folder the track becomes a file URL any player opens",
          target.url == (song / "1 - Dawn.flac").as_uri())

    missing = Track(id="Gone/Missing.flac", title="Missing", source="mpd-test")
    check("a file the music folder does not have gives no URL",
          (await backend.stream_target(missing)).url is None)

    radio = Track(id="http://radio.example/stream.mp3", title="Radio",
                  source="mpd-test")
    check("a web stream in MPD's database is already a URL",
          (await backend.stream_target(radio)).url
          == "http://radio.example/stream.mp3")

    check("cover art arrives whole even though MPD sends it in pieces",
          await backend.cover_bytes(track.id, 0) == ART)
    check("art embedded in the file is found when there is no folder image",
          await backend.cover_bytes("Cobalt Choir/Deep Blue/1 - Azure.flac", 0)
          == EMBEDDED)
    check("a track with no art at all says so",
          await backend.cover_bytes("Alba Nova/Second Wind/1 - Gust.flac", 0)
          is None)

    await backend.close()
    await server.stop()



async def test_sink() -> None:
    server = FakeMpd()
    await server.start()
    backend = MpdBackend(profile(server))
    await backend.connect()

    sink = MpdSink(backend)
    ended: list[str] = []

    async def on_ended(reason: str) -> None:
        ended.append(reason)

    sink.wire(on_ended, lambda: None)
    await sink.start()

    check("the output belongs to its profile, so the hub can drop both together",
          sink.source == "mpd-test" and sink.id == "mpd:mpd-test")
    check("MPD's own play order is neutralised, because the queue is ours",
          server.options == {"repeat": "0", "random": "0", "single": "1",
                             "consume": "0"})
    check("the volume MPD reports is adopted", abs(sink.state.volume - 0.42) < 0.01)

    track = (await backend.album_tracks("Alba Nova" + ID_SEP + "First Light"))[0]
    target = await backend.stream_target(track)

    await sink.play(target, track)
    check("playing hands MPD one song and nothing else",
          server.queue == ["Alba Nova/First Light/1 - Dawn.flac"])
    check("and it is playing", sink.state.status == "playing")

    await sink.pause()
    check("pause reaches MPD", server.status == "pause"
          and sink.state.status == "paused")
    await sink.resume()
    check("resume reaches MPD", server.status == "play"
          and sink.state.status == "playing")

    await sink.seek(0.3)
    check("seeking moves MPD's position", abs(server.elapsed() - 0.3) < 0.15)

    check("the track running out is reported as the end of the track",
          await wait_for(lambda: ended == ["eof"]))
    check("and the output settles on stopped", sink.state.status == "stopped")

    ended.clear()
    await sink.play(target, track)
    await sink.stop()
    await asyncio.sleep(0.3)
    check("stopping it ourselves is not the end of a track", ended == [])

    ended.clear()
    await sink.play(target, track)
    await asyncio.sleep(0.1)
    await backend.call("stop")          # as though ncmpcpp had done it
    await asyncio.sleep(0.4)
    check("somebody else stopping MPD mid-track does not roll the queue on",
          ended == [])
    check("and we follow MPD rather than insisting", sink.state.status == "stopped")

    ended.clear()
    await sink.play(target, track)
    check("a change made elsewhere is noticed without waiting for a poll",
          await wait_for(lambda: ended == ["eof"], timeout=2.0))

    ended.clear()
    server.broken.add("Alba Nova/First Light/1 - Dawn.flac")
    await sink.play(target, track)
    check("a track MPD cannot play is reported as a failure, not as an ending",
          await wait_for(lambda: ended == ["error"]))
    check("and the reason is passed along", bool(sink.state.error))
    server.broken.clear()

    ended.clear()
    await sink.play(target, track)
    check("the failure is forgotten, so the next track is not blamed for it",
          sink.state.error is None and "clearerror" in server.seen)
    check("and that track ends normally",
          await wait_for(lambda: ended == ["eof"], timeout=2.0))

    foreign = StreamTarget(url="http://music.example/stream?id=7",
                           source="subsonic")
    await sink.play(foreign, track)
    check("a web stream from another service plays on MPD too",
          server.queue == ["http://music.example/stream?id=7"])

    local = StreamTarget(url="file:///tmp/nothing.flac", source="subsonic")
    try:
        await sink.play(local, track)
        check("a local file from another service is refused with a reason", False)
    except BackendError as exc:
        check("a local file from another service is refused with a reason",
              "web streams" in str(exc))

    await sink.set_volume(0.8)
    check("volume is passed on when MPD has a mixer", server.volume == 80)

    await sink.close()
    await backend.close()
    await server.stop()


async def test_sink_without_mixer() -> None:
    server = FakeMpd(mixer=False)
    await server.start()
    backend = MpdBackend(profile(server))
    await backend.connect()
    sink = MpdSink(backend)
    sink.wire(lambda reason: asyncio.sleep(0), lambda: None)
    await sink.start()

    check("an output with no mixer says volume is not on offer",
          sink.capabilities()["volume"] is False)
    await sink.set_volume(0.5)
    check("and setting it is remembered rather than pushed at MPD",
          abs(sink.state.volume - 0.5) < 0.001 and "setvol" not in server.seen)

    await sink.close()
    await backend.close()
    await server.stop()


async def main() -> None:
    import tempfile
    await test_library()
    await test_connecting()
    with tempfile.TemporaryDirectory() as tmp:
        await test_stream_targets(pathlib.Path(tmp))
    await test_sink()
    await test_sink_without_mixer()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for label in FAILURES:
            print("  - " + label)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())

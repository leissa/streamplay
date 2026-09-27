"""Exercises the queue against a real mpv process, generating its own tones.

Run with ``python3 tests/test_player.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import math
import pathlib
import struct
import sys
import tempfile
import time
import wave

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay import player as player_module
from streamplay.backends.base import Sink, SourceUnavailable, StreamTarget
from streamplay.models import Track
from streamplay.player import UnifiedPlayer
from streamplay.mpvproc import MpvError
from streamplay.sinks import MpvSink

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


def make_tones(directory: pathlib.Path, count: int, seconds: float = 3.0) -> None:
    for index in range(1, count + 1):
        freq = 180 + index * 70
        with wave.open(str(directory / f"{index}.wav"), "w") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"".join(
                struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * t / 8000)))
                for t in range(int(8000 * seconds))
            ))


class FakeLibrary:
    """Stands in for a music service: maps a track back to a local file."""

    def __init__(self, source: str, directory: pathlib.Path) -> None:
        self.source = source
        self.directory = directory
        self.scrobbles: list[tuple[str, bool]] = []

    def tracks(self, count: int) -> list[Track]:
        return [
            Track(id=str(i), title=f"{self.source} {i}", artist="Test",
                  album="Tones", duration=3.0, backend="fake", source=self.source)
            for i in range(1, count + 1)
        ]

    async def stream_target(self, track: Track) -> StreamTarget:
        return StreamTarget(url=(self.directory / f"{track.id}.wav").as_uri(),
                            source=self.source)

    async def scrobble(self, track: Track, submission: bool) -> None:
        self.scrobbles.append((track.id, submission))


class Router:
    """The hub's role: send each track to the service it came from."""

    def __init__(self, *libraries: FakeLibrary) -> None:
        self.libraries = {lib.source: lib for lib in libraries}

    async def stream_target(self, track: Track) -> StreamTarget:
        library = self.libraries.get(track.source)
        if library is None:
            raise SourceUnavailable(f"{track.source} is not connected")
        return await library.stream_target(track)

    async def scrobble(self, track: Track, submission: bool) -> None:
        library = self.libraries.get(track.source)
        if library is not None:
            await library.scrobble(track, submission)

    def unavailable(self, track: Track, sink: Sink | None) -> str | None:
        if track.source not in self.libraries:
            return f"{track.source} is not connected"
        if sink is not None and not sink.plays(track):
            return f"{sink.name} plays only its own library"
        return None


class FakeSink(Sink):
    """A silent output, so switching destinations can be tested offline."""

    id = "fake"
    name = "Fake output"

    def __init__(self, own: str | None = None) -> None:
        super().__init__()
        self.played: list[str] = []
        #: When set, the output refuses every other service, as Kodi and MPD do.
        self.own = own

    def plays(self, track: Track) -> bool:
        return self.own is None or track.source == self.own

    async def play(self, target: StreamTarget, track: Track) -> None:
        self.played.append(track.title)
        self.state.status = "playing"
        self.state.position = 0.0
        self.state.duration = track.duration
        self._changed()

    async def resume(self) -> None:
        self.state.status = "playing"
        self._changed()

    async def pause(self) -> None:
        self.state.status = "paused"
        self._changed()

    async def stop(self) -> None:
        self.state.status = "stopped"
        self.state.position = 0.0
        self._changed()

    async def seek(self, position: float) -> None:
        self.state.position = position
        self._changed()

    async def set_volume(self, volume: float) -> None:
        self.state.volume = volume
        self._changed()


class DeadSink(FakeSink):
    """Takes a track and then reports nothing playing, like Kodi refusing a URL."""

    async def play(self, target: StreamTarget, track: Track) -> None:
        await super().play(target, track)
        self.state.status = "stopped"
        self._changed()


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        subsonic_dir = root / "subsonic"
        kodi_dir = root / "kodi"
        subsonic_dir.mkdir()
        kodi_dir.mkdir()
        make_tones(subsonic_dir, 4)
        make_tones(kodi_dir, 4)

        subsonic = FakeLibrary("subsonic", subsonic_dir)
        kodi = FakeLibrary("kodi-box", kodi_dir)
        router = Router(subsonic, kodi)

        player = UnifiedPlayer(router, lambda event, data: None, {"volume": 0.0})
        local = MpvSink(0.0)
        await local.start()
        await player.set_sink(local)

        await player.enqueue(subsonic.tracks(4), mode="replace")
        await asyncio.sleep(1.0)
        state = player.state()
        check("replace starts playing the first track",
              state["status"] == "playing" and state["index"] == 0)
        check("queue holds four tracks", len(player.queue()["tracks"]) == 4)
        check("every entry has a stable uid",
              all(t["uid"] for t in player.queue()["tracks"]))

        await player.pause()
        await asyncio.sleep(0.2)
        check("pause", player.state()["status"] == "paused")
        await player.play()
        await asyncio.sleep(0.3)
        check("resume", player.state()["status"] == "playing")

        await player.next()
        await asyncio.sleep(0.8)
        check("next moves on", player.state()["index"] == 1)
        await player.seek(2.0)
        await asyncio.sleep(0.4)
        check("seek lands where asked", player.state()["position"] >= 1.8)
        await player.previous()
        await asyncio.sleep(0.5)
        check("previous restarts the current track when past 3s is false",
              player.state()["index"] == 0)

        await player.play_index(2)
        await asyncio.sleep(4.0)
        state = player.state()
        check("advances by itself at end of file", state["index"] == 3)
        check("the track it advanced to really plays",
              state["status"] == "playing" and not state["error"]
              and state["position"] > 0.1)

        await player.set_repeat("one")
        await player.play_index(0)
        await asyncio.sleep(4.0)
        state = player.state()
        check("repeat one stays put", state["index"] == 0)
        check("repeat one keeps the audio going",
              state["status"] == "playing" and not state["error"])
        await player.next()
        await asyncio.sleep(0.5)
        check("an explicit next escapes repeat one", player.state()["index"] == 1)
        await player.set_repeat("none")

        await player.enqueue(subsonic.tracks(4), mode="replace")
        await asyncio.sleep(0.6)
        await player.set_shuffle(True)
        visited = {player.state()["index"]}
        for _ in range(3):
            await player.next()
            await asyncio.sleep(0.4)
            visited.add(player.state()["index"])
        check("shuffle visits every track exactly once", visited == {0, 1, 2, 3})
        check("shuffle stops at the end without repeat",
              player.state()["canNext"] is False)
        await player.set_repeat("all")
        await player.next()
        await asyncio.sleep(0.4)
        check("repeat all wraps around", player.state()["index"] in {0, 1, 2, 3})
        await player.set_repeat("none")
        await player.set_shuffle(False)

        await player.enqueue(subsonic.tracks(4), mode="replace")
        await asyncio.sleep(0.6)
        await player.play_index(1)
        await asyncio.sleep(0.4)
        await player.move(3, 0)
        check("moving an entry shifts the playing index", player.state()["index"] == 2)
        check("the moved entry is now first",
              player.queue()["tracks"][0]["title"] == "subsonic 4")

        await player.remove([0])
        check("removing ahead of the current entry shifts it back",
              player.state()["index"] == 1)
        playing = player.state()["track"]["title"]
        await player.remove([1])
        await asyncio.sleep(0.7)
        state = player.state()
        check("removing what is playing moves on rather than stopping",
              state["track"]["title"] != playing and state["status"] == "playing")

        mixed = [subsonic.tracks(4)[0], kodi.tracks(4)[0],
                 subsonic.tracks(4)[1], kodi.tracks(4)[1]]
        await player.enqueue(mixed, mode="replace")
        await asyncio.sleep(1.0)
        sources = [t["source"] for t in player.queue()["tracks"]]
        check("the queue interleaves both services",
              sources == ["subsonic", "kodi-box", "subsonic", "kodi-box"])
        check("it starts on the first service's track",
              player.state()["track"]["source"] == "subsonic")
        await player.next()
        await asyncio.sleep(1.0)
        state = player.state()
        check("crossing into the other service keeps playing",
              state["track"]["source"] == "kodi-box"
              and state["status"] == "playing" and not state["error"])
        await player.next()
        await asyncio.sleep(1.0)
        check("and crosses back again",
              player.state()["track"]["source"] == "subsonic"
              and player.state()["status"] == "playing")

        await player.play_index(0)
        await asyncio.sleep(1.5)
        before = player.state()
        fake = FakeSink()
        await player.set_sink(fake)
        await asyncio.sleep(0.2)
        check("switching output keeps the same track",
              player.state()["track"]["uid"] == before["track"]["uid"])
        check("the new output was handed the track", len(fake.played) == 1)
        check("playback continues on the new output",
              player.state()["status"] == "playing")
        check("the reported output changed", player.state()["output"] == "fake")
        check("roughly the same position is restored",
              fake.state.position >= 1.0)

        await player.set_sink(local)
        await asyncio.sleep(0.5)
        check("switching back works", player.state()["output"] == "local")

        orphan = Track(id="1", title="Orphan", duration=3.0,
                       backend="fake", source="not-connected")
        await player.enqueue([orphan], mode="replace")
        await asyncio.sleep(0.6)
        state = player.state()
        check("a track from a disconnected service reports an error",
              bool(state["error"]) and state["status"] == "stopped")

        # Tracks from a disconnected service must be stepped over, not fatal.
        gone = [Track(id="1", title="Gone 1", duration=3.0, backend="fake",
                      source="switched-off"),
                Track(id="2", title="Gone 2", duration=3.0, backend="fake",
                      source="switched-off")]
        mixed = gone + [subsonic.tracks(4)[2]]
        await player.enqueue(mixed, mode="replace")
        await asyncio.sleep(1.5)
        state = player.state()
        check("skips tracks whose service is off and plays the next one",
              state["status"] == "playing"
              and state["track"]["source"] == "subsonic")

        # With nothing playable it must give up rather than loop for ever.
        await player.enqueue(gone, mode="replace")
        await asyncio.sleep(1.5)
        state = player.state()
        check("stops when the whole queue is unplayable",
              state["status"] == "stopped" and bool(state["error"]))
        check("and explains why",
              "queue" in (state["error"] or "").lower())

        await player.enqueue(subsonic.tracks(2), mode="replace")
        await asyncio.sleep(0.5)
        await player.set_volume(0.33)
        check("volume is applied", abs(player.state()["volume"] - 0.33) < 0.02)
        await player.clear()
        check("clear empties the queue and stops",
              player.queue()["tracks"] == []
              and player.state()["status"] == "stopped")

        check("a play was reported to the service it came from",
              any(sub for _, sub in subsonic.scrobbles))

        await player.set_sink(None, carry_over=False)
        await local.close()

        await test_foreign_tracks_are_skipped(subsonic, kodi, router)
        await test_output_that_never_starts(subsonic, router)
        await test_first_track_advances(subsonic, router)
        await test_gapless_handover(subsonic, router)
        await test_mpv_losing_its_socket()


async def test_foreign_tracks_are_skipped(subsonic, kodi, router) -> None:
    """An output bound to one service steps over the rest of the queue."""
    player = UnifiedPlayer(router, lambda event, data: None, {"volume": 0.0})
    await player.set_sink(FakeSink())
    await player.enqueue(subsonic.tracks(2) + [kodi.tracks(1)[0]], mode="replace")
    await asyncio.sleep(0.2)
    check("the permissive output starts on the first track",
          player.state()["track"]["source"] == "subsonic")

    theirs = FakeSink("kodi-box")
    await player.set_sink(theirs)
    await asyncio.sleep(0.2)
    state = player.state()
    check("switching to an output that cannot reach a service skips to one it can",
          state["status"] == "playing" and state["track"]["source"] == "kodi-box")
    check("and only its own track was handed over", theirs.played == ["kodi-box 1"])

    entries = player.queue()["tracks"]
    check("the queue marks the stranded entries unavailable",
          all(e.get("unavailable") for e in entries[:2])
          and not entries[2].get("unavailable"))
    check("naming the output that cannot play them",
          "Fake output" in entries[0]["unavailable"])
    await player.set_sink(None, carry_over=False)


async def test_output_that_never_starts(library, router) -> None:
    """A remote output can swallow a track silently; the queue must not park on it."""
    player = UnifiedPlayer(router, lambda event, data: None, {"volume": 0.0})
    sink = DeadSink()
    await player.set_sink(sink)
    saved, player_module.START_TIMEOUT = player_module.START_TIMEOUT, 0.3
    try:
        await player.enqueue(library.tracks(2), mode="replace")
        await asyncio.sleep(1.4)
    finally:
        player_module.START_TIMEOUT = saved
    check("a track the output never started is given up on",
          sink.played == ["subsonic 1", "subsonic 2"])
    check("and the queue ends up stopped rather than stuck",
          player.state()["status"] == "stopped")
    await player.set_sink(None, carry_over=False)


async def test_first_track_advances(library, router) -> None:
    """Loading into an idle mpv produces no end-file, so nothing may be waiting for one."""
    player = UnifiedPlayer(router, lambda event, data: None, {"volume": 0.0})
    sink = MpvSink(0.0)
    await sink.start()
    await player.set_sink(sink)
    await player.enqueue(library.tracks(2), mode="replace")
    await asyncio.sleep(3.8)
    check("the first track after startup advances at its end",
          player.state()["index"] == 1 and player.state()["status"] == "playing")
    await player.set_sink(None, carry_over=False)
    await sink.close()


async def test_gapless_handover(library, router) -> None:
    """mpv moves on from its own playlist, and a queue edit re-targets what it holds."""
    player = UnifiedPlayer(router, lambda event, data: None, {"volume": 0.0})
    sink = MpvSink(0.0)
    await sink.start()
    await player.set_sink(sink)
    tracks = library.tracks(4)
    await player.enqueue(tracks[:2], mode="replace")
    await asyncio.sleep(0.5)
    await player.enqueue([tracks[3]], mode="next")
    await asyncio.sleep(0.3)

    modes: list[str] = []
    loadfile = sink._mpv.loadfile

    async def spy(url: str, mode: str = "replace") -> int | None:
        modes.append(mode)
        return await loadfile(url, mode)
    sink._mpv.loadfile = spy

    await asyncio.sleep(3.0)
    state = player.state()
    check("the handover advances the queue", state["index"] == 1 and state["status"] == "playing")
    check("the track inserted as next is what plays",
          player.queue()["tracks"][1]["id"] == "4"
          and str(await sink._mpv.get_property("path")).endswith("/4.wav"))
    check("mpv moved on without the track being loaded afresh", "replace" not in modes)
    await player.set_sink(None, carry_over=False)
    await sink.close()


async def test_mpv_losing_its_socket() -> None:
    """mpv going away must not leave commands waiting out their timeout.

    systemd signals a whole control group, so mpv dies as the daemon does.
    """
    sink = MpvSink(0.0)
    await sink.start()
    mpv = sink._mpv
    check("mpv starts out alive", mpv.alive)

    # Close the socket while mpv is still running and not yet reaped.
    mpv._writer.close()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if not mpv.alive:
            break

    check("mpv counts as gone the moment its socket dies", not mpv.alive)

    started = time.monotonic()
    try:
        await mpv.set_property("pause", True)
        refused = False
    except MpvError:
        refused = True
    elapsed = time.monotonic() - started
    check("and a command is refused at once, not after the timeout",
          refused and elapsed < 1.0)

    started = time.monotonic()
    await sink.stop()
    check("so stopping the output returns straight away",
          time.monotonic() - started < 1.0)

    # It also has to be recoverable, with the next play starting a fresh mpv.
    await sink.start()
    check("mpv comes back after losing its socket", sink._mpv.alive)

    await sink.close()
    check("closing leaves nothing running", not sink._mpv.alive)


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print("  -", name)
        sys.exit(1)
    print("all checks passed")

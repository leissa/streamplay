"""End-to-end test of the control protocol with two services connected at once.

Run with ``python3 tests/test_protocol.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import json
import math
import pathlib
import socket
import struct
import sys
import tempfile
import wave
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import websockets

from streamplay import backends, hub as hub_module, secretstore
from streamplay.backends.base import Backend, Sink, StreamTarget
from streamplay.config import Config
from streamplay.hub import Hub
from streamplay.models import Album, Artist, Track
from streamplay.server import ControlServer

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_tone(path: pathlib.Path, freq: int, seconds: float = 2.0) -> None:
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"".join(
            struct.pack("<h", int(2000 * math.sin(2 * math.pi * freq * t / 8000)))
            for t in range(int(8000 * seconds))
        ))


class FakeBackend(Backend):
    """A two-album library backed by generated tones."""

    kind = "fake"
    audio_root: pathlib.Path = pathlib.Path(".")

    async def connect(self) -> None:
        if self.profile.get("failConnect"):
            from streamplay.backends.base import BackendError
            raise BackendError("deliberately unreachable")

    def _prefix(self) -> str:
        return self.profile.get("prefix", self.source)

    async def artists(self) -> list[Artist]:
        return [self.tag(Artist(id="a1", name=f"{self._prefix()} Artist"))]

    async def artist_albums(self, artist_id: str) -> list[Album]:
        return await self.albums()

    #: Out of order, and one undated, so the daemon's merge sort is what is tested.
    ALBUMS = (("al1", 2005), ("al2", 1990), ("al3", None))

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        return [
            self.tag(Album(id=album_id, name=f"{self._prefix()} {album_id}",
                           artist=f"{self._prefix()} Artist", year=year))
            for album_id, year in self.ALBUMS
        ]

    async def album_tracks(self, album_id: str) -> list[Track]:
        return [
            self.tag(Track(id=str(n), title=f"{self._prefix()} Track {n}",
                           artist=f"{self._prefix()} Artist",
                           album=f"{self._prefix()} Album",
                           duration=2.0, backend=self.kind))
            for n in (1, 2)
        ]

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        return {"artists": await self.artists(), "albums": await self.albums(),
                "tracks": await self.album_tracks("al1")}

    async def genres(self) -> list[str]:
        return ["Tone", self._prefix()]

    async def stream_target(self, track: Track) -> StreamTarget:
        path = self.audio_root / f"{self.source}-{track.id}.wav"
        return StreamTarget(url=path.as_uri(), source=self.source)


class FakeSink(Sink):
    def __init__(self, source: str, name: str) -> None:
        super().__init__()
        self.id, self.name, self.source = f"rooms:{source}:{name}", name, source
        self.started = self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def play(self, target, track) -> None: ...
    async def resume(self) -> None: ...
    async def pause(self) -> None: ...
    async def stop(self) -> None: ...
    async def seek(self, position: float) -> None: ...
    async def set_volume(self, volume: float) -> None: ...


class FakeRooms(Backend):
    """Outputs only, found and lost the way discovery would."""

    kind = "rooms"
    has_library = False

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        self.found = {"kitchen": FakeSink(self.source, "kitchen")}

    def find(self, *names: str) -> None:
        self.found = {n: self.found.get(n) or FakeSink(self.source, n) for n in names}
        self.sinks_changed()

    def sinks(self) -> list[Sink]:
        return list(self.found.values())

    async def connect(self) -> None: ...
    async def artists(self): return []
    async def artist_albums(self, artist_id): return []
    async def albums(self, sort="alphabetical", offset=0, limit=100): return []
    async def album_tracks(self, album_id): return []
    async def search(self, query, limit=40): return {}
    async def stream_target(self, track): raise AssertionError("no library")


async def test_outputs(hub: Hub) -> None:
    hub.config.upsert({"id": "rooms", "name": "Rooms", "type": "rooms", "enabled": True})
    await hub.connect_source("rooms")
    rooms = hub.sources["rooms"]
    kitchen = rooms.found["kitchen"]
    check("an output-only service offers its outputs",
          kitchen.started and hub.sinks.get(kitchen.id) is kitchen)
    check("an output-only service says it has no library",
          [s["hasLibrary"] for s in hub.sources_json() if s["id"] == "rooms"] == [False])
    check("browsing ignores a service without a library",
          len(await hub.gather(None, lambda b: b.albums())) == 6)

    await hub.set_output(kitchen.id)
    rooms.find("attic")
    await asyncio.sleep(0.1)
    attic = rooms.found["attic"]
    check("an output that goes away is closed and dropped",
          kitchen.closed and kitchen.id not in hub.sinks)
    check("playback falls back to this computer", hub.player.sink is hub.sinks["local"])
    check("an output that turns up is started and offered",
          attic.started and hub.sinks.get(attic.id) is attic)

    alpha, track = hub.sources["alpha"], Track(id="1", title="One", source="alpha")
    attic.web_streams_only = True
    check("an output for web streams takes a library that serves them",
          hub.unavailable(track, attic) is None)
    alpha.web_streams = False
    check("an output for web streams refuses local files",
          hub.unavailable(track, attic) == "attic cannot play music from Alpha")
    del alpha.web_streams

    hub.config.settings["output"] = "rooms:rooms:study"
    rooms.find("attic", "study")
    await asyncio.sleep(0.1)
    check("the saved output is picked up once it turns up",
          hub.player.sink is rooms.found["study"])
    check("an output still offered is left alone", not attic.closed)

    await hub.disconnect_source("rooms")
    check("disconnecting a service drops all its outputs",
          not any(s.source == "rooms" for s in hub.sinks.values()))


class Applet:
    """The client half of the protocol, as the QML applet speaks it."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.socket: Any = None
        self.events: list[tuple[str, dict]] = []
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "Applet":
        self.socket = await websockets.connect(self.url)
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._reader:
            self._reader.cancel()
        await self.socket.close()

    async def _read(self) -> None:
        async for raw in self.socket:
            message = json.loads(raw)
            if "id" in message:
                future = self._pending.pop(message["id"], None)
                if future and not future.done():
                    future.set_result(message)
            else:
                self.events.append((message.get("event"), message.get("data")))

    async def call(self, method: str, **params: Any) -> dict:
        self._next_id += 1
        call_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        await self.socket.send(json.dumps(
            {"id": call_id, "method": method, "params": params}))
        return await asyncio.wait_for(future, 20)

    def events_named(self, name: str) -> list[dict]:
        return [data for event, data in self.events if event == name]


async def main() -> None:
    backends.BACKEND_TYPES["fake"] = FakeBackend
    backends.BACKEND_TYPES["rooms"] = FakeRooms
    hub_module.SECRETS_RETRY = 0.05
    keyring_up = asyncio.Event()

    def load_all() -> dict:
        if not keyring_up.is_set():
            raise secretstore.SecretStoreError("not running yet")
        return {("alpha", "password"): "hunter2"}
    secretstore.load_all = load_all

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        FakeBackend.audio_root = root
        for source in ("alpha", "beta"):
            for n in (1, 2):
                write_tone(root / f"{source}-{n}.wav", 300 + n * 120)

        config = Config(root / "config.json")
        config.upsert({"id": "alpha", "name": "Alpha", "type": "fake",
                       "prefix": "Alpha", "enabled": True})
        config.upsert({"id": "beta", "name": "Beta", "type": "fake",
                       "prefix": "Beta", "enabled": True})
        config.settings["port"] = free_port()

        hub = Hub(config, enable_mpris=False)
        server = ControlServer(hub, port=config.settings["port"])
        serving = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.3)
        await hub.start()
        check("a missing keyring does not stop the services connecting",
              set(hub.sources) == {"alpha", "beta"})
        keyring_up.set()
        await asyncio.sleep(0.3)
        check("passwords arrive once the keyring appears",
              config.profiles["alpha"].get("password") == "hunter2")

        async with Applet(f"ws://127.0.0.1:{config.settings['port']}/") as applet:
            reply = await applet.call("hello")
            snapshot = reply["result"]
            check("hello succeeds", reply["ok"])
            states = {s["id"]: s["state"] for s in snapshot["sources"]}
            check("both services connect at once",
                  states == {"alpha": "connected", "beta": "connected"})
            check("the local output is offered",
                  any(o["id"] == "local" for o in snapshot["outputs"]))
            check("passwords never reach the applet",
                  all("password" not in p for p in snapshot["profiles"]))

            reply = await applet.call("library.artists")
            names = sorted(a["name"] for a in reply["result"]["artists"])
            check("artists from both services are merged",
                  names == ["Alpha Artist", "Beta Artist"])
            check("each artist says where it came from",
                  sorted(a["source"] for a in reply["result"]["artists"])
                  == ["alpha", "beta"])

            reply = await applet.call("library.albums", source="beta")
            albums = reply["result"]["albums"]
            check("a source filter narrows the results",
                  len(albums) == 3
                  and all(a["source"] == "beta" for a in albums))

            reply = await applet.call("library.albums", sort="byYear")
            years = [a.get("year") for a in reply["result"]["albums"]]
            check("byYear sorts oldest first, undated last",
                  years == [1990, 1990, 2005, 2005, None, None])

            reply = await applet.call("library.albums", sort="byYearDesc")
            years = [a.get("year") for a in reply["result"]["albums"]]
            check("byYearDesc sorts newest first, undated last",
                  years == [2005, 2005, 1990, 1990, None, None])

            # An artist's albums must follow the same setting, or the sort only seems to work.
            reply = await applet.call("library.artistAlbums", id="a1",
                                      source="alpha", sort="byYearDesc")
            years = [a.get("year") for a in reply["result"]["albums"]]
            check("artist albums honour byYearDesc", years == [2005, 1990, None])

            reply = await applet.call("library.artistAlbums", id="a1",
                                      source="alpha", sort="byYear")
            years = [a.get("year") for a in reply["result"]["albums"]]
            check("artist albums honour byYear", years == [1990, 2005, None])

            reply = await applet.call("library.artistAlbums", id="a1",
                                      source="alpha")
            years = [a.get("year") for a in reply["result"]["albums"]]
            check("artist albums default to year order",
                  years == [1990, 2005, None])

            reply = await applet.call("library.genres")
            check("genres are merged and de-duplicated",
                  reply["result"]["genres"] == ["Alpha", "Beta", "Tone"])

            reply = await applet.call("library.search", query="track")
            check("search spans every service",
                  len(reply["result"]["tracks"]) == 4)

            await applet.call("queue.add", albumId="al1", source="alpha",
                              mode="replace", play=True)
            await applet.call("queue.add", albumId="al1", source="beta",
                              mode="append")
            await asyncio.sleep(1.0)

            reply = await applet.call("queue.get")
            queue = reply["result"]
            check("the queue holds tracks from both services",
                  [t["source"] for t in queue["tracks"]]
                  == ["alpha", "alpha", "beta", "beta"])

            reply = await applet.call("hello")
            state = reply["result"]["state"]
            check("playback started on the first track",
                  state["status"] == "playing" and state["index"] == 0)

            await applet.call("queue.move", **{"from": 3, "to": 0})
            reply = await applet.call("queue.get")
            check("reordering works across services",
                  reply["result"]["tracks"][0]["source"] == "beta")
            check("the playing track moved with it",
                  reply["result"]["index"] == 1)

            await applet.call("queue.playIndex", index=0)
            await asyncio.sleep(1.0)
            reply = await applet.call("hello")
            check("jumping to another service's track plays it",
                  reply["result"]["state"]["track"]["source"] == "beta"
                  and reply["result"]["state"]["status"] == "playing"
                  and not reply["result"]["state"]["error"])

            await applet.call("queue.remove", indexes=[0])
            reply = await applet.call("queue.get")
            check("dequeue removes one entry", len(reply["result"]["tracks"]) == 3)

            await applet.call("player.pause")
            await asyncio.sleep(0.3)
            reply = await applet.call("hello")
            check("pause over the wire", reply["result"]["state"]["status"] == "paused")

            await applet.call("player.setVolume", volume=0.25)
            await applet.call("player.setRepeat", mode="all")
            await applet.call("player.setShuffle", shuffle=True)
            reply = await applet.call("hello")
            state = reply["result"]["state"]
            check("volume, repeat and shuffle round-trip",
                  abs(state["volume"] - 0.25) < 0.02
                  and state["repeat"] == "all" and state["shuffle"] is True)

            reply = await applet.call("sources.disconnect", id="beta")
            states = {s["id"]: s["state"] for s in reply["result"]["sources"]}
            check("a service can be disconnected on its own",
                  states["beta"] == "disconnected" and states["alpha"] == "connected")

            reply = await applet.call("library.artists")
            check("browsing then only covers what is left",
                  len(reply["result"]["artists"]) == 1)

            reply = await applet.call("sources.connect", id="beta")
            states = {s["id"]: s["state"] for s in reply["result"]["sources"]}
            check("and can be brought back", states["beta"] == "connected")

            reply = await applet.call("profiles.save", profile={
                "name": "Broken", "type": "fake", "failConnect": True})
            check("a server that will not answer is still saved",
                  reply["ok"] and "connectError" in reply["result"])

            reply = await applet.call("profiles.delete", id="broken")
            check("and can be deleted again", reply["result"]["removed"])

            reply = await applet.call("player.seek", position="not a number")
            check("a bad argument comes back as an error, not a crash",
                  not reply["ok"] and "error" in reply)

            reply = await applet.call("library.albumTracks", id="al1")
            check("an ambiguous request without a source is refused",
                  not reply["ok"])

            check("state changes were pushed, not just polled",
                  len(applet.events_named("state")) > 0)
            check("queue changes were pushed too",
                  len(applet.events_named("queue")) > 0)

        await test_outputs(hub)
        await hub.close()
        serving.cancel()
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print("  -", name)
        sys.exit(1)
    print("all checks passed")

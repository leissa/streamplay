"""Drives the Kodi output with scripted notifications against a stubbed JSON-RPC,
so no Kodi is needed. Run with ``python3 tests/test_kodi.py`` from ``daemon``.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streamplay.backends.base import StreamTarget
from streamplay.backends.kodi import KodiSink
from streamplay.models import Track

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


class FakeKodi:
    source = "kodi-test"
    name = "Test Kodi"
    host = "127.0.0.1"
    ws_port = 0

    def __init__(self) -> None:
        self.playing = False

    async def call(self, method: str, params: dict | None = None):
        if method == "Player.Open":
            self.playing = True
        elif method == "Player.Stop":
            self.playing = False
        elif method == "Player.GetActivePlayers":
            return [{"type": "audio", "playerid": 0}] if self.playing else []
        elif method == "Player.GetProperties":
            return {"speed": 1, "time": {"seconds": 1}, "totaltime": {"seconds": 3}}
        return {}


def notification(method: str, **data) -> dict:
    return {"method": method, "params": {"data": data}}


async def test_sink() -> None:
    kodi = FakeKodi()
    sink = KodiSink(kodi)
    ended: list[str] = []

    async def on_ended(reason: str) -> None:
        ended.append(reason)

    sink.wire(on_ended, lambda: None)
    target = StreamTarget(url="http://music/1.flac", source="subsonic")
    track = Track(id="1", title="One", duration=3.0)

    async def play() -> None:
        await sink.play(target, track)
        await sink._on_notification(notification("Player.OnPlay"))

    async def finish() -> None:
        kodi.playing = False
        await sink._on_notification(notification("Player.OnStop", end=True))

    await play()
    await finish()
    check("a track played on an idle Kodi reports eof", ended == ["eof"])

    await play()
    await finish()
    check("so does the next one", ended == ["eof", "eof"])

    await play()
    await sink.play(target, track)
    await sink._on_notification(notification("Player.OnStop", end=False))
    await sink._on_notification(notification("Player.OnPlay"))
    check("replacing a playing track is not eof", ended == ["eof", "eof"])
    check("and leaves the output playing", sink.state.status == "playing")

    await finish()
    check("the replacement still reports eof", ended == ["eof"] * 3)

    await play()
    await sink.stop()
    await sink._on_notification(notification("Player.OnStop", end=False))
    check("our own stop is not eof", ended == ["eof"] * 3)

    await play()
    kodi.playing = False
    await sink._on_notification(notification("Player.OnStop", end=False))
    check("a stop on Kodi itself is not eof", ended == ["eof"] * 3)
    check("but stops the output", sink.state.status == "stopped")

    await play()
    await finish()
    check("playing after a stop still reports eof", ended == ["eof"] * 4)


async def main() -> None:
    await test_sink()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for label in FAILURES:
            print("  - " + label)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    asyncio.run(main())

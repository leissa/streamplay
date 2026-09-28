"""The local audio sink: mpv playing whatever URL it is handed."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .backends.base import BackendError, Sink, StreamTarget
from .models import Track
from .mpvproc import Mpv, MpvError

log = logging.getLogger(__name__)


class MpvSink(Sink):
    id = "local"
    name = "This computer"

    def __init__(self, initial_volume: float = 0.7) -> None:
        super().__init__()
        self.state.volume = initial_volume
        self._mpv = Mpv(self._on_event, self._on_property)
        #: The entry we loaded; an end-file for any other is stale.
        self._entry: int | None = None
        self._entry_pending = False
        #: ``(uid, entry)`` appended behind the current track, so mpv moves on gaplessly.
        #: Keyed by queue uid because Subsonic salts every stream URL afresh.
        self._next: tuple[str, int] | None = None
        #: The queue uid mpv already moved on to, which the next ``play`` adopts.
        self._handed_over: str | None = None
        self._preload_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._mpv.start()
        await self._mpv.set_property("volume", round(self.state.volume * 100))

    async def close(self) -> None:
        await self._mpv.close()


    async def _on_event(self, name: str, payload: dict) -> None:
        if name == "start-file":
            if self._entry_pending:
                self._entry = payload.get("playlist_entry_id")
                self._entry_pending = False
        elif name == "end-file":
            if self._entry is None or payload.get("playlist_entry_id") != self._entry:
                return
            self._entry = None
            reason = payload.get("reason")
            if reason == "eof" and self._next is not None:
                (self._handed_over, self._entry), self._next = self._next, None
                await self._ended("eof")
            elif reason == "eof":
                self.state.status = "stopped"
                await self._ended("eof")
            elif reason == "error":
                self.state.status = "stopped"
                self.state.error = "Playback failed"
                await self._ended("error")
        elif name == "file-loaded":
            self.state.error = None
            self._changed()
        elif name == "ipc-closed":
            self.state.status = "stopped"
            self.state.error = "The audio engine stopped unexpectedly"
            self._changed()

    async def _on_property(self, name: str, value: Any) -> None:
        if name == "time-pos" and value is not None:
            self.state.position = float(value)
            self._changed()
        elif name == "duration" and value:
            self.state.duration = float(value)
            self._changed()
        elif name == "pause" and value is not None and self.state.status != "stopped":
            self.state.status = "paused" if value else "playing"
            self._changed()
        elif name == "volume" and value is not None:
            volume = max(0.0, min(1.0, float(value) / 100.0))
            if abs(volume - self.state.volume) > 0.001:
                self.state.volume = volume
                self._changed()
        elif name == "cache-buffering-state":
            buffering = value is not None and float(value) < 100
            if buffering != self.state.buffering:
                self.state.buffering = buffering
                self._changed()


    async def _ensure_running(self) -> None:
        if not self._mpv.alive:
            await self._mpv.start()
            await self._mpv.set_property("volume", round(self.state.volume * 100))

    async def play(self, target: StreamTarget, track: Track, start: float = 0.0) -> None:
        if not target.url:
            raise BackendError(f"Cannot play {track.title} on this computer")
        await self._ensure_running()

        handed_over, self._handed_over = self._handed_over, None
        self.state.status = "playing"
        self.state.position = start
        self.state.duration = track.duration
        self.state.error = None
        if track.uid and handed_over == track.uid and self._entry is not None:
            self._changed()
            return

        self._entry, self._entry_pending = None, False
        self._next = None
        try:
            await self._mpv.set_property("pause", False)
            entry = await self._mpv.loadfile(target.url, "replace", start=start)
        except MpvError as exc:
            self.state.status = "stopped"
            self.state.error = str(exc)
            self._changed()
            raise BackendError(str(exc)) from exc
        self._entry, self._entry_pending = entry, entry is None
        self._changed()

    async def resume(self) -> None:
        await self._ensure_running()
        await self._mpv.set_property("pause", False)
        self.state.status = "playing"
        self._changed()

    async def pause(self) -> None:
        if self.state.status == "stopped":
            return
        await self._mpv.set_property("pause", True)
        self.state.status = "paused"
        self._changed()

    async def stop(self) -> None:
        self._entry, self._entry_pending = None, False
        self._next, self._handed_over = None, None
        self.state.status = "stopped"
        self.state.position = 0.0
        self.state.buffering = False
        try:
            await self._mpv.stop()
        except MpvError:
            pass
        self._changed()

    async def seek(self, position: float) -> None:
        try:
            await self._mpv.command("seek", max(0.0, position), "absolute")
        except MpvError as exc:
            log.debug("seek failed: %s", exc)
            return
        self.state.position = max(0.0, position)
        self._changed()

    async def set_volume(self, volume: float) -> None:
        self.state.volume = max(0.0, min(1.0, float(volume)))
        try:
            await self._mpv.set_property("volume", round(self.state.volume * 100))
        except MpvError:
            pass
        self._changed()

    async def preload(self, target: StreamTarget | None, track: Track | None) -> None:
        url = target.url if target and track and track.uid else None
        async with self._preload_lock:
            if self._next is not None and url and self._next[0] == track.uid:
                return
            # Appending to an idle mpv would start playing it.
            if url and self._entry is None:
                return
            try:
                if self._next is not None:
                    self._next = None
                    await self._mpv.command("playlist-clear")
                if url:
                    entry = await self._mpv.loadfile(url, "append")
                    if entry is not None:
                        self._next = (track.uid, entry)
            except MpvError as exc:
                log.debug("preload failed: %s", exc)

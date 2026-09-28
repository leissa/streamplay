"""The one queue, holding tracks from any service and playing through any sink.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from typing import Any, Callable, Iterable, Sequence

from .backends.base import (REPEAT_MODES, BackendError, Sink,
                            SourceUnavailable)
from .models import Track

log = logging.getLogger(__name__)

#: Give up on a run of unplayable tracks instead of spinning through the queue.
MAX_CONSECUTIVE_ERRORS = 5

#: A remote output that never starts a track would otherwise park the queue on it.
START_TIMEOUT = 10.0

_uid_counter = itertools.count(1)


class UnifiedPlayer:
    """Queue, play order and transport, independent of source and destination.

    ``resolver`` supplies ``stream_target(track)``, ``scrobble(track, submission)``
    and ``unavailable(track, sink)``.
    """

    def __init__(self, resolver, emit: Callable[[str, dict], None],
                 settings: dict[str, Any] | None = None) -> None:
        self._resolver = resolver
        self._emit = emit
        settings = settings or {}

        self._tracks: list[Track] = []
        self._index: int = -1
        self._status: str = "stopped"
        self._shuffle: bool = bool(settings.get("shuffle", False))
        self._repeat: str = str(settings.get("repeat", "none"))
        self._order: list[int] = []
        self._error_streak = 0
        #: Consecutive tracks skipped because their service is switched off.
        self._skipped = 0
        self._last_error: str | None = None
        self._scrobbled = False
        self._started_at = 0.0
        self._last_position_emit = 0.0

        self._sink: Sink | None = None
        self._pending_volume: float = float(settings.get("volume", 0.7))
        self._last_state: dict[str, Any] | None = None
        #: Bumped on every change to what comes next, so a slow resolve cannot win.
        self._preload_generation = 0
        #: Bumped on every load, so a start watchdog knows it is stale.
        self._load_token = 0


    @property
    def sink(self) -> Sink | None:
        return self._sink

    async def set_sink(self, sink: Sink | None, carry_over: bool = True) -> None:
        """Switch output, optionally picking up where the old one left off."""
        previous, self._sink = self._sink, sink
        was_playing = self._status == "playing"
        position = previous.state.position if previous else 0.0
        volume = previous.state.volume if previous else self._pending_volume

        if previous is not None:
            previous.wire(None, None)
            try:
                await previous.stop()
            except Exception:
                log.debug("could not stop the previous output", exc_info=True)

        if sink is None:
            self._status = "stopped"
            self._changed()
            return

        sink.wire(self._on_sink_ended, self._on_sink_changed)
        self._pending_volume = volume
        try:
            await sink.set_volume(volume)
        except Exception:
            log.debug("could not carry the volume over", exc_info=True)

        if carry_over and was_playing and self.current is not None:
            await self._load(self._index, start=position if position > 1.0 else 0.0)
        else:
            self._status = "stopped"
            self._changed()

    def _require_sink(self) -> Sink:
        if self._sink is None:
            raise BackendError("No playback output is available")
        return self._sink


    @property
    def current(self) -> Track | None:
        if 0 <= self._index < len(self._tracks):
            return self._tracks[self._index]
        return None

    def state(self) -> dict[str, Any]:
        track = self.current
        sink_state = self._sink.state if self._sink else None
        position = sink_state.position if sink_state else 0.0
        duration = (sink_state.duration if sink_state and sink_state.duration
                    else (track.duration if track else 0.0))
        return {
            "status": self._status,
            "track": track.to_json() if track else None,
            "position": round(position, 3),
            "duration": round(duration, 3),
            "volume": round(sink_state.volume if sink_state
                            else self._pending_volume, 4),
            "shuffle": self._shuffle,
            "repeat": self._repeat,
            "index": self._index,
            "queueLength": len(self._tracks),
            "buffering": bool(sink_state.buffering) if sink_state else False,
            "error": self._last_error or (sink_state.error if sink_state else None),
            "output": self._sink.id if self._sink else None,
            "outputName": self._sink.name if self._sink else None,
            "canNext": self._peek(+1) is not None,
            "canPrevious": self._peek(-1) is not None or position > 3,
            "capabilities": self._sink.capabilities() if self._sink else {},
        }

    def queue(self) -> dict[str, Any]:
        tracks = []
        for track in self._tracks:
            entry = track.to_json()
            reason = self._unavailable(track)
            if reason is not None:
                entry["unavailable"] = reason
            tracks.append(entry)
        return {"tracks": tracks, "index": self._index}

    def _unavailable(self, track: Track) -> str | None:
        return self._resolver.unavailable(track, self._sink)

    def _changed(self) -> None:
        state = self.state()
        self._last_state = state
        self._emit("state", state)

    def _on_sink_changed(self) -> None:
        """Sink callback; plain progress goes out as a smaller rate-limited event.
        """
        state = self.state()
        previous, self._last_state = self._last_state, state

        if previous is not None and previous.keys() == state.keys() and all(
            previous[key] == state[key]
            for key in state if key not in ("position", "duration")
        ):
            now = time.monotonic()
            if now - self._last_position_emit < 0.9:
                return
            self._last_position_emit = now
            self._emit("position", {"position": state["position"],
                                    "duration": state["duration"]})
        else:
            self._emit("state", state)

        if self._status == "playing":
            self._check_scrobble()

    def _queue_changed(self) -> None:
        self._emit("queue", self.queue())
        self._changed()
        self._schedule_preload()

    def _schedule_preload(self) -> None:
        self._preload_generation += 1
        asyncio.create_task(self._preload(self._preload_generation))

    async def _preload(self, generation: int) -> None:
        sink = self._sink
        if sink is None:
            return
        index = self._peek(+1) if self._status != "stopped" else None
        track = self._tracks[index] if index is not None else None
        target = None
        if track is not None and self._unavailable(track) is not None:
            track = None
        if track is not None:
            try:
                target = await self._resolver.stream_target(track)
            except BackendError as exc:
                log.debug("cannot preload %s: %s", track.title, exc)
                track = None
        if generation != self._preload_generation or sink is not self._sink:
            return
        try:
            await sink.preload(target, track)
        except Exception:
            log.debug("preload failed", exc_info=True)


    def _rebuild_order(self) -> None:
        """Recompute the shuffle walk, keeping the current track at its head."""
        if not self._shuffle:
            self._order = []
            return
        rest = [i for i in range(len(self._tracks)) if i != self._index]
        random.shuffle(rest)
        self._order = ([self._index] if self._index >= 0 else []) + rest

    def _peek(self, direction: int) -> int | None:
        """Index of the next/previous track, honouring shuffle and repeat."""
        if not self._tracks:
            return None
        if self._repeat == "one":
            return self._index if self._index >= 0 else 0

        if self._shuffle:
            if not self._order:
                self._rebuild_order()
            try:
                pos = self._order.index(self._index)
            except ValueError:
                return self._order[0] if self._order else None
            target = pos + direction
            if 0 <= target < len(self._order):
                return self._order[target]
            if self._repeat == "all":
                return self._order[target % len(self._order)]
            return None

        target = self._index + direction
        if 0 <= target < len(self._tracks):
            return target
        if self._repeat == "all" and self._tracks:
            return target % len(self._tracks)
        return None


    async def _load(self, index: int, start: float = 0.0) -> None:
        if not (0 <= index < len(self._tracks)):
            await self.stop()
            return
        sink = self._require_sink()
        track = self._tracks[index]

        reason = self._unavailable(track)
        if reason is not None:
            self._index = index
            await self._step_over(reason)
            return

        self._index = index
        self._load_token += 1
        self._scrobbled = False
        self._started_at = time.monotonic()
        self._last_error = None
        self._status = "playing"
        self._changed()

        try:
            target = await self._resolver.stream_target(track)
            await sink.play(target, track, start)
        except SourceUnavailable as exc:
            await self._step_over(str(exc))
            return
        except BackendError as exc:
            log.warning("cannot play %s: %s", track.title, exc)
            self._last_error = str(exc)
            self._error_streak += 1
            if self._error_streak >= MAX_CONSECUTIVE_ERRORS:
                self._error_streak = 0
                await self.stop()
            else:
                await self._advance(+1)
            return
        except Exception as exc:
            log.exception("unexpected failure playing %s", track.title)
            self._last_error = str(exc)
            await self.stop()
            return

        self._error_streak = 0
        self._skipped = 0
        self._changed()
        self._schedule_preload()
        asyncio.create_task(self._watch_start(self._load_token))
        asyncio.create_task(self._scrobble(track, submission=False))

    async def _step_over(self, reason: str) -> None:
        """Skip an entry nothing can play, giving up once the whole queue has been tried.
        """
        self._skipped += 1
        if self._skipped >= max(1, len(self._tracks)):
            self._skipped = 0
            self._last_error = f"Nothing in the queue can be played: {reason}"
            await self.stop()
        else:
            self._last_error = reason
            await self._advance(+1)

    async def _watch_start(self, token: int) -> None:
        """An output that swallowed a track without a word must not park the queue.
        """
        await asyncio.sleep(START_TIMEOUT)
        sink = self._sink
        if token != self._load_token or sink is None or self._status != "playing":
            return
        if sink.state.status == "stopped" and not sink.state.buffering:
            log.info("%s never started on %s",
                     self.current.title if self.current else "the track", sink.name)
            await self._on_sink_ended("error")

    async def _scrobble(self, track: Track, submission: bool) -> None:
        try:
            await self._resolver.scrobble(track, submission)
        except Exception as exc:
            log.debug("scrobble failed: %s", exc)

    async def _advance(self, direction: int = 1, user: bool = False) -> None:
        nxt = self._peek(direction)
        # Repeat-one should still let an explicit Next move on.
        if user and self._repeat == "one":
            saved, self._repeat = self._repeat, "none"
            try:
                nxt = self._peek(direction)
            finally:
                self._repeat = saved
        if nxt is None:
            await self.stop()
            return
        await self._load(nxt)

    async def _on_sink_ended(self, reason: str) -> None:
        if reason == "eof":
            self._error_streak = 0
            await self._advance(+1)
        elif reason == "error":
            self._error_streak += 1
            track = self.current
            self._last_error = (f"Could not play {track.title}" if track
                                else "Playback error")
            if self._error_streak >= MAX_CONSECUTIVE_ERRORS:
                self._error_streak = 0
                await self.stop()
            else:
                await self._advance(+1)

    def _check_scrobble(self) -> None:
        """Submit a play once half the track (or four minutes) has gone by."""
        if self._scrobbled or self.current is None or self._sink is None:
            return
        played = time.monotonic() - self._started_at
        duration = self._sink.state.duration or self.current.duration or 0
        if played >= 240 or (duration and self._sink.state.position >= duration / 2):
            self._scrobbled = True
            asyncio.create_task(self._scrobble(self.current, submission=True))


    async def play(self) -> None:
        if self.current is None:
            if self._tracks:
                await self._load(self._index if self._index >= 0 else 0)
            return
        if self._status == "stopped":
            await self._load(self._index)
            return
        await self._require_sink().resume()
        self._status = "playing"
        self._changed()

    async def pause(self) -> None:
        if self.current is None or self._status == "stopped":
            return
        await self._require_sink().pause()
        self._status = "paused"
        self._changed()

    async def play_pause(self) -> None:
        if self._status == "playing":
            await self.pause()
        else:
            await self.play()

    async def stop(self) -> None:
        self._status = "stopped"
        self._preload_generation += 1
        if self._sink is not None:
            await self._sink.stop()
        self._changed()

    async def next(self) -> None:
        self._skipped = 0
        await self._advance(+1, user=True)

    async def previous(self) -> None:
        # Match every other music player: restart the track first.
        position = self._sink.state.position if self._sink else 0.0
        if position > 3.0 and self.current is not None:
            await self.seek(0.0)
            return
        await self._advance(-1, user=True)

    async def seek(self, position: float) -> None:
        if self.current is None:
            return
        await self._require_sink().seek(max(0.0, position))
        self._emit("seeked", {"position": round(max(0.0, position), 3)})
        self._changed()

    async def seek_relative(self, offset: float) -> None:
        if self._sink is None:
            return
        target = max(0.0, self._sink.state.position + offset)
        duration = self._sink.state.duration
        if duration and target >= duration:
            await self.next()
            return
        await self.seek(target)

    async def set_volume(self, volume: float) -> None:
        self._pending_volume = max(0.0, min(1.0, float(volume)))
        if self._sink is not None:
            await self._sink.set_volume(self._pending_volume)
        self._changed()

    async def set_shuffle(self, shuffle: bool) -> None:
        self._shuffle = bool(shuffle)
        self._rebuild_order()
        self._changed()
        self._schedule_preload()

    async def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_MODES:
            raise ValueError(f"unknown repeat mode {mode!r}")
        self._repeat = mode
        self._changed()
        self._schedule_preload()


    @staticmethod
    def _stamp(tracks: Sequence[Track]) -> list[Track]:
        out = []
        for track in tracks:
            copy = track.copy()
            copy.uid = f"t{next(_uid_counter)}"
            out.append(copy)
        return out

    async def enqueue(self, tracks: Sequence[Track], mode: str = "append",
                      start: bool = False) -> None:
        if not tracks:
            return
        fresh = self._stamp(tracks)

        if mode == "replace":
            self._tracks = fresh
            self._index = -1
            self._rebuild_order()
            self._queue_changed()
            await self._load(0)
            return

        if mode == "next" and self._index >= 0:
            at = self._index + 1
            self._tracks[at:at] = fresh
        else:
            at = len(self._tracks)
            self._tracks.extend(fresh)

        self._rebuild_order()
        self._queue_changed()

        if start or (self.current is None and self._status == "stopped"):
            await self._load(at)

    async def remove(self, indexes: Iterable[int]) -> None:
        drop = sorted({i for i in indexes if 0 <= i < len(self._tracks)}, reverse=True)
        if not drop:
            return
        removing_current = self._index in drop
        for i in drop:
            del self._tracks[i]
            if i < self._index:
                self._index -= 1

        if removing_current:
            # Keep playing from where the removed track used to be.
            target = min(self._index, len(self._tracks) - 1)
            was_playing = self._status != "stopped"
            self._index = -1
            self._rebuild_order()
            self._queue_changed()
            if target >= 0 and was_playing:
                await self._load(target)
            else:
                await self.stop()
            return

        self._rebuild_order()
        self._queue_changed()

    async def move(self, source: int, target: int) -> None:
        if not (0 <= source < len(self._tracks)):
            return
        target = max(0, min(target, len(self._tracks) - 1))
        if source == target:
            return
        track = self._tracks.pop(source)
        self._tracks.insert(target, track)

        if self._index == source:
            self._index = target
        elif source < self._index <= target:
            self._index -= 1
        elif target <= self._index < source:
            self._index += 1

        self._rebuild_order()
        self._queue_changed()

    async def clear(self) -> None:
        self._tracks = []
        self._index = -1
        self._order = []
        await self.stop()
        self._queue_changed()

    async def play_index(self, index: int) -> None:
        if 0 <= index < len(self._tracks):
            self._skipped = 0
            await self._load(index)

"""Interfaces for the two kinds of thing the daemon plugs together: a *backend* is
a library you browse, a *sink* is somewhere audio comes out.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator

from ..models import Album, Artist, Track

log = logging.getLogger(__name__)

REPEAT_MODES = ("none", "all", "one")


class BackendError(RuntimeError):
    """Raised for anything the user should see as a connection/API failure."""


class SourceUnavailable(BackendError):
    """The service a track came from is not connected, so the player skips it.
    """


@dataclass
class StreamTarget:
    """How to play one track.

    ``url`` is openable by any player.
    ``native`` is a backend-specific handle its own sink can use instead.
    """

    url: str | None = None
    native: dict[str, Any] | None = None
    source: str = ""


class Backend(abc.ABC):
    """Read-only access to a music library, and whatever outputs the service offers."""

    #: ``subsonic`` / ``jellyfin`` / ``kodi`` / ``mpd`` / ...; also stamped onto every Track.
    kind: str = ""
    #: False for a service that is only somewhere to play, whose library calls return nothing.
    has_library: bool = True
    #: Whether its stream URLs are web streams any output can open, which MPD's ``file://`` are not.
    web_streams: bool = True

    def __init__(self, profile: dict[str, Any]) -> None:
        self.profile = profile
        self.source = str(profile.get("id") or self.kind)
        self.name = profile.get("name") or profile.get("id") or self.kind
        self._on_sinks_changed: Callable[[], None] | None = None

    @abc.abstractmethod
    async def connect(self) -> None:
        """Verify the configuration works. Raise BackendError otherwise."""

    async def close(self) -> None:
        return None

    def sinks(self) -> list[Sink]:
        """The outputs this service offers right now, each keeping its id across calls."""
        return []

    def watch_sinks(self, callback: Callable[[], None] | None) -> None:
        self._on_sinks_changed = callback

    def sinks_changed(self) -> None:
        """Tell the hub to call :meth:`sinks` again."""
        if self._on_sinks_changed is not None:
            self._on_sinks_changed()

    async def artists(self) -> list[Artist]:
        return []

    async def artist_albums(self, artist_id: str) -> list[Album]:
        return []

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        return []

    async def album_tracks(self, album_id: str) -> list[Track]:
        return []

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        return {"artists": [], "albums": [], "tracks": []}

    async def genres(self) -> list[str]:
        return []

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        return []

    async def playlists(self) -> list[dict[str, Any]]:
        """Stored playlists. Each carries its ``source``, like every other
        library item, because ids are only unique within one service."""
        return []

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        return []


    async def stream_target(self, track: Track) -> StreamTarget:
        """Work out how the given track can actually be played."""
        raise BackendError(f"{self.name} has no music of its own")

    def cover_request(self, cover_id: str, size: int) -> tuple[str, dict, dict] | None:
        """``(url, params, headers)`` to fetch cover art, or None if unsupported."""
        return None

    async def cover_bytes(self, cover_id: str, size: int) -> bytes | None:
        """Cover art the backend fetches itself, tried before :meth:`cover_request`.
        """
        return None

    async def scrobble(self, track: Track, submission: bool) -> None:
        return None


    def tag(self, item):
        """Stamp an item with the profile it came from and hand it back."""
        item.source = self.source
        return item


@dataclass
class SinkState:
    """What a sink reports back about the one track it is playing."""

    status: str = "stopped"          # playing / paused / stopped
    position: float = 0.0
    duration: float = 0.0
    volume: float = 1.0
    buffering: bool = False
    error: str | None = None


class Sink(abc.ABC):
    """Somewhere audio comes out; it plays one target and knows nothing of queues.
    """

    #: Identifier used on the wire, e.g. ``local`` or ``kodi:livingroom``.
    id: str = "sink"
    name: str = "Sink"
    #: Profile id this output belongs to, so the hub can drop both together.
    source: str = ""
    #: Plays other services' tracks only as web streams, so not MPD's.
    web_streams_only: bool = False

    def __init__(self) -> None:
        self.state = SinkState()
        self._on_ended: Callable[[str], Awaitable[None]] | None = None
        self._on_changed: Callable[[], None] | None = None

    def wire(self, on_ended: Callable[[str], Awaitable[None]],
             on_changed: Callable[[], None]) -> None:
        """``on_ended(reason)`` for eof/error, ``on_changed()`` for state."""
        self._on_ended = on_ended
        self._on_changed = on_changed

    async def _ended(self, reason: str) -> None:
        if self._on_ended is not None:
            await self._on_ended(reason)

    def _changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @abc.abstractmethod
    async def play(self, target: StreamTarget, track: Track) -> None: ...

    @abc.abstractmethod
    async def resume(self) -> None: ...

    @abc.abstractmethod
    async def pause(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def seek(self, position: float) -> None: ...

    @abc.abstractmethod
    async def set_volume(self, volume: float) -> None: ...

    async def preload(self, target: StreamTarget | None, track: Track | None) -> None:
        """A hint of what plays after the current track, None if nothing does."""
        return None

    def plays(self, track: Track) -> bool:
        """Whether this output can play a track from that service at all."""
        return True

    def capabilities(self) -> dict[str, bool]:
        return {"seek": True, "volume": True}


class PolledSink(Sink):
    """An output polled for its state, whose service reports a bare stop for eof and a user's stop alike.
    """

    POLL_INTERVAL = 1.0
    #: How far short of the end a stop still counts as eof.
    EOF_SLACK = 1.0

    def __init__(self) -> None:
        super().__init__()
        #: Set while a play/stop is halfway through, when the service passes through stop.
        self._changing = False
        #: When :attr:`state.position` was last read; see :meth:`_near_end`.
        self._position_at = 0.0
        self._poll_task: asyncio.Task | None = None

    def _start_polling(self) -> None:
        self._poll_task = asyncio.create_task(self._poll_loop(), name=f"poll-{self.id}")

    async def close(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    def _poll_interval(self) -> float:
        return self.POLL_INTERVAL

    def _should_poll(self) -> bool:
        return self.state.status == "playing"

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval())
                if self._should_poll():
                    await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s poll: %s", self.name, exc)

    @abc.abstractmethod
    async def _sync(self) -> None:
        """Read the service's state into :attr:`state`."""

    @contextlib.contextmanager
    def _transition(self) -> Iterator[None]:
        self._changing = True
        try:
            yield
        finally:
            self._changing = False

    def _note_position(self, position: float) -> None:
        """Record where playback is, and when we learned it."""
        self.state.position = position
        self._position_at = time.monotonic()

    def _near_end(self) -> bool:
        """Was the track about to finish when it stopped?

        Position is the only evidence, carried forward because a short track can start and end between polls.
        """
        if self.state.duration <= 0:
            return True
        position = self.state.position
        if self.state.status == "playing":
            position += max(0.0, time.monotonic() - self._position_at)
        # Never let the slack swallow a whole short track.
        slack = min(self.EOF_SLACK, self.state.duration / 2)
        return position >= self.state.duration - slack

    async def _started(self, track: Track) -> None:
        self.state.status = "playing"
        self._note_position(0.0)
        self.state.duration = track.duration
        self.state.error = None
        self._changed()
        await self._sync()

    async def _stop_with(self, command: Awaitable[Any]) -> None:
        """Stop the service by ``command``, which must not count as a user's stop."""
        with self._transition():
            try:
                await command
            except BackendError as exc:
                log.debug("%s stop: %s", self.name, exc)
        self.state.status = "stopped"
        self.state.buffering = False
        self._note_position(0.0)
        self._changed()

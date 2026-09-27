"""Interfaces for the two kinds of thing the daemon plugs together: a *backend* is
a library you browse, a *sink* is somewhere audio comes out.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..models import Album, Artist, Track

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

    @abc.abstractmethod
    async def artists(self) -> list[Artist]: ...

    @abc.abstractmethod
    async def artist_albums(self, artist_id: str) -> list[Album]: ...

    @abc.abstractmethod
    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]: ...

    @abc.abstractmethod
    async def album_tracks(self, album_id: str) -> list[Track]: ...

    @abc.abstractmethod
    async def search(self, query: str, limit: int = 40) -> dict[str, list]: ...

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


    @abc.abstractmethod
    async def stream_target(self, track: Track) -> StreamTarget:
        """Work out how the given track can actually be played."""

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

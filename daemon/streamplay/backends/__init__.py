"""Backend registry."""

from __future__ import annotations

from typing import Any

from .base import (Backend, BackendError, Sink, SinkState, SourceUnavailable,
                   StreamTarget)
from .emby import EmbyBackend
from .jellyfin import JellyfinBackend
from .kodi import KodiBackend, KodiSink
from .lyrion import LyrionBackend, LyrionSink
from .mpd import MpdBackend, MpdSink
from .plex import PlexBackend
from .subsonic import SubsonicBackend
from .upnp import UpnpBackend, UpnpSink
from .youtube import YouTubeBackend

__all__ = [
    "Backend", "BackendError", "Sink", "SinkState", "SourceUnavailable",
    "StreamTarget",
    "EmbyBackend", "JellyfinBackend", "KodiBackend", "KodiSink",
    "LyrionBackend", "LyrionSink", "MpdBackend", "MpdSink", "PlexBackend",
    "SubsonicBackend", "UpnpBackend", "UpnpSink", "YouTubeBackend",
    "BACKEND_TYPES", "create_backend",
]

BACKEND_TYPES = {
    "subsonic": SubsonicBackend,
    "jellyfin": JellyfinBackend,
    "emby": EmbyBackend,
    "plex": PlexBackend,
    "kodi": KodiBackend,
    "mpd": MpdBackend,
    "lyrion": LyrionBackend,
    "upnp": UpnpBackend,
    "youtube": YouTubeBackend,
}


def create_backend(profile: dict[str, Any]) -> Backend:
    kind = str(profile.get("type") or "subsonic")
    try:
        cls = BACKEND_TYPES[kind]
    except KeyError:
        raise BackendError(f"Unknown backend type {kind!r}") from None
    return cls(profile)


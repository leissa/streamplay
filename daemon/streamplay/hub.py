"""The daemon's centre: every connected service, the shared queue, and the outputs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from . import __version__, secretstore
from .backends import (BACKEND_TYPES, Backend, BackendError, Sink,
                       SourceUnavailable, create_backend)
from .config import Config
from .covers import CoverCache
from .models import Track
from .mpris import MprisService, art_url
from .player import UnifiedPlayer
from .sinks import MpvSink

log = logging.getLogger(__name__)

#: How long to sit on playback-setting changes before writing config.json.
SETTINGS_FLUSH_DELAY = 3.0

#: Seconds between attempts to reach a Secret Service that is not running yet.
SECRETS_RETRY = 5.0

#: Size (px) of the cover handed to MPRIS / Now Playing.
ART_SIZE = 512

LOCAL_OUTPUT = "local"


class Hub:
    def __init__(self, config: Config, enable_mpris: bool = True) -> None:
        self.config = config
        self.covers = CoverCache()

        self.sources: dict[str, Backend] = {}
        self.source_state: dict[str, dict[str, Any]] = {}
        self.sinks: dict[str, Sink] = {}
        self.player = UnifiedPlayer(self, self.emit, config.settings)

        self._subscribers: set[Callable[[str, dict], None]] = set()
        self._mpris = MprisService(self) if enable_mpris else None
        self._lock = asyncio.Lock()
        self._art_by_key: dict[str, str] = {}
        self._art_pending: set[str] = set()
        self._settings_task: asyncio.Task | None = None
        self._secrets_task: asyncio.Task | None = None


    async def start(self) -> None:
        if self._mpris is not None:
            try:
                self._mpris.start(asyncio.get_running_loop())
            except Exception as exc:
                log.warning("MPRIS unavailable: %s", exc)
                self._mpris = None

        await self._ensure_local_sink()

        if self.config.profiles and not await self._load_secrets(warn=True):
            self._secrets_task = asyncio.create_task(self._retry_secrets())
        await self._connect_enabled()

        await self.set_output(self.config.settings.get("output") or LOCAL_OUTPUT,
                              persist=False)
        self.emit("sources", self.sources_json())
        self.broadcast_state()

    async def _load_secrets(self, warn: bool = False) -> bool:
        try:
            secrets = await asyncio.to_thread(secretstore.load_all)
        except secretstore.SecretStoreError as exc:
            (log.warning if warn else log.debug)("cannot read passwords: %s", exc)
            return False
        self.config.merge_secrets(secrets)
        return True

    async def _retry_secrets(self) -> None:
        while not await self._load_secrets():
            await asyncio.sleep(SECRETS_RETRY)
        log.info("passwords read from the Secret Service")
        await self._connect_enabled()

    async def _connect_enabled(self) -> None:
        for profile_id, profile in list(self.config.profiles.items()):
            if profile.get("enabled", True) and profile_id not in self.sources:
                try:
                    await self.connect_source(profile_id)
                except BackendError as exc:
                    log.warning("could not connect %s: %s", profile_id, exc)

    async def _ensure_local_sink(self) -> None:
        if LOCAL_OUTPUT in self.sinks:
            return
        sink = MpvSink(float(self.config.settings.get("volume", 0.7)))
        try:
            await sink.start()
        except Exception as exc:
            log.error("local playback unavailable: %s", exc)
            return
        self.sinks[LOCAL_OUTPUT] = sink

    async def close(self) -> None:
        if self._secrets_task:
            self._secrets_task.cancel()
            self._secrets_task = None
        if self._settings_task:
            self._settings_task.cancel()
            self._settings_task = None
        await self.player.set_sink(None, carry_over=False)
        for sink in list(self.sinks.values()):
            try:
                await sink.close()
            except Exception:
                log.debug("sink shutdown failed", exc_info=True)
        self.sinks.clear()
        for backend in list(self.sources.values()):
            try:
                await backend.close()
            except Exception:
                log.debug("backend shutdown failed", exc_info=True)
        self.sources.clear()
        if self._mpris is not None:
            self._mpris.stop()


    def subscribe(self, callback: Callable[[str, dict], None]) -> None:
        self._subscribers.add(callback)

    def unsubscribe(self, callback: Callable[[str, dict], None]) -> None:
        self._subscribers.discard(callback)

    def emit(self, event: str, data: dict) -> None:
        if event == "state":
            data = self._decorate_state(data)
            self._schedule_settings_flush(data)
            if self._mpris is not None:
                self._mpris.push_state(data)
        elif event == "seeked":
            if self._mpris is not None:
                self._mpris.push_seeked(float(data.get("position") or 0.0))

        for callback in list(self._subscribers):
            try:
                callback(event, data)
            except Exception:
                log.exception("subscriber failed for %s", event)

    def broadcast_state(self) -> None:
        self.emit("state", self.player.state())

    def broadcast_queue(self) -> None:
        """Which entries are playable depends on the services and the output."""
        self.emit("queue", self.player.queue())


    def _decorate_state(self, state: dict[str, Any]) -> dict[str, Any]:
        state = dict(state)
        state["sources"] = self.sources_json()
        state["outputs"] = self.outputs_json()

        track = state.get("track") or {}
        cover_id, source = track.get("coverId"), track.get("source")
        if cover_id and source:
            key = f"{source}\0{cover_id}"
            url = self._art_by_key.get(key)
            if url:
                state["artUrl"] = url
            else:
                self._request_art(key, source, cover_id)
            state["coverUrl"] = self.cover_url(source, cover_id)
        return state

    def cover_url(self, source: str, cover_id: str, size: int = 0) -> str:
        port = self.config.settings.get("port", 8760)
        query = urlencode({"src": source, "id": cover_id, "size": size})
        return f"http://127.0.0.1:{port}/cover?{query}"

    def _request_art(self, key: str, source: str, cover_id: str) -> None:
        backend = self.sources.get(source)
        if key in self._art_pending or backend is None:
            return
        self._art_pending.add(key)

        async def run() -> None:
            try:
                path = await self.covers.fetch(backend, source, cover_id, ART_SIZE)
                url = art_url(path)
                if url:
                    self._art_by_key[key] = url
                    # Re-emit so MPRIS picks up the artwork it just missed.
                    self.broadcast_state()
            finally:
                self._art_pending.discard(key)

        asyncio.create_task(run())


    def _schedule_settings_flush(self, state: dict[str, Any]) -> None:
        settings = self.config.settings
        wanted = {
            "volume": state.get("volume", settings.get("volume")),
            "shuffle": state.get("shuffle", settings.get("shuffle")),
            "repeat": state.get("repeat", settings.get("repeat")),
        }
        if all(settings.get(k) == v for k, v in wanted.items()):
            return
        settings.update(wanted)
        if self._settings_task and not self._settings_task.done():
            return

        async def flush() -> None:
            await asyncio.sleep(SETTINGS_FLUSH_DELAY)
            try:
                self.config.save()
            except OSError as exc:
                log.warning("cannot save settings: %s", exc)

        self._settings_task = asyncio.create_task(flush())


    def sources_json(self) -> list[dict[str, Any]]:
        out = []
        for profile_id, profile in self.config.profiles.items():
            cls = BACKEND_TYPES.get(profile.type, Backend)
            status = self.source_state.get(
                profile_id, {"state": "disconnected", "message": None})
            out.append({
                "id": profile_id,
                "name": profile.name,
                "type": profile.type,
                "enabled": profile.get("enabled", True),
                "state": status["state"],
                "message": status.get("message"),
                "canPlayback": cls.sinks is not Backend.sinks,
                "hasLibrary": cls.has_library,
            })
        return out

    def outputs_json(self) -> list[dict[str, Any]]:
        outputs = []
        for sink_id, sink in self.sinks.items():
            outputs.append({
                "id": sink_id,
                "name": sink.name,
                "kind": "local" if sink_id == LOCAL_OUTPUT else "remote",
                "active": self.player.sink is sink,
            })
        return outputs

    def _set_source_state(self, profile_id: str, state: str,
                          message: str | None = None) -> None:
        self.source_state[profile_id] = {"state": state, "message": message}
        self.emit("sources", self.sources_json())

    async def connect_source(self, profile_id: str) -> None:
        async with self._lock:
            profile = self.config.profiles.get(profile_id)
            if profile is None:
                raise BackendError(f"No music server named {profile_id!r}")

            await self._drop_source(profile_id)
            self._set_source_state(profile_id, "connecting")

            try:
                backend = create_backend(profile)
                await backend.connect()
            except BackendError as exc:
                self._set_source_state(profile_id, "error", str(exc))
                raise
            except Exception as exc:
                log.exception("connecting to %s failed", profile_id)
                self._set_source_state(profile_id, "error", str(exc))
                raise BackendError(str(exc)) from exc

            self.sources[profile_id] = backend
            await self._sync_sinks(profile_id, backend)
            backend.watch_sinks(lambda: asyncio.create_task(
                self._sinks_changed(profile_id, backend)))

            self._set_source_state(profile_id, "connected")
            self.broadcast_state()
            self.broadcast_queue()

    async def disconnect_source(self, profile_id: str) -> None:
        async with self._lock:
            await self._drop_source(profile_id)
            self._set_source_state(profile_id, "disconnected")
            self.broadcast_state()
            self.broadcast_queue()

    async def _sinks_changed(self, profile_id: str, backend: Backend) -> None:
        async with self._lock:
            if self.sources.get(profile_id) is not backend:
                return
            await self._sync_sinks(profile_id, backend)
            self.broadcast_state()
            self.broadcast_queue()

    async def _sync_sinks(self, profile_id: str, backend: Backend) -> None:
        """Bring the outputs of one service in line with what it offers now."""
        offered = {sink.id: sink for sink in backend.sinks()}
        for sink_id, sink in list(self.sinks.items()):
            if sink.source == profile_id and offered.get(sink_id) is not sink:
                await self._remove_sink(sink_id)

        new = [sink for sink_id, sink in offered.items() if sink_id not in self.sinks]
        started = await asyncio.gather(*(sink.start() for sink in new),
                                       return_exceptions=True)
        for sink, failure in zip(new, started):
            if isinstance(failure, BaseException):
                log.warning("%s is not usable as an output: %s", sink.name, failure)
                continue
            sink_id = sink.id
            self.sinks[sink_id] = sink
            # An output found by discovery turns up after start() has settled on local.
            if (sink_id == self.config.settings.get("output")
                    and self.player.sink is self.sinks.get(LOCAL_OUTPUT)
                    and self.player.state()["status"] != "playing"):
                await self.player.set_sink(sink)

    async def _remove_sink(self, sink_id: str) -> None:
        sink = self.sinks.pop(sink_id, None)
        if sink is None:
            return
        if self.player.sink is sink:
            await self.player.set_sink(self.sinks.get(LOCAL_OUTPUT), carry_over=False)
        try:
            await sink.close()
        except Exception:
            log.debug("sink shutdown failed", exc_info=True)

    async def _drop_source(self, profile_id: str) -> None:
        """Tear down a service and anything that depended on it."""
        # Outputs are found by the profile they belong to, not by a composed id.
        for sink_id in [k for k, v in self.sinks.items() if v.source == profile_id]:
            await self._remove_sink(sink_id)

        backend = self.sources.pop(profile_id, None)
        if backend is not None:
            backend.watch_sinks(None)
            try:
                await backend.close()
            except Exception:
                log.debug("backend shutdown failed", exc_info=True)
        self._art_by_key = {k: v for k, v in self._art_by_key.items()
                            if not k.startswith(profile_id + "\0")}

    async def reconnect_all(self) -> None:
        for profile_id, profile in list(self.config.profiles.items()):
            if profile.get("enabled", True):
                try:
                    await self.connect_source(profile_id)
                except BackendError as exc:
                    log.info("reconnect of %s failed: %s", profile_id, exc)


    async def set_output(self, sink_id: str, persist: bool = True) -> None:
        sink = self.sinks.get(sink_id) or self.sinks.get(LOCAL_OUTPUT)
        if sink is None:
            raise BackendError("No playback output is available")
        if self.player.sink is sink:
            return
        await self.player.set_sink(sink)
        if persist:
            self.config.set_setting("output", sink.id)
        self.emit("state", self.player.state())
        self.broadcast_queue()


    def source_name(self, source: str) -> str:
        profile = self.config.profiles.get(source)
        return profile.name if profile else source

    def unavailable(self, track: Track, sink: Sink | None) -> str | None:
        """Why the track cannot be played as things stand, or None."""
        backend = self.sources.get(track.source)
        if backend is None:
            return f"{self.source_name(track.source)} is not connected"
        if sink is None or sink.source == track.source:
            return None
        if not sink.plays(track):
            return f"{sink.name} plays only its own library"
        if sink.web_streams_only and not backend.web_streams:
            return f"{sink.name} cannot play music from {backend.name}"
        return None

    async def stream_target(self, track: Track):
        backend = self.sources.get(track.source)
        if backend is None:
            raise SourceUnavailable(
                f"{self.source_name(track.source)} is not connected")
        return await backend.stream_target(track)

    async def scrobble(self, track: Track, submission: bool) -> None:
        backend = self.sources.get(track.source)
        if backend is not None:
            await backend.scrobble(track, submission)


    def libraries(self) -> list[Backend]:
        return [b for b in self.sources.values() if b.has_library]

    def backend(self, source: str | None) -> Backend:
        if not source:
            libraries = self.libraries()
            if len(libraries) == 1:
                return libraries[0]
            raise BackendError("Say which music server to use")
        backend = self.sources.get(source)
        if backend is None:
            raise BackendError(f"{source} is not connected")
        return backend

    def selected(self, source: str | None) -> list[Backend]:
        """One named service, or all connected ones when none is named."""
        if source:
            return [self.backend(source)]
        libraries = self.libraries()
        if not libraries:
            raise BackendError("No music server is connected")
        return libraries

    async def gather(self, source: str | None, call) -> list:
        """Run a library call across the selected services and merge the results."""
        backends = self.selected(source)
        results = await asyncio.gather(
            *(call(backend) for backend in backends), return_exceptions=True)
        merged: list = []
        for backend, result in zip(backends, results):
            if isinstance(result, Exception):
                log.info("%s: %s", backend.name, result)
                continue
            merged.extend(result)
        return merged


    async def tracks_for(self, spec: dict[str, Any]) -> list[Track]:
        """Resolve a browse selection into concrete tracks."""
        source = spec.get("source")

        if spec.get("albumId"):
            return await self.backend(source).album_tracks(str(spec["albumId"]))

        if spec.get("playlistId"):
            return await self.backend(source).playlist_tracks(str(spec["playlistId"]))

        if spec.get("artistId"):
            backend = self.backend(source)
            out: list[Track] = []
            for album in await backend.artist_albums(str(spec["artistId"])):
                out.extend(await backend.album_tracks(album.id))
            return out

        return [self._track_from_json(t) for t in spec.get("tracks") or []]

    @staticmethod
    def _track_from_json(data: dict[str, Any]) -> Track:
        extra: dict[str, Any] = {}
        if data.get("backend") == "kodi" and str(data.get("id", "")).isdigit():
            extra["songid"] = int(data["id"])
        if data.get("file"):
            extra["file"] = data["file"]
        return Track(
            id=str(data.get("id")),
            title=data.get("title") or "",
            artist=data.get("artist") or "",
            album=data.get("album") or "",
            duration=float(data.get("duration") or 0),
            backend=data.get("backend") or "",
            source=data.get("source") or "",
            artist_id=data.get("artistId"),
            album_id=data.get("albumId"),
            track_no=data.get("trackNo"),
            disc_no=data.get("discNo"),
            year=data.get("year"),
            genre=data.get("genre"),
            cover_id=data.get("coverId"),
            extra=extra,
        )


    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._decorate_state(self.player.state()),
            "queue": self.player.queue(),
            "sources": self.sources_json(),
            "outputs": self.outputs_json(),
            "profiles": self.config.redacted_profiles(),
            "settings": self.config.settings,
            "daemon": {"version": __version__,
                       "home": str(Path(__file__).resolve().parent.parent)},
        }

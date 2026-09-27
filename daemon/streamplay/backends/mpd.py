"""MPD over its own text protocol.

Like Kodi, MPD turns up twice: a library (:class:`MpdBackend`) and an output
(:class:`MpdSink`), independent of each other because the daemon owns the queue.
MPD has no ids, so a tag value is the id, and it serves no audio of its own --
see :meth:`MpdBackend._file_url`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..models import Album, Artist, Track
from .base import Backend, BackendError, PolledSink, Sink, StreamTarget

log = logging.getLogger(__name__)

#: Joins an album's artist and title into one id; no tag can contain it.
ID_SEP = "\x1f"

CONNECT_TIMEOUT = 8.0
COMMAND_TIMEOUT = 30.0


def _quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _term(value: str) -> str:
    """A filter value, single-quoted the way MPD's filter grammar wants."""
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _filter(*clauses: str) -> str:
    """Combine ``(tag == 'x')`` clauses into one filter expression."""
    kept = [c for c in clauses if c]
    if not kept:
        return "(base '')"      # matches the whole library
    if len(kept) == 1:
        return kept[0]
    return "(" + " AND ".join(kept) + ")"


def _eq(tag: str, value: str) -> str:
    return f"({tag} == {_term(value)})"


def _contains(tag: str, value: str) -> str:
    return f"({tag} contains {_term(value)})"


def _int(value: str | None) -> int | None:
    """``"3"`` and ``"3/12"`` both mean three; anything else means nothing."""
    if not value:
        return None
    head = str(value).split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def _year(value: str | None) -> int | None:
    """MPD dates run from a bare year to a full ISO timestamp."""
    if not value:
        return None
    return _int(str(value)[:4])


class MpdCommandError(BackendError):
    """MPD answered with ACK. The connection is still usable."""


class MpdConnection:
    """One connection speaking the MPD text protocol.

    Replies stay a list of pairs because their order carries meaning: a song is
    one run of keys.
    A lock serialises commands, since MPD has no request ids to match answers by.
    """

    def __init__(self, host: str, port: int, password: str = "",
                 unix_socket: str = "", label: str = "MPD") -> None:
        self.host = host
        self.port = port
        self.password = password
        self.unix_socket = unix_socket
        self.label = label
        self.version = ""
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()


    async def _open(self) -> None:
        try:
            if self.unix_socket:
                opening = asyncio.open_unix_connection(self.unix_socket)
            else:
                opening = asyncio.open_connection(self.host, self.port)
            reader, writer = await asyncio.wait_for(opening, CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise BackendError(f"{self.label}: timed out connecting") from None
        except OSError as exc:
            raise BackendError(f"{self.label}: {exc.strerror or exc}") from exc

        try:
            banner = await asyncio.wait_for(reader.readline(), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            writer.close()
            raise BackendError(f"{self.label}: no greeting") from None

        text = banner.decode("utf-8", "replace").strip()
        if not text.startswith("OK MPD "):
            writer.close()
            raise BackendError(f"{self.label}: not an MPD server")
        self.version = text[len("OK MPD "):]
        self._reader, self._writer = reader, writer

        if self.password:
            try:
                await self._exchange("password", self.password)
            except MpdCommandError:
                await self.close()
                raise BackendError(f"{self.label}: the password was rejected") from None

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass


    async def command(self, *args: Any) -> list[tuple[str, str]]:
        async with self._lock:
            if not self.connected:
                await self._open()
            try:
                return (await self._exchange(*args))[0]
            except MpdCommandError:
                raise
            except BackendError:
                # A dropped socket looks exactly like this.
                await self.close()
                await self._open()
                return (await self._exchange(*args))[0]

    async def binary(self, name: str, uri: str) -> bytes | None:
        """Read a binary reply, which MPD hands over ``binarylimit`` bytes at a time."""
        async with self._lock:
            if not self.connected:
                await self._open()
            chunks: list[bytes] = []
            offset = total = 0
            while True:
                pairs, blob = await self._exchange(name, uri, offset)
                if not blob:
                    break
                chunks.append(blob)
                offset += len(blob)
                total = next((_int(v) or 0 for k, v in pairs if k == "size"),
                             total)
                if offset >= total:
                    break
            return b"".join(chunks) or None

    async def _exchange(
            self, *args: Any) -> tuple[list[tuple[str, str]], bytes | None]:
        """Send one command; return its key/value pairs and any binary blob."""
        assert self._reader is not None and self._writer is not None
        line = " ".join([str(args[0])] + [_quote(a) for a in args[1:]])
        try:
            self._writer.write((line + "\n").encode("utf-8"))
            await self._writer.drain()
            return await asyncio.wait_for(self._read_reply(), COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            await self.close()
            raise BackendError(f"{self.label}: {args[0]} timed out") from None
        except (OSError, ConnectionError) as exc:
            await self.close()
            raise BackendError(f"{self.label}: {exc}") from exc

    async def _read_reply(self) -> tuple[list[tuple[str, str]], bytes | None]:
        assert self._reader is not None
        pairs: list[tuple[str, str]] = []
        blob: bytes | None = None
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("the server closed the connection")
            text = raw.decode("utf-8", "replace").rstrip("\n")

            if text == "OK":
                return pairs, blob
            if text.startswith("ACK "):
                raise MpdCommandError(f"{self.label}: {self._ack_message(text)}")
            if text.startswith("binary: "):
                size = _int(text[len("binary: "):]) or 0
                blob = await self._reader.readexactly(size)
                await self._reader.readexactly(1)   # the newline after the blob
                continue

            key, _, value = text.partition(": ")
            pairs.append((key, value))

    @staticmethod
    def _ack_message(text: str) -> str:
        """Pull the human half out of ``ACK [50@0] {find} No such directory``."""
        _, _, tail = text.partition("} ")
        return tail or text[len("ACK "):]


def _runs(pairs: Sequence[tuple[str, str]], start: str) -> Iterable[dict[str, str]]:
    """Split a flat reply into one dict per item, each starting at a ``start`` key."""
    current: dict[str, str] | None = None
    for key, value in pairs:
        if key == start:
            if current is not None:
                yield current
            current = {key: value}
        elif current is not None and key not in current:
            current[key] = value
    if current is not None:
        yield current


def _grouped(pairs: Sequence[tuple[str, str]],
             wanted: str) -> Iterable[tuple[str, dict[str, str]]]:
    """Walk a ``list <tag> group <tag>…`` reply.

    MPD prints a group's value just before the run it applies to.
    """
    groups: dict[str, str] = {}
    for key, value in pairs:
        if key.lower() == wanted.lower():
            yield value, dict(groups)
        else:
            groups[key.lower()] = value


class MpdBackend(Backend):
    kind = "mpd"
    web_streams = False

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__(profile)
        self.unix_socket = str(profile.get("socket") or "").strip()
        self.host = str(profile.get("host") or "127.0.0.1").strip()
        self.port = int(profile.get("port") or 6600)
        self.password = str(profile.get("password") or "")
        #: Where MPD's files live as far as *this* machine is concerned.
        self.music_directory = str(profile.get("musicDirectory") or "").strip()

        self.client = MpdConnection(self.host, self.port, self.password,
                                    self.unix_socket, self.name)
        self._sink = MpdSink(self)

    def sinks(self) -> list[Sink]:
        return [self._sink]

    async def call(self, *args: Any) -> list[tuple[str, str]]:
        return await self.client.command(*args)


    async def connect(self) -> None:
        await self.call("ping")
        if not self.music_directory:
            await self._discover_music_directory()

    async def _discover_music_directory(self) -> None:
        """Ask MPD where its files are, which only socket clients may do."""
        try:
            pairs = await self.call("config")
        except BackendError as exc:
            log.debug("%s: no music directory from config: %s", self.name, exc)
            return
        for key, value in pairs:
            if key == "music_directory" and value:
                self.music_directory = value
                log.info("%s: music directory is %s", self.name, value)

    async def close(self) -> None:
        await self.client.close()


    def _track(self, song: dict[str, str]) -> Track:
        uri = song.get("file", "")
        album_artist = song.get("AlbumArtist") or song.get("Artist") or ""
        return Track(
            # The path is the only stable handle MPD has.
            id=uri,
            title=song.get("Title") or Path(uri).stem or "Unknown",
            artist=song.get("Artist") or song.get("AlbumArtist") or "",
            album=song.get("Album") or "",
            duration=float(song.get("duration") or song.get("Time") or 0),
            backend=self.kind,
            source=self.source,
            artist_id=album_artist or None,
            album_id=self._album_id(album_artist, song.get("Album") or "") or None,
            track_no=_int(song.get("Track")),
            disc_no=_int(song.get("Disc")),
            year=_year(song.get("Date") or song.get("OriginalDate")),
            genre=song.get("Genre") or None,
            cover_id=uri or None,
            extra={"uri": uri},
        )

    @staticmethod
    def _album_id(artist: str, album: str) -> str:
        return f"{artist}{ID_SEP}{album}" if album else ""

    @staticmethod
    def _split_album_id(album_id: str) -> tuple[str, str]:
        artist, _, album = str(album_id).partition(ID_SEP)
        return artist, album

    def _album(self, name: str, groups: dict[str, str]) -> Album:
        artist = groups.get("albumartist") or ""
        return Album(
            id=self._album_id(artist, name),
            name=name,
            artist=artist,
            source=self.source,
            artist_id=artist or None,
            year=_year(groups.get("date")),
        )


    async def _album_list(self, *clauses: str) -> list[Album]:
        """Every album matching the clauses, with its artist and year.

        Track counts stay zero because ``count`` takes only one ``group``.
        Grouping by date is what yields a year, and also why duplicates have to
        be folded: tracks tagged ``1998`` and ``1998-04-02`` are two groups.
        """
        pairs = await self.call(
            "list", "album", _filter(*clauses), "group", "albumartist",
            "group", "date")

        seen: dict[tuple[str, str], Album] = {}
        for name, groups in _grouped(pairs, "Album"):
            if not name:
                continue
            album = self._album(name, groups)
            key = (album.artist.casefold(), album.name.casefold())
            first = seen.get(key)
            if first is None:
                seen[key] = album
            elif album.year is not None:
                # A disagreeing date is usually a reissue tag, so take the earliest.
                first.year = (album.year if first.year is None
                              else min(first.year, album.year))
        return list(seen.values())

    def _artists_of(self, albums: list[Album]) -> list[Artist]:
        """The album artists in a list of albums, with how many each has.

        MPD's ``list albumartist`` gives the names but not the counts.
        """
        counts: dict[str, int] = {}
        for album in albums:
            if album.artist:
                counts[album.artist] = counts.get(album.artist, 0) + 1
        return [Artist(id=name, name=name, source=self.source,
                       album_count=count)
                for name, count in counts.items()]

    async def artists(self) -> list[Artist]:
        return self._artists_of(await self._album_list())

    async def artist_albums(self, artist_id: str) -> list[Album]:
        return await self._album_list(_eq("AlbumArtist", artist_id))

    async def albums(self, sort: str = "alphabetical", offset: int = 0,
                     limit: int = 100) -> list[Album]:
        # MPD cannot sort or page a ``list``, and its database is in memory anyway.
        albums = await self._album_list()
        albums.sort(key=lambda a: (a.artist.lower(), a.year or 0, a.name.lower()))
        return albums[offset:offset + limit] if limit else albums[offset:]

    async def album_tracks(self, album_id: str) -> list[Track]:
        artist, album = self._split_album_id(album_id)
        clauses = [_eq("Album", album)]
        if artist:
            clauses.append(_eq("AlbumArtist", artist))
        pairs = await self.call("find", _filter(*clauses))
        tracks = [self._track(s) for s in _runs(pairs, "file")]
        tracks.sort(key=lambda t: (t.disc_no or 0, t.track_no or 0, t.title.lower()))
        return tracks

    async def search(self, query: str, limit: int = 40) -> dict[str, list]:
        """Find artists, albums and tracks whose names contain the query.

        Artists and albums are sieved here because ``list`` matches case-sensitively.
        """
        needle = query.casefold()
        albums, songs = await asyncio.gather(
            self._album_list(),
            # ``search``, unlike ``list`` and ``find``, ignores case.
            self.call("search", _contains("Title", query),
                      "window", f"0:{max(1, limit)}"),
            return_exceptions=True,
        )

        def ok(result: Any) -> list:
            """One arm failing should not empty the other."""
            if isinstance(result, Exception):
                log.debug("%s search: %s", self.name, result)
                return []
            return result

        albums = ok(albums)
        return {
            "artists": [a for a in self._artists_of(albums)
                        if needle in a.name.casefold()][:limit],
            "albums": [a for a in albums
                       if needle in a.name.casefold()
                       or needle in a.artist.casefold()][:limit],
            "tracks": [self._track(s) for s in _runs(ok(songs), "file")],
        }

    async def genres(self) -> list[str]:
        pairs = await self.call("list", "genre")
        return sorted({value for key, value in pairs
                       if key == "Genre" and value})

    async def genre_albums(self, genre: str, offset: int = 0,
                           limit: int = 100) -> list[Album]:
        albums = await self._album_list(_eq("Genre", genre))
        for album in albums:
            # Grouping by genre would split an album tagged inconsistently.
            album.genre = genre
        return albums[offset:offset + limit] if limit else albums[offset:]

    async def playlists(self) -> list[dict[str, Any]]:
        pairs = await self.call("listplaylists")
        return [{"id": name, "source": self.source, "name": name}
                for name in (e.get("playlist") for e in _runs(pairs, "playlist"))
                if name]

    async def playlist_tracks(self, playlist_id: str) -> list[Track]:
        pairs = await self.call("listplaylistinfo", playlist_id)
        return [self._track(s) for s in _runs(pairs, "file")]


    def _file_url(self, uri: str) -> str | None:
        """The track as a URL any player can open, if the files are reachable here."""
        if uri.startswith(("http://", "https://")):
            return uri            # a radio stream sitting in MPD's database
        if not self.music_directory:
            return None
        path = Path(self.music_directory) / uri
        try:
            return path.as_uri() if path.is_file() else None
        except (OSError, ValueError):
            return None

    async def stream_target(self, track: Track) -> StreamTarget:
        uri = track.extra.get("uri") or track.id
        if not uri:
            raise BackendError(f"{self.name}: {track.title} has no path")
        return StreamTarget(url=self._file_url(uri), native={"uri": uri},
                            source=self.source)

    async def cover_bytes(self, cover_id: str, size: int) -> bytes | None:
        """Cover art, which MPD sends down the control connection rather than over HTTP.

        ``albumart`` is the folder image and ``readpicture`` the embedded one.
        ``size`` is ignored: MPD hands over whatever it has.
        """
        if not cover_id:
            return None
        for command in ("albumart", "readpicture"):
            try:
                data = await self.client.binary(command, cover_id)
            except BackendError as exc:
                log.debug("%s %s: %s", self.name, command, exc)
                continue
            if data:
                return data
        return None


class MpdSink(PolledSink):
    """Plays one track at a time on an MPD instance.

    MPD's own queue is deliberately unused; ours is the source of truth.
    """

    def __init__(self, backend: MpdBackend) -> None:
        super().__init__()
        self.backend = backend
        self.source = backend.source
        self.id = f"mpd:{backend.source}"
        self.name = backend.name
        #: A second connection, because ``idle`` blocks until something happens.
        self.watcher = MpdConnection(
            backend.host, backend.port, backend.password,
            backend.unix_socket, backend.name)
        self._has_mixer = True
        self._idle_task: asyncio.Task | None = None

    async def start(self) -> None:
        # Whatever the user left MPD set to, our queue does the deciding.
        await self.backend.call("repeat", "0")
        await self.backend.call("random", "0")
        await self.backend.call("consume", "0")
        await self.backend.call("single", "1")
        await self._sync()
        self._idle_task = asyncio.create_task(
            self._idle_loop(), name=f"mpd-idle-{self.backend.source}")
        # Keeps the progress bar moving, since ``idle`` says nothing about elapsed.
        self._start_polling()

    async def close(self) -> None:
        await super().close()
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        await self.watcher.close()


    async def _idle_loop(self) -> None:
        """Follow MPD's change notifications, so another client shows up at once."""
        backoff = 1.0
        while True:
            try:
                await self.watcher.command("idle", "player", "mixer", "options")
                backoff = 1.0
                await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("%s idle: %s", self.name, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _sync(self) -> None:
        try:
            status = dict(await self.backend.call("status"))
        except BackendError as exc:
            self.state.error = str(exc)
            self._changed()
            return
        if self._changing:
            return

        # Both loops can be here at once, and the connection serialises the reads.
        was, near_end = self.state.status, self._near_end()
        state = status.get("state", "stop")
        self.state.status = {"play": "playing", "pause": "paused"}.get(
            state, "stopped")
        self._note_position(float(status.get("elapsed") or 0.0))
        self.state.duration = float(status.get("duration") or self.state.duration)
        self.state.error = status.get("error") or None

        # No volume, or -1, means MPD's output has no mixer and the level is not ours.
        volume = _int(status.get("volume"))
        self._has_mixer = volume is not None and volume >= 0
        if self._has_mixer:
            self.state.volume = max(0.0, min(1.0, (volume or 0) / 100.0))

        if self.state.status == "stopped" and was == "playing":
            self._note_position(0.0)
            if self.state.error:
                # Separate from eof so the player steps over the track and says why.
                await self._ended("error")
                return
            if near_end:
                await self._ended("eof")
                return
            # Otherwise somebody stopped MPD from another client.
        self._changed()


    def plays(self, track: Track) -> bool:
        return track.source == self.backend.source

    def _uri_for(self, target: StreamTarget, track: Track) -> str:
        if target.native and target.source == self.backend.source:
            uri = str(target.native.get("uri") or "")
            if uri:
                return uri
        if target.url and target.url.startswith(("http://", "https://")):
            return target.url
        if target.url:
            raise BackendError(
                f"{self.name} can only play its own library and web streams")
        raise BackendError(f"{self.name} cannot play {track.title}")

    async def play(self, target: StreamTarget, track: Track) -> None:
        uri = self._uri_for(target, track)

        # Replacing the queue takes MPD through stop.
        with self._transition():
            # MPD keeps the last playback error until told to forget it.
            await self.backend.call("clearerror")
            await self.backend.call("clear")
            await self.backend.call("single", "1")
            await self.backend.call("add", uri)
            await self.backend.call("play")
        await self._started(track)

    async def resume(self) -> None:
        await self.backend.call("pause", "0")
        await self._sync()

    async def pause(self) -> None:
        if self.state.status == "stopped":
            return
        await self.backend.call("pause", "1")
        await self._sync()

    async def stop(self) -> None:
        await self._stop_with(self.backend.call("stop"))

    async def seek(self, position: float) -> None:
        try:
            await self.backend.call("seekcur", f"{max(0.0, position):.3f}")
        except BackendError as exc:
            log.debug("%s seek: %s", self.name, exc)
            return
        self._note_position(max(0.0, position))
        self._changed()

    async def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(1.0, float(volume)))
        if not self._has_mixer:
            # No mixer: remember the wish without pretending it did anything.
            self.state.volume = volume
            self._changed()
            return
        try:
            await self.backend.call("setvol", str(int(round(volume * 100))))
        except BackendError as exc:
            log.debug("%s setvol: %s", self.name, exc)
            return
        self.state.volume = volume
        self._changed()

    def capabilities(self) -> dict[str, bool]:
        return {"seek": True, "volume": self._has_mixer}

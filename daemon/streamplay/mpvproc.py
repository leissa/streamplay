"""Thin asyncio wrapper around an ``mpv --idle`` process driven over JSON IPC.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")

#: Properties the player needs to be told about as they change.
OBSERVED = (
    "pause",
    "time-pos",
    "duration",
    "core-idle",
    "volume",
    "eof-reached",
    "seeking",
    "cache-buffering-state",
    "metadata",
)


class MpvError(RuntimeError):
    pass


class Mpv:
    """One long-lived mpv process; its callbacks run on the same loop as everything else.
    """

    def __init__(
        self,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_property: Callable[[str, Any], Awaitable[None]],
        extra_args: list[str] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_property = on_property
        self._extra_args = extra_args or []

        self._socket = RUNTIME_DIR / f"streamplay-mpv-{os.getpid()}.sock"
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        # Handlers issue mpv commands, so running them in the reader would deadlock.
        self._events: asyncio.Queue[tuple[str, str, object]] = asyncio.Queue()
        self._event_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._starting: asyncio.Lock = asyncio.Lock()
        self._closed = False


    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._writer is not None
        )

    async def start(self) -> None:
        async with self._starting:
            if self.alive:
                return
            await self._teardown()

            if not shutil.which("mpv"):
                raise MpvError("mpv is not installed")

            self._socket.unlink(missing_ok=True)
            args = [
                "mpv",
                "--idle=yes",
                "--no-video",
                "--no-terminal",
                "--audio-display=no",
                "--gapless-audio=yes",
                "--prefetch-playlist=yes",
                "--keep-open=no",
                "--force-window=no",
                "--msg-level=all=warn",
                "--replaygain=track",
                "--audio-client-name=streamplay",
                # Buffer network streams so a hiccup is not an audible dropout.
                "--cache=yes",
                "--demuxer-max-bytes=64MiB",
                "--demuxer-readahead-secs=30",
                f"--input-ipc-server={self._socket}",
                *self._extra_args,
            ]
            log.info("starting mpv")
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            deadline = time.monotonic() + 10.0
            last_exc: Exception | None = None
            while time.monotonic() < deadline:
                if self._proc.returncode is not None:
                    raise MpvError(f"mpv exited immediately ({self._proc.returncode})")
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        str(self._socket)
                    )
                    break
                except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                    last_exc = exc
                    await asyncio.sleep(0.05)
            else:
                raise MpvError(f"could not connect to mpv IPC socket: {last_exc}")

            self._closed = False
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="mpv-reader"
            )
            self._event_task = asyncio.create_task(
                self._event_loop(), name="mpv-events"
            )
            for index, prop in enumerate(OBSERVED, start=1):
                await self.command("observe_property", index, prop)

    def _drop_ipc(self) -> None:
        """Forget the IPC connection and fail whatever was waiting on it.

        Otherwise :attr:`alive` stays true and the next command waits out its
        full timeout, which cost ten seconds and a SIGKILL on every restart.
        """
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MpvError("mpv connection closed"))
        self._pending.clear()

    async def _teardown(self) -> None:
        tasks = [t for t in (self._reader_task, self._event_task)
                 if t is not None and t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        # Await them: their cleanup clears the connection start() is about to replace.
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reader_task = self._event_task = None
        self._drop_ipc()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        self._socket.unlink(missing_ok=True)

    async def close(self) -> None:
        self._closed = True
        await self._teardown()


    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            # mpv closing the socket is how it reports its own exit.
            log.debug("mpv IPC read ended: %s", exc)
        except Exception:
            log.exception("mpv reader failed")
        finally:
            # Before announcing the loss, since the handler may try to talk to mpv.
            self._drop_ipc()
            if not self._closed:
                log.warning("mpv IPC connection lost")
                self._events.put_nowait(("event", "ipc-closed", {}))

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "request_id" in msg:
            fut = self._pending.pop(msg["request_id"], None)
            if fut and not fut.done():
                if msg.get("error", "success") != "success":
                    fut.set_exception(MpvError(msg.get("error", "unknown error")))
                else:
                    fut.set_result(msg.get("data"))
            return

        event = msg.get("event")
        if event == "property-change":
            self._events.put_nowait(
                ("property", msg.get("name", ""), msg.get("data")))
        elif event:
            self._events.put_nowait(("event", event, msg))

    async def _event_loop(self) -> None:
        while True:
            kind, name, payload = await self._events.get()
            try:
                if kind == "property":
                    await self._on_property(name, payload)
                else:
                    await self._on_event(name, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mpv %s handler failed for %s", kind, name)

    async def command(self, *args: Any, timeout: float = 10.0) -> Any:
        return await self._send(list(args), args[0], timeout)

    async def _send(self, command: Any, name: str, timeout: float) -> Any:
        if not self.alive:
            raise MpvError("mpv is not running")
        assert self._writer is not None

        request_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        payload = json.dumps({"command": command, "request_id": request_id})
        try:
            self._writer.write(payload.encode("utf-8") + b"\n")
            await self._writer.drain()
        except Exception as exc:
            self._pending.pop(request_id, None)
            raise MpvError(str(exc)) from exc

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise MpvError(f"mpv command timed out: {name}") from exc


    async def set_property(self, name: str, value: Any) -> None:
        await self.command("set_property", name, value)

    async def get_property(self, name: str) -> Any:
        return await self.command("get_property", name)

    async def loadfile(self, url: str, mode: str = "replace",
                       start: float = 0.0) -> int | None:
        """Returns the playlist entry id, which mpv before 0.38 does not report."""
        command: dict[str, Any] = {"name": "loadfile", "url": url, "flags": mode}
        if start > 0:
            # Named, because mpv 0.38 moved the positional options argument.
            command["options"] = {"start": f"{start:.3f}"}
        reply = await self._send(command, "loadfile", timeout=30.0)
        return reply.get("playlist_entry_id") if isinstance(reply, dict) else None

    async def stop(self) -> None:
        await self.command("stop")

"""Daemon entry point: ``python -m streamplay``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from .config import CONFIG_FILE, Config
from .hub import Hub
from .server import ControlServer

log = logging.getLogger("streamplay")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="streamplay",
        description="Control daemon for self-hosted music libraries "
                    "(Subsonic, Jellyfin, Emby, Plex, Kodi, MPD, Lyrion, UPnP) with MPRIS2 support.",
    )
    parser.add_argument("-c", "--config", type=Path, default=CONFIG_FILE,
                        help=f"configuration file (default: {CONFIG_FILE})")
    parser.add_argument("-p", "--port", type=int, default=None,
                        help="override the control port from the config file")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--no-mpris", action="store_true",
                        help="do not register an MPRIS2 D-Bus service")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="repeat for more logging")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    config = Config(args.config)
    port = args.port or int(config.settings.get("port", 8760))
    config.settings["port"] = port

    hub = Hub(config, enable_mpris=not args.no_mpris)
    server = ControlServer(hub, host=args.host, port=port)

    loop = asyncio.get_running_loop()
    stopping = loop.create_future()

    def request_stop(signame: str) -> None:
        log.info("received %s, shutting down", signame)
        if not stopping.done():
            stopping.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_stop, sig.name)

    await hub.start()
    serving = asyncio.create_task(server.serve_forever(), name="control-server")

    done, _ = await asyncio.wait(
        {serving, stopping}, return_when=asyncio.FIRST_COMPLETED)

    exit_code = 0
    if serving in done:
        try:
            serving.result()
        except OSError as exc:
            log.error("cannot listen on %s:%d: %s", args.host, port, exc)
            exit_code = 1
        except Exception:
            log.exception("control server crashed")
            exit_code = 1

    serving.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serving
    await server.close()
    await hub.close()
    return exit_code


class _AbortedRequestFilter(logging.Filter):
    """Drop the tracebacks from HTTP requests QML abandoned mid-flight.
    """

    MESSAGES = ("opening handshake failed", "unexpected internal error")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() not in self.MESSAGES:
            return True
        exc = record.exc_info[1] if record.exc_info else None
        while exc is not None:
            if isinstance(exc, (AssertionError, ConnectionError)) or type(
                exc
            ).__name__ in ("ConnectionClosedError", "ConnectionClosedOK"):
                log.debug("client went away mid-request")
                return False
            exc = exc.__cause__ or exc.__context__
        return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.server").addFilter(_AbortedRequestFilter())

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

"""Entry point: run the simple-talk mesh server."""

import argparse
import logging

from . import __version__
from .server import ChatServer
from .protocol import DEFAULT_PORT


async def run(host: str, port: int, hop_ms: int, seed: int | None) -> None:
    srv = ChatServer(host, port, hop_ms=hop_ms, seed=seed)
    await srv.start()
    try:
        await srv.serve()
    finally:
        await srv.shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="simple-talk-server", description="simulated mesh chat server")
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default %(default)s)")
    p.add_argument("--hop-ms", type=int, default=120, help="simulated per-hop latency in ms (default %(default)s)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible topology")
    p.add_argument("--log-level", default="INFO", help="logging level (default %(default)s)")
    p.add_argument("--version", action="version", version=f"simple-talk-server {__version__}")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    import asyncio

    try:
        asyncio.run(run(args.host, args.port, args.hop_ms, args.seed))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
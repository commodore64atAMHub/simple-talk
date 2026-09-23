"""Async socket client for simple-talk."""

import asyncio
import logging

from . import protocol

log = logging.getLogger("stclient")


class MeshClient:
    """Thin asyncio line-protocol client that dispatches to handlers.

    Handlers are registered with ``on(type, callback)``; callbacks are
    async and awaited sequentially in the read loop.
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.writer: asyncio.StreamWriter | None = None
        self.reader: asyncio.StreamReader | None = None
        self._handlers: dict = {}
        self._closed = False

    def on(self, kind: str, cb):
        self._handlers[kind] = cb

    def on_close(self, cb):
        self._handlers["_closed"] = cb

    @property
    def closed(self) -> bool:
        return self._closed

    async def connect(self, nick: str, timeout: float = 10.0) -> None:
        """Open the TCP connection and send ``hello``.

        ``timeout`` bounds the connect phase. Without it, a firewall or
        network that silently drops packets makes the client hang forever
        with no explanation.
        """
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=max(1.0, timeout)
        )
        await self._write({"type": "hello", "nick": nick})

    async def _write(self, payload: dict) -> None:
        if self.writer is None or self.reader is None:
            raise RuntimeError("client not connected")
        self.writer.write(protocol.encode(payload))
        await self.writer.drain()

    async def say(self, to: str, body: str) -> None:
        await self._write({"type": "send", "to": to, "body": body})

    async def wire(self, kind: str, a: str, b: str) -> None:
        """Snip (``kind="cut"``) or re-attach (``kind="link"``) a wire."""
        if kind not in ("cut", "link"):
            raise ValueError(f"unknown wire action: {kind}")
        await self._write({"type": kind, "a": a, "b": b})

    async def bye(self) -> None:
        try:
            await self._write({"type": "bye"})
        except Exception:
            pass

    async def run(self) -> None:
        """Read-loop; returns when the server closes the connection."""
        while True:
            try:
                raw = await self.reader.readline()
            except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
                log.debug("read error: %s", exc)
                break
            if not raw:
                break
            try:
                msg = protocol.decode(raw)
            except ValueError:
                log.warning("dropping malformed frame")
                continue
            cb = self._handlers.get(msg.get("type"))
            if cb:
                try:
                    await cb(msg)
                except Exception:
                    log.exception("handler for %r failed", msg.get("type"))
        self._closed = True
        closed = self._handlers.get("_closed")
        if closed:
            try:
                await closed()
            except Exception:
                log.exception("close handler failed")

    def close(self) -> None:
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None
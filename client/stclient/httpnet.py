"""HTTP transport for the simple-talk client (no third-party deps).

``net.MeshClient`` speaks newline-delimited JSON over one long-lived TCP
socket.  This class has the *same* interface -- ``on``/``on_close``/
``connect``/``say``/``wire``/``bye``/``run``/``close`` -- but talks to
an HTTP server instead (``server/http.py`` or any of the servers in
``templates/``):

* ``connect()``       -> ``POST /api/hello`` (gets a session token)
* the read loop       -> ``GET /api/events`` long-polling; every frame
                         the TCP server would have pushed arrives here
* ``say``/``wire``    -> ``POST /api/send`` / ``POST /api/wire``
* ``bye()``           -> ``POST /api/bye``

Requests are spoken directly over asyncio streams (a few lines of
HTTP/1.1, ``Connection: close``) rather than through ``urllib`` in a
thread pool: a long-poll that sits for 15s must be cancellable the
moment the user quits, and an executor thread would otherwise keep the
event loop alive until it timed out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from urllib.parse import urlsplit

log = logging.getLogger("stclient.http")

POLL_WAIT = 15.0  # seconds a single /api/events long-poll may park for


class HttpMeshClient:
    """Drop-in stand-in for :class:`stclient.net.MeshClient` over HTTP."""

    def __init__(self, base_url: str, poll_wait: float = POLL_WAIT):
        parts = urlsplit(base_url if "//" in base_url else "//" + base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {parts.scheme!r} (use http:// or https://)")
        if not parts.hostname:
            raise ValueError(f"no host in URL: {base_url!r}")
        default_port = 443 if parts.scheme == "https" else 80
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or default_port
        self.base_path = parts.path.rstrip("/")
        self.base_url = f"{self.scheme}://{self.host}:{self.port}{self.base_path}"
        self.poll_wait = max(1.0, poll_wait)
        self.token: str | None = None
        self._since = 0
        self._handlers: dict = {}
        self._closed = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HttpMeshClient({self.base_url!r})"

    # ---- handler plumbing (mirrors MeshClient) --------------------------
    def on(self, kind: str, cb) -> None:
        self._handlers[kind] = cb

    def on_close(self, cb) -> None:
        self._handlers["_closed"] = cb

    @property
    def closed(self) -> bool:
        return self._closed

    async def _dispatch(self, msg: dict) -> None:
        cb = self._handlers.get(msg.get("type"))
        if cb:
            try:
                await cb(msg)
            except Exception:
                log.exception("handler for %r failed", msg.get("type"))

    # ---- raw HTTP/1.1 ----------------------------------------------------
    async def _request(
        self, method: str, path: str, payload: dict | None = None, timeout: float = 15.0
    ) -> tuple[int, dict]:
        """One request on a fresh connection; returns ``(status, json)``.

        Transport problems raise (``ConnectionError``/``TimeoutError``);
        an HTTP 4xx/5xx is a normal return so callers can read the
        protocol-shaped error body the server attached.
        """
        target = self.base_path + path
        host_hdr = self.host if (
            self.scheme == "http" and self.port == 80
        ) or (self.scheme == "https" and self.port == 443) else f"{self.host}:{self.port}"
        lines = [
            f"{method} {target} HTTP/1.1",
            f"Host: {host_hdr}",
            "Accept: application/json",
            "Connection: close",
        ]
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body

        ssl_ctx = ssl.create_default_context() if self.scheme == "https" else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ssl_ctx),
            timeout=timeout,
        )
        try:
            writer.write(request)
            await writer.drain()
            status, headers = await self._read_head(reader, timeout)
            data = await self._read_body(reader, headers, timeout)
            if not data:
                return status, {}
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.warning("non-JSON response (HTTP %d): %.120s", status, data)
                return status, {}
            return status, parsed if isinstance(parsed, dict) else {}
        except asyncio.TimeoutError:
            raise TimeoutError(f"{method} {target} timed out after {timeout:g}s") from None
        except (ConnectionError, OSError) as exc:
            raise ConnectionError(f"{method} {target} failed: {exc}") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _read_head(reader, timeout: float) -> tuple[int, dict]:
        status_line = await asyncio.wait_for(reader.readline(), timeout)
        if not status_line:
            raise ConnectionError("server closed the connection without a response")
        parts = status_line.decode("latin-1").split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise ConnectionError(f"malformed status line: {status_line[:80]!r}")
        try:
            status = int(parts[1])
        except ValueError:
            raise ConnectionError(f"malformed status code: {parts[1]!r}") from None
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            if not line:
                raise ConnectionError("truncated headers")
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        return status, headers

    @staticmethod
    async def _read_body(reader, headers: dict, timeout: float) -> bytes:
        if headers.get("transfer-encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size_line = await asyncio.wait_for(reader.readline(), timeout)
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while True:  # trailer headers, then the final CRLF
                        line = await asyncio.wait_for(reader.readline(), timeout)
                        if line in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(await asyncio.wait_for(reader.readexactly(size), timeout))
                await asyncio.wait_for(reader.readexactly(2), timeout)  # CRLF
            return b"".join(chunks)
        length = headers.get("content-length")
        if length is not None:
            try:
                n = int(length)
            except ValueError:
                raise ConnectionError(f"bad Content-Length: {length!r}") from None
            return await asyncio.wait_for(reader.readexactly(n), timeout) if n else b""
        # No framing at all: read to end of connection (we sent Connection: close).
        return await asyncio.wait_for(reader.read(), timeout)

    # ---- transport API (same shapes as MeshClient) ------------------------
    async def connect(self, nick: str, timeout: float = 10.0) -> None:
        """Join the mesh over HTTP.

        A rejected nick arrives as an ``error`` frame on TCP after the
        handshake; here the same frame is dispatched from the response
        so the UI can show it, then ``ConnectionError`` aborts startup
        just like a failed handshake would.
        """
        status, body = await self._request(
            "POST", "/api/hello", {"nick": nick}, timeout=max(1.0, timeout)
        )
        token = body.get("token") if status == 200 else None
        if not token:
            reason = body.get("reason") or f"HTTP {status} from {self.base_url}"
            await self._dispatch({"type": "error", "reason": reason})
            raise ConnectionError(reason)
        self.token = str(token)
        log.debug("joined as %s (node %s)", body.get("nick"), body.get("node_id"))

    async def say(self, to: str, body: str) -> None:
        await self._post("/api/send", {"token": self.token, "to": to, "body": body})

    async def wire(self, kind: str, a: str, b: str) -> None:
        """Snip (``kind="cut"``) or re-attach (``kind="link"``) a wire."""
        if kind not in ("cut", "link"):
            raise ValueError(f"unknown wire action: {kind}")
        await self._post("/api/wire", {"token": self.token, "action": kind, "a": a, "b": b})

    async def bye(self) -> None:
        try:
            await self._post("/api/bye", {"token": self.token}, check=True)
        except Exception:
            pass  # mirrors MeshClient.bye(): leaving is best-effort

    async def _post(self, path: str, payload: dict, check: bool = False) -> None:
        try:
            status, body = await self._request(
                "POST", path, payload, timeout=self.poll_wait + 10
            )
        except TimeoutError as exc:
            raise ConnectionError(str(exc)) from exc
        if status == 401 and check is False:
            # Session gone (server restart, idle reap): the event poll
            # will notice too -- but the caller deserves an instant nudge.
            # 4xx validation failures (404 no such node, 409 no route...)
            # are NOT this: they also arrive as streamed `error` events,
            # which is where the UI reads them from -- same as TCP.
            raise ConnectionError(body.get("reason") or "session expired")
        if status >= 500:
            raise ConnectionError(body.get("reason") or f"HTTP {status}")

    async def run(self) -> None:
        """Long-poll loop; returns when the stream ends or we are closed."""
        while not self._closed:
            path = (
                f"/api/events?token={self.token}"
                f"&since={self._since}&wait={self.poll_wait:g}"
            )
            try:
                status, body = await self._request(
                    "GET", path, None, timeout=self.poll_wait + 10
                )
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, TimeoutError) as exc:
                log.debug("event stream ended: %s", exc)
                break
            if status >= 400:
                log.info("event stream refused: %s", body.get("reason") or status)
                break
            events = body.get("events")
            if not isinstance(events, list):
                log.warning("malformed poll response: %.120s", body)
                break
            try:
                self._since = int(body.get("cursor", self._since + len(events)))
            except (TypeError, ValueError):
                self._since += len(events)
            for msg in events:
                if not isinstance(msg, dict):
                    continue
                await self._dispatch(msg)
        self._closed = True
        closed = self._handlers.get("_closed")
        if closed:
            try:
                await closed()
            except Exception:
                log.exception("close handler failed")

    def close(self) -> None:
        self._closed = True  # any in-flight long-poll dies with its task

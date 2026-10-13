"""Threaded HTTP front end for :mod:`.httpchat` (standard library only).

One small ``ThreadingHTTPServer`` maps the JSON endpoints described in
``httpchat`` onto the mesh engine, serves the browser UI at ``/``, and
does CORS so the page in ``templates/browser`` can run straight from
``file://``.  Run it with ``python3 server/http.py`` -- same flags as
``server/main.py``, plus ``--ui``.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .httpchat import ChatError, HttpChat
from .protocol import DEFAULT_HTTP_PORT

log = logging.getLogger("stserver.httpserv")

MAX_WAIT_S = 60.0  # longest a client may park a long-poll
DEFAULT_WAIT_S = 20.0

LANDING_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>simple-talk &middot; HTTP mesh server</title>
<style>
 body { font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
        background:#101418; color:#cdd6d4; max-width:46rem; margin:3rem auto; padding:0 1rem}
 h1 { color:#89b4fa; font-size:1.4rem} a {color:#89dceb}
 code,pre {background:#1c2128; padding:.15rem .4rem; border-radius:4px}
 td {padding:.2rem 1rem .2rem 0; vertical-align:top}
</style></head><body>
<h1>simple-talk &rarr; HTTP mesh server</h1>
<p>The mesh is up. You can talk to it with <code>curl</code>, a browser,
or the TUI client:</p>
<pre>python3 client/main.py --url http://127.0.0.1:__PORT__ --nick you</pre>
<table>
<tr><td><code>POST /api/hello</code></td><td>join the mesh <code>{"nick": "..."}</code></td></tr>
<tr><td><code>GET&nbsp; /api/events</code></td><td>long-poll events <code>?token=&amp;since=&amp;wait=</code></td></tr>
<tr><td><code>POST /api/send</code></td><td>broadcast/unicast <code>{"token","to","body"}</code></td></tr>
<tr><td><code>POST /api/wire</code></td><td>snip/reattach <code>{"token","action","a","b"}</code></td></tr>
<tr><td><code>POST /api/bye</code></td><td>leave <code>{"token"}</code></td></tr>
<tr><td><code>GET&nbsp; /api/ping</code></td><td>liveness check</td></tr>
</table>
<p>See <code>templates/README.md</code> for the full contract, and
<code>templates/browser/index.html</code> for a ready-made chat page.</p>
</body></html>
"""


def find_ui(explicit: str | None = None) -> bytes:
    """Locate the browser chat page, else fall back to a landing page."""
    here = Path(__file__).resolve()
    candidates = [Path(explicit)] if explicit else []
    candidates += [
        here.parent / "index.html",
        here.parents[2] / "templates" / "browser" / "index.html",
        Path.cwd() / "templates" / "browser" / "index.html",
        Path.cwd() / "index.html",
    ]
    for candidate in candidates:
        try:
            data = candidate.read_bytes()
            log.info("serving UI from %s", candidate)
            return data
        except OSError:
            continue
    if explicit:
        log.warning("--ui %s could not be read; serving the landing page instead", explicit)
    return LANDING_HTML.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    """Routes the handful of endpoints; every body is JSON."""

    protocol_version = "HTTP/1.1"
    server_version = f"simple-talk-http/{__version__}"
    timeout = 30  # reap sockets whose peer stalls before sending a request

    # -- plumbing ---------------------------------------------------------
    @property
    def chat(self) -> HttpChat:
        return self.server.chat

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type, authorization")
        self.send_header("Access-Control-Max-Age", "600")

    def _respond(self, status: int, blob: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(blob)

    def _json(self, status: int, payload: dict) -> None:
        self._respond(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _html(self, status: int, html: bytes) -> None:
        self._respond(status, html, "text/html; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ChatError(400, "bad Content-Length")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ChatError(400, "request body must be JSON")
        if not isinstance(data, dict):
            raise ChatError(400, "request body must be a JSON object")
        return data

    # -- verbs ------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # CORS preflight (browser templates)
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        try:
            if method == "GET":
                self._get(path, query)
            else:
                self._post(path)
        except ChatError as exc:
            self._json(exc.status, exc.payload)
        except (BrokenPipeError, ConnectionResetError):
            log.debug("peer hung up during %s %s", method, self.path)
        except Exception:
            log.exception("internal error on %s %s", method, self.path)
            try:
                self._json(500, {"type": "error", "reason": "internal server error"})
            except OSError:
                pass

    # -- GET routes ---------------------------------------------------------
    def _get(self, path: str, query: dict) -> None:
        if path in ("/", "/index.html"):
            self._html(200, self.server.ui_html)
        elif path == "/api/ping":
            self._json(200, {"type": "pong", "version": __version__})
        elif path == "/api/state":
            self._json(200, self.chat.state())
        elif path == "/api/events":
            token = _first(query, "token")
            since = _int(query, "since", 0)
            wait = min(_float(query, "wait", DEFAULT_WAIT_S), MAX_WAIT_S)
            session = self.chat.get_session(token)
            events, cursor = self.chat.events(session, since, wait)
            self._json(200, {"events": events, "cursor": cursor})
        else:
            raise ChatError(404, "no such endpoint")

    # -- POST routes ---------------------------------------------------------
    def _post(self, path: str) -> None:
        if path == "/api/hello":
            body = self._body()
            session = self.chat.hello(str(body.get("nick") or ""))
            self._json(
                200,
                {
                    "ok": True,
                    "token": session.token,
                    "node_id": session.node_id,
                    "nick": session.nick,
                },
            )
        elif path == "/api/send":
            body = self._body()
            session = self.chat.get_session(body.get("token"))
            self.chat.send(
                session,
                str(body.get("to") or "*"),
                str(body.get("body") or ""),
                body.get("id"),
            )
            self._json(200, {"ok": True})
        elif path == "/api/wire":
            body = self._body()
            session = self.chat.get_session(body.get("token"))
            self.chat.wire(
                session,
                str(body.get("action") or ""),
                str(body.get("a") or ""),
                str(body.get("b") or ""),
            )
            self._json(200, {"ok": True})
        elif path == "/api/bye":
            body = self._body()
            self.chat.close_session(str(body.get("token") or ""))
            self._json(200, {"ok": True})
        else:
            raise ChatError(404, "no such endpoint")


def _first(query: dict, key: str, default: str = "") -> str:
    values = query.get(key) or []
    return values[0] if values else default


def _int(query: dict, key: str, default: int) -> int:
    try:
        return int(_first(query, key, str(default)))
    except ValueError:
        return default


def _float(query: dict, key: str, default: float) -> float:
    try:
        return float(_first(query, key, str(default)))
    except ValueError:
        return default


class HttpChatServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple, chat: HttpChat, ui_html: bytes):
        self.chat = chat
        self.ui_html = ui_html
        super().__init__(addr, Handler)


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_HTTP_PORT,
    hop_ms: int = 120,
    seed: int | None = None,
    ui: str | None = None,
) -> None:
    from .server import lan_ipv4_addresses  # local import: socket helpers

    chat = HttpChat(hop_ms=hop_ms, seed=seed)
    chat.start()
    server = HttpChatServer((host, port), chat, find_ui(ui))
    bound_port = server.server_address[1]
    base = f"http://{host}:{bound_port}"
    log.info("http mesh server listening on %s", base)
    if host in ("0.0.0.0", "::", ""):
        for addr in lan_ipv4_addresses():
            log.info(
                "  reachable from other machines at http://%s:%s  "
                "(client: python3 client/main.py --url http://%s:%s)",
                addr,
                bound_port,
                addr,
                bound_port,
            )
        log.info(
            "if peers cannot connect, the network likely blocks device-to-device "
            "traffic (guest/school Wi-Fi client isolation) -- try a phone hotspot"
        )
    else:
        log.info(
            "bound to %s (loopback) -- other machines cannot reach it. "
            "Restart with --host 0.0.0.0 to accept LAN connections",
            host,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        chat.stop()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="simple-talk-http-server",
        description="mesh chat server over HTTP (same simulation as the TCP server)",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (default 127.0.0.1; use 0.0.0.0 so other machines can join)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="port (default %(default)s)",
    )
    p.add_argument(
        "--hop-ms",
        type=int,
        default=120,
        help="simulated per-hop latency in ms (default %(default)s)",
    )
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible topology")
    p.add_argument(
        "--ui",
        default=None,
        help="HTML file to serve at / (default: templates/browser/index.html if found)",
    )
    p.add_argument("--log-level", default="INFO", help="logging level (default %(default)s)")
    p.add_argument(
        "--version",
        action="version",
        version=f"simple-talk-http-server {__version__}",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        serve(args.host, args.port, hop_ms=args.hop_ms, seed=args.seed, ui=args.ui)
    except OSError as exc:
        log.error("could not listen on %s:%s -- %s", args.host, args.port, exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

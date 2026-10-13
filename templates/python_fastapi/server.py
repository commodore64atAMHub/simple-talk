#!/usr/bin/env python3
"""simple-talk over HTTP -- FastAPI edition.

The mesh chat described in templates/README.md behind a FastAPI app:
same endpoints, same JSON frames, same behaviour as every other server
in templates/ -- plus the free OpenAPI playground at /docs that FastAPI
hands you.

    pip install fastapi uvicorn
    python3 server.py --port 8080

Then, in other terminals:

    python3 client/main.py --url http://127.0.0.1:8080 --nick alice   # this repo's TUI
    open http://127.0.0.1:8080/                                       # browser chat page

Verify it with this repo's conformance test:

    python3 scripts/http_smoke_test.py --cmd "python3 templates/python_fastapi/server.py --port {port}"

A note on the plumbing: the mesh engine below is synchronous and
thread-based (a parked long-poll is just a thread waiting on a
condition variable), so every route that touches it runs through
``run_in_threadpool`` -- FastAPI stays async, the engine stays simple,
and nobody has to rewrite the relay loop as an async state machine.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import secrets
import socket
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("http-mesh")

GOLDEN_ANGLE = 2.3999632297286533  # radians -- node placement spiral
MAX_NICK = 24
MAX_BODY = 2048
DEFAULT_PORT = 8080
MAX_WAIT_S = 60.0
DEFAULT_WAIT_S = 20.0


# ======================================================================
# the mesh
# ======================================================================
class ChatError(Exception):
    """Maps onto an HTTP status plus a protocol-shaped JSON body."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason

    @property
    def payload(self) -> dict:
        return {"type": "error", "reason": self.reason}


class Mesh:
    """The simulated network: nodes on a golden spiral, BFS routing.

    Nodes link to +/-2 ring neighbours plus a few random shortcuts; manual
    ``cut``/``link`` edits overlay the automatic shape and survive every
    rebuild (so /dc and /cn keep their promise).
    """

    def __init__(self, ring_degree: int = 2, shortcuts: int = 3, rng=None):
        self.ring_degree = ring_degree
        self.shortcuts = shortcuts
        self.rng = rng or random.Random()
        self.nodes: dict[int, dict] = {}  # id -> {id, nick, x, y}
        self.adj: dict[int, set[int]] = {}
        self.order: list[int] = []
        self._cuts: set[tuple[int, int]] = set()
        self._links: set[tuple[int, int]] = set()

    @staticmethod
    def place(node_id: int) -> tuple[float, float]:
        r = 3.0 * (node_id + 1) ** 0.5
        a = node_id * GOLDEN_ANGLE
        return round(r * math.cos(a), 1), round(r * math.sin(a), 1)

    def add(self, node_id: int, nick: str) -> dict:
        self.order.append(node_id)
        x, y = self.place(node_id)
        self.nodes[node_id] = {"id": node_id, "nick": nick, "x": x, "y": y}
        self.rebuild()
        return self.nodes[node_id]

    def remove(self, node_id: int) -> None:
        self.nodes.pop(node_id, None)
        if node_id in self.order:
            self.order.remove(node_id)
        self._cuts = {p for p in self._cuts if node_id not in p}
        self._links = {p for p in self._links if node_id not in p}
        self.rebuild()

    def rebuild(self) -> None:
        ids, live = self.order, set(self.order)
        if self._cuts or self._links:
            # Someone is sculpting the mesh: keep their shape, only wire
            # newcomers to the most recent member (they can /dc it away).
            adj, fresh = {}, []
            for i in ids:
                if i in self.adj:
                    adj[i] = {j for j in self.adj[i] if j in live}
                else:
                    adj[i] = set()
                    fresh.append(i)
            if len(ids) > 1:
                for i in fresh:
                    prev = [j for j in ids if j != i][-1]
                    adj[i].add(prev)
                    adj[prev].add(i)
        else:
            adj = {i: set() for i in ids}
            n = len(ids)
            if n > 1:
                k = min(self.ring_degree, n - 1)
                for idx, i in enumerate(ids):
                    for d in range(1, k + 1):
                        adj[i].add(ids[(idx - d) % n])
                        adj[i].add(ids[(idx + d) % n])
                existing = sum(len(s) for s in adj.values()) // 2
                extra = max(0, min(self.shortcuts, n * (n - 1) // 2 - existing))
                for _ in range(extra):
                    a, b = self.rng.sample(ids, 2)
                    adj[a].add(b)
                    adj[b].add(a)
        for a, b in self._cuts:  # manual edits win over the auto shape
            if a in adj and b in adj:
                adj[a].discard(b)
                adj[b].discard(a)
        for a, b in self._links:
            if a in adj and b in adj:
                adj[a].add(b)
                adj[b].add(a)
        self.adj = {i: frozenset(s) for i, s in adj.items()}

    def cut(self, a: int, b: int) -> bool:
        if a == b or b not in self.adj.get(a, ()):
            return False
        pair = (a, b) if a <= b else (b, a)
        self._cuts.add(pair)
        self._links.discard(pair)
        self.rebuild()
        return True

    def link(self, a: int, b: int) -> bool:
        if a == b or a not in self.nodes or b not in self.nodes:
            return False
        if b in self.adj.get(a, ()):
            return False
        pair = (a, b) if a <= b else (b, a)
        self._links.add(pair)
        self._cuts.discard(pair)
        self.rebuild()
        return True

    def peers_of(self, node_id: int) -> list[int]:
        return sorted(self.adj.get(node_id, ()))

    def nick_of(self, node_id: int) -> str:
        node = self.nodes.get(node_id)
        return node["nick"] if node else f"node{node_id}"

    def route(self, src: int, dst: int) -> list[int] | None:
        """Shortest path (BFS), src and dst inclusive; None when cut off."""
        if src not in self.adj or dst not in self.adj:
            return None
        if src == dst:
            return [src]
        prev = {src: None}
        queue = deque([src])
        while queue:
            cur = queue.popleft()
            for nxt in self.adj[cur]:
                if nxt not in prev:
                    prev[nxt] = cur
                    if nxt == dst:
                        path = [dst]
                        while prev[path[-1]] is not None:
                            path.append(prev[path[-1]])
                        path.reverse()
                        return path
                    queue.append(nxt)
        return None

    def node_map(self) -> list[dict]:
        return [
            {**self.nodes[i], "peers": self.peers_of(i)} for i in self.order
        ]


# ======================================================================
# the chat (sessions, events, relay animation)
# ======================================================================
class Session:
    """One HTTP client's foothold in the mesh: a node plus an event queue."""

    __slots__ = ("token", "nick", "node_id", "events", "last_seen")

    def __init__(self, token: str, nick: str, node_id: int):
        self.token = token
        self.nick = nick
        self.node_id = node_id
        self.events: list[dict] = []
        self.last_seen = time.monotonic()


class HttpChat:
    def __init__(self, hop_ms: int = 120, seed: int | None = None,
                 session_ttl: float = 90.0, max_events: int = 10000):
        self.hop_base = max(5, hop_ms) / 1000.0
        self.session_ttl = session_ttl
        self.max_events = max_events
        self.rng = random.Random(seed)
        self.mesh = Mesh(rng=self.rng)
        self.by_nick: dict[str, int] = {}
        self.next_id = 1
        self.sessions: dict[str, Session] = {}
        self.by_node: dict[int, Session] = {}
        self.closed = False
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._janitor, name="session-janitor", daemon=True).start()

    def stop(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def _janitor(self) -> None:
        """Drop sessions whose browser/client stopped polling."""
        while not self.closed:
            time.sleep(10)
            with self.cond:
                now = time.monotonic()
                stale = [s for s in list(self.sessions.values())
                         if now - s.last_seen > self.session_ttl]
            for session in stale:
                log.info("%s idle for %.0fs -- dropping the session",
                         session.nick, self.session_ttl)
                self.close_session(session.token)

    @staticmethod
    def _key(nick: str) -> str:
        return nick.casefold()

    # -- sessions ---------------------------------------------------------
    def get_session(self, token) -> Session:
        with self.cond:
            session = self.sessions.get(str(token or ""))
            if session is None:
                raise ChatError(401, "unknown or expired session")
            session.last_seen = time.monotonic()
            return session

    def hello(self, nick: str) -> Session:
        nick = (nick or "").strip()
        with self.cond:
            if not (1 <= len(nick) <= MAX_NICK):
                raise ChatError(400, f"nick must be 1-{MAX_NICK} characters")
            if not nick.isprintable():
                raise ChatError(400, "nick has unprintable characters")
            if self._key(nick) in self.by_nick:
                raise ChatError(409, f"nick already in use: {nick}")
            node_id = self.next_id
            self.next_id += 1
            node = self.mesh.add(node_id, nick)
            self.by_nick[self._key(nick)] = node_id
            session = Session(secrets.token_urlsafe(18), nick, node_id)
            self.sessions[session.token] = session
            self.by_node[node_id] = session
            log.info("node %d joined as %r over http", node_id, nick)
            # Same order as the TCP server: topology first, then the
            # joiner's welcome, then the join notice to everyone else.
            self._announce()
            self._push(session, {
                "type": "welcome",
                "self": {k: node[k] for k in ("id", "nick", "x", "y")},
                "nodes": self.mesh.node_map(),
                "peers": self.mesh.peers_of(node_id),
            })
            join_ev = {"type": "node_join", "node": {"id": node_id, "nick": nick}}
            for other in list(self.sessions.values()):
                if other.token != session.token:
                    self._push(other, join_ev)
            self.cond.notify_all()
        return session

    def close_session(self, token: str) -> bool:
        with self.cond:
            session = self.sessions.pop(str(token or ""), None)
            if session is None:
                return False
            self.by_node.pop(session.node_id, None)
            self.cond.notify_all()
            left = self._drop_node(session.node_id)
            if left is not None:
                self._announce_leave({"id": session.node_id, "nick": left})
            log.info("%r left; %d remain", session.nick, len(self.sessions))
            return True

    def _drop_node(self, node_id: int) -> str | None:
        if node_id not in self.mesh.nodes:
            return None
        nick = self.mesh.nick_of(node_id)
        self.mesh.remove(node_id)
        if self._key(nick) in self.by_nick:
            del self.by_nick[self._key(nick)]
        return nick

    def _announce_leave(self, node: dict) -> None:
        leave_ev = {"type": "node_leave", "node": node}
        for session in list(self.sessions.values()):
            self._push(session, leave_ev)
        self._announce()

    # -- event fan-out ----------------------------------------------------
    def _push(self, session: Session | None, event: dict) -> None:
        with self.cond:
            if session is None or self.sessions.get(session.token) is not session:
                return  # departed session: drop on the floor
            session.events.append(event)
            if len(session.events) > self.max_events:
                # Nobody is draining this queue -- let go of the client
                # instead of growing the list forever.
                log.warning("%s fell %d events behind -- dropping the session",
                            session.nick, len(session.events))
                self._close_overflow(session)
                return
            self.cond.notify_all()

    def _close_overflow(self, session: Session) -> None:
        self.sessions.pop(session.token, None)
        self.by_node.pop(session.node_id, None)
        left = self._drop_node(session.node_id)
        if left is not None:
            self._announce_leave({"id": session.node_id, "nick": left})
        self.cond.notify_all()

    def _push_all(self, event: dict) -> None:
        for session in list(self.sessions.values()):
            self._push(session, event)

    def _push_node(self, node_id: int, event: dict) -> None:
        self._push(self.by_node.get(node_id), event)

    def _announce(self) -> None:
        """Neighbour lists first, then the full map (same as server.py)."""
        order = list(self.mesh.order)
        for nid in order:
            self._push_node(nid, {"type": "peers", "peers": self.mesh.peers_of(nid)})
        map_ev = {"type": "node_map", "nodes": self.mesh.node_map()}
        for nid in order:
            self._push_node(nid, map_ev)

    # -- long-poll ---------------------------------------------------------
    def events(self, session: Session, since: int, wait_s: float) -> tuple[list[dict], int]:
        deadline = time.monotonic() + max(0.0, wait_s)
        with self.cond:
            if session.token not in self.sessions:
                raise ChatError(401, "unknown or expired session")
            session.last_seen = time.monotonic()
            while len(session.events) <= since and not self.closed:
                if session.token not in self.sessions:
                    raise ChatError(401, "unknown or expired session")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(min(remaining, 1.0))  # also re-check drops
            if session.token not in self.sessions:
                raise ChatError(401, "unknown or expired session")
            tail = session.events[since:]
            return tail, since + len(tail)

    # -- chat traffic -------------------------------------------------------
    def send(self, session: Session, to, body, msg_id=None) -> None:
        body = str(body or "")[:MAX_BODY]
        if not body.strip():
            return
        target = str(to or "*")
        msg_id = (msg_id or uuid.uuid4().hex)[:16]
        with self.cond:
            if session.token not in self.sessions:
                raise ChatError(401, "unknown or expired session")
            src_id, src_nick = session.node_id, session.nick
            if target == "*":
                recipients = [(nid, self.mesh.nick_of(nid))
                              for nid in list(self.mesh.order) if nid != src_id]
                job, args = self._broadcast, (src_id, src_nick, body, msg_id, recipients)
            else:
                dst_id = self.by_nick.get(self._key(target))
                if dst_id is None:
                    raise self._streamed_error(session, msg_id, 404, f"no such node: {target}")
                if dst_id not in self.mesh.nodes:
                    raise self._streamed_error(session, msg_id, 409, "target not connected")
                path = self.mesh.route(src_id, dst_id)
                if not path:
                    raise self._streamed_error(session, msg_id, 409, "no route through the mesh")
                names = [self.mesh.nick_of(i) for i in path]
                job = self._relay
                args = (src_id, src_nick, dst_id, target, body, msg_id, path, names)
        threading.Thread(target=job, args=args, name=f"send-{msg_id}", daemon=True).start()

    def _streamed_error(self, session, msg_id, status: int, reason: str) -> ChatError:
        """Failures reach the TUI as an `error` event (like the TCP
        server writes them to the socket), plus as a real HTTP status
        for curl."""
        self._push(session, {"type": "error", "id": msg_id, "reason": reason})
        return ChatError(status, reason)

    def _hop_delay(self) -> float:
        with self.cond:
            return self.hop_base * (0.7 + self.rng.random() * 0.6)

    def _relay(self, src_id, src_nick, dst_id, dst_nick, body, msg_id, path, names) -> None:
        hops = len(path) - 1
        for idx in range(1, hops + 1):
            time.sleep(self._hop_delay())
            with self.cond:
                if self.closed:
                    return
                # relay ticks are public: every node animates the packet
                self._push_all({"type": "relay", "id": msg_id, "from": src_nick,
                                "to": dst_nick, "hop": idx, "hops": hops,
                                "via": self.mesh.nick_of(path[idx])})
        with self.cond:
            self._push_node(dst_id, {"type": "message", "id": msg_id, "from": src_nick,
                                     "to": dst_nick, "body": body, "hops": hops,
                                     "route": names})
            self._push_node(src_id, {"type": "ack", "id": msg_id, "to": dst_nick,
                                     "hops": hops, "route": names})
            log.info("relayed %d bytes %s -> %s (%d hop(s))",
                     len(body), src_nick, dst_nick, hops)

    def _broadcast(self, src_id, src_nick, body, msg_id, recipients) -> None:
        for nid, nick in recipients:
            time.sleep(self._hop_delay())
            with self.cond:
                if self.closed:
                    return
                self._push_node(nid, {"type": "message", "id": msg_id, "from": src_nick,
                                      "to": "*", "body": body, "hops": 1,
                                      "route": [src_nick, nick]})
        with self.cond:
            self._push_node(src_id, {"type": "ack", "id": msg_id, "to": "*",
                                     "hops": 1, "route": [src_nick]})
            log.info("broadcast from %s to %d node(s)", src_nick, len(recipients))

    def wire(self, session: Session, action, a_nick, b_nick) -> None:
        if action not in ("cut", "link"):
            raise ChatError(400, f"unknown wire action: {action}")
        a_nick, b_nick = str(a_nick or "").strip(), str(b_nick or "").strip()
        with self.cond:
            if session.token not in self.sessions:
                raise ChatError(401, "unknown or expired session")
            ia, ib = self.by_nick.get(self._key(a_nick)), self.by_nick.get(self._key(b_nick))
            if ia is None or ib is None:
                missing = a_nick if ia is None else b_nick
                raise self._streamed_error(session, None, 404, f"no such node: {missing}")
            if ia == ib:
                raise self._streamed_error(session, None, 400, "a wire needs two different nodes")
            if action == "cut":
                ok, failure = self.mesh.cut(ia, ib), f"no such wire: {a_nick} <-> {b_nick}"
            else:
                ok, failure = self.mesh.link(ia, ib), f"wire already exists: {a_nick} <-> {b_nick}"
            if not ok:
                raise self._streamed_error(session, None, 409, failure)
            wire_ev = {"type": "wire", "action": action, "a": a_nick,
                       "b": b_nick, "by": session.nick}
            # Snip text: operator only.  Re-attach: both ends too.
            targets = {session.token: session}
            if action == "link":
                for nid in (ia, ib):
                    other = self.by_node.get(nid)
                    if other is not None:
                        targets[other.token] = other
            for target in targets.values():
                self._push(target, wire_ev)
            self._announce()
            log.info("wire %s <-> %s %s by %s", a_nick, b_nick,
                     "snipped" if action == "cut" else "linked", session.nick)

    def state(self) -> dict:
        with self.cond:
            return {"nodes": self.mesh.node_map(), "sessions": len(self.sessions)}


LANDING_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>simple-talk HTTP server</title>
<style>body{font:15px/1.6 ui-monospace,monospace;background:#101418;color:#cdd6d4;
max-width:44rem;margin:3rem auto;padding:0 1rem}code{background:#1c2128;padding:.1rem .4rem;
border-radius:4px}h1{color:#89b4fa}</style></head><body>
<h1>simple-talk &rarr; HTTP mesh server</h1>
<p>Drop <code>templates/browser/index.html</code> next to this file (or just open it
from the repo) for a chat page, or use the TUI client:</p>
<p><code>python3 client/main.py --url http://127.0.0.1:__PORT__ --nick you</code></p>
<p>Full endpoint contract: <code>templates/README.md</code>.</p>
</body></html>
"""


def find_ui(explicit: str | None) -> bytes:
    here = Path(__file__).resolve()
    candidates = [Path(explicit)] if explicit else []
    candidates += [here.parent / "index.html", here.parent / ".." / "browser" / "index.html",
                   Path.cwd() / "templates" / "browser" / "index.html",
                   Path.cwd() / "index.html"]
    for candidate in candidates:
        try:
            return candidate.read_bytes()
        except OSError:
            continue
    if explicit:
        log.warning("--ui %s not found; serving the landing page", explicit)
    return LANDING_HTML.encode()


def lan_ipv4() -> list[str]:
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET,
                                        socket.SOCK_STREAM):
            addrs.add(info[4][0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(a for a in addrs if not a.startswith(("127.", "169.254.")))


# ======================================================================
# the FastAPI app (everything above is framework-agnostic)
# ======================================================================
UI_HTML: bytes = b""
CHAT: HttpChat | None = None  # built by serve() before any request lands


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Every parked long-poll holds a worker thread; anyio's default
    # budget (40) is a bit tight for a room full of chat clients.
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = 256
    yield


app = FastAPI(lifespan=lifespan, title="simple-talk HTTP mesh")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "authorization"],
    max_age=600,
)


def body_json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ChatError(400, "request body must be JSON")
    if not isinstance(data, dict):
        raise ChatError(400, "request body must be a JSON object")
    return data


@app.exception_handler(ChatError)
async def chat_error(request: Request, exc: ChatError):
    return JSONResponse(exc.payload, status_code=exc.status)


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    log.exception("internal error on %s %s", request.method, request.url.path)
    return JSONResponse({"type": "error", "reason": "internal server error"},
                        status_code=500)


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index():
    return UI_HTML


@app.get("/api/ping")
async def ping():
    return {"type": "pong", "version": "fastapi"}


@app.get("/api/state")
async def state():
    return await run_in_threadpool(CHAT.state)


@app.get("/api/events")
async def events(request: Request):
    """Long-poll: parks a worker thread until an event or the timeout."""
    args = request.query_params
    session = await run_in_threadpool(CHAT.get_session, args.get("token", ""))
    try:
        since = int(args.get("since", "0"))
    except ValueError:
        since = 0
    try:
        wait = min(float(args.get("wait", DEFAULT_WAIT_S)), MAX_WAIT_S)
    except ValueError:
        wait = DEFAULT_WAIT_S
    tail, cursor = await run_in_threadpool(CHAT.events, session, since, wait)
    return {"events": tail, "cursor": cursor}


@app.post("/api/hello")
async def hello(request: Request):
    data = body_json(await request.body())
    session = await run_in_threadpool(CHAT.hello, str(data.get("nick") or ""))
    return {"ok": True, "token": session.token,
            "node_id": session.node_id, "nick": session.nick}


@app.post("/api/send")
async def send(request: Request):
    data = body_json(await request.body())
    session = await run_in_threadpool(CHAT.get_session, data.get("token"))
    await run_in_threadpool(CHAT.send, session, data.get("to"),
                            data.get("body"), data.get("id"))
    return {"ok": True}


@app.post("/api/wire")
async def wire(request: Request):
    data = body_json(await request.body())
    session = await run_in_threadpool(CHAT.get_session, data.get("token"))
    await run_in_threadpool(CHAT.wire, session, str(data.get("action") or ""),
                            str(data.get("a") or ""), str(data.get("b") or ""))
    return {"ok": True}


@app.post("/api/bye")
async def bye(request: Request):
    data = body_json(await request.body())
    await run_in_threadpool(CHAT.close_session, str(data.get("token") or ""))
    return {"ok": True}


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, hop_ms: int = 120,
          seed: int | None = None, ui: str | None = None) -> None:
    global CHAT, UI_HTML
    CHAT = HttpChat(hop_ms=hop_ms, seed=seed)
    CHAT.start()
    UI_HTML = find_ui(ui)
    log.info("http mesh server (fastapi) listening on http://%s:%s", host, port)
    if host in ("0.0.0.0", "::", ""):
        for addr in lan_ipv4():
            log.info("  reachable at http://%s:%s  (client: --url http://%s:%s)",
                     addr, port, addr, port)
    else:
        log.info("bound to %s (loopback) -- restart with --host 0.0.0.0 for LAN use", host)
    try:
        # access_log off: every long-poll would be a line of noise.
        uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    finally:
        CHAT.stop()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="server.py",
                                description="simple-talk mesh chat over HTTP (FastAPI)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind host (default 127.0.0.1; 0.0.0.0 for the LAN)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default %(default)s)")
    p.add_argument("--hop-ms", type=int, default=120,
                   help="simulated per-hop latency in ms (default %(default)s)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible topology")
    p.add_argument("--ui", default=None, help="HTML file to serve at /")
    p.add_argument("--log-level", default="INFO", help="logging level (default %(default)s)")
    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
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

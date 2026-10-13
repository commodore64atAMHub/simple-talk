"""The mesh simulation behind the HTTP transport.

``server.py`` gives every node one long-lived TCP socket and streams
newline-delimited JSON frames down it.  That is lovely until you want to
poke the mesh from curl, a browser, or a language where wrestling a raw
socket by hand is a chore.  This module runs the *identical* mesh
simulation (same topology, BFS routing, hop-by-hop relay animation and
wire snipping) behind a handful of tiny HTTP endpoints:

``POST /api/hello``   join the mesh, get a session token
``GET  /api/events``  long-poll this node's event stream -- the very
                      frames the TCP server would have pushed
                      (welcome, peers, node_map, node_join, node_leave,
                      relay, message, ack, wire, error)
``POST /api/send``    broadcast (``to: "*"``) or unicast a message
``POST /api/wire``    snip (``cut``) or re-attach (``link``) a wire
``POST /api/bye``     leave the mesh

Because the events are the same dicts as on the wire, the client only
needs a different transport (``stclient.httpnet``) and the TUI never
learns the difference.

Threading model: HTTP is request/response, so every request runs on its
own thread (``ThreadingHTTPServer``).  One lock + one condition guard
all chat state; relay threads sleep *outside* the lock so a slow hop
never stalls a long-poll.
"""

from __future__ import annotations

import logging
import random
import secrets
import threading
import time
import uuid

from .protocol import MAX_BODY, MAX_NICK
from .mesh import Mesh

log = logging.getLogger("stserver.http")


class ChatError(Exception):
    """A failure that maps onto an HTTP status plus a protocol-shaped body."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason

    @property
    def payload(self) -> dict:
        return {"type": "error", "reason": self.reason}


class Session:
    """One HTTP client's foothold in the mesh: a node plus an event queue."""

    __slots__ = ("token", "nick", "node_id", "events", "last_seen")

    def __init__(self, token: str, nick: str, node_id: int):
        self.token = token
        self.nick = nick
        self.node_id = node_id
        self.events: list[dict] = []  # ordered, cursor-addressed by index
        self.last_seen = time.monotonic()


class HttpChat:
    """Mesh coordinator for HTTP clients.  See the module docstring."""

    def __init__(
        self,
        hop_ms: int = 120,
        seed: int | None = None,
        session_ttl: float = 90.0,
        max_events: int = 10000,
    ):
        self.hop_base = max(5, hop_ms) / 1000.0
        self.session_ttl = session_ttl
        self.max_events = max_events
        self.rng = random.Random(seed)
        self.mesh = Mesh(rng=self.rng)  # shares the RNG, like ChatServer
        self._by_nick: dict[str, int] = {}
        self._next_id = 1
        self._sessions: dict[str, Session] = {}
        self._by_node: dict[int, Session] = {}
        self._closed = False
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self._janitor: threading.Thread | None = None

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Start reaping sessions whose client stopped polling."""
        self._janitor = threading.Thread(
            target=self._janitor_loop, name="http-session-janitor", daemon=True
        )
        self._janitor.start()

    def stop(self) -> None:
        with self.cond:
            self._closed = True
            self.cond.notify_all()

    @staticmethod
    def _nick_key(nick: str) -> str:
        return nick.casefold()

    # ---- janitor --------------------------------------------------------
    def _janitor_loop(self) -> None:
        while not self._closed:
            time.sleep(10)
            stale = []
            with self.cond:
                now = time.monotonic()
                for session in list(self._sessions.values()):
                    if now - session.last_seen > self.session_ttl:
                        stale.append(session)
            for session in stale:
                log.info(
                    "session %s idle for %.0fs -- dropping it",
                    session.nick,
                    self.session_ttl,
                )
                self.close_session(session.token)

    # ---- sessions -------------------------------------------------------
    def get_session(self, token: str | None) -> Session:
        """Look up a live session (touching its idle timer) or raise 401."""
        with self.cond:
            session = self._sessions.get(str(token or ""))
            if session is None:
                raise ChatError(401, "unknown or expired session")
            session.last_seen = time.monotonic()
            return session

    def hello(self, nick: str) -> Session:
        """Join the mesh.  Raises :class:`ChatError` on a bad/taken nick.

        The caller gets back a session; the ``welcome`` frame itself
        travels through the event queue (first poll of ``/api/events``),
        exactly like the TCP server pushes it after the handshake.
        """
        nick = (nick or "").strip()
        with self.cond:
            if not (1 <= len(nick) <= MAX_NICK):
                raise ChatError(400, f"nick must be 1-{MAX_NICK} characters")
            if not nick.isprintable():
                raise ChatError(400, "nick has unprintable characters")
            if self._nick_key(nick) in self._by_nick:
                raise ChatError(409, f"nick already in use: {nick}")

            node_id = self._next_id
            self._next_id += 1
            self.mesh.add(node_id, nick, None)
            self._by_nick[self._nick_key(nick)] = node_id
            session = Session(secrets.token_urlsafe(18), nick, node_id)
            self._sessions[session.token] = session
            self._by_node[node_id] = session
            log.info("node %d joined as %r over http", node_id, nick)

            # Same order as the TCP server: topology first, then the
            # joiner's welcome snapshot, then the join notice to others.
            self._announce_locked()
            node = self.mesh.nodes[node_id]
            self._push(
                session,
                {
                    "type": "welcome",
                    "self": {"id": node_id, "nick": nick, "x": node.x, "y": node.y},
                    "nodes": self.mesh.node_map(),
                    "peers": self.mesh.peers_of(node_id),
                },
            )
            join_ev = {"type": "node_join", "node": {"id": node_id, "nick": nick}}
            for other in list(self._sessions.values()):
                if other.token != session.token:
                    self._push(other, join_ev)
            self.cond.notify_all()
        return session

    def close_session(self, token: str) -> bool:
        """Say goodbye: drop the node and announce the departure."""
        with self.cond:
            return self._close_session_locked(token)

    def _close_session_locked(self, token: str) -> bool:
        session = self._sessions.pop(token, None)
        if session is None:
            return False
        self._by_node.pop(session.node_id, None)
        self.cond.notify_all()
        left = self._drop_node_locked(session.node_id)
        if left is not None:
            self._announce_leave_locked({"id": session.node_id, "nick": left})
        log.info("session for %r left; %d remain", session.nick, len(self._sessions))
        return True

    def _drop_node_locked(self, node_id: int) -> str | None:
        if node_id not in self.mesh.nodes:
            return None
        nick = self.mesh.nick_of(node_id)
        self.mesh.remove(node_id)
        if self._nick_key(nick) in self._by_nick:
            del self._by_nick[self._nick_key(nick)]
        return nick

    def _announce_leave_locked(self, node: dict) -> None:
        leave_ev = {"type": "node_leave", "node": node}
        for session in list(self._sessions.values()):
            self._push(session, leave_ev)
        self._announce_locked()

    # ---- event fan-out --------------------------------------------------
    def _push(self, session: Session | None, event: dict) -> None:
        """Queue ``event`` for one session (no-op if it has left)."""
        with self.cond:
            if session is None or self._sessions.get(session.token) is not session:
                return
            session.events.append(event)
            if len(session.events) > self.max_events:
                # Nobody is draining this queue (the client vanished or
                # fell hopelessly behind).  Drop it rather than grow the
                # list forever; the transport reports the disconnect.
                log.warning(
                    "%s is %d events behind -- dropping the session",
                    session.nick,
                    len(session.events),
                )
                self._close_session_locked(session.token)
                return
            self.cond.notify_all()

    def _push_all(self, event: dict) -> None:
        for session in list(self._sessions.values()):
            self._push(session, event)

    def _push_node(self, node_id: int, event: dict) -> None:
        self._push(self._by_node.get(node_id), event)

    def _announce_locked(self) -> None:
        """Push neighbour lists, then the full node map, to every node."""
        order = list(self.mesh._order)
        for nid in order:
            self._push_node(nid, {"type": "peers", "peers": self.mesh.peers_of(nid)})
        # Shared dict on purpose: events are never mutated after push.
        nodes_ev = {"type": "node_map", "nodes": self.mesh.node_map()}
        for nid in order:
            self._push_node(nid, nodes_ev)

    # ---- event delivery (long-poll) -------------------------------------
    def events(self, session: Session, since: int, wait_s: float) -> tuple[list[dict], int]:
        """Return events after cursor ``since``, waiting up to ``wait_s``.

        Returns ``(events, new_cursor)``; an empty list means the wait
        timed out (the client should just ask again).
        """
        deadline = time.monotonic() + max(0.0, wait_s)
        with self.cond:
            if session.token not in self._sessions:
                raise ChatError(401, "unknown or expired session")
            session.last_seen = time.monotonic()
            while len(session.events) <= since and not self._closed:
                if session.token not in self._sessions:
                    raise ChatError(401, "unknown or expired session")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Wake periodically as well, so a dropped session or a
                # server shutdown is noticed even with no new events.
                self.cond.wait(min(remaining, 1.0))
            if session.token not in self._sessions:
                raise ChatError(401, "unknown or expired session")
            tail = session.events[since:]
            return tail, since + len(tail)

    # ---- chat traffic ---------------------------------------------------
    def send(self, session: Session, to: str, body: str, msg_id: str | None = None) -> None:
        """Broadcast or unicast.  Validation is synchronous (so curl gets
        a real status code); the hop-by-hop relay runs on its own thread
        so this returns immediately, like the TCP ``send`` frame does.
        """
        body = str(body or "")[:MAX_BODY]
        if not body.strip():
            return
        target = str(to or "*")
        msg_id = (msg_id or uuid.uuid4().hex)[:16]
        with self.cond:
            if session.token not in self._sessions:
                raise ChatError(401, "unknown or expired session")
            src_id, src_nick = session.node_id, session.nick
            if target == "*":
                recipients = [
                    (nid, self.mesh.nick_of(nid))
                    for nid in list(self.mesh._order)
                    if nid != src_id
                ]
                job_args = (src_id, src_nick, body, msg_id, recipients)
                job = self._broadcast_job
            else:
                dst_id = self._by_nick.get(self._nick_key(target))
                if dst_id is None:
                    raise self._streamed_error(
                        session, msg_id, 404, f"no such node: {target}"
                    )
                if dst_id not in self.mesh.nodes:
                    raise self._streamed_error(
                        session, msg_id, 409, "target not connected"
                    )
                route = self.mesh.route(src_id, dst_id)
                if not route:
                    raise self._streamed_error(
                        session, msg_id, 409, "no route through the mesh"
                    )
                names = [self.mesh.nick_of(i) for i in route]
                job_args = (src_id, src_nick, dst_id, target, body, msg_id, route, names)
                job = self._relay_job
        thread = threading.Thread(
            target=job, args=job_args, name=f"mesh-send-{msg_id}", daemon=True
        )
        thread.start()

    def _streamed_error(
        self, session: Session, msg_id: str, status: int, reason: str
    ) -> ChatError:
        """Also deliver the failure as an ``error`` event, the way the
        TCP server writes it to the socket -- the TUI reads it from the
        stream, not from the HTTP status."""
        self._push(session, {"type": "error", "id": msg_id, "reason": reason})
        return ChatError(status, reason)

    def _hop_delay(self) -> float:
        with self.cond:
            return self.hop_base * (0.7 + self.rng.random() * 0.6)

    def _relay_job(
        self,
        src_id: int,
        src_nick: str,
        dst_id: int,
        dst_nick: str,
        body: str,
        msg_id: str,
        route: list[int],
        names: list[str],
    ) -> None:
        """Store-and-forward a unicast packet through the mesh graph."""
        hops = len(route) - 1
        # A relay tick goes to EVERY node (sender included) so every
        # client's mesh view can animate the packet -- same as TCP.
        for idx in range(1, hops + 1):
            time.sleep(self._hop_delay())
            with self.cond:
                if self._closed:
                    return
                self._push_all(
                    {
                        "type": "relay",
                        "id": msg_id,
                        "from": src_nick,
                        "to": dst_nick,
                        "hop": idx,
                        "hops": hops,
                        "via": self.mesh.nick_of(route[idx]),
                    }
                )
        with self.cond:
            self._push_node(
                dst_id,
                {
                    "type": "message",
                    "id": msg_id,
                    "from": src_nick,
                    "to": dst_nick,
                    "body": body,
                    "hops": hops,
                    "route": names,
                },
            )
            self._push_node(
                src_id,
                {
                    "type": "ack",
                    "id": msg_id,
                    "to": dst_nick,
                    "hops": hops,
                    "route": names,
                },
            )
            log.info(
                "relayed %d bytes %s -> %s (%d hop(s))",
                len(body),
                src_nick,
                dst_nick,
                hops,
            )

    def _broadcast_job(
        self,
        src_id: int,
        src_nick: str,
        body: str,
        msg_id: str,
        recipients: list[tuple[int, str]],
    ) -> None:
        """Flood a message to every other node, one simulated hop at a time."""
        for nid, nick in recipients:
            time.sleep(self._hop_delay())
            with self.cond:
                if self._closed:
                    return
                self._push_node(
                    nid,
                    {
                        "type": "message",
                        "id": msg_id,
                        "from": src_nick,
                        "to": "*",
                        "body": body,
                        "hops": 1,
                        "route": [src_nick, nick],
                    },
                )
        with self.cond:
            self._push_node(
                src_id,
                {
                    "type": "ack",
                    "id": msg_id,
                    "to": "*",
                    "hops": 1,
                    "route": [src_nick],
                },
            )
            log.info(
                "broadcast from %s to %d node(s)", src_nick, len(recipients)
            )

    def wire(self, session: Session, action: str, a_nick: str, b_nick: str) -> None:
        """Snip (``cut``) or re-attach (``link``) a wire between two nodes."""
        if action not in ("cut", "link"):
            raise ChatError(400, f"unknown wire action: {action}")
        a_nick = str(a_nick or "").strip()
        b_nick = str(b_nick or "").strip()
        with self.cond:
            if session.token not in self._sessions:
                raise ChatError(401, "unknown or expired session")
            ia = self._by_nick.get(self._nick_key(a_nick))
            ib = self._by_nick.get(self._nick_key(b_nick))
            if ia is None or ib is None:
                missing = a_nick if ia is None else b_nick
                raise self._streamed_error(session, "", 404, f"no such node: {missing}")
            if ia == ib:
                raise self._streamed_error(
                    session, "", 400, "a wire needs two different nodes"
                )
            if action == "cut":
                ok = self.mesh.cut(ia, ib)
                failure = f"no such wire: {a_nick} <-> {b_nick}"
            else:
                ok = self.mesh.link(ia, ib)
                failure = f"wire already exists: {a_nick} <-> {b_nick}"
            if not ok:
                raise self._streamed_error(session, "", 409, failure)

            wire_ev = {
                "type": "wire",
                "action": action,
                "a": a_nick,
                "b": b_nick,
                "by": session.nick,
            }
            # The snip text goes to the operator alone; a re-attach also
            # reaches both ends of the wire (see server.py -- same rule).
            targets = {session.token: session}
            if action == "link":
                for nid in (ia, ib):
                    other = self._by_node.get(nid)
                    if other is not None:
                        targets[other.token] = other
            for target in targets.values():
                self._push(target, wire_ev)
            self._announce_locked()
            log.info(
                "wire %s <-> %s %s by %s",
                a_nick,
                b_nick,
                "snipped" if action == "cut" else "linked",
                session.nick,
            )

    # ---- introspection ---------------------------------------------------
    def state(self) -> dict:
        with self.cond:
            return {
                "nodes": self.mesh.node_map(),
                "sessions": len(self._sessions),
            }

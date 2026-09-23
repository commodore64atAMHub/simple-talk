"""Mesh chat server: single coordinator that simulates a mesh network.

Every connected client becomes a node in the simulated mesh graph.
Unicast messages are routed through the mesh hop-by-hop with simulated
transmission delay (packets visibly traverse intermediate nodes);
broadcasts flood to every node.  The server never lets one node talk
"directly" to a non-neighbour -- all traffic goes through the mesh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import uuid

from .mesh import Mesh

log = logging.getLogger("stserver")


def lan_ipv4_addresses() -> list[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses.

    These are the addresses a remote client should point ``--host`` at.
    Falls back to ``ipconfig``/``ip addr`` when detection fails.
    """
    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        ):
            addrs.add(info[4][0])
    except OSError:
        pass
    # Discovering the default-route source address (no packets are sent;
    # connect() on a UDP socket only picks a local address).
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(a for a in addrs if not a.startswith("127.") and not a.startswith("169.254."))


class ChatServer:
    def __init__(self, host: str, port: int, hop_ms: int = 120, seed: int | None = None):
        self.host = host
        self.port = port
        self.hop_base = max(5, hop_ms) / 1000.0
        self.rng = random.Random(seed)
        self.mesh = Mesh(rng=self.rng)
        self._by_nick: dict[str, int] = {}
        self._next_id = 1
        self._server: asyncio.Server | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        bound_port = self._server.sockets[0].getsockname()[1] if self._server.sockets else self.port
        log.info("mesh server listening on %s", addrs)

        if self.host in ("0.0.0.0", "::") or self.host == "":
            lan = lan_ipv4_addresses()
            if lan:
                log.info(
                    "reachable from other machines on this network at:\n%s",
                    "\n".join(f"  {a}:{bound_port}   (client: python client/main.py --host {a})" for a in lan),
                )
            else:
                log.info(
                    "could not auto-detect a LAN address -- run `ipconfig` here and use an IPv4 "
                    "address from the ACTIVE adapter (Wi-Fi, not Hyper-V/VPN virtual adapters)"
                )
            if os.name == "nt":
                log.info(
                    "Windows firewall tip: allow python (and the built simple-talk-server) for "
                    "Private networks, and make sure the Wi-Fi/ethernet profile is set to Private "
                    "-- on Public/domain profiles inbound connections are silently dropped"
                )
            log.info(
                "if peers still cannot connect, the network likely blocks device-to-device "
                "traffic (guest/school Wi-Fi client isolation) -- fall back to a phone hotspot"
            )
        else:
            log.warning(
                "bound to %s (loopback) -- other machines cannot reach the mesh. "
                "Restart with --host 0.0.0.0 to accept LAN connections",
                self.host,
            )

    async def serve(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    async def shutdown(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------
    def _next_node_id(self) -> int:
        n = self._next_id
        self._next_id += 1
        return n

    @staticmethod
    def _nick_key(nick: str) -> str:
        return nick.casefold()

    @staticmethod
    async def _send_raw(writer, msg: dict) -> None:
        if writer is None:
            return
        try:
            writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            pass

    # ------------------------------------------------------------------
    async def _on_connect(self, reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("node connecting from %s", peer)
        node_id = None
        try:
            nick = await self._handshake(reader, writer)
            if nick is None:
                return
            node_id = self._next_node_id()
            node = self.mesh.add(node_id, nick, writer)
            self._by_nick[self._nick_key(nick)] = node_id
            log.info("node %d joined as %r", node_id, nick)
            await self._announce()
            await self._welcome(node)
            await self._serve(node, reader, writer)
        except (ValueError, json.JSONDecodeError) as exc:
            log.info("rejecting %s: %s", peer, exc)
            await self._send_raw(writer, {"type": "error", "reason": str(exc)})
        except asyncio.CancelledError:
            pass
        finally:
            if node_id is not None:
                nick_left = self._drop(node_id)
                if nick_left is not None:
                    self._spawn_leave(node_id, nick_left)
            try:
                writer.close()
            except Exception:
                pass
            log.info("node %s disconnected", peer)

    async def _handshake(self, reader, writer) -> str | None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError:
            raise ValueError("no hello received within 10s")
        if not raw:
            return None
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError("malformed hello")
        if msg.get("type") != "hello":
            raise ValueError("expected hello")
        nick = (msg.get("nick") or "").strip()
        if not (1 <= len(nick) <= 24):
            raise ValueError("nick must be 1-24 characters")
        if not nick.isprintable():
            raise ValueError("nick has unprintable characters")
        if self._nick_key(nick) in self._by_nick:
            raise ValueError(f"nick already in use: {nick}")
        return nick

    # ------------------------------------------------------------------
    async def _announce(self) -> None:
        """Push updated neighbor lists to every node after topology change."""
        for nid in self.mesh._order:
            node = self.mesh.nodes[nid]
            await self._safe_send(node, {"type": "peers", "peers": self.mesh.peers_of(nid)})
        for nid in self.mesh._order:
            node = self.mesh.nodes[nid]
            await self._safe_send(
                node,
                {"type": "node_map", "nodes": self.mesh.node_map()},
            )

    async def _welcome(self, node) -> None:
        mine = node
        await self._safe_send(
            node,
            {
                "type": "welcome",
                "self": {"id": mine.node_id, "nick": mine.nick, "x": mine.x, "y": mine.y},
                "nodes": self.mesh.node_map(),
                "peers": self.mesh.peers_of(mine.node_id),
            },
        )
        for other in self.mesh._order:
            if other == mine.node_id:
                continue
            await self._safe_send(
                self.mesh.nodes[other],
                {"type": "node_join", "node": {"id": mine.node_id, "nick": mine.nick}},
            )

    async def _serve(self, node, reader, writer) -> None:
        while True:
            try:
                raw = await reader.readline()
            except (ConnectionError, asyncio.IncompleteReadError, OSError):
                return
            if not raw:
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("dropping malformed frame from %s", node.nick)
                continue
            mtype = msg.get("type")
            if mtype == "send":
                try:
                    await self._handle_send(node, msg)
                except (ConnectionError, OSError, RuntimeError):
                    return
            elif mtype in ("cut", "link"):
                try:
                    await self._handle_wire(node, msg)
                except (ConnectionError, OSError, RuntimeError):
                    return
            elif mtype == "bye":
                return
            elif mtype == "ping":
                await self._safe_send(node, {"type": "pong"})
            else:
                log.debug("node %s sent unknown type %r", node.nick, mtype)

    # ------------------------------------------------------------------
    async def _handle_send(self, src: "Node", msg: dict) -> None:
        body = str(msg.get("body") or "")[:2048]
        if not body.strip():
            return
        target = str(msg.get("to") or "*")
        msg_id = (msg.get("id") or uuid.uuid4().hex)[:16]
        if target == "*":
            await self._broadcast(src, body, msg_id)
            return
        dst_id = self._by_nick.get(self._nick_key(target))
        if dst_id is None:
            await self._safe_send(src, {"type": "error", "id": msg_id, "reason": f"no such node: {target}"})
            return
        dst = self.mesh.nodes.get(dst_id)
        if dst is None:
            await self._safe_send(src, {"type": "error", "id": msg_id, "reason": "target not connected"})
            return
        await self._relay(src, dst, body, msg_id)

    async def _handle_wire(self, src: "Node", msg: dict) -> None:
        """Snip (``cut``) or re-attach (``link``) a wire between two nodes.

        Anyone may reshape the mesh -- it is the shared simulation. The
        ``wire`` event only carries the chat-log line, so it goes just to
        the parties who care (snip: the operator alone; link: both ends of
        the wire, plus the operator); the fresh topology push still
        reaches every node, so all maps and status bars update either way.
        """
        action = msg.get("type")
        a_nick = str(msg.get("a") or "").strip()
        b_nick = str(msg.get("b") or "").strip()

        def resolve(nick: str) -> int | None:
            return self._by_nick.get(self._nick_key(nick))

        ia, ib = resolve(a_nick), resolve(b_nick)
        if ia is None or ib is None:
            missing = a_nick if ia is None else b_nick
            await self._safe_send(src, {"type": "error", "reason": f"no such node: {missing}"})
            return
        if ia == ib:
            await self._safe_send(src, {"type": "error", "reason": "a wire needs two different nodes"})
            return
        if action == "cut":
            ok = self.mesh.cut(ia, ib)
            failure = f"no such wire: {a_nick} <-> {b_nick}"
        else:  # "link"
            ok = self.mesh.link(ia, ib)
            failure = f"wire already exists: {a_nick} <-> {b_nick}"
        if not ok:
            await self._safe_send(src, {"type": "error", "reason": failure})
            return

        wire = {"type": "wire", "action": action, "a": a_nick, "b": b_nick, "by": src.nick}
        # Send the chat-log line only to the parties who care (see
        # docstring); _announce() below still pushes topology to everyone.
        targets: dict[int, "Node"] = {src.node_id: src}
        if action == "link":
            targets[ia] = self.mesh.nodes[ia]
            targets[ib] = self.mesh.nodes[ib]
        for target in targets.values():
            await self._safe_send(target, wire)
        await self._announce()
        log.info(
            "wire %s <-> %s %s by %s",
            a_nick,
            b_nick,
            "snipped" if action == "cut" else "linked",
            src.nick,
        )

    async def _relay(self, src: Node, dst: Node, body: str, msg_id: str) -> None:
        """Store-and-forward a unicast packet through the mesh graph."""
        router = self.mesh.route(src.node_id, dst.node_id)
        if not router:
            await self._safe_send(src, {"type": "error", "id": msg_id, "reason": "no route through the mesh"})
            return
        names = [self.mesh.nick_of(i) for i in router]
        hops = len(router) - 1

        # Forward hop-by-hop. A relay tick is broadcast to EVERY node (not
        # just the sender) so each client's live mesh view can animate the
        # packet walking through the network -- including the final delivery
        # hop (hop == hops), which immediately precedes message/ack.
        for idx in range(1, hops + 1):
            mid = router[idx]
            await asyncio.sleep(self._hop_delay())
            tick = {
                "type": "relay",
                "id": msg_id,
                "from": src.nick,
                "to": dst.nick,
                "hop": idx,
                "hops": hops,
                "via": self.mesh.nick_of(mid),
            }
            for nid in list(self.mesh._order):
                await self._safe_send(self.mesh.nodes.get(nid), tick)

        await self._safe_send(
            dst,
            {
                "type": "message",
                "id": msg_id,
                "from": src.nick,
                "to": dst.nick,
                "body": body,
                "hops": hops,
                "route": names,
            },
        )
        await self._safe_send(
            src,
            {"type": "ack", "id": msg_id, "to": dst.nick, "hops": hops, "route": names},
        )
        log.info("relayed %d bytes %s -> %s (%d hop(s))", len(body), src.nick, dst.nick, hops)

    async def _broadcast(self, src: Node, body: str, msg_id: str) -> None:
        for nid in list(self.mesh._order):
            node = self.mesh.nodes[nid]
            if node.node_id == src.node_id:
                continue
            await asyncio.sleep(self._hop_delay())
            await self._safe_send(
                node,
                {
                    "type": "message",
                    "id": msg_id,
                    "from": src.nick,
                    "to": "*",
                    "body": body,
                    "hops": 1,
                    "route": [src.nick, node.nick],
                },
            )
        await self._safe_send(src, {"type": "ack", "id": msg_id, "to": "*", "hops": 1, "route": [src.nick]})
        log.info("broadcast from %s to %d node(s)", src.nick, len(self.mesh._order) - 1)

    # ------------------------------------------------------------------
    def _hop_delay(self) -> float:
        return self.hop_base * (0.7 + self.rng.random() * 0.6)

    async def _safe_send(self, node, msg: dict) -> None:
        if node is None or node.writer is None:
            return
        try:
            node.writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await node.writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            nick = self._drop(node.node_id)
            if nick is not None:
                self._spawn_leave(node.node_id, nick)

    def _drop(self, node_id: int) -> str | None:
        """Remove a departed node. Returns its nick if it was still present."""
        if node_id not in self.mesh.nodes:
            return None
        nick = self.mesh.nick_of(node_id)
        self.mesh.remove(node_id)
        if self._nick_key(nick) in self._by_nick:
            del self._by_nick[self._nick_key(nick)]
        log.info("node %d (%s) left; %d remain", node_id, nick, len(self.mesh.nodes))
        return nick

    def _spawn_leave(self, node_id: int, nick: str) -> None:
        """Announce a departure without blocking (or recursing into) the caller."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._leave_notice(node_id, nick))
        self._bg_tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieve silently: dead-peer races are expected

    async def _leave_notice(self, node_id: int, nick: str) -> None:
        """Tell every remaining node someone left, then push the new topology."""
        msg = {"type": "node_leave", "node": {"id": node_id, "nick": nick}}
        for nid in list(self.mesh._order):
            await self._safe_send(self.mesh.nodes.get(nid), msg)
        await self._announce()


def run(host: str = "127.0.0.1", port: int = 8765, hop_ms: int = 120, seed: int | None = None):
    async def _main():
        srv = ChatServer(host, port, hop_ms=hop_ms, seed=seed)
        await srv.start()
        try:
            await srv.serve()
        finally:
            await srv.shutdown()

    asyncio.run(_main())
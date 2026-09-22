"""Simulated mesh topology.

The server itself is a single coordinator process, but it presents a
*mesh network* to the clients: each connected client becomes a node in a
graph.  Nodes are placed on a golden-spiral layout, are linked to nearby
ring neighbours plus a few random shortcut links (a connected mesh), and
all traffic is routed hop-by-hop through that graph with simulated
per-hop transmission delay.

This makes ordinary chat behave like a store-and-forward mesh radio
network: messages take real time to hop through intermediate nodes, and
every node only ever "talks" to its direct neighbours.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import Optional

GOLDEN_ANGLE = 2.3999632297286533  # radians


@dataclass
class Node:
    node_id: int
    nick: str
    x: float
    y: float
    writer: Optional[object] = None


class Mesh:
    def __init__(self, ring_degree: int = 2, shortcuts: int = 3, rng: random.Random = None):
        self.ring_degree = ring_degree
        self.shortcuts = shortcuts
        self.rng = rng or random.Random()
        self.nodes: dict[int, Node] = {}
        self.adj: dict[int, set[int]] = {}
        self._order: list[int] = []

    # ---- node placement (deterministic per id) -----------------------
    @staticmethod
    def _place(node_id: int) -> tuple[float, float]:
        r = 3.0 * (node_id + 1) ** 0.5
        a = node_id * GOLDEN_ANGLE
        return round(r * math.cos(a), 1), round(r * math.sin(a), 1)

    # ---- membership -----------------------------------------------------
    def add(self, node_id: int, nick: str, writer) -> Node:
        self._order.append(node_id)
        node = self.nodes.setdefault(node_id, Node(node_id, nick, *self._place(node_id), writer))
        self.rebuild()
        return node

    def remove(self, node_id: int) -> None:
        if node_id in self.nodes:
            del self.nodes[node_id]
        if node_id in self._order:
            self._order.remove(node_id)
        self.rebuild()

    def rebuild(self) -> None:
        """(Re)compute adjacency for every node."""
        ids = self._order
        n = len(ids)
        adj = {i: set() for i in ids}
        if n > 1:
            k = min(self.ring_degree, n - 1)
            for idx, i in enumerate(ids):
                for d in range(1, k + 1):
                    adj[i].add(ids[(idx - d) % n])
                    adj[i].add(ids[(idx + d) % n])
            existing = sum(len(s) for s in adj.values()) // 2
            max_edges = n * (n - 1) // 2
            extra = max(0, min(self.shortcuts, max_edges - existing))
            for _ in range(extra):
                a, b = self.rng.sample(ids, 2)
                adj[a].add(b)
                adj[b].add(a)
        self.adj = {i: frozenset(s) for i, s in adj.items()}

    # ---- queries ---------------------------------------------------------
    def peers_of(self, node_id: int) -> list[int]:
        return sorted(self.adj.get(node_id, ()))

    def nick_of(self, node_id: int) -> str:
        node = self.nodes.get(node_id)
        return node.nick if node else f"node{node_id}"

    def route(self, src: int, dst: int) -> Optional[list[int]]:
        """Shortest path (BFS) through the mesh, src and dst inclusive."""
        if src not in self.adj or dst not in self.adj:
            return None
        if src == dst:
            return [src]
        prev = {src: None}
        q = deque([src])
        found = False
        while q:
            cur = q.popleft()
            if cur == dst:
                found = True
                break
            for nxt in self.adj[cur]:
                if nxt not in prev:
                    prev[nxt] = cur
                    q.append(nxt)
        if not found:
            return None
        path = [dst]
        cur = dst
        while prev[cur] is not None:
            cur = prev[cur]
            path.append(cur)
        path.reverse()
        return path

    def node_map(self) -> list[dict]:
        out = []
        for i in self._order:
            nd = self.nodes[i]
            out.append(
                {
                    "id": nd.node_id,
                    "nick": nd.nick,
                    "x": nd.x,
                    "y": nd.y,
                    "peers": self.peers_of(i),
                }
            )
        return out
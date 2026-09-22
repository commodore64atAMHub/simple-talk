"""ASCII rendering of the simulated mesh topology for the client's `/show`.

Draws every mesh node as a labelled letter on a scaled layout and connects
neighbours with box/line-drawing characters, then prints a legend with
nicknames, coordinates and the hop distance from *you* (BFS over the graph).
"""

from __future__ import annotations

import sys
from collections import deque

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _glyphs() -> dict:
    """Pick a glyph set that the current stdout encoding can actually emit.

    Real terminals (and anything where stdout is UTF-8) get the nice
    box-drawing characters; ANSI codepages (cp1252/cp437...) fall back to
    plain ASCII so rendering can never crash on an odd console.
    """
    enc = (sys.stdout.encoding or "").lower()
    use_unicode = "utf" in enc or enc in ("cp65001",)
    return {
        "h": "\u2500" if use_unicode else "-",  # ─
        "v": "\u2502" if use_unicode else "|",  # │
        "cross": "\u253c" if use_unicode else "+",  # ┼
        "slash": "\u2571" if use_unicode else "/",  # ╱
        "backslash": "\u2572" if use_unicode else "\\",  # ╲
    }


H = None  # set per-render in _edge_char via glyphs
V = None
CROSS = None
SLASH = None
BACKSLASH = None


def _set_glyphs(g: dict) -> None:
    global H, V, CROSS, SLASH, BACKSLASH
    H, V, CROSS, SLASH, BACKSLASH = g["h"], g["v"], g["cross"], g["slash"], g["backslash"]


def _line_points(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham line between two cells; endpoints included."""
    pts = []
    x, y = x0, y0
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        pts.append((x, y))
        if (x, y) == (x1, y1):
            return pts
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def _edge_char(x0: int, y0: int, x1: int, y1: int) -> str:
    """Pick a drawing character for the whole (straight-ish) link."""
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    if dx >= 2 * dy:
        return H
    if dy >= 2 * dx:
        return V
    return SLASH if (x1 - x0) * (y1 - y0) >= 0 else BACKSLASH


def _hops(adj: dict, src, dst):
    """Hop distance between two node ids (BFS), or None if unreachable."""
    if src == dst:
        return 0
    if src is None:
        return None
    seen = {src}
    queue = deque([(src, 0)])
    while queue:
        cur, d = queue.popleft()
        for nxt in adj.get(cur, ()):
            if nxt in seen:
                continue
            if nxt == dst:
                return d + 1
            seen.add(nxt)
            queue.append((nxt, d + 1))
    return None


def render_mesh(nodes: list[dict], edges, self_id=None, width: int = 74, height: int = 18) -> list[str]:
    """Render the mesh as ASCII art.

    ``nodes``: list of dicts with ``id``, ``nick``, ``x``, ``y``.
    ``edges``: iterable of ``(id_a, id_b)`` pairs.
    ``self_id``: your node id (marked in the legend; hop counts measured from it).
    Returns a list of text lines, ready to be printed/added to the log.
    """
    if not nodes:
        return ["(mesh empty)"]
    _set_glyphs(_glyphs())
    width = max(24, int(width))
    height = max(8, int(height))

    label_of = {}
    for i, nd in enumerate(nodes):
        label_of[nd["id"]] = LETTERS[i] if i < len(LETTERS) else f"#{nd['id']}"

    xs = [nd["x"] for nd in nodes]
    ys = [nd["y"] for nd in nodes]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x or 1.0
    span_y = max_y - min_y or 1.0

    def cell(nd):
        cx = round((nd["x"] - min_x) / span_x * (width - 1))
        cy = round((nd["y"] - min_y) / span_y * (height - 1))
        return max(0, min(cx, width - 1)), max(0, min(cy, height - 1))

    cell_of = {nd["id"]: cell(nd) for nd in nodes}
    canvas = [[" " for _ in range(width)] for _ in range(height)]
    node_cells = set(cell_of.values())

    def put(cx: int, cy: int, ch: str) -> None:
        if 0 <= cx < width and 0 <= cy < height:
            canvas[cy][cx] = ch

    # links first (nodes are drawn over them)
    for a, b in edges:
        if a not in cell_of or b not in cell_of:
            continue
        ax, ay = cell_of[a]
        bx, by = cell_of[b]
        ch = _edge_char(ax, ay, bx, by)
        for px, py in _line_points(ax, ay, bx, by):
            if (px, py) in node_cells:
                continue
            cur = canvas[py][px]
            if cur == " ":
                put(px, py, ch)
            elif cur != ch and {cur, ch} == {H, V}:
                put(px, py, CROSS)

    for nd in nodes:
        cx, cy = cell_of[nd["id"]]
        put(cx, cy, label_of[nd["id"]])

    adj: dict = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    you = self_id if self_id in cell_of else None
    legend = []
    for nd in nodes:
        h = _hops(adj, you, nd["id"])
        you_mark = " (you)" if you == nd["id"] else ""
        hop = "" if h is None else f", {h} hop" + ("s" if h != 1 else "")
        legend.append(f"{label_of[nd['id']]}={nd['nick']}{you_mark} @ ({nd['x']}, {nd['y']}){hop}")

    lines = [f"mesh: {len(nodes)} node(s), {len(edges)} link(s)"]
    if you is not None:
        lines.append(f"you are {label_of[you]}={next(nd['nick'] for nd in nodes if nd['id'] == you)}")
    lines += ["".join(row) for row in canvas]
    for i in range(0, len(legend), 4):
        lines.append("  " + "   ".join(legend[i : i + 4]))
    return lines
"""ASCII rendering of the simulated mesh topology for the client's `/show`.

Nodes are placed on a scaled copy of the mesh's golden-spiral layout;
links are drawn with box-drawing characters using a *per-cell direction
map*, so corners (┐ └ ...) and junctions (┼ ...) come out right instead
of a staircase of dashes. A legend lists nicknames, coordinates and the
hop distance from you (BFS over the graph).

Rows must keep their exact columns to look right, so the client adds
them with the ``map`` log kind (no timestamps, no re-wrapping by the
TUI -- see ``ui.ChatUI._render``).
"""

from __future__ import annotations

import sys
from collections import deque

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

PAD = 1  # blank margin so the diagram never touches the canvas border

# Orthogonal direction set per cell -> glyph. All 16 subsets are covered.
_BOX = {
    frozenset(): " ",
    frozenset("U"): "\u2502",  # │
    frozenset("D"): "\u2502",  # │
    frozenset("L"): "\u2500",  # ─
    frozenset("R"): "\u2500",  # ─
    frozenset("UD"): "\u2502",  # │
    frozenset("LR"): "\u2500",  # ─
    frozenset("UL"): "\u2518",  # ┘
    frozenset("UR"): "\u2514",  # └
    frozenset("DL"): "\u2510",  # ┐
    frozenset("DR"): "\u250c",  # ┌
    frozenset("UDL"): "\u2524",  # ┤
    frozenset("UDR"): "\u251c",  # ├
    frozenset("ULR"): "\u2534",  # ┴
    frozenset("DLR"): "\u252c",  # ┬
    frozenset("UDLR"): "\u253c",  # ┼
}

_DIAG_BACK = "\u2572"  # ╲  top-left -> bottom-right
_DIAG_FWD = "\u2571"  # ╱  bottom-left -> top-right
_DIAG_CROSS = "\u2573"  # ╳

# Plain-ASCII fallback for consoles that cannot encode box drawing.
_ASCII = {
    "\u2500": "-",
    "\u2502": "|",
    "\u250c": "+",
    "\u2510": "+",
    "\u2514": "+",
    "\u2518": "+",
    "\u251c": "+",
    "\u2524": "+",
    "\u252c": "+",
    "\u2534": "+",
    "\u253c": "+",
    "\u2571": "/",
    "\u2572": "\\",
    "\u2573": "+",
}


def _use_unicode() -> bool:
    enc = (sys.stdout.encoding or "").lower()
    return "utf" in enc or enc in ("cp65001",)


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


def _draw_edges(cell_of: dict, edges) -> tuple[dict, dict]:
    """Record, per cell, how links enter/leave it.

    Returns ``(ortho, diag)`` where ``ortho[(x, y)]`` is a set of
    ``U/D/L/R`` directions and ``diag[(x, y)]`` a set of diagonal glyphs.
    """
    ortho: dict = {}
    diag: dict = {}
    for a, b in edges:
        if a not in cell_of or b not in cell_of or a == b:
            continue
        x0, y0 = cell_of[a]
        x1, y1 = cell_of[b]
        pts = _line_points(x0, y0, x1, y1)
        for (px, py), (qx, qy) in zip(pts, pts[1:]):
            dx, dy = qx - px, qy - py
            if dy == 0:
                ortho.setdefault((px, py), set()).add("R" if dx > 0 else "L")
                ortho.setdefault((qx, qy), set()).add("L" if dx > 0 else "R")
            elif dx == 0:
                ortho.setdefault((px, py), set()).add("D" if dy > 0 else "U")
                ortho.setdefault((qx, qy), set()).add("U" if dy > 0 else "D")
            else:
                g = _DIAG_BACK if dx * dy > 0 else _DIAG_FWD
                diag.setdefault((px, py), set()).add(g)
                diag.setdefault((qx, qy), set()).add(g)
    return ortho, diag


def _nudge(cell: tuple, width: int, height: int, taken: set) -> tuple:
    """Nearest free cell when two nodes land on the same square."""
    offsets = [(dx, dy) for dx in range(-3, 4) for dy in range(-3, 4)]
    offsets.sort(key=lambda d: (max(abs(d[0]), abs(d[1])), abs(d[0]) + abs(d[1])))
    for dx, dy in offsets:
        c = (cell[0] + dx, cell[1] + dy)
        if 0 <= c[0] < width and 0 <= c[1] < height and c not in taken:
            return c
    return cell


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


def _wrap_entries(entries: list[str], width: int) -> list[str]:
    """Greedy-join legend entries with gaps, preserving column layout."""
    lines: list[str] = []
    cur = ""
    for entry in entries:
        if not cur:
            cur = entry
        elif len(cur) + 4 + len(entry) <= width:
            cur += "    " + entry
        else:
            lines.append(cur)
            cur = entry
    if cur:
        lines.append(cur)
    return ["  " + ln for ln in lines]


def render_mesh(nodes: list[dict], edges, self_id=None, width: int = 74, height: int = 18) -> list[str]:
    """Render the mesh as ASCII art.

    ``nodes``: list of dicts with ``id``, ``nick``, ``x``, ``y``.
    ``edges``: iterable of ``(id_a, id_b)`` pairs.
    ``self_id``: your node id (marked in the legend; hops measured from it).
    Returns a list of text lines, ready to be added to the log with the
    ``map`` kind so the TUI preserves their exact columns.
    """
    if not nodes:
        return ["(mesh empty)"]
    width = max(16, int(width))
    height = max(8, int(height))
    use_unicode = _use_unicode()

    label_of = {}
    for i, nd in enumerate(nodes):
        label_of[nd["id"]] = LETTERS[i] if i < len(LETTERS) else f"#{nd['id']}"

    xs = [nd["x"] for nd in nodes]
    ys = [nd["y"] for nd in nodes]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x or 1.0
    span_y = max_y - min_y or 1.0
    inner_w = max(1, width - 1 - 2 * PAD)
    inner_h = max(1, height - 1 - 2 * PAD)

    def cell(nd):
        cx = PAD + max(0, min(round((nd["x"] - min_x) / span_x * inner_w), width - 1 - PAD))
        cy = PAD + max(0, min(round((nd["y"] - min_y) / span_y * inner_h), height - 1 - PAD))
        return cx, cy

    # place nodes (nudging the rare collision to the nearest free square)
    cell_of: dict = {}
    taken: set = set()
    for nd in nodes:
        c = cell(nd)
        if c in taken:
            c = _nudge(c, width, height, taken)
        taken.add(c)
        cell_of[nd["id"]] = c
    label_at = {cell_of[nd["id"]]: label_of[nd["id"]] for nd in nodes}

    ortho, diag = _draw_edges(cell_of, edges)

    rows = []
    for y in range(height):
        chars = []
        for x in range(width):
            pos = (x, y)
            if pos in label_at:
                chars.append(label_at[pos])
                continue
            # Diagonal steps share their cells with the horizontal/vertical
            # runs they connect; if ortho won, row transitions would vanish
            # (the old "wall of dashes" bug), so diag gets priority.
            d = diag.get(pos)
            dirs = ortho.get(pos)
            if d:
                g = _DIAG_CROSS if len(d) > 1 else next(iter(d))
            elif dirs:
                g = _BOX.get(frozenset(dirs), "\u253c")
            else:
                g = " "
            if not use_unicode:
                g = _ASCII.get(g, g)
            chars.append(g)
        rows.append("".join(chars).rstrip())

    adj: dict = {}
    for a, b in edges:
        if a in cell_of and b in cell_of and a != b:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

    you = self_id if self_id in cell_of else None
    entries = []
    for nd in nodes:
        h = _hops(adj, you, nd["id"])
        you_mark = " (you)" if you == nd["id"] else ""
        hop = "" if h is None else f", {h} hop" + ("s" if h != 1 else "")
        entries.append(f"{label_of[nd['id']]}={nd['nick']}{you_mark} @ ({nd['x']}, {nd['y']}){hop}")

    lines = [f"mesh: {len(nodes)} node(s), {len(edges)} link(s)"]
    if you is not None:
        lines.append(f"you are {label_of[you]}={next(nd['nick'] for nd in nodes if nd['id'] == you)}")
    lines += rows
    lines += _wrap_entries(entries, width)
    return lines

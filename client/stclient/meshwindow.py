"""Live mesh visualization in a tkinter window (opened by ``/show``).

The window owns its own thread (Tk must stay off the asyncio loop); the
client posts state snapshots and packet events through a queue, the
window drains it on a ~30fps tick and redraws. Nodes are drawn as
labelled circles on the mesh's real golden-spiral geometry (uniform
scale, unlike the ASCII map), and packets animate smoothly between nodes
as ``relay`` events arrive, then fade out on delivery. A node that leaves
fades out at its last position ("nick left") so departures are visible
in the mesh itself.

Falls back to the ASCII map (``meshview``) when tkinter is unavailable,
when ``--plain`` is active, or if the window fails to open -- see
``App.on_show``.
"""

from __future__ import annotations

import math
import queue
import threading
import time

try:
    import tkinter as tk
except Exception:  # pragma: no cover - environment dependent
    tk = None

WINDOW_TITLE = "simple-talk mesh"

BG = "#0f1419"
GRID = "#1a2129"
EDGE = "#3a4a5c"
NODE_FILL = "#182430"
NODE_EDGE = "#5b8db8"
SELF_FILL = "#2b2313"
SELF_EDGE = "#e0b243"
FG = "#d8dee9"
MUTED = "#7a8a99"
PACKET = "#e5c07b"

NODE_R = 17
PACKET_R = 6
TICK_MS = 33  # ~30fps
GHOST_TTL = 2.5  # seconds a departure marker stays before fading out


def available() -> bool:
    """True when tkinter imported successfully on this machine."""
    return tk is not None


def _mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colors: t=0 -> c1, t=1 -> c2."""
    a = [int(c1[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i : i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class MeshWindow:
    """A live top-down view of the mesh, updated from client events."""

    def __init__(self):
        if tk is None:
            raise RuntimeError("tkinter is not available")
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error: BaseException | None = None

    # -- lifecycle --------------------------------------------------------
    @property
    def alive(self) -> bool:
        return (
            self._error is None
            and self._thread is not None
            and self._thread.is_alive()
            and not self._closed.is_set()
        )

    def start(self, timeout: float = 3.0) -> bool:
        """Create the window on its own thread. Returns False if it failed."""
        self._thread = threading.Thread(target=self._run, name="mesh-viz", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.alive

    def _run(self) -> None:
        try:
            self._ui()
        except BaseException as exc:  # no display / Tk failure -> caller falls back
            self._error = exc
        finally:
            self._ready.set()
            self._closed.set()

    # -- client -> window --------------------------------------------------
    def _put(self, msg) -> None:
        if not self._closed.is_set():
            self._q.put(msg)

    def push_state(self, nodes, edges, self_id) -> None:
        """Full topology snapshot: node dicts (id/nick/x/y), edge pairs."""
        self._put(("state", nodes, edges, self_id))

    def push_relay(self, msg: dict) -> None:
        """One hop of a packet (``via`` = node it just reached)."""
        self._put(("relay", msg))

    def push_arrival(self, msg: dict) -> None:
        """A delivered ``message``/``ack`` frame (carries the full route)."""
        self._put(("arrival", msg))

    def push_leave(self, node) -> None:
        """A node departed: fade a marker at its last position."""
        self._put(("leave", node))

    def raise_window(self) -> None:
        self._put(("raise",))

    def close(self) -> None:
        if not self._closed.is_set():
            self._q.put(("close",))
        self._closed.set()

    def dump(self, timeout: float = 2.0) -> dict:
        """Debug/test helper: a snapshot of what the window currently shows."""
        if not self.alive:
            raise RuntimeError("mesh window is not open")
        rq: queue.Queue = queue.Queue()
        self._q.put(("dump", rq))
        return rq.get(timeout=timeout)

    # -- the window itself (runs on the viz thread) -------------------------
    def _ui(self) -> None:
        root = tk.Tk()
        root.title(WINDOW_TITLE)
        root.geometry("780x560")
        root.minsize(420, 320)
        root.configure(bg=BG)

        canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        footer = tk.Label(
            root,
            bg=BG,
            fg=MUTED,
            anchor="w",
            padx=10,
            pady=5,
            text="waiting for mesh data -- updates arrive live",
        )
        footer.pack(fill="x")

        # ---- model (only touched on the viz thread) ----------------------
        nodes: dict = {}  # id -> node dict
        edges: list = []  # [(id_a, id_b)]
        self_id = None
        pos: dict = {}  # nick -> (x, y) canvas coords
        packets: dict = {}  # msg_id -> animation state
        ghosts: list = []  # {nick, x, y, born} departure markers
        dirty = [True]
        errors: list = []

        def layout():
            w = max(canvas.winfo_width(), 120)
            h = max(canvas.winfo_height(), 120)
            pad = 54
            xs = [n.get("x", 0.0) for n in nodes.values()] or [0.0]
            ys = [n.get("y", 0.0) for n in nodes.values()] or [0.0]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            span_x = max(max_x - min_x, 1e-6)
            span_y = max(max_y - min_y, 1e-6)
            avail_w = max(w - 2 * pad, 40)
            avail_h = max(h - 2 * pad, 40)
            s = min(avail_w / span_x, avail_h / span_y)  # uniform scale
            off_x = (w - span_x * s) / 2
            off_y = (h - span_y * s) / 2
            out = {}
            for n in nodes.values():
                nick = n.get("nick") or f"node{n.get('id')}"
                out[nick] = (
                    off_x + (n.get("x", 0.0) - min_x) * s,
                    off_y + (max_y - n.get("y", 0.0)) * s,  # flip y: math-up
                )
            return out, w, h

        def draw_static() -> None:
            canvas.delete("static")
            pos_new, w, h = layout()
            pos.clear()
            pos.update(pos_new)
            step = 36
            for gx in range(step // 2, int(w), step):
                for gy in range(step // 2, int(h), step):
                    canvas.create_oval(gx, gy, gx + 1, gy + 1, fill=GRID, outline="", tags="static")
            nick_of = {i: (n.get("nick") or f"node{i}") for i, n in nodes.items()}
            for a, b in edges:
                pa, pb = pos.get(nick_of.get(a)), pos.get(nick_of.get(b))
                if pa and pb:
                    canvas.create_line(pa[0], pa[1], pb[0], pb[1], fill=EDGE, width=2, tags="static")
            me = None
            for n in nodes.values():
                nick = n.get("nick") or f"node{n.get('id')}"
                p = pos.get(nick)
                if not p:
                    continue
                is_self = self_id is not None and n.get("id") == self_id
                if is_self:
                    me = nick
                x, y = p
                canvas.create_oval(
                    x - NODE_R,
                    y - NODE_R,
                    x + NODE_R,
                    y + NODE_R,
                    fill=SELF_FILL if is_self else NODE_FILL,
                    outline=SELF_EDGE if is_self else NODE_EDGE,
                    width=3 if is_self else 2,
                    tags="static",
                )
                canvas.create_text(
                    x,
                    y + NODE_R + 11,
                    text=nick,
                    fill=SELF_EDGE if is_self else FG,
                    font=("Segoe UI", 9, "bold" if is_self else "normal"),
                    tags="static",
                )
            footer.config(
                text=(
                    f"mesh: {len(nodes)} node(s), {len(edges)} link(s)"
                    + (f"   |   you are {me}" if me else "")
                    + "   |   live: joins and packets appear here"
                    if nodes
                    else "waiting for mesh data -- updates arrive live"
                )
            )

        # ---- packet animation ---------------------------------------------
        def start_packet(pid: str, start_nick: str) -> dict:
            p = {
                "nick": start_nick,
                "target": None,
                "t": 0.0,
                "dur": 0.3,
                "queue": [],
                "fade_after": False,
                "hold": None,
                "fade": 1.0,
                "fading": False,
                "born": time.monotonic(),
            }
            packets[pid] = p
            return p

        def packet_xy(p):
            a = pos.get(p["nick"])
            if p["target"] is None:
                return a
            b = pos.get(p["target"])
            if not a or not b:
                return a or b
            t = min(max(p["t"], 0.0), 1.0)
            t = t * t * (3 - 2 * t)  # smoothstep
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

        def draw_packets() -> None:
            canvas.delete("packet")
            for p in packets.values():
                pt = packet_xy(p)
                if not pt:
                    continue
                r = max(PACKET_R * p["fade"], 1.0)
                canvas.create_oval(
                    pt[0] - r,
                    pt[1] - r,
                    pt[0] + r,
                    pt[1] + r,
                    fill=PACKET,
                    outline=PACKET if p["target"] else "",
                    width=1,
                    tags="packet",
                )

        def update_packets(dt: float, now: float) -> None:
            if len(packets) > 64:  # safety valve
                for pid in sorted(packets, key=lambda k: packets[k]["born"])[: len(packets) - 64]:
                    packets.pop(pid, None)
            for pid in list(packets):
                p = packets.get(pid)
                if p is None:
                    continue
                if now - p["born"] > 30:  # never leak a stuck packet
                    packets.pop(pid, None)
                    continue
                if p["fading"]:
                    p["fade"] -= dt * 4.0
                    if p["fade"] <= 0:
                        packets.pop(pid, None)
                    continue
                if p["hold"] is not None and now >= p["hold"]:
                    p["hold"] = None
                    p["fading"] = True
                if p["target"] is None and p["queue"]:
                    nxt = p["queue"].pop(0)
                    if nxt in pos and p["nick"] in pos:
                        ax, ay = pos[p["nick"]]
                        bx, by = pos[nxt]
                        p["target"] = nxt
                        p["t"] = 0.0
                        p["dur"] = max(0.18, min(0.55, math.hypot(bx - ax, by - ay) / 850.0))
                    else:
                        p["nick"] = nxt  # node vanished mid-route: teleport
                if p["target"] is not None:
                    p["t"] += dt / max(p["dur"], 1e-6)
                    if p["t"] >= 1.0:
                        p["nick"] = p["target"]
                        p["target"] = None
                        p["t"] = 0.0
                if (
                    p["target"] is None
                    and not p["queue"]
                    and p["fade_after"]
                    and p["hold"] is None
                ):
                    p["hold"] = now + 0.25

        # ---- incoming events -------------------------------------------------
        def draw_ghosts(now: float) -> None:
            canvas.delete("ghost")
            for g in list(ghosts):
                age = now - g["born"]
                if age > GHOST_TTL:
                    ghosts.remove(g)
                    continue
                col = _mix(MUTED, BG, age / GHOST_TTL)  # bright -> background
                x, y = g["x"], g["y"]
                canvas.create_oval(
                    x - NODE_R,
                    y - NODE_R,
                    x + NODE_R,
                    y + NODE_R,
                    fill=BG,
                    outline=col,
                    width=2,
                    dash=(4, 3),
                    tags="ghost",
                )
                canvas.create_text(x, y, text="\u00d7", fill=col, font=("Segoe UI", 12), tags="ghost")
                canvas.create_text(x, y + NODE_R + 11, text=f"{g['nick']} left", fill=col, tags="ghost")

        def on_relay(m: dict) -> None:
            pid = str(m.get("id") or "")
            via = m.get("via")
            if not pid or not via:
                return
            p = packets.get(pid) or start_packet(pid, str(m.get("from") or ""))
            if p["fading"]:
                p["fading"] = False
                p["fade"] = 1.0
            if p["nick"] != via or p["queue"] or p["target"] is not None:
                p["queue"].append(str(via))
            try:
                hop, hops = int(m.get("hop") or 0), int(m.get("hops") or 0)
            except (TypeError, ValueError):
                hop, hops = 0, 0
            if hops > 0 and hop >= hops:
                p["fade_after"] = True  # final hop == delivery

        def on_arrival(m: dict) -> None:
            pid = str(m.get("id") or "")
            route = [str(r) for r in (m.get("route") or [])]
            if not pid:
                return
            p = packets.get(pid)
            if p is None:
                if len(route) < 2:
                    return  # nothing animatable (self-send / no path)
                p = start_packet(pid, route[0])
            end = route[-1] if route else None
            if end and p["nick"] != end and p["target"] != end and (not p["queue"] or p["queue"][-1] != end):
                p["queue"].append(end)
            p["fade_after"] = True

        # ---- main loop ----------------------------------------------------------
        def on_close() -> None:
            self._closed.set()
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)
        canvas.bind("<Configure>", lambda _e: dirty.__setitem__(0, True))

        last = [time.monotonic()]

        def tick() -> None:
            nonlocal self_id
            pending = []
            try:
                while True:
                    try:
                        msg = self._q.get_nowait()
                    except queue.Empty:
                        break
                    kind = msg[0]
                    if kind == "state":
                        _, nlist, elist, sid = msg
                        nodes.clear()
                        nodes.update({n["id"]: dict(n) for n in nlist})
                        edges[:] = [tuple(e) for e in elist]
                        self_id = sid
                        dirty[0] = True
                    elif kind == "relay":
                        on_relay(msg[1])
                    elif kind == "arrival":
                        on_arrival(msg[1])
                    elif kind == "leave":
                        nd = msg[1]
                        nick = nd.get("nick") or f"node{nd.get('id')}"
                        p = pos.get(nick)
                        if p is None:
                            p = layout()[0].get(nick)  # never drawn yet
                        if p is not None:
                            ghosts.append(
                                {"nick": nick, "x": p[0], "y": p[1], "born": time.monotonic()}
                            )
                    elif kind == "raise":
                        root.lift()
                        root.attributes("-topmost", True)
                        root.after(150, lambda: root.attributes("-topmost", False))
                    elif kind == "close":
                        for rq in pending:
                            _snapshot(rq)
                        on_close()
                        return
                    elif kind == "dump":
                        # answered after the draw pass below, so a dump always
                        # reflects what is actually on screen right now
                        pending.append(msg[1])
                now = time.monotonic()
                dt, last[0] = now - last[0], now
                if dirty[0]:
                    draw_static()
                    dirty[0] = False
                if packets:
                    update_packets(dt, now)
                    draw_packets()
                elif canvas.find_withtag("packet"):
                    canvas.delete("packet")
                if ghosts:
                    draw_ghosts(now)
                elif canvas.find_withtag("ghost"):
                    canvas.delete("ghost")
            except Exception as exc:  # keep the window alive no matter what
                errors.append(f"{exc!r}")
            for rq in pending:
                try:
                    _snapshot(rq)
                except Exception:
                    pass
            root.after(TICK_MS, tick)

        def _snapshot(rq) -> None:
            rq.put(
                {
                    "title": root.title(),
                    "items": len(canvas.find_all()),
                    "static": len(canvas.find_withtag("static")),
                    "packet_items": len(canvas.find_withtag("packet")),
                    "packets": len(packets),
                    "left": [g["nick"] for g in ghosts],
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "footer": str(footer.cget("text")),
                    "error": errors[-1] if errors else None,
                }
            )

        self._ready.set()
        root.after(0, tick)
        root.mainloop()
        # mainloop returned: the window was closed and the thread ends
        # (alive -> False; a fresh MeshWindow is created on the next /show)

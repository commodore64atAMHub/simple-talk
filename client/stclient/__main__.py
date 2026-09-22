"""Entry point: run the simple-talk client."""

import argparse
import asyncio
import getpass
import logging
import sys

from . import __version__, protocol
from .meshview import render_mesh
from .net import MeshClient
from .ui import ChatUI, KeyReader, PlainUI, Terminal, enable_windows_vt, term_size

HELP_TEXT = (
    "commands:\n"
    "  type a line                broadcast to the whole mesh\n"
    "  /msg <nick> <text>         send to one node via mesh routing\n"
    "  /n <nick> <text>           alias for /msg\n"
    "  /show                      draw the current mesh (nodes + links)\n"
    "  /help                      this help\n"
    "  /quit                      exit (Ctrl-C / Ctrl-D also work)"
)


def hops_str(hops: int) -> str:
    return f"{hops} hop" + ("s" if hops != 1 else "")


class App:
    """Wires the network client and the display together."""

    def __init__(self, args):
        self.args = args
        self.client = MeshClient(args.host, args.port)
        self.display = None
        self.keys = None
        self.terminal = None
        self.quit_now = False
        self._nodes_map = {}
        self._self = None
        self._peers = []

    # ---- display helpers -------------------------------------------
    def nick_of(self, node_id):
        nd = self._nodes_map.get(node_id)
        return nd.get("nick", f"node{node_id}") if isinstance(nd, dict) else f"node{node_id}"

    def peers_label(self) -> str:
        names = [self.nick_of(i) for i in self._peers]
        if not names:
            return "none"
        if len(names) > 6:
            return ", ".join(names[:5]) + f", +{len(names) - 5} more"
        return ", ".join(names)

    def refresh_status(self):
        if not self._self:
            return
        nick, x, y = self._self["nick"], self._self["x"], self._self["y"]
        n = len(self._nodes_map)
        self.display.set_status(
            f"you: {nick} @ ({x}, {y})   mesh nodes: {n}   neighbors: {self.peers_label()}"
        )

    # ---- async submit (from TUI or plain input) --------------------
    async def submit(self, text: str):
        text = text.strip()
        if not text:
            return
        if self.quit_now:
            return
        if text.startswith("/"):
            await self._command(text)
            return
        self.display.add_raw(f"you -> *: {text}", kind="m")
        self.display.set_activity("flooding broadcast across the mesh...")
        try:
            await self.client.say("*", text)
        except (ConnectionError, RuntimeError):
            self.display.add_raw("connection lost")

    async def _command(self, text: str):
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if cmd in ("/quit", "/exit"):
            await self.quit()
        elif cmd in ("/msg", "/n"):
            bits = rest.split(maxsplit=1)
            if len(bits) != 2:
                self.display.add_raw("usage: /msg <nick> <text>")
                return
            target, body = bits
            self.display.add_raw(f"you -> {target}: {body}", kind="m")
            self.display.set_activity(f"routing message to {target} hop-by-hop...")
            try:
                await self.client.say(target, body)
            except (ConnectionError, RuntimeError):
                self.display.add_raw("connection lost")
        elif cmd == "/show":
            await self.on_show()
        elif cmd == "/help":
            for line in HELP_TEXT.splitlines():
                self.display.add_raw(line, kind="?")
        else:
            self.display.add_raw(f"unknown command: {cmd}   (try /help)")

    async def quit(self):
        if self.quit_now:
            return
        self.quit_now = True
        try:
            await self.client.bye()
        except Exception:
            pass
        self.client.close()
        if self._quit_evt:
            self._quit_evt.set()

    async def wait_quit(self):
        await self._quit_evt.wait()

    # ---- server message handlers ------------------------------------
    async def on_welcome(self, msg):
        self._self = msg["self"]
        self._nodes_map = {n["id"]: n for n in msg.get("nodes", [])}
        self._peers = msg.get("peers", [])
        self.refresh_status()
        self.display.add_raw(
            f"connected as {self._self['nick']} at node {self._self['id']} "
            f"({len(self._nodes_map)} node(s) online in the mesh)"
        )
        for n in msg.get("nodes", []):
            if n["id"] != self._self["id"]:
                self.display.add_raw(f"  online: {n['nick']}")

    async def on_peers(self, msg):
        self._peers = msg.get("peers", [])
        self.refresh_status()

    async def on_node_map(self, msg):
        self._nodes_map = {n["id"]: n for n in msg.get("nodes", [])}
        own = self._nodes_map.get(self._self["id"]) if self._self else None
        if isinstance(own, dict) and "peers" in own:
            self._peers = own["peers"]
        self.refresh_status()

    async def on_node_join(self, msg):
        node = msg.get("node", {})
        self.display.add_raw(f"* mesh node joined: {node.get('nick')}")
        if node.get("id") is not None and node["id"] not in self._nodes_map:
            self._nodes_map[node["id"]] = node
        self.refresh_status()

    async def on_node_leave(self, msg):
        self.display.add_raw(f"x mesh node left: {msg.get('nick')}")

    async def on_relay(self, msg):
        abbr = (msg.get("id") or "")[:6]
        self.display.set_activity(
            f"packet {abbr} hop {msg.get('hop')}/{msg.get('hops')} forwarding via {msg.get('via')}"
        )

    async def on_message(self, msg):
        route = msg.get("route") or []
        suffix = ""
        if len(route) > 1:
            suffix = f"   [{hops_str(msg.get('hops'))}: {' -> '.join(route)}]"
        self.display.add_raw(
            f"{msg.get('from')} >> {msg.get('to')}: {msg.get('body', '')}" + suffix,
            kind="!",
        )
        if route:
            self.display.set_activity("arrived via " + " -> ".join(route))

    async def on_ack(self, msg):
        to = msg.get("to")
        route = msg.get("route") or []
        detail = f"[{hops_str(msg.get('hops'))}: {' -> '.join(route)}]" if route else ""
        self.display.add_raw(f"ack: delivered to {to} {detail}", kind="ok")
        self.display.set_activity(f"delivered to {to} {detail}")

    async def on_error(self, msg):
        reason = msg.get("reason", "unknown")
        if self._self is None:
            # Rejected during the handshake (e.g. duplicate nick) -- do not
            # linger as a zombie "connecting to mesh..." client.
            self.display.add_raw(f"connection rejected by server: {reason}", kind="!")
            await self.quit()
            return
        self.display.add_raw(f"error: {reason}", kind="!")

    async def on_show(self):
        if self._self is None:
            self.display.add_raw("not connected to a mesh yet", kind="?")
            return
        nodes = list(self._nodes_map.values())
        if not nodes:
            self.display.add_raw("no mesh data yet", kind="?")
            return
        edges = set()
        for nd in nodes:
            for pid in nd.get("peers", []):
                if pid in self._nodes_map:
                    edges.add(tuple(sorted((nd["id"], pid))))
        cols, rows = term_size()
        width = min(max(cols - 2, 40), 86)
        height = min(max(rows - 8, 10), 20)
        for line in render_mesh(nodes, sorted(edges), self_id=self._self["id"], width=width, height=height):
            self.display.add_raw(line, kind="?")

    async def on_closed(self):
        self.display.add_raw("connection to mesh server closed")


def make_display(app, args, loop):
    if not args.plain and sys.stdin.isatty() and sys.stdout.isatty():
        return ChatUI(
            submit=lambda text: asyncio.create_task(app.submit(text)),
            on_quit=lambda: asyncio.create_task(app.quit()),
            loop=loop,
        )
    return PlainUI()


async def plain_input_loop(app):
    loop = asyncio.get_running_loop()
    while not app.quit_now:
        sys.stdout.write("> ")
        sys.stdout.flush()
        try:
            line = await loop.run_in_executor(None, _read_line)
        except EOFError:
            break
        if app.quit_now:
            break
        if not line.strip():
            continue
        await app.submit(line)


def _read_line():
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        raise EOFError
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


async def run(args) -> int:
    app = App(args)
    app._quit_evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    app.display = make_display(app, args, loop)
    plain = isinstance(app.display, PlainUI)

    for kind, cb in [
        ("welcome", app.on_welcome),
        ("peers", app.on_peers),
        ("node_map", app.on_node_map),
        ("node_join", app.on_node_join),
        ("node_leave", app.on_node_leave),
        ("relay", app.on_relay),
        ("message", app.on_message),
        ("ack", app.on_ack),
        ("error", app.on_error),
    ]:
        app.client.on(kind, cb)
    app.client.on_close(app.on_closed)

    app.terminal = Terminal()
    app.keys = KeyReader(app.display.on_key, loop) if not plain else None

    try:
        app.terminal.enter_raw()
        if app.keys:
            app.keys.start()
        try:
            await app.client.connect(args.nick, timeout=args.timeout)
        except (TimeoutError, asyncio.TimeoutError):
            print(
                f"timed out after {args.timeout:g}s waiting for {args.host}:{args.port} -- the server is not answering.\n"
                "  check that:\n"
                f"    - the server was started with --host 0.0.0.0 (default {args.host} is loopback-only)\n"
                "    - you used the right address from ipconfig (IPv4 of the ACTIVE adapter, not a VPN/Hyper-V one)\n"
                "    - Windows Firewall allows the server on inbound Private networks\n"
                "    - the network permits device-to-device traffic (school/guest wifi often isolates clients -- try a hotspot)",
                file=sys.stderr,
            )
            return 1
        except (OSError, ConnectionError) as exc:
            print(f"could not connect to {args.host}:{args.port}: {exc}", file=sys.stderr)
            return 1

        reader_task = asyncio.create_task(app.client.run())
        quit_task = asyncio.create_task(app.wait_quit())
        input_task = asyncio.create_task(plain_input_loop(app)) if plain else None
        try:
            pending = {reader_task, quit_task}
            if input_task is not None:
                pending.add(input_task)
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            app.quit_now = True
        finally:
            for task in (reader_task, quit_task, input_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (reader_task, quit_task, input_task):
                if task is not None:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
    finally:
        if app.keys:
            app.keys.stop()
        app.terminal.restore()
        sys.stdout.write("\r\n")
        sys.stdout.flush()
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="simple-talk", description="mesh-network chat client")
    p.add_argument("--host", default="127.0.0.1", help="mesh server host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=protocol.DEFAULT_PORT, help="mesh server port (default %(default)s)")
    p.add_argument("--nick", default=getpass.getuser(), help="nickname (default: current user)")
    p.add_argument("--timeout", type=float, default=10.0, help="seconds to wait for the server to accept the connection (default %(default)s)")
    p.add_argument("--plain", action="store_true", help="force plain line mode (no TUI)")
    p.add_argument("--version", action="version", version=f"simple-talk {__version__}")
    return p.parse_args(argv)


def _ensure_utf8_stdio():
    """Write UTF-8 (with lossy fallback) instead of the ANSI codepage.

    ``/show`` draws box-drawing characters; on a console that would
    otherwise be cp1252/cp437 this keeps rendering from crashing (and
    preserves the nice glyphs when possible).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    enable_windows_vt()
    _ensure_utf8_stdio()
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
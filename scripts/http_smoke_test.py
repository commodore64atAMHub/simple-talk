#!/usr/bin/env python3
"""End-to-end smoke test for the HTTP transport.

Two phases, both run against a live HTTP mesh server:

  A. protocol -- drives the endpoints with plain HTTP (curl-style) and
     checks join/welcome, broadcast, unicast routing, wire snipping and
     re-attaching (including the errors), long-poll behaviour, and
     departures.
  B. client   -- runs the real TUI transport
     (``stclient.httpnet.HttpMeshClient``) through the same
     conversation, proving ``python3 client/main.py --url ...`` works.

By default it starts ``server/http.py`` itself.  Point it at any other
HTTP mesh server -- e.g. one of the templates -- with ``--cmd``:

    python3 scripts/http_smoke_test.py --cmd "node templates/node_http/server.js --port {port}"
    python3 scripts/http_smoke_test.py --cmd "python3 templates/python_flask/server.py --port {port}"
    python3 scripts/http_smoke_test.py --url http://127.0.0.1:9000   # already running

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Append (never insert): `server/http.py` exists and must not shadow the
# stdlib `http` package for this process.
sys.path.append(str(ROOT / "client"))
sys.path.append(str(ROOT / "server"))

Tolerance = 8.0  # seconds a well-behaved server gets for any one step


def req(method: str, url: str, payload=None, timeout: float = 15.0):
    """One HTTP request; HTTP errors come back as ``(status, body)``."""
    if isinstance(payload, bytes):
        data = payload
    elif payload is not None:
        data = json.dumps(payload).encode()
    else:
        data = None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {}
    except urllib.error.URLError as exc:
        raise ConnectionError(f"{method} {url}: {exc.reason}") from exc


def wait_for(cond, timeout: float = Tolerance, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.03)
    raise TimeoutError(f"timeout waiting for {what}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Peer:
    """A session on the server, collecting its event stream (phase A)."""

    def __init__(self, base: str, nick: str):
        self.base = base
        self.nick = nick
        self.token: str | None = None
        self.since = 0
        self.events: list[dict] = []

    # -- endpoints -------------------------------------------------------
    def join(self):
        status, body = req("POST", f"{self.base}/api/hello", {"nick": self.nick})
        if status == 200:
            self.token = body.get("token")
        return status, body
    def poll(self, wait: float = 1.0):
        status, body = req(
            "GET",
            f"{self.base}/api/events?token={self.token}&since={self.since}&wait={wait:g}",
            timeout=wait + 10,
        )
        if status == 200 and isinstance(body.get("events"), list):
            self.events.extend(body["events"])
            self.since = int(body.get("cursor", self.since))
        return status, body

    def send(self, to: str, text: str):
        return req(
            "POST",
            f"{self.base}/api/send",
            {"token": self.token, "to": to, "body": text},
        )

    def wire(self, action: str, a: str, b: str):
        return req(
            "POST",
            f"{self.base}/api/wire",
            {"token": self.token, "action": action, "a": a, "b": b},
        )

    def bye(self):
        return req("POST", f"{self.base}/api/bye", {"token": self.token})

    # -- assertions helpers -------------------------------------------------
    def wait_for_event(self, pred, what: str, timeout: float = Tolerance):
        def found():
            if pred(self.events):
                return True
            self.poll(wait=0.5)
            return pred(self.events)

        wait_for(found, timeout, f"{self.nick}: {what}")
        return next(e for e in self.events if pred([e]))

    def current_map(self) -> dict[str, set[str]] | None:
        """Latest node_map seen so far, as {nick: {neighbour nicks}}."""
        maps = [e for e in self.events if e.get("type") == "node_map"]
        if not maps:
            return None
        nodes = maps[-1]["nodes"]
        nick_of = {n["id"]: n["nick"] for n in nodes}
        return {n["nick"]: {nick_of[p] for p in n["peers"]} for n in nodes}

    def wait_map(self, expect: dict[str, set[str]], timeout: float = Tolerance):
        def step():
            if self.current_map() == expect:
                return True
            self.poll(wait=0.5)
            return self.current_map() == expect

        wait_for(step, timeout, f"{self.nick}: topology {expect}")
        return self.current_map()

    def wait_nodes(self, nicks: set[str], timeout: float = Tolerance):
        """Wait until node_map lists exactly these nicks."""

        def step():
            seen = self.current_map()
            if seen is not None and set(seen) == nicks:
                return True
            self.poll(wait=0.5)
            seen = self.current_map()
            return seen is not None and set(seen) == nicks

        wait_for(step, timeout, f"{self.nick}: nodes {sorted(nicks)}")
        return self.current_map()

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]


# ======================================================================
# phase A: protocol over plain HTTP
# ======================================================================
def phase_protocol(base: str) -> None:
    status, body = req("GET", f"{base}/api/ping")
    assert status == 200 and body.get("type") == "pong", (status, body)

    alice, bob, gamma = Peer(base, "alice"), Peer(base, "bob"), Peer(base, "gamma")
    peers = [alice, bob, gamma]
    for peer in peers:
        status, body = peer.join()
        assert status == 200 and body.get("token"), (peer.nick, status, body)

    # duplicate nick rejected with a protocol-shaped error
    status, body = req("POST", f"{base}/api/hello", {"nick": "Alice"})
    assert status in (400, 409) and "in use" in body.get("reason", ""), (status, body)
    status, body = req("POST", f"{base}/api/hello", {"nick": ""})
    assert status == 400 and body.get("type") == "error", (status, body)

    # welcome travels on the event stream, in TCP order:
    # peers -> node_map -> welcome -> (others) node_join
    for peer in peers:
        peer.wait_for_event(lambda es: any(e.get("type") == "welcome" for e in es), "welcome")
        welcome = next(e for e in peer.events if e.get("type") == "welcome")
        assert welcome["self"]["nick"] == peer.nick, welcome
        assert any(n["nick"] == peer.nick for n in welcome["nodes"]), welcome
    # ...and everyone converges on the full node map
    for peer in peers:
        peer.wait_nodes({"alice", "bob", "gamma"})
    alice.wait_for_event(
        lambda es: any(e.get("type") == "node_join" and e["node"]["nick"] == "gamma" for e in es),
        "gamma's join notice",
    )

    # --- broadcast floods to every node ---
    status, body = bob.send("*", "hello everyone")
    assert status == 200 and body.get("ok"), (status, body)
    for peer in (alice, gamma):
        peer.wait_for_event(
            lambda es: any(e.get("body") == "hello everyone" for e in es),
            "broadcast delivery",
        )
        msg = next(e for e in peer.events if e.get("body") == "hello everyone")
        assert msg["type"] == "message" and msg["to"] == "*", msg
    # the sender gets an ack, never its own message back
    bob.wait_for_event(lambda es: any(e.get("type") == "ack" for e in es), "ack to bob")
    assert not any(e.get("body") == "hello everyone" for e in bob.events)

    # --- unicast routes through the mesh (3 nodes, ring degree 2 = full mesh) ---
    status, body = alice.send("gamma", "direct ping")
    assert status == 200, (status, body)
    gamma.wait_for_event(
        lambda es: any(e.get("body") == "direct ping" for e in es), "unicast delivery"
    )
    msg = next(e for e in gamma.events if e.get("body") == "direct ping")
    assert msg["hops"] >= 1 and msg["route"][0] == "alice" and msg["route"][-1] == "gamma", msg
    alice.wait_for_event(
        lambda es: any(e.get("type") == "ack" and e.get("to") == "gamma" for e in es),
        "unicast ack",
    )
    # relay ticks are public (every node animates the packet)
    alice.wait_for_event(lambda es: any(e.get("type") == "relay" for e in es), "relay tick")

    # unknown target: HTTP status AND a streamed error, like TCP
    status, body = alice.send("ghost", "anyone there")
    assert status in (400, 404) and "no such node" in body.get("reason", ""), (status, body)
    alice.wait_for_event(
        lambda es: any(e.get("type") == "error" and "no such node" in e.get("reason", "") for e in es),
        "streamed no-such-node error",
    )

    # --- snip alpha<->beta: only the operator sees the snip text ---
    status, body = alice.wire("cut", "alice", "bob")
    assert status == 200, (status, body)
    alice.wait_for_event(
        lambda es: any(e.get("type") == "wire" and e.get("action") == "cut" for e in es),
        "cut event at alice",
    )
    tri = {"alice": {"gamma"}, "bob": {"gamma"}, "gamma": {"alice", "bob"}}
    for peer in peers:
        peer.wait_map(tri)
    for quiet in (bob, gamma):
        quiet.poll(wait=1.5)  # anything wrong would land inside this window
        assert "wire" not in quiet.types(), (quiet.nick, quiet.types())

    # traffic reroutes around the snip: 2 hops via gamma
    status, body = alice.send("bob", "detour please")
    assert status == 200, (status, body)
    bob.wait_for_event(
        lambda es: any(e.get("body") == "detour please" for e in es), "rerouted delivery"
    )
    detour = next(e for e in bob.events if e.get("body") == "detour please")
    assert detour["hops"] == 2 and detour["route"] == ["alice", "gamma", "bob"], detour
    alice.wait_for_event(
        lambda es: any(
            e.get("type") == "relay" and e.get("hops") == 2 and e.get("via") == "gamma"
            for e in es
        ),
        "two-hop relay animation",
    )

    # --- sever the mesh: bob ends up isolated -> no route ---
    alice.wire("cut", "bob", "gamma")
    split = {"alice": {"gamma"}, "bob": set(), "gamma": {"alice"}}
    alice.wait_map(split)
    status, body = alice.send("bob", "are you there")
    assert status in (400, 409) and "no route" in body.get("reason", ""), (status, body)
    alice.wait_for_event(
        lambda es: any(e.get("type") == "error" and "no route" in e.get("reason", "") for e in es),
        "streamed no-route error",
    )

    # --- wire errors (rejected WITHOUT changing the topology) ---
    status, body = alice.wire("cut", "alice", "bob")  # already snipped above
    assert status in (400, 409) and "no such wire" in body.get("reason", ""), (status, body)
    status, body = alice.wire("cut", "alice", "ghost")
    assert status in (400, 404) and "no such node" in body.get("reason", ""), (status, body)
    status, body = alice.wire("cut", "alice", "alice")
    assert status == 400 and "two different" in body.get("reason", ""), (status, body)
    status, body = alice.wire("teleport", "alice", "bob")
    assert status == 400 and "unknown wire action" in body.get("reason", ""), (status, body)
    assert alice.current_map() == split, alice.current_map()

    # --- weld it back: both endpoints (and the operator) get the notice ---
    status, body = alice.wire("link", "alice", "bob")
    assert status == 200, (status, body)
    for peer in (alice, bob):
        peer.wait_for_event(
            lambda es: any(e.get("type") == "wire" and e.get("action") == "link" for e in es),
            f"link event at {peer.nick}",
        )
    status, body = alice.wire("link", "alice", "bob")  # duplicate link
    assert status in (400, 409) and "already exists" in body.get("reason", ""), (status, body)
    healed = {"alice": {"gamma", "bob"}, "bob": {"alice"}, "gamma": {"alice"}}
    gamma.wait_map(healed)
    assert "wire" not in gamma.types(), gamma.types()  # not a party to this wire

    # --- long-poll returns as soon as an event lands ---
    timing = {}
    # drain the observer's welcome burst first, then time a parked poll
    observer = Peer(base, "drifter")
    assert observer.join()[0] == 200
    observer.poll(wait=1.0)
    first_events = len(observer.events)
    assert first_events >= 3, first_events  # peers, node_map, welcome, node_join...

    def parked_poll():
        started = time.monotonic()
        status, body = req(
            "GET",
            f"{base}/api/events?token={observer.token}&since={observer.since}&wait=8",
            timeout=15,
        )
        timing["elapsed"] = time.monotonic() - started
        timing["status"] = status

    watcher = threading.Thread(target=parked_poll)
    watcher.start()
    time.sleep(0.4)
    alice.send("*", "poke the observer")
    watcher.join(timeout=12)
    assert not watcher.is_alive(), "long-poll did not return"
    assert timing["status"] == 200, timing
    assert timing["elapsed"] < 5, f"long-poll ignored the new event: {timing}"
    observer.poll(wait=0.5)
    assert any(e.get("body") == "poke the observer" for e in observer.events), observer.types()
    assert "wire" not in observer.types()  # saw no private wire chatter

    # --- auth + endpoint hygiene ---
    status, body = req("GET", f"{base}/api/events?token=bogus&since=0&wait=0")
    assert status in (400, 401) and body.get("type") == "error", (status, body)
    status, body = req("POST", f"{base}/api/send", {"token": "bogus", "to": "*", "body": "x"})
    assert status in (401, 403) and body.get("type") == "error", (status, body)
    status, body = req("POST", f"{base}/api/hello", "not an object".encode())
    assert status == 400, (status, body)
    status, body = req("GET", f"{base}/nope")
    assert status == 404, (status, body)
    status, body = req("GET", f"{base}/api/state")
    assert status == 200 and len(body.get("nodes", [])) >= 3, (status, body)
    observer.bye()  # the witness leaves before the departure checks below

    # --- departures are announced ---
    status, body = gamma.bye()
    assert status == 200, (status, body)
    alice.wait_for_event(
        lambda es: any(
            e.get("type") == "node_leave" and (e.get("node") or {}).get("nick") == "gamma"
            for e in es
        ),
        "gamma's departure",
    )
    for peer in (alice, bob):
        peer.wait_map({"alice": {"bob"}, "bob": {"alice"}})
    status, body = req("GET", f"{base}/api/state")
    assert len(body.get("nodes", [])) == 2, body

    for peer in (alice, bob):
        peer.bye()


# ======================================================================
# phase B: the real client transport
# ======================================================================
async def await_for(cond, timeout: float = Tolerance, what: str = "condition"):
    """Event-loop friendly twin of :func:`wait_for` (never blocks the loop)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.03)
    raise TimeoutError(f"timeout waiting for {what}")


def phase_client(base: str) -> None:
    from stclient.httpnet import HttpMeshClient

    async def main() -> None:
        async def box_for(nick: str):
            client = HttpMeshClient(base, poll_wait=5.0)
            box: dict = {
                "welcome": [],
                "messages": [],
                "acks": [],
                "errors": [],
                "relays": [],
                "wires": [],
                "leaves": [],
                "maps": [],
            }

            async def collect(kind, key):
                async def handler(msg):
                    box[key].append(msg)

                client.on(kind, handler)

            for kind, key in (
                ("welcome", "welcome"),
                ("message", "messages"),
                ("ack", "acks"),
                ("error", "errors"),
                ("relay", "relays"),
                ("wire", "wires"),
                ("node_leave", "leaves"),
                ("node_map", "maps"),
            ):
                await collect(kind, key)
            return client, box

        xray, x_box = await box_for("xray")
        yankee, y_box = await box_for("yankee")
        tasks = []
        try:
            await xray.connect("xray")
            await yankee.connect("yankee")
            tasks = [asyncio.create_task(xray.run()), asyncio.create_task(yankee.run())]

            await await_for(lambda: x_box["welcome"], what="xray welcome")
            await await_for(lambda: y_box["welcome"], what="yankee welcome")
            assert x_box["welcome"][0]["self"]["nick"] == "xray", x_box["welcome"][0]

            await xray.say("*", "broadcast over http")
            await await_for(
                lambda: any(m.get("body") == "broadcast over http" for m in y_box["messages"]),
                what="broadcast to yankee",
            )

            await xray.say("yankee", "unicast over http")
            await await_for(
                lambda: any(m.get("body") == "unicast over http" for m in y_box["messages"]),
                what="unicast to yankee",
            )
            msg = next(m for m in y_box["messages"] if m.get("body") == "unicast over http")
            assert msg["route"][0] == "xray" and msg["route"][-1] == "yankee", msg
            await await_for(lambda: x_box["acks"], what="ack to xray")
            await await_for(lambda: x_box["relays"], what="relay animation at xray")

            await xray.wire("cut", "xray", "yankee")
            await await_for(lambda: x_box["wires"], what="cut event")

            def topology(box):
                maps = [e for e in box["maps"] if e.get("type") == "node_map"]
                if not maps:
                    return None
                nodes = maps[-1]["nodes"]
                nick_of = {n["id"]: n["nick"] for n in nodes}
                return {n["nick"]: {nick_of[p] for p in n["peers"]} for n in nodes}

            isolated = {"xray": set(), "yankee": set()}
            await await_for(lambda: topology(x_box) == isolated, what="cut visible at xray")
            await await_for(lambda: topology(y_box) == isolated, what="cut visible at yankee")

            # duplicate nick: rejected exactly like the TCP handshake
            dup, d_box = await box_for("xray")
            rejected = False
            try:
                await dup.connect("xray")
            except (ConnectionError, OSError):
                rejected = True
            assert rejected, "duplicate nick was not rejected"
            await await_for(lambda: d_box["errors"], what="streamed rejection reason")
            assert "in use" in d_box["errors"][0].get("reason", ""), d_box["errors"]

            await yankee.bye()
            await await_for(
                lambda: any(
                    (l.get("node") or {}).get("nick") == "yankee" for l in x_box["leaves"]
                ),
                what="departure notice at xray",
            )
        finally:
            for client in (xray, yankee):
                client.close()
            for task in tasks:
                task.cancel()
            await asyncio.sleep(0.05)

    asyncio.run(main())


# ======================================================================
# runner
# ======================================================================
def wait_ready(base: str, timeout: float, proc: subprocess.Popen | None, log) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            log.flush()
            raise RuntimeError(f"server exited with code {proc.returncode}")
        try:
            status, body = req("GET", f"{base}/api/ping", timeout=2)
            if status == 200:
                return
        except (ConnectionError, OSError):
            pass
        time.sleep(0.25)
    log.flush()
    raise TimeoutError(f"server at {base} did not answer /api/ping within {timeout:g}s")


def main() -> int:
    p = argparse.ArgumentParser(description="HTTP transport smoke test")
    p.add_argument("--url", help="test an already-running server instead of spawning one")
    p.add_argument("--cmd", help="command to spawn a server; {port} (or a trailing --port) is substituted")
    p.add_argument("--ready-timeout", type=float, default=60.0,
                   help="seconds to wait for the spawned server (go run compiles first)")
    args = p.parse_args()

    proc = None
    log = sys.stdout
    if args.url:
        base = args.url.rstrip("/")
        wait_ready(base, args.ready_timeout, None, log)
    else:
        port = free_port()
        if args.cmd and "{port}" in args.cmd:
            command = args.cmd.format(port=port)
        elif args.cmd:
            command = f"{args.cmd} --port {port}"
        else:
            command = f"{sys.executable} {ROOT / 'server' / 'http.py'} --port {port} --hop-ms 40 --seed 7"
        base = f"http://127.0.0.1:{port}"
        tmp = tempfile.NamedTemporaryFile(
            "w+", prefix="http-smoke-", suffix=".log", delete=False
        )
        log = open(tmp.name, "w+")
        print(f"spawning: {command}")
        proc = subprocess.Popen(
            command, shell=True, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT)
        )
        try:
            wait_ready(base, args.ready_timeout, proc, log)
        except Exception as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            _dump_log(log)
            _stop(proc)
            return 1

    print(f"phase A: protocol checks against {base}")
    try:
        phase_protocol(base)
        print("phase A: ok")
        print("phase B: client transport (stclient.httpnet)")
        phase_client(base)
        print("phase B: ok")
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"FAIL: {exc}", file=sys.stderr)
        _dump_log(log)
        return 1
    finally:
        if proc is not None:
            _stop(proc)

    print("HTTP SMOKE TEST PASS")
    print("  join/welcome, duplicate nick, broadcast, unicast routing")
    print("  wire snip (private notice), reroute, sever -> no route, heal")
    print("  long-poll wakes on events; auth errors; departures announced")
    print("  client transport: connect/say/wire/bye over HTTP")
    return 0


def _dump_log(log) -> None:
    try:
        log.flush()
        with open(log.name if hasattr(log, "name") else log, "r") as fh:
            text = fh.read().strip()
        if text:
            print("--- server log ---", file=sys.stderr)
            print(text[-4000:], file=sys.stderr)
    except Exception:
        pass


def _stop(proc: subprocess.Popen) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Smoke test for simple-talk: runs the mesh server in-process and
exercises unicast routing, broadcast flooding, wire snipping/relinking
(the /dc, /cn protocol -- including sculpting a five-node bus),
departure notices, and duplicate-nick rejection through the real wire
protocol.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "server"))

from stclient.net import MeshClient  # noqa: E402
from stserver.server import ChatServer  # noqa: E402


async def wait_for(cond, timeout=6.0, what="condition"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.03)
    raise TimeoutError(f"timeout waiting for {what}")


def connect_client(host, port, nick):
    c = MeshClient(host, port)
    box = {
        "welcome": None,
        "messages": [],
        "acks": [],
        "errors": [],
        "wires": [],
        "maps": [],
        "leaves": [],
    }

    async def h_welcome(m):
        box["welcome"] = m

    async def h_msg(m):
        box["messages"].append(m)

    async def h_ack(m):
        box["acks"].append(m)

    async def h_err(m):
        box["errors"].append(m)

    async def h_wire(m):
        box["wires"].append(m)

    async def h_map(m):
        box["maps"].append(m)

    async def h_leave(m):
        box["leaves"].append(m)

    c.on("welcome", h_welcome)
    c.on("message", h_msg)
    c.on("ack", h_ack)
    c.on("error", h_err)
    c.on("wire", h_wire)
    c.on("node_map", h_map)
    c.on("node_leave", h_leave)
    return c, box


async def main() -> int:
    srv = ChatServer("127.0.0.1", 0, hop_ms=50, seed=7)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    clients: list[MeshClient] = []
    tasks: list[asyncio.Task] = []
    try:
        alpha, a_box = connect_client("127.0.0.1", port, "alpha")
        beta, b_box = connect_client("127.0.0.1", port, "beta")
        gamma, g_box = connect_client("127.0.0.1", port, "gamma")
        clients += [alpha, beta, gamma]
        for c, nick in ((alpha, "alpha"), (beta, "beta"), (gamma, "gamma")):
            await c.connect(nick)
        tasks = [asyncio.create_task(c.run()) for c in (alpha, beta, gamma)]

        for name, box, nick in (("alpha", a_box, "alpha"), ("beta", b_box, "beta"), ("gamma", g_box, "gamma")):
            await wait_for(lambda b=box: b["welcome"] is not None, what=f"{name} welcome")

        await asyncio.sleep(0.3)

        # --- unicast through the mesh ---
        await alpha.say("beta", "ping via mesh")
        await wait_for(
            lambda: any(m.get("body") == "ping via mesh" and m.get("to") == "beta" for m in b_box["messages"]),
            what="unicast delivery to beta",
        )
        msg = next(m for m in b_box["messages"] if m.get("body") == "ping via mesh")
        hops = msg.get("hops")
        assert hops >= 1, f"expected >=1 hops, got {hops}"
        assert list(msg.get("route", []))[0] == "alpha"
        assert list(msg.get("route", []))[-1] == "beta"
        await wait_for(
            lambda: any(m.get("id") == msg["id"] for m in a_box["acks"]),
            what="ack to alpha",
        )
        # --- broadcast flooding ---
        await gamma.say("*", "hello everyone")
        await wait_for(
            lambda: any(m.get("to") == "*" and m.get("body") == "hello everyone" for m in a_box["messages"]),
            what="broadcast to alpha",
        )

        # --- snip / re-attach wires: sculpt your own topology ---
        # NOTE: a client's welcome snapshot is taken when *it* joins
        # (alpha joined alone), so live topology comes from node_map.
        def topology(box):
            """Latest node_map as {nick: {neighbor nicks}}."""
            nodes = box["maps"][-1]["nodes"]
            nick_of = {n["id"]: n["nick"] for n in nodes}
            return {n["nick"]: {nick_of[p] for p in n["peers"]} for n in nodes}

        await wait_for(
            lambda: a_box["maps"]
            and {n["nick"] for n in a_box["maps"][-1]["nodes"]} == {"alpha", "beta", "gamma"},
            what="full node map",
        )
        assert topology(a_box) == {
            "alpha": {"beta", "gamma"},
            "beta": {"alpha", "gamma"},
            "gamma": {"alpha", "beta"},
        }, topology(a_box)  # 3 nodes, ring degree 2 -> complete graph

        # 1) snip alpha-beta: the operator alone sees the snip text, and
        #    traffic reroutes via gamma
        await alpha.wire("cut", "alpha", "beta")
        await wait_for(lambda: a_box["wires"], what="cut event at alpha")
        ev = a_box["wires"][-1]
        assert (ev.get("action"), ev.get("a"), ev.get("b"), ev.get("by")) == (
            "cut",
            "alpha",
            "beta",
            "alpha",
        ), ev
        tri = {
            "alpha": {"gamma"},
            "beta": {"gamma"},
            "gamma": {"alpha", "beta"},
        }
        await wait_for(lambda: a_box["maps"] and topology(a_box) == tri, what="cut in node map")
        await wait_for(lambda: b_box["maps"] and topology(b_box) == tri, what="cut visible at beta")
        await wait_for(lambda: g_box["maps"] and topology(g_box) == tri, what="cut visible at gamma")
        # snip text is private: the others learn through node_map alone
        # (any wire event would sit BEFORE their fresh map on the same
        # stream, so these silence checks are race-free)
        assert b_box["wires"] == [] and g_box["wires"] == [], (b_box["wires"], g_box["wires"])

        await alpha.say("beta", "detour please")
        await wait_for(
            lambda: any(m.get("body") == "detour please" for m in b_box["messages"]),
            what="rerouted delivery",
        )
        detour = next(m for m in b_box["messages"] if m.get("body") == "detour please")
        assert detour["hops"] == 2 and detour["route"] == ["alpha", "gamma", "beta"], detour

        # 2) snip beta's last wire too -> beta is isolated, no route
        await alpha.wire("cut", "beta", "gamma")
        await wait_for(
            lambda: a_box["maps"] and topology(a_box) == {"alpha": {"gamma"}, "beta": set(), "gamma": {"alpha"}},
            what="beta isolated",
        )
        await alpha.say("beta", "are you there")
        await wait_for(lambda: a_box["errors"], what="no-route error")
        assert "no route" in a_box["errors"][-1].get("reason", ""), a_box["errors"]

        # 3) rebuild part of it by hand -> exact shape, no auto shortcuts pop back
        await alpha.wire("link", "alpha", "beta")
        bus = {"alpha": {"gamma", "beta"}, "beta": {"alpha"}, "gamma": {"alpha"}}
        await wait_for(lambda: len(a_box["wires"]) >= 2, what="link text at the operator")
        await wait_for(
            lambda: b_box["wires"] and b_box["wires"][-1].get("action") == "link",
            what="link text at the other end (beta)",
        )
        await wait_for(lambda: a_box["maps"] and topology(a_box) == bus, what="link in node map")
        await wait_for(lambda: g_box["maps"] and topology(g_box) == bus, what="link map at gamma")
        assert g_box["wires"] == [], g_box["wires"]  # not a party to this wire: no text
        await alpha.say("beta", "wire is back")
        await wait_for(
            lambda: any(m.get("body") == "wire is back" for m in b_box["messages"]),
            what="delivery after relink",
        )
        back = next(m for m in b_box["messages"] if m.get("body") == "wire is back")
        assert back["route"] == ["alpha", "beta"], back

        # 4) errors: duplicate cut, duplicate link, unknown node
        #    (all rejected WITHOUT changing the topology)
        await alpha.wire("cut", "beta", "gamma")  # still snipped from step 2
        await wait_for(lambda: len(a_box["errors"]) >= 2, what="duplicate-cut error")
        assert "no such wire" in a_box["errors"][-1].get("reason", ""), a_box["errors"]
        await alpha.wire("link", "alpha", "beta")  # already linked in step 3
        await wait_for(lambda: len(a_box["errors"]) >= 3, what="duplicate-link error")
        assert "already exists" in a_box["errors"][-1].get("reason", ""), a_box["errors"]
        await alpha.wire("cut", "alpha", "ghost")
        await wait_for(lambda: len(a_box["errors"]) >= 4, what="unknown-node error")
        assert "no such node" in a_box["errors"][-1].get("reason", ""), a_box["errors"]
        assert topology(a_box) == bus  # still exactly our bus: nothing re-rolled

        # 5) restore the full mesh for good measure
        await alpha.wire("link", "beta", "gamma")
        full = {
            "alpha": {"gamma", "beta"},
            "beta": {"alpha", "gamma"},
            "gamma": {"alpha", "beta"},
        }
        await wait_for(lambda: a_box["maps"] and topology(a_box) == full, what="full mesh restored")
        await wait_for(
            lambda: g_box["wires"] and g_box["wires"][-1].get("action") == "link",
            what="link text at gamma (it is an endpoint)",
        )

        # --- sculpt a 5-node bus: alpha-beta-gamma-delta-epsilon ---
        # delta and epsilon join a mesh that is already being sculpted:
        # each newcomer gets one default wire and no auto shortcuts return.
        delta, dl_box = connect_client("127.0.0.1", port, "delta")
        epsilon, ep_box = connect_client("127.0.0.1", port, "epsilon")
        await delta.connect("delta")
        await epsilon.connect("epsilon")
        clients += [delta, epsilon]
        tasks += [asyncio.create_task(delta.run()), asyncio.create_task(epsilon.run())]
        five = {"alpha", "beta", "gamma", "delta", "epsilon"}
        await wait_for(
            lambda: a_box["maps"] and {n["nick"] for n in a_box["maps"][-1]["nodes"]} == five,
            what="5-node map",
        )
        assert topology(a_box) == {
            "alpha": {"beta", "gamma"},
            "beta": {"alpha", "gamma"},
            "gamma": {"alpha", "beta", "delta"},
            "delta": {"gamma", "epsilon"},
            "epsilon": {"delta"},
        }, topology(a_box)

        # one snip shapes the bus
        await alpha.wire("cut", "alpha", "gamma")
        bus5 = {
            "alpha": {"beta"},
            "beta": {"alpha", "gamma"},
            "gamma": {"beta", "delta"},
            "delta": {"gamma", "epsilon"},
            "epsilon": {"delta"},
        }
        await wait_for(lambda: a_box["maps"] and topology(a_box) == bus5, what="bus carved")

        # end-to-end traffic crawls the whole line, one hop at a time
        await alpha.say("epsilon", "end of the line")
        await wait_for(
            lambda: any(m.get("body") == "end of the line" for m in ep_box["messages"]),
            what="bus end-to-end delivery",
        )
        e2e = next(m for m in ep_box["messages"] if m.get("body") == "end of the line")
        assert e2e["hops"] == 4 and e2e["route"] == ["alpha", "beta", "gamma", "delta", "epsilon"], e2e
        await delta.say("alpha", "back down the bus")
        await wait_for(
            lambda: any(m.get("body") == "back down the bus" for m in a_box["messages"]),
            what="bus reverse delivery",
        )
        rev = next(m for m in a_box["messages"] if m.get("body") == "back down the bus")
        assert rev["hops"] == 3 and rev["route"] == ["delta", "gamma", "beta", "alpha"], rev

        # sever the middle: two islands, no route across
        await alpha.wire("cut", "beta", "gamma")
        halves = {
            "alpha": {"beta"},
            "beta": {"alpha"},
            "gamma": {"delta"},
            "delta": {"gamma", "epsilon"},
            "epsilon": {"delta"},
        }
        await wait_for(lambda: a_box["maps"] and topology(a_box) == halves, what="bus split")
        await alpha.say("epsilon", "anyone over there")
        await wait_for(lambda: len(a_box["errors"]) >= 5, what="no route across the severed bus")
        assert "no route" in a_box["errors"][-1].get("reason", ""), a_box["errors"]
        # affected non-operators still get no snip text
        await wait_for(lambda: b_box["maps"] and topology(b_box) == halves, what="split at beta")
        await wait_for(lambda: g_box["maps"] and topology(g_box) == halves, what="split at gamma")
        assert len(b_box["wires"]) == 2 and len(g_box["wires"]) == 1, (b_box["wires"], g_box["wires"])

        # weld it back and traffic flows end to end again
        await alpha.wire("link", "beta", "gamma")
        await wait_for(lambda: a_box["maps"] and topology(a_box) == bus5, what="bus healed")
        await wait_for(lambda: len(b_box["wires"]) >= 3, what="heal text at beta (endpoint)")
        await alpha.say("epsilon", "welded")
        await wait_for(
            lambda: any(m.get("body") == "welded" for m in ep_box["messages"]),
            what="post-heal delivery",
        )
        healed = next(m for m in ep_box["messages"] if m.get("body") == "welded")
        assert healed["hops"] == 4, healed

        # --- departures are announced: epsilon quits the mesh ---
        epsilon.close()
        await wait_for(
            lambda: any((l.get("node") or {}).get("nick") == "epsilon" for l in a_box["leaves"]),
            what="node_leave at alpha",
        )
        await wait_for(
            lambda: any((l.get("node") or {}).get("nick") == "epsilon" for l in g_box["leaves"]),
            what="node_leave at gamma",
        )
        four = {"alpha", "beta", "gamma", "delta"}
        await wait_for(
            lambda: a_box["maps"] and {n["nick"] for n in a_box["maps"][-1]["nodes"]} == four,
            what="map drops epsilon",
        )
        assert topology(a_box) == {
            "alpha": {"beta"},
            "beta": {"alpha", "gamma"},
            "gamma": {"beta", "delta"},
            "delta": {"gamma"},
        }, topology(a_box)

        # --- duplicate nick rejected ---
        dup, d_box = connect_client("127.0.0.1", port, "alpha")
        await dup.connect("alpha")
        d_task = asyncio.create_task(dup.run())
        clients.append(dup)
        tasks.append(d_task)
        await wait_for(lambda: d_box["errors"], what="duplicate-nick rejection")
        assert any("in use" in e.get("reason", "") for e in d_box["errors"])
        d_task.cancel()

        print("SMOKE TEST PASS")
        print(f"  unicast: alpha -> beta  via {' -> '.join(msg['route'])}  ({hops} hop(s))")
        print("  broadcast: gamma -> all  delivered")
        print("  wires: snipped, rerouted via gamma, isolated (no route), relinked")
        print("  bus: carved, severed (no route), healed; epsilon's leave announced")
        print("  duplicate nick 'alpha' rejected")

        return 0
    finally:
        # Close clients BEFORE shutdown: the server's wait_closed() blocks
        # until its connection handlers finish, and those sit in readline()
        # until the peer disconnects -- otherwise any test failure would
        # hang here forever inside finally and never print its traceback.
        for c in clients:
            c.close()
        for t in tasks:
            t.cancel()
        await asyncio.sleep(0.1)
        try:
            await asyncio.wait_for(srv.shutdown(), timeout=5)
        except Exception as e:  # never mask the test's real failure
            print(f"shutdown issue: {e!r}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
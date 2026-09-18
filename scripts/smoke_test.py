#!/usr/bin/env python3
"""Smoke test for simple-talk: runs the mesh server in-process and
exercises unicast routing, broadcast flooding, and duplicate-nick
rejection through the real wire protocol.
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
    box = {"welcome": None, "messages": [], "acks": [], "errors": []}

    async def h_welcome(m):
        box["welcome"] = m

    async def h_msg(m):
        box["messages"].append(m)

    async def h_ack(m):
        box["acks"].append(m)

    async def h_err(m):
        box["errors"].append(m)

    c.on("welcome", h_welcome)
    c.on("message", h_msg)
    c.on("ack", h_ack)
    c.on("error", h_err)
    return c, box


async def main() -> int:
    srv = ChatServer("127.0.0.1", 0, hop_ms=50, seed=7)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        alpha, a_box = connect_client("127.0.0.1", port, "alpha")
        beta, b_box = connect_client("127.0.0.1", port, "beta")
        gamma, g_box = connect_client("127.0.0.1", port, "gamma")
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

        # --- duplicate nick rejected ---
        dup, d_box = connect_client("127.0.0.1", port, "alpha")
        await dup.connect("alpha")
        d_task = asyncio.create_task(dup.run())
        await wait_for(lambda: d_box["errors"], what="duplicate-nick rejection")
        assert any("in use" in e.get("reason", "") for e in d_box["errors"])
        d_task.cancel()

        print("SMOKE TEST PASS")
        print(f"  unicast: alpha -> beta  via {' -> '.join(msg['route'])}  ({hops} hop(s))")
        print("  broadcast: gamma -> all  delivered")
        print("  duplicate nick 'alpha' rejected")

        for c in (alpha, beta, gamma):
            c.close()
        await asyncio.sleep(0.3)
        return 0
    finally:
        await srv.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
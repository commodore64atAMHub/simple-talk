# simple-talk

A tiny mesh-network chat. A single **Python server** imitates a wireless
**mesh network**: every connected client becomes a *node* on a graph, and
messages are **store-and-forwarded hop-by-hop** through neighbouring nodes
with simulated per-hop latency — so packets visibly travel across the mesh
instead of being shot straight point-to-point.

The **client** has a no-dependency ANSI TUI and compiles to **a single
binary with Nuitka** (`--onefile`) via the Makefile.

```
Terminal 1                     Terminal 2                  Terminal 3
$ make dev-server    ->        $ make dev -- --nick alice   $ make dev -- --nick bob
```

## Quick start

```sh
# 1. terminal: start the mesh
python3 server/main.py

# 2. terminals: join as nodes (add as many as you like)
python3 client/main.py --nick alice
python3 client/main.py --nick bob
```

Type a line to **broadcast** to the whole mesh; `/msg bob hello` to route a
message to one node. Watch the status line and per-hop activity as packets
hop node-to-node.

TCP not your thing (firewall, proxy, a locked-down network, a browser
tab)? The same chat also runs over HTTP — see
[HTTP mode](#http-mode-when-tcp-is-annoying).

### Building the single-binary client (and server)

Requires a C toolchain (gcc/clang/MSVC) and `pip install nuitka`.

```sh
make deps          # pip install "nuitka[onefile]" (+ patchelf on Linux)
make               # generic build: compiles both binaries (same as `make all`)
make client        # -> dist/simple-talk        (Windows: dist/simple-talk.exe)
make server        # -> dist/simple-talk-server
make server-http   # -> dist/simple-talk-http-server   (the HTTP server)
make all           # both of the TCP pair

make install       # build, then install binaries into ~/.local/bin
make install PREFIX=/usr/local     # or a custom prefix
make uninstall     # remove installed binaries

make test          # end-to-end smoke test of the mesh
make test-http     # conformance test of the HTTP server + client
make run -- --nick alice          # run the built client binary
make run-server                   # run the built server binary
make clean
```

`make install` copies `simple-talk` and `simple-talk-server` into
`$(PREFIX)/bin` (default `~/.local/bin`, so no root needed) and marks them
executable. Use `DESTDIR=/tmp/pkg` for staged packaging. The installer is a
small cross-platform Python helper (`scripts/install.py`), so it works on
Windows too.

Notes on cross-platform builds:

* Linux/macOS: needs `gcc`/`clang` (Xcode CLT on macOS). On Linux Nuitka's
  onefile mode also needs `patchelf` — `make deps` installs it via pip.
* Windows: use GNU make (Git for Windows/MSYS2) **or** run the Nuitka
  command directly in cmd/PowerShell:
  `python -m nuitka --onefile --assume-yes-for-downloads --remove-output --output-dir=dist client/main.py`
  Windows builds produce `dist/simple-talk.exe` and use MSVC. The TUI
  auto-enables VT processing on console.
* The client is pure standard library — no pip deps to bundle — so the
  onefile binary stays small (a few MB compressed with `nuitka[onefile]`,
  which pulls in `zstandard`). This includes the tkinter mesh window;
  on Linux distributions that ship `python3-tk` separately, `/show`
  simply falls back to the ASCII map.

## Commands

| Command           | Meaning                                   |
| ----------------- | ----------------------------------------- |
| `any text`        | broadcast to every node on the mesh       |
| `/msg <nick> ...` | route a unicast message via the mesh      |
| `/show`           | open the live mesh window (ASCII map in `--plain`) |
| `/dc <nick>`      | snip a wire — traffic reroutes around it |
| `/cn <nick>`      | re-attach a snipped wire                  |
| `/help`           | list commands                             |
| `/quit`           | leave (Ctrl-D / Ctrl-C also work)         |

Wire confirmations stay between the right people: a `/dc` snip prints only
on the operator's screen, while a `/cn` re-attach prints for both ends of
the new wire. Every map and status bar still updates either way.

`/show` opens a live window (tkinter — still pure standard library, no pip
deps) showing nodes as labelled circles with your node highlighted. It
redraws itself when nodes join or leave, fades a marker where a departed
node stood (the log shows `* mesh node left: <nick>`), and animates every
packet walking hop-by-hop along its route; close it whenever you like. It
falls back to an
ASCII map in the log under `--plain`, on a Python built without tkinter, or
if the window fails to open.

## Server options

```
python3 server/main.py --help
  --host HOST      bind address            (default 127.0.0.1)
  --port PORT      listen port             (default 8765)
  --hop-ms N       simulated per-hop latency in ms (default 120)
  --seed N         fix the random topology
```

## HTTP mode (when TCP is annoying)

Sometimes a raw socket is the annoying part: a firewall rule, a proxy,
a school network, a browser tab. `server/http.py` runs the **same mesh
simulation** over a small JSON API instead:

```sh
# terminal 1: the HTTP server (default port 8080, not 8765)
python3 server/http.py

# terminal 2+: the very same TUI, pointed at a URL
python3 client/main.py --url http://127.0.0.1:8080 --nick alice

# ...or just open the chat page the server hosts at /
open http://127.0.0.1:8080/
```

It takes the same flags as `server/main.py` (`--host`, `--port`,
`--hop-ms`, `--seed`, `--log-level` — plus `--ui` to pick which HTML
file is served at `/`) and speaks the identical event frames, so every
command, the `/show` mesh window and the whole TUI behave exactly as
over TCP — only the transport differs. The browser page is the same
chat from a third kind of client: join, broadcast, `/msg`, `/dc`,
`/cn`, `/part`.

The API contract — endpoints, event frames, behavioural rules — is
documented in [`templates/README.md`](templates/README.md). That
directory holds interchangeable, copy-paste single-file servers in
other frameworks and languages; each one is the same chat, drop-in
behind the same URL:

| Template                  | Needs                          |
| ------------------------- | ------------------------------ |
| `templates/python_stdlib` | nothing (Python standard lib)  |
| `templates/python_flask`  | `pip install flask`            |
| `templates/python_fastapi`| `pip install fastapi uvicorn`  |
| `templates/node_http`     | nothing (Node's `http` module) |
| `templates/node_express`  | `npm install express`          |
| `templates/go_nethttp`    | nothing (Go `net/http`)        |
| `templates/browser`       | the chat page itself (served by every server at `/`) |

Every implementation — theirs, yours, or `server/http.py` — is checked
by one conformance test:

```sh
python3 scripts/http_smoke_test.py                                        # server/http.py
python3 scripts/http_smoke_test.py --cmd "node templates/node_http/server.js --port {port}"
```

## Playing across machines (LAN)

The server binds **loopback only** by default, so a remote friend will not
reach it. On the host machine:

```sh
python3 server/main.py --host 0.0.0.0
```

The server then prints every IPv4 address other machines can connect to,
plus Windows-firewall hints. On the friend's machine:

```sh
python3 client/main.py --host <host-ip> --nick buddy
```

Where `<host-ip>` is the IPv4 from `ipconfig` **of the active adapter**
(Wi-Fi / ethernet — not a Hyper-V or VPN virtual adapter); run `ipconfig`
and pick the one that is on the same subnet as your friend. The client
fails fast with a checklist if the server never answers.

Common reasons two school laptops can't talk even with `--host 0.0.0.0`:

* **Same default nick.** Both laptops often share the same logged-in
  username, so both default to the same nick and the second one is
  rejected with `nick already in use`. Pass `--nick` explicitly on both.
* **Windows Firewall.** Allow python / `simple-talk-server` on inbound
  *Private* networks; on a *Public* (or domain-managed) profile inbound
  connections are silently dropped.
* **Wi-Fi client isolation.** Many school/guest networks block
  device-to-device traffic entirely. Fall back to a phone hotspot, or
  cable the two laptops together.
* **Wrong address.** The `ipconfig` output lists several adapters; use the
  one that shares the same subnet as your friend's laptop.

## How the mesh works

1. **Topology.** Every joined client becomes a `Node` placed on a
   deterministic golden-spiral layout (`server/stserver/mesh.py`). Nodes
   are linked to ±`ring_degree` ring neighbours plus a few random shortcut
   links, yielding a connected, evenly-meshed graph. Joining/leaving
   recomputes adjacency and every node gets a fresh neighbour list.

2. **Routing.** Unicast messages are routed through the graph with a BFS
   shortest path. Each hop costs a simulated transmission delay, and the
   server emits a `relay` event per hop to every node, so any client's
   live mesh window can animate the packet traversing the mesh.
   Broadcasts flood to every node. Anyone can reshape the graph live
   with `/dc` and `/cn`: snip and re-attach wires until you have your
   own topology — bus, ring, star, full mesh — and watch routing follow
   the shape you built (or fail with `no route` when you isolate a
   node). Manual edits take over from the automatic ring/shortcut
   links, so nothing you didn't ask for pops back in.

3. **Wire protocol.** Newline-delimited JSON over TCP
   (`client/stclient/protocol.py`, `server/stserver/protocol.py`):
   `hello`, `welcome`, `peers`, `node_map`, `node_join`, `node_leave`,
   `send`, `relay`, `message`, `ack`, `cut`, `link`, `wire`, `error`.

## Layout

```
client/            client: main.py (entry) + stclient/ package:
                   net.py (TCP transport), httpnet.py (HTTP transport),
                   ui.py, protocol.py, meshview.py (ASCII map),
                   meshwindow.py (live GUI), __main__.py
server/            server: main.py (TCP entry), http.py (HTTP entry) +
                   stserver/ package: mesh.py, server.py (TCP engine),
                   httpchat.py, httpserv.py (HTTP engine), protocol.py
templates/         interchangeable HTTP servers + browser chat page
                   (see templates/README.md)
scripts/           smoke_test.py (TCP end-to-end), http_smoke_test.py (HTTP conformance)
Makefile           cross-platform build + dev/test targets
```

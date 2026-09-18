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

### Building the single-binary client (and server)

Requires a C toolchain (gcc/clang/MSVC) and `pip install nuitka`.

```sh
make deps          # pip install "nuitka[onefile]" (+ patchelf on Linux)
make               # generic build: compiles both binaries (same as `make all`)
make client        # -> dist/simple-talk        (Windows: dist/simple-talk.exe)
make server        # -> dist/simple-talk-server
make all           # both

make install       # build, then install binaries into ~/.local/bin
make install PREFIX=/usr/local     # or a custom prefix
make uninstall     # remove installed binaries

make test          # end-to-end smoke test of the mesh
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
  which pulls in `zstandard`).

## Commands

| Command           | Meaning                                   |
| ----------------- | ----------------------------------------- |
| `any text`        | broadcast to every node on the mesh       |
| `/msg <nick> ...` | route a unicast message via the mesh      |
| `/help`           | list commands                             |
| `/quit`           | leave (Ctrl-D / Ctrl-C also work)         |

## Server options

```
python3 server/main.py --help
  --host HOST      bind address            (default 127.0.0.1)
  --port PORT      listen port             (default 8765)
  --hop-ms N       simulated per-hop latency in ms (default 120)
  --seed N         fix the random topology
```

## How the mesh works

1. **Topology.** Every joined client becomes a `Node` placed on a
   deterministic golden-spiral layout (`server/stserver/mesh.py`). Nodes
   are linked to ±`ring_degree` ring neighbours plus a few random shortcut
   links, yielding a connected, evenly-meshed graph. Joining/leaving
   recomputes adjacency and every node gets a fresh neighbour list.

2. **Routing.** Unicast messages are routed through the graph with a BFS
   shortest path. Each hop costs a simulated transmission delay, and the
   server emits a `relay` event per hop so senders (and intermediate nodes)
   see the packet traversing the mesh. Broadcasts flood to every node.

3. **Wire protocol.** Newline-delimited JSON over TCP
   (`client/stclient/protocol.py`, `server/stserver/protocol.py`):
   `hello`, `welcome`, `peers`, `node_map`, `node_join`, `node_leave`,
   `send`, `relay`, `message`, `ack`, `error`.

## Layout

```
client/            client package: net.py, ui.py, protocol.py, __main__.py
server/            mesh sim: mesh.py, server.py, __main__.py, protocol.py
scripts/           smoke_test.py (end-to-end test over the real protocol)
Makefile           cross-platform build + dev/test targets
```# simple-talk

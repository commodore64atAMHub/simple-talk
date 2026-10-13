# HTTP templates

Sometimes TCP is more trouble than it is worth: a firewall rule, a
proxy, a locked-down school network, a browser tab. Every server in
this directory is the **same simple-talk mesh chat** (same topology,
BFS routing, hop-by-hop relay animation, `/dc` `/cn` wire sculpting)
speaking a small, boring, totally standard **HTTP + JSON** API instead
of a raw socket.

They are interchangeable: one client, one test, any server.

```sh
# pick one -- each listens on http://127.0.0.1:8080 by default
python3 templates/python_stdlib/server.py          # no dependencies
python3 templates/python_flask/server.py           # pip install flask
python3 templates/python_fastapi/server.py         # pip install fastapi uvicorn
node templates/node_http/server.js                 # no dependencies
node templates/node_express/server.js              # npm install express
go run templates/go_nethttp/main.go                # no dependencies

# chat with any of them from this repo's TUI
python3 client/main.py --url http://127.0.0.1:8080 --nick alice

# ...or from the browser (each server also serves it at /)
open http://127.0.0.1:8080/

# verify an implementation with the shared conformance test
python3 scripts/http_smoke_test.py --cmd "node templates/node_http/server.js --port {port}"
```

`python3 server/http.py` in the repo root is the "integrated" Python
server (it reuses `stserver/mesh.py`); `templates/python_stdlib` is the
portable single-file twin of it.

---

## The API

All bodies are JSON. Every response carries `Content-Length` and an
`Access-Control-Allow-Origin: *` CORS header (unconditionally, or at
least whenever the request carries an `Origin` header -- which the
browser page always does), so any of these servers works with the
browser page even from `file://`. Every server answers an `OPTIONS`
preflight with `2xx` plus those CORS headers; the exact status is the
framework's choice (`204` from plain HTTP, `200` from uvicorn's CORS
middleware) -- the browser accepts either. Some servers also send
`Connection: close`; that is fine too, because the client sends
`Connection: close` itself and reads exactly `Content-Length` bytes, so
keep-alive works either way. Default port is **8080**; the CLI flags are
`--host` (default `127.0.0.1`), `--port`, `--hop-ms` (default `120`),
`--seed`.

| Endpoint | Body / query | Success | Errors |
| --- | --- | --- | --- |
| `GET /` | – | browser chat page (`index.html` next to the server, else `../browser/index.html`, else a small landing page) | – |
| `GET /api/ping` | – | `{"type":"pong","version":...}` | – |
| `GET /api/state` | – | `{"nodes":[...],"sessions":n}` (debug, no auth) | – |
| `POST /api/hello` | `{"nick": "..."}` | `200 {"ok":true,"token","node_id","nick"}` | `400` bad nick, `409` nick in use |
| `GET /api/events` | `?token=&since=&wait=` | `200 {"events":[...],"cursor":n}` | `401` unknown/expired session |
| `POST /api/send` | `{"token","to","body","id"?}` | `200 {"ok":true}` | `401`, `404 no such node`, `409 no route` |
| `POST /api/wire` | `{"token","action":"cut"\|"link","a","b"}` | `200 {"ok":true}` | `401`, `400`, `404 no such node`, `409 no such wire / already exists` |
| `POST /api/bye` | `{"token"}` | `200 {"ok":true}` (idempotent) | – |

* **Errors are doubled on purpose.** A failed `send`/`wire` returns the
  HTTP status *and* pushes an `{"type":"error","reason":...}` event onto
  the caller's stream, because the TUI reads errors from the stream
  (that is what the TCP server would have written to the socket).
* **`/api/events` is a long poll.** It returns as soon as any event
  arrives, or when `wait` seconds pass (default 20, max 60) with an
  empty list -- the client then simply asks again. `since` is the
  `cursor` of the previous answer (an index into this session's ordered
  event queue; `0` on the first poll). `token` identifies the session.
* **Sessions** are created by `hello` and die on `bye`, on an idle
  timer (no poll/send for ~90s), or when the server shuts down. A dead
  session answers `401`, which the client treats as "connection lost".

## The event frames

Identical dicts to the TCP wire protocol, so the same handlers work:

```jsonc
{"type":"welcome","self":{"id":1,"nick":"alice","x":-3.1,"y":2.9},
 "nodes":[{"id":1,"nick":"alice","x":-3.1,"y":2.9,"peers":[2]}],"peers":[2]}
{"type":"peers","peers":[2,3]}                    // my direct neighbours
{"type":"node_map","nodes":[...]}                 // full topology, same shape as nodes above
{"type":"node_join","node":{"id":2,"nick":"bob"}}
{"type":"node_leave","node":{"id":2,"nick":"bob"}}
{"type":"relay","id":"ab12","from":"alice","to":"bob","hop":1,"hops":2,"via":"carol"}
{"type":"message","id":"ab12","from":"alice","to":"bob","body":"hi","hops":2,"route":["alice","carol","bob"]}
{"type":"ack","id":"ab12","to":"bob","hops":2,"route":["alice","carol","bob"]}
{"type":"wire","action":"cut","a":"alice","b":"bob","by":"alice"}
{"type":"error","id":"ab12","reason":"no route through the mesh"}
```

## Behavioural rules (what the conformance test checks)

1. **Join order:** the joiner receives `peers`, `node_map`, `welcome`;
   everyone else receives `peers`, `node_map`, `node_join`.
2. **Placement:** node `i` sits at `x,y = round(3*sqrt(i+1)*cos/sin(i*GOLDEN_ANGLE), 1)`
   with `GOLDEN_ANGLE = 2.3999632297286533`.
3. **Topology:** ring of degree 2 (`k = min(2, n-1)` neighbours each
   side) plus up to 3 random shortcut edges; with 3 nodes that is a
   complete graph, deterministically. Manual `cut`/`link` edits overlay
   the automatic shape and survive rebuilds; when any edit exists, a
   newcomer gets exactly one default wire to the most recent member.
4. **Unicast** finds a BFS shortest path; per hop it sleeps a jittered
   delay (`hop_ms * (0.7 + rng*0.6)`) and pushes a `relay` event to
   *every* session, then `message` to the target and `ack` to the
   sender. No path -> `409` + streamed `error` (`"no route"`).
5. **Broadcast** (`to: "*"`) delivers `message` to every *other* node
   (one simulated hop of delay each, no `relay` ticks) and `ack` to the
   sender -- never back to the sender as a `message`.
6. **Wire events are nearly private:** a `cut` notice goes to the
   operator only; a `link` notice to the operator and both endpoints.
   Everybody still gets the fresh `peers`/`node_map` push.
7. **Leaves** (`bye`, idle timeout) push `node_leave` to the remaining
   sessions, then a fresh `peers`/`node_map` to them.
8. **Validation:** nicks are 1-24 printable characters, unique
   case-insensitively; bodies are clipped to 2048 chars and blank
   bodies are ignored; message ids default to 16 hex chars.

## Adding your own framework

Copy `templates/python_stdlib/server.py` (or the closest sibling in
your language), keep the endpoints and frames above, and run:

```sh
python3 scripts/http_smoke_test.py --cmd "your-server --port {port}"
python3 scripts/http_smoke_test.py --url http://127.0.0.1:8080   # already running
```

Phase A of the test drives the raw HTTP contract (curl-style); phase B
runs this repo's real client transport (`stclient/httpnet.py`) against
your server, which is exactly what `python3 client/main.py --url ...`
uses.

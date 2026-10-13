#!/usr/bin/env node
/**
 * simple-talk over HTTP -- Express edition.
 *
 * A single-file HTTP mesh-chat server built on Express 5.  It speaks
 * the same conversation as ``templates/python_stdlib/server.py`` in
 * this repository, but carries its own copy of the mesh engine so this
 * one file (plus its ``package.json``) is all you need:
 *
 *     npm install express
 *     node server.js --port 8080        # or: npm start
 *
 * Then, in other terminals:
 *
 *     python3 client/main.py --url http://127.0.0.1:8080 --nick alice   # this repo's TUI
 *     open http://127.0.0.1:8080/                                       # browser chat page
 *
 * Verify it with this repo's smoke test:
 *
 *     python3 scripts/http_smoke_test.py --cmd "node templates/node_express/server.js --port {port}"
 *
 * The endpoints (and the JSON frames that ride them) are documented in
 * templates/README.md -- every server in templates/ is interchangeable.
 */
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { parseArgs } = require('node:util');
const express = require('express');

const GOLDEN_ANGLE = 2.3999632297286533; // radians -- node placement spiral
const MAX_NICK = 24;
const MAX_BODY = 2048;
const DEFAULT_PORT = 8080;
const MAX_WAIT_S = 60.0;
const DEFAULT_WAIT_S = 20.0;
const SESSION_TTL_MS = 90 * 1000; // idle sessions die after ~90s
const MAX_EVENTS = 10000; // queue cap; beyond it, drop the session
const JANITOR_MS = 10 * 1000;
const SERVER_ID = 'simple-talk-http/express';

const USAGE = `simple-talk mesh chat over HTTP (Express)

Usage: node server.js [options]
  --host <host>      bind host (default 127.0.0.1; 0.0.0.0 for the LAN)
  --port <port>      port (default ${DEFAULT_PORT})
  --hop-ms <ms>      simulated per-hop latency in ms (default 120)
  --seed <int>       RNG seed for reproducible topology
  --ui <file>        HTML file to serve at /
  --log-level <lvl>  DEBUG, INFO, WARNING or ERROR (default INFO)
  -h, --help         show this help`;

// ======================================================================
// small helpers
// ======================================================================
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, WARN: 30, ERROR: 40, CRITICAL: 40 };
let threshold = 20;

function pad7(level) {
  return level.padEnd(7);
}

const log = {
  debug(msg) {
    if (threshold <= 10) console.log(`${new Date().toISOString()} ${pad7('DEBUG')} http-mesh: ${msg}`);
  },
  info(msg) {
    if (threshold <= 20) console.log(`${new Date().toISOString()} ${pad7('INFO')} http-mesh: ${msg}`);
  },
  warn(msg) {
    if (threshold <= 30) console.error(`${new Date().toISOString()} ${pad7('WARNING')} http-mesh: ${msg}`);
  },
  error(msg) {
    console.error(`${new Date().toISOString()} ${pad7('ERROR')} http-mesh: ${msg}`);
  },
};

/** ChatError maps onto an HTTP status plus a protocol-shaped JSON body. */
class ChatError extends Error {
  constructor(status, reason) {
    super(reason);
    this.status = status;
    this.reason = reason;
  }

  get payload() {
    return { type: 'error', reason: this.reason };
  }
}

/** Python's ``str(x or "")`` -- blank out the empty-ish JSON values. */
function pyStr(v) {
  if (v === null || v === undefined || v === '' || v === 0 || v === false) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

/** Python's ``repr(s)`` for log lines: 'quoted', backslashes escaped. */
function pyRepr(s) {
  return `'${String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
}

/** Python's ``round(x, 1)`` -- ties to even, as close as floats allow. */
function round1(x) {
  const v = x * 10;
  const floor = Math.floor(v);
  const frac = v - floor;
  let n;
  if (frac > 0.5) n = floor + 1;
  else if (frac < 0.5) n = floor;
  else n = floor % 2 === 0 ? floor : floor + 1;
  return n / 10;
}

/** ``nick.isprintable()`` -- good enough for the protocol's purposes. */
function isPrintable(s) {
  for (const ch of s) {
    const cp = ch.codePointAt(0);
    if (cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f)) return false; // C0/C1 controls
    if (cp === 0x2028 || cp === 0x2029) return false; // line/para separators
    if (cp === 0x200b || (cp >= 0x200c && cp <= 0x200f)) return false; // zero-width etc.
    if (cp >= 0x202a && cp <= 0x202e) return false; // bidi controls
    if (cp >= 0x2060 && cp <= 0x2064) return false;
    if (cp >= 0x2066 && cp <= 0x206f) return false;
    if (cp === 0xfeff) return false;
  }
  return true;
}

function nickKey(nick) {
  return nick.toLowerCase(); // Python's casefold()
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * A tiny seedable PRNG (mulberry32); without a seed we just use
 * Math.random.  Same shape as Python's ``random.Random`` for the two
 * things the mesh draws from it: shortcut pairs and hop jitter.
 */
function makeRng(seed) {
  if (seed === null || seed === undefined) return { random: Math.random };
  let s = seed >>> 0;
  return {
    random() {
      s = (s + 0x6d2b79f5) >>> 0;
      let t = s;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    },
  };
}

function randomHex(bytes) {
  return crypto.randomBytes(bytes).toString('hex');
}

// ======================================================================
// the mesh
// ======================================================================
const pairKey = (a, b) => (a <= b ? `${a},${b}` : `${b},${a}`);

class Mesh {
  /**
   * The simulated network: nodes on a golden spiral, BFS routing.
   *
   * Nodes link to +/-2 ring neighbours plus a few random shortcuts; manual
   * ``cut``/``link`` edits overlay the automatic shape and survive every
   * rebuild (so /dc and /cn keep their promise).
   */
  constructor(ringDegree = 2, shortcuts = 3, rng = Math.random) {
    this.ringDegree = ringDegree;
    this.shortcuts = shortcuts;
    this.rng = rng; // () -> float in [0, 1)
    this.nodes = new Map(); // id -> {id, nick, x, y}
    this.adj = new Map(); // id -> Set(id)
    this.order = []; // ids in join order
    this._cuts = new Set(); // pair keys "a,b" (a <= b)
    this._links = new Set();
  }

  static place(nodeId) {
    const r = 3.0 * Math.sqrt(nodeId + 1);
    const a = nodeId * GOLDEN_ANGLE;
    return [round1(r * Math.cos(a)), round1(r * Math.sin(a))];
  }

  add(nodeId, nick) {
    this.order.push(nodeId);
    const [x, y] = Mesh.place(nodeId);
    this.nodes.set(nodeId, { id: nodeId, nick, x, y });
    this.rebuild();
    return this.nodes.get(nodeId);
  }

  remove(nodeId) {
    this.nodes.delete(nodeId);
    const at = this.order.indexOf(nodeId);
    if (at !== -1) this.order.splice(at, 1);
    for (const set of [this._cuts, this._links]) {
      for (const key of [...set]) {
        const [a, b] = key.split(',').map(Number);
        if (a === nodeId || b === nodeId) set.delete(key);
      }
    }
    this.rebuild();
  }

  rebuild() {
    const ids = this.order;
    const live = new Set(ids);
    let adj = new Map();
    if (this._cuts.size || this._links.size) {
      // Someone is sculpting the mesh: keep their shape, only wire
      // newcomers to the most recent member (they can /dc it away).
      adj = new Map();
      const fresh = [];
      for (const i of ids) {
        if (this.adj.has(i)) {
          adj.set(i, new Set([...this.adj.get(i)].filter((j) => live.has(j))));
        } else {
          adj.set(i, new Set());
          fresh.push(i);
        }
      }
      if (ids.length > 1) {
        for (const i of fresh) {
          const prev = ids.filter((j) => j !== i).pop();
          adj.get(i).add(prev);
          adj.get(prev).add(i);
        }
      }
    } else {
      adj = new Map();
      for (const i of ids) adj.set(i, new Set());
      const n = ids.length;
      if (n > 1) {
        const k = Math.min(this.ringDegree, n - 1);
        for (let idx = 0; idx < n; idx++) {
          for (let d = 1; d <= k; d++) {
            adj.get(ids[idx]).add(ids[(idx - d + n) % n]);
            adj.get(ids[idx]).add(ids[(idx + d) % n]);
          }
        }
        let existing = 0;
        for (const set of adj.values()) existing += set.size;
        existing = Math.floor(existing / 2);
        const extra = Math.max(0, Math.min(this.shortcuts, Math.floor((n * (n - 1)) / 2) - existing));
        for (let t = 0; t < extra; t++) {
          const [a, b] = this._samplePair(ids);
          adj.get(a).add(b);
          adj.get(b).add(a);
        }
      }
    }
    for (const key of this._cuts) {
      const [a, b] = key.split(',').map(Number);
      if (adj.has(a) && adj.has(b)) {
        adj.get(a).delete(b);
        adj.get(b).delete(a);
      }
    }
    for (const key of this._links) {
      const [a, b] = key.split(',').map(Number);
      if (adj.has(a) && adj.has(b)) {
        adj.get(a).add(b);
        adj.get(b).add(a);
      }
    }
    this.adj = adj;
  }

  /** ``rng.sample(ids, 2)`` -- two distinct members, uniformly. */
  _samplePair(ids) {
    const i = Math.floor(this.rng.random() * ids.length);
    let j = Math.floor(this.rng.random() * (ids.length - 1));
    if (j >= i) j += 1;
    return [ids[i], ids[j]];
  }

  cut(a, b) {
    if (a === b || !this.adj.has(a) || !this.adj.get(a).has(b)) return false;
    this._cuts.add(pairKey(a, b));
    this._links.delete(pairKey(a, b));
    this.rebuild();
    return true;
  }

  link(a, b) {
    if (a === b || !this.nodes.has(a) || !this.nodes.has(b)) return false;
    if (this.adj.has(a) && this.adj.get(a).has(b)) return false;
    this._links.add(pairKey(a, b));
    this._cuts.delete(pairKey(a, b));
    this.rebuild();
    return true;
  }

  peersOf(nodeId) {
    return [...(this.adj.get(nodeId) || [])].sort((x, y) => x - y);
  }

  nickOf(nodeId) {
    const node = this.nodes.get(nodeId);
    return node ? node.nick : `node${nodeId}`;
  }

  /** Shortest path (BFS), src and dst inclusive; null when cut off. */
  route(src, dst) {
    if (!this.adj.has(src) || !this.adj.has(dst)) return null;
    if (src === dst) return [src];
    const prev = new Map([[src, null]]);
    const queue = [src];
    let head = 0;
    while (head < queue.length) {
      const cur = queue[head++];
      for (const nxt of this.adj.get(cur)) {
        if (!prev.has(nxt)) {
          prev.set(nxt, cur);
          if (nxt === dst) {
            const path = [dst];
            while (prev.get(path[path.length - 1]) !== null) {
              path.push(prev.get(path[path.length - 1]));
            }
            path.reverse();
            return path;
          }
          queue.push(nxt);
        }
      }
    }
    return null;
  }

  nodeMap() {
    return this.order.map((i) => ({
      ...this.nodes.get(i),
      peers: this.peersOf(i),
    }));
  }
}

// ======================================================================
// the chat (sessions, events, relay animation)
// ======================================================================

/** One HTTP client's foothold in the mesh: a node plus an event queue. */
class Session {
  constructor(token, nick, nodeId) {
    this.token = token;
    this.nick = nick;
    this.nodeId = nodeId;
    this.events = [];
    this.lastSeen = Date.now();
    this.wakers = new Set(); // parked long-polls waiting for events
  }
}

class HttpChat {
  constructor(hopMs = 120, seed = null, sessionTtlMs = SESSION_TTL_MS, maxEvents = MAX_EVENTS) {
    this.hopBase = Math.max(5, hopMs); // simulated per-hop latency, ms
    this.sessionTtlMs = sessionTtlMs;
    this.maxEvents = maxEvents;
    this.rng = makeRng(seed);
    this.mesh = new Mesh(2, 3, () => this.rng.random());
    this.byNick = new Map(); // casefolded nick -> node id
    this.nextId = 1;
    this.sessions = new Map(); // token -> Session
    this.byNode = new Map(); // node id -> Session
    this.closed = false;
    this._janitor = null;
  }

  // -- lifecycle --------------------------------------------------------
  start() {
    this._janitor = setInterval(() => {
      const now = Date.now();
      for (const session of [...this.sessions.values()]) {
        if (now - session.lastSeen > this.sessionTtlMs) {
          log.info(`${session.nick} idle for ${Math.round(this.sessionTtlMs / 1000)}s -- dropping the session`);
          this.closeSession(session.token);
        }
      }
    }, JANITOR_MS);
  }

  stop() {
    this.closed = true;
    if (this._janitor) clearInterval(this._janitor);
    for (const session of [...this.sessions.values()]) this._wake(session);
  }

  // -- sessions ---------------------------------------------------------
  getSession(token) {
    const session = this.sessions.get(pyStr(token));
    if (!session) throw new ChatError(401, 'unknown or expired session');
    session.lastSeen = Date.now();
    return session;
  }

  hello(nickInput) {
    const nick = pyStr(nickInput).trim();
    if ([...nick].length < 1 || [...nick].length > MAX_NICK) {
      throw new ChatError(400, `nick must be 1-${MAX_NICK} characters`);
    }
    if (!isPrintable(nick)) throw new ChatError(400, 'nick has unprintable characters');
    if (this.byNick.has(nickKey(nick))) throw new ChatError(409, `nick already in use: ${nick}`);
    const nodeId = this.nextId++;
    const node = this.mesh.add(nodeId, nick);
    this.byNick.set(nickKey(nick), nodeId);
    const session = new Session(crypto.randomBytes(18).toString('base64url'), nick, nodeId);
    this.sessions.set(session.token, session);
    this.byNode.set(nodeId, session);
    log.info(`node ${nodeId} joined as ${pyRepr(nick)} over http`);
    // Same order as the TCP server: topology first, then the
    // joiner's welcome, then the join notice to everyone else.
    this._announce();
    this.push(session, {
      type: 'welcome',
      self: { id: node.id, nick: node.nick, x: node.x, y: node.y },
      nodes: this.mesh.nodeMap(),
      peers: this.mesh.peersOf(nodeId),
    });
    const joinEv = { type: 'node_join', node: { id: nodeId, nick } };
    for (const other of [...this.sessions.values()]) {
      if (other.token !== session.token) this.push(other, joinEv);
    }
    return session;
  }

  closeSession(token) {
    const session = this.sessions.get(pyStr(token));
    if (!session) return false;
    this.sessions.delete(session.token);
    this.byNode.delete(session.nodeId);
    this._wake(session); // let a parked long-poll notice the drop
    const left = this._dropNode(session.nodeId);
    if (left !== null) this._announceLeave({ id: session.nodeId, nick: left });
    log.info(`${pyRepr(session.nick)} left; ${this.sessions.size} remain`);
    return true;
  }

  _dropNode(nodeId) {
    if (!this.mesh.nodes.has(nodeId)) return null;
    const nick = this.mesh.nickOf(nodeId);
    this.mesh.remove(nodeId);
    this.byNick.delete(nickKey(nick));
    return nick;
  }

  _announceLeave(node) {
    const leaveEv = { type: 'node_leave', node };
    for (const session of [...this.sessions.values()]) this.push(session, leaveEv);
    this._announce();
  }

  // -- event fan-out ----------------------------------------------------
  push(session, event) {
    if (!session || this.sessions.get(session.token) !== session) return; // departed: drop on the floor
    session.events.push(event);
    if (session.events.length > this.maxEvents) {
      // Nobody is draining this queue -- let go of the client
      // instead of growing the list forever.
      log.warn(`${session.nick} fell ${session.events.length} events behind -- dropping the session`);
      this._closeOverflow(session);
      return;
    }
    this._wake(session);
  }

  _closeOverflow(session) {
    this.sessions.delete(session.token);
    this.byNode.delete(session.nodeId);
    const left = this._dropNode(session.nodeId);
    if (left !== null) this._announceLeave({ id: session.nodeId, nick: left });
  }

  pushAll(event) {
    for (const session of [...this.sessions.values()]) this.push(session, event);
  }

  pushNode(nodeId, event) {
    this.push(this.byNode.get(nodeId), event);
  }

  _announce() {
    // Neighbour lists first, then the full map (same as server.py).
    const order = [...this.mesh.order];
    for (const nid of order) this.pushNode(nid, { type: 'peers', peers: this.mesh.peersOf(nid) });
    const mapEv = { type: 'node_map', nodes: this.mesh.nodeMap() };
    for (const nid of order) this.pushNode(nid, mapEv);
  }

  _wake(session) {
    for (const waker of [...session.wakers]) waker();
  }

  // -- long-poll ---------------------------------------------------------
  _waitFor(session, ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        session.wakers.delete(finish);
        resolve();
      };
      const timer = setTimeout(finish, ms);
      session.wakers.add(finish);
    });
  }

  async events(session, since, waitS) {
    const deadline = Date.now() + Math.max(0, waitS) * 1000;
    if (!this.sessions.has(session.token)) throw new ChatError(401, 'unknown or expired session');
    session.lastSeen = Date.now();
    while (session.events.length <= since && !this.closed) {
      if (!this.sessions.has(session.token)) throw new ChatError(401, 'unknown or expired session');
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await this._waitFor(session, Math.min(remaining, 1000)); // also re-check drops
    }
    if (!this.sessions.has(session.token)) throw new ChatError(401, 'unknown or expired session');
    const tail = session.events.slice(since);
    return { events: tail, cursor: since + tail.length };
  }

  // -- chat traffic -------------------------------------------------------
  send(session, to, body, msgId) {
    const text = pyStr(body).slice(0, MAX_BODY);
    if (text.trim() === '') return; // blank bodies are ignored
    const target = to === null || to === undefined || to === '' || to === 0 || to === false ? '*' : String(to);
    const id = msgId ? String(msgId).slice(0, 16) : randomHex(8);
    if (!this.sessions.has(session.token)) throw new ChatError(401, 'unknown or expired session');
    const srcId = session.nodeId;
    const srcNick = session.nick;
    if (target === '*') {
      const recipients = [];
      for (const nid of [...this.mesh.order]) {
        if (nid !== srcId) recipients.push([nid, this.mesh.nickOf(nid)]);
      }
      this._fire(() => this._broadcast(srcId, srcNick, text, id, recipients));
    } else {
      const dstId = this.byNick.get(nickKey(target));
      if (dstId === undefined) throw this._streamedError(session, id, 404, `no such node: ${target}`);
      if (!this.mesh.nodes.has(dstId)) throw this._streamedError(session, id, 409, 'target not connected');
      const pathTo = this.mesh.route(srcId, dstId);
      if (!pathTo) throw this._streamedError(session, id, 409, 'no route through the mesh');
      const names = pathTo.map((i) => this.mesh.nickOf(i));
      this._fire(() => this._relay(srcId, srcNick, dstId, target, text, id, pathTo, names));
    }
  }

  /**
   * Failures reach the TUI as an ``error`` event (like the TCP server
   * would have written to the socket), plus as a real HTTP status for
   * curl.
   */
  _streamedError(session, msgId, status, reason) {
    this.push(session, { type: 'error', id: msgId === undefined ? null : msgId, reason });
    return new ChatError(status, reason);
  }

  _hopDelay() {
    return this.hopBase * (0.7 + this.rng.random() * 0.6);
  }

  /** Fire-and-forget the way Python starts a relay thread; never crash. */
  _fire(job) {
    job().catch((err) => log.error(`relay job failed: ${err && err.stack ? err.stack : err}`));
  }

  async _relay(srcId, srcNick, dstId, dstNick, body, id, route, names) {
    const hops = route.length - 1;
    for (let idx = 1; idx <= hops; idx++) {
      await sleep(this._hopDelay());
      if (this.closed) return;
      // relay ticks are public: every node animates the packet
      this.pushAll({
        type: 'relay',
        id,
        from: srcNick,
        to: dstNick,
        hop: idx,
        hops,
        via: this.mesh.nickOf(route[idx]),
      });
    }
    this.pushNode(dstId, { type: 'message', id, from: srcNick, to: dstNick, body, hops, route: names });
    this.pushNode(srcId, { type: 'ack', id, to: dstNick, hops, route: names });
    log.info(`relayed ${body.length} bytes ${srcNick} -> ${dstNick} (${hops} hop(s))`);
  }

  async _broadcast(srcId, srcNick, body, id, recipients) {
    for (const [nid, nick] of recipients) {
      await sleep(this._hopDelay());
      if (this.closed) return;
      this.pushNode(nid, { type: 'message', id, from: srcNick, to: '*', body, hops: 1, route: [srcNick, nick] });
    }
    this.pushNode(srcId, { type: 'ack', id, to: '*', hops: 1, route: [srcNick] });
    log.info(`broadcast from ${srcNick} to ${recipients.length} node(s)`);
  }

  wire(session, actionInput, aInput, bInput) {
    const action = pyStr(actionInput);
    if (action !== 'cut' && action !== 'link') throw new ChatError(400, `unknown wire action: ${action}`);
    const aNick = pyStr(aInput).trim();
    const bNick = pyStr(bInput).trim();
    if (!this.sessions.has(session.token)) throw new ChatError(401, 'unknown or expired session');
    const ia = this.byNick.get(nickKey(aNick));
    const ib = this.byNick.get(nickKey(bNick));
    if (ia === undefined || ib === undefined) {
      const missing = ia === undefined ? aNick : bNick;
      throw this._streamedError(session, null, 404, `no such node: ${missing}`);
    }
    if (ia === ib) throw this._streamedError(session, null, 400, 'a wire needs two different nodes');
    let ok;
    let failure;
    if (action === 'cut') {
      ok = this.mesh.cut(ia, ib);
      failure = `no such wire: ${aNick} <-> ${bNick}`;
    } else {
      ok = this.mesh.link(ia, ib);
      failure = `wire already exists: ${aNick} <-> ${bNick}`;
    }
    if (!ok) throw this._streamedError(session, null, 409, failure);
    const wireEv = { type: 'wire', action, a: aNick, b: bNick, by: session.nick };
    // Snip text: operator only.  Re-attach: both ends too.
    const targets = new Map([[session.token, session]]);
    if (action === 'link') {
      for (const nid of [ia, ib]) {
        const other = this.byNode.get(nid);
        if (other) targets.set(other.token, other);
      }
    }
    for (const target of targets.values()) this.push(target, wireEv);
    this._announce();
    log.info(`wire ${aNick} <-> ${bNick} ${action === 'cut' ? 'snipped' : 'linked'} by ${session.nick}`);
  }

  state() {
    return { nodes: this.mesh.nodeMap(), sessions: this.sessions.size };
  }
}

// ======================================================================
// the UI (browser chat page or a small landing page)
// ======================================================================
const LANDING_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>simple-talk HTTP server</title>
<style>body{font:15px/1.6 ui-monospace,monospace;background:#101418;color:#cdd6d4;
max-width:44rem;margin:3rem auto;padding:0 1rem}code{background:#1c2128;padding:.1rem .4rem;
border-radius:4px}h1{color:#89b4fa}</style></head><body>
<h1>simple-talk &rarr; HTTP mesh server</h1>
<p>Drop <code>templates/browser/index.html</code> next to this file (or just open it
from the repo) for a chat page, or use the TUI client:</p>
<p><code>python3 client/main.py --url http://127.0.0.1:__PORT__ --nick you</code></p>
<p>Full endpoint contract: <code>templates/README.md</code>.</p>
</body></html>
`;

function findUi(explicit) {
  const here = __dirname;
  const candidates = [];
  if (explicit) candidates.push(path.resolve(explicit));
  candidates.push(
    path.join(here, 'index.html'),
    path.join(here, '..', 'browser', 'index.html'),
    path.join(process.cwd(), 'templates', 'browser', 'index.html'),
    path.join(process.cwd(), 'index.html'),
  );
  for (const candidate of candidates) {
    try {
      return fs.readFileSync(candidate);
    } catch {
      // not there -- try the next one
    }
  }
  if (explicit) log.warn(`--ui ${explicit} not found; serving the landing page`);
  return null;
}

// ======================================================================
// the HTTP layer (Express)
// ======================================================================
function respond(req, res, status, buf, ctype) {
  if (res.writableEnded || res.destroyed) return;
  const headers = {
    'Cache-Control': 'no-store',
    Connection: 'close',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'content-type, authorization',
    'Access-Control-Max-Age': '600',
    Server: SERVER_ID,
  };
  if (ctype) headers['Content-Type'] = ctype;
  if (status !== 204) headers['Content-Length'] = String(buf.length);
  try {
    res.shouldKeepAlive = false;
    res.writeHead(status, headers);
    if (req.method === 'HEAD' || status === 204) res.end();
    else res.end(buf);
  } catch {
    // the client hung up mid-write; nothing to do
  }
}

function json(req, res, status, payload) {
  respond(req, res, status, Buffer.from(JSON.stringify(payload), 'utf8'), 'application/json; charset=utf-8');
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
    req.on('aborted', () => reject(new Error('request aborted')));
  });
}

/** Parse the body by hand: the error strings are part of the contract. */
async function readJsonBody(req) {
  const buf = await readBody(req);
  if (buf.length <= 0) return {};
  let data;
  try {
    data = JSON.parse(buf.toString('utf8'));
  } catch {
    throw new ChatError(400, 'request body must be JSON');
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new ChatError(400, 'request body must be a JSON object');
  }
  return data;
}

function parseSince(value) {
  if (value === null || value === undefined) return 0;
  const v = String(value).trim();
  if (!/^[+-]?\d+$/.test(v)) return 0;
  const n = Number.parseInt(v, 10);
  return Number.isSafeInteger(n) ? Math.max(0, n) : 0;
}

function parseWait(value) {
  if (value === null || value === undefined) return DEFAULT_WAIT_S;
  const v = String(value).trim();
  if (!/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(v)) return DEFAULT_WAIT_S;
  return Math.min(Number(v), MAX_WAIT_S);
}

/** Express 5 forwards rejected promises to the error middleware on its own;
 *  wrapping keeps the flow obvious and mirrors a plain try/catch. */
const wrap = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// ======================================================================
// networking niceties + main
// ======================================================================
function lanIpv4() {
  const addrs = new Set();
  for (const infos of Object.values(os.networkInterfaces())) {
    for (const info of infos || []) {
      if (info.family === 'IPv4' && !info.internal) addrs.add(info.address);
    }
  }
  return [...addrs].filter((a) => !a.startsWith('127.') && !a.startsWith('169.254.')).sort();
}

// The bound port (for the landing page's __PORT__).
let port_bound = DEFAULT_PORT;

function intFlag(raw, flag) {
  const v = String(raw).trim();
  if (!/^[+-]?\d+$/.test(v)) throw new Error(`${flag} expects an integer, got: ${raw}`);
  const n = Number.parseInt(v, 10);
  if (!Number.isSafeInteger(n)) throw new Error(`${flag} expects an integer, got: ${raw}`);
  return n;
}

function buildApp(chat, uiBytes) {
  const app = express();
  app.disable('x-powered-by');
  app.disable('etag');

  // Every response carries the CORS prelude; OPTIONS answers 204.
  app.use((req, res, next) => {
    res.on('error', () => {}); // a client that vanishes mid-response
    if (req.method === 'OPTIONS') return respond(req, res, 204, Buffer.alloc(0), null);
    next();
  });

  app.get('/', (req, res) => {
    const page = uiBytes || Buffer.from(LANDING_HTML.replace('__PORT__', String(port_bound)), 'utf8');
    respond(req, res, 200, page, 'text/html; charset=utf-8');
  });
  app.get('/index.html', (req, res) => {
    const page = uiBytes || Buffer.from(LANDING_HTML.replace('__PORT__', String(port_bound)), 'utf8');
    respond(req, res, 200, page, 'text/html; charset=utf-8');
  });

  app.get('/api/ping', (req, res) => json(req, res, 200, { type: 'pong', version: 'express' }));

  app.get('/api/state', (req, res) => json(req, res, 200, chat.state()));

  app.get(
    '/api/events',
    wrap(async (req, res) => {
      const q = new URL(req.originalUrl, 'http://mesh.invalid').searchParams;
      const session = chat.getSession(q.get('token'));
      const out = await chat.events(session, parseSince(q.get('since')), parseWait(q.get('wait')));
      json(req, res, 200, out);
    }),
  );

  app.post(
    '/api/hello',
    wrap(async (req, res) => {
      const body = await readJsonBody(req);
      const session = chat.hello(body.nick);
      json(req, res, 200, { ok: true, token: session.token, node_id: session.nodeId, nick: session.nick });
    }),
  );

  app.post(
    '/api/send',
    wrap(async (req, res) => {
      const body = await readJsonBody(req);
      const session = chat.getSession(body.token);
      chat.send(session, body.to, body.body, body.id);
      json(req, res, 200, { ok: true });
    }),
  );

  app.post(
    '/api/wire',
    wrap(async (req, res) => {
      const body = await readJsonBody(req);
      const session = chat.getSession(body.token);
      chat.wire(session, body.action, body.a, body.b);
      json(req, res, 200, { ok: true });
    }),
  );

  app.post(
    '/api/bye',
    wrap(async (req, res) => {
      const body = await readJsonBody(req);
      chat.closeSession(body.token);
      json(req, res, 200, { ok: true });
    }),
  );

  // Nothing matched.
  app.use((req, res) => json(req, res, 404, { type: 'error', reason: 'no such endpoint' }));

  // ChatError -> its HTTP status; anything else -> 500, never a crash.
  app.use((err, req, res, next) => {
    if (res.headersSent) return next(err);
    if (err instanceof ChatError) return json(req, res, err.status, err.payload);
    log.error(`internal error on ${req.method} ${req.url}\n${err && err.stack ? err.stack : err}`);
    json(req, res, 500, { type: 'error', reason: 'internal server error' });
  });

  return app;
}

function main() {
  let args;
  try {
    args = parseArgs({
      options: {
        host: { type: 'string', default: '127.0.0.1' },
        port: { type: 'string', default: String(DEFAULT_PORT) },
        'hop-ms': { type: 'string', default: '120' },
        seed: { type: 'string' },
        ui: { type: 'string' },
        'log-level': { type: 'string', default: 'INFO' },
        help: { type: 'boolean', short: 'h' },
      },
    });
  } catch (err) {
    console.error(`${err.message}\n\n${USAGE}`);
    process.exit(2);
  }
  if (args.values.help) {
    console.log(USAGE);
    return;
  }

  const host = args.values.host;
  let port;
  let hopMs;
  let seed = null;
  try {
    port = intFlag(args.values.port, '--port');
    hopMs = intFlag(args.values['hop-ms'], '--hop-ms');
    if (args.values.seed !== undefined) seed = intFlag(args.values.seed, '--seed');
  } catch (err) {
    console.error(`${err.message}\n\n${USAGE}`);
    process.exit(2);
  }
  threshold = LEVELS[String(args.values['log-level'] || 'INFO').toUpperCase()] || 20;

  const chat = new HttpChat(hopMs, seed);
  chat.start();
  const uiBytes = findUi(args.values.ui);
  const app = buildApp(chat, uiBytes);

  const server = app.listen(port, host, () => {
    // Express calls this callback on listen *errors* too (see its
    // app.listen: server.once('error', done)) -- address() is null then;
    // the 'error' listener below deals with those.
    const addr = server.address();
    if (!addr) return;
    port_bound = addr.port;
    log.info(`http mesh server listening on http://${host}:${port_bound}`);
    if (host === '0.0.0.0' || host === '::' || host === '') {
      for (const addr of lanIpv4()) {
        log.info(`  reachable at http://${addr}:${port_bound}  (client: --url http://${addr}:${port_bound})`);
      }
    } else {
      log.info(`bound to ${host} (loopback) -- restart with --host 0.0.0.0 for LAN use`);
    }
  });

  let listening = false;
  server.on('listening', () => {
    listening = true;
  });
  server.on('error', (err) => {
    if (!listening && (err.code === 'EADDRINUSE' || err.code === 'EACCES')) {
      log.error(`could not listen on ${host}:${port} -- ${err.message}`);
      chat.stop();
      process.exit(1);
    }
    log.error(`server error: ${err && err.stack ? err.stack : err}`);
  });

  const shutdown = () => {
    chat.stop();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1000).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

// A crashed handler must not take the whole mesh down.
process.on('uncaughtException', (err) => log.error(`uncaught exception: ${err && err.stack ? err.stack : err}`));
process.on('unhandledRejection', (err) => log.error(`unhandled rejection: ${err && err.stack ? err.stack : err}`));

main();

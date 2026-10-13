// simple-talk over HTTP -- Go standard library edition.
//
// A single-file, zero-dependency HTTP mesh-chat server.  It speaks the
// same conversation as server/http.py in this repository, but carries
// its own copy of the mesh engine so you can lift this one file out and
// run it anywhere Go exists:
//
//	go run main.go --port 8080
//
// Then, in other terminals:
//
//	python3 client/main.py --url http://127.0.0.1:8080 --nick alice   # this repo's TUI
//	open http://127.0.0.1:8080/                                       # browser chat page
//
// Verify it with this repo's smoke test:
//
//	python3 scripts/http_smoke_test.py --cmd "go run templates/go_nethttp/main.go --port {port}"
//
// The endpoints (and the JSON frames that ride them) are documented in
// templates/README.md -- every server in templates/ is interchangeable.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	mrand "math/rand"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"
	"unicode/utf8"
)

// ======================================================================
// constants (mirroring templates/python_stdlib/server.py)
// ======================================================================

const (
	goldenAngle  = 2.3999632297286533 // radians -- node placement spiral
	maxNick      = 24                 // printable characters
	maxBody      = 2048               // clipped message bodies
	defaultPort  = 8080               // --port
	defaultHopMS = 120                // --hop-ms
	defaultWaitS = 20.0               // long-poll wait without ?wait=
	maxWaitS     = 60.0               // long-poll wait ceiling
	sessionTTL   = 90 * time.Second   // idle sessions die quietly
	maxEvents    = 10000              // queue cap; beyond it: drop the session
	janitorEvery = 10 * time.Second   // idle-check cadence
	version      = "nethttp"          // /api/ping version string
)

// event is one JSON frame on a session's stream.  Maps (rather than
// structs) keep every frame shape exactly as the TCP protocol spells
// them, including "id": null on streamed wire errors.
type event = map[string]any

// chatError maps onto an HTTP status plus a protocol-shaped JSON body.
type chatError struct {
	status int
	reason string
}

func (e *chatError) Error() string { return e.reason }

func (e *chatError) payload() event {
	return event{"type": "error", "reason": e.reason}
}

// ======================================================================
// the mesh
// ======================================================================

// meshNode is one node's place on the golden spiral.
type meshNode struct {
	id   int
	nick string
	x, y float64
}

// nodeInfo is the wire shape of a node entry: {id,nick,x,y,peers}.
type nodeInfo struct {
	ID    int     `json:"id"`
	Nick  string  `json:"nick"`
	X     float64 `json:"x"`
	Y     float64 `json:"y"`
	Peers []int   `json:"peers"`
}

// mesh is the simulated network: nodes on a golden spiral, BFS routing.
//
// Nodes link to +/-2 ring neighbours plus a few random shortcuts; manual
// cut/link edits overlay the automatic shape and survive every rebuild
// (so /dc and /cn keep their promise).
//
// A chat owns exactly one mesh and guards it with chat.mu -- none of
// these methods lock on their own.
type mesh struct {
	ringDegree int
	shortcuts  int
	rng        *mrand.Rand

	nodes map[int]meshNode
	adj   map[int]map[int]struct{} // live adjacency, recomputed by rebuild
	order []int                    // join order (stable iteration order)

	// Manual wire edits, stored as overlays so they survive every
	// rebuild -- including re-rolled random shortcuts -- until changed
	// again.  Pairs are stored low-id-first.
	cuts  map[[2]int]struct{}
	links map[[2]int]struct{}
}

func newMesh(rng *mrand.Rand) *mesh {
	return &mesh{
		ringDegree: 2,
		shortcuts:  3,
		rng:        rng,
		nodes:      map[int]meshNode{},
		adj:        map[int]map[int]struct{}{},
		cuts:       map[[2]int]struct{}{},
		links:      map[[2]int]struct{}{},
	}
}

func pair(a, b int) [2]int {
	if a > b {
		a, b = b, a
	}
	return [2]int{a, b}
}

// place puts node i at x,y = round(3*sqrt(i+1)*cos/sin(i*GOLDEN_ANGLE), 1).
func place(nodeID int) (float64, float64) {
	r := 3.0 * math.Sqrt(float64(nodeID+1))
	a := float64(nodeID) * goldenAngle
	return round1(r * math.Cos(a)), round1(r * math.Sin(a))
}

// round1 rounds to one decimal digit (Python's round(v, 1)).
func round1(v float64) float64 {
	return math.Round(v*10) / 10
}

func (m *mesh) add(nodeID int, nick string) meshNode {
	m.order = append(m.order, nodeID)
	x, y := place(nodeID)
	n := meshNode{id: nodeID, nick: nick, x: x, y: y}
	m.nodes[nodeID] = n
	m.rebuild()
	return n
}

func (m *mesh) remove(nodeID int) {
	delete(m.nodes, nodeID)
	for i, id := range m.order {
		if id == nodeID {
			m.order = append(m.order[:i], m.order[i+1:]...)
			break
		}
	}
	for p := range m.cuts {
		if p[0] == nodeID || p[1] == nodeID {
			delete(m.cuts, p)
		}
	}
	for p := range m.links {
		if p[0] == nodeID || p[1] == nodeID {
			delete(m.links, p)
		}
	}
	m.rebuild()
}

// rebuild recomputes adjacency for every node.
//
// Once someone has sculpted the mesh by hand (cuts/links non-empty) the
// existing shape is preserved -- only wires of departed nodes are
// pruned, newcomers get one default wire -- so the auto-generated
// ring/shortcut links can never re-appear on top of what someone
// deliberately snipped.  This is what lets people carve their own
// topology (bus, ring, star, full mesh...) with cut/link.  Fully
// automatic meshes keep the original ring + random-shortcut behaviour.
func (m *mesh) rebuild() {
	ids := m.order
	live := make(map[int]struct{}, len(ids))
	for _, id := range ids {
		live[id] = struct{}{}
	}

	adj := make(map[int]map[int]struct{}, len(ids))
	if len(m.cuts) > 0 || len(m.links) > 0 {
		// Someone is sculpting the mesh: keep their shape, only wire
		// newcomers to the most recent member (they can /dc it away).
		var fresh []int
		for _, id := range ids {
			if prev, ok := m.adj[id]; ok {
				keep := make(map[int]struct{}, len(prev))
				for j := range prev {
					if _, ok := live[j]; ok {
						keep[j] = struct{}{}
					}
				}
				adj[id] = keep
			} else {
				adj[id] = map[int]struct{}{}
				fresh = append(fresh, id)
			}
		}
		if len(ids) > 1 {
			for _, id := range fresh {
				prev := -1
				for i := len(ids) - 1; i >= 0; i-- { // last member that isn't id
					if ids[i] != id {
						prev = ids[i]
						break
					}
				}
				if prev >= 0 {
					adj[id][prev] = struct{}{}
					adj[prev][id] = struct{}{}
				}
			}
		}
	} else {
		for _, id := range ids {
			adj[id] = map[int]struct{}{}
		}
		n := len(ids)
		if n > 1 {
			k := m.ringDegree
			if k > n-1 {
				k = n - 1
			}
			for idx, id := range ids {
				for d := 1; d <= k; d++ {
					adj[id][ids[(idx-d+n)%n]] = struct{}{}
					adj[id][ids[(idx+d)%n]] = struct{}{}
				}
			}
			existing := 0
			for _, s := range adj {
				existing += len(s)
			}
			existing /= 2
			extra := n*(n-1)/2 - existing
			if extra > m.shortcuts {
				extra = m.shortcuts
			}
			for e := 0; e < extra; e++ {
				a, b := m.sample2(ids)
				adj[a][b] = struct{}{}
				adj[b][a] = struct{}{}
			}
		}
	}

	// Manual wire edits win over whatever base topology we just built.
	for p := range m.cuts {
		if _, ok := adj[p[0]]; ok {
			if _, ok := adj[p[1]]; ok {
				delete(adj[p[0]], p[1])
				delete(adj[p[1]], p[0])
			}
		}
	}
	for p := range m.links {
		if _, ok := adj[p[0]]; ok {
			if _, ok := adj[p[1]]; ok {
				adj[p[0]][p[1]] = struct{}{}
				adj[p[1]][p[0]] = struct{}{}
			}
		}
	}
	m.adj = adj
}

// sample2 draws two distinct ids (Python's random.sample(ids, 2)).
func (m *mesh) sample2(ids []int) (int, int) {
	i := m.rng.Intn(len(ids))
	j := m.rng.Intn(len(ids) - 1)
	if j >= i {
		j++
	}
	return ids[i], ids[j]
}

// cut snips the wire a<->b; false when no such wire exists.
func (m *mesh) cut(a, b int) bool {
	if a == b {
		return false
	}
	neighbours, ok := m.adj[a]
	if !ok {
		return false
	}
	if _, ok := neighbours[b]; !ok {
		return false
	}
	p := pair(a, b)
	delete(m.links, p)
	m.cuts[p] = struct{}{}
	m.rebuild()
	return true
}

// link re-attaches (or creates) the wire a<->b; false when the nodes
// are unknown or already linked.
func (m *mesh) link(a, b int) bool {
	if a == b {
		return false
	}
	if _, ok := m.nodes[a]; !ok {
		return false
	}
	if _, ok := m.nodes[b]; !ok {
		return false
	}
	if neighbours, ok := m.adj[a]; ok {
		if _, ok := neighbours[b]; ok {
			return false
		}
	}
	p := pair(a, b)
	delete(m.cuts, p)
	m.links[p] = struct{}{}
	m.rebuild()
	return true
}

func (m *mesh) peersOf(nodeID int) []int {
	out := make([]int, 0, len(m.adj[nodeID]))
	for j := range m.adj[nodeID] {
		out = append(out, j)
	}
	sort.Ints(out)
	return out
}

func (m *mesh) nickOf(nodeID int) string {
	if n, ok := m.nodes[nodeID]; ok {
		return n.nick
	}
	return fmt.Sprintf("node%d", nodeID)
}

// route is the shortest path (BFS) from src to dst, both inclusive;
// nil when the mesh is cut in two.  Neighbours are visited in sorted
// order so equal-length alternatives resolve deterministically.
func (m *mesh) route(src, dst int) []int {
	if _, ok := m.adj[src]; !ok {
		return nil
	}
	if _, ok := m.adj[dst]; !ok {
		return nil
	}
	if src == dst {
		return []int{src}
	}
	prev := map[int]int{src: -1}
	queue := []int{src}
	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		next := m.peersOf(cur) // sorted snapshot
		for _, nxt := range next {
			if _, seen := prev[nxt]; seen {
				continue
			}
			prev[nxt] = cur
			if nxt == dst {
				return buildPath(prev, src, dst)
			}
			queue = append(queue, nxt)
		}
	}
	return nil
}

func buildPath(prev map[int]int, src, dst int) []int {
	path := []int{}
	for cur := dst; cur != src; cur = prev[cur] {
		path = append(path, cur)
	}
	path = append(path, src)
	for i, j := 0, len(path)-1; i < j; i, j = i+1, j-1 {
		path[i], path[j] = path[j], path[i]
	}
	return path
}

// nodeMap is the full topology in join order: [{id,nick,x,y,peers}].
func (m *mesh) nodeMap() []nodeInfo {
	out := make([]nodeInfo, 0, len(m.order))
	for _, id := range m.order {
		n := m.nodes[id]
		out = append(out, nodeInfo{ID: n.id, Nick: n.nick, X: n.x, Y: n.y, Peers: m.peersOf(id)})
	}
	return out
}

// ======================================================================
// the chat (sessions, events, relay animation)
// ======================================================================

// session is one HTTP client's foothold in the mesh: a node plus an
// ordered queue of events with a cursor (the `since` parameter).
type session struct {
	token    string
	nick     string
	nodeID   int
	events   []event
	lastSeen time.Time
}

// chat drives the mesh: sessions, event fan-out, hop-by-hop relay.
//
// Every piece of state lives behind mu.  Long sleeps (the simulated
// per-hop latency) always happen OUTSIDE the lock, so one slow packet
// never stalls the whole network.
type chat struct {
	hopBaseMS  float64 // max(5, hop-ms); multiplied by 0.7..1.3 jitter
	sessionTTL time.Duration
	maxEvents  int
	rng        *mrand.Rand
	mesh       *mesh
	byNick     map[string]int // case-folded nick -> node id
	nextID     int
	sessions   map[string]*session
	byNode     map[int]*session
	closed     bool
	stopped    sync.Once
	stopCh     chan struct{}
	mu         sync.Mutex
	cond       *sync.Cond // guards mu; signalled on every push/drop
}

func newChat(hopMS int, seed int64) *chat {
	base := hopMS
	if base < 5 {
		base = 5
	}
	var rng *mrand.Rand
	if seed != 0 {
		rng = mrand.New(mrand.NewSource(seed))
	} else {
		rng = mrand.New(mrand.NewSource(time.Now().UnixNano()))
	}
	c := &chat{
		hopBaseMS:  float64(base),
		sessionTTL: sessionTTL,
		maxEvents:  maxEvents,
		rng:        rng,
		mesh:       newMesh(rng),
		byNick:     map[string]int{},
		nextID:     1,
		sessions:   map[string]*session{},
		byNode:     map[int]*session{},
		stopCh:     make(chan struct{}),
	}
	c.cond = sync.NewCond(&c.mu)
	return c
}

// -- lifecycle --------------------------------------------------------

func (c *chat) start() {
	go c.janitor()
}

// stop wakes every parked long-poll and tells the janitor to go home.
// Called once on Ctrl-C / SIGTERM.
func (c *chat) stop() {
	c.stopped.Do(func() {
		c.mu.Lock()
		c.closed = true
		c.cond.Broadcast()
		c.mu.Unlock()
		close(c.stopCh)
	})
}

// janitor drops sessions whose browser/client stopped polling.
func (c *chat) janitor() {
	ticker := time.NewTicker(janitorEvery)
	defer ticker.Stop()
	for {
		select {
		case <-c.stopCh:
			return
		case <-ticker.C:
		}
		c.mu.Lock()
		now := time.Now()
		var stale []*session
		for _, s := range c.sessions {
			if now.Sub(s.lastSeen) > c.sessionTTL {
				stale = append(stale, s)
			}
		}
		c.mu.Unlock()
		for _, s := range stale {
			log.Printf("%s idle for %.0fs -- dropping the session", s.nick, c.sessionTTL.Seconds())
			c.closeSession(s.token)
		}
	}
}

func key(nick string) string {
	return strings.ToLower(nick) // Python's casefold; ASCII-equivalent
}

// -- sessions ---------------------------------------------------------

// getSession resolves a token and refreshes the idle timer.
func (c *chat) getSession(token string) (*session, *chatError) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.sessions[token]
	if !ok {
		return nil, &chatError{status: 401, reason: "unknown or expired session"}
	}
	s.lastSeen = time.Now()
	return s, nil
}

// hello validates the nick and joins the mesh.
//
// Order matters and matches the TCP server: topology first (peers +
// node_map pushed to everyone, joiner included), then the joiner's
// welcome, then the join notice to everyone else.
func (c *chat) hello(rawNick string) (*session, *chatError) {
	nick := strings.TrimSpace(rawNick)
	c.mu.Lock()
	defer c.mu.Unlock()

	if n := utf8.RuneCountInString(nick); n < 1 || n > maxNick {
		return nil, &chatError{status: 400, reason: fmt.Sprintf("nick must be 1-%d characters", maxNick)}
	}
	if !printable(nick) {
		return nil, &chatError{status: 400, reason: "nick has unprintable characters"}
	}
	if _, taken := c.byNick[key(nick)]; taken {
		return nil, &chatError{status: 409, reason: "nick already in use: " + nick}
	}

	nodeID := c.nextID
	c.nextID++
	n := c.mesh.add(nodeID, nick)
	c.byNick[key(nick)] = nodeID

	s := &session{
		token:    newToken(),
		nick:     nick,
		nodeID:   nodeID,
		events:   make([]event, 0),
		lastSeen: time.Now(),
	}
	c.sessions[s.token] = s
	c.byNode[nodeID] = s

	log.Printf("node %d joined as %q over http", nodeID, nick)
	c.announceLocked()
	c.pushLocked(s, event{
		"type":  "welcome",
		"self":  event{"id": n.id, "nick": n.nick, "x": n.x, "y": n.y},
		"nodes": c.mesh.nodeMap(),
		"peers": c.mesh.peersOf(nodeID),
	})
	joinEv := event{"type": "node_join", "node": event{"id": nodeID, "nick": nick}}
	for _, other := range c.snapshotSessionsLocked() {
		if other.token != s.token {
			c.pushLocked(other, joinEv)
		}
	}
	c.cond.Broadcast()
	return s, nil
}

// closeSession retires a token: the node leaves the mesh and everyone
// hears about it.  Idempotent (an unknown token is just false).
func (c *chat) closeSession(token string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.sessions[token]
	if !ok {
		return false
	}
	delete(c.sessions, token)
	delete(c.byNode, s.nodeID)
	if left, ok := c.dropNodeLocked(s.nodeID); ok {
		c.announceLeaveLocked(s.nodeID, left)
	}
	c.cond.Broadcast()
	log.Printf("%q left; %d remain", s.nick, len(c.sessions))
	return true
}

// dropNodeLocked removes the node from the mesh; returns its nick when
// it was really there.
func (c *chat) dropNodeLocked(nodeID int) (string, bool) {
	if _, ok := c.mesh.nodes[nodeID]; !ok {
		return "", false
	}
	nick := c.mesh.nickOf(nodeID)
	c.mesh.remove(nodeID)
	delete(c.byNick, key(nick))
	return nick, true
}

// announceLeaveLocked: node_leave to the remaining sessions, then a
// fresh peers/node_map push to them (behavioural rule 7).
func (c *chat) announceLeaveLocked(nodeID int, nick string) {
	leaveEv := event{"type": "node_leave", "node": event{"id": nodeID, "nick": nick}}
	for _, s := range c.snapshotSessionsLocked() {
		c.pushLocked(s, leaveEv)
	}
	c.announceLocked()
}

// -- event fan-out (all *Locked helpers assume c.mu is held) ----------

func (c *chat) snapshotSessionsLocked() []*session {
	out := make([]*session, 0, len(c.sessions))
	for _, s := range c.sessions {
		out = append(out, s)
	}
	return out
}

// pushLocked appends one event to a session's queue.  Departed sessions
// drop on the floor; a queue that grows past maxEvents loses its
// session instead (nobody is draining it -- let go of the client).
func (c *chat) pushLocked(s *session, ev event) {
	if cur, ok := c.sessions[s.token]; !ok || cur != s {
		return // departed session: drop on the floor
	}
	s.events = append(s.events, ev)
	if len(s.events) > c.maxEvents {
		log.Printf("%s fell %d events behind -- dropping the session", s.nick, len(s.events))
		c.closeOverflowLocked(s)
		return
	}
	c.cond.Broadcast()
}

// closeOverflowLocked is _close_overflow from the Python reference:
// the node departs as if the client had said bye.
func (c *chat) closeOverflowLocked(s *session) {
	delete(c.sessions, s.token)
	delete(c.byNode, s.nodeID)
	if left, ok := c.dropNodeLocked(s.nodeID); ok {
		c.announceLeaveLocked(s.nodeID, left)
	}
	c.cond.Broadcast()
}

func (c *chat) pushNodeLocked(nodeID int, ev event) {
	if s, ok := c.byNode[nodeID]; ok {
		c.pushLocked(s, ev)
	}
}

func (c *chat) pushAllLocked(ev event) {
	for _, s := range c.snapshotSessionsLocked() {
		c.pushLocked(s, ev)
	}
}

// announceLocked: neighbour lists first, then the full map -- to every
// session, in mesh order.  The order list is snapshotted because a
// push can (via queue overflow) reshape the mesh mid-walk.
func (c *chat) announceLocked() {
	order := append([]int(nil), c.mesh.order...)
	for _, nid := range order {
		c.pushNodeLocked(nid, event{"type": "peers", "peers": c.mesh.peersOf(nid)})
	}
	mapEv := event{"type": "node_map", "nodes": c.mesh.nodeMap()}
	for _, nid := range order {
		c.pushNodeLocked(nid, mapEv)
	}
}

// streamedError pushes {"type":"error",...} onto the caller's stream --
// that is what the TCP server would have written to the socket -- and
// returns the same failure as an HTTP response (errors are doubled on
// purpose; the TUI reads them from the stream).
func (c *chat) streamedErrorLocked(s *session, msgID any, status int, reason string) *chatError {
	c.pushLocked(s, event{"type": "error", "id": msgID, "reason": reason})
	return &chatError{status: status, reason: reason}
}

// -- long-poll ---------------------------------------------------------

// events waits until the session's queue holds something past `since`,
// or waitS seconds pass, then hands back the tail and the new cursor.
//
// The wait is a condition-variable wait with a deadline: a timer wakes
// every waiter at its own deadline (capped to 1s slices so drops and
// shutdown are re-checked, exactly like Python's cond.wait(min(rem,1))),
// while a push broadcasts immediately -- so a parked poll returns the
// moment an event lands.
func (c *chat) events(s *session, since int, waitS float64) ([]event, int, *chatError) {
	if since < 0 {
		since = 0 // garbage cursor in; sane cursor out
	}
	waitD := time.Duration(waitS * float64(time.Second))
	deadline := time.Now().Add(waitD)

	c.mu.Lock()
	defer c.mu.Unlock()
	if c.sessions[s.token] != s {
		return nil, 0, &chatError{status: 401, reason: "unknown or expired session"}
	}
	s.lastSeen = time.Now()

	for len(s.events) <= since && !c.closed {
		if c.sessions[s.token] != s {
			return nil, 0, &chatError{status: 401, reason: "unknown or expired session"}
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		c.timedWait(remaining)
	}
	if c.sessions[s.token] != s {
		return nil, 0, &chatError{status: 401, reason: "unknown or expired session"}
	}

	start := since
	if start > len(s.events) {
		start = len(s.events)
	}
	tail := make([]event, 0, len(s.events)-start)
	tail = append(tail, s.events[start:]...)
	return tail, since + len(tail), nil
}

// timedWait parks on c.cond for at most d (never more than 1s, so the
// loop above re-checks its conditions).  sync.Cond has no built-in
// timeout, so a timer broadcasts on our behalf: the timer cannot fire
// the wakeup early -- it blocks on the mutex we still hold until Wait
// releases it -- which makes the missed-wakeup window zero.  A timer
// that fires just after we wake broadcasts to everybody; harmless,
// because every waiter re-checks its own predicate.
func (c *chat) timedWait(d time.Duration) {
	if d > time.Second {
		d = time.Second
	}
	timer := time.AfterFunc(d, func() {
		c.mu.Lock()
		defer c.mu.Unlock()
		c.cond.Broadcast()
	})
	c.cond.Wait()
	timer.Stop() // false means its broadcast is already (harmlessly) coming
}

// -- chat traffic -------------------------------------------------------

// hopDelay is hop_ms * (0.7 + rng*0.6) -- the jittered per-hop latency.
func (c *chat) hopDelay() time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	ms := c.hopBaseMS * (0.7 + c.rng.Float64()*0.6)
	return time.Duration(ms * float64(time.Millisecond))
}

// send validates and dispatches one message.  Blank bodies are
// silently ignored (rule 8); unknown targets / unroutable pairs fail
// with a status AND a streamed error event.
func (c *chat) send(s *session, rawTo, rawBody, rawID any) *chatError {
	body := clipRunes(pyString(rawBody, ""), maxBody)
	if strings.TrimSpace(body) == "" {
		return nil // blank bodies are ignored, not errors
	}
	target := pyString(rawTo, "*")
	msgID := clipRunes(pyString(rawID, randomHex(8)), 16)

	c.mu.Lock()
	if cur, ok := c.sessions[s.token]; !ok || cur != s {
		c.mu.Unlock()
		return &chatError{status: 401, reason: "unknown or expired session"}
	}
	srcID, srcNick := s.nodeID, s.nick

	if target == "*" {
		var recipients []nodeNick
		for _, nid := range c.mesh.order {
			if nid != srcID {
				recipients = append(recipients, nodeNick{id: nid, nick: c.mesh.nickOf(nid)})
			}
		}
		c.mu.Unlock()
		go c.broadcast(srcID, srcNick, body, msgID, recipients)
		return nil
	}

	dstID, known := c.byNick[key(target)]
	if !known {
		ce := c.streamedErrorLocked(s, msgID, 404, "no such node: "+target)
		c.mu.Unlock()
		return ce
	}
	if _, ok := c.mesh.nodes[dstID]; !ok {
		ce := c.streamedErrorLocked(s, msgID, 409, "target not connected")
		c.mu.Unlock()
		return ce
	}
	path := c.mesh.route(srcID, dstID)
	if path == nil {
		ce := c.streamedErrorLocked(s, msgID, 409, "no route through the mesh")
		c.mu.Unlock()
		return ce
	}
	names := make([]string, 0, len(path))
	for _, id := range path {
		names = append(names, c.mesh.nickOf(id))
	}
	c.mu.Unlock()

	go c.relay(srcID, srcNick, dstID, target, body, msgID, path, names)
	return nil
}

type nodeNick struct {
	id   int
	nick string
}

// relay animates a unicast packet: one jittered sleep per hop, a
// public relay tick to EVERY session after each one, then the message
// to the target and the ack to the sender.
func (c *chat) relay(srcID int, srcNick string, dstID int, dstNick, body, msgID string, path []int, names []string) {
	hops := len(path) - 1
	for idx := 1; idx <= hops; idx++ {
		time.Sleep(c.hopDelay())
		c.mu.Lock()
		if c.closed {
			c.mu.Unlock()
			return
		}
		// relay ticks are public: every node animates the packet
		c.pushAllLocked(event{
			"type": "relay", "id": msgID, "from": srcNick, "to": dstNick,
			"hop": idx, "hops": hops, "via": c.mesh.nickOf(path[idx]),
		})
		c.mu.Unlock()
	}
	c.mu.Lock()
	c.pushNodeLocked(dstID, event{
		"type": "message", "id": msgID, "from": srcNick, "to": dstNick,
		"body": body, "hops": hops, "route": names,
	})
	c.pushNodeLocked(srcID, event{
		"type": "ack", "id": msgID, "to": dstNick, "hops": hops, "route": names,
	})
	log.Printf("relayed %d bytes %s -> %s (%d hop(s))", len(body), srcNick, dstNick, hops)
	c.mu.Unlock()
}

// broadcast delivers to every other node -- one simulated hop of delay
// each, no relay ticks -- and acks the sender last.  The sender never
// sees its own message back (rule 5).
func (c *chat) broadcast(srcID int, srcNick, body, msgID string, recipients []nodeNick) {
	for _, r := range recipients {
		time.Sleep(c.hopDelay())
		c.mu.Lock()
		if c.closed {
			c.mu.Unlock()
			return
		}
		c.pushNodeLocked(r.id, event{
			"type": "message", "id": msgID, "from": srcNick, "to": "*",
			"body": body, "hops": 1, "route": []string{srcNick, r.nick},
		})
		c.mu.Unlock()
	}
	c.mu.Lock()
	c.pushNodeLocked(srcID, event{
		"type": "ack", "id": msgID, "to": "*", "hops": 1, "route": []string{srcNick},
	})
	log.Printf("broadcast from %s to %d node(s)", srcNick, len(recipients))
	c.mu.Unlock()
}

// wire sculpts the topology (action "cut" or "link").
//
// Privacy (rule 6): a cut notice goes to the operator only; a link
// notice to the operator and both endpoints.  Everybody still gets the
// fresh peers/node_map push that follows.
func (c *chat) wire(s *session, action, rawA, rawB string) *chatError {
	if action != "cut" && action != "link" {
		return &chatError{status: 400, reason: "unknown wire action: " + action}
	}
	a, b := strings.TrimSpace(rawA), strings.TrimSpace(rawB)

	c.mu.Lock()
	defer c.mu.Unlock()
	if cur, ok := c.sessions[s.token]; !ok || cur != s {
		return &chatError{status: 401, reason: "unknown or expired session"}
	}
	ia, okA := c.byNick[key(a)]
	ib, okB := c.byNick[key(b)]
	if !okA || !okB {
		missing := a
		if okA {
			missing = b
		}
		return c.streamedErrorLocked(s, nil, 404, "no such node: "+missing)
	}
	if ia == ib {
		return c.streamedErrorLocked(s, nil, 400, "a wire needs two different nodes")
	}

	var ok bool
	var failure string
	if action == "cut" {
		ok = c.mesh.cut(ia, ib)
		failure = "no such wire: " + a + " <-> " + b
	} else {
		ok = c.mesh.link(ia, ib)
		failure = "wire already exists: " + a + " <-> " + b
	}
	if !ok {
		return c.streamedErrorLocked(s, nil, 409, failure)
	}

	wireEv := event{"type": "wire", "action": action, "a": a, "b": b, "by": s.nick}
	// Snip text: operator only.  Re-attach: both ends too.
	targets := map[string]*session{s.token: s}
	if action == "link" {
		for _, nid := range []int{ia, ib} {
			if other, ok := c.byNode[nid]; ok {
				targets[other.token] = other
			}
		}
	}
	for _, t := range targets {
		c.pushLocked(t, wireEv)
	}
	c.announceLocked()

	what := "snipped"
	if action == "link" {
		what = "linked"
	}
	log.Printf("wire %s <-> %s %s by %s", a, b, what, s.nick)
	return nil
}

func (c *chat) state() event {
	c.mu.Lock()
	defer c.mu.Unlock()
	return event{"nodes": c.mesh.nodeMap(), "sessions": len(c.sessions)}
}

// ======================================================================
// small helpers
// ======================================================================

// printable mirrors Python's str.isprintable(): letters, marks,
// numbers, punctuation and symbols only -- no spaces, no controls.
func printable(s string) bool {
	for _, r := range s {
		if !unicode.IsPrint(r) || unicode.IsSpace(r) {
			return false
		}
	}
	return true
}

// pyTruthy mirrors Python's truthiness for JSON body fields ("" , 0,
// false, null, [] and {} are all falsy).
func pyTruthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case float64:
		return x != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// pyString mirrors Python's str(value or default) on a JSON body field.
func pyString(v any, def string) string {
	if !pyTruthy(v) {
		return def
	}
	switch x := v.(type) {
	case string:
		return x
	case bool:
		if x {
			return "True"
		}
		return "False"
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	default:
		return fmt.Sprintf("%v", x)
	}
}

// clipRunes cuts a string to n code points (Python slices by str
// characters, not bytes).
func clipRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

func randomBytes(n int) []byte {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil { // crypto/rand; cannot realistically fail
		panic("crypto/rand unavailable: " + err.Error())
	}
	return b
}

func randomHex(byteCount int) string {
	return hex.EncodeToString(randomBytes(byteCount))
}

// newToken is secrets.token_urlsafe(18).
func newToken() string {
	return base64.RawURLEncoding.EncodeToString(randomBytes(18))
}

// jsonBytes marshals without HTML escaping and without Encoder's
// trailing newline, so bytes match Python's json.dumps output shape.
func jsonBytes(v any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return []byte(`{"type":"error","reason":"internal server error"}`)
	}
	return bytes.TrimRight(buf.Bytes(), "\n")
}

// ======================================================================
// the HTTP layer
// ======================================================================

const landingHTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>simple-talk HTTP server</title>
<style>body{font:15px/1.6 ui-monospace,monospace;background:#101418;color:#cdd6d4;
max-width:44rem;margin:3rem auto;padding:0 1rem}code{background:#1c2128;padding:.1rem .4rem;
border-radius:4px}h1{color:#89b4fa}</style></head><body>
<h1>simple-talk &rarr; HTTP mesh server</h1>
<p>Drop <code>templates/browser/index.html</code> next to this file (or just open it
from the repo) for a chat page, or use the TUI client:</p>
<p><code>python3 client/main.py --url http://%s --nick you</code></p>
<p>Full endpoint contract: <code>templates/README.md</code>.</p>
</body></html>
`

// findUI picks the page served at "/": index.html next to the running
// binary, then ../browser/index.html, then the repo copies, and
// finally a small landing page.
func findUI(port int) []byte {
	var candidates []string
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		candidates = append(candidates,
			filepath.Join(dir, "index.html"),
			filepath.Join(dir, "..", "browser", "index.html"))
	}
	if wd, err := os.Getwd(); err == nil {
		candidates = append(candidates,
			filepath.Join(wd, "templates", "browser", "index.html"),
			filepath.Join(wd, "index.html"))
	}
	for _, c := range candidates {
		if b, err := os.ReadFile(c); err == nil {
			return b
		}
	}
	return []byte(fmt.Sprintf(landingHTML, fmt.Sprintf("127.0.0.1:%d", port)))
}

// app is the single http.Handler: routes, CORS, JSON framing.
type app struct {
	chat *chat
	ui   []byte
}

func (a *app) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Every response carries the CORS quartet + Connection: close, so
	// the browser page works from file:// and no goroutine parks on an
	// idle socket.
	h := w.Header()
	h.Set("Access-Control-Allow-Origin", "*")
	h.Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	h.Set("Access-Control-Allow-Headers", "content-type, authorization")
	h.Set("Access-Control-Max-Age", "600")
	h.Set("Connection", "close")
	h.Set("Server", "simple-talk-http/"+version)

	if r.Method == http.MethodOptions { // CORS preflight, any path
		h.Set("Content-Length", "0")
		w.WriteHeader(http.StatusNoContent)
		return
	}

	wrote := false
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("internal error on %s %s: %v\n%s", r.Method, r.URL.Path, rec, debug.Stack())
			if !wrote {
				writeJSON(w, http.StatusInternalServerError,
					event{"type": "error", "reason": "internal server error"})
			}
		}
	}()

	switch r.Method {
	case http.MethodGet, http.MethodHead:
		wrote = a.serveGet(w, r)
	case http.MethodPost:
		wrote = a.servePost(w, r)
	default:
		writeJSON(w, http.StatusNotImplemented,
			event{"type": "error", "reason": "unsupported method"})
		wrote = true
	}
}

func (a *app) serveGet(w http.ResponseWriter, r *http.Request) bool {
	switch r.URL.Path {
	case "/", "/index.html":
		writeBytes(w, http.StatusOK, "text/html; charset=utf-8", a.ui)
		return true
	case "/api/ping":
		writeJSON(w, http.StatusOK, event{"type": "pong", "version": version})
		return true
	case "/api/state":
		writeJSON(w, http.StatusOK, a.chat.state())
		return true
	case "/api/events":
		q := r.URL.Query()
		s, ce := a.chat.getSession(q.Get("token"))
		if ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		since := 0
		if v, err := strconv.Atoi(q.Get("since")); err == nil {
			since = v
		}
		wait := defaultWaitS
		if v, err := strconv.ParseFloat(q.Get("wait"), 64); err == nil && !math.IsNaN(v) {
			if v < 0 {
				v = 0
			}
			if v > maxWaitS {
				v = maxWaitS
			}
			wait = v
		}
		events, cursor, ce := a.chat.events(s, since, wait)
		if ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		writeJSON(w, http.StatusOK, event{"events": events, "cursor": cursor})
		return true
	default:
		writeJSON(w, http.StatusNotFound, event{"type": "error", "reason": "no such endpoint"})
		return true
	}
}

func (a *app) servePost(w http.ResponseWriter, r *http.Request) bool {
	body, ce := decodeBody(r)
	if ce != nil {
		writeJSON(w, ce.status, ce.payload())
		return true
	}
	switch r.URL.Path {
	case "/api/hello":
		s, ce := a.chat.hello(pyString(body["nick"], ""))
		if ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		writeJSON(w, http.StatusOK, event{
			"ok": true, "token": s.token, "node_id": s.nodeID, "nick": s.nick,
		})
		return true
	case "/api/send":
		s, ce := a.chat.getSession(pyString(body["token"], ""))
		if ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		if ce := a.chat.send(s, body["to"], body["body"], body["id"]); ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		writeJSON(w, http.StatusOK, event{"ok": true})
		return true
	case "/api/wire":
		s, ce := a.chat.getSession(pyString(body["token"], ""))
		if ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		if ce := a.chat.wire(s,
			pyString(body["action"], ""),
			pyString(body["a"], ""),
			pyString(body["b"], ""),
		); ce != nil {
			writeJSON(w, ce.status, ce.payload())
			return true
		}
		writeJSON(w, http.StatusOK, event{"ok": true})
		return true
	case "/api/bye":
		a.chat.closeSession(pyString(body["token"], ""))
		writeJSON(w, http.StatusOK, event{"ok": true}) // idempotent
		return true
	default:
		writeJSON(w, http.StatusNotFound, event{"type": "error", "reason": "no such endpoint"})
		return true
	}
}

// decodeBody reads the request JSON: {} when empty, 400 when it is not
// a JSON object (mirroring the Python reference exactly).
func decodeBody(r *http.Request) (map[string]any, *chatError) {
	data, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, &chatError{status: 400, reason: "request body must be JSON"}
	}
	if len(data) == 0 {
		return map[string]any{}, nil
	}
	var v any
	if err := json.Unmarshal(data, &v); err != nil {
		return nil, &chatError{status: 400, reason: "request body must be JSON"}
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, &chatError{status: 400, reason: "request body must be a JSON object"}
	}
	return m, nil
}

// writeJSON emits a JSON response with explicit Content-Length.
func writeJSON(w http.ResponseWriter, status int, v any) {
	writeBytes(w, status, "application/json; charset=utf-8", jsonBytes(v))
}

func writeBytes(w http.ResponseWriter, status int, ctype string, blob []byte) {
	h := w.Header()
	h.Set("Content-Type", ctype)
	h.Set("Content-Length", strconv.Itoa(len(blob)))
	h.Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	if blob != nil {
		w.Write(blob)
	}
}

// ======================================================================
// wiring it up
// ======================================================================

func run(host string, port, hopMS int, seed int64) error {
	chat := newChat(hopMS, seed)
	chat.start()

	addr := net.JoinHostPort(host, strconv.Itoa(port))
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		chat.stop()
		return fmt.Errorf("could not listen on %s -- %v", addr, err)
	}

	srv := &http.Server{
		Handler:           &app{chat: chat, ui: findUI(port)},
		ReadHeaderTimeout: 30 * time.Second,
	}
	// No lingering keep-alive sockets: every response says
	// "Connection: close" and we tell the server to mean it.
	srv.SetKeepAlivesEnabled(false)

	log.Printf("http mesh server listening on http://%s:%d", host, port)
	if host == "0.0.0.0" || host == "::" || host == "" {
		log.Printf("  reachable on every interface (client: --url http://%s:%d)", host, port)
	} else {
		log.Printf("bound to %s (loopback) -- restart with --host 0.0.0.0 for LAN use", host)
	}

	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ln) }()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(sig)

	select {
	case <-sig:
		log.Printf("shutting down")
	case err := <-serveErr:
		chat.stop()
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("server error: %v", err)
		}
		return nil
	}

	// Wake every parked long-poll first, then let in-flight responses
	// drain (Shutdown refuses new connections and waits).
	chat.stop()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("forced shutdown: %v", err)
		srv.Close()
	}
	return nil
}

func main() {
	host := flag.String("host", "127.0.0.1",
		"bind host (default 127.0.0.1; 0.0.0.0 for the LAN)")
	port := flag.Int("port", defaultPort, "port (default 8080)")
	hopMS := flag.Int("hop-ms", defaultHopMS,
		"simulated per-hop latency in ms (default 120)")
	seed := flag.Int64("seed", 0,
		"RNG seed for reproducible topology (0 = no seed)")
	flag.Parse()

	if err := run(*host, *port, *hopMS, *seed); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}

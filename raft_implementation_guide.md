# Raft Consensus Algorithm — Full Implementation Guide

> Everything that was built, every bug that was hit, and how it all works.

---

## What Was Built

A fully working 3-node Raft consensus cluster in pure Python (asyncio, no external libraries). It runs in a single process, uses real TCP sockets for inter-node communication, persists state to disk, and survives leader crashes with automatic re-election.

**All tests pass:**
- Leader election in ~190ms
- Log replication to all nodes
- Leader crash → automatic re-election → cluster resumes

---

## File Structure

```
raft/
├── config.py          # Timeouts, ports, peer addresses
├── rpc.py             # Message definitions + JSON serialization
├── log.py             # Raft log (append, get, truncate, slice)
├── state_machine.py   # Key-value store (the actual application)
├── persistence.py     # Save/load state to disk (JSON files)
├── transport.py       # Async TCP send/receive layer
├── node.py            # The entire Raft brain (850 lines)
├── main.py            # Wires everything, runs CLI or demo
└── tests/
    ├── test_log.py
    ├── test_rpc.py
    └── test_state_machine.py
```

---

## How Raft Works (Quick Recap)

Raft is a **consensus algorithm** — it makes a cluster of nodes agree on a sequence of values even when some nodes crash.

It solves three sub-problems:

| Problem | Solution |
|---|---|
| Who's in charge? | **Leader Election** — one leader per term |
| How do we agree on values? | **Log Replication** — leader appends, majority ACKs = committed |
| What if the leader dies? | **Safety** — new leader always has all committed entries |

**The core flow:**

```
Client submits command
  → Leader appends to its log
  → Leader sends AppendEntries to all followers
  → Majority ACK → Leader commits
  → Leader tells followers to commit on next heartbeat
  → All nodes apply committed entries to state machine
```

---

## File-by-File Breakdown

---

### `config.py`

```python
NODES = {
    0: ("localhost", 5000),
    1: ("localhost", 5001),
    2: ("localhost", 5002),
}

ELECTION_TIMEOUT_MIN = 0.15   # seconds
ELECTION_TIMEOUT_MAX = 0.30
HEARTBEAT_INTERVAL   = 0.05
```

The timing constants are critical:
- **Election timeout** must be much larger than heartbeat interval. If the leader's heartbeat arrives before the follower's timer fires, no unnecessary election happens.
- **Randomized** timeout (0.15–0.30s) is the core liveness trick — if all nodes had the same timeout, they'd all call elections simultaneously and keep splitting votes forever.

---

### `rpc.py` — Message Definitions

Raft only needs two RPCs. Everything else is derived from these.

**RequestVote** — sent by a candidate asking for votes:
```python
@dataclass
class RequestVote:
    term: int             # candidate's current term
    candidate_id: int     # who's asking
    last_log_index: int   # candidate's last log entry index
    last_log_term: int    # candidate's last log entry term
```

**AppendEntries** — sent by the leader (also serves as heartbeat when entries=[]):
```python
@dataclass
class AppendEntries:
    term: int
    leader_id: int
    prev_log_index: int   # index of entry just before new ones
    prev_log_term: int    # term of that entry (consistency check)
    entries: List[dict]   # new entries to append ([] = heartbeat)
    leader_commit: int    # leader's commit_index
```

Serialized as JSON lines over TCP. Each message gets a `type` field so the receiver knows which dataclass to reconstruct.

---

### `log.py` — The Raft Log

The log is 1-indexed. Index 0 is a sentinel entry (term=0, command=None) so that `prev_log_index=0` means "the beginning."

```
Index:  0    1    2    3    4
Term:  [0]  [1]  [1]  [2]  [2]
Cmd:   [-] [SET] [SET] [SET] [DEL]
        ^sentinel
```

Key operations:

```python
log.append(entry)           # add new entry (must be next index, no gaps)
log.get(index)              # get entry at 1-based index
log.term_at(index)          # get term at index (returns 0 for out of range)
log.last_index()            # highest index in log
log.last_term()             # term of last entry
log.slice_from(index)       # all entries from index onwards
log.truncate_from(index)    # delete index and everything after (conflict resolution)
```

The 1-based index matters because Raft's paper uses 1-based indexing, and `prev_log_index=0` is a valid way to say "I have no previous entry."

---

### `state_machine.py` — The Application

The simplest possible state machine: a key-value store.

```python
commands = [
    {"op": "set",    "key": "x", "value": 42},
    {"op": "delete", "key": "x"},
    {"op": "get",    "key": "x"},   # read-only
    {"op": "noop"},                  # leader uses this on election
]
```

The state machine is **only modified when entries are committed and applied** — never when they're first received. This is the key safety property.

---

### `persistence.py` — Disk Storage

In production Raft, three things MUST be written to disk before responding to any RPC:

1. `current_term` — so a node never votes for two candidates in the same term after a crash
2. `voted_for` — same reason
3. `log` — so committed entries aren't lost

Here they're stored as JSON files in `storage/node_N/`. In a real system you'd use `fsync()` to guarantee durability. For a mini-project, JSON is fine.

---

### `transport.py` — Async TCP Layer

Each node runs a TCP server on its port. Outgoing calls open a short-lived connection, send one JSON line, read one JSON line back, and close.

```
Node 0 (port 5000)          Node 1 (port 5001)
    │                             │
    │── connect ─────────────────►│
    │── RequestVote (JSON line) ──►│  server calls on_request_vote()
    │◄─ RequestVoteResponse ───────│
    │── close ────────────────────►│
```

Two key timeouts:
- `CONNECT_TIMEOUT = 0.5s` — how long to wait to establish connection
- `READ_TIMEOUT = 0.5s` — how long to wait for response

If a peer is dead, the connection fails fast and the exception is silently swallowed. This is correct — Raft handles unavailable peers by just not counting their vote/ACK.

---

### `node.py` — The Core (The Whole Algorithm)

This is where everything lives. Here's the complete state a node tracks:

```python
# Persistent (saved to disk)
self.current_term = 0       # election cycle
self.voted_for = None       # who I voted for this term
self.log = RaftLog()        # the log

# Volatile
self.commit_index = 0       # highest entry known committed
self.last_applied = 0       # highest entry applied to state machine
self.role = "follower"      # follower | candidate | leader
self.leader_id = None

# Leader-only volatile (reset on each election)
self.next_index = {}        # next log index to send to each peer
self.match_index = {}       # highest index replicated on each peer
```

---

#### Role Transitions

```
           timeout                     majority votes
follower ──────────► candidate ──────────────────────► leader
   ▲                     │                               │
   │                     │ sees higher term              │
   └─────────────────────┘◄──────────────────────────────┘
          step down                  sees higher term
```

**`_become_follower(term)`** — resets to follower, clears voted_for, saves term to disk, resets election timer.

**`_become_candidate()`** — increments term, votes for self, saves to disk. Does NOT reset election timer (this was a critical bug — see below).

**`_become_leader()`** — initializes `next_index` and `match_index` for all peers, sends a no-op to assert leadership, starts heartbeat loop.

---

#### Election Timer

The timer is a single persistent asyncio task per node:

```python
async def _run_election_timer(self, extra=0.0):
    await asyncio.sleep(self._election_timeout() + extra)
    while self.role != "leader":
        await self._start_election()
        if self.role == "leader":
            break
        await asyncio.sleep(self._election_timeout())  # retry on split vote
```

On startup, each node gets an `extra` delay proportional to its ID:

```python
extra = self.id * (ELECTION_TIMEOUT_MAX - ELECTION_TIMEOUT_MIN)
# Node 0: +0.00s  (fires first)
# Node 1: +0.15s  (fires second)
# Node 2: +0.30s  (fires third)
```

This ensures Node 0 runs its election while Nodes 1 and 2 are still followers — they grant votes immediately and Node 0 wins cleanly.

After the first election, the stagger is no longer needed — heartbeats keep followers' timers reset.

---

#### Leader Election — `_start_election`

```python
async def _start_election(self):
    self._become_candidate()
    votes  = 1   # vote for self
    needed = len(NODES) // 2 + 1   # majority (2 out of 3)
    term   = self.current_term

    async def request_vote(peer_id):
        nonlocal votes
        resp = await self.transport.send_request_vote(peer_id, RequestVote(...))
        if resp.term > self.current_term:
            self._become_follower(resp.term)   # step down — higher term exists
            return
        if self.role == "candidate" and self.current_term == term and resp.vote_granted:
            votes += 1
            if votes >= needed:
                self._become_leader()

    await asyncio.gather(*[request_vote(p) for p in self.peers])
```

The `self.current_term == term` check is subtle but critical — if a step-down happened during the gather (another node had a higher term), we abort even if we somehow got enough votes.

---

#### Handling Incoming Vote Requests — `handle_request_vote`

```python
async def handle_request_vote(self, msg):
    if msg.term > self.current_term:
        self._become_follower(msg.term)   # always step down on higher term

    grant = False
    if (
        msg.term >= self.current_term
        and (self.voted_for is None or self.voted_for == msg.candidate_id)
        and self._candidate_log_ok(msg.last_log_index, msg.last_log_term)
    ):
        self.voted_for = msg.candidate_id
        self._reset_election_timer()   # reset — valid leader activity
        grant = True

    return RequestVoteResponse(term=self.current_term, vote_granted=grant)
```

The **log ok check** (§5.4.1 of the Raft paper) prevents a node with a stale log from becoming leader:

```python
def _candidate_log_ok(self, last_log_index, last_log_term):
    my_last_term  = self.log.last_term()
    my_last_index = self.log.last_index()
    if last_log_term != my_last_term:
        return last_log_term > my_last_term   # higher term wins
    return last_log_index >= my_last_index    # same term: longer log wins
```

---

#### Log Replication — `handle_append_entries`

This is the most complex handler. It has to:

1. Reject stale leaders (lower term)
2. Reset election timer (valid leader is alive)
3. Do a consistency check against `prev_log_index` / `prev_log_term`
4. Truncate conflicting entries
5. Append new entries
6. Advance commit index

```python
# Consistency check
if msg.prev_log_index > 0:
    if self.log.last_index() < msg.prev_log_index:
        return AppendEntriesResponse(success=False, ...)   # missing entries
    if self.log.term_at(msg.prev_log_index) != msg.prev_log_term:
        self.log.truncate_from(msg.prev_log_index)         # conflict — delete
        return AppendEntriesResponse(success=False, ...)
```

On failure, the leader backs off its `next_index` for that peer and retries with earlier entries. This guarantees eventual consistency.

---

#### Committing Entries — `_try_advance_commit`

The leader only advances `commit_index` when a **majority** has the entry AND it's from the **current term**:

```python
async def _try_advance_commit(self):
    for n in range(self.log.last_index(), self.commit_index, -1):
        if self.log.term_at(n) != self.current_term:
            continue   # §5.4.2 — never directly commit old-term entries
        replicated = 1 + sum(1 for m in self.match_index.values() if m >= n)
        if replicated > len(NODES) // 2:
            self.commit_index = n
            await self._apply_committed_entries()
            break
```

The "never commit old-term entries directly" rule (§5.4.2) is the subtlest safety rule in Raft. A leader commits old entries **indirectly** by appending a new entry in the current term — once that new entry is committed, everything before it is committed too.

---

### `main.py` — CLI and Demo

```bash
python main.py          # interactive CLI
python main.py --demo   # runs automated demo
```

**CLI commands:**
```
set <key> <value>    — submit SET to leader
get <key>            — read from leader state machine
delete <key>         — submit DELETE to leader
status               — print cluster table
kill <id>            — crash a node (stops its TCP server)
```

**Status table output:**
```
┌────┬───────────┬──────┬────────┬───────────────────────┐
│ ID │   Role    │ Term │ Log Ln │ Store                 │
├────┼───────────┼──────┼────────┼───────────────────────┤
│  0 │ LEADER    │    1 │      4 │ {'x': 42, 'y': 1}    │
│  1 │ FOLLOWER  │    1 │      4 │ {'x': 42, 'y': 1}    │
│  2 │ FOLLOWER  │    1 │      4 │ {'x': 42, 'y': 1}    │
└────┴───────────┴──────┴────────┴───────────────────────┘
```

---

## The Bug That Took the Longest to Find

**Symptom:** All three nodes kept becoming candidates endlessly. No leader was ever elected. Terms incremented to 40+ in 3 seconds.

**Root cause:** `_become_candidate()` was calling `_reset_election_timer()`. This created a **second concurrent election task** while the first one was still awaiting votes over TCP. Both tasks were running `_start_election` simultaneously, each calling `_become_candidate` again, each bumping the term. By the time any vote came back, `self.current_term != term` (the check inside `request_vote`) was always true, so `_become_leader` was never called.

**Fix:** Remove `_reset_election_timer()` from `_become_candidate()`. The election timer loop is the single owner of the retry cycle. `_become_candidate` just updates state.

```python
# WRONG
def _become_candidate(self):
    self.current_term += 1
    self.voted_for = self.id
    self.role = "candidate"
    self._reset_election_timer()   # ← creates a second racing task

# CORRECT
def _become_candidate(self):
    self.current_term += 1
    self.voted_for = self.id
    self.role = "candidate"
    # timer loop handles retries — don't touch it here
```

**Lesson:** In asyncio, creating a task that calls the same function you're currently running is effectively spawning a parallel execution. Any shared mutable state (like `current_term`) becomes a race condition.

---

## What the Visualizer Should Show

Since you're building the visualizer, here's what's worth displaying:

**Per-node state (poll `node.status()` every ~100ms):**
- Role (follower / candidate / leader) — color coded
- Current term
- Log length
- Commit index vs last applied
- State machine store contents

**The `status()` method returns all of this:**
```python
{
    "id": 0,
    "role": "leader",
    "term": 2,
    "leader_id": 0,
    "log_length": 5,
    "commit_index": 5,
    "last_applied": 5,
    "store": {"x": 42},
    "message_count": 47,
}
```

**What makes the demo compelling:**
1. Start cluster → watch one node go from FOLLOWER to CANDIDATE to LEADER in real time
2. Submit a SET command → watch log_length increment on all nodes (replication)
3. Kill the leader with `kill <id>` → watch re-election happen live
4. Show the store is preserved after re-election

**How to poll the cluster from your visualizer:**

If your visualizer is a separate process, the simplest approach is to expose a status HTTP endpoint in `main.py` using `aiohttp` — one GET `/status` endpoint that returns JSON for all nodes. Then your visualizer just polls that every 200ms.

Alternatively, run the cluster and visualizer in the same process — `node.status()` is directly callable.

---

## How to Run

```bash
# Start interactive cluster
python main.py

# Run automated demo (shows election + replication + crash recovery)
python main.py --demo

# Run unit tests (no pytest needed)
python -m pytest tests/    # if pytest installed
# or
python tests/test_log.py
python tests/test_rpc.py
python tests/test_state_machine.py
```

No external dependencies. Pure stdlib: `asyncio`, `json`, `dataclasses`, `os`, `random`, `logging`.

---

## Key Raft Rules (The Ones That Trip Everyone Up)

| Rule | Where | Why |
|---|---|---|
| Step down on any higher term | Every RPC handler, first thing | Ensures term monotonicity |
| Only commit current-term entries | `_try_advance_commit` | Prevents log divergence after re-election |
| Candidate log must be "at least as up-to-date" | `_candidate_log_ok` | Ensures new leader has all committed entries |
| Reset election timer only on VALID leader contact | `handle_append_entries` | Don't reset on stale leaders |
| Persist term + voted_for before responding | Every vote grant | Can't vote twice in same term after crash |
| Don't call `_reset_election_timer` from `_become_candidate` | `node.py` | Prevents racing election tasks (see bug above) |

"""
Full Raft node implementation.

Roles:   follower → candidate → leader (and back)
RPCs:    RequestVote, AppendEntries
Safety:  persistent hard state, log consistency checks, commit rules
"""
import asyncio
import logging
import random
from typing import Optional

from config import NODES, ELECTION_TIMEOUT_MIN, ELECTION_TIMEOUT_MAX, HEARTBEAT_INTERVAL
from log import RaftLog, LogEntry
from rpc import (
    RequestVote, RequestVoteResponse,
    AppendEntries, AppendEntriesResponse,
)
from state_machine import StateMachine
from persistence import save_hard_state, load_hard_state, save_log, load_log

logger = logging.getLogger(__name__)


class Node:

    def __init__(self, node_id: int, transport):
        self.id        = node_id
        self.peers     = [pid for pid in NODES if pid != node_id]
        self.transport = transport

        # ── Persistent state ──────────────────────────────────────
        self.current_term, self.voted_for = load_hard_state(node_id)
        self.log: RaftLog = load_log(node_id)

        # ── Volatile state ────────────────────────────────────────
        self.commit_index  = 0
        self.last_applied  = 0
        self.role          = "follower"
        self.leader_id: Optional[int] = None

        # ── Leader-only volatile state ────────────────────────────
        self.next_index:  dict[int, int] = {}
        self.match_index: dict[int, int] = {}

        # ── Application state machine ─────────────────────────────
        self.state_machine = StateMachine()

        # ── Async tasks ───────────────────────────────────────────
        self._election_timer_task: Optional[asyncio.Task] = None
        self._heartbeat_task:      Optional[asyncio.Task] = None

        # ── Observability ─────────────────────────────────────────
        self.message_count = 0

        # ── Step-by-step mode ─────────────────────────────────────
        self.step_by_step = True
        self._step_event  = asyncio.Event()
        self._step_event.set()   # not paused by default

    # ═══════════════════════════════════════════════════════════════
    # Startup
    # ═══════════════════════════════════════════════════════════════

    def start(self):
        extra = self.id * (ELECTION_TIMEOUT_MAX - ELECTION_TIMEOUT_MIN)
        self._election_timer_task = asyncio.create_task(
            self._run_election_timer(extra=extra)
        )
        logger.info(f"[Node {self.id}] Started — term={self.current_term}")

    # ═══════════════════════════════════════════════════════════════
    # Step-by-step
    # ═══════════════════════════════════════════════════════════════

    async def _wait_for_step(self, label: str):
        if not self.step_by_step:
            return
        logger.info(f"[Node {self.id}] ⏸  PAUSED — {label}")
        self._step_event.clear()
        await self._step_event.wait()

    def advance_step(self):
        """Called externally (CLI / visualizer) to unpause this node."""
        self._step_event.set()

    # ═══════════════════════════════════════════════════════════════
    # Role Transitions
    # ═══════════════════════════════════════════════════════════════

    def _become_follower(self, term: int, reset_timer: bool = True):
        self.role      = "follower"
        self.leader_id = None
        self._set_term(term)
        self._cancel_heartbeats()
        if reset_timer:
            self._reset_election_timer()

    def _become_candidate(self):
        self.current_term += 1
        self.voted_for    = self.id
        self.role         = "candidate"
        self.leader_id    = None
        self._persist_hard_state()
        logger.info(f"[Node {self.id}] → CANDIDATE term={self.current_term}")
        # do NOT reset timer — _run_election_timer owns the retry cycle

    def _become_leader(self):
        if self.role != "candidate":
            return
        self.role      = "leader"
        self.leader_id = self.id
        logger.info(f"[Node {self.id}] → LEADER    term={self.current_term} 🎉")

        for peer in self.peers:
            self.next_index[peer]  = self.log.last_index() + 1
            self.match_index[peer] = 0

        asyncio.create_task(self._append_noop())
        self._start_heartbeats()

    # ═══════════════════════════════════════════════════════════════
    # Election Timer — sole owner of retry scheduling
    # ═══════════════════════════════════════════════════════════════

    def _election_timeout(self) -> float:
        return random.uniform(ELECTION_TIMEOUT_MIN, ELECTION_TIMEOUT_MAX)

    def _reset_election_timer(self):
        if self._election_timer_task and not self._election_timer_task.done():
            self._election_timer_task.cancel()
        self._election_timer_task = asyncio.create_task(self._run_election_timer())

    async def _run_election_timer(self, extra: float = 0.0):
        try:
            await asyncio.sleep(self._election_timeout() + extra)
            if self.role != "leader":
                await self._start_election()
                if self.role != "leader":
                    self._reset_election_timer()
        except asyncio.CancelledError:
            pass

    # ═══════════════════════════════════════════════════════════════
    # Leader Election
    # ═══════════════════════════════════════════════════════════════

    async def _start_election(self):
        await self._wait_for_step(
            f"starting election — will become candidate term={self.current_term + 1}"
        )
        self._become_candidate()

        votes  = 1
        needed = len(NODES) // 2 + 1
        term   = self.current_term

        msg = RequestVote(
            term           = self.current_term,
            candidate_id   = self.id,
            last_log_index = self.log.last_index(),
            last_log_term  = self.log.last_term(),
        )

        async def request_vote(peer_id: int):
            nonlocal votes
            try:
                resp = await self.transport.send_request_vote(peer_id, msg)
                self.message_count += 1
                if resp.term > self.current_term:
                    self._become_follower(resp.term, reset_timer=False)
                    return
                if (
                    self.role == "candidate"
                    and self.current_term == term
                    and resp.vote_granted
                ):
                    votes += 1
                    if votes >= needed:
                        self._become_leader()
            except Exception:
                pass

        await asyncio.gather(*[request_vote(p) for p in self.peers])
        await self._wait_for_step(
            f"election done — role={self.role} term={self.current_term}"
        )

    # ═══════════════════════════════════════════════════════════════
    # Incoming RPC Handlers
    # ═══════════════════════════════════════════════════════════════

    async def handle_request_vote(self, msg: RequestVote) -> RequestVoteResponse:
        self.message_count += 1

        if msg.term > self.current_term:
            self._become_follower(msg.term, reset_timer=False)

        grant = False
        if (
            msg.term >= self.current_term
            and (self.voted_for is None or self.voted_for == msg.candidate_id)
            and self._candidate_log_ok(msg.last_log_index, msg.last_log_term)
        ):
            self.voted_for = msg.candidate_id
            self._persist_hard_state()
            self._reset_election_timer()   # reset only on vote grant
            grant = True

        return RequestVoteResponse(term=self.current_term, vote_granted=grant)

    def _candidate_log_ok(self, last_log_index: int, last_log_term: int) -> bool:
        my_last_term  = self.log.last_term()
        my_last_index = self.log.last_index()
        if last_log_term != my_last_term:
            return last_log_term > my_last_term
        return last_log_index >= my_last_index

    async def handle_append_entries(self, msg: AppendEntries) -> AppendEntriesResponse:
        self.message_count += 1

        if msg.term > self.current_term:
            self._become_follower(msg.term, reset_timer=False)

        if msg.term < self.current_term:
            return AppendEntriesResponse(self.current_term, False, 0)

        # Valid leader — reset timer once here, and only here
        self._reset_election_timer()
        if self.role == "candidate":
            self.role = "follower"
        self.leader_id = msg.leader_id

        # ── Log consistency check ──────────────────────────────────
        if msg.prev_log_index > 0:
            if self.log.last_index() < msg.prev_log_index:
                return AppendEntriesResponse(
                    self.current_term, False, self.log.last_index()
                )
            if self.log.term_at(msg.prev_log_index) != msg.prev_log_term:
                self.log.truncate_from(msg.prev_log_index)
                self._persist_log()
                return AppendEntriesResponse(
                    self.current_term, False, self.log.last_index()
                )

        # ── Append new entries ─────────────────────────────────────
        if msg.entries:
            await self._wait_for_step(
                f"appending {len(msg.entries)} entries from leader {msg.leader_id}"
            )

        for i, entry_dict in enumerate(msg.entries):
            idx   = msg.prev_log_index + 1 + i
            entry = LogEntry(**entry_dict)
            if self.log.last_index() >= idx:
                if self.log.term_at(idx) != entry.term:
                    self.log.truncate_from(idx)
                    self.log.append(entry)
            else:
                self.log.append(entry)

        if msg.entries:
            self._persist_log()

        # ── Advance commit index ───────────────────────────────────
        if msg.leader_commit > self.commit_index:
            self.commit_index = min(msg.leader_commit, self.log.last_index())
            await self._apply_committed_entries()

        return AppendEntriesResponse(self.current_term, True, self.log.last_index())

    # ═══════════════════════════════════════════════════════════════
    # Log Replication (Leader)
    # ═══════════════════════════════════════════════════════════════

    async def client_request(self, command: dict) -> bool:
        if self.role != "leader":
            return False

        entry = LogEntry(
            term    = self.current_term,
            index   = self.log.last_index() + 1,
            command = command,
        )
        self.log.append(entry)
        self.match_index[self.id] = self.log.last_index()
        self._persist_log()

        await self._replicate_to_peers()
        return True

    async def _append_noop(self):
        await self.client_request({"op": "noop"})

    async def _replicate_to_peers(self):
        tasks = [asyncio.create_task(self._replicate_to(p)) for p in self.peers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _replicate_to(self, peer_id: int, depth: int = 0):
        if self.role != "leader" or depth > 10:
            return

        ni       = self.next_index.get(peer_id, 1)
        prev_idx = ni - 1
        prev_trm = self.log.term_at(prev_idx) if prev_idx > 0 else 0
        entries  = self.log.slice_from(ni)

        msg = AppendEntries(
            term           = self.current_term,
            leader_id      = self.id,
            prev_log_index = prev_idx,
            prev_log_term  = prev_trm,
            entries        = [
                {"term": e.term, "index": e.index, "command": e.command}
                for e in entries
            ],
            leader_commit  = self.commit_index,
        )

        try:
            resp: AppendEntriesResponse = await self.transport.send_append_entries(
                peer_id, msg
            )
            self.message_count += 1

            if resp.term > self.current_term:
                self._become_follower(resp.term)
                return

            if resp.success:
                self.match_index[peer_id] = resp.match_index
                self.next_index[peer_id]  = resp.match_index + 1
                await self._try_advance_commit()
            else:
                self.next_index[peer_id] = max(1, resp.match_index + 1)
                await self._replicate_to(peer_id, depth + 1)

        except Exception as e:
            logger.debug(f"[Node {self.id}] AppendEntries → {peer_id} failed: {e}")

    async def _try_advance_commit(self):
        if self.role != "leader":
            return

        for n in range(self.log.last_index(), self.commit_index, -1):
            if self.log.term_at(n) != self.current_term:
                continue   # §5.4.2
            replicated = 1 + sum(1 for m in self.match_index.values() if m >= n)
            if replicated > len(NODES) // 2:
                self.commit_index = n
                await self._apply_committed_entries()
                break

    # ═══════════════════════════════════════════════════════════════
    # Heartbeats (Leader)
    # ═══════════════════════════════════════════════════════════════

    def _start_heartbeats(self):
        self._cancel_heartbeats()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _cancel_heartbeats(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    async def _heartbeat_loop(self):
        try:
            while self.role == "leader":
                await self._replicate_to_peers()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            pass

    # ═══════════════════════════════════════════════════════════════
    # Apply Committed Entries → State Machine
    # ═══════════════════════════════════════════════════════════════

    async def _apply_committed_entries(self):
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log.get(self.last_applied)
            if entry and entry.command:
                await self._wait_for_step(
                    f"applying index={self.last_applied} cmd={entry.command}"
                )
                result = self.state_machine.apply(entry.command)
                logger.debug(
                    f"[Node {self.id}] Applied index={self.last_applied} "
                    f"cmd={entry.command} result={result}"
                )

    # ═══════════════════════════════════════════════════════════════
    # Persistence Helpers
    # ═══════════════════════════════════════════════════════════════

    def _set_term(self, term: int):
        self.current_term = term
        self.voted_for    = None
        self._persist_hard_state()

    def _persist_hard_state(self):
        save_hard_state(self.id, self.current_term, self.voted_for)

    def _persist_log(self):
        save_log(self.id, self.log)

    # ═══════════════════════════════════════════════════════════════
    # Status (for visualizer / CLI)
    # ═══════════════════════════════════════════════════════════════

    def status(self) -> dict:
        return {
            "id":            self.id,
            "role":          self.role,
            "term":          self.current_term,
            "leader_id":     self.leader_id,
            "log_length":    self.log.last_index(),
            "commit_index":  self.commit_index,
            "last_applied":  self.last_applied,
            "store":         self.state_machine.snapshot(),
            "message_count": self.message_count,
            "step_by_step":  self.step_by_step,
            "paused":        not self._step_event.is_set(),
        }
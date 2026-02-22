from dataclasses import dataclass, asdict
from typing import List, Any
import json


@dataclass
class RequestVote:
    term: int
    candidate_id: int
    last_log_index: int
    last_log_term: int


@dataclass
class RequestVoteResponse:
    term: int
    vote_granted: bool


@dataclass
class AppendEntries:
    term: int
    leader_id: int
    prev_log_index: int
    prev_log_term: int
    entries: List[dict]   # [] = heartbeat
    leader_commit: int


@dataclass
class AppendEntriesResponse:
    term: int
    success: bool
    match_index: int


# ── Serialization helpers ──────────────────────────────────────────────────────

RPC_TYPES = {
    "RequestVote":          RequestVote,
    "RequestVoteResponse":  RequestVoteResponse,
    "AppendEntries":        AppendEntries,
    "AppendEntriesResponse": AppendEntriesResponse,
}


def encode(msg) -> bytes:
    payload = {"type": type(msg).__name__, "data": asdict(msg)}
    return (json.dumps(payload) + "\n").encode()


def decode(raw: str):
    payload = json.loads(raw)
    cls = RPC_TYPES[payload["type"]]
    return cls(**payload["data"])

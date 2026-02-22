"""
Persistent storage for Raft node state.
In a real system every write to current_term, voted_for, or log
must be fsynced before responding to RPCs.
Here we write JSON files — good enough for a mini-project.
"""
import json
import os
from config import STORAGE_DIR
from log import RaftLog, LogEntry


def _path(node_id: int, filename: str) -> str:
    d = os.path.join(STORAGE_DIR, f"node_{node_id}")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def save_hard_state(node_id: int, current_term: int, voted_for):
    data = {"current_term": current_term, "voted_for": voted_for}
    p = _path(node_id, "hard_state.json")
    with open(p, "w") as f:
        json.dump(data, f)


def load_hard_state(node_id: int):
    p = _path(node_id, "hard_state.json")
    if not os.path.exists(p):
        return 0, None
    with open(p) as f:
        data = json.load(f)
    return data["current_term"], data["voted_for"]


def save_log(node_id: int, log: RaftLog):
    p = _path(node_id, "log.json")
    with open(p, "w") as f:
        json.dump(log.to_list(), f)


def load_log(node_id: int) -> RaftLog:
    p = _path(node_id, "log.json")
    if not os.path.exists(p):
        return RaftLog()
    with open(p) as f:
        data = json.load(f)
    return RaftLog.from_list(data)

from dataclasses import dataclass, asdict
from typing import List, Any, Optional
import json
import os


@dataclass
class LogEntry:
    term: int
    index: int          # 1-based
    command: Any        # arbitrary dict


class RaftLog:
    """
    1-indexed Raft log.
    _entries[0] is a sentinel (index=0, term=0) so real entries start at [1].
    """

    def __init__(self):
        self._entries: List[LogEntry] = [LogEntry(term=0, index=0, command=None)]

    # ── Read ──────────────────────────────────────────────────────────────────

    def last_index(self) -> int:
        return len(self._entries) - 1

    def last_term(self) -> int:
        return self._entries[-1].term

    def term_at(self, index: int) -> int:
        if index <= 0 or index >= len(self._entries):
            return 0
        return self._entries[index].term

    def get(self, index: int) -> Optional[LogEntry]:
        if index <= 0 or index >= len(self._entries):
            return None
        return self._entries[index]

    def slice_from(self, index: int) -> List[LogEntry]:
        """Return all entries with index >= `index`."""
        if index >= len(self._entries):
            return []
        return self._entries[index:]

    # ── Write ─────────────────────────────────────────────────────────────────

    def append(self, entry: LogEntry):
        # Ensure index is consistent
        assert entry.index == len(self._entries), (
            f"Log gap: expected index {len(self._entries)}, got {entry.index}"
        )
        self._entries.append(entry)

    def truncate_from(self, index: int):
        """Delete all entries with index >= `index`."""
        if index < len(self._entries):
            self._entries = self._entries[:index]

    # ── Persistence ───────────────────────────────────────────────────────────

    def to_list(self) -> List[dict]:
        return [asdict(e) for e in self._entries[1:]]  # skip sentinel

    @classmethod
    def from_list(cls, data: List[dict]) -> "RaftLog":
        log = cls()
        for d in data:
            log._entries.append(LogEntry(**d))
        return log

    def __len__(self):
        return len(self._entries) - 1  # exclude sentinel

    def __repr__(self):
        return f"RaftLog(len={len(self)}, last_term={self.last_term()})"

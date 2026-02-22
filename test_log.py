"""Unit tests for RaftLog."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from log import RaftLog, LogEntry


def make_entry(index, term, command=None):
    return LogEntry(term=term, index=index, command=command or {"op": "noop"})


def test_empty_log():
    log = RaftLog()
    assert log.last_index() == 0
    assert log.last_term()  == 0
    assert len(log) == 0


def test_append_and_get():
    log = RaftLog()
    log.append(make_entry(1, 1))
    assert log.last_index() == 1
    assert log.last_term()  == 1
    assert log.get(1).term  == 1


def test_term_at():
    log = RaftLog()
    log.append(make_entry(1, 1))
    log.append(make_entry(2, 2))
    assert log.term_at(1) == 1
    assert log.term_at(2) == 2
    assert log.term_at(0) == 0
    assert log.term_at(9) == 0  # out of range


def test_slice_from():
    log = RaftLog()
    for i in range(1, 6):
        log.append(make_entry(i, i))
    sliced = log.slice_from(3)
    assert len(sliced) == 3
    assert sliced[0].index == 3


def test_truncate():
    log = RaftLog()
    for i in range(1, 6):
        log.append(make_entry(i, i))
    log.truncate_from(3)
    assert log.last_index() == 2
    assert len(log) == 2


def test_serialization():
    log = RaftLog()
    log.append(make_entry(1, 1, {"op": "set", "key": "x", "value": 42}))
    log.append(make_entry(2, 1, {"op": "set", "key": "y", "value": 99}))
    data    = log.to_list()
    restored = RaftLog.from_list(data)
    assert restored.last_index() == 2
    assert restored.get(1).command["key"] == "x"
    assert restored.get(2).command["value"] == 99


def test_append_wrong_index():
    log = RaftLog()
    with pytest.raises(AssertionError):
        log.append(make_entry(5, 1))  # gap — should raise

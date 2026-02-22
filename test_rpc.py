import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rpc import RequestVote, RequestVoteResponse, AppendEntries, AppendEntriesResponse, encode, decode


def roundtrip(msg):
    return decode(encode(msg).decode().strip())


def test_request_vote():
    msg = RequestVote(term=3, candidate_id=1, last_log_index=5, last_log_term=2)
    out = roundtrip(msg)
    assert isinstance(out, RequestVote)
    assert out.term == 3
    assert out.candidate_id == 1


def test_request_vote_response():
    msg = RequestVoteResponse(term=3, vote_granted=True)
    out = roundtrip(msg)
    assert out.vote_granted is True


def test_append_entries_heartbeat():
    msg = AppendEntries(term=1, leader_id=0, prev_log_index=0,
                        prev_log_term=0, entries=[], leader_commit=0)
    out = roundtrip(msg)
    assert isinstance(out, AppendEntries)
    assert out.entries == []


def test_append_entries_with_entries():
    entries = [{"term": 1, "index": 1, "command": {"op": "set", "key": "x", "value": 1}}]
    msg = AppendEntries(term=1, leader_id=0, prev_log_index=0,
                        prev_log_term=0, entries=entries, leader_commit=0)
    out = roundtrip(msg)
    assert len(out.entries) == 1
    assert out.entries[0]["command"]["key"] == "x"


def test_append_entries_response():
    msg = AppendEntriesResponse(term=2, success=True, match_index=5)
    out = roundtrip(msg)
    assert out.success is True
    assert out.match_index == 5

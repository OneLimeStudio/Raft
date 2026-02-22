import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from state_machine import StateMachine


def test_set_and_get():
    sm = StateMachine()
    sm.apply({"op": "set", "key": "x", "value": 42})
    assert sm.get("x") == 42


def test_delete():
    sm = StateMachine()
    sm.apply({"op": "set", "key": "x", "value": 1})
    sm.apply({"op": "delete", "key": "x"})
    assert sm.get("x") is None


def test_noop():
    sm = StateMachine()
    result = sm.apply({"op": "noop"})
    assert result is None
    assert sm.applied_count == 0


def test_snapshot():
    sm = StateMachine()
    sm.apply({"op": "set", "key": "a", "value": 1})
    sm.apply({"op": "set", "key": "b", "value": 2})
    snap = sm.snapshot()
    assert snap == {"a": 1, "b": 2}

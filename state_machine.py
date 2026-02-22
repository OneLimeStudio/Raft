from typing import Any, Dict, Optional


class StateMachine:
    """
    Simple key-value store state machine.
    Commands:
        {"op": "set",    "key": "x", "value": 1}
        {"op": "delete", "key": "x"}
        {"op": "noop"}   # used by leader on election
    """

    def __init__(self):
        self.store: Dict[str, Any] = {}
        self._applied_count = 0

    def apply(self, command: dict) -> Optional[Any]:
        if command is None:
            return None

        op = command.get("op")

        if op == "set":
            key, value = command["key"], command["value"]
            self.store[key] = value
            self._applied_count += 1
            return value

        elif op == "delete":
            key = command["key"]
            old = self.store.pop(key, None)
            self._applied_count += 1
            return old

        elif op == "get":
            # Read-only — applied for linearizability in this simple version
            return self.store.get(command["key"])

        elif op == "noop":
            return None

        return None

    def get(self, key: str) -> Optional[Any]:
        return self.store.get(key)

    def snapshot(self) -> dict:
        return dict(self.store)

    @property
    def applied_count(self) -> int:
        return self._applied_count

    def __repr__(self):
        return f"StateMachine(store={self.store})"

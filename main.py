"""
main.py — Spin up a 3-node Raft cluster in a single process.

Usage:
    python main.py                    # start cluster + interactive CLI
    python main.py --demo             # auto-run demo commands
"""
import asyncio
import logging
import argparse
import sys

from config import NODES
from transport import Transport
from node import Node

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Build cluster ──────────────────────────────────────────────────────────────

def build_cluster():
    nodes = {}
    transports = {}

    for node_id in NODES:
        t = Transport(node_id)
        n = Node(node_id, t)
        # Wire callbacks after both objects exist
        t.on_request_vote   = n.handle_request_vote
        t.on_append_entries = n.handle_append_entries
        nodes[node_id]      = n
        transports[node_id] = t

    return nodes, transports


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_leader(nodes: dict) -> Node | None:
    for n in nodes.values():
        if n.role == "leader":
            return n
    return None


def print_status(nodes: dict):
    print("\n┌─────────────────────────────────────────────────────────┐")
    print("│  Raft Cluster Status                                    │")
    print("├────┬───────────┬──────┬────────┬───────────────────────┤")
    print("│ ID │   Role    │ Term │ Log Ln │ Store                 │")
    print("├────┼───────────┼──────┼────────┼───────────────────────┤")
    for node in nodes.values():
        s = node.status()
        role_str = s["role"].upper().ljust(9)
        store_str = str(s["store"])[:21].ljust(21)
        print(f"│ {s['id']:2} │ {role_str} │ {s['term']:4} │ {s['log_length']:6} │ {store_str} │")
    print("└────┴───────────┴──────┴────────┴───────────────────────┘\n")


# ── Interactive CLI ────────────────────────────────────────────────────────────

async def interactive_cli(nodes: dict):
    print("\nRaft cluster running. Commands:")
    print("  set <key> <value>  — submit a SET command to the leader")
    print("  get <key>          — read a value from leader state machine")
    print("  delete <key>       — delete a key")
    print("  status             — print cluster status")
    print("  kill <id>          — simulate node crash (stops its server)")
    print("  q / quit           — exit\n")

    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(None, lambda: input("raft> ").strip())
        except (EOFError, KeyboardInterrupt):
            break

        parts = line.split()
        if not parts:
            continue

        cmd = parts[0].lower()

        if cmd in ("q", "quit", "exit"):
            break

        elif cmd == "status":
            print_status(nodes)

        elif cmd == "set" and len(parts) == 3:
            key, value = parts[1], parts[2]
            # Try to parse as int/float
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass

            leader = find_leader(nodes)
            if not leader:
                print("  ✗ No leader elected yet — try again in a moment")
                continue
            ok = await leader.client_request({"op": "set", "key": key, "value": value})
            if ok:
                print(f"  ✓ SET {key} = {value}  (committed by node {leader.id})")
            else:
                print("  ✗ Failed — node is not leader")

        elif cmd == "get" and len(parts) == 2:
            key = parts[1]
            leader = find_leader(nodes)
            if not leader:
                print("  ✗ No leader")
                continue
            val = leader.state_machine.get(key)
            print(f"  {key} = {val!r}")

        elif cmd == "delete" and len(parts) == 2:
            key = parts[1]
            leader = find_leader(nodes)
            if not leader:
                print("  ✗ No leader")
                continue
            await leader.client_request({"op": "delete", "key": key})
            print(f"  ✓ DELETE {key}")

        elif cmd == "kill" and len(parts) == 2:
            nid = int(parts[1])
            nodes[nid].transport.stop()
            nodes[nid].role = "dead"
            
            # ADD THESE TWO LINES
            if nodes[nid]._election_timer_task:
                nodes[nid]._election_timer_task.cancel()
            if nodes[nid]._heartbeat_task:
                nodes[nid]._heartbeat_task.cancel()
            
            print(f"  ✓ Node {nid} killed — watch for re-election")

        else:
            print(f"  ✗ Unknown command: {line}")


# ── Demo ───────────────────────────────────────────────────────────────────────

async def run_demo(nodes: dict):
    print("\n[Demo] Waiting for leader election...")
    await asyncio.sleep(0.8)

    leader = find_leader(nodes)
    if not leader:
        print("[Demo] No leader elected — something is wrong!")
        return

    print(f"[Demo] Leader is Node {leader.id}\n")
    print_status(nodes)

    print("[Demo] Submitting commands...")
    commands = [
        {"op": "set", "key": "name",    "value": "raft-demo"},
        {"op": "set", "key": "version", "value": 1},
        {"op": "set", "key": "status",  "value": "running"},
    ]
    for c in commands:
        await leader.client_request(c)
        print(f"  → {c}")
        await asyncio.sleep(0.1)

    await asyncio.sleep(0.3)
    print("\n[Demo] After replication:")
    print_status(nodes)

    print("[Demo] Killing the leader...")
    leader.transport.stop()
    leader.role = "dead"
    await asyncio.sleep(0.8)

    new_leader = find_leader(nodes)
    if new_leader:
        print(f"[Demo] New leader elected: Node {new_leader.id}")
    else:
        print("[Demo] No new leader yet...")

    print_status(nodes)
    print("[Demo] Done!")


# ── Entry point ────────────────────────────────────────────────────────────────

async def run(demo=False):
    nodes, transports = build_cluster()

    # Start all TCP servers
    await asyncio.gather(*[t.start_server() for t in transports.values()])

    # Kick off election timers
    for n in nodes.values():
        n.start()

    if demo:
        await run_demo(nodes)
    else:
        await interactive_cli(nodes)


def main():
    parser = argparse.ArgumentParser(description="Raft consensus demo")
    parser.add_argument("--demo", action="store_true", help="Run automated demo")
    args = parser.parse_args()

    try:
        asyncio.run(run(demo=args.demo))
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()

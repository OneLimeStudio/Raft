"""
main.py — Spin up a Raft cluster in a single process.

Usage:
    python main.py                    # start cluster + interactive CLI
    python main.py --demo             # auto-run demo commands

CLI commands:
    set <key> <value>   submit SET to leader
    get <key>           read from leader
    delete <key>        submit DELETE to leader
    status              print cluster table
    kill <id>           simulate node crash
    step                enable step-by-step mode (all nodes pause at each action)
    next / n            advance all paused nodes one step
    auto                disable step-by-step, resume normal operation
    q / quit            exit
"""
import asyncio
import logging
import argparse
from frontend import launch_visualizer
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
    nodes      = {}
    transports = {}

    for node_id in NODES:
        t = Transport(node_id)
        n = Node(node_id, t)
        t.on_request_vote   = n.handle_request_vote
        t.on_append_entries = n.handle_append_entries
        nodes[node_id]      = n
        transports[node_id] = t

    return nodes, transports


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_leader(nodes: dict):
    for n in nodes.values():
        if n.role == "leader":
            return n
    return None


def print_status(nodes: dict):
    print("\n┌─────────────────────────────────────────────────────────────────┐")
    print("│  Raft Cluster Status                                            │")
    print("├────┬───────────┬──────┬────────┬────────┬───────────────────────┤")
    print("│ ID │   Role    │ Term │ Log Ln │ Commit │ Store                 │")
    print("├────┼───────────┼──────┼────────┼────────┼───────────────────────┤")
    for node in nodes.values():
        s        = node.status()
        role_str = s["role"].upper().ljust(9)
        store_str = str(s["store"])[:21].ljust(21)
        paused   = " ⏸" if s.get("paused") else ""
        print(
            f"│ {s['id']:2} │ {role_str} │ {s['term']:4} │ "
            f"{s['log_length']:6} │ {s['commit_index']:6} │ {store_str} │{paused}"
        )
    print("└────┴───────────┴──────┴────────┴────────┴───────────────────────┘\n")


# ── Interactive CLI ────────────────────────────────────────────────────────────

async def interactive_cli(nodes: dict):
    print("\nRaft cluster running. Commands:")
    print("  set <key> <value>  — submit SET to the leader")
    print("  get <key>          — read from leader state machine")
    print("  delete <key>       — submit DELETE to the leader")
    print("  status             — print cluster table")
    print("  kill <id>          — simulate node crash")
    print("  step               — enable step-by-step mode")
    print("  next / n           — advance one step (in step-by-step mode)")
    print("  auto               — disable step-by-step, resume normal")
    print("  q / quit           — exit\n")

    loop = asyncio.get_event_loop()

    while True:
        try:
            line = await loop.run_in_executor(
                None, lambda: input("raft> ").strip()
            )
        except (EOFError, KeyboardInterrupt):
            break

        parts = line.split()
        if not parts:
            continue

        cmd = parts[0].lower()

        # ── quit ──────────────────────────────────────────────────
        if cmd in ("q", "quit", "exit"):
            break

        # ── status ────────────────────────────────────────────────
        elif cmd == "status":
            print_status(nodes)

        # ── set ───────────────────────────────────────────────────
        elif cmd == "set" and len(parts) == 3:
            key, value = parts[1], parts[2]
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass

            leader = find_leader(nodes)
            if not leader:
                print("  ✗ No leader — try again in a moment")
                continue
            ok = await leader.client_request({"op": "set", "key": key, "value": value})
            if ok:
                print(f"  ✓ SET {key} = {value}  (committed by node {leader.id})")
            else:
                print("  ✗ Failed — leader stepped down mid-request")

        # ── get ───────────────────────────────────────────────────
        elif cmd == "get" and len(parts) == 2:
            key    = parts[1]
            leader = find_leader(nodes)
            if not leader:
                print("  ✗ No leader")
                continue
            val = leader.state_machine.get(key)
            print(f"  {key} = {val!r}")

        # ── delete ────────────────────────────────────────────────
        elif cmd == "delete" and len(parts) == 2:
            key    = parts[1]
            leader = find_leader(nodes)
            if not leader:
                print("  ✗ No leader")
                continue
            await leader.client_request({"op": "delete", "key": key})
            print(f"  ✓ DELETE {key}")

        # ── kill ──────────────────────────────────────────────────
        elif cmd == "kill" and len(parts) == 2:
            try:
                nid = int(parts[1])
            except ValueError:
                print("  ✗ kill <id> expects an integer")
                continue

            if nid not in nodes:
                print(f"  ✗ Unknown node id {nid}")
                continue

            n = nodes[nid]
            n.transport.stop()
            n.role = "dead"
            if n._election_timer_task:
                n._election_timer_task.cancel()
            if n._heartbeat_task:
                n._heartbeat_task.cancel()
            print(f"  ✓ Node {nid} killed — watch for re-election")

        # ── step-by-step mode on ──────────────────────────────────
        elif cmd == "step":
            for n in nodes.values():
                if n.role != "dead":
                    n.step_by_step = True
            print("  ✓ Step-by-step ON — cluster pauses at each action")
            print("    Type 'next' or 'n' to advance, 'auto' to resume")

        # ── advance one step ──────────────────────────────────────
        elif cmd in ("next", "n"):
            advanced = 0
            for n in nodes.values():
                if n.step_by_step and not n._step_event.is_set():
                    n.advance_step()
                    advanced += 1
            if advanced == 0:
                print("  (no nodes currently paused)")
            else:
                print(f"  ✓ Advanced {advanced} node(s)")

        # ── step-by-step mode off ─────────────────────────────────
        elif cmd == "auto":
            for n in nodes.values():
                n.step_by_step = False
                n.advance_step()   # release any currently paused nodes
            print("  ✓ Step-by-step OFF — cluster running normally")

        else:
            print(f"  ✗ Unknown command: {line!r}")


# ── Demo ───────────────────────────────────────────────────────────────────────

async def run_demo(nodes: dict):
    print("\n[Demo] Waiting for leader election...")
    await asyncio.sleep(1.5)

    leader = find_leader(nodes)
    if not leader:
        print("[Demo] No leader elected — something is wrong!")
        return

    print(f"[Demo] Leader is Node {leader.id}")
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
        await asyncio.sleep(0.15)

    await asyncio.sleep(0.5)
    print("\n[Demo] After replication:")
    print_status(nodes)

    print(f"[Demo] Killing leader Node {leader.id}...")
    leader.transport.stop()
    leader.role = "dead"
    if leader._election_timer_task:
        leader._election_timer_task.cancel()
    if leader._heartbeat_task:
        leader._heartbeat_task.cancel()

    await asyncio.sleep(2.0)

    new_leader = find_leader(nodes)
    if new_leader:
        print(f"[Demo] New leader: Node {new_leader.id} at term {new_leader.current_term}")
    else:
        print("[Demo] No new leader yet...")

    print_status(nodes)
    print("[Demo] Done!")


# ── Entry point ────────────────────────────────────────────────────────────────

async def run(demo=False):
    nodes, transports = build_cluster()

    await asyncio.gather(*[t.start_server() for t in transports.values()])

    for n in nodes.values():
        n.start()
    await launch_visualizer(nodes)
    # if demo:
    #     await run_demo(nodes)
    # else:
    #     await interactive_cli(nodes)


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
"""
raft_visualizer.py — Pygame visualizer that reads from REAL Raft nodes.

Two ways to use it:

  1. STANDALONE (recommended):
       python raft_visualizer.py
     Boots the full cluster itself and opens the visualizer window.

  2. FROM main.py:
     Replace `await interactive_cli(nodes)` with:
       from raft_visualizer import launch_visualizer
       await launch_visualizer(nodes)

Architecture:
  • asyncio loop  — owns all real Node objects, runs the Raft protocol
  • pygame thread — reads node.status() each frame (read-only, safe)
  • cmd_queue     — pygame thread pushes CmdXxx objects
  • result_queue  — asyncio loop pushes back (text, is_error) tuples
"""

import pygame
import math
import random
import time
import sys
import asyncio
import threading
import queue
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# ── Palette ───────────────────────────────────────────────────────────────────
BG           = (8,   10,  18)
GRID_C       = (18,  22,  36)
FOLLOWER_C   = (40,  90, 175)
CANDIDATE_C  = (210, 145,  20)
LEADER_C     = (20,  185,  90)
DEAD_C       = (70,  20,  20)
EDGE_C       = (30,  48,  80)
MSG_VOTE_C   = (230, 185,  40)
MSG_HB_C     = (55,  155, 255)
MSG_LOG_C    = (255, 100,  75)
TEXT_MAIN    = (210, 225, 255)
TEXT_DIM     = (75,   95, 135)
PANEL_BG     = (12,  15,  25)
ACCENT       = (20,  200, 120)
INPUT_BG     = (14,  18,  32)
INPUT_BORDER = (45,  72, 120)
INPUT_ACTIVE = (20,  200, 120)
STEP_C       = (255, 200,  50)
AUTO_C       = (20,  200, 120)
TOAST_OK     = (20,  200, 120)
TOAST_ERR    = (220,  60,  60)

ROLE_COLORS = {
    "follower":  FOLLOWER_C,
    "candidate": CANDIDATE_C,
    "leader":    LEADER_C,
    "dead":      DEAD_C,
}

BANNER_H   = 44
INPUT_H    = 36
PANEL_FRAC = 0.725

# ── Fonts ─────────────────────────────────────────────────────────────────────
F_LARGE = F_MED = F_SMALL = F_MONO = F_TINY = None

def _load_fonts():
    global F_LARGE, F_MED, F_SMALL, F_MONO, F_TINY
    pygame.font.init()
    for name in ("Courier New", "Courier", "DejaVu Sans Mono", "monospace", None):
        try:
            F_TINY  = pygame.font.SysFont(name, 11)
            F_MONO  = pygame.font.SysFont(name, 13)
            F_SMALL = pygame.font.SysFont(name, 15)
            F_MED   = pygame.font.SysFont(name, 20)
            F_LARGE = pygame.font.SysFont(name, 26)
            break
        except Exception:
            continue


# ═════════════════════════════════════════════════════════════════════════════
# Snapshot — thread-safe copy of one node's state
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class NodeSnapshot:
    id:           int
    role:         str
    term:         int
    leader_id:    Optional[int]
    log_length:   int
    commit_index: int
    last_applied: int
    store:        dict
    message_count:int
    step_by_step: bool
    paused:       bool
    voted_for:    Optional[int]


def _snap(node) -> NodeSnapshot:
    s = node.status()
    return NodeSnapshot(
        id            = s["id"],
        role          = s["role"],
        term          = s["term"],
        leader_id     = s["leader_id"],
        log_length    = s["log_length"],
        commit_index  = s["commit_index"],
        last_applied  = s["last_applied"],
        store         = dict(s["store"]),
        message_count = s["message_count"],
        step_by_step  = s["step_by_step"],
        paused        = s["paused"],
        voted_for     = getattr(node, "voted_for", None),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Visual-only types (live only in pygame thread)
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class VisMessage:
    src:      int
    dst:      int
    kind:     str
    label:    str  = ""
    progress: float = 0.0
    speed:    float = 1.6

@dataclass
class VisFlash:
    nid:   int
    color: tuple
    ttl:   float = 0.45
    age:   float = 0.0

@dataclass
class Toast:
    text:  str
    color: tuple
    ttl:   float = 2.8
    age:   float = 0.0


# ═════════════════════════════════════════════════════════════════════════════
# Commands pushed from pygame thread → asyncio loop
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class CmdSet:      key: str; value: object
@dataclass
class CmdGet:      key: str
@dataclass
class CmdDelete:   key: str
@dataclass
class CmdKill:     nid: int
@dataclass
class CmdKillLeader: pass
@dataclass
class CmdStep:     pass
@dataclass
class CmdNext:     pass
@dataclass
class CmdAuto:     pass
@dataclass
class CmdStatus:   pass


# ═════════════════════════════════════════════════════════════════════════════
# CLI parser
# ═════════════════════════════════════════════════════════════════════════════

def parse_command(raw: str):
    parts = raw.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower()

    if cmd == "set" and len(parts) >= 3:
        key = parts[1]
        val_s = " ".join(parts[2:])
        try:    val = int(val_s)
        except ValueError:
            try:    val = float(val_s)
            except ValueError: val = val_s
        return CmdSet(key, val)

    if cmd == "get"    and len(parts) == 2:  return CmdGet(parts[1])
    if cmd == "delete" and len(parts) == 2:  return CmdDelete(parts[1])
    if cmd == "kill"   and len(parts) == 2:
        try:    return CmdKill(int(parts[1]))
        except ValueError: return None
    if cmd == "status":              return CmdStatus()
    if cmd == "step":                return CmdStep()
    if cmd in ("next", "n"):         return CmdNext()
    if cmd == "auto":                return CmdAuto()
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Input bar
# ═════════════════════════════════════════════════════════════════════════════

class InputBar:
    def __init__(self):
        self.text     = ""
        self.active   = False
        self.history: List[str] = []
        self.hist_idx = -1
        self.blink    = 0.0

    def tick(self, dt):
        self.blink = (self.blink + dt) % 1.0

    def handle_event(self, event) -> Optional[str]:
        if event.type != pygame.KEYDOWN:
            return None
        if not self.active:
            if event.key == pygame.K_TAB:
                self.active = True
            return None
        k = event.key
        if k == pygame.K_ESCAPE:
            self.active = False; self.text = ""; self.hist_idx = -1; return None
        if k == pygame.K_RETURN:
            cmd = self.text.strip()
            self.text = ""; self.active = False; self.hist_idx = -1
            if cmd: self.history.append(cmd)
            return cmd or None
        if k == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
        elif k == pygame.K_UP:
            if self.history:
                self.hist_idx = min(self.hist_idx + 1, len(self.history) - 1)
                self.text = self.history[-(self.hist_idx + 1)]
        elif k == pygame.K_DOWN:
            if self.hist_idx > 0:
                self.hist_idx -= 1
                self.text = self.history[-(self.hist_idx + 1)]
            else:
                self.hist_idx = -1; self.text = ""
        else:
            ch = event.unicode
            if ch and ch.isprintable():
                self.text += ch
        return None

    def draw(self, screen, x, y, w, h):
        border = INPUT_ACTIVE if self.active else INPUT_BORDER
        pygame.draw.rect(screen, INPUT_BG, (x, y, w, h))
        pygame.draw.rect(screen, border,   (x, y, w, h), 2)
        prompt = F_MONO.render("raft> ", True, ACCENT if self.active else TEXT_DIM)
        screen.blit(prompt, (x + 8, y + h // 2 - prompt.get_height() // 2))
        tx = x + 8 + prompt.get_width()
        if not self.active and not self.text:
            hint = F_MONO.render(
                "TAB  |  set <k> <v>  |  get <k>  |  delete <k>  "
                "|  kill <id>  |  step  |  next  |  auto  |  status",
                True, TEXT_DIM)
            screen.blit(hint, (tx, y + h // 2 - hint.get_height() // 2))
        else:
            ts = F_MONO.render(self.text, True, TEXT_MAIN)
            screen.blit(ts, (tx, y + h // 2 - ts.get_height() // 2))
            if self.active and self.blink < 0.5:
                pygame.draw.rect(screen, TEXT_MAIN,
                                 (tx + ts.get_width() + 2, y + 6, 2, h - 12))


# ═════════════════════════════════════════════════════════════════════════════
# Renderer
# ═════════════════════════════════════════════════════════════════════════════

class Renderer:
    def __init__(self):
        self.screen = pygame.display.set_mode((1320, 820), pygame.RESIZABLE)
        pygame.display.set_caption("Raft Visualizer  —  live cluster")
        self.clock  = pygame.time.Clock()
        self.W = 1320
        self.H = 820

    def _node_r(self, n):
        if n <= 3: return 38
        if n <= 5: return 34
        if n <= 7: return 28
        return 22

    def _ring_positions(self, nids):
        cx = self.W * 0.39
        cy = self.H * 0.51
        n  = len(nids)
        r  = min(self.W * 0.25, (self.H - BANNER_H - INPUT_H) * 0.35)
        r  = min(r * (1 + max(0, n - 5) * 0.04), min(self.W * 0.28, self.H * 0.38))
        if n == 1:
            return {nids[0]: (cx, cy)}
        pos = {}
        for i, nid in enumerate(sorted(nids)):
            a = -math.pi / 2 + 2 * math.pi * i / n
            pos[nid] = (cx + r * math.cos(a), cy + r * math.sin(a))
        return pos

    def _arc(self, cx, cy, r, a0, a1, color, w=3):
        pts = [(int(cx + r * math.cos(a0 + (a1-a0)*i/64)),
                int(cy + r * math.sin(a0 + (a1-a0)*i/64))) for i in range(65)]
        if len(pts) > 1:
            pygame.draw.lines(self.screen, color, False, pts, w)

    def _draw_bg(self):
        self.screen.fill(BG)
        for x in range(0, self.W, 38):
            pygame.draw.line(self.screen, GRID_C, (x, 0), (x, self.H))
        for y in range(0, self.H, 38):
            pygame.draw.line(self.screen, GRID_C, (0, y), (self.W, y))

    def _draw_edges(self, pos):
        nids = sorted(pos)
        for i, a in enumerate(nids):
            for b in nids[i+1:]:
                pygame.draw.line(self.screen, EDGE_C,
                    (int(pos[a][0]), int(pos[a][1])),
                    (int(pos[b][0]), int(pos[b][1])), 1)

    def _draw_node(self, snap: NodeSnapshot, pos, t, base_r, flashes):
        x, y  = int(pos[0]), int(pos[1])
        color = ROLE_COLORS.get(snap.role, FOLLOWER_C)
        f_id  = F_MED if base_r < 30 else F_LARGE
        f_rl  = F_TINY if base_r < 30 else F_SMALL

        # leader glow
        if snap.role == "leader":
            pulse = 0.5 + 0.5 * math.sin(t * 3.5)
            gr    = int(base_r + 16 + pulse * 10)
            gs    = pygame.Surface((gr*2, gr*2), pygame.SRCALPHA)
            pygame.draw.circle(gs, (*LEADER_C, 26), (gr, gr), gr)
            self.screen.blit(gs, (x - gr, y - gr))

        # flashes
        for fl in flashes:
            if fl.nid == snap.id:
                a = int(160 * (1 - fl.age / fl.ttl))
                fs = pygame.Surface((base_r*2+6, base_r*2+6), pygame.SRCALPHA)
                pygame.draw.circle(fs, (*fl.color, a), (base_r+3, base_r+3), base_r+3)
                self.screen.blit(fs, (x-base_r-3, y-base_r-3))

        # step-mode indicators
        if snap.paused:
            pygame.draw.circle(self.screen, STEP_C, (x, y), base_r + 11, 2)
        elif snap.step_by_step:
            self._arc(x, y, base_r+9, 0, 2*math.pi, STEP_C, 1)

        # ring + fill
        pygame.draw.circle(self.screen,
                           (200,230,255) if snap.role == "leader" else color,
                           (x, y), base_r+2, 2)
        pygame.draw.circle(self.screen,
                           tuple(max(0, c-55) for c in color),
                           (x, y), base_r)

        # labels
        id_s = f_id.render(str(snap.id), True, TEXT_MAIN)
        self.screen.blit(id_s, (x - id_s.get_width()//2, y - id_s.get_height()//2))

        rl = f_rl.render(snap.role.upper(), True, color)
        self.screen.blit(rl, (x - rl.get_width()//2, y + base_r + 5))

        tl = F_TINY.render(f"T{snap.term}", True, TEXT_DIM)
        self.screen.blit(tl, (x - tl.get_width()//2, y + base_r + 19))

        if base_r >= 28 and snap.voted_for is not None \
                and snap.role not in ("leader","dead"):
            vl = F_TINY.render(f"voted→{snap.voted_for}", True, MSG_VOTE_C)
            self.screen.blit(vl, (x - vl.get_width()//2, y + base_r + 33))

        if snap.paused:
            pl = F_TINY.render("⏸", True, STEP_C)
            self.screen.blit(pl, (x - pl.get_width()//2, y - base_r - 16))

    def _draw_messages(self, pos, messages):
        for msg in messages:
            if msg.src not in pos or msg.dst not in pos:
                continue
            p1, p2 = pos[msg.src], pos[msg.dst]
            tt = msg.progress
            mx = p1[0] + (p2[0]-p1[0])*tt
            my = p1[1] + (p2[1]-p1[1])*tt
            color = {"vote": MSG_VOTE_C, "heartbeat": MSG_HB_C,
                     "log":  MSG_LOG_C}.get(msg.kind, TEXT_MAIN)
            for frac in (0.10, 0.22):
                bt = max(0.0, tt - frac)
                bx = p1[0] + (p2[0]-p1[0])*bt
                by = p1[1] + (p2[1]-p1[1])*bt
                s  = pygame.Surface((8,8), pygame.SRCALPHA)
                pygame.draw.circle(s, (*color, int(70*(1-frac/0.22))), (4,4), 4)
                self.screen.blit(s, (int(bx)-4, int(by)-4))
            pygame.draw.circle(self.screen, color, (int(mx), int(my)), 6)
            sym = {"vote":"V","heartbeat":"♥","log":"L"}.get(msg.kind,"?")
            self.screen.blit(F_MONO.render(sym, True, color), (int(mx)+8, int(my)-8))
            if msg.label:
                self.screen.blit(F_TINY.render(msg.label, True, color),
                                 (int(mx)+8, int(my)+6))

    def _draw_banner(self, snaps):
        surf = pygame.Surface((self.W, BANNER_H), pygame.SRCALPHA)
        any_step   = any(s.step_by_step for s in snaps.values())
        any_paused = any(s.paused       for s in snaps.values())
        if any_step:
            surf.fill((20, 18, 5, 215))
            surf.blit(F_MED.render("⏭  STEP MODE", True, STEP_C), (14, 5))
            surf.blit(F_TINY.render(
                "next / n  advance step    auto  resume    TAB  command bar",
                True, TEXT_DIM), (14, 27))
            if any_paused:
                surf.blit(F_MED.render("⏸  nodes waiting", True, CANDIDATE_C), (260, 5))
        else:
            surf.fill((5, 18, 10, 215))
            surf.blit(F_MED.render("▶  LIVE", True, AUTO_C), (14, 5))
            surf.blit(F_TINY.render(
                "step  enable step-by-step    K  kill leader    TAB  command bar",
                True, TEXT_DIM), (14, 27))
        self.screen.blit(surf, (0, 0))

    def _draw_panel(self, snaps, event_log):
        px = int(self.W * PANEL_FRAC)
        pw = self.W - px - 6
        ph = self.H - BANNER_H - INPUT_H - 4
        py = BANNER_H + 2

        surf = pygame.Surface((pw, ph), pygame.SRCALPHA)
        surf.fill((*PANEL_BG, 238))
        self.screen.blit(surf, (px, py))

        y = py + 8

        def ln(text, color=TEXT_MAIN, font=None):
            nonlocal y
            f   = font or F_SMALL
            sur = f.render(text, True, color)
            self.screen.blit(sur, (px + 12, y))
            y  += sur.get_height() + 2

        ln("RAFT  VISUALIZER", ACCENT, F_MED)
        ln("─"*30, TEXT_DIM, F_MONO)

        leader   = next((s for s in snaps.values() if s.role=="leader"), None)
        alive    = sum(1 for s in snaps.values() if s.role != "dead")
        majority = len(snaps)//2 + 1

        ln(f"Nodes   : {len(snaps)}")
        ln(f"Alive   : {alive}")
        ln(f"Majority: {majority}")
        ln(f"Leader  : {'Node '+str(leader.id)+'  T'+str(leader.term) if leader else 'none'}",
           LEADER_C if leader else CANDIDATE_C)

        y += 4
        ln("─"*30, TEXT_DIM, F_MONO)
        ln("ID  ROLE       TERM  LOG  CMT  MSGS", TEXT_DIM, F_MONO)
        for nid in sorted(snaps):
            s     = snaps[nid]
            color = ROLE_COLORS.get(s.role, TEXT_DIM)
            pmark = "⏸" if s.paused else (" S" if s.step_by_step else "  ")
            row   = (f"{s.id:2}  {s.role[:9].ljust(9)}  {s.term:4}  "
                     f"{s.log_length:3}  {s.commit_index:3}  {s.message_count:4} {pmark}")
            ln(row, color, F_MONO)

        y += 4
        ln("─"*30, TEXT_DIM, F_MONO)
        ln("State machine  (leader):", TEXT_DIM)
        if leader and leader.store:
            for k, v in list(leader.store.items())[:10]:
                ln(f"  {k}: {v}"[:34], TEXT_MAIN, F_MONO)
        else:
            ln("  (empty)", TEXT_DIM, F_MONO)

        y += 4
        ln("─"*30, TEXT_DIM, F_MONO)
        ln("Event log:", TEXT_DIM)
        avail = (py + ph) - y - 105
        for entry in event_log[-max(1, avail//15):]:
            ln(entry[:36], TEXT_DIM, F_TINY)

        y = py + ph - 105
        ln("─"*30, TEXT_DIM, F_MONO)
        for key, desc in [("N/next","advance step"),("auto","resume normal"),
                          ("step","step-by-step"),("K","kill leader"),
                          ("TAB","command bar"),("Q/ESC","quit")]:
            ln(f"  {key:<7} {desc}", TEXT_DIM, F_TINY)

    def _draw_legend(self):
        x = 14
        y = self.H - INPUT_H - 148
        for color, label in [(FOLLOWER_C,"Follower"),(CANDIDATE_C,"Candidate"),
                              (LEADER_C,"Leader"),(DEAD_C,"Dead")]:
            pygame.draw.circle(self.screen, color, (x+7, y+7), 7)
            self.screen.blit(F_TINY.render(label, True, TEXT_DIM), (x+20, y+1))
            y += 19
        y += 4
        for color, label in [(MSG_HB_C,"♥ Heartbeat"),(MSG_VOTE_C,"V Vote RPC"),
                              (MSG_LOG_C,"L Log entry")]:
            pygame.draw.circle(self.screen, color, (x+5, y+6), 5)
            self.screen.blit(F_TINY.render(label, True, TEXT_DIM), (x+16, y))
            y += 17

    def _draw_toasts(self, toasts):
        ty  = BANNER_H + 8
        px  = int(self.W * PANEL_FRAC) - 420
        for toast in reversed(toasts[-5:]):
            alpha = int(255 * min(1.0, (toast.ttl - toast.age) / 0.35))
            surf  = pygame.Surface((410, 28), pygame.SRCALPHA)
            surf.fill((12, 16, 28, min(alpha, 220)))
            pygame.draw.rect(surf, (*toast.color, alpha), (0, 0, 410, 28), 2)
            surf.blit(F_SMALL.render(toast.text[:52], True, toast.color), (10, 5))
            self.screen.blit(surf, (px, ty))
            ty += 32

    def draw(self, snaps, messages, flashes, toasts, event_log, t, ibar):
        self.W, self.H = self.screen.get_size()
        self._draw_bg()
        pos    = self._ring_positions(list(snaps.keys()))
        base_r = self._node_r(len(snaps))
        self._draw_edges(pos)
        self._draw_messages(pos, messages)
        for nid, snap in snaps.items():
            if nid in pos:
                self._draw_node(snap, pos[nid], t, base_r, flashes)
        self._draw_panel(snaps, event_log)
        self._draw_legend()
        self._draw_banner(snaps)
        self._draw_toasts(toasts)
        ibar.draw(self.screen, 0, self.H - INPUT_H,
                  int(self.W * PANEL_FRAC), INPUT_H)
        pygame.display.flip()


# ═════════════════════════════════════════════════════════════════════════════
# Pygame thread
# ═════════════════════════════════════════════════════════════════════════════

def _pygame_thread(nodes: dict, cmd_queue: queue.Queue,
                   result_queue: queue.Queue, stop_event: threading.Event):
    pygame.init()
    _load_fonts()

    renderer = Renderer()
    ibar     = InputBar()

    messages:  List[VisMessage] = []
    flashes:   List[VisFlash]   = []
    toasts:    List[Toast]      = []
    event_log: List[str]        = []

    prev_roles:  Dict[int, str] = {}
    prev_counts: Dict[int, int] = {}
    prev_logs:   Dict[int, int] = {}

    def elog(text):
        ts = time.strftime("%H:%M:%S")
        event_log.append(f"{ts}  {text}")
        if len(event_log) > 100:
            event_log.pop(0)

    def toast(text, error=False):
        toasts.append(Toast(text, TOAST_ERR if error else TOAST_OK))

    def flash(nid, color):
        flashes.append(VisFlash(nid, color))

    elog(f"Connected to live cluster  ({len(nodes)} nodes)")

    t_last = time.time()
    t      = 0.0

    while not stop_event.is_set():
        now    = time.time()
        dt     = min(now - t_last, 0.05)
        t_last = now
        t     += dt

        # ── events ────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                stop_event.set(); break

            cmd_text = ibar.handle_event(event)
            if cmd_text:
                cmd = parse_command(cmd_text)
                if cmd:
                    cmd_queue.put(cmd)
                    elog(f"→  {cmd_text}")
                else:
                    toast(f"Unknown: {cmd_text!r}", error=True)
                continue

            if event.type == pygame.KEYDOWN and not ibar.active:
                k = event.key
                if k in (pygame.K_q, pygame.K_ESCAPE):
                    stop_event.set()
                elif k == pygame.K_k:
                    cmd_queue.put(CmdKillLeader()); elog("→  kill leader")
                elif k in (pygame.K_n, pygame.K_RIGHT):
                    cmd_queue.put(CmdNext()); elog("→  next")
                elif k == pygame.K_a:
                    # peek at current state to toggle
                    try:
                        any_step = any(n.step_by_step for n in nodes.values())
                    except Exception:
                        any_step = False
                    if any_step:
                        cmd_queue.put(CmdAuto()); elog("→  auto")
                    else:
                        cmd_queue.put(CmdStep()); elog("→  step")
                elif k == pygame.K_SPACE:
                    cmd_queue.put(CmdNext())
            elif event.type == pygame.VIDEORESIZE:
                renderer.screen = pygame.display.set_mode(event.size, pygame.RESIZABLE)

        # ── drain result queue ─────────────────────────────────────
        while True:
            try:
                text, is_err = result_queue.get_nowait()
                toast(text, error=is_err)
                elog(text)
            except queue.Empty:
                break

        # ── snapshot real nodes ───────────────────────────────────
        try:
            snaps = {nid: _snap(n) for nid, n in nodes.items()}
        except Exception as e:
            snaps = {}

        # ── infer visual events from state changes ────────────────
        for nid, snap in snaps.items():
            pr = prev_roles.get(nid)
            pc = prev_counts.get(nid, 0)
            pl = prev_logs.get(nid, 0)

            if pr == "follower" and snap.role == "candidate":
                flash(nid, CANDIDATE_C)
                elog(f"Node {nid} → CANDIDATE  T{snap.term}")
                peers = [p for p in snaps if p != nid and snaps[p].role != "dead"]
                for peer in peers:
                    messages.append(VisMessage(nid, peer, "vote",
                                               speed=random.uniform(1.0,1.8)))

            elif pr == "candidate" and snap.role == "leader":
                flash(nid, LEADER_C)
                toast(f"Node {nid} became leader  (T{snap.term})")
                elog(f"Node {nid} → LEADER  T{snap.term} 🎉")

            elif snap.role == "dead" and pr not in ("dead", None):
                flash(nid, DEAD_C)
                elog(f"Node {nid} → DEAD")

            if snap.role == "leader" and snap.message_count > pc:
                # emit visual heartbeat dots proportional to new messages
                peers = [p for p in snaps if p != nid and snaps[p].role != "dead"]
                for peer in random.sample(peers, min(len(peers), snap.message_count - pc)):
                    kind = "log" if snap.log_length > snaps[peer].log_length else "heartbeat"
                    messages.append(VisMessage(nid, peer, kind,
                                               speed=random.uniform(1.0,1.6)))

            if snap.role == "follower" and snap.log_length > pl:
                flash(nid, MSG_LOG_C)

            prev_roles[nid]  = snap.role
            prev_counts[nid] = snap.message_count
            prev_logs[nid]   = snap.log_length

        # ── animate ───────────────────────────────────────────────
        messages = [m for m in messages if m.progress < 1.0]
        for m in messages:
            m.progress = min(1.0, m.progress + m.speed * dt)
        if len(messages) > 80:
            messages = messages[-80:]

        for fl in flashes:  fl.age += dt
        flashes = [fl for fl in flashes if fl.age < fl.ttl]

        for to in toasts:   to.age += dt
        toasts = [to for to in toasts if to.age < to.ttl]

        ibar.tick(dt)
        renderer.draw(snaps, messages, flashes, toasts, event_log, t, ibar)
        renderer.clock.tick(60)

    pygame.quit()


# ═════════════════════════════════════════════════════════════════════════════
# Asyncio command executor
# ═════════════════════════════════════════════════════════════════════════════

async def _run_commands(nodes: dict, cmd_queue: queue.Queue,
                        result_queue: queue.Queue, stop_event: threading.Event):
    def find_leader():
        return next((n for n in nodes.values() if n.role == "leader"), None)

    def push(text, error=False):
        result_queue.put((text, error))

    while not stop_event.is_set():
        await asyncio.sleep(0.02)
        while not cmd_queue.empty():
            try:
                cmd = cmd_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(cmd, CmdSet):
                ldr = find_leader()
                if not ldr:
                    push("No leader — wait for election", error=True)
                else:
                    ok = await ldr.client_request(
                        {"op": "set", "key": cmd.key, "value": cmd.value})
                    push(f"✓  SET  {cmd.key} = {cmd.value}" if ok
                         else "SET failed — leader stepped down", error=not ok)

            elif isinstance(cmd, CmdGet):
                ldr = find_leader()
                if not ldr:
                    push("No leader", error=True)
                else:
                    val = ldr.state_machine.get(cmd.key)
                    push(f"{cmd.key} = {val!r}")

            elif isinstance(cmd, CmdDelete):
                ldr = find_leader()
                if not ldr:
                    push("No leader", error=True)
                else:
                    await ldr.client_request({"op": "delete", "key": cmd.key})
                    push(f"✓  DELETE  {cmd.key}")

            elif isinstance(cmd, CmdKill):
                n = nodes.get(cmd.nid)
                if not n:
                    push(f"Unknown node {cmd.nid}", error=True)
                elif n.role == "dead":
                    push(f"Node {cmd.nid} already dead", error=True)
                else:
                    n.transport.stop()
                    n.role = "dead"
                    if n._election_timer_task: n._election_timer_task.cancel()
                    if n._heartbeat_task:      n._heartbeat_task.cancel()
                    push(f"✗  Node {cmd.nid} killed")

            elif isinstance(cmd, CmdKillLeader):
                ldr = find_leader()
                if not ldr:
                    push("No leader to kill", error=True)
                else:
                    ldr.transport.stop()
                    ldr.role = "dead"
                    if ldr._election_timer_task: ldr._election_timer_task.cancel()
                    if ldr._heartbeat_task:      ldr._heartbeat_task.cancel()
                    push(f"✗  Node {ldr.id} (leader) killed — re-election starting")

            elif isinstance(cmd, CmdStep):
                for n in nodes.values():
                    if n.role != "dead":
                        n.step_by_step = True
                push("Step-by-step ON — press N / next to advance")

            elif isinstance(cmd, CmdNext):
                advanced = sum(
                    1 for n in nodes.values()
                    if n.step_by_step and not n._step_event.is_set()
                    and not n.advance_step()  # advance_step returns None
                )
                # advance_step() returns None so the above won't count right;
                # do it properly:
                advanced = 0
                for n in nodes.values():
                    if n.step_by_step and not n._step_event.is_set():
                        n.advance_step()
                        advanced += 1
                push(f"Advanced {advanced} node(s)" if advanced
                     else "No nodes currently paused")

            elif isinstance(cmd, CmdAuto):
                for n in nodes.values():
                    n.step_by_step = False
                    n.advance_step()
                push("Auto mode — cluster running normally")

            elif isinstance(cmd, CmdStatus):
                ldr = find_leader()
                push(f"Leader: {'Node '+str(ldr.id) if ldr else 'none'}")
                for n in sorted(nodes.values(), key=lambda x: x.id):
                    s = n.status()
                    push(f"  [{s['id']}] {s['role'].upper():<9} "
                         f"T{s['term']}  log={s['log_length']}  cmt={s['commit_index']}")


# ═════════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════════

async def launch_visualizer(nodes: dict):
    """
    Drop-in replacement for interactive_cli(nodes) in main.py.

    Usage in main.py:
        from raft_visualizer import launch_visualizer
        ...
        await launch_visualizer(nodes)
    """
    cmd_q    = queue.Queue()
    result_q = queue.Queue()
    stop     = threading.Event()

    t = threading.Thread(target=_pygame_thread,
                         args=(nodes, cmd_q, result_q, stop), daemon=True)
    t.start()

    await _run_commands(nodes, cmd_q, result_q, stop)
    t.join(timeout=2.0)


# ═════════════════════════════════════════════════════════════════════════════
# Standalone entry point — boots real cluster then opens visualizer
# ═════════════════════════════════════════════════════════════════════════════

async def _standalone():
    from config    import NODES
    from transport import Transport
    from node      import Node

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    nodes, transports = {}, {}
    for nid in NODES:
        t = Transport(nid)
        n = Node(nid, t)
        t.on_request_vote   = n.handle_request_vote
        t.on_append_entries = n.handle_append_entries
        nodes[nid]      = n
        transports[nid] = t

    await asyncio.gather(*[t.start_server() for t in transports.values()])
    for n in nodes.values():
        n.start()

    print(f"Cluster started: {len(nodes)} nodes  "
          f"({', '.join(str(k) for k in sorted(nodes))})")
    print("Opening visualizer window...")
    await launch_visualizer(nodes)


if __name__ == "__main__":
    try:
        asyncio.run(_standalone())
    except KeyboardInterrupt:
        print("\nShutting down.")
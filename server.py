#!/usr/bin/env python3
"""
Laser Chess — Auto-Matchmaking Server v3
Queue-based: players join a mode queue (bullet/blitz/rapid), server auto-matches
by ELO proximity. Search range widens every 10 s until anyone is accepted.

Local:   pip install websockets && python server.py
Deploy:  Push to GitHub → Render.com auto-deploys from this repo
"""

import asyncio
import json
import os
import random
import sys
import time

sys.stdout.reconfigure(line_buffering=True)

try:
    import websockets
    from websockets.server import serve
except ImportError:
    print("Install: pip install websockets")
    exit(1)

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8765))

# --- Chess-themed name generator ---
_ADJ = [
    "Swift","Bold","Sly","Fierce","Quick","Sharp","Wild","Dark","Bright",
    "Lucky","Brave","Cool","Keen","Grim","Deft","Calm","Red","Iron","Jade","Void"
]
_PIECE = ["Pawn","Rook","Bishop","Knight","King","Queen","Castle"]

def _gen_name():
    return random.choice(_ADJ) + random.choice(_PIECE)

# --- State ---

class Player:
    __slots__ = ("ws","id","player_id","name",
                 "elo_bullet","elo_blitz","elo_rapid",
                 "match","best_score","queue_mode","queue_time")
    def __init__(self, ws, pid, name):
        self.ws         = ws
        self.id         = pid
        self.player_id  = ""
        self.name       = name
        self.elo_bullet = 1000
        self.elo_blitz  = 1000
        self.elo_rapid  = 1000
        self.match      = None
        self.best_score = 0
        self.queue_mode = None
        self.queue_time = None

class Match:
    __slots__ = ("p1","p2","seed","ended","mode")
    def __init__(self, p1, p2, seed, mode):
        self.p1   = p1
        self.p2   = p2
        self.seed = seed
        self.ended = False
        self.mode  = mode

players = {}   # ws -> Player
queues  = {"bullet": [], "blitz": [], "rapid": []}
_next_id = 0

# --- ELO helpers ---

def _get_elo(p, mode):
    if mode == "blitz": return p.elo_blitz
    if mode == "rapid": return p.elo_rapid
    return p.elo_bullet

def _set_elo(p, mode, value):
    value = max(100, value)
    if mode == "blitz": p.elo_blitz  = value
    elif mode == "rapid": p.elo_rapid = value
    else: p.elo_bullet = value

# --- Communication ---

async def _send(ws, data):
    try:
        await ws.send(json.dumps(data))
    except Exception:
        pass

# --- Queue handlers ---

async def _handle_queue(player, data):
    mode = data.get("time_mode", "bullet")
    if mode not in queues:
        mode = "bullet"

    # Update ELOs from client
    if "elo_bullet" in data: player.elo_bullet = max(100, int(data["elo_bullet"]))
    if "elo_blitz"  in data: player.elo_blitz  = max(100, int(data["elo_blitz"]))
    if "elo_rapid"  in data: player.elo_rapid  = max(100, int(data["elo_rapid"]))
    if "name"      in data and data["name"]: player.name      = data["name"]
    if "player_id" in data:                  player.player_id = data["player_id"]

    # Remove from any existing queue
    for q in queues.values():
        if player in q:
            q.remove(player)

    player.queue_mode = mode
    player.queue_time = time.monotonic()
    queues[mode].append(player)

    await _send(player.ws, {"type": "queued", "time_mode": mode})
    print(f"  [QUEUE] {player.name} queued for {mode} (ELO {_get_elo(player, mode)}) | queue size: {len(queues[mode])}")

async def _handle_leave_queue(player):
    for q in queues.values():
        if player in q:
            q.remove(player)
    player.queue_mode = None
    player.queue_time = None
    await _send(player.ws, {"type": "queue_cancelled"})
    print(f"  [QUEUE] {player.name} left queue")

# --- Match handlers ---

async def _handle_score(player, data):
    m = player.match
    if not m or m.ended:
        return
    player.best_score = data.get("best_score", 0)
    opp = m.p2 if player == m.p1 else m.p1
    await _send(opp.ws, {"type": "opponent_score", "best_score": player.best_score})

async def _handle_match_end(player, data):
    m = player.match
    if not m or m.ended:
        return
    player.best_score = data.get("best_score", player.best_score)
    m.ended = True
    mode = m.mode

    K = 32
    elo_changes = {}
    for p, o in [(m.p1, m.p2), (m.p2, m.p1)]:
        result = "win" if p.best_score > o.best_score else ("lose" if p.best_score < o.best_score else "draw")
        actual = 1.0 if result == "win" else (0.0 if result == "lose" else 0.5)
        p_elo = _get_elo(p, mode)
        o_elo = _get_elo(o, mode)
        expected = 1.0 / (1.0 + 10 ** ((o_elo - p_elo) / 400.0))
        elo_change = int(round(K * (actual - expected)))
        elo_changes[p.id] = (result, elo_change, o)

    for p in [m.p1, m.p2]:
        result, elo_change, opponent = elo_changes[p.id]
        new_elo = max(100, _get_elo(p, mode) + elo_change)
        _set_elo(p, mode, new_elo)
        await _send(p.ws, {
            "type": "match_result",
            "result": result,
            "my_score": p.best_score,
            "opp_score": opponent.best_score,
            "elo_change": elo_change,
            "opponent_name": opponent.name,
            "opponent_elo": _get_elo(opponent, mode),
            "opponent_player_id": opponent.player_id,
            "time_mode": mode
        })

    m.p1.match = None
    m.p2.match = None
    print(f"  [END] {m.p1.name}({_get_elo(m.p1,mode)})={m.p1.best_score} vs "
          f"{m.p2.name}({_get_elo(m.p2,mode)})={m.p2.best_score} [{mode}]")

async def _handle_disconnect(player):
    # Remove from any queue
    for q in queues.values():
        if player in q:
            q.remove(player)

    m = player.match
    if m and not m.ended:
        m.ended = True
        mode = m.mode
        opp = m.p2 if player == m.p1 else m.p1

        K = 32
        p_elo = _get_elo(player, mode)
        o_elo = _get_elo(opp, mode)
        expected = 1.0 / (1.0 + 10 ** ((p_elo - o_elo) / 400.0))
        elo_change = int(round(K * (1.0 - expected)))

        new_elo = max(100, o_elo + elo_change)
        _set_elo(opp, mode, new_elo)
        opp.match = None

        await _send(opp.ws, {
            "type": "opponent_disconnected",
            "elo_change": elo_change,
            "my_score": opp.best_score,
            "opp_score": player.best_score,
            "opponent_name": player.name,
            "opponent_elo": _get_elo(player, mode),
            "opponent_player_id": player.player_id,
            "time_mode": mode
        })
        print(f"  [DC] {player.name} left match -> {opp.name} wins (+{elo_change} {mode} ELO)")

    if player.ws in players:
        del players[player.ws]
    print(f"  [-] {player.name} disconnected ({len(players)} online)")

# --- Auto-Matchmaking ---

async def _try_match_queue(mode: str):
    queue = queues[mode]
    if len(queue) < 2:
        return

    now = time.monotonic()
    matched_ids = set()
    to_match = []

    for i in range(len(queue)):
        p1 = queue[i]
        if id(p1) in matched_ids:
            continue

        elo1   = _get_elo(p1, mode)
        wait1  = now - p1.queue_time
        range1 = int(100 + 50 * (wait1 / 10.0))   # +50 ELO per 10s waited

        best      = None
        best_diff = float('inf')

        for j in range(i + 1, len(queue)):
            p2 = queue[j]
            if id(p2) in matched_ids:
                continue

            elo2   = _get_elo(p2, mode)
            wait2  = now - p2.queue_time
            range2 = int(100 + 50 * (wait2 / 10.0))

            diff = abs(elo1 - elo2)
            # Both players must be within each other's acceptable range
            if diff <= min(range1, range2) and diff < best_diff:
                best_diff = diff
                best = p2

        if best:
            matched_ids.add(id(p1))
            matched_ids.add(id(best))
            to_match.append((p1, best))

    # Remove matched players from queue before starting matches
    queues[mode] = [p for p in queue if id(p) not in matched_ids]

    for p1, p2 in to_match:
        asyncio.create_task(_start_match(p1, p2, mode))

async def _start_match(p1, p2, mode):
    seed = random.randint(0, 2**31 - 1)
    m = Match(p1, p2, seed, mode)
    p1.match = m; p1.queue_mode = None; p1.best_score = 0
    p2.match = m; p2.queue_mode = None; p2.best_score = 0

    await _send(p1.ws, {
        "type": "match_start",
        "seed": seed,
        "opponent": p2.name,
        "opponent_elo": _get_elo(p2, mode),
        "opponent_player_id": p2.player_id,
        "time_mode": mode
    })
    await _send(p2.ws, {
        "type": "match_start",
        "seed": seed,
        "opponent": p1.name,
        "opponent_elo": _get_elo(p1, mode),
        "opponent_player_id": p1.player_id,
        "time_mode": mode
    })
    print(f"  [MATCH] {p1.name}({_get_elo(p1,mode)}) vs {p2.name}({_get_elo(p2,mode)}) | {mode} seed={seed}")

async def _matchmaking_loop():
    while True:
        await asyncio.sleep(1.0)
        for mode in queues:
            await _try_match_queue(mode)

# --- WebSocket Handler ---

async def handler(ws):
    global _next_id
    _next_id += 1
    name = _gen_name()
    names = {p.name for p in players.values()}
    while name in names:
        name = _gen_name() + str(random.randint(10, 99))

    player = Player(ws, _next_id, name)
    players[ws] = player
    print(f"  [+] {name} connected ({len(players)} online)")
    await _send(ws, {"type": "welcome", "id": player.id, "name": name})

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
                t = data.get("type", "")

                if t == "queue_for_match":
                    await _handle_queue(player, data)

                elif t == "leave_queue":
                    await _handle_leave_queue(player)

                elif t == "score_update":
                    await _handle_score(player, data)

                elif t == "ghost_pos":
                    m = player.match
                    if m and not m.ended:
                        opp = m.p2 if player == m.p1 else m.p1
                        print(f"  [GHOST] {player.name} -> {opp.name} ({data.get('x')},{data.get('y')})")
                        await _send(opp.ws, {
                            "type": "opponent_ghost",
                            "x": data.get("x", 0),
                            "y": data.get("y", 0)
                        })

                elif t == "match_end":
                    await _handle_match_end(player, data)

                elif t == "update_info":
                    if "name"      in data and data["name"]: player.name      = data["name"]
                    if "elo_bullet" in data: player.elo_bullet = max(100, int(data["elo_bullet"]))
                    if "elo_blitz"  in data: player.elo_blitz  = max(100, int(data["elo_blitz"]))
                    if "elo_rapid"  in data: player.elo_rapid  = max(100, int(data["elo_rapid"]))

            except json.JSONDecodeError:
                pass

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await _handle_disconnect(player)

# --- Main ---

async def main():
    print(f"=== Laser Chess Server v3 (auto-matchmaking) ===")
    print(f"Listening on ws://{HOST}:{PORT}")
    print()
    asyncio.create_task(_matchmaking_loop())
    async with serve(handler, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

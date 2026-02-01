#!/usr/bin/env python3
"""
Laser Chess — Matchmaking + Relay Server
Players see a lobby of who's online, click to challenge.

Local:   pip install websockets && python server.py
Deploy:  Push to GitHub → connect to Render.com (free tier)
"""

import asyncio
import json
import os
import random

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
    __slots__ = ("ws","id","name","in_lobby","match","best_score")
    def __init__(self, ws, pid, name):
        self.ws = ws
        self.id = pid
        self.name = name
        self.in_lobby = False
        self.match = None
        self.best_score = 0

class Match:
    __slots__ = ("p1","p2","seed","ended")
    def __init__(self, p1, p2, seed):
        self.p1 = p1
        self.p2 = p2
        self.seed = seed
        self.ended = False

players = {}   # ws -> Player
_next_id = 0

# --- Helpers ---

async def _send(ws, data):
    try:
        await ws.send(json.dumps(data))
    except Exception:
        pass

async def _broadcast_lobby():
    """Send updated lobby list to everyone in the lobby."""
    lobby = [p for p in players.values() if p.in_lobby]
    total = len(players)
    plist = [{"id": p.id, "name": p.name} for p in lobby]
    msg = {"type": "lobby_list", "players": plist, "total_online": total}
    for p in lobby:
        await _send(p.ws, msg)

# --- Handlers ---

async def _handle_challenge(player, data):
    tid = data.get("target_id")
    target = None
    for p in players.values():
        if p.id == tid:
            target = p
            break

    if not target or not target.in_lobby:
        await _send(player.ws, {"type": "challenge_failed", "msg": "Player is no longer available"})
        return
    if target.id == player.id:
        await _send(player.ws, {"type": "challenge_failed", "msg": "Cannot challenge yourself"})
        return
    if not player.in_lobby:
        await _send(player.ws, {"type": "challenge_failed", "msg": "You are not in the lobby"})
        return

    seed = random.randint(0, 2**31 - 1)
    m = Match(player, target, seed)

    player.in_lobby = False
    player.match = m
    player.best_score = 0
    target.in_lobby = False
    target.match = m
    target.best_score = 0

    await _send(player.ws, {"type": "match_start", "seed": seed, "opponent": target.name})
    await _send(target.ws, {"type": "match_start", "seed": seed, "opponent": player.name})
    await _broadcast_lobby()
    print(f"  [MATCH] {player.name} vs {target.name} | seed={seed}")

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
    for p, o in [(m.p1, m.p2), (m.p2, m.p1)]:
        r = "win" if p.best_score > o.best_score else ("lose" if p.best_score < o.best_score else "draw")
        await _send(p.ws, {"type": "match_result", "result": r, "my_score": p.best_score, "opp_score": o.best_score})
    m.p1.match = None
    m.p2.match = None
    print(f"  [END] {m.p1.name}={m.p1.best_score} vs {m.p2.name}={m.p2.best_score}")

async def _handle_disconnect(player):
    m = player.match
    if m and not m.ended:
        m.ended = True
        opp = m.p2 if player == m.p1 else m.p1
        opp.match = None
        await _send(opp.ws, {"type": "opponent_disconnected"})
        print(f"  [DC] {player.name} left match -> {opp.name} wins")

    was_lobby = player.in_lobby
    if player.ws in players:
        del players[player.ws]
    if was_lobby:
        await _broadcast_lobby()
    print(f"  [-] {player.name} disconnected ({len(players)} online)")

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
                if t == "join_lobby":
                    player.in_lobby = True
                    await _broadcast_lobby()
                elif t == "leave_lobby":
                    player.in_lobby = False
                    await _broadcast_lobby()
                elif t == "challenge":
                    await _handle_challenge(player, data)
                elif t == "score_update":
                    await _handle_score(player, data)
                elif t == "match_end":
                    await _handle_match_end(player, data)
                elif t == "rejoin_lobby":
                    player.match = None
                    player.best_score = 0
                    player.in_lobby = True
                    await _broadcast_lobby()
            except json.JSONDecodeError:
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await _handle_disconnect(player)

# --- Main ---

async def main():
    print(f"=== Laser Chess Server ===")
    print(f"Listening on ws://{HOST}:{PORT}")
    print()
    async with serve(handler, HOST, PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

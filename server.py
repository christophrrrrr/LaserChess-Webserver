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
    __slots__ = ("ws", "id", "player_id", "name", "elo", "in_lobby", "match", "best_score")
    def __init__(self, ws, pid, name, elo=1000, player_id=""):
        self.ws = ws
        self.id = pid  # Session ID (server-assigned)
        self.player_id = player_id  # Persistent player ID (from client)
        self.name = name
        self.elo = elo
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
    plist = [{"id": p.id, "name": p.name, "elo": p.elo} for p in lobby]
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

    # Include opponent's name, ELO, and player_id in match_start
    await _send(player.ws, {
        "type": "match_start",
        "seed": seed,
        "opponent": target.name,
        "opponent_elo": target.elo,
        "opponent_player_id": target.player_id
    })
    await _send(target.ws, {
        "type": "match_start",
        "seed": seed,
        "opponent": player.name,
        "opponent_elo": player.elo,
        "opponent_player_id": player.player_id
    })
    await _broadcast_lobby()
    print(f"  [MATCH] {player.name} ({player.elo}) vs {target.name} ({target.elo}) | seed={seed}")

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
    
    # Calculate ELO changes using standard chess formula (K-factor = 32)
    K = 32
    elo_changes = {}
    for p, o in [(m.p1, m.p2), (m.p2, m.p1)]:
        result = "win" if p.best_score > o.best_score else ("lose" if p.best_score < o.best_score else "draw")
        actual = 1.0 if result == "win" else (0.0 if result == "lose" else 0.5)
        expected = 1.0 / (1.0 + 10 ** ((o.elo - p.elo) / 400.0))
        elo_change = int(round(K * (actual - expected)))
        elo_changes[p.id] = (result, elo_change, o)
    
    # Update ELOs and send results
    for p in [m.p1, m.p2]:
        result, elo_change, opponent = elo_changes[p.id]
        p.elo += elo_change
        p.elo = max(100, p.elo)  # Minimum ELO is 100
        await _send(p.ws, {
            "type": "match_result",
            "result": result,
            "my_score": p.best_score,
            "opp_score": opponent.best_score,
            "elo_change": elo_change,
            "opponent_name": opponent.name,
            "opponent_elo": opponent.elo,
            "opponent_player_id": opponent.player_id
        })
    
    m.p1.match = None
    m.p2.match = None
    print(f"  [END] {m.p1.name}({m.p1.elo})={m.p1.best_score} vs {m.p2.name}({m.p2.elo})={m.p2.best_score}")

async def _handle_disconnect(player):
    m = player.match
    if m and not m.ended:
        m.ended = True
        opp = m.p2 if player == m.p1 else m.p1
        
        # Calculate opponent's ELO gain (treat as win)
        K = 32
        expected = 1.0 / (1.0 + 10 ** ((player.elo - opp.elo) / 400.0))
        elo_change = int(round(K * (1.0 - expected)))
        
        opp.elo += elo_change
        opp.elo = max(100, opp.elo)
        
        opp.match = None
        await _send(opp.ws, {
            "type": "opponent_disconnected",
            "elo_change": elo_change,
            "my_score": opp.best_score,
            "opp_score": player.best_score,
            "opponent_name": player.name,
            "opponent_elo": player.elo,
            "opponent_player_id": player.player_id
        })
        print(f"  [DC] {player.name} left match -> {opp.name} wins (+{elo_change} ELO)")

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
                    # Update player info from client data
                    if "name" in data and data["name"]:
                        player.name = data["name"]
                    if "elo" in data:
                        player.elo = data["elo"]
                    if "player_id" in data:
                        player.player_id = data["player_id"]
                    player.in_lobby = True
                    print(f"  [LOBBY] {player.name} (ELO {player.elo}) joined lobby")
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
                    # Update player info on rejoin too
                    if "name" in data and data["name"]:
                        player.name = data["name"]
                    if "elo" in data:
                        player.elo = data["elo"]
                    player.match = None
                    player.best_score = 0
                    player.in_lobby = True
                    print(f"  [LOBBY] {player.name} (ELO {player.elo}) rejoined lobby")
                    await _broadcast_lobby()
                elif t == "update_info":
                    # Allow updating name/elo at any time
                    if "name" in data and data["name"]:
                        player.name = data["name"]
                    if "elo" in data:
                        player.elo = data["elo"]
                    if player.in_lobby:
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

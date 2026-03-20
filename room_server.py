import eventlet
eventlet.monkey_patch()
import os
import random
import string
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "freshplus-room-secret-2024")
CORS(app, origins="*")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1e6,
    logger=False,
    engineio_logger=False,
)

# ── In-memory store ───────────────────────────────────────────────────────────
rooms   = {}
sid_map = {}  # sid -> (room_id, username)

# ── Spam koruması ─────────────────────────────────────────────────────────────
_last_event = {}  # sid -> {"seek": t, "play": t, "pause": t}
SPAM_LIMITS = {"seek": 0.3, "play": 0.2, "pause": 0.2}

def _check_spam(sid, event_name):
    now = time.time()
    if sid not in _last_event:
        _last_event[sid] = {}
    last  = _last_event[sid].get(event_name, 0)
    limit = SPAM_LIMITS.get(event_name, 0.1)
    if now - last < limit:
        return False
    _last_event[sid][event_name] = now
    return True


def gen_room_id():
    while True:
        rid = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if rid not in rooms:
            return rid


def room_info(room):
    return {
        "room_id":          room["room_id"],
        "title":            room["title"],
        "url":              room["url"],
        "poster":           room["poster"],
        "host":             room["host"],
        "users":            list(room["users"].keys()),
        "state":            room["state"],
        "position":         room["position"],
        "position_updated": room["position_updated"],
    }


def current_position(room):
    """Oynatılıyorsa geçen süreyi ekler, negatif değeri engeller."""
    pos = room["position"]
    if room["state"] == "playing":
        elapsed = time.time() - room["position_updated"]
        pos = pos + elapsed
    return max(0.0, pos)


# ── Global Sync Loop ──────────────────────────────────────────────────────────
def _global_sync_loop():
    """
    Her 2 saniyede bir tüm aktif odalara sync_global eventi gönderir.
    Host bağlı olmasa bile client'lar sunucu saatiyle senkron olur.
    """
    while True:
        eventlet.sleep(2.0)
        try:
            now = time.time()
            for room_id, room in list(rooms.items()):
                if not room["users"]:
                    continue
                pos = current_position(room)
                socketio.emit("sync_global", {
                    "position":    pos,
                    "state":       room["state"],
                    "server_time": now,
                }, to=room_id)
        except Exception as e:
            print(f"[SyncLoop] Hata: {e}")

eventlet.spawn(_global_sync_loop)


# ── HTTP Endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "service":     "FreshPlus Room Server v3",
        "rooms":       len(rooms),
        "status":      "ok",
        "server_time": time.time(),
    })


@app.route("/api/room/create", methods=["POST"])
def create_room():
    data   = request.get_json(force=True, silent=True) or {}
    title  = data.get("title",    "").strip()
    url    = data.get("url",      "").strip()
    poster = data.get("poster",   "").strip()
    host   = data.get("username", "").strip()
    if not url or not host:
        return jsonify({"ok": False, "error": "url ve username gerekli"}), 400
    room_id = gen_room_id()
    now     = time.time()
    rooms[room_id] = {
        "room_id": room_id, "title": title, "url": url, "poster": poster,
        "host": host, "users": {}, "state": "paused",
        "position": 0.0, "position_updated": now,
        "messages": [], "created_at": now,
    }
    print(f"[Room] Oluşturuldu: {room_id} | {title[:40]} | Host: {host}")
    return jsonify({"ok": True, "room_id": room_id, "server_time": now})


@app.route("/api/room/<room_id>", methods=["GET"])
def get_room(room_id):
    room = rooms.get(room_id.upper())
    if not room:
        return jsonify({"ok": False, "error": "Oda bulunamadı"}), 404
    info = room_info(room)
    info["server_time"] = time.time()
    return jsonify({"ok": True, "room": info})


@app.route("/api/room/<room_id>/check-username", methods=["POST"])
def check_username(room_id):
    room = rooms.get(room_id.upper())
    if not room:
        return jsonify({"ok": False, "error": "Oda bulunamadı"}), 404
    data     = request.get_json(force=True, silent=True) or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Kullanıcı adı gerekli"}), 400
    if username in room["users"]:
        old_sid = room["users"][username].get("sid")
        if old_sid and old_sid in sid_map:
            return jsonify({"ok": False, "error": "Bu kullanıcı adı alınmış"}), 409
        del room["users"][username]
    return jsonify({"ok": True})


# ── WebSocket Events ───────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print(f"[WS] Bağlandı: {request.sid}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    _last_event.pop(sid, None)
    if sid not in sid_map:
        return
    room_id, username = sid_map.pop(sid)
    room = rooms.get(room_id)
    if room and username in room["users"]:
        del room["users"][username]
        print(f"[WS] Ayrıldı: {username} | Oda: {room_id} | Kalan: {list(room['users'].keys())}")

        # Host ayrıldıysa yeni host ata
        if username == room["host"] and room["users"]:
            new_host = next(iter(room["users"]))
            room["host"] = new_host
            print(f"[Room] Yeni host: {new_host} | Oda: {room_id}")
            emit("host_changed", {
                "new_host":    new_host,
                "server_time": time.time(),
            }, to=room_id)

        emit("user_left", {
            "username":    username,
            "users":       list(room["users"].keys()),
            "host":        room["host"],
            "server_time": time.time(),
        }, to=room_id)

        if not room["users"]:
            del rooms[room_id]
            print(f"[Room] Silindi (boş): {room_id}")


@socketio.on("ping_time")
def on_ping_time(data):
    emit("pong_time", {
        "client_ts":   data.get("client_ts", 0),
        "server_time": time.time(),
    })


@socketio.on("join")
def on_join(data):
    room_id  = (data.get("room_id")  or "").strip().upper()
    username = (data.get("username") or "").strip()
    if not room_id or not username:
        emit("error", {"msg": "room_id ve username gerekli"}); return
    room = rooms.get(room_id)
    if not room:
        emit("error", {"msg": "Oda bulunamadı"}); return

    if username in room["users"]:
        old_sid = room["users"][username].get("sid")
        if old_sid and old_sid in sid_map:
            del sid_map[old_sid]
        del room["users"][username]

    now = time.time()
    room["users"][username] = {"sid": request.sid, "joined_at": now}
    sid_map[request.sid] = (room_id, username)
    join_room(room_id)
    print(f"[WS] Katıldı: {username} | Oda: {room_id} | Toplam: {len(room['users'])}")

    pos = current_position(room)
    emit("joined", {
        "room":        room_info(room),
        "url":         room["url"],
        "messages":    room["messages"][-50:],
        "is_host":     username == room["host"],
        "position":    pos,
        "state":       room["state"],
        "server_time": now,
    })
    emit("user_joined", {
        "username":    username,
        "users":       list(room["users"].keys()),
        "host":        room["host"],
        "server_time": now,
    }, to=room_id)


@socketio.on("play")
def on_play(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]: return
    if not _check_spam(sid, "play"): return

    now = time.time()
    pos = float(data.get("position", current_position(room)))
    room["state"] = "playing"
    room["position"] = pos
    room["position_updated"] = now

    emit("play", {"position": pos, "by": username, "server_time": now}, to=room_id)
    print(f"[Sync] PLAY @{pos:.2f}s | Oda: {room_id}")


@socketio.on("pause")
def on_pause(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]: return
    if not _check_spam(sid, "pause"): return

    now = time.time()
    pos = float(data.get("position", current_position(room)))
    room["state"] = "paused"
    room["position"] = pos
    room["position_updated"] = now

    emit("pause", {"position": pos, "by": username, "server_time": now}, to=room_id)
    print(f"[Sync] PAUSE @{pos:.2f}s | Oda: {room_id}")


@socketio.on("seek")
def on_seek(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]: return
    if not _check_spam(sid, "seek"): return

    now = time.time()
    pos = float(data.get("position", 0))
    room["position"] = pos
    room["position_updated"] = now

    emit("seek", {
        "position": pos, "by": username,
        "server_time": now, "state": room["state"],
    }, to=room_id)
    print(f"[Sync] SEEK @{pos:.2f}s | Oda: {room_id}")


@socketio.on("sync_position")
def on_sync_position(data):
    """Host 1 sn'de bir güncel pozisyonu push eder."""
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]: return

    now   = time.time()
    pos   = float(data.get("position", 0))
    state = data.get("state", room["state"])
    room["position"] = pos
    room["position_updated"] = now
    room["state"] = state

    payload = {"position": pos, "state": state, "server_time": now}
    for uname, udata in list(room["users"].items()):
        if uname != username:
            emit("sync_position", payload, to=udata["sid"])


@socketio.on("request_sync")
def on_request_sync(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return

    now       = time.time()
    pos       = current_position(room)
    host_data = room["users"].get(room["host"])

    if host_data:
        emit("sync_requested", {"by": username, "server_time": now}, to=host_data["sid"])
    else:
        # Host yoksa sunucu authoritative
        emit("sync_position", {
            "position": pos, "state": room["state"], "server_time": now,
        }, to=sid)


@socketio.on("close_room")
def on_close_room(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]: return
    emit("room_closed", {"by": username, "server_time": time.time()}, to=room_id)
    del rooms[room_id]
    print(f"[Room] Kapatıldı: {room_id} | Host: {username}")


@socketio.on("chat")
def on_chat(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return
    text = (data.get("text") or "").strip()
    if not text or len(text) > 500: return
    now = datetime.now().strftime("%H:%M")
    msg = {"user": username, "text": text, "time": now}
    room["messages"].append(msg)
    if len(room["messages"]) > 200:
        room["messages"] = room["messages"][-200:]
    emit("chat", msg, to=room_id)


@socketio.on("poke")
def on_poke(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return
    target = data.get("target")
    if not target or target not in room["users"]: return
    target_sid = room["users"][target]["sid"]
    emit("poked", {"by": username, "server_time": time.time()}, to=target_sid)
    print(f"[Poke] {username} -> {target}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"FreshPlus Room Server v3 -> port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

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
)
# ── In-memory store ───────────────────────────────────────────────────────────
# rooms = {
#   "ABC123": {
#     "room_id": "ABC123",
#     "title": "Film Adı",
#     "url": "https://...m3u8",
#     "poster": "https://...",
#     "host": "kullanici1",
#     "users": {"kullanici1": {"sid": "...", "joined_at": 1234}},
#     "state": "paused",   # playing | paused
#     "position": 0.0,     # saniye
#     "position_updated": 1234,
#     "messages": [{"user": "...", "text": "...", "time": "..."}],
#     "created_at": 1234,
#   }
# }
rooms = {}

# sid → (room_id, username) mapping
sid_map = {}


def gen_room_id():
    while True:
        rid = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if rid not in rooms:
            return rid


def room_info(room):
    return {
        "room_id":  room["room_id"],
        "title":    room["title"],
        "url":      room["url"],
        "poster":   room["poster"],
        "host":     room["host"],
        "users":    list(room["users"].keys()),
        "state":    room["state"],
        "position": room["position"],
    }


# ── HTTP Endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "service": "FreshPlus Room Server",
        "rooms":   len(rooms),
        "status":  "ok"
    })

@app.route("/api/room/create", methods=["POST"])
def create_room():
    data    = request.get_json(force=True, silent=True) or {}
    title   = data.get("title", "").strip()
    url     = data.get("url", "").strip()
    poster  = data.get("poster", "").strip()
    host    = data.get("username", "").strip()

    if not url or not host:
        return jsonify({"ok": False, "error": "url ve username gerekli"}), 400

    room_id = gen_room_id()
    rooms[room_id] = {
        "room_id":          room_id,
        "title":            title,
        "url":              url,
        "poster":           poster,
        "host":             host,
        "users":            {},
        "state":            "paused",
        "position":         0.0,
        "position_updated": time.time(),
        "messages":         [],
        "created_at":       time.time(),
    }
    print(f"[Room] Oluşturuldu: {room_id} | {title[:40]} | Host: {host}")
    return jsonify({"ok": True, "room_id": room_id})


@app.route("/api/room/<room_id>", methods=["GET"])
def get_room(room_id):
    room = rooms.get(room_id.upper())
    if not room:
        return jsonify({"ok": False, "error": "Oda bulunamadı"}), 404
    return jsonify({"ok": True, "room": room_info(room)})


@app.route("/api/room/<room_id>/check-username", methods=["POST"])
def check_username(room_id):
    room = rooms.get(room_id.upper())
    if not room:
        return jsonify({"ok": False, "error": "Oda bulunamadi"}), 404
    data = request.get_json(force=True, silent=True) or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Kullanici adi gerekli"}), 400
    if username in room["users"]:
        # Eski baglanti hala var mi kontrol et
        old_sid = room["users"][username].get("sid")
        if old_sid and old_sid in sid_map:
            # Hala bagli, izin verme
            return jsonify({"ok": False, "error": "Bu kullanici adi alinmis"}), 409
        else:
            # Eski baglanti kopmus, temizle
            del room["users"][username]
    return jsonify({"ok": True})


# ── WebSocket Events ───────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    print(f"[WS] Bağlandı: {request.sid}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    if sid in sid_map:
        room_id, username = sid_map.pop(sid)
        room = rooms.get(room_id)
        if room and username in room["users"]:
            del room["users"][username]
            print(f"[WS] Ayrildi: {username} | Oda: {room_id}")
            emit("user_left", {
                "username": username,
                "users": list(room["users"].keys()),
            }, to=room_id)
            if not room["users"]:
                del rooms[room_id]
                print(f"[Room] Silindi (bos): {room_id}")


@socketio.on("join")
def on_join(data):
    room_id  = (data.get("room_id") or "").strip().upper()
    username = (data.get("username") or "").strip()

    if not room_id or not username:
        emit("error", {"msg": "room_id ve username gerekli"})
        return

    room = rooms.get(room_id)
    if not room:
        emit("error", {"msg": "Oda bulunamadi"})
        return

    # Eski baglanti varsa temizle
    if username in room["users"]:
        old_sid = room["users"][username].get("sid")
        if old_sid and old_sid in sid_map:
            del sid_map[old_sid]
        del room["users"][username]

    # Odaya kayit
    room["users"][username] = {
        "sid":       request.sid,
        "joined_at": time.time(),
    }
    sid_map[request.sid] = (room_id, username)
    join_room(room_id)

    print(f"[WS] Katildi: {username} | Oda: {room_id}")

    emit("joined", {
        "room":     room_info(room),
        "url":      room["url"],
        "messages": room["messages"][-50:],
        "is_host":  username == room["host"],
    })

    emit("user_joined", {
        "username": username,
        "users":    list(room["users"].keys()),
    }, to=room_id)

@socketio.on("play")
def on_play(data):
    """
    data: {room_id, position}
    """
    sid     = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return

    # Sadece host kontrol edebilir
    if username != room["host"]: return

    pos = float(data.get("position", room["position"]))
    room["state"]            = "playing"
    room["position"]         = pos
    room["position_updated"] = time.time()

    emit("play", {"position": pos, "by": username}, to=room_id)
    print(f"[Sync] PLAY @{pos:.1f}s | Oda: {room_id}")


@socketio.on("pause")
def on_pause(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return
    if username != room["host"]: return

    pos = float(data.get("position", room["position"]))
    room["state"]            = "paused"
    room["position"]         = pos
    room["position_updated"] = time.time()

    emit("pause", {"position": pos, "by": username}, to=room_id)
    print(f"[Sync] PAUSE @{pos:.1f}s | Oda: {room_id}")


@socketio.on("seek")
def on_seek(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return
    if username != room["host"]: return

    pos = float(data.get("position", 0))
    room["position"]         = pos
    room["position_updated"] = time.time()

    emit("seek", {"position": pos, "by": username}, to=room_id)
    print(f"[Sync] SEEK @{pos:.1f}s | Oda: {room_id}")



@socketio.on("sync_position")
def on_sync_position(data):
    """Host her 5sn'de pozisyonunu gönderir."""
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return
    if username != room["host"]: return

    pos = float(data.get("position", 0))
    room["position"]         = pos
    room["position_updated"] = time.time()
    room["state"]            = data.get("state", room["state"])

    # Sadece diğer kullanıcılara gönder (host'a değil)
    for uname, udata in room["users"].items():
        if uname != username:
            emit("sync_position", {
                "position": pos,
                "state":    room["state"],
            }, to=udata["sid"])


@socketio.on("close_room")
def on_close_room(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return
    if username != room["host"]: return
    emit("room_closed", {"by": username}, to=room_id)
    del rooms[room_id]
    print(f"[Room] Kapatildi: {room_id} | Host: {username}")


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

    # Son 200 mesajı tut
    if len(room["messages"]) > 200:
        room["messages"] = room["messages"][-200:]

    emit("chat", msg, to=room_id)


@socketio.on("request_sync")
def on_request_sync(data):
    sid = request.sid
    if sid not in sid_map: return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room: return

    host_data = room["users"].get(room["host"])
    if host_data:
        # Host'a sync isteği gönder
        emit("sync_requested", {"by": username}, to=host_data["sid"])
    else:
        # Host yoksa room state'inden gönder
        emit("sync_position", {
            "position": room["position"],
            "state":    room["state"],
        }, to=sid)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"FreshPlus Room Server -> port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

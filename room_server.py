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
# rooms = {
#   "ABC123": {
#     "room_id", "title", "url", "poster", "host",
#     "users": {username: {"sid", "joined_at"}},
#     "state": "paused"|"playing",
#     "position": float (saniye),
#     "position_updated": float (time.time()),
#     "messages": [...],
#     "created_at": float,
#   }
# }
rooms   = {}
sid_map = {}  # sid -> (room_id, username)


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
    """
    Oda kaydedilmiş pozisyonunu, eğer oynatılıyorsa geçen süreyi
    ekleyerek anlık tahmini pozisyon döner.
    """
    pos = room["position"]
    if room["state"] == "playing":
        elapsed = time.time() - room["position_updated"]
        pos = pos + elapsed
    return max(0.0, pos)


# ── HTTP Endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "service":     "FreshPlus Room Server v2",
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
        "room_id":          room_id,
        "title":            title,
        "url":              url,
        "poster":           poster,
        "host":             host,
        "users":            {},
        "state":            "paused",
        "position":         0.0,
        "position_updated": now,
        "messages":         [],
        "created_at":       now,
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
    if sid not in sid_map:
        return
    room_id, username = sid_map.pop(sid)
    room = rooms.get(room_id)
    if room and username in room["users"]:
        del room["users"][username]
        print(f"[WS] Ayrıldı: {username} | Oda: {room_id} | Kalan: {list(room['users'].keys())}")
        emit("user_left", {
            "username":    username,
            "users":       list(room["users"].keys()),
            "server_time": time.time(),
        }, to=room_id)
        if not room["users"]:
            del rooms[room_id]
            print(f"[Room] Silindi (boş): {room_id}")


@socketio.on("ping_time")
def on_ping_time(data):
    """
    İstemci RTT (round-trip time) ölçümü için kullanır.
    İstemci kendi timestamp'ini gönderir, sunucu server_time ekleyerek
    hemen geri döner. İstemci farktan RTT ve clock offset hesaplar.
    """
    emit("pong_time", {
        "client_ts":   data.get("client_ts", 0),
        "server_time": time.time(),
    })


@socketio.on("join")
def on_join(data):
    room_id  = (data.get("room_id")  or "").strip().upper()
    username = (data.get("username") or "").strip()

    if not room_id or not username:
        emit("error", {"msg": "room_id ve username gerekli"})
        return

    room = rooms.get(room_id)
    if not room:
        emit("error", {"msg": "Oda bulunamadı"})
        return

    # Eski bağlantı varsa temizle
    if username in room["users"]:
        old_sid = room["users"][username].get("sid")
        if old_sid and old_sid in sid_map:
            del sid_map[old_sid]
        del room["users"][username]

    now = time.time()
    room["users"][username] = {
        "sid":       request.sid,
        "joined_at": now,
    }
    sid_map[request.sid] = (room_id, username)
    join_room(room_id)

    print(f"[WS] Katıldı: {username} | Oda: {room_id} | Toplam: {len(room['users'])}")

    # Anlık hesaplanmış pozisyon
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

    # Odadaki herkese yeni kullanıcı bildir
    emit("user_joined", {
        "username":    username,
        "users":       list(room["users"].keys()),
        "server_time": now,
    }, to=room_id)


@socketio.on("play")
def on_play(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return

    now = time.time()
    pos = float(data.get("position", current_position(room)))
    room["state"]            = "playing"
    room["position"]         = pos
    room["position_updated"] = now

    emit("play", {
        "position":    pos,
        "by":          username,
        "server_time": now,
    }, to=room_id)
    print(f"[Sync] PLAY @{pos:.2f}s | Oda: {room_id}")


@socketio.on("pause")
def on_pause(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return

    now = time.time()
    pos = float(data.get("position", current_position(room)))
    room["state"]            = "paused"
    room["position"]         = pos
    room["position_updated"] = now

    emit("pause", {
        "position":    pos,
        "by":          username,
        "server_time": now,
    }, to=room_id)
    print(f"[Sync] PAUSE @{pos:.2f}s | Oda: {room_id}")


@socketio.on("seek")
def on_seek(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return

    now = time.time()
    pos = float(data.get("position", 0))
    room["position"]         = pos
    room["position_updated"] = now

    emit("seek", {
        "position":    pos,
        "by":          username,
        "server_time": now,
        "state":       room["state"],
    }, to=room_id)
    print(f"[Sync] SEEK @{pos:.2f}s | Oda: {room_id}")


@socketio.on("sync_position")
def on_sync_position(data):
    """
    Host 3 sn'de bir güncel pozisyonu push eder.
    Sunucu hem room state'i günceller hem de sadece izleyicilere iletir.
    """
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return

    now   = time.time()
    pos   = float(data.get("position", 0))
    state = data.get("state", room["state"])

    room["position"]         = pos
    room["position_updated"] = now
    room["state"]            = state

    payload = {
        "position":    pos,
        "state":       state,
        "server_time": now,
    }

    for uname, udata in list(room["users"].items()):
        if uname != username:
            emit("sync_position", payload, to=udata["sid"])


@socketio.on("request_sync")
def on_request_sync(data):
    """
    İzleyici bağlandığında host'tan anlık durum ister.
    Host yoksa sunucu anlık hesaplanmış pozisyonla cevap verir.
    """
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room:
        return

    now       = time.time()
    pos       = current_position(room)
    host_data = room["users"].get(room["host"])

    if host_data:
        emit("sync_requested", {
            "by":          username,
            "server_time": now,
        }, to=host_data["sid"])
    else:
        emit("sync_position", {
            "position":    pos,
            "state":       room["state"],
            "server_time": now,
        }, to=sid)


@socketio.on("close_room")
def on_close_room(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return

    emit("room_closed", {
        "by":          username,
        "server_time": time.time(),
    }, to=room_id)
    del rooms[room_id]
    print(f"[Room] Kapatıldı: {room_id} | Host: {username}")


@socketio.on("chat")
def on_chat(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room:
        return

    text = (data.get("text") or "").strip()
    if not text or len(text) > 500:
        return

    now = datetime.now().strftime("%H:%M")
    msg = {"user": username, "text": text, "time": now}
    room["messages"].append(msg)
    if len(room["messages"]) > 200:
        room["messages"] = room["messages"][-200:]

    emit("chat", msg, to=room_id)


@socketio.on("poke")
def on_poke(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room:
        return
    target = data.get("target")
    if not target or target not in room["users"]:
        return
    target_sid = room["users"][target]["sid"]
    emit("poked", {"by": username, "server_time": time.time()}, to=target_sid)
    print(f"[Poke] {username} -> {target}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"FreshPlus Room Server v2 -> port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

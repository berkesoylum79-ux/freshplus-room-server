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
rooms   = {}   # room_id -> room dict
sid_map = {}   # sid -> (room_id, username)

# ── Spam koruması ─────────────────────────────────────────────────────────────
_last_event  = {}   # sid -> {event_name: timestamp}
SPAM_LIMITS  = {"seek": 0.15, "play": 0.1, "pause": 0.1}

def _check_spam(sid, event_name):
    now   = time.time()
    bucket = _last_event.setdefault(sid, {})
    limit  = SPAM_LIMITS.get(event_name, 0.1)
    if now - bucket.get(event_name, 0) < limit:
        return False
    bucket[event_name] = now
    return True


def _gen_room_id():
    while True:
        rid = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if rid not in rooms:
            return rid


def _room_info(room):
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
        "seq":              room["seq"],
    }


def _current_position(room, now=None):
    """
    Odanın anlık pozisyonunu döndürür.
    now parametresi verilerek timestamp tutarsızlığı (S-9) önlenir:
    hem server_time hem position aynı 'now' ile hesaplanır.
    """
    if now is None:
        now = time.time()
    pos = room["position"]
    if room["state"] == "playing":
        pos += max(0.0, now - room["position_updated"])
    return max(0.0, pos)


def _advance_seq(room):
    """Her state değişikliğinde sıra numarasını artır (S-16)."""
    room["seq"] += 1
    return room["seq"]


# ── Oda bazlı sync greenlet ───────────────────────────────────────────────────
SYNC_INTERVAL = 0.5   # saniye (eskisi 2.0 — S-1 çözümü: daha sık, tek kaynak)

def _room_sync_loop(room_id):
    """
    Her odaya özel greenlet.
    Oda boşalınca veya silinince otomatik durur.
    Boş oda için CPU harcanmaz (S-13 çözümü).
    """
    while True:
        eventlet.sleep(SYNC_INTERVAL)
        room = rooms.get(room_id)
        if not room:
            return                      # oda silinmiş, loop biter
        if not room["users"]:
            continue                    # geçici boş — host yokken bile bekle

        now = time.time()               # S-9: tek time.time() çağrısı
        pos = _current_position(room, now)

        try:
            # S-17: sadece non-host kullanıcılara gönder.
            # Host kendi pozisyonunu zaten biliyor; kendine sync göndermek
            # "paused" state'inde spurious seek tetikleyebilir.
            host_sid = room["users"].get(room["host"], {}).get("sid")
            for uname, udata in list(room["users"].items()):
                if udata["sid"] == host_sid:
                    continue
                socketio.emit("sync_global", {
                    "position":    pos,
                    "state":       room["state"],
                    "server_time": now,     # S-9: pozisyon ve server_time aynı now
                    "seq":         room["seq"],
                }, to=udata["sid"])
        except Exception as e:
            print(f"[SyncLoop:{room_id}] Hata: {e}")


# ── HTTP Endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "service":     "FreshPlus Room Server v4",
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

    room_id = _gen_room_id()
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
        "seq":              0,          # S-16: event sıra numarası
    }

    # Oda bazlı sync greenlet başlat (S-13 çözümü)
    eventlet.spawn(_room_sync_loop, room_id)

    print(f"[Room] Oluşturuldu: {room_id} | {title[:40]} | Host: {host}")
    return jsonify({"ok": True, "room_id": room_id, "server_time": now})


@app.route("/api/room/<room_id>", methods=["GET"])
def get_room(room_id):
    room = rooms.get(room_id.upper())
    if not room:
        return jsonify({"ok": False, "error": "Oda bulunamadı"}), 404
    now  = time.time()
    info = _room_info(room)
    info["server_time"] = now
    info["position"]    = _current_position(room, now)
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
    if not room:
        return

    if username not in room["users"]:
        return

    del room["users"][username]
    print(f"[WS] Ayrıldı: {username} | Oda: {room_id} | Kalan: {list(room['users'].keys())}")

    now = time.time()

    # Host ayrıldıysa yeni host ata
    if username == room["host"] and room["users"]:
        new_host = next(iter(room["users"]))
        room["host"] = new_host
        _advance_seq(room)
        print(f"[Room] Yeni host: {new_host} | Oda: {room_id}")
        socketio.emit("host_changed", {
            "new_host":    new_host,
            "server_time": now,
            "seq":         room["seq"],
        }, to=room_id)

    socketio.emit("user_left", {
        "username":    username,
        "users":       list(room["users"].keys()),
        "host":        room["host"],
        "server_time": now,
    }, to=room_id)

    if not room["users"]:
        del rooms[room_id]
        print(f"[Room] Silindi (boş): {room_id}")
        # Greenlet rooms.get(room_id) None görünce kendiliğinden durur


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
        emit("error", {"msg": "room_id ve username gerekli"})
        return

    room = rooms.get(room_id)
    if not room:
        emit("error", {"msg": "Oda bulunamadı"})
        return

    # Eski bağlantıyı temizle
    if username in room["users"]:
        old_sid = room["users"][username].get("sid")
        if old_sid and old_sid in sid_map:
            del sid_map[old_sid]
        del room["users"][username]

    now = time.time()
    room["users"][username] = {"sid": request.sid, "joined_at": now}
    sid_map[request.sid]    = (room_id, username)
    join_room(room_id)

    print(f"[WS] Katıldı: {username} | Oda: {room_id} | Toplam: {len(room['users'])}")

    pos = _current_position(room, now)

    emit("joined", {
        "room":        _room_info(room),
        "url":         room["url"],
        "messages":    room["messages"][-50:],
        "is_host":     username == room["host"],
        "position":    pos,
        "state":       room["state"],
        "server_time": now,
        "seq":         room["seq"],
    })

    # Diğer kullanıcılara bildir
    socketio.emit("user_joined", {
        "username":    username,
        "users":       list(room["users"].keys()),
        "host":        room["host"],
        "server_time": now,
    }, to=room_id, skip_sid=request.sid)


@socketio.on("play")
def on_play(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return
    if not _check_spam(sid, "play"):
        return

    now  = time.time()
    pos  = float(data.get("position", _current_position(room, now)))
    seq  = _advance_seq(room)

    room["state"]            = "playing"
    room["position"]         = pos
    room["position_updated"] = now

    # S-17: host'u dahil etme (skip_sid), izleyicilere anında gönder
    host_sid = room["users"].get(username, {}).get("sid")
    payload  = {"position": pos, "by": username, "server_time": now, "seq": seq}
    socketio.emit("play", payload, to=room_id, skip_sid=host_sid)
    print(f"[Sync] PLAY @{pos:.2f}s | seq={seq} | Oda: {room_id}")


@socketio.on("pause")
def on_pause(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return
    if not _check_spam(sid, "pause"):
        return

    now  = time.time()
    pos  = float(data.get("position", _current_position(room, now)))
    seq  = _advance_seq(room)

    room["state"]            = "paused"
    room["position"]         = pos
    room["position_updated"] = now

    host_sid = room["users"].get(username, {}).get("sid")
    payload  = {"position": pos, "by": username, "server_time": now, "seq": seq}
    socketio.emit("pause", payload, to=room_id, skip_sid=host_sid)
    print(f"[Sync] PAUSE @{pos:.2f}s | seq={seq} | Oda: {room_id}")


@socketio.on("seek")
def on_seek(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return
    if not _check_spam(sid, "seek"):
        return

    now  = time.time()
    pos  = float(data.get("position", 0))
    seq  = _advance_seq(room)

    room["position"]         = pos
    room["position_updated"] = now
    # state değişmez, sadece pozisyon sıfırlanır

    host_sid = room["users"].get(username, {}).get("sid")
    payload  = {
        "position":    pos,
        "by":          username,
        "server_time": now,
        "state":       room["state"],
        "seq":         seq,
    }
    socketio.emit("seek", payload, to=room_id, skip_sid=host_sid)
    print(f"[Sync] SEEK @{pos:.2f}s | seq={seq} | state={room['state']} | Oda: {room_id}")


# sync_position KALDIRILDI (S-1 ve S-13 çözümü).
# Host pozisyon push'u yoktu; sync_global (500ms, sunucu) tek kaynak.


@socketio.on("request_sync")
def on_request_sync(data):
    """
    Reconnect sonrası izleyici joined event üzerinden initial state alır.
    Bu handler geriye dönük uyumluluk için kalıyor ama artık
    joined üzerinden gelen verilerle sync_global yeterli.
    """
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room:
        return

    now = time.time()
    pos = _current_position(room, now)
    emit("sync_global", {
        "position":    pos,
        "state":       room["state"],
        "server_time": now,
        "seq":         room["seq"],
    })


@socketio.on("close_room")
def on_close_room(data):
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return

    now = time.time()
    socketio.emit("room_closed", {"by": username, "server_time": now}, to=room_id)
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

    socketio.emit("chat", msg, to=room_id)


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
    socketio.emit("poked", {"by": username, "server_time": time.time()}, to=target_sid)
    print(f"[Poke] {username} -> {target}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"FreshPlus Room Server v4 -> port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

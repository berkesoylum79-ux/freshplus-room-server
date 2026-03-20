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
_last_event = {}
SPAM_LIMITS = {
    "seek":               0.08,   # max 12 Hz
    "play":               0.08,
    "pause":              0.08,
    "position_heartbeat": 0.45,   # max ~2 Hz
}

# ── Sync döngüsü hızı ─────────────────────────────────────────────────────────
# 5 Hz = 200ms.  İstemci 100ms dead-zone ile çalışır → en fazla 100ms steady drift.
SYNC_INTERVAL = 0.2


def _check_spam(sid, event_name):
    now    = time.time()
    bucket = _last_event.setdefault(sid, {})
    limit  = SPAM_LIMITS.get(event_name, 0.08)
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
    """Oda bilgilerini özet dict olarak döndür."""
    return {
        "room_id":      room["room_id"],
        "title":        room["title"],
        "url":          room["url"],
        "poster":       room["poster"],
        "host":         room["host"],
        "users":        list(room["users"].keys()),
        "state":        room["state"],
        "ref_position": room["ref_position"],   # Referans pozisyon (saniye)
        "ref_time":     room["ref_time"],        # Referans zaman (unix ts, server)
        "seq":          room["seq"],
    }


def _current_position(room, now=None):
    """
    Referans noktasından anlık pozisyonu hesapla.

    Tasarım: Sunucu pozisyonu hiçbir zaman 'oynatarak hesaplamaz'.
    Bunun yerine (ref_position, ref_time) çiftini saklar.
    ref_time anından bu yana geçen süre kadar ilerleme varsayılır (state=playing ise).
    İstemci de aynı formülü kullanır — böylece ikisi her zaman aynı noktada buluşur.
    """
    if now is None:
        now = time.time()
    pos = room["ref_position"]
    if room["state"] == "playing":
        pos += max(0.0, now - room["ref_time"])
    return max(0.0, pos)


def _advance_seq(room):
    """Sıra numarasını artır. Stale event filtrelemesi için (S-16)."""
    room["seq"] += 1
    return room["seq"]


# ── Oda bazlı sync greenlet ───────────────────────────────────────────────────
def _room_sync_loop(room_id):
    """
    Oda bazlı 5 Hz (200ms) sync döngüsü.

    Sadece host dışındaki kullanıcılara gönderilir (S-17).
    Oda boşalınca ya da silinince otomatik durur.

    Önemli tasarım kararı:
    ref_position + ref_time gönderilir, HESAPLANMIŞ pozisyon değil.
    İstemci, kendi clock_offset'ini kullanarak hedef pozisyonu hesaplar.
    Bu şekilde sunucu ve istemci birbirinden bağımsız ama tutarlı hesap yapar.
    """
    while True:
        eventlet.sleep(SYNC_INTERVAL)
        room = rooms.get(room_id)
        if not room:
            return           # oda silinmiş, loop biter
        if not room["users"]:
            continue         # geçici boş — bekle

        now      = time.time()
        host_sid = room["users"].get(room["host"], {}).get("sid")

        payload = {
            "ref_position": room["ref_position"],
            "ref_time":     room["ref_time"],
            "state":        room["state"],
            "server_time":  now,
            "seq":          room["seq"],
        }

        try:
            for uname, udata in list(room["users"].items()):
                if udata["sid"] != host_sid:
                    socketio.emit("sync_global", payload, to=udata["sid"])
        except Exception as exc:
            print(f"[SyncLoop:{room_id}] Hata: {exc}")


# ── HTTP Endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({
        "service":     "FreshPlus Room Server v5",
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
        "room_id":      room_id,
        "title":        title,
        "url":          url,
        "poster":       poster,
        "host":         host,
        "users":        {},
        "state":        "paused",
        "ref_position": 0.0,    # saniye — referans pozisyon
        "ref_time":     now,    # unix timestamp — ref_position'ın kaydedildiği an
        "messages":     [],
        "created_at":   now,
        "seq":          0,
    }

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
    info["position"]    = _current_position(room, now)   # eski istemciler için
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
    if not room or username not in room["users"]:
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


@socketio.on("ping_time")
def on_ping_time(data):
    """Clock sync — istemci RTT ve offset hesaplar."""
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

    # Stale bağlantıyı temizle (reconnect senaryosu)
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
        "room":         _room_info(room),
        "url":          room["url"],
        "messages":     room["messages"][-50:],
        "is_host":      username == room["host"],
        # Yeni referans nokta formatı
        "ref_position": room["ref_position"],
        "ref_time":     room["ref_time"],
        # Eski istemciler için
        "position":     pos,
        "state":        room["state"],
        "server_time":  now,
        "seq":          room["seq"],
    })

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

    now = time.time()
    pos = float(data.get("position", _current_position(room, now)))
    seq = _advance_seq(room)

    # Referans noktasını güncelle
    room["state"]        = "playing"
    room["ref_position"] = pos
    room["ref_time"]     = now

    host_sid = room["users"].get(username, {}).get("sid")
    payload  = {
        "ref_position": pos,
        "ref_time":     now,
        "server_time":  now,
        "seq":          seq,
    }
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

    now = time.time()
    pos = float(data.get("position", _current_position(room, now)))
    seq = _advance_seq(room)

    room["state"]        = "paused"
    room["ref_position"] = pos
    room["ref_time"]     = now

    host_sid = room["users"].get(username, {}).get("sid")
    payload  = {
        "ref_position": pos,
        "ref_time":     now,
        "server_time":  now,
        "seq":          seq,
    }
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

    now = time.time()
    pos = float(data.get("position", 0.0))
    seq = _advance_seq(room)

    # Seek sadece pozisyonu sıfırlar, state değişmez
    room["ref_position"] = pos
    room["ref_time"]     = now

    host_sid = room["users"].get(username, {}).get("sid")
    payload  = {
        "ref_position": pos,
        "ref_time":     now,
        "state":        room["state"],
        "server_time":  now,
        "seq":          seq,
    }
    socketio.emit("seek", payload, to=room_id, skip_sid=host_sid)
    print(f"[Sync] SEEK @{pos:.2f}s | seq={seq} | state={room['state']} | Oda: {room_id}")


@socketio.on("position_heartbeat")
def on_position_heartbeat(data):
    """
    Host oynatırken 2 saniyede bir gerçek pozisyonu gönderir.
    Sunucunun ref_position tahminini düzeltir — uzun süreli drift'i önler.
    Sadece playing state'inde anlamlı.
    """
    sid = request.sid
    if sid not in sid_map:
        return
    if not _check_spam(sid, "position_heartbeat"):
        return

    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room or username != room["host"]:
        return
    if room["state"] != "playing":
        return

    now = time.time()
    pos = float(data.get("position", 0.0))

    # Referans noktasını yenile — hesap hatası birikmesini önler
    room["ref_position"] = pos
    room["ref_time"]     = now


@socketio.on("request_sync")
def on_request_sync(data):
    """Reconnect sonrası istemci bu event ile anlık durumu ister."""
    sid = request.sid
    if sid not in sid_map:
        return
    room_id, username = sid_map[sid]
    room = rooms.get(room_id)
    if not room:
        return

    now = time.time()
    emit("sync_global", {
        "ref_position": room["ref_position"],
        "ref_time":     room["ref_time"],
        "state":        room["state"],
        "server_time":  now,
        "seq":          room["seq"],
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

    now_str = datetime.now().strftime("%H:%M")
    msg     = {"user": username, "text": text, "time": now_str}
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
    print(f"FreshPlus Room Server v5 -> port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

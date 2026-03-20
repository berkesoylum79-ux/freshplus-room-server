import eventlet
eventlet.monkey_patch()
import os, random, string, time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room
from flask_cors import CORS

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "freshplus-room-secret-2024")
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet",
    ping_timeout=60, ping_interval=25, max_http_buffer_size=1e6,
    logger=False, engineio_logger=False)

rooms   = {}
sid_map = {}
_last_event = {}
SPAM_LIMITS = {"seek": 0.15, "play": 0.1, "pause": 0.1}

def _spam(sid, ev):
    now = time.time()
    b   = _last_event.setdefault(sid, {})
    if now - b.get(ev, 0) < SPAM_LIMITS.get(ev, 0.1): return False
    b[ev] = now; return True

def _gen():
    while True:
        r = "".join(random.choices(string.ascii_uppercase+string.digits, k=6))
        if r not in rooms: return r

def _info(room):
    return {"room_id": room["room_id"], "title": room["title"], "url": room["url"],
            "poster": room["poster"], "host": room["host"],
            "users": list(room["users"].keys()), "state": room["state"],
            "position": room["position"], "position_updated": room["position_updated"],
            "seq": room["seq"]}

def _pos(room, now=None):
    if now is None: now = time.time()
    p = room["position"]
    if room["state"] == "playing": p += max(0.0, now - room["position_updated"])
    return max(0.0, p)

def _seq(room):
    room["seq"] += 1; return room["seq"]

SYNC_INTERVAL = 0.5

def _sync_loop(room_id):
    while True:
        eventlet.sleep(SYNC_INTERVAL)
        room = rooms.get(room_id)
        if not room: return
        if not room["users"]: continue
        now = time.time()
        pos = _pos(room, now)
        host_sid = room["users"].get(room["host"], {}).get("sid")
        try:
            for uname, ud in list(room["users"].items()):
                if ud["sid"] == host_sid: continue
                socketio.emit("sync_global", {"position": pos, "state": room["state"],
                    "server_time": now, "seq": room["seq"]}, to=ud["sid"])
        except Exception as e:
            print(f"[SyncLoop:{room_id}] {e}")

@app.route("/")
def index():
    return jsonify({"service":"FreshPlus Room Server v4","rooms":len(rooms),"status":"ok","server_time":time.time()})

@app.route("/api/room/create", methods=["POST"])
def create_room():
    d = request.get_json(force=True, silent=True) or {}
    url = d.get("url","").strip(); host = d.get("username","").strip()
    if not url or not host: return jsonify({"ok":False,"error":"url ve username gerekli"}),400
    rid = _gen(); now = time.time()
    rooms[rid] = {"room_id":rid,"title":d.get("title","").strip(),
        "url":url,"poster":d.get("poster","").strip(),"host":host,"users":{},
        "state":"paused","position":0.0,"position_updated":now,
        "messages":[],"created_at":now,"seq":0}
    eventlet.spawn(_sync_loop, rid)
    print(f"[Room] Oluşturuldu: {rid} | Host: {host}")
    return jsonify({"ok":True,"room_id":rid,"server_time":now})

@app.route("/api/room/<room_id>", methods=["GET"])
def get_room(room_id):
    room = rooms.get(room_id.upper())
    if not room: return jsonify({"ok":False,"error":"Oda bulunamadı"}),404
    now = time.time(); info = _info(room)
    info["server_time"] = now; info["position"] = _pos(room, now)
    return jsonify({"ok":True,"room":info})

@app.route("/api/room/<room_id>/check-username", methods=["POST"])
def check_username(room_id):
    room = rooms.get(room_id.upper())
    if not room: return jsonify({"ok":False,"error":"Oda bulunamadı"}),404
    d = request.get_json(force=True, silent=True) or {}
    u = d.get("username","").strip()
    if not u: return jsonify({"ok":False,"error":"Kullanıcı adı gerekli"}),400
    if u in room["users"]:
        old = room["users"][u].get("sid")
        if old and old in sid_map: return jsonify({"ok":False,"error":"Bu kullanıcı adı alınmış"}),409
        del room["users"][u]
    return jsonify({"ok":True})

@socketio.on("connect")
def on_connect(): print(f"[WS] Bağlandı: {request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid; _last_event.pop(sid, None)
    if sid not in sid_map: return
    rid, u = sid_map.pop(sid); room = rooms.get(rid)
    if not room or u not in room["users"]: return
    del room["users"][u]; now = time.time()
    print(f"[WS] Ayrıldı: {u} | Oda: {rid}")
    if u == room["host"] and room["users"]:
        nh = next(iter(room["users"])); room["host"] = nh; _seq(room)
        socketio.emit("host_changed",{"new_host":nh,"server_time":now,"seq":room["seq"]},to=rid)
    socketio.emit("user_left",{"username":u,"users":list(room["users"].keys()),
        "host":room["host"],"server_time":now},to=rid)
    if not room["users"]: del rooms[rid]; print(f"[Room] Silindi: {rid}")

@socketio.on("ping_time")
def on_ping(d): emit("pong_time",{"client_ts":d.get("client_ts",0),"server_time":time.time()})

@socketio.on("join")
def on_join(data):
    rid = (data.get("room_id") or "").strip().upper()
    u   = (data.get("username") or "").strip()
    if not rid or not u: emit("error",{"msg":"room_id ve username gerekli"}); return
    room = rooms.get(rid)
    if not room: emit("error",{"msg":"Oda bulunamadı"}); return
    if u in room["users"]:
        old = room["users"][u].get("sid")
        if old and old in sid_map: del sid_map[old]
        del room["users"][u]
    now = time.time()
    room["users"][u] = {"sid":request.sid,"joined_at":now}
    sid_map[request.sid] = (rid, u); join_room(rid)
    print(f"[WS] Katıldı: {u} | {rid} | {len(room['users'])} kişi")
    pos = _pos(room, now)
    emit("joined",{"room":_info(room),"url":room["url"],"messages":room["messages"][-50:],
        "is_host":u==room["host"],"position":pos,"state":room["state"],
        "server_time":now,"seq":room["seq"]})
    socketio.emit("user_joined",{"username":u,"users":list(room["users"].keys()),
        "host":room["host"],"server_time":now},to=rid,skip_sid=request.sid)

@socketio.on("play")
def on_play(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, u = sid_map[sid]; room = rooms.get(rid)
    if not room or u != room["host"] or not _spam(sid,"play"): return
    now = time.time(); pos = float(data.get("position", _pos(room,now))); s = _seq(room)
    room["state"]="playing"; room["position"]=pos; room["position_updated"]=now
    host_sid = room["users"].get(u,{}).get("sid")
    socketio.emit("play",{"position":pos,"by":u,"server_time":now,"seq":s},to=rid,skip_sid=host_sid)
    print(f"[Sync] PLAY @{pos:.2f}s seq={s} | {rid}")

@socketio.on("pause")
def on_pause(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, u = sid_map[sid]; room = rooms.get(rid)
    if not room or u != room["host"] or not _spam(sid,"pause"): return
    now = time.time(); pos = float(data.get("position", _pos(room,now))); s = _seq(room)
    room["state"]="paused"; room["position"]=pos; room["position_updated"]=now
    host_sid = room["users"].get(u,{}).get("sid")
    socketio.emit("pause",{"position":pos,"by":u,"server_time":now,"seq":s},to=rid,skip_sid=host_sid)
    print(f"[Sync] PAUSE @{pos:.2f}s seq={s} | {rid}")

@socketio.on("seek")
def on_seek(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, u = sid_map[sid]; room = rooms.get(rid)
    if not room or u != room["host"] or not _spam(sid,"seek"): return
    now = time.time(); pos = float(data.get("position",0)); s = _seq(room)
    room["position"]=pos; room["position_updated"]=now
    host_sid = room["users"].get(u,{}).get("sid")
    socketio.emit("seek",{"position":pos,"by":u,"server_time":now,"state":room["state"],"seq":s},
        to=rid,skip_sid=host_sid)
    print(f"[Sync] SEEK @{pos:.2f}s seq={s} | {rid}")

@socketio.on("request_sync")
def on_req_sync(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, _ = sid_map[sid]; room = rooms.get(rid)
    if not room: return
    now = time.time()
    emit("sync_global",{"position":_pos(room,now),"state":room["state"],"server_time":now,"seq":room["seq"]})

@socketio.on("close_room")
def on_close(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, u = sid_map[sid]; room = rooms.get(rid)
    if not room or u != room["host"]: return
    socketio.emit("room_closed",{"by":u,"server_time":time.time()},to=rid)
    del rooms[rid]; print(f"[Room] Kapatıldı: {rid}")

@socketio.on("chat")
def on_chat(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, u = sid_map[sid]; room = rooms.get(rid)
    if not room: return
    text = (data.get("text") or "").strip()
    if not text or len(text) > 500: return
    msg = {"user":u,"text":text,"time":datetime.now().strftime("%H:%M")}
    room["messages"].append(msg)
    if len(room["messages"]) > 200: room["messages"] = room["messages"][-200:]
    socketio.emit("chat", msg, to=rid)

@socketio.on("poke")
def on_poke(data):
    sid = request.sid
    if sid not in sid_map: return
    rid, u = sid_map[sid]; room = rooms.get(rid)
    if not room: return
    t = data.get("target")
    if not t or t not in room["users"]: return
    socketio.emit("poked",{"by":u,"server_time":time.time()},to=room["users"][t]["sid"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"FreshPlus Room Server v4 -> port {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

#!/usr/bin/env python3
"""AI Launcher — Mobile-friendly terminal launcher for Claude / Codex on a Linux server.

Requires: flask, flask-sock, tmux
Install : pip install flask flask-sock
Run     : python3 app.py
"""

import os, signal, subprocess, time, json, threading, uuid, pty, select, struct, fcntl, termios
from pathlib import Path
from functools import wraps
from flask import Flask, request, session as fs, redirect, jsonify, render_template_string
from flask_sock import Sock

# ── Config ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
sock = Sock(app)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")   # set via env var in production
META_FILE  = Path(os.environ.get("META_FILE", "/opt/ai-launcher/sessions.json"))
SHORTCUTS_FILE = Path(os.environ.get("SHORTCUTS_FILE", "/opt/ai-launcher/shortcuts.json"))
SC_COLORS  = ["#e8703a","#7c6af7","#2e86de","#e74c3c","#f39c12","#8e44ad","#16a085","#d35400","#2980b9","#1abc9c"]
SC_USERS   = ["root"]   # extend with your unix users, e.g. ["root", "alice"]

def _load_shortcuts():
    try:
        return json.loads(SHORTCUTS_FILE.read_text())
    except Exception:
        return []

def _save_shortcuts(data):
    SHORTCUTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── Tool definitions ──────────────────────────────────────────────────────────────
# Each tool appears as a button on the home screen.
# "cmd"    : command to run in a new tmux session
# "resume" : command to attach to an existing Claude conversation (-r flag)
# "user"   : unix user to run as (non-root uses `su - <user>`)
TOOLS = [
    {"id": "claude",  "label": "Claude",  "desc": "Claude AI assistant", "color": "#4f86c6",
     "cmd": "claude",  "resume": "claude -r", "user": "root"},
    # Add more tools here, e.g.:
    # {"id": "codex", "label": "Codex", "desc": "OpenAI Codex",  "color": "#e08c3a",
    #  "cmd": "/path/to/codex", "resume": "/path/to/codex", "user": "root"},
    {"id": "shell",  "label": "Terminal", "desc": "Open a plain Bash shell", "color": "#607d8b",
     "cmd": "bash",    "resume": "bash",      "user": "root"},
]
TOOL_MAP = {t["id"]: t for t in TOOLS}

# ── Session metadata persistence ─────────────────────────────────────────────────
def _load_meta():
    try:
        return json.loads(META_FILE.read_text())
    except Exception:
        return {}

def _save_meta(m):
    try:
        META_FILE.write_text(json.dumps(m, indent=2))
    except Exception:
        pass

# ── tmux helpers ─────────────────────────────────────────────────────────────────
def _tmux_sessions():
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    return {s for s in r.stdout.strip().split("\n") if s}

def _tmux_create(name, cmd_str, cwd="/root", cols=80, rows=24):
    env = {**os.environ, "TERM": "xterm-256color"}
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name,
         "-x", str(cols), "-y", str(rows), "-c", cwd, cmd_str],
        check=True, env=env
    )
    subprocess.run(["tmux", "set-option", "-t", name, "status", "off"],
                   capture_output=True)

def _tmux_kill(name):
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)

def _tmux_attach_pty(name, rows=24, cols=80):
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color"}
    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", name],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True, preexec_fn=os.setsid, env=env,
    )
    os.close(slave_fd)
    return master_fd, proc

def _build_cmd(tool_id=None, resume=False, custom=None):
    if custom:
        return custom
    t = TOOL_MAP[tool_id]
    raw = t["resume"] if resume else t["cmd"]
    if t["user"] == "root":
        return raw
    return f"su - {t['user']} -c '{raw}'"

def _create_session(label, color, cmd_str, tool_id=None, cols=80, rows=24):
    name = f"ai-{uuid.uuid4().hex[:8]}"
    if tool_id and TOOL_MAP.get(tool_id, {}).get("user", "root") != "root":
        cwd = f"/home/{TOOL_MAP[tool_id]['user']}"
    else:
        cwd = "/root"
    _tmux_create(name, cmd_str, cwd, cols=cols, rows=rows)
    meta = _load_meta()
    meta[name] = {
        "name": name, "label": label, "color": color,
        "tool_id": tool_id, "created_at": int(time.time()),
    }
    _save_meta(meta)
    return name

# ── Auth ─────────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not fs.get("ok"):
            return redirect("/login")
        return f(*a, **kw)
    return w

# ── HTTP routes ──────────────────────────────────────────────────────────────────
@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "AI Launcher",
        "short_name": "AI Launcher",
        "description": "Mobile AI tool launcher",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#4f86c6",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]
    })

@app.route("/sw.js")
def service_worker():
    js = """
const CACHE = 'ai-launcher-v1';
self.addEventListener('install', e => e.waitUntil(self.skipWaiting()));
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  e.respondWith(fetch(e.request).catch(() =>
    new Response('<h2 style="font-family:sans-serif;padding:2rem">Offline — check network</h2>',
      {headers:{'Content-Type':'text/html;charset=utf-8'}})));
});
"""
    return app.response_class(js, mimetype="application/javascript")

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("u") == ADMIN_USER and request.form.get("p") == ADMIN_PASS:
            fs["ok"] = True
            return redirect("/")
        err = "Wrong username or password"
    return render_template_string(LOGIN_HTML, err=err)

@app.route("/logout")
def logout():
    fs.clear()
    return redirect("/login")

@app.route("/api/memory")
@login_required
def api_memory():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.split()[0])
    total = info["MemTotal"]
    avail = info["MemAvailable"]
    used  = total - avail
    pct   = round(used / total * 100)
    return jsonify({"total_mb": total//1024, "used_mb": used//1024, "pct": pct})

def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@app.route("/")
@login_required
def index():
    return _no_cache(app.make_response(render_template_string(INDEX_HTML, tools=TOOLS)))

@app.route("/t/<name>")
@login_required
def terminal_page(name):
    running = _tmux_sessions()
    if name not in running:
        return redirect("/")
    meta = _load_meta()
    sess = meta.get(name, {"name": name, "label": name, "color": "#888", "tool_id": None})
    tool = TOOL_MAP.get(sess.get("tool_id"), {})
    return _no_cache(app.make_response(render_template_string(TERMINAL_HTML, sess=sess, tool=tool)))

@app.route("/api/sessions")
@login_required
def api_sessions():
    running = _tmux_sessions()
    meta = _load_meta()
    dead = [k for k in meta if k not in running]
    if dead:
        for k in dead:
            del meta[k]
        _save_meta(meta)
    result = []
    for name in running:
        m = meta.get(name, {})
        result.append({
            "name": name,
            "label": m.get("label", name),
            "color": m.get("color", "#888"),
            "tool_id": m.get("tool_id"),
            "created_at": m.get("created_at", 0),
        })
    result.sort(key=lambda x: -x["created_at"])
    return jsonify(result)

@app.route("/api/new", methods=["POST"])
@login_required
def api_new():
    d = request.get_json() or {}
    custom  = d.get("cmd", "").strip()
    tool_id = d.get("tool_id", "")
    resume  = bool(d.get("resume"))
    try:
        cols = max(20, min(int(d.get("cols", 80)), 500))
        rows = max(5,  min(int(d.get("rows", 24)), 200))
    except (ValueError, TypeError):
        cols, rows = 80, 24
    if d.get("shortcut_id"):
        sc = next((s for s in _load_shortcuts() if s["id"] == d["shortcut_id"]), None)
        if not sc:
            return jsonify({"ok": False, "error": "Shortcut not found"}), 400
        user = sc.get("user", "root")
        raw  = sc["cmd"].replace("'", "'\\''")
        if user == "root":
            cmd = f"bash -c '{raw}; exec bash'"
        else:
            cmd = f"su - {user} -c '{raw}; exec bash'"
        name = _create_session(sc["name"], sc["color"], cmd, cols=cols, rows=rows)
    elif custom:
        safe = custom.replace("'", "'\\''")
        wrapped = f"bash -c '{safe}; exec bash'"
        name = _create_session(custom[:28] + ("…" if len(custom) > 28 else ""), "#888", wrapped, cols=cols, rows=rows)
    elif tool_id in TOOL_MAP:
        t    = TOOL_MAP[tool_id]
        cmd  = _build_cmd(tool_id, resume)
        lbl  = t["label"] + (" · resume" if resume else "")
        name = _create_session(lbl, t["color"], cmd, tool_id, cols=cols, rows=rows)
    else:
        return jsonify({"ok": False, "error": "Bad request"}), 400
    return jsonify({"ok": True, "name": name})

@app.route("/api/close/<name>", methods=["POST"])
@login_required
def api_close(name):
    _tmux_kill(name)
    meta = _load_meta()
    meta.pop(name, None)
    _save_meta(meta)
    return jsonify({"ok": True})

@app.route("/api/shortcuts", methods=["GET"])
@login_required
def api_shortcuts_list():
    return jsonify(_load_shortcuts())

@app.route("/api/shortcuts", methods=["POST"])
@login_required
def api_shortcuts_create():
    d = request.get_json() or {}
    name = d.get("name", "").strip()[:40]
    cmd  = d.get("cmd",  "").strip()
    if not name or not cmd:
        return jsonify({"ok": False, "error": "Name and command are required"}), 400
    shortcuts = _load_shortcuts()
    sc = {
        "id":         uuid.uuid4().hex[:8],
        "name":       name,
        "cmd":        cmd,
        "user":       d.get("user", "root") if d.get("user") in SC_USERS else "root",
        "pinned":     bool(d.get("pinned", False)),
        "color":      SC_COLORS[len(shortcuts) % len(SC_COLORS)],
        "created_at": int(time.time()),
    }
    shortcuts.append(sc)
    _save_shortcuts(shortcuts)
    return jsonify({"ok": True, "shortcut": sc})

@app.route("/api/shortcuts/<sid>", methods=["PUT"])
@login_required
def api_shortcuts_update(sid):
    d = request.get_json() or {}
    shortcuts = _load_shortcuts()
    for sc in shortcuts:
        if sc["id"] == sid:
            if "name"   in d: sc["name"]   = str(d["name"]).strip()[:40]
            if "cmd"    in d: sc["cmd"]    = str(d["cmd"]).strip()
            if "user"   in d and d["user"] in SC_USERS: sc["user"] = d["user"]
            if "pinned" in d: sc["pinned"] = bool(d["pinned"])
            _save_shortcuts(shortcuts)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not found"}), 404

@app.route("/api/shortcuts/<sid>", methods=["DELETE"])
@login_required
def api_shortcuts_delete(sid):
    shortcuts = [s for s in _load_shortcuts() if s["id"] != sid]
    _save_shortcuts(shortcuts)
    return jsonify({"ok": True})

# ── WebSocket terminal ───────────────────────────────────────────────────────────
@sock.route("/ws/<name>")
def ws_terminal(ws, name):
    if not fs.get("ok"):
        return
    if name not in _tmux_sessions():
        return

    # Client measures screen size before opening WebSocket; pass ?cols=X&rows=Y in URL
    try:
        cols = max(10, min(int(request.args.get('cols', 80)), 500))
        rows = max(5,  min(int(request.args.get('rows', 24)), 200))
    except (ValueError, TypeError):
        cols, rows = 80, 24

    master_fd, proc = _tmux_attach_pty(name, rows=rows, cols=cols)
    subprocess.run(["tmux", "resize-window", "-t", name,
                    "-x", str(cols), "-y", str(rows)], capture_output=True)
    alive = threading.Event()
    alive.set()

    def reader():
        while alive.is_set():
            try:
                r, _, _ = select.select([master_fd], [], [], 0.04)
                if r:
                    data = os.read(master_fd, 4096)
                    ws.send(data.decode("utf-8", errors="replace"))
            except Exception:
                alive.clear()
                break

    threading.Thread(target=reader, daemon=True).start()

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, str) and msg.startswith("{"):
                try:
                    obj = json.loads(msg)
                    if obj.get("type") == "resize":
                        rows, cols = int(obj["rows"]), int(obj["cols"])
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                        subprocess.run(
                            ["tmux", "resize-window", "-t", name,
                             "-x", str(cols), "-y", str(rows)],
                            capture_output=True
                        )
                    continue
                except Exception:
                    pass
            try:
                os.write(master_fd, msg.encode() if isinstance(msg, str) else msg)
            except Exception:
                break
    except Exception:
        pass
    finally:
        alive.clear()
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass

# ── HTML templates ───────────────────────────────────────────────────────────────
PWA_META = """
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#4f86c6">
<meta name="mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="/static/icon.svg">
<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>
"""

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AI Launcher</title>""" + PWA_META + r"""
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:16px;padding:40px 32px;width:100%;max-width:360px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
h1{font-size:22px;font-weight:700;color:#111;margin-bottom:4px}
.sub{font-size:13px;color:#999;margin-bottom:28px}
label{display:block;font-size:13px;font-weight:500;color:#555;margin-bottom:6px}
input{width:100%;padding:12px 14px;border:1.5px solid #e5e7eb;border-radius:10px;font-size:15px;outline:none;transition:border-color .2s}
input:focus{border-color:#4f86c6}
.field{margin-bottom:16px}
.btn{width:100%;padding:13px;background:#111;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;margin-top:8px}
.btn:active{opacity:.8}
.err{color:#d32f2f;font-size:13px;margin-top:12px;text-align:center}
</style>
</head>
<body>
<div class="card">
  <h1>AI Launcher</h1>
  <div class="sub">Sign in to continue</div>
  <form method="post">
    <div class="field"><label>Username</label><input name="u" type="text" placeholder="admin" autocomplete="username"></div>
    <div class="field"><label>Password</label><input name="p" type="password" placeholder="••••••••" autocomplete="current-password"></div>
    <button class="btn" type="submit">Sign in</button>
    {% if err %}<div class="err">{{ err }}</div>{% endif %}
  </form>
</div>
</body>
</html>"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AI Launcher</title>""" + PWA_META + r"""<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f2f4f7;color:#111}
.header{background:#fff;border-bottom:1px solid #e5e7eb;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.header h1{font-size:17px;font-weight:700}
.logout{font-size:13px;color:#888;text-decoration:none;padding:6px 12px;border-radius:8px;background:#f5f5f5}
.section{padding:16px 16px 8px;font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.8px}
.list{padding:0 12px}
.card{background:#fff;border-radius:14px;margin-bottom:10px;padding:14px 16px;display:flex;align-items:center;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.dot{width:10px;height:10px;border-radius:50%;margin-right:14px;flex-shrink:0}
.info{flex:1;min-width:0}
.name{font-size:15px;font-weight:600;color:#111}
.desc{font-size:12px;color:#999;margin-top:2px}
.actions{display:flex;gap:6px;flex-shrink:0}
.btn{border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;padding:8px 12px;white-space:nowrap}
.btn:active{opacity:.7}
.btn-primary{background:#111;color:#fff}
.btn-secondary{background:#f0f0f0;color:#555}
.btn-danger{background:#fdecea;color:#d32f2f}
.custom-box{margin:0 12px 4px;display:flex;gap:8px}
.custom-box input{flex:1;min-width:0;padding:11px 12px;border:1.5px solid #e5e7eb;border-radius:10px;font-size:14px;outline:none}
.custom-box input:focus{border-color:#4f86c6}
.custom-box button{padding:11px 14px;background:#111;color:#fff;border:none;border-radius:10px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0}
.session-label{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.session-time{font-size:11px;color:#aaa;margin-top:2px}
.empty{text-align:center;padding:24px;color:#ccc;font-size:13px}
.pb{padding-bottom:32px}
.cnt{color:#bbb;font-weight:400;margin-left:4px}
.mem-bar-wrap{margin:0 12px 4px;background:#f0f0f0;border-radius:8px;height:6px;overflow:hidden}
.mem-bar{height:100%;border-radius:8px;transition:width .5s,background .5s}
.mem-info{margin:4px 12px 12px;font-size:12px;color:#999;display:flex;justify-content:space-between}
.mem-warn{background:#fff3cd;border:1px solid #ffc107;border-radius:10px;margin:0 12px 8px;padding:10px 14px;font-size:13px;color:#856404;display:none}
.sec-hdr{display:flex;align-items:center;justify-content:space-between;padding:8px 16px}
.sec-lbl{font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.8px}
.sec-add{background:#111;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:600;padding:6px 12px;cursor:pointer}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:200;align-items:flex-end}
.modal.show{display:flex}
.mbox{background:#fff;border-radius:20px 20px 0 0;padding:20px 16px 36px;width:100%}
.mtitle{font-size:16px;font-weight:700;margin-bottom:16px;color:#111}
.mfield,.mselect{display:block;width:100%;margin-bottom:12px;padding:12px;border:1.5px solid #e5e7eb;border-radius:10px;font-size:14px;outline:none;background:#fff;-webkit-appearance:none}
.mfield:focus,.mselect:focus{border-color:#4f86c6}
.mcheck-row{display:flex;align-items:center;gap:10px;margin-bottom:20px;font-size:14px;color:#333}
.mcheck-row input[type=checkbox]{width:18px;height:18px;flex-shrink:0}
.mbtns{display:flex;gap:8px}
.mbtn{flex:1;padding:12px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer}
.mbtn-save{background:#111;color:#fff}
.mbtn-cancel{background:#f0f0f0;color:#555}
.mbtn-del{background:#fdecea;color:#d32f2f;width:100%;margin-top:10px}
</style>
</head>
<body>
<div class="header">
  <h1>AI Launcher</h1>
  <a href="/logout" class="logout">Sign out</a>
</div>

<div class="section">Tools</div>
<div class="list" id="tools-list">
{% for t in tools %}
<div class="card">
  <div class="dot" style="background:{{t.color}}"></div>
  <div class="info">
    <div class="name">{{t.label}}</div>
    <div class="desc">{{t.desc}}</div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" onclick="newSession('{{t.id}}',false)">New</button>
    <button class="btn btn-secondary" onclick="newSession('{{t.id}}',true)">Resume</button>
  </div>
</div>
{% endfor %}
<div id="pinned-sc"></div>
</div>

<div class="sec-hdr">
  <span class="sec-lbl">Shortcuts</span>
  <button class="sec-add" onclick="openScModal()">＋ Add</button>
</div>
<div class="list" id="my-sc-list"></div>

<div class="section">One-shot command</div>
<div class="custom-box">
  <input id="custom-cmd" type="text" placeholder="e.g. python3 /opt/test.py">
  <button onclick="runCustom()">Run</button>
</div>

<div class="section">Server Memory</div>
<div class="mem-bar-wrap"><div class="mem-bar" id="mem-bar"></div></div>
<div class="mem-info"><span id="mem-text">Loading…</span><span id="mem-pct"></span></div>
<div class="mem-warn" id="mem-warn">⚠️ Memory above 80% — consider closing idle sessions.</div>

<div class="section">Active sessions<span class="cnt" id="scnt"></span></div>
<div class="list" id="session-list"><div class="empty">No active sessions</div></div>

<div style="height:32px"></div>

<!-- Shortcut modal -->
<div class="modal" id="sc-modal" onclick="if(event.target===this)closeScModal()">
  <div class="mbox">
    <div class="mtitle" id="sc-modal-title">Add shortcut</div>
    <input class="mfield" id="sc-name" placeholder="Name (e.g. Research)">
    <input class="mfield" id="sc-cmd" placeholder="Command (e.g. claude)">
    <select class="mselect" id="sc-user">
      <option value="root">root</option>
    </select>
    <label class="mcheck-row">
      <input type="checkbox" id="sc-pinned">
      Pin to tools area
    </label>
    <div class="mbtns">
      <button class="mbtn mbtn-save" onclick="saveShortcut()">Save</button>
      <button class="mbtn mbtn-cancel" onclick="closeScModal()">Cancel</button>
    </div>
    <div id="sc-del-wrap" style="display:none">
      <button class="mbtn mbtn-del" onclick="deleteShortcut()">Delete shortcut</button>
    </div>
  </div>
</div>

<script>
function relTime(ts){
  const d=Math.floor(Date.now()/1000-ts);
  if(d<60)return d+'s ago';
  if(d<3600)return Math.floor(d/60)+'m ago';
  if(d<86400)return Math.floor(d/3600)+'h ago';
  return Math.floor(d/86400)+'d ago';
}
function _estSize(){
  return {cols:Math.max(40,Math.floor(window.innerWidth/8.4)),
          rows:Math.max(20,Math.floor((window.innerHeight-90)/17))};
}
async function newSession(toolId,resume){
  const sz=_estSize();
  const r=await fetch('/api/new',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tool_id:toolId,resume,...sz})});
  const d=await r.json();
  if(d.ok){location.href='/t/'+d.name}else{alert(d.error||'Failed')}
}
async function runCustom(){
  const cmd=document.getElementById('custom-cmd').value.trim();
  if(!cmd)return;
  const sz=_estSize();
  const r=await fetch('/api/new',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd,...sz})});
  const d=await r.json();
  if(d.ok){location.href='/t/'+d.name}else{alert(d.error||'Failed')}
}
async function closeSession(name){
  if(!confirm('Close this session?'))return;
  await fetch('/api/close/'+name,{method:'POST'});
  loadSessions();
}
async function loadSessions(){
  const r=await fetch('/api/sessions');
  const list=await r.json();
  document.getElementById('scnt').textContent=list.length?'('+list.length+')':'';
  const el=document.getElementById('session-list');
  if(!list.length){el.innerHTML='<div class="empty">No active sessions</div>';return;}
  el.innerHTML=list.map(s=>`
    <div class="card">
      <div class="dot" style="background:${s.color}"></div>
      <div class="info">
        <div class="session-label">${s.label}</div>
        <div class="session-time">${relTime(s.created_at)}</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="location.href='/t/${s.name}'">Open</button>
        <button class="btn btn-danger" onclick="closeSession('${s.name}')">Close</button>
      </div>
    </div>`).join('');
}
async function loadMemory(){
  const r=await fetch('/api/memory');
  const d=await r.json();
  const bar=document.getElementById('mem-bar');
  const warn=document.getElementById('mem-warn');
  bar.style.width=d.pct+'%';
  bar.style.background=d.pct>=80?'#e53935':d.pct>=60?'#fb8c00':'#43a047';
  document.getElementById('mem-text').textContent=`${d.used_mb} MB / ${d.total_mb} MB`;
  document.getElementById('mem-pct').textContent=d.pct+'%';
  warn.style.display=d.pct>=80?'block':'none';
}

// ── Shortcuts ──────────────────────────────────────
let _scs = [], _scEditId = null;
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function loadShortcuts(){
  try{
    const r = await fetch('/api/shortcuts');
    _scs = await r.json();
    renderScs();
  }catch(e){}
}

function renderScs(){
  const pinned = _scs.filter(s=>s.pinned);
  const free   = _scs.filter(s=>!s.pinned);
  document.getElementById('pinned-sc').innerHTML = pinned.map(s=>`
    <div class="card">
      <div class="dot" style="background:${s.color}"></div>
      <div class="info">
        <div class="name">${esc(s.name)}</div>
        <div class="desc">${esc(s.cmd)}${s.user!=='root'?' · '+esc(s.user):''}</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="launchSc('${s.id}')">New</button>
        <button class="btn btn-secondary" onclick="openScModal('${s.id}')">Edit</button>
      </div>
    </div>`).join('');
  document.getElementById('my-sc-list').innerHTML = free.map(s=>`
    <div class="card">
      <div class="dot" style="background:${s.color}"></div>
      <div class="info">
        <div class="name">${esc(s.name)}</div>
        <div class="desc">${esc(s.cmd)}${s.user!=='root'?' · '+esc(s.user):''}</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="launchSc('${s.id}')">New</button>
        <button class="btn btn-secondary" onclick="openScModal('${s.id}')">Edit</button>
      </div>
    </div>`).join('');
}

function openScModal(id=null){
  _scEditId = id;
  const editing = !!id;
  document.getElementById('sc-modal-title').textContent = editing?'Edit shortcut':'Add shortcut';
  document.getElementById('sc-del-wrap').style.display = editing?'block':'none';
  if(editing){
    const sc = _scs.find(s=>s.id===id)||{};
    document.getElementById('sc-name').value   = sc.name||'';
    document.getElementById('sc-cmd').value    = sc.cmd||'';
    document.getElementById('sc-user').value   = sc.user||'root';
    document.getElementById('sc-pinned').checked = !!sc.pinned;
  } else {
    document.getElementById('sc-name').value   = '';
    document.getElementById('sc-cmd').value    = '';
    document.getElementById('sc-user').value   = 'root';
    document.getElementById('sc-pinned').checked = false;
  }
  document.getElementById('sc-modal').classList.add('show');
  document.getElementById('sc-name').focus();
}
function closeScModal(){ document.getElementById('sc-modal').classList.remove('show'); }

async function saveShortcut(){
  const name   = document.getElementById('sc-name').value.trim();
  const cmd    = document.getElementById('sc-cmd').value.trim();
  const user   = document.getElementById('sc-user').value;
  const pinned = document.getElementById('sc-pinned').checked;
  if(!name||!cmd){ alert('Name and command are required'); return; }
  if(_scEditId){
    await fetch('/api/shortcuts/'+_scEditId,{method:'PUT',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name,cmd,user,pinned})});
  } else {
    await fetch('/api/shortcuts',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name,cmd,user,pinned})});
  }
  closeScModal();
  loadShortcuts();
}

async function deleteShortcut(){
  if(!confirm('Delete this shortcut?')) return;
  await fetch('/api/shortcuts/'+_scEditId,{method:'DELETE'});
  closeScModal();
  loadShortcuts();
}

async function launchSc(id){
  const sz = _estSize();
  const r = await fetch('/api/new',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({shortcut_id:id,...sz})});
  const d = await r.json();
  if(d.ok){ location.href='/t/'+d.name; } else { alert(d.error||'Failed'); }
}

loadSessions();
loadShortcuts();
loadMemory();
setInterval(loadSessions,8000);
setInterval(loadMemory,30000);
</script>
</body>
</html>"""

TERMINAL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>{{ sess.label }}</title>""" + PWA_META + r"""<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html{height:100%;overflow:hidden}
body{position:fixed;top:0;left:0;right:0;bottom:0;background:#fafafa;display:flex;flex-direction:column;overflow:hidden}
.toolbar{background:#fff;border-bottom:1px solid #e5e7eb;padding:8px 12px;display:flex;flex-wrap:wrap;align-items:center;gap:6px;flex-shrink:0}
.trow1{display:flex;align-items:center;gap:8px;width:100%}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.label{flex:1;font-size:14px;font-weight:600;color:#111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.tbtns{display:flex;gap:6px;flex-shrink:0}
.tbtn{border:none;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;padding:6px 10px;white-space:nowrap}
.tbtn:active{opacity:.7}
.t-back{background:#f0f0f0;color:#333}
.t-new{background:#e8f0fe;color:#1565c0}
.t-fs{background:#f0f0f0;color:#333}
.t-close{background:#fdecea;color:#d32f2f}
#term-wrap{flex:1;overflow:hidden;padding:2px}
.xterm{height:100%}
.xterm-viewport{overflow-y:scroll!important}
.keybar{display:flex;overflow-x:auto;background:#f0f0f0;border-bottom:1px solid #ddd;padding:4px 8px;gap:5px;flex-shrink:0;-webkit-overflow-scrolling:touch}
.keybar::-webkit-scrollbar{display:none}
.kbtn{border:1px solid #ccc;border-radius:6px;background:#fff;color:#333;font-size:12px;font-weight:600;padding:5px 11px;white-space:nowrap;cursor:pointer;flex-shrink:0;user-select:none}
.kbtn:active{background:#ddd}
.kbtn-danger{background:#fff5f5;border-color:#f9a8a8;color:#c62828}
.sesbar{display:flex;overflow-x:auto;background:#fafafa;border-bottom:1px solid #e8e8e8;padding:4px 8px;gap:5px;flex-shrink:0;-webkit-overflow-scrolling:touch}
.sesbar::-webkit-scrollbar{display:none}
.seschip{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap;cursor:pointer;border:1.5px solid #e0e0e0;background:#fff;color:#555;flex-shrink:0;user-select:none}
.seschip.cur{border-color:var(--sc);color:var(--sc);background:#fff}
.seschip:active{opacity:.6}
.sesdot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
</style>
</head>
<body>
<div class="toolbar">
  <div class="trow1">
    <div class="dot" style="background:{{ sess.color }}"></div>
    <div class="label">{{ sess.label }}</div>
    <div class="tbtns">
      <button class="tbtn t-back" onclick="location.href='/'">Home</button>
      {% if tool and tool.id %}
      <button class="tbtn t-new" onclick="newSame()">New</button>
      {% endif %}
      <button class="tbtn t-fs" onclick="redraw()" style="background:#e8f5e9;color:#2e7d32">Redraw</button>
      <button class="tbtn t-fs" id="fs-btn" onclick="toggleFs()">Fullscreen</button>
      <button class="tbtn t-close" onclick="closeAndBack()">Close</button>
    </div>
  </div>
</div>
<div class="sesbar" id="sesbar"></div>
<div class="keybar">
  <button class="kbtn" onclick="sendKey('\x1b')">ESC</button>
  <button class="kbtn" onclick="sendKey('\t')">TAB</button>
  <button class="kbtn" onclick="sendKey('\x1b[A')">↑</button>
  <button class="kbtn" onclick="sendKey('\x1b[B')">↓</button>
  <button class="kbtn" onclick="sendKey('\x1b[D')">←</button>
  <button class="kbtn" onclick="sendKey('\x1b[C')">→</button>
  <button class="kbtn kbtn-danger" onclick="sendKey('\x03')">Ctrl+C</button>
  <button class="kbtn" onclick="sendKey('\x04')">Ctrl+D</button>
  <button class="kbtn" onclick="sendKey('\x0c')">Ctrl+L</button>
  <button class="kbtn" onclick="sendKey('\x1a')">Ctrl+Z</button>
  <button class="kbtn" onclick="sendKey('|')">|</button>
  <button class="kbtn" onclick="sendKey('~')">~</button>
</div>
<div id="term-wrap"></div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script>
const SESS = "{{ sess.name }}";
const TOOL = "{{ tool.id if tool and tool.id else '' }}";

const term = new Terminal({
  theme:{
    background:'#fafafa',foreground:'#1a1a1a',cursor:'#444',cursorAccent:'#fafafa',
    selectionBackground:'#b3d4fd',
    black:'#2e2e2e',red:'#c0392b',green:'#27ae60',yellow:'#d68910',
    blue:'#1a5276',magenta:'#6c3483',cyan:'#0e6655',white:'#ecf0f1',
    brightBlack:'#7f8c8d',brightRed:'#e74c3c',brightGreen:'#2ecc71',brightYellow:'#f1c40f',
    brightBlue:'#2980b9',brightMagenta:'#9b59b6',brightCyan:'#1abc9c',brightWhite:'#ffffff',
  },
  fontFamily:'Menlo,Monaco,"Courier New",monospace',
  fontSize:14,lineHeight:1.3,
  scrollback:3000,cursorBlink:true,allowTransparency:false,
});

const fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(document.getElementById('term-wrap'));

// Touch scroll: swipe up/down to scroll terminal history
(function(){
  const el = document.getElementById('term-wrap');
  let startY = 0;
  el.addEventListener('touchstart', e => { startY = e.touches[0].clientY; }, {passive:true});
  el.addEventListener('touchmove', e => {
    const dy = startY - e.touches[0].clientY;
    startY = e.touches[0].clientY;
    if(Math.abs(dy) > 1){ term.scrollLines(Math.round(dy / 5)); e.preventDefault(); }
  }, {passive:false});
})();

const proto = location.protocol==='https:'?'wss:':'ws:';
let ws, reconnTimer;

function doFit(){
  fitAddon.fit();
  if(ws && ws.readyState===1) ws.send(JSON.stringify({type:'resize',rows:term.rows,cols:term.cols}));
}

function connect(){
  clearTimeout(reconnTimer);
  fitAddon.fit();
  const cols = term.cols || 80;
  const rows = term.rows || 24;
  ws = new WebSocket(`${proto}//${location.host}/ws/${SESS}?cols=${cols}&rows=${rows}`);
  ws.onopen = ()=>{
    term.write('\x1b[2K\r');
    setTimeout(()=>{ doFit(); ws.send('\x0c'); }, 400);
  };
  ws.onmessage = e=>{ term.write(e.data); };
  ws.onclose = ()=>{
    term.write('\r\n\x1b[33m[Disconnected — reconnecting in 2s...]\x1b[0m');
    reconnTimer = setTimeout(connect, 2000);
  };
  ws.onerror = ()=>{ ws.close(); };
}

connect();
term.onData(d=>{ if(ws && ws.readyState===1) ws.send(d); });
function sendKey(s){ if(ws && ws.readyState===1){ ws.send(s); term.focus(); } }

// Session switcher bar
async function loadSesBar(){
  try{
    const r=await fetch('/api/sessions');
    const list=await r.json();
    const bar=document.getElementById('sesbar');
    if(!list.length){bar.style.display='none';return;}
    bar.style.display='flex';
    bar.innerHTML=list.map(s=>`
      <div class="seschip${s.name===SESS?' cur':''}"
           style="--sc:${s.color}"
           onclick="switchSes('${s.name}')">
        <span class="sesdot" style="background:${s.color}"></span>
        ${s.label}
      </div>`).join('');
    const cur=bar.querySelector('.cur');
    if(cur) cur.scrollIntoView({inline:'center',behavior:'smooth'});
  }catch(e){}
}
function switchSes(name){ if(name!==SESS) window.open('/t/'+name,'_blank'); }
loadSesBar();
setInterval(loadSesBar,8000);

// Reconnect immediately when app returns to foreground
document.addEventListener('visibilitychange', ()=>{
  if(!document.hidden && ws.readyState !== WebSocket.OPEN){
    clearTimeout(reconnTimer);
    connect();
  }
});

const ro = new ResizeObserver(doFit);
ro.observe(document.getElementById('term-wrap'));
window.addEventListener('resize', doFit);

function redraw(){
  doFit();
  setTimeout(()=>{ if(ws&&ws.readyState===1) ws.send('\x0c'); }, 200);
}
async function newSame(){
  const sz=_estSize?_estSize():{cols:80,rows:24};
  const r=await fetch('/api/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tool_id:TOOL,...sz})});
  const d=await r.json();
  if(d.ok) window.open('/t/'+d.name,'_blank');
}
function _estSize(){
  return {cols:Math.max(40,Math.floor(window.innerWidth/8.4)),
          rows:Math.max(20,Math.floor((window.innerHeight-90)/17))};
}
async function closeAndBack(){
  if(!confirm('Close this session and return home?'))return;
  await fetch('/api/close/'+SESS,{method:'POST'});
  location.href='/';
}
function toggleFs(){
  const btn=document.getElementById('fs-btn');
  if(!document.fullscreenElement){
    document.documentElement.requestFullscreen().catch(()=>{});
    btn.textContent='Exit fullscreen';
  }else{
    document.exitFullscreen();
    btn.textContent='Fullscreen';
  }
}
document.addEventListener('fullscreenchange',()=>setTimeout(doFit,200));
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7681))
    app.run(host="0.0.0.0", port=port, debug=False)

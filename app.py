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
KEYBAR_FILE    = Path(os.environ.get("KEYBAR_FILE",    "/opt/ai-launcher/keybar.json"))
SERVERS_FILE   = Path(os.environ.get("SERVERS_FILE",   "/opt/ai-launcher/servers.json"))
SSHKEYS_DIR    = Path(os.environ.get("SSHKEYS_DIR",    "/opt/ai-launcher/ssh_keys"))
SSHKEYS_DIR.mkdir(mode=0o700, exist_ok=True)
SC_COLORS  = ["#e8703a","#7c6af7","#2e86de","#e74c3c","#f39c12","#8e44ad","#16a085","#d35400","#2980b9","#1abc9c"]
SC_USERS   = ["root"]

BUILTIN_KEYS = [
    {"id": "esc",         "label": "ESC",    "type": "key",    "key": "\x1b"},
    {"id": "tab",         "label": "TAB",    "type": "key",    "key": "\t"},
    {"id": "arrow_up",    "label": "↑",      "type": "key",    "key": "\x1b[A"},
    {"id": "arrow_down",  "label": "↓",      "type": "key",    "key": "\x1b[B"},
    {"id": "arrow_left",  "label": "←",      "type": "key",    "key": "\x1b[D"},
    {"id": "arrow_right", "label": "→",      "type": "key",    "key": "\x1b[C"},
    {"id": "ctrl_c",      "label": "Ctrl+C", "type": "key",    "key": "\x03"},
    {"id": "ctrl_d",      "label": "Ctrl+D", "type": "key",    "key": "\x04"},
    {"id": "ctrl_l",      "label": "Ctrl+L", "type": "key",    "key": "\x0c"},
    {"id": "ctrl_z",      "label": "Ctrl+Z", "type": "key",    "key": "\x1a"},
    {"id": "pipe",        "label": "|",      "type": "key",    "key": "|"},
    {"id": "tilde",       "label": "~",      "type": "key",    "key": "~"},
]

def _load_shortcuts():
    try:
        return json.loads(SHORTCUTS_FILE.read_text())
    except Exception:
        return []

def _save_shortcuts(data):
    SHORTCUTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def _load_keybar():
    try:
        return json.loads(KEYBAR_FILE.read_text())
    except Exception:
        return {"hidden": [], "custom": []}

def _save_keybar(data):
    KEYBAR_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def _load_servers():
    try:
        return json.loads(SERVERS_FILE.read_text())
    except Exception:
        return []

def _save_servers(data):
    SERVERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ── Tool definitions ──────────────────────────────────────────────────────────────
# Each tool appears as a button on the home screen.
# "cmd"    : command to run in a new tmux session
# "resume" : command to attach to an existing Claude conversation (-r flag)
# "user"   : unix user to run as (non-root uses `su - <user>`)
TOOLS = [
    # Add your tools here. Each entry appears as a button on the home screen.
    # "cmd"    : command to start a new session
    # "resume" : command to resume/continue (e.g. claude -r)
    # "user"   : unix user to run as ("root" runs directly; others use `su - <user>`)
    {"id": "claude",  "label": "Claude",  "desc": "Claude AI",    "color": "#4f86c6",
     "cmd": "claude", "resume": "claude -r", "user": "root"},
    {"id": "codex",   "label": "Codex",   "desc": "OpenAI Codex", "color": "#e74c3c",
     "cmd": "codex",  "resume": "codex",     "user": "root"},
    {"id": "shell",   "label": "Shell",   "desc": "Bash shell",   "color": "#607d8b",
     "cmd": "bash",   "resume": "bash",      "user": "root"},
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
    safe = raw.replace("'", "'\\''")
    if t["user"] == "root":
        return f"bash -c '{safe}; exec bash'"
    return f"su - {t['user']} -c '{safe}; exec bash'"

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

@app.route("/api/sessions/<name>", methods=["PUT"])
@login_required
def api_session_update(name):
    d = request.get_json() or {}
    meta = _load_meta()
    if name not in meta:
        return jsonify({"ok": False, "error": "not found"}), 404
    if "label" in d:
        meta[name]["label"] = str(d["label"]).strip()[:60]
    _save_meta(meta)
    return jsonify({"ok": True})

@app.route("/api/history/<name>")
@login_required
def api_history(name):
    if name not in _tmux_sessions():
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        lines = max(100, min(int(request.args.get("lines", 3000)), 10000))
    except (ValueError, TypeError):
        lines = 3000
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", name, "-S", f"-{lines}", "-e", "-p"],
        capture_output=True, text=True
    )
    return jsonify({"ok": True, "text": r.stdout})

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

@app.route("/api/shortcuts/reorder", methods=["POST"])
@login_required
def api_shortcuts_reorder():
    ids = (request.get_json() or {}).get("ids", [])
    shortcuts = _load_shortcuts()
    sc_map = {s["id"]: s for s in shortcuts}
    reordered = [sc_map[i] for i in ids if i in sc_map]
    rest = [s for s in shortcuts if s["id"] not in {r["id"] for r in reordered}]
    _save_shortcuts(reordered + rest)
    return jsonify({"ok": True})

@app.route("/api/servers", methods=["GET"])
@login_required
def api_servers_list():
    return jsonify(_load_servers())

@app.route("/api/servers", methods=["POST"])
@login_required
def api_servers_create():
    d = request.get_json() or {}
    name = str(d.get("name","")).strip()[:40]
    host = str(d.get("host","")).strip()
    user = str(d.get("user","")).strip()
    try:
        port = max(1, min(int(d.get("port", 22)), 65535))
    except (ValueError, TypeError):
        port = 22
    tools = [{"label": str(t.get("label",""))[:30].strip(), "cmd": str(t.get("cmd","")).strip()}
             for t in d.get("tools", []) if str(t.get("cmd","")).strip()]
    if not name or not host or not user:
        return jsonify({"ok": False, "error": "name/host/user 不能为空"}), 400
    servers = _load_servers()
    sv = {"id": uuid.uuid4().hex[:8], "name": name, "host": host,
          "user": user, "port": port, "tools": tools, "has_key": False,
          "color": SC_COLORS[len(servers) % len(SC_COLORS)],
          "created_at": int(time.time())}
    ssh_key = str(d.get("ssh_key","")).strip()
    if ssh_key:
        kp = SSHKEYS_DIR / f"{sv['id']}.pem"
        kp.write_text(ssh_key + "\n")
        kp.chmod(0o600)
        sv["has_key"] = True
    servers.append(sv)
    _save_servers(servers)
    return jsonify({"ok": True, "server": sv})

@app.route("/api/servers/<sid>", methods=["PUT"])
@login_required
def api_servers_update(sid):
    d = request.get_json() or {}
    servers = _load_servers()
    for sv in servers:
        if sv["id"] == sid:
            if "name"  in d: sv["name"]  = str(d["name"]).strip()[:40]
            if "host"  in d: sv["host"]  = str(d["host"]).strip()
            if "user"  in d: sv["user"]  = str(d["user"]).strip()
            if "port"  in d:
                try: sv["port"] = max(1, min(int(d["port"]), 65535))
                except: pass
            if "tools" in d:
                sv["tools"] = [{"label": str(t.get("label",""))[:30].strip(), "cmd": str(t.get("cmd","")).strip()}
                               for t in d["tools"] if str(t.get("cmd","")).strip()]
            ssh_key = str(d.get("ssh_key","")).strip()
            if ssh_key:
                kp = SSHKEYS_DIR / f"{sid}.pem"
                kp.write_text(ssh_key + "\n")
                kp.chmod(0o600)
                sv["has_key"] = True
            elif d.get("clear_key"):
                kp = SSHKEYS_DIR / f"{sid}.pem"
                if kp.exists(): kp.unlink()
                sv["has_key"] = False
            _save_servers(servers)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not found"}), 404

@app.route("/api/servers/<sid>", methods=["DELETE"])
@login_required
def api_servers_delete(sid):
    servers = [s for s in _load_servers() if s["id"] != sid]
    _save_servers(servers)
    kp = SSHKEYS_DIR / f"{sid}.pem"
    if kp.exists(): kp.unlink()
    return jsonify({"ok": True})

@app.route("/api/keybar", methods=["GET"])
@login_required
def api_keybar_get():
    kb = _load_keybar()
    return jsonify({
        "builtins": BUILTIN_KEYS,
        "hidden":   kb.get("hidden", []),
        "custom":   kb.get("custom", []),
    })

@app.route("/api/keybar", methods=["POST"])
@login_required
def api_keybar_save():
    d = request.get_json() or {}
    kb = {
        "hidden": [str(h) for h in d.get("hidden", []) if isinstance(h, str)],
        "custom": [
            {
                "id":    c.get("id") or uuid.uuid4().hex[:8],
                "label": str(c.get("label", ""))[:20],
                "type":  "key",
                "key":   str(c.get("key", "")),
            }
            for c in d.get("custom", [])
            if c.get("label") and c.get("key") is not None
        ],
    }
    _save_keybar(kb)
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
  <h1>AI 工作台</h1>
  <div class="sub">请登录以继续</div>
  <form method="post">
    <div class="field"><label>用户名</label><input name="u" type="text" placeholder="admin" autocomplete="username"></div>
    <div class="field"><label>密码</label><input name="p" type="password" placeholder="••••••••" autocomplete="current-password"></div>
    <button class="btn" type="submit">登录</button>
    {% if err %}<div class="err">用户名或密码错误</div>{% endif %}
  </form>
</div>
</body>
</html>"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AI 工作台</title>""" + PWA_META + r"""<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f2f4f7;color:#111}
.header{background:#fff;border-bottom:1px solid #e5e7eb;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.header h1{font-size:17px;font-weight:700}
.hbtns{display:flex;gap:8px;align-items:center}
.lang-btn{font-size:12px;color:#888;padding:5px 10px;border-radius:8px;background:#f5f5f5;border:none;cursor:pointer;font-weight:500}
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
.oneshot-trigger{margin:0 12px 4px;display:flex}
.oneshot-trigger button{flex:1;padding:12px;background:#f5f5f5;color:#555;border:1.5px dashed #ddd;border-radius:10px;font-size:14px;font-weight:500;cursor:pointer;text-align:left}
.oneshot-trigger button:active{background:#eee}
.session-label{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.session-time{font-size:11px;color:#aaa;margin-top:2px}
.empty{text-align:center;padding:24px;color:#ccc;font-size:13px}
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
.mbtn-run{background:#111;color:#fff;width:100%;margin-top:0}
.sort-item{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #f0f0f0}
.sort-item:last-child{border-bottom:none}
.sort-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.sort-name{flex:1;font-size:14px;color:#333;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sort-pin{font-size:10px;background:#e8f0fe;color:#1565c0;border-radius:4px;padding:2px 5px;margin-left:6px}
.sort-arrows{display:flex;gap:4px;flex-shrink:0}
.sort-arrows button{border:1px solid #ddd;background:#fff;border-radius:6px;padding:5px 10px;font-size:14px;cursor:pointer;line-height:1}
.sort-arrows button:disabled{opacity:.25;cursor:default}
.sv-tools-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.sv-tool-entry{display:flex;gap:6px;margin-bottom:8px;align-items:center}
.sv-tool-entry .mfield{flex:1;margin-bottom:0}
.sv-tool-del{border:none;background:#fdecea;color:#d32f2f;border-radius:6px;padding:8px 12px;cursor:pointer;font-size:13px;flex-shrink:0}
.sv-key-box{background:#f5f5f5;border-radius:10px;padding:10px 12px;margin-bottom:12px}
.sv-key-box textarea{width:100%;height:72px;border:1.5px solid #e5e7eb;border-radius:8px;font-size:11px;font-family:monospace;padding:8px;resize:none;box-sizing:border-box;outline:none;background:#fff}
.sv-key-box textarea:focus{border-color:#4f86c6}
.sv-badge{font-size:10px;background:#e8f0fe;color:#1565c0;border-radius:4px;padding:2px 6px;margin-left:4px;vertical-align:middle}
.sv-add-tool{width:100%;padding:9px;border:1.5px dashed #ddd;background:#f9f9f9;border-radius:8px;font-size:13px;color:#666;cursor:pointer;margin-bottom:12px}
.sv-add-tool:active{background:#eee}
</style>
</head>
<body>
<div class="header">
  <h1 id="h-title">AI 工作台</h1>
  <div class="hbtns">
    <button class="lang-btn" onclick="toggleLang()" id="lang-btn">EN</button>
    <a href="/logout" class="logout" id="h-logout">退出</a>
  </div>
</div>

<div class="section" id="s-tools">工具</div>
<div class="list" id="tools-list">
{% for t in tools %}
<div class="card">
  <div class="dot" style="background:{{t.color}}"></div>
  <div class="info">
    <div class="name">{{t.label}}</div>
    <div class="desc">{{t.desc}}</div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" onclick="newSession('{{t.id}}',false)" data-i18n="new">新建</button>
    <button class="btn btn-secondary" onclick="newSession('{{t.id}}',true)" data-i18n="resume">恢复</button>
  </div>
</div>
{% endfor %}
<div id="pinned-sc"></div>
</div>

<div class="sec-hdr">
  <span class="sec-lbl" id="s-shortcuts">我的快捷</span>
  <div style="display:flex;gap:6px">
    <button class="sec-add" onclick="openSortModal()" id="sort-btn" style="background:#f0f0f0;color:#555">↕ 排序</button>
    <button class="sec-add" onclick="openScModal()" id="sc-add-btn">＋ 添加</button>
  </div>
</div>
<div class="list" id="my-sc-list"></div>

<div class="sec-hdr">
  <span class="sec-lbl" id="s-servers">我的服务器</span>
  <button class="sec-add" onclick="openSvModal()" id="sv-add-btn">＋ 添加</button>
</div>
<div class="list" id="sv-list"></div>

<!-- Server modal -->
<div class="modal" id="sv-modal" onclick="if(event.target===this)closeSvModal()">
  <div class="mbox" style="max-height:85vh;overflow-y:auto">
    <div class="mtitle" id="sv-modal-title">添加服务器</div>
    <input class="mfield" id="sv-name" placeholder="名称（如：我的 VPS）">
    <input class="mfield" id="sv-host" placeholder="服务器地址（IP 或域名）" autocomplete="off">
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <input class="mfield" id="sv-user" placeholder="用户名（如：root）" style="flex:2;margin-bottom:0" autocomplete="off">
      <input class="mfield" id="sv-port" placeholder="端口" value="22" style="flex:1;margin-bottom:0">
    </div>
    <div class="sv-key-box">
      <div style="font-size:12px;font-weight:600;color:#555;margin-bottom:6px">SSH 私钥（选填）</div>
      <textarea id="sv-key" placeholder="粘贴 ~/.ssh/id_rsa 内容，留空则在终端中手动认证"></textarea>
      <div id="sv-key-hint" style="display:none;font-size:11px;color:#1565c0;margin-top:4px">已存有 SSH Key — 留空则保留原有，填写则替换</div>
      <label style="display:flex;align-items:center;gap:6px;margin-top:6px;font-size:12px;color:#888">
        <input type="checkbox" id="sv-clear-key"> 清除已存 SSH Key
      </label>
    </div>
    <div style="font-size:12px;font-weight:600;color:#555;margin-bottom:8px">AI 工具</div>
    <div id="sv-tools-list"></div>
    <button class="sv-add-tool" onclick="addSvTool()">＋ 添加工具行</button>
    <div class="mbtns">
      <button class="mbtn mbtn-save" onclick="saveSv()" id="sv-save-btn">保存</button>
      <button class="mbtn mbtn-cancel" onclick="closeSvModal()" id="sv-cancel-btn">取消</button>
    </div>
    <div id="sv-del-wrap" style="display:none">
      <button class="mbtn mbtn-del" onclick="deleteSv()" style="width:100%;margin-top:10px">删除此服务器</button>
    </div>
  </div>
</div>

<div class="section" id="s-oneshot">一次性命令</div>
<div class="oneshot-trigger">
  <button onclick="openCmdModal()" id="oneshot-btn">⌨ 输入命令并运行…</button>
</div>

<div class="section" id="s-mem">服务器内存</div>
<div class="mem-bar-wrap"><div class="mem-bar" id="mem-bar"></div></div>
<div class="mem-info"><span id="mem-text">加载中…</span><span id="mem-pct"></span></div>
<div class="mem-warn" id="mem-warn">⚠️ 内存使用超过 80%，建议关闭闲置会话。</div>

<div class="section">
  <span id="s-sessions">活跃会话</span><span class="cnt" id="scnt"></span>
</div>
<div class="list" id="session-list"><div class="empty" id="no-sessions">暂无活跃会话</div></div>

<div style="height:32px"></div>

<!-- One-shot command modal -->
<div class="modal" id="cmd-modal" onclick="if(event.target===this)closeCmdModal()">
  <div class="mbox">
    <div class="mtitle" id="cmd-modal-title">一次性命令</div>
    <input class="mfield" id="custom-cmd" placeholder="例如：python3 /opt/test.py" autocomplete="off">
    <div class="mbtns" style="margin-top:4px">
      <button class="mbtn mbtn-save" onclick="runCustom()" id="cmd-run-btn">运行</button>
      <button class="mbtn mbtn-cancel" onclick="closeCmdModal()" id="cmd-cancel-btn">取消</button>
    </div>
  </div>
</div>

<!-- Shortcut modal -->
<div class="modal" id="sc-modal" onclick="if(event.target===this)closeScModal()">
  <div class="mbox">
    <div class="mtitle" id="sc-modal-title">添加快捷方式</div>
    <input class="mfield" id="sc-name" placeholder="名称（如：调研任务）">
    <input class="mfield" id="sc-cmd" placeholder="命令（如：claude）">
    <select class="mselect" id="sc-user">
      {% for u in sc_users %}<option value="{{u}}">{{u}}</option>{% endfor %}
    </select>
    <label class="mcheck-row">
      <input type="checkbox" id="sc-pinned">
      <span id="sc-pin-lbl">置顶到工具区</span>
    </label>
    <div class="mbtns">
      <button class="mbtn mbtn-save" onclick="saveShortcut()" id="sc-save-btn">保存</button>
      <button class="mbtn mbtn-cancel" onclick="closeScModal()" id="sc-cancel-btn">取消</button>
    </div>
    <div id="sc-del-wrap" style="display:none">
      <button class="mbtn mbtn-del" onclick="deleteShortcut()" id="sc-del-btn">删除此快捷方式</button>
    </div>
  </div>
</div>

<!-- Sort modal -->
<div class="modal" id="sort-modal" onclick="if(event.target===this)closeSortModal()">
  <div class="mbox">
    <div class="mtitle" id="sort-modal-title">快捷方式排序</div>
    <div id="sort-list" style="max-height:55vh;overflow-y:auto"></div>
    <div class="mbtns">
      <button class="mbtn mbtn-save" onclick="saveSortOrder()" id="sort-save-btn">保存</button>
      <button class="mbtn mbtn-cancel" onclick="closeSortModal()" id="sort-cancel-btn">取消</button>
    </div>
  </div>
</div>

<script>
// ── i18n ─────────────────────────────────────────────────────────────────────
const I18N = {
  zh: {
    title:'AI 工作台', logout:'退出', langBtn:'EN',
    sTools:'工具', sShortcuts:'我的快捷', scAdd:'＋ 添加',
    sOneshot:'一次性命令', oneshotBtn:'⌨ 输入命令并运行…',
    sMem:'服务器内存', memLoading:'加载中…',
    memWarn:'⚠️ 内存使用超过 80%，建议关闭闲置会话。',
    sSessions:'活跃会话', noSessions:'暂无活跃会话',
    btnNew:'新建', btnResume:'恢复', btnOpen:'打开', btnClose:'关闭',
    btnEdit:'编辑', btnLaunch:'启动',
    cmdTitle:'一次性命令', cmdPlaceholder:'例如：python3 /opt/test.py',
    cmdRun:'运行', cmdCancel:'取消',
    scTitleAdd:'添加快捷方式', scTitleEdit:'编辑快捷方式',
    scNamePh:'名称（如：调研任务）', scCmdPh:'命令（如：claude）',
    scPinLbl:'置顶到工具区',
    scSave:'保存', scCancel:'取消', scDel:'删除此快捷方式',
    confirmClose:'确定关闭这个会话？',
    confirmDel:'确定删除此快捷方式？',
    errRequired:'名称和命令不能为空',
    errFailed:'操作失败',
    sortTitle:'快捷方式排序', sortSave:'保存', sortCancel:'取消', sortBtn:'↕ 排序',
  },
  en: {
    title:'AI Launcher', logout:'Sign out', langBtn:'中',
    sTools:'Tools', sShortcuts:'Shortcuts', scAdd:'＋ Add',
    sOneshot:'One-shot command', oneshotBtn:'⌨ Type a command to run…',
    sMem:'Server Memory', memLoading:'Loading…',
    memWarn:'⚠️ Memory above 80% — consider closing idle sessions.',
    sSessions:'Active sessions', noSessions:'No active sessions',
    btnNew:'New', btnResume:'Resume', btnOpen:'Open', btnClose:'Close',
    btnEdit:'Edit', btnLaunch:'Launch',
    cmdTitle:'One-shot command', cmdPlaceholder:'e.g. python3 /opt/test.py',
    cmdRun:'Run', cmdCancel:'Cancel',
    scTitleAdd:'Add shortcut', scTitleEdit:'Edit shortcut',
    scNamePh:'Name (e.g. Research)', scCmdPh:'Command (e.g. claude)',
    scPinLbl:'Pin to tools area',
    scSave:'Save', scCancel:'Cancel', scDel:'Delete shortcut',
    confirmClose:'Close this session?',
    confirmDel:'Delete this shortcut?',
    errRequired:'Name and command are required',
    errFailed:'Failed',
    sortTitle:'Sort shortcuts', sortSave:'Save', sortCancel:'Cancel', sortBtn:'↕ Sort',
  }
};
let _lang = localStorage.getItem('ai_lang') || 'zh';
function t(k){ return (I18N[_lang]||I18N.zh)[k]||k; }
function applyLang(){
  document.documentElement.lang = _lang;
  document.getElementById('h-title').textContent    = t('title');
  document.getElementById('h-logout').textContent   = t('logout');
  document.getElementById('lang-btn').textContent   = t('langBtn');
  document.getElementById('s-tools').textContent    = t('sTools');
  document.getElementById('s-shortcuts').textContent= t('sShortcuts');
  document.getElementById('sc-add-btn').textContent = t('scAdd');
  document.getElementById('s-oneshot').textContent  = t('sOneshot');
  document.getElementById('oneshot-btn').textContent= t('oneshotBtn');
  document.getElementById('s-mem').textContent      = t('sMem');
  document.getElementById('s-sessions').textContent = t('sSessions');
  document.getElementById('mem-warn').textContent   = t('memWarn');
  const ns = document.getElementById('no-sessions');
  if(ns) ns.textContent = t('noSessions');
  document.getElementById('cmd-modal-title').textContent = t('cmdTitle');
  document.getElementById('custom-cmd').placeholder = t('cmdPlaceholder');
  document.getElementById('cmd-run-btn').textContent = t('cmdRun');
  document.getElementById('cmd-cancel-btn').textContent = t('cmdCancel');
  document.getElementById('sc-pin-lbl').textContent  = t('scPinLbl');
  document.getElementById('sort-btn').textContent    = t('sortBtn');
  document.getElementById('sort-modal-title').textContent = t('sortTitle');
  document.getElementById('sort-save-btn').textContent    = t('sortSave');
  document.getElementById('sort-cancel-btn').textContent  = t('sortCancel');
  // tool buttons
  document.querySelectorAll('[data-i18n="new"]').forEach(el=>el.textContent=t('btnNew'));
  document.querySelectorAll('[data-i18n="resume"]').forEach(el=>el.textContent=t('btnResume'));
}
function toggleLang(){
  _lang = _lang==='zh'?'en':'zh';
  localStorage.setItem('ai_lang', _lang);
  applyLang();
  renderScs();
  loadSessions();
}
applyLang();

// ── Utilities ─────────────────────────────────────────────────────────────────
function relTime(ts){
  const d=Math.floor(Date.now()/1000-ts);
  if(d<60)return d+(t('langBtn')==='EN'?'秒前':'s ago');
  if(d<3600)return Math.floor(d/60)+(t('langBtn')==='EN'?'分钟前':'m ago');
  if(d<86400)return Math.floor(d/3600)+(t('langBtn')==='EN'?'小时前':'h ago');
  return Math.floor(d/86400)+(t('langBtn')==='EN'?'天前':'d ago');
}
function _estSize(){
  return {cols:Math.max(40,Math.floor(window.innerWidth/8.4)),
          rows:Math.max(20,Math.floor((window.innerHeight-90)/17))};
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// ── Sessions ──────────────────────────────────────────────────────────────────
async function newSession(toolId,resume){
  const sz=_estSize();
  const r=await fetch('/api/new',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tool_id:toolId,resume,...sz})});
  const d=await r.json();
  if(d.ok){location.href='/t/'+d.name}else{alert(d.error||t('errFailed'))}
}
async function closeSession(name){
  if(!confirm(t('confirmClose')))return;
  await fetch('/api/close/'+name,{method:'POST'});
  loadSessions();
}
async function loadSessions(){
  const r=await fetch('/api/sessions');
  const list=await r.json();
  document.getElementById('scnt').textContent=list.length?'('+list.length+')':'';
  const el=document.getElementById('session-list');
  if(!list.length){el.innerHTML='<div class="empty">'+t('noSessions')+'</div>';return;}
  el.innerHTML=list.map(s=>`
    <div class="card">
      <div class="dot" style="background:${s.color}"></div>
      <div class="info">
        <div class="session-label">${esc(s.label)}</div>
        <div class="session-time">${relTime(s.created_at)}</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="location.href='/t/${s.name}'">${t('btnOpen')}</button>
        <button class="btn btn-danger" onclick="closeSession('${s.name}')">${t('btnClose')}</button>
      </div>
    </div>`).join('');
}

// ── Memory ────────────────────────────────────────────────────────────────────
async function loadMemory(){
  const r=await fetch('/api/memory');
  const d=await r.json();
  const bar=document.getElementById('mem-bar');
  bar.style.width=d.pct+'%';
  bar.style.background=d.pct>=80?'#e53935':d.pct>=60?'#fb8c00':'#43a047';
  document.getElementById('mem-text').textContent=`${d.used_mb} MB / ${d.total_mb} MB`;
  document.getElementById('mem-pct').textContent=d.pct+'%';
  document.getElementById('mem-warn').style.display=d.pct>=80?'block':'none';
}

// ── One-shot command modal ────────────────────────────────────────────────────
function openCmdModal(){
  document.getElementById('cmd-modal').classList.add('show');
  setTimeout(()=>document.getElementById('custom-cmd').focus(),100);
}
function closeCmdModal(){
  document.getElementById('cmd-modal').classList.remove('show');
  document.getElementById('custom-cmd').value='';
}
async function runCustom(){
  const cmd=document.getElementById('custom-cmd').value.trim();
  if(!cmd)return;
  const sz=_estSize();
  const r=await fetch('/api/new',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd,...sz})});
  const d=await r.json();
  if(d.ok){location.href='/t/'+d.name}else{alert(d.error||t('errFailed'))}
}
document.getElementById('custom-cmd').addEventListener('keydown',e=>{
  if(e.key==='Enter') runCustom();
  if(e.key==='Escape') closeCmdModal();
});

// ── Shortcuts ─────────────────────────────────────────────────────────────────
let _scs = [], _scEditId = null;

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
        <button class="btn btn-primary" onclick="launchSc('${s.id}')">${t('btnLaunch')}</button>
        <button class="btn btn-secondary" onclick="openScModal('${s.id}')">${t('btnEdit')}</button>
      </div>
    </div>`).join('');
  document.getElementById('my-sc-list').innerHTML = free.length ? free.map(s=>`
    <div class="card">
      <div class="dot" style="background:${s.color}"></div>
      <div class="info">
        <div class="name">${esc(s.name)}</div>
        <div class="desc">${esc(s.cmd)}${s.user!=='root'?' · '+esc(s.user):''}</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" onclick="launchSc('${s.id}')">${t('btnLaunch')}</button>
        <button class="btn btn-secondary" onclick="openScModal('${s.id}')">${t('btnEdit')}</button>
      </div>
    </div>`).join('') : '';
}

function openScModal(id=null){
  _scEditId = id;
  const editing = !!id;
  document.getElementById('sc-modal-title').textContent = t(editing?'scTitleEdit':'scTitleAdd');
  document.getElementById('sc-del-wrap').style.display = editing?'block':'none';
  document.getElementById('sc-save-btn').textContent   = t('scSave');
  document.getElementById('sc-cancel-btn').textContent = t('scCancel');
  document.getElementById('sc-del-btn') && (document.getElementById('sc-del-btn').textContent=t('scDel'));
  document.getElementById('sc-name').placeholder = t('scNamePh');
  document.getElementById('sc-cmd').placeholder  = t('scCmdPh');
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
  if(!name||!cmd){ alert(t('errRequired')); return; }
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
  if(!confirm(t('confirmDel'))) return;
  await fetch('/api/shortcuts/'+_scEditId,{method:'DELETE'});
  closeScModal();
  loadShortcuts();
}

async function launchSc(id){
  const sz = _estSize();
  const r = await fetch('/api/new',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({shortcut_id:id,...sz})});
  const d = await r.json();
  if(d.ok){ location.href='/t/'+d.name; } else { alert(d.error||t('errFailed')); }
}

// ── Sort shortcuts ────────────────────────────────────────────────────────────
let _sortOrder = [];
function openSortModal(){
  if(!_scs.length){ alert(_lang==='zh'?'暂无快捷方式':'No shortcuts yet'); return; }
  _sortOrder = [..._scs];
  renderSortList();
  document.getElementById('sort-modal').classList.add('show');
}
function closeSortModal(){ document.getElementById('sort-modal').classList.remove('show'); }
function renderSortList(){
  const el = document.getElementById('sort-list');
  const n = _sortOrder.length;
  el.innerHTML = _sortOrder.map((s,i)=>`
    <div class="sort-item">
      <div class="sort-dot" style="background:${s.color}"></div>
      <div class="sort-name">${esc(s.name)}${s.pinned?`<span class="sort-pin">${_lang==='zh'?'置顶':'pinned'}</span>`:''}</div>
      <div class="sort-arrows">
        <button onclick="moveSort(${i},-1)" ${i===0?'disabled':''}>↑</button>
        <button onclick="moveSort(${i},1)" ${i===n-1?'disabled':''}>↓</button>
      </div>
    </div>`).join('');
}
function moveSort(i,dir){
  const j=i+dir;
  if(j<0||j>=_sortOrder.length)return;
  [_sortOrder[i],_sortOrder[j]]=[_sortOrder[j],_sortOrder[i]];
  renderSortList();
}
async function saveSortOrder(){
  const ids=_sortOrder.map(s=>s.id);
  await fetch('/api/shortcuts/reorder',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
  closeSortModal();
  loadShortcuts();
}

// ── Server management ─────────────────────────────────────────────────────────
let _servers = [], _svEditId = null, _svToolDraft = [];

async function loadServers(){
  try{
    const r = await fetch('/api/servers');
    _servers = await r.json();
    renderServers();
  }catch(e){}
}

function renderServers(){
  const el = document.getElementById('sv-list');
  if(!_servers.length){ el.innerHTML=''; return; }
  el.innerHTML = _servers.map(sv=>`
    <div class="card">
      <div class="dot" style="background:${sv.color}"></div>
      <div class="info">
        <div class="name">${esc(sv.name)}${sv.has_key?'<span class="sv-badge">SSH Key</span>':''}</div>
        <div class="desc">${esc(sv.user)}@${esc(sv.host)}${sv.port!=22?':'+sv.port:''}</div>
        ${sv.tools.length?`<div class="sv-tools-row">${sv.tools.map(t=>
          `<button class="btn btn-primary" onclick="launchSvTool('${sv.id}',${JSON.stringify(t.cmd)},${JSON.stringify(sv.name+' · '+t.label)},'${sv.color}')">${esc(t.label)||'连接'}</button>`
        ).join('')}</div>`:'<div class="sv-tools-row"><button class="btn btn-primary" onclick="launchSvTool(\''+sv.id+'\',\'bash\','+JSON.stringify(sv.name)+',\''+sv.color+'\')">连接 Shell</button></div>'}
      </div>
      <div class="actions">
        <button class="btn btn-secondary" onclick="openSvModal('${sv.id}')">编辑</button>
      </div>
    </div>`).join('');
}

function openSvModal(id=null){
  _svEditId = id;
  const editing = !!id;
  document.getElementById('sv-modal-title').textContent = editing?'编辑服务器':'添加服务器';
  document.getElementById('sv-del-wrap').style.display = editing?'block':'none';
  document.getElementById('sv-key-hint').style.display = 'none';
  document.getElementById('sv-clear-key').checked = false;
  if(editing){
    const sv = _servers.find(s=>s.id===id)||{};
    document.getElementById('sv-name').value = sv.name||'';
    document.getElementById('sv-host').value = sv.host||'';
    document.getElementById('sv-user').value = sv.user||'';
    document.getElementById('sv-port').value = sv.port||22;
    document.getElementById('sv-key').value = '';
    document.getElementById('sv-key-hint').style.display = sv.has_key?'block':'none';
    _svToolDraft = JSON.parse(JSON.stringify(sv.tools||[]));
  } else {
    document.getElementById('sv-name').value='';
    document.getElementById('sv-host').value='';
    document.getElementById('sv-user').value='';
    document.getElementById('sv-port').value='22';
    document.getElementById('sv-key').value='';
    _svToolDraft=[];
  }
  renderSvTools();
  document.getElementById('sv-modal').classList.add('show');
  document.getElementById('sv-name').focus();
}
function closeSvModal(){ document.getElementById('sv-modal').classList.remove('show'); }

function addSvTool(){ _svToolDraft.push({label:'',cmd:''}); renderSvTools(); }
function removeSvTool(i){ _svToolDraft.splice(i,1); renderSvTools(); }
function renderSvTools(){
  const el = document.getElementById('sv-tools-list');
  el.innerHTML = _svToolDraft.map((t,i)=>`
    <div class="sv-tool-entry">
      <input class="mfield" value="${esc(t.label)}" placeholder="工具名（如 Claude）"
             oninput="_svToolDraft[${i}].label=this.value">
      <input class="mfield" value="${esc(t.cmd)}" placeholder="命令（如 claude）"
             oninput="_svToolDraft[${i}].cmd=this.value">
      <button class="sv-tool-del" onclick="removeSvTool(${i})">×</button>
    </div>`).join('');
}

async function saveSv(){
  const name = document.getElementById('sv-name').value.trim();
  const host = document.getElementById('sv-host').value.trim();
  const user = document.getElementById('sv-user').value.trim();
  const port = document.getElementById('sv-port').value.trim()||'22';
  const ssh_key = document.getElementById('sv-key').value.trim();
  const clear_key = document.getElementById('sv-clear-key').checked;
  if(!name||!host||!user){ alert('名称、地址、用户名不能为空'); return; }
  const body = {name,host,user,port:parseInt(port),tools:_svToolDraft,ssh_key,clear_key};
  if(_svEditId){
    await fetch('/api/servers/'+_svEditId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  } else {
    await fetch('/api/servers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  }
  closeSvModal(); loadServers();
}

async function deleteSv(){
  if(!confirm('删除此服务器配置？')) return;
  await fetch('/api/servers/'+_svEditId,{method:'DELETE'});
  closeSvModal(); loadServers();
}

async function launchSvTool(svId, toolCmd, label, color){
  const sv = _servers.find(s=>s.id===svId);
  if(!sv) return;
  const safe = toolCmd.replace(/'/g,"'\\''");
  const keyFlag = sv.has_key ? `-i /opt/ai-launcher/ssh_keys/${svId}.pem ` : '';
  const cmd = `ssh -t -p ${sv.port} ${keyFlag}-o StrictHostKeyChecking=no -o ConnectTimeout=10 ${sv.user}@${sv.host} '${safe}; exec bash'`;
  const sz = _estSize();
  const r = await fetch('/api/new',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd,...sz})});
  const d = await r.json();
  if(d.ok){
    await fetch('/api/sessions/'+d.name,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({label})});
    location.href='/t/'+d.name;
  } else { alert(d.error||'连接失败'); }
}

loadSessions();
loadShortcuts();
loadServers();
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
body{position:fixed;top:0;left:0;right:0;bottom:0;background:#1e1e1e;display:flex;flex-direction:column;overflow:hidden}
.toolbar{background:#fff;border-bottom:1px solid #e5e7eb;padding:8px 12px;display:flex;flex-wrap:wrap;align-items:center;gap:6px;flex-shrink:0}
.trow1{display:flex;align-items:center;gap:8px;width:100%}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.label{flex:1;font-size:14px;font-weight:600;color:#111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;cursor:pointer;user-select:none}
.label:active{opacity:.6}
.label-input{display:none;flex:1;background:transparent;border:none;border-bottom:2px solid #4f86c6;color:#111;font-size:14px;font-weight:600;outline:none;padding:2px 0;min-width:60px;max-width:200px}
.notif-on{background:#1a3a1a!important;color:#7ecf7e!important}
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
.sesbar{display:flex;overflow-x:auto;background:#252525;border-bottom:1px solid #3a3a3a;padding:4px 8px;gap:5px;flex-shrink:0;-webkit-overflow-scrolling:touch}
.sesbar::-webkit-scrollbar{display:none}
.seschip{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:12px;font-size:11px;font-weight:600;white-space:nowrap;cursor:pointer;border:1.5px solid #555;background:#3a3a3a;color:#bbb;flex-shrink:0;user-select:none}
.seschip.cur{border-color:var(--sc);color:var(--sc);background:#2a2a2a}
.seschip:active{opacity:.6}
.sesdot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.kbtn-edit{background:#e8f0fe;border-color:#c5d8f8;color:#1565c0;margin-left:2px}
.follow-btn{position:fixed;bottom:60px;right:14px;background:rgba(20,20,20,.82);color:#fff;border:none;border-radius:18px;padding:7px 14px;font-size:12px;font-weight:600;cursor:pointer;display:none;z-index:50;box-shadow:0 2px 8px rgba(0,0,0,.25);backdrop-filter:blur(4px)}
.kb-section{font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.8px;margin:12px 0 6px}
.kb-toggle-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid #f0f0f0;font-size:14px;color:#333}
.kb-toggle-row:last-child{border-bottom:none}
.kbtoggle{width:40px;height:22px;border-radius:11px;border:none;cursor:pointer;transition:background .2s;flex-shrink:0}
.kbtoggle.on{background:#111}.kbtoggle.off{background:#ccc}
.kb-custom-item{display:flex;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid #f0f0f0}
.kb-custom-item:last-child{border-bottom:none}
.kb-custom-label{flex:1;font-size:14px;color:#333;min-width:0;word-break:break-all}
.kb-del{border:none;background:#fdecea;color:#d32f2f;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;flex-shrink:0}
.kb-add-row{display:flex;gap:6px;margin-top:10px;align-items:center}
.kb-add-row .mfield{margin-bottom:0;flex:1;min-width:0}
.kb-add-btn{padding:12px 14px;background:#111;color:#fff;border:none;border-radius:10px;font-size:18px;cursor:pointer;flex-shrink:0;line-height:1}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:200;align-items:flex-end}
.modal.show{display:flex}
.mbox{background:#fff;border-radius:20px 20px 0 0;padding:20px 16px 36px;width:100%}
.mtitle{font-size:16px;font-weight:700;margin-bottom:16px;color:#111}
.mfield{display:block;width:100%;margin-bottom:12px;padding:12px;border:1.5px solid #e5e7eb;border-radius:10px;font-size:14px;outline:none;background:#fff}
.mfield:focus{border-color:#4f86c6}
.mbtns{display:flex;gap:8px;margin-top:16px}
.mbtn{flex:1;padding:12px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer}
.mbtn-save{background:#111;color:#fff}
.mbtn-cancel{background:#f0f0f0;color:#555}
.hist-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:400;flex-direction:column}
.hist-modal.show{display:flex}
.hist-box{background:#1a1a1a;flex:1;display:flex;flex-direction:column;overflow:hidden}
.hist-bar{background:#252525;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;border-bottom:1px solid #3a3a3a;gap:8px}
.hist-title{font-size:14px;font-weight:600;color:#ccc}
.hist-close-btn{background:#444;color:#ccc;border:none;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;flex-shrink:0}
.hist-lines-sel{background:#3a3a3a;color:#ccc;border:1px solid #555;border-radius:6px;padding:4px 6px;font-size:12px}
.hist-content{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:10px}
.hist-pre{font-family:Menlo,Monaco,"Courier New",monospace;font-size:12px;line-height:1.55;color:#d4d4d4;white-space:pre-wrap;word-break:break-word;margin:0}
.confirmbar{display:flex;align-items:center;background:#2a2a2a;border-bottom:1px solid #444;padding:4px 8px;gap:5px;flex-shrink:0}
.cfbtn{width:36px;border:1px solid #555;border-radius:7px;background:#3a3a3a;color:#eee;font-size:15px;font-weight:700;padding:5px 0;cursor:pointer;user-select:none;text-align:center;flex-shrink:0}
.cfbtn:active{background:#555}
.cfbtn-enter{width:44px;background:#1a3a1a;border-color:#4a7a4a;color:#7ecf7e}
#pet-canvas{margin-left:auto;display:block;image-rendering:pixelated;cursor:default}
.theme-popup{display:none;position:fixed;top:58px;right:8px;background:#fff;border:1px solid #ddd;border-radius:12px;padding:14px;z-index:300;box-shadow:0 4px 20px rgba(0,0,0,.25);width:230px}
.theme-popup.show{display:block}
.theme-swatches{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.swatch{width:34px;height:34px;border-radius:8px;cursor:pointer;border:2px solid transparent;flex-shrink:0;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.swatch.active,.swatch:active{border-color:#4f86c6;box-shadow:0 0 0 3px rgba(79,134,198,.35)}
.swatch.edit-target{border-color:#f90!important;box-shadow:0 0 0 3px rgba(255,153,0,.45)!important}
.theme-custom-row{display:flex;align-items:center;gap:10px;font-size:13px;color:#555}
.theme-custom-row input[type=color]{width:38px;height:30px;border:1px solid #ddd;border-radius:6px;cursor:pointer;padding:1px}
</style>
</head>
<body>
<div class="toolbar">
  <div class="trow1">
    <div class="dot" style="background:{{ sess.color }}"></div>
    <div class="label" id="sess-label" onclick="startRename()" title="点击重命名">{{ sess.label }}</div>
    <input class="label-input" id="label-input" maxlength="40"
           onkeydown="if(event.key==='Enter')saveRename();else if(event.key==='Escape')cancelRename();"
           onblur="saveRename()">
    <div class="tbtns">
      <button class="tbtn t-back" onclick="location.href='/'">主页</button>
      {% if tool and tool.id %}
      <button class="tbtn t-new" onclick="newSame()">新建</button>
      {% endif %}
      <button class="tbtn t-fs" onclick="redraw()" style="background:#e8f5e9;color:#2e7d32">刷新</button>
      <button class="tbtn" id="notif-btn" onclick="toggleNotif()" title="开启通知" style="background:#3a3a3a;color:#ccc">🔕</button>
      <button class="tbtn" onclick="openHistory()" style="background:#2a2a2a;color:#ccc">历史</button>
      <button class="tbtn" id="theme-btn" onclick="toggleThemePopup();event.stopPropagation()" title="背景色" style="background:#3a3a3a;color:#eee;font-size:15px;padding:6px 9px">🎨</button>
      <button class="tbtn t-close" onclick="closeAndBack()">关闭</button>
    </div>
  </div>
</div>
<div class="sesbar" id="sesbar"></div>
<div class="confirmbar">
  <button class="cfbtn" onclick="sendKey('1\n')">1</button>
  <button class="cfbtn" onclick="sendKey('2\n')">2</button>
  <button class="cfbtn" onclick="sendKey('3\n')">3</button>
  <button class="cfbtn cfbtn-enter" onclick="sendKey('\r')">↵</button>
  <canvas id="pet-canvas" width="60" height="33"></canvas>
</div>
<div class="keybar" id="keybar"></div>
<button class="follow-btn" id="follow-btn" onclick="scrollToBottom()">⬇ 跟随实时</button>

<!-- Keybar edit modal -->
<div class="modal" id="kb-modal" onclick="if(event.target===this)closeKbModal()">
  <div class="mbox" style="max-height:80vh;overflow-y:auto;padding-bottom:40px">
    <div class="mtitle">编辑快捷键栏</div>
    <div class="kb-section">内置按键</div>
    <div id="kb-builtins-list"></div>
    <div class="kb-section">自定义按键</div>
    <div id="kb-custom-list"></div>
    <div class="kb-add-row">
      <input class="mfield" id="kb-new-label" placeholder="按键名称" maxlength="20">
      <input class="mfield" id="kb-new-key" placeholder="发送内容（↵=回车）">
      <button class="kb-add-btn" onclick="addKbCustom()">＋</button>
    </div>
    <div class="mbtns" style="margin-top:16px">
      <button class="mbtn mbtn-save" onclick="saveKb()">保存</button>
      <button class="mbtn mbtn-cancel" onclick="closeKbModal()">取消</button>
    </div>
  </div>
</div>

<!-- History viewer modal -->
<div class="hist-modal" id="hist-modal">
  <div class="hist-box">
    <div class="hist-bar">
      <span class="hist-title">历史记录</span>
      <select class="hist-lines-sel" id="hist-lines" onchange="loadHistory()">
        <option value="500">近 500 行</option>
        <option value="1500" selected>近 1500 行</option>
        <option value="3000">近 3000 行</option>
        <option value="5000">近 5000 行</option>
      </select>
      <button class="hist-close-btn" onclick="exportHistory()" style="background:#1a3a2a;color:#7ecf7e">导出</button>
      <button class="hist-close-btn" onclick="closeHistory()">关闭</button>
    </div>
    <div class="hist-content" id="hist-content">
      <div style="color:#555;text-align:center;padding:40px 0">加载中…</div>
    </div>
  </div>
</div>

<!-- Theme / background color popup -->
<div class="theme-popup" id="theme-popup" onclick="event.stopPropagation()">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <span style="font-size:13px;font-weight:600;color:#333">终端背景色</span>
    <button id="swatch-edit-btn" onclick="toggleSwatchEdit()" style="font-size:11px;padding:3px 8px;border:1px solid #ccc;border-radius:6px;cursor:pointer;background:#f5f5f5;color:#555;white-space:nowrap">✏ 编辑色块</button>
  </div>
  <div id="edit-hint" style="display:none;font-size:11px;color:#888;margin-bottom:6px">点色块选中（橙框），再用取色器修改颜色</div>
  <div class="theme-swatches" id="swatch-container"></div>
  <div class="theme-custom-row">
    <span>取色:</span>
    <input type="color" id="custom-bg-picker" value="#1e1e1e" oninput="onPickerInput(this.value)">
  </div>
</div>

<div id="term-wrap"></div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script>
const SESS = "{{ sess.name }}";
const TOOL = "{{ tool.id if tool and tool.id else '' }}";

const term = new Terminal({
  theme:{
    background:'#1e1e1e',foreground:'#d4d4d4',cursor:'#d4d4d4',cursorAccent:'#1e1e1e',
    selectionBackground:'#264f78',
    black:'#1e1e1e',red:'#f44747',green:'#4ec9b0',yellow:'#dcdcaa',
    blue:'#569cd6',magenta:'#c678dd',cyan:'#4ec9b0',white:'#d4d4d4',
    brightBlack:'#858585',brightRed:'#f44747',brightGreen:'#4ec9b0',brightYellow:'#dcdcaa',
    brightBlue:'#569cd6',brightMagenta:'#c678dd',brightCyan:'#4ec9b0',brightWhite:'#ffffff',
  },
  fontFamily:'Menlo,Monaco,"Courier New",monospace',
  fontSize:14,lineHeight:1.3,
  scrollback:3000,cursorBlink:true,allowTransparency:false,
  scrollOnUserInput:true,
});

const fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.open(document.getElementById('term-wrap'));


// Follow-bottom button: show when user has scrolled up
term.onScroll(() => {
  const buf = term.buffer.active;
  const atBottom = buf.viewportY >= buf.length - term.rows;
  document.getElementById('follow-btn').style.display = atBottom ? 'none' : 'block';
});
function scrollToBottom(){ term.scrollToBottom(); }

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
  ws.onmessage = e=>{ term.write(e.data); _onOutput(); };
  ws.onclose = ()=>{
    term.write('\r\n\x1b[33m[Disconnected — reconnecting in 2s...]\x1b[0m');
    reconnTimer = setTimeout(connect, 2000);
  };
  ws.onerror = ()=>{ ws.close(); };
}

connect();
term.onData(d=>{ if(ws && ws.readyState===1) ws.send(d); });
function sendKey(s){ if(ws && ws.readyState===1){ ws.send(s); term.focus(); if(window._petAlert)window._petAlert(); } }

// ── Copy-mode state ──────────────────────────────────────────────────────────
let _inCopyMode = false;
function enterCopyMode(){ _inCopyMode = true;  sendKey('\x02['); }
function exitCopyMode(){  _inCopyMode = false; sendKey('q'); }
function kbUp(){   sendKey(_inCopyMode ? '\x1b[A\x1b[A\x1b[A\x1b[A\x1b[A' : '\x1b[A'); }
function kbDown(){ sendKey(_inCopyMode ? '\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B' : '\x1b[B'); }

// ── Keybar ───────────────────────────────────────────────────────────────────
let _kbData = {builtins:[], hidden:[], custom:[]};
let _kbHiddenDraft = [], _kbCustomDraft = [];

function kbEsc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

async function loadKeybar(){
  try{
    const r = await fetch('/api/keybar');
    _kbData = await r.json();
  }catch(e){}
  renderKeybar();
}

function kbScroll(n){ term.scrollLines(n); }

// Map builtin IDs to special handlers (avoids quote/scope issues in onclick)
const _kbSpecial = {
  arrow_up:    'kbUp()',
  arrow_down:  'kbDown()',
};

function renderKeybar(){
  const bar = document.getElementById('keybar');
  let html = '';
  for(const b of _kbData.builtins){
    if(_kbData.hidden.includes(b.id)) continue;
    let cls = 'kbtn' + (b.id==='ctrl_c'?' kbtn-danger':'');
    const special = _kbSpecial[b.id];
    if(special){
      html += `<button class="${cls}" onclick="${special}">${kbEsc(b.label)}</button>`;
    } else if(b.type==='scroll'){
      html += `<button class="${cls}" onclick="kbScroll(${b.lines})">${kbEsc(b.label)}</button>`;
    } else {
      html += `<button class="${cls}" onclick='sendKey(${JSON.stringify(b.key)})'>${kbEsc(b.label)}</button>`;
    }
  }
  for(const c of _kbData.custom){
    html += `<button class="kbtn" onclick='sendKey(${JSON.stringify(c.key)})'>${kbEsc(c.label)}</button>`;
  }
  html += `<button class="kbtn kbtn-edit" onclick="openKbModal()">✎</button>`;
  bar.innerHTML = html;
}

function openKbModal(){
  _kbHiddenDraft = [...(_kbData.hidden||[])];
  _kbCustomDraft = JSON.parse(JSON.stringify(_kbData.custom||[]));
  document.getElementById('kb-builtins-list').innerHTML = (_kbData.builtins||[]).map(b=>{
    const on = !_kbHiddenDraft.includes(b.id);
    return `<div class="kb-toggle-row"><span>${kbEsc(b.label)}</span>
      <button class="kbtoggle ${on?'on':'off'}" id="kbt-${b.id}" onclick="toggleKbBuiltin('${b.id}')"></button></div>`;
  }).join('');
  renderKbCustomList();
  document.getElementById('kb-modal').classList.add('show');
}
function closeKbModal(){ document.getElementById('kb-modal').classList.remove('show'); }

function toggleKbBuiltin(id){
  const idx = _kbHiddenDraft.indexOf(id);
  if(idx>=0) _kbHiddenDraft.splice(idx,1); else _kbHiddenDraft.push(id);
  const btn = document.getElementById('kbt-'+id);
  btn.className = 'kbtoggle '+(_kbHiddenDraft.includes(id)?'off':'on');
}

function renderKbCustomList(){
  const el = document.getElementById('kb-custom-list');
  el.innerHTML = _kbCustomDraft.length ? _kbCustomDraft.map((c,i)=>
    `<div class="kb-custom-item">
      <span class="kb-custom-label">${kbEsc(c.label)}<br>
        <small style="color:#999">${kbEsc(c.key.replace(/\n/g,'↵').replace(/\r/g,'↵'))}</small>
      </span>
      <button class="kb-del" onclick="delKbCustom(${i})">删除</button>
    </div>`).join('')
    : '<div style="color:#ccc;font-size:13px;padding:8px 0">暂无自定义按键</div>';
}

function addKbCustom(){
  const label = document.getElementById('kb-new-label').value.trim();
  const raw   = document.getElementById('kb-new-key').value;
  if(!label || raw===''){alert('请填写按键名称和发送内容');return;}
  // Replace literal ↵ with \n
  const key = raw.replace(/↵/g,'\n');
  _kbCustomDraft.push({id:Math.random().toString(36).slice(2,10), label, type:'key', key});
  document.getElementById('kb-new-label').value='';
  document.getElementById('kb-new-key').value='';
  renderKbCustomList();
}

function delKbCustom(i){ _kbCustomDraft.splice(i,1); renderKbCustomList(); }

async function saveKb(){
  await fetch('/api/keybar',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({hidden:_kbHiddenDraft,custom:_kbCustomDraft})});
  _kbData.hidden = _kbHiddenDraft;
  _kbData.custom = _kbCustomDraft;
  closeKbModal();
  renderKeybar();
}

// ── Session rename ────────────────────────────────────────────────────────────
let _renaming = false;
function startRename(){
  if(_renaming) return;
  _renaming = true;
  const lbl = document.getElementById('sess-label');
  const inp = document.getElementById('label-input');
  inp.value = lbl.textContent.trim();
  lbl.style.display = 'none';
  inp.style.display = 'block';
  inp.focus(); inp.select();
}
async function saveRename(){
  if(!_renaming) return;
  _renaming = false;
  const lbl = document.getElementById('sess-label');
  const inp = document.getElementById('label-input');
  const v = inp.value.trim() || lbl.textContent.trim();
  inp.style.display = 'none';
  lbl.style.display = '';
  lbl.textContent = v;
  document.title = v;
  await fetch(`/api/sessions/${SESS}`, {method:'PUT',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({label:v})});
}
function cancelRename(){
  if(!_renaming) return;
  _renaming = false;
  document.getElementById('label-input').style.display = 'none';
  document.getElementById('sess-label').style.display = '';
}

// ── Output notifications ──────────────────────────────────────────────────────
let _unread = false, _notifEnabled = false;
const _origTitle = document.title;
function _updateNotifBtn(){
  const btn = document.getElementById('notif-btn');
  if(!btn) return;
  btn.textContent = _notifEnabled ? '🔔' : '🔕';
  btn.title = _notifEnabled ? '通知已开启（点击关闭）' : '开启通知';
  btn.classList.toggle('notif-on', _notifEnabled);
}
async function toggleNotif(){
  if(_notifEnabled){
    _notifEnabled = false; localStorage.setItem('notif','0'); _updateNotifBtn(); return;
  }
  if(!('Notification' in window)){ alert('此浏览器不支持系统通知'); return; }
  const p = await Notification.requestPermission();
  _notifEnabled = (p === 'granted');
  localStorage.setItem('notif', _notifEnabled?'1':'0');
  _updateNotifBtn();
  if(!_notifEnabled) alert('请在浏览器设置中允许通知权限');
}
function _onOutput(){
  if(!document.hidden) return;
  if(!_unread){ _unread = true; document.title = '⚡ ' + _origTitle; }
  if(_notifEnabled && Notification.permission==='granted'){
    new Notification('AI 工作台有新输出', {
      body: document.getElementById('sess-label')?.textContent || SESS,
      icon: '/static/icon.svg', tag:'ai-out', silent:true,
    });
  }
}
document.addEventListener('visibilitychange', ()=>{
  if(!document.hidden && _unread){ _unread=false; document.title=_origTitle; }
});
_notifEnabled = localStorage.getItem('notif')==='1' && Notification.permission==='granted';
_updateNotifBtn();

// ── Export history ────────────────────────────────────────────────────────────
async function exportHistory(){
  const lines = document.getElementById('hist-lines')?.value || 5000;
  try{
    const r = await fetch(`/api/history/${SESS}?lines=${lines}`);
    const d = await r.json();
    if(!d.ok){ alert('导出失败'); return; }
    const plain = d.text.replace(/\x1b\[[0-9;]*[A-Za-z]/g,'').replace(/\x1b[^\x1b]*/g,'');
    const blob = new Blob([plain], {type:'text/plain;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const label = (document.getElementById('sess-label')?.textContent||'session').trim().replace(/[/\\:*?"<>|]/g,'-');
    const dt = new Date().toISOString().slice(0,16).replace('T','_').replace(':','-');
    a.href=url; a.download=`${label}_${dt}.txt`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  }catch(e){ alert('导出失败'); }
}

// ── History viewer ────────────────────────────────────────────────────────────
function _ansiToHtml(raw){
  const clean=raw
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g,'')
    .replace(/\x1b\[[0-9;]*[ABCDEFGHIJKLMPSTXZnsu]/g,'')
    .replace(/\x1b[()[B][AB012]/g,'')
    .replace(/\x1b[^[\]()m]/g,'');
  const FG={30:'#858585',31:'#f44747',32:'#4ec9b0',33:'#dcdcaa',
            34:'#569cd6',35:'#c678dd',36:'#4ec9b0',37:'#d4d4d4',
            90:'#858585',91:'#f44747',92:'#4ec9b0',93:'#dcdcaa',
            94:'#569cd6',95:'#c678dd',96:'#4ec9b0',97:'#fff'};
  const BG={40:'#000',41:'#5a0000',42:'#005a00',43:'#5a5a00',
            44:'#00005a',45:'#5a005a',46:'#005a5a',47:'#d4d4d4'};
  let fg='',bg='',bold=false,out='',sp=false;
  const x=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const cls=()=>{if(sp){out+='</span>';sp=false;}};
  const opn=()=>{const s=(fg?`color:${fg};`:'')+( bg?`background:${bg};`:'')+( bold?'font-weight:700;':'');if(s){out+=`<span style="${s}">`;sp=true;}};
  const parts=clean.split(/\x1b\[([0-9;]*)m/);
  for(let i=0;i<parts.length;i++){
    if(i%2===0){if(parts[i])out+=x(parts[i]);}
    else{
      cls();
      for(const c of(parts[i]||'0').split(';').map(Number)){
        if(c===0){fg='';bg='';bold=false;}
        else if(c===1)bold=true;else if(c===22)bold=false;
        else if(FG[c])fg=FG[c];else if(BG[c])bg=BG[c];
        else if(c===39)fg='';else if(c===49)bg='';
      }
      opn();
    }
  }
  cls(); return out;
}
async function loadHistory(){
  const lines=document.getElementById('hist-lines').value;
  document.getElementById('hist-content').innerHTML='<div style="color:#555;text-align:center;padding:40px 0">加载中…</div>';
  try{
    const r=await fetch(`/api/history/${SESS}?lines=${lines}`);
    const d=await r.json();
    if(!d.ok){document.getElementById('hist-content').innerHTML='<div style="color:#f44;padding:20px">加载失败</div>';return;}
    const pre=document.createElement('pre');
    pre.className='hist-pre';
    pre.innerHTML=_ansiToHtml(d.text);
    const box=document.getElementById('hist-content');
    box.innerHTML=''; box.appendChild(pre);
    box.scrollTop=box.scrollHeight;
  }catch(e){document.getElementById('hist-content').innerHTML='<div style="color:#f44;padding:20px">加载失败</div>';}
}
function openHistory(){ document.getElementById('hist-modal').classList.add('show'); loadHistory(); }
function closeHistory(){ document.getElementById('hist-modal').classList.remove('show'); }

// ── Background color picker ──────────────────────────────────────────────────
const _DEFAULT_SWATCHES = ['#1e1e1e','#0a192f','#2d1b69','#0d3b00','#3b0000','#1a1200'];
let _swatches = (()=>{ try{ return JSON.parse(localStorage.getItem('term_swatches'))||_DEFAULT_SWATCHES; }catch(e){ return [..._DEFAULT_SWATCHES]; } })();
let _swatchEditMode=false, _swatchEditIdx=-1, _savedBg='#1e1e1e';

function _saveSwatches(){ localStorage.setItem('term_swatches', JSON.stringify(_swatches)); }

function renderSwatches(){
  const c = document.getElementById('swatch-container');
  if(!c) return;
  c.innerHTML = _swatches.map((col,i)=>{
    let cls = 'swatch';
    if(col===_savedBg) cls+=' active';
    if(_swatchEditMode && i===_swatchEditIdx) cls+=' edit-target';
    return `<div class="${cls}" data-color="${col}" style="background:${col}" onclick="swatchClick(${i})" title="${col}"></div>`;
  }).join('');
}

function swatchClick(i){
  if(_swatchEditMode){
    _swatchEditIdx = i;
    document.getElementById('custom-bg-picker').value = _swatches[i];
    renderSwatches();
  } else {
    applyBg(_swatches[i]);
  }
}

function toggleSwatchEdit(){
  _swatchEditMode = !_swatchEditMode;
  _swatchEditIdx = -1;
  const btn = document.getElementById('swatch-edit-btn');
  btn.textContent = _swatchEditMode ? '完成' : '✏ 编辑色块';
  btn.style.background = _swatchEditMode ? '#4f86c6' : '#f5f5f5';
  btn.style.color = _swatchEditMode ? '#fff' : '#555';
  document.getElementById('edit-hint').style.display = _swatchEditMode ? 'block' : 'none';
  renderSwatches();
}

function onPickerInput(color){
  if(_swatchEditMode && _swatchEditIdx >= 0){
    _swatches[_swatchEditIdx] = color;
    _saveSwatches();
    renderSwatches();
  }
  applyBg(color);
}

function applyBg(color){
  _savedBg = color;
  localStorage.setItem('term_bg', color);
  const picker = document.getElementById('custom-bg-picker');
  if(picker) picker.value = color;
  renderSwatches();
  term.options.theme = {...(term.options.theme||{}), background:color, cursorAccent:color};
  document.body.style.background = color;
  document.getElementById('term-wrap').style.background = color;
  term.refresh(0, term.rows-1);
}

function toggleThemePopup(){
  renderSwatches();
  document.getElementById('theme-popup').classList.toggle('show');
}
document.addEventListener('click', ()=>{
  const p = document.getElementById('theme-popup');
  if(p) p.classList.remove('show');
});
(function(){
  const saved = localStorage.getItem('term_bg') || '#1e1e1e';
  renderSwatches();
  setTimeout(()=>applyBg(saved), 300);
})();

loadKeybar();

// ── Session switcher bar ─────────────────────────────────────────────────────
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

// Resize handler: use visualViewport API to correctly handle mobile keyboard
function _applyViewport(){
  const vvh = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.body.style.height = vvh + 'px';
  doFit();
  setTimeout(()=>term.scrollToBottom(), 100);
}
_applyViewport();
if(window.visualViewport){
  window.visualViewport.addEventListener('resize', _applyViewport);
} else {
  window.addEventListener('resize', ()=>{ doFit(); setTimeout(()=>term.scrollToBottom(),100); });
}

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
  if(!confirm('关闭此会话并返回主页？'))return;
  await fetch('/api/close/'+SESS,{method:'POST'});
  location.href='/';
}
// ── Pixel Pet (Holstein Cow) ──────────────────────────────────────────────────
(function(){
  const cv=document.getElementById('pet-canvas');
  if(!cv)return;
  const cx=cv.getContext('2d');
  const S=3;
  // palette: 0=transparent 1=white 2=dark 3=pink 4=tan/horn 5=hoof-gray
  const C=[null,'#f0f0f0','#2d2d2d','#ffb3ba','#c8a057','#5a5a5a'];
  // 20-col × 11-row pixel grid (cow facing left, tail on right)
  const BODY=[
    [0,4,0,4,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,4,4,4,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [1,1,2,1,2,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [1,1,1,3,3,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,1,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0],
    [0,1,2,1,1,1,1,1,2,1,1,1,1,0,0,0,0,0,0,0],
    [1,1,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0,0,0,0],
    [0,0,5,0,0,5,0,0,5,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,5,0,0,5,0,0,5,0,0,0,0,0,0,0,0,0,0,0],
    [0,5,5,0,5,5,0,5,5,0,0,0,0,0,0,0,0,0,0,0],
  ];
  // tail frames: 3 positions (up / mid / down), each is an array of {r,c}
  const TAIL=[
    [{r:6,c:13},{r:5,c:14},{r:4,c:15}],
    [{r:7,c:13},{r:7,c:14},{r:6,c:15}],
    [{r:7,c:13},{r:8,c:14},{r:9,c:15}],
  ];
  const TSEQ=[0,1,2,1];
  let ti=0,tacc=0,blink=false,bacc=0,bnext=3000+Math.random()*4000,alertT=0,lt=0;

  function draw(){
    cx.clearRect(0,0,cv.width,cv.height);
    for(let r=0;r<BODY.length;r++){
      for(let c=0;c<BODY[r].length;c++){
        let v=BODY[r][c]; if(!v)continue;
        if(blink&&r===3&&(c===2||c===4))v=1;
        cx.fillStyle=C[v];
        cx.fillRect(c*S,r*S,S,S);
      }
    }
    cx.fillStyle=C[2];
    for(const p of TAIL[TSEQ[ti]])cx.fillRect(p.c*S,p.r*S,S,S);
    if(alertT>0){
      cx.fillStyle='#f5c518';
      cx.font='bold 9px sans-serif';
      cx.textAlign='right';
      cx.fillText('!',cv.width-1,9);
      cx.textAlign='left';
    }
  }

  function tick(ts){
    const dt=Math.min(ts-lt,100); lt=ts;
    tacc+=dt; if(tacc>420){tacc=0;ti=(ti+1)%TSEQ.length;}
    bacc+=dt;
    if(!blink&&bacc>bnext){blink=true;bacc=0;bnext=3000+Math.random()*4000;setTimeout(()=>blink=false,140);}
    if(alertT>0)alertT-=dt;
    draw();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  window._petAlert=()=>{alertT=900;};
})();
</script>
</body>
</html>"""

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 7681))
    app.run(host=host, port=port, debug=False)

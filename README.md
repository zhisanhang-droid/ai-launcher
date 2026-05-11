# AI Launcher

A mobile-friendly web terminal for running AI tools (Claude, Codex, or any CLI) on a remote Linux server — accessible from any browser, no app install required.

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![Flask](https://img.shields.io/badge/flask-sock-green) ![tmux](https://img.shields.io/badge/backend-tmux-orange)

## Why

Mobile SSH apps are clunky. AI Launcher gives you a clean home-screen launcher with big tap targets, a mobile-optimised xterm.js terminal, and persistent sessions via tmux — switching apps on your phone never kills your Claude conversation.

## Features

- **Tool launcher** — one-tap buttons to start Claude, Codex, or any custom command
- **Persistent tmux sessions** — close the browser; your session keeps running
- **Multi-session** — run several tools in parallel, switch via the session bar
- **Session rename** — tap a session label to rename it inline
- **Dark terminal theme** — customizable background via color picker + 6 editable color swatches (saved in localStorage)
- **Confirm row** — dedicated `1 / 2 / 3 / ↵` button row above the keybar for common prompts
- **Mobile keybar** — ESC, TAB, arrows, Ctrl+C/D/L/Z without a hardware keyboard; add/hide keys; shortcuts persist on server
- **History viewer** — tap the clock icon for a scrollable popup of full terminal history (up to 10 000 lines, ANSI-rendered)
- **AI output notifications** — Web Notification + title flash when output arrives while the tab is hidden (toggle 🔕/🔔)
- **Session export** — download terminal history as `.txt`
- **Remote server management** — store SSH connection profiles (host, port, user, optional identity file), tap to open an SSH session
- **Custom shortcuts** — save and pin named commands to the home screen
- **PWA** — add to home screen for a full-screen app experience (requires HTTPS)
- **Auto-reconnect** — WebSocket reconnects automatically when you switch back

## Requirements

```
Python 3.8+
tmux
pip install flask flask-sock
```

## Quick Start

```bash
git clone https://github.com/zhisanhang-droid/ai-launcher.git
cd ai-launcher
pip install flask flask-sock

# Required: set strong credentials
export SECRET_KEY="a-long-random-string"
export ADMIN_USER="admin"
export ADMIN_PASS="your-secure-password"

# Create data directory
mkdir -p /opt/ai-launcher

# Recommended: tmux follows latest client size
echo 'set -g window-size latest' >> ~/.tmux.conf

python3 app.py
```

Open `http://your-server:5000` in your browser.

## Customizing Tools

Edit the `TOOLS` list near the top of `app.py`:

```python
TOOLS = [
    {"id": "claude",  "label": "Claude",  "desc": "Claude AI",    "color": "#4f86c6",
     "cmd": "claude", "resume": "claude -r", "user": "root"},
    {"id": "codex",   "label": "Codex",   "desc": "OpenAI Codex", "color": "#e74c3c",
     "cmd": "codex",  "resume": "codex",     "user": "root"},
    {"id": "shell",   "label": "Shell",   "desc": "Bash shell",   "color": "#607d8b",
     "cmd": "bash",   "resume": "bash",      "user": "root"},
]
```

- `cmd` — command run when starting a new session
- `resume` — command run when resuming (e.g. `claude -r` resumes the last conversation)
- `user` — unix user to run as (`"root"` runs directly; others use `su - <user>`)

To allow shortcuts to be created for additional unix users, edit `SC_USERS`:

```python
SC_USERS = ["root", "alice", "bob"]
```

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `change-me-in-production` | Flask session secret — **change this!** |
| `ADMIN_USER` | `admin` | Login username |
| `ADMIN_PASS` | `changeme` | Login password — **change this!** |
| `META_FILE` | `/opt/ai-launcher/sessions.json` | Session metadata storage |
| `SHORTCUTS_FILE` | `/opt/ai-launcher/shortcuts.json` | Shortcuts storage |
| `KEYBAR_FILE` | `/opt/ai-launcher/keybar.json` | Keybar config storage |
| `SERVERS_FILE` | `/opt/ai-launcher/servers.json` | Remote server profiles |
| `SSHKEYS_DIR` | `/opt/ai-launcher/ssh_keys` | SSH private key storage (chmod 700) |
| `PORT` | `5000` | Listening port |

## Running as a systemd Service

```ini
# /etc/systemd/system/ai-launcher.service
[Unit]
Description=AI Launcher
After=network.target

[Service]
User=root
WorkingDirectory=/opt/ai-launcher
ExecStart=/usr/bin/python3 /opt/ai-launcher/app.py
Environment=SECRET_KEY=your-random-secret
Environment=ADMIN_USER=admin
Environment=ADMIN_PASS=your-secure-password
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now ai-launcher
```

## Running Behind nginx (HTTPS recommended for PWA + notifications)

WebSockets require the `Upgrade` header to pass through:

```nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600;
}
```

## Architecture

```
Browser (xterm.js + WebSocket)
        │
        ▼
Flask app (app.py)  ──── /api/* REST routes (sessions, shortcuts, servers, history)
        │
        ▼  PTY (pty.openpty)
   tmux session  ──── keeps process alive across disconnects
        │
        ▼
   claude / codex / bash / ...
```

Key design decisions:

- **Size-first PTY** — browser measures terminal size before opening the WebSocket, passes `?cols=X&rows=Y` in the URL, so the PTY is created at the correct width from byte zero
- **`tmux resize-window`** called after every PTY resize (TIOCSWINSZ alone is not enough for tmux)
- **`set -g window-size latest`** in `~/.tmux.conf` makes tmux follow the most recent client's size
- **visualViewport API** for accurate mobile keyboard detection — body height tracks the visible area so the terminal never hides behind the virtual keyboard
- **`; exec bash` appended** to all tool commands so tmux sessions persist even if the tool process exits

## Security Notes

- This app is designed for **personal / single-user use** on a private server.
- Always set strong `ADMIN_PASS` and `SECRET_KEY` via environment variables — never commit them.
- SSH private keys are stored in `SSHKEYS_DIR` with mode 600. Keep this on local disk.
- Consider putting the app behind a VPN or SSH tunnel rather than exposing it directly.
- HTTPS is required for PWA "Add to Home Screen" and Web Notifications.

## Mobile Tips

- Add to iPhone/Android home screen for a full-screen experience.
- The confirm row (`1 / 2 / 3 / ↵`) saves taps when responding to Claude prompts.
- Use the history viewer (clock icon) to scroll long outputs — it renders in a native-scroll popup that doesn't fight the virtual keyboard.
- The background color picker is in the top-right corner of the home screen.

## License

MIT

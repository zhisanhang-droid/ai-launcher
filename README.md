# AI Launcher

A lightweight, mobile-friendly web terminal launcher for running AI tools (Claude, Codex, etc.) on a remote Linux server — accessible from any phone browser.

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![Flask](https://img.shields.io/badge/flask-sock-green) ![tmux](https://img.shields.io/badge/backend-tmux-orange)

## Why

Mobile SSH apps are clunky. This gives you a clean home-screen launcher with big tap targets, a mobile-optimised xterm.js terminal, and persistent sessions via tmux — so switching apps on your phone doesn't kill your Claude conversation.

## Features

- **Tool launcher** — one-tap buttons to start Claude, Codex, or any custom command
- **Persistent sessions** — powered by tmux; disconnecting never kills the process
- **Multi-session** — run several tools in parallel, switch between them via the session bar
- **Mobile keyboard bar** — ESC, TAB, arrows, Ctrl+C/D/L/Z without a hardware keyboard
- **Touch scroll** — swipe up/down to scroll terminal history
- **Custom shortcuts** — save and pin your own named commands to the home screen
- **One-shot commands** — run any ad-hoc command without creating a saved shortcut
- **Server memory bar** — live RAM usage at a glance
- **PWA** — add to home screen for a full-screen, app-like experience (requires HTTPS)
- **Auto-reconnect** — WebSocket reconnects automatically when you switch back from another app

## Requirements

```
Python 3.8+
tmux
pip install flask flask-sock
```

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/ai-launcher
cd ai-launcher

# Set credentials via environment variables (do NOT hardcode in production)
export ADMIN_USER=admin
export ADMIN_PASS=your-secure-password

# Create data directory
mkdir -p /opt/ai-launcher
touch /opt/ai-launcher/sessions.json /opt/ai-launcher/shortcuts.json

# Recommended: use window-size latest so tmux follows the most recent client size
echo 'set -g window-size latest' >> ~/.tmux.conf

python3 app.py
```

Open `http://your-server-ip:7681` in your phone browser.

## Configuration

Edit the `TOOLS` list in `app.py` to define your own buttons:

```python
TOOLS = [
    {
        "id":     "claude",           # unique identifier
        "label":  "Claude",           # display name
        "desc":   "Claude AI",        # subtitle
        "color":  "#4f86c6",          # dot colour (any CSS colour)
        "cmd":    "claude",           # command for a new session
        "resume": "claude -r",        # command to resume last conversation
        "user":   "root",             # unix user to run as
    },
    # Add Codex, custom scripts, etc.
]
```

All config can also be set via environment variables:

| Variable         | Default                              | Description              |
|------------------|--------------------------------------|--------------------------|
| `ADMIN_USER`     | `admin`                              | Login username           |
| `ADMIN_PASS`     | `changeme`                           | Login password           |
| `SECRET_KEY`     | `change-me-in-production`            | Flask session secret     |
| `META_FILE`      | `/opt/ai-launcher/sessions.json`     | Session metadata path    |
| `SHORTCUTS_FILE` | `/opt/ai-launcher/shortcuts.json`    | Custom shortcuts path    |
| `PORT`           | `7681`                               | Port to listen on        |

## Run as a systemd service

```ini
# /etc/systemd/system/ai-launcher.service
[Unit]
Description=AI Launcher
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/ai-launcher/app.py
Environment=ADMIN_USER=admin
Environment=ADMIN_PASS=your-secure-password
Environment=SECRET_KEY=your-random-secret
Restart=always
RestartSec=3
User=root
WorkingDirectory=/opt/ai-launcher

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now ai-launcher
```

## Architecture

```
Browser (xterm.js + WebSocket)
        │
        ▼
Flask app (app.py)  ──── /api/* REST routes (sessions, shortcuts, memory)
        │
        ▼  PTY (pty.openpty)
   tmux session  ──── keeps process alive across disconnects
        │
        ▼
   claude / codex / bash / ...
```

Key design decisions:

- **Size-first PTY**: the browser measures the terminal size *before* opening the WebSocket and passes `?cols=X&rows=Y` in the URL, so the PTY is created at the correct width from byte zero — no garbled output.
- **`tmux resize-window`** is called after every PTY resize (TIOCSWINSZ alone is not enough for tmux).
- **`set -g window-size latest`** in `~/.tmux.conf` makes tmux follow the most recent client's size instead of the smallest.
- **`position:fixed` on `<body>`** locks the layout against mobile browser chrome collapsing (the "100vh issue"), keeping the toolbar always visible.

## Security notes

- This app is designed for **personal / single-user use** on a private server.
- Always set a strong `ADMIN_PASS` via environment variable.
- Consider putting it behind a VPN or SSH tunnel rather than exposing it to the public internet.
- HTTPS is required for the PWA "Add to Home Screen" feature.

## License

MIT

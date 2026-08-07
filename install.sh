#!/bin/bash
# Install the Agent Fleet dashboard as a launchd user agent.
# Idempotent: safe to re-run to pick up changes.
set -euo pipefail

LABEL="com.agentfleet.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || true)"
PORT="${PORT:-8787}"

if [ -z "$PYTHON" ]; then
  echo "python3 not found on PATH." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>fleet</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/agent-fleet.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/agent-fleet.log</string>
</dict>
</plist>
PLIST_EOF

# bootout first so a re-run picks up plist changes; ignore "not loaded".
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true

# bootout returns before launchd has finished unloading the job, so an
# immediate bootstrap can lose the race and fail with "Bootstrap failed: 5:
# Input/output error" — leaving the service UNLOADED and the dashboard
# silently gone. Retry until it takes.
bootstrap_error=""
for attempt in 1 2 3 4 5 6; do
  if bootstrap_error="$(launchctl bootstrap "gui/$UID" "$PLIST" 2>&1)"; then
    bootstrap_error=""
    break
  fi
  [ "$attempt" -lt 6 ] && sleep 1
done

if [ -n "$bootstrap_error" ]; then
  echo "Failed to load the launchd agent after 6 attempts:" >&2
  echo "  $bootstrap_error" >&2
  echo "The dashboard is NOT running. Try: ./uninstall.sh && ./install.sh" >&2
  exit 1
fi

# Loading is not serving. Confirm the port actually answers before claiming
# success, so a crash-on-startup cannot be reported as a good install.
serving=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/fleet" >/dev/null 2>&1; then
    serving=1
    break
  fi
  sleep 1
done

if [ -z "$serving" ]; then
  echo "The launchd agent loaded but nothing is answering on port $PORT." >&2
  echo "Check the log: ~/Library/Logs/agent-fleet.log" >&2
  exit 1
fi

echo "Installed. Dashboard: http://127.0.0.1:$PORT"
echo "Logs: ~/Library/Logs/agent-fleet.log"
echo
echo "To make it feel like a native app:"
echo "  1. Open http://127.0.0.1:$PORT in Chrome"
echo "  2. ⋮ menu → Cast, Save & Share → Install page as app"
echo "  3. It gets its own Dock icon and window. ⌘-Tab to it like any app."

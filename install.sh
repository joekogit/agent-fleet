#!/bin/bash
# Install the Agent Fleet dashboard as a launchd user agent.
# Idempotent: safe to re-run to pick up changes.
set -euo pipefail

LABEL="com.joekocovsky.agentfleet"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
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
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed. Dashboard: http://127.0.0.1:$PORT"
echo "Logs: ~/Library/Logs/agent-fleet.log"
echo
echo "To make it feel like a native app:"
echo "  1. Open http://127.0.0.1:$PORT in Chrome"
echo "  2. ⋮ menu → Cast, Save & Share → Install page as app"
echo "  3. It gets its own Dock icon and window. ⌘-Tab to it like any app."

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
  echo "Agent Fleet needs Python 3.9 or newer (macOS ships one at /usr/bin/python3)." >&2
  exit 1
fi

# Existing-but-too-old is a different failure from missing, and it surfaces far
# later (as a SyntaxError in the log) if it is not caught here.
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer required; $PYTHON is $("$PYTHON" -V 2>&1)." >&2
  exit 1
fi

# Neither source existing is not an install failure, but it is the whole reason
# the board would come up empty — say so now rather than let it look broken.
CLI_DIR="${AGENT_FLEET_HOME:-$HOME}/.claude"
APP_DIR="${AGENT_FLEET_APP_SUPPORT:-$HOME/Library/Application Support/Claude}"
if [ ! -d "$CLI_DIR" ] && [ ! -d "$APP_DIR" ]; then
  echo "Warning: found no Claude data to read." >&2
  echo "  looked for: $CLI_DIR" >&2
  echo "  looked for: $APP_DIR" >&2
  echo "The dashboard will install and run, but show an empty fleet until a" >&2
  echo "Claude session writes to one of those. Override either location with" >&2
  echo "AGENT_FLEET_HOME / AGENT_FLEET_APP_SUPPORT." >&2
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# A launchd job inherits nothing from this shell, so any path override in
# effect at install time has to be written into the plist or the installed
# service would silently ignore it.
ENV_BLOCK=""
if [ -n "${AGENT_FLEET_HOME:-}" ] || [ -n "${AGENT_FLEET_APP_SUPPORT:-}" ]; then
  ENV_BLOCK="  <key>EnvironmentVariables</key>
  <dict>"
  [ -n "${AGENT_FLEET_HOME:-}" ] && ENV_BLOCK="$ENV_BLOCK
    <key>AGENT_FLEET_HOME</key><string>$AGENT_FLEET_HOME</string>"
  [ -n "${AGENT_FLEET_APP_SUPPORT:-}" ] && ENV_BLOCK="$ENV_BLOCK
    <key>AGENT_FLEET_APP_SUPPORT</key><string>$AGENT_FLEET_APP_SUPPORT</string>"
  ENV_BLOCK="$ENV_BLOCK
  </dict>"
fi

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
$ENV_BLOCK
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

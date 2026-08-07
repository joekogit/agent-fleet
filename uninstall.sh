#!/bin/bash
# Remove the Agent Fleet launchd agent. Leaves the repo untouched.
set -euo pipefail

LABEL="com.joekocovsky.agentfleet"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Uninstalled. If you installed the Chrome app window, remove it from"
echo "chrome://apps as well."

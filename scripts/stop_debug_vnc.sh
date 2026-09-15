#!/bin/sh
set -eu

state_dir=/tmp/mymcp-debug-ui

for name in novnc x11vnc fluxbox; do
    pid_file="$state_dir/$name.pid"
    if [ -f "$pid_file" ]; then
        pid=$(sed -n '1p' "$pid_file")
        [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
    fi
done

if [ -d "$state_dir" ]; then
    find "$state_dir" -depth -delete
fi

echo "MCP debug desktop stopped."

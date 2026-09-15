#!/bin/sh
set -eu

state_dir=/tmp/mymcp-debug-ui
hold_file="$state_dir/hold-browser-open"
mkdir -p "$state_dir"

display="${DISPLAY:-:99}"
if ! xdpyinfo -display "$display" >/dev/null 2>&1; then
    echo "The MCP display $display is unavailable. Set MCP_CHATGPT_BROWSER_HEADLESS=false and recreate the container." >&2
    exit 1
fi

start_process() {
    name="$1"
    shift
    pid_file="$state_dir/$name.pid"
    if [ -f "$pid_file" ]; then
        old_pid=$(sed -n '1p' "$pid_file")
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            return
        fi
    fi
    nohup "$@" >"$state_dir/$name.log" 2>&1 </dev/null &
    echo "$!" >"$pid_file"
}

export DISPLAY="$display"
start_process fluxbox fluxbox
start_process x11vnc x11vnc -display "$display" -forever -shared -localhost -nopw -rfbport 5900
start_process novnc websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900
touch "$hold_file"

echo "MCP debug desktop is available on container port 6080."
echo "While this viewer is active, sent browser contexts remain open for 60 seconds."

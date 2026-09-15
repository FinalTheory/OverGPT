#!/bin/sh
set -eu

export PYTHONDONTWRITEBYTECODE=1

export DISPLAY="${DISPLAY:-:99}"

cleanup() {
    for process_id in ${NOVNC_PID:-} ${VNC_PID:-} ${WM_PID:-} ${XVFB_PID:-}; do
        [ -z "$process_id" ] || kill "$process_id" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY" -screen 0 "${MCP_VNC_GEOMETRY:-1440x900x24}" -nolisten tcp &
XVFB_PID=$!

attempt=0
until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 50 ]; then
        echo "Xvfb did not become ready" >&2
        exit 1
    fi
    sleep 0.1
done

fluxbox >/tmp/fluxbox.log 2>&1 &
WM_PID=$!
x11vnc -display "$DISPLAY" -forever -shared -localhost -nopw -rfbport 5900 \
    >/tmp/x11vnc.log 2>&1 &
VNC_PID=$!
websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 \
    >/tmp/novnc.log 2>&1 &
NOVNC_PID=$!

echo "ChatGPT login desktop is ready on noVNC port 6080."
echo "Close the Chromium window after login to stop this container."
python chatgpt_playwright.py login

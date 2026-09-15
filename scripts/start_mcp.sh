#!/bin/sh
set -eu

export PYTHONDONTWRITEBYTECODE=1

case "${MCP_CHATGPT_BROWSER_HEADLESS:-true}" in
    0|false|no|off)
        export DISPLAY="${DISPLAY:-:99}"
        display_number="${DISPLAY#:}"
        display_number="${display_number%%.*}"
        case "$display_number" in
            ''|*[!0-9]*)
                echo "Unsupported DISPLAY value: $DISPLAY" >&2
                exit 1
                ;;
        esac
        rm -f "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}"
        Xvfb "$DISPLAY" -screen 0 "${MCP_VNC_GEOMETRY:-1440x900x24}" \
            -nolisten tcp >/tmp/mcp-xvfb.log 2>&1 &
        attempt=0
        until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
            attempt=$((attempt + 1))
            if [ "$attempt" -ge 50 ]; then
                echo "MCP Xvfb did not become ready" >&2
                exit 1
            fi
            sleep 0.1
        done
        ;;
esac

exec python server.py

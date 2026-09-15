#!/bin/sh
set -eu

env_file="${1:-./.env}"
mode="${2:-login}"

if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
fi

host="${MCP_SYNC_HOST:-}"
ssh_port="${MCP_SYNC_PORT:-22}"

case "$mode" in
    login)
        local_port="${MCP_VNC_LOCAL_PORT:-6080}"
        remote_port="${MCP_VNC_PORT:-6080}"
        ;;
    debug)
        local_port="${MCP_DEBUG_VNC_LOCAL_PORT:-6081}"
        remote_port="${MCP_DEBUG_VNC_PORT:-6081}"
        ;;
    *)
        echo "Unknown VNC tunnel mode: $mode" >&2
        exit 1
        ;;
esac

[ -n "$host" ] || {
    echo "MCP_SYNC_HOST must be set in $env_file." >&2
    exit 1
}
command -v ssh >/dev/null 2>&1 || { echo "ssh is required" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }

if command -v open >/dev/null 2>&1; then
    browser_open=open
elif command -v xdg-open >/dev/null 2>&1; then
    browser_open=xdg-open
else
    echo "Neither open nor xdg-open is available." >&2
    exit 1
fi

url="http://127.0.0.1:$local_port/vnc.html?autoconnect=true&resize=scale"
(
    sleep 1
    attempt=0
    while [ "$attempt" -lt 50 ]; do
        if curl -fsS "http://127.0.0.1:$local_port/vnc.html" >/dev/null 2>&1; then
            "$browser_open" "$url"
            exit 0
        fi
        attempt=$((attempt + 1))
        sleep 0.2
    done
    echo "Timed out waiting for noVNC on 127.0.0.1:$local_port." >&2
) &
opener_pid=$!
trap 'kill "$opener_pid" >/dev/null 2>&1 || true' EXIT INT TERM

echo "Forwarding 127.0.0.1:$local_port to $host:127.0.0.1:$remote_port"
echo "Press Ctrl+C to stop the SSH tunnel."
ssh -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "$local_port:127.0.0.1:$remote_port" \
    -p "$ssh_port" "$host"

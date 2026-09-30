#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR=${XDG_STATE_HOME:-"$HOME/.local/state"}/wechat-cli
RUNTIME_DIR=${XDG_RUNTIME_DIR:-"/tmp/wechat-cli-$(id -u)"}/wechat-cli

cd -- "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export WECHAT_DISPLAY=${WECHAT_DISPLAY:-:99}

if [[ -S "$RUNTIME_DIR/service.sock" || -f "$RUNTIME_DIR/service.pid" ]]; then
    python3 -m wechat_cli service stop || true
fi

terminate() {
    local pattern=$1 process_id
    local -a process_ids=()
    mapfile -t process_ids < <(pgrep -f -- "$pattern" || true)
    for process_id in "${process_ids[@]}"; do
        kill -TERM "$process_id" 2>/dev/null || true
    done
    sleep 0.3
    for process_id in "${process_ids[@]}"; do
        kill -KILL "$process_id" 2>/dev/null || true
    done
}

terminate '^python3 -m wechat_cli demo( |$)'
terminate '^python -m wechat_cli demo( |$)'
terminate '^python3 -m wechat_cli mcp( |$)'
terminate '^python -m wechat_cli mcp( |$)'
terminate '^x11vnc .* -display :99( |$)'
terminate '^openbox.*:99( |$)'
terminate '^Xvfb :99( |$)'
terminate '^/opt/wechat/'

rm -f -- "$RUNTIME_DIR/service.sock" "$RUNTIME_DIR/service.pid"
printf 'wechat-cli processes on %s have been stopped.\n' "$WECHAT_DISPLAY"

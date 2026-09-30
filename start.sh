#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR=${XDG_STATE_HOME:-"$HOME/.local/state"}/wechat-cli
LOG_DIR="$STATE_DIR/logs"
DEMO_PORT=${WECHAT_DEMO_PORT:-8765}

cd -- "$ROOT_DIR"
mkdir -p -- "$LOG_DIR"

export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export WECHAT_DISPLAY=${WECHAT_DISPLAY:-:99}

printf 'wechatcli operation URL: http://127.0.0.1:%s\n' "$DEMO_PORT"
printf 'The browser controller is localhost-only and is not exposed to the LAN.\n'

if [[ ! -x "$ROOT_DIR/mcp.sh" || ! -x "$ROOT_DIR/console.sh" ]]; then
    printf 'mcp.sh and console.sh must be executable. Run: chmod +x %q %q\n' \
        "$ROOT_DIR/mcp.sh" "$ROOT_DIR/console.sh" >&2
    exit 1
fi
python3 -c 'from wechat_cli.mcp import MCPServer' >/dev/null

python3 -m wechat_cli call session.start --params '{}'

if ! python3 - <<PY
import socket
with socket.socket() as connection:
    connection.settimeout(0.2)
    raise SystemExit(0 if connection.connect_ex(("127.0.0.1", $DEMO_PORT)) == 0 else 1)
PY
then
    setsid -f env PYTHONPATH="$PYTHONPATH" WECHAT_DISPLAY="$WECHAT_DISPLAY" \
        python3 -m wechat_cli demo --host 127.0.0.1 --port "$DEMO_PORT" \
        >>"$LOG_DIR/demo.log" 2>&1 < /dev/null
fi

printf 'wechat-cli is ready. Demo: http://127.0.0.1:%s; MCP launcher: %s\n' \
    "$DEMO_PORT" "$ROOT_DIR/mcp.sh"

if [[ ${WECHAT_NO_CONSOLE:-0} != 1 && -t 0 && -t 1 ]]; then
    exec "$ROOT_DIR/console.sh"
fi

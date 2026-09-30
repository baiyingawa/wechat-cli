#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEMO_PORT=${WECHAT_DEMO_PORT:-8765}

cd -- "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export WECHAT_DISPLAY=${WECHAT_DISPLAY:-:99}

printf '\033]0;wechatcli\007'
exec python3 -m wechat_cli.console

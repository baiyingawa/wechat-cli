#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEMO_PORT=${WECHAT_DEMO_PORT:-8765}

cd -- "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export WECHAT_DISPLAY=${WECHAT_DISPLAY:-:99}

printf '\033]0;wechatcli\007'
printf 'wechatcli console\n'
printf 'Operation URL: http://127.0.0.1:%s\n' "$DEMO_PORT"
printf 'Use /help to list commands. Services remain running after /exit.\n\n'

show_help() {
    cat <<'EOF'
/help                         Show this help.
/url                          Print the local browser controller URL.
/status                       Print the visible WeChat session status.
/doctor                       Print display and dependency diagnostics.
/call METHOD [JSON_PARAMS]    Run a wechat-cli method, for example:
                               /call message.read {"chat":"False","limit":30}
/mcp                          Print the MCP launcher path and configuration hint.
/exit                         Close this console without stopping services.
EOF
}

while true; do
    if ! IFS= read -r -p 'wechatcli> ' line; then
        printf '\n'
        break
    fi
    command=${line%% *}
    remainder=${line#"$command"}
    remainder=${remainder# }
    case "$command" in
        /help)
            show_help
            ;;
        /url)
            printf 'http://127.0.0.1:%s\n' "$DEMO_PORT"
            ;;
        /status)
            python3 -m wechat_cli call session.status --params '{}'
            ;;
        /doctor)
            python3 -m wechat_cli doctor
            ;;
        /mcp)
            printf 'MCP launcher: %s/mcp.sh\n' "$ROOT_DIR"
            printf 'Windows launcher: %s/mcp-wsl.bat\n' "$ROOT_DIR"
            ;;
        /call)
            method=${remainder%% *}
            params=${remainder#"$method"}
            params=${params# }
            if [[ -z "$method" ]]; then
                printf 'Usage: /call METHOD [JSON_PARAMS]\n' >&2
            else
                python3 -m wechat_cli call "$method" --params "${params:-{}}"
            fi
            ;;
        /exit|/quit)
            break
            ;;
        '')
            ;;
        *)
            printf 'Unknown command: %s. Use /help.\n' "$command" >&2
            ;;
    esac
done

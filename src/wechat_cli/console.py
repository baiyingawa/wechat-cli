import json
import io
import os
import re
import shlex
import sys
import time
import uuid

from PIL import Image

from .config import Config
from .errors import AutomationError
from .registry import capabilities
from . import service


STYLES = {
    "heading": "1;36",
    "command": "1;36",
    "success": "1;32",
    "error": "1;31",
    "warning": "1;33",
    "link": "4;36",
    "muted": "2",
    "key": "36",
    "string": "32",
    "number": "33",
}


def colors_enabled(stream=None):
    stream = sys.stdout if stream is None else stream
    return ("NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
            and getattr(stream, "isatty", lambda: False)())


def styled(text, role, stream=None):
    if not colors_enabled(stream):
        return text
    return f"\033[{STYLES[role]}m{text}\033[0m"


def print_error(text):
    print(styled(text, "error", sys.stderr), file=sys.stderr)


def print_json(value):
    output = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if not colors_enabled():
        print(output)
        return

    def highlight(match):
        token = match.group()
        if token.startswith('"'):
            if match.lastgroup == "key":
                role = "heading" if token in ('"ok"', '"error"', '"result"') else "key"
            else:
                role = "string"
        else:
            role = {"true": "success", "false": "error", "null": "muted"}.get(token, "number")
        return f"\033[{STYLES[role]}m{token}\033[0m"

    print(re.sub(r'(?P<key>"(?:[^"\\]|\\.)*")(?=\s*:)|"(?:[^"\\]|\\.)*"'
                 r'|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?',
                 highlight, output))


def qr_terminal_art(path, columns=48):
    with Image.open(path) as source:
        image = source.convert("L")
        side = min(columns, image.width, image.height)
        image = image.resize((side, side), Image.Resampling.NEAREST)
        pixels = image.load()
        return "\n".join("".join("██" if pixels[x, y] < 150 else "  " for x in range(side))
                         for y in range(side))


def run_login(config, methods):
    from .routing import resolve
    config = resolve(config)
    print(styled("正在启动并识别登录界面；扫码后会自动检测登录完成。按 Ctrl-C 返回控制台。", "warning"))
    last_qr_digest = None
    screenshot_printed = False
    while True:
        request = build_request("session.login", {}, methods)
        response = service.call(config, request, follow_target=False)
        if not response.get("ok"):
            print_json(response)
            return
        result = response["result"]
        if result["state"] == "logged_in":
            print(styled("登录完成。", "success"))
            return
        screenshot = result.get("screenshot")
        qr = result.get("qr")
        if qr and qr["digest"] != last_qr_digest:
            print("二维码截图：" + styled(qr["path"], "link"))
            if config.target == "woc" and os.environ.get("WECHAT_WOC_WORKER") != "1":
                from .woc import read_login_qr
                print(qr_terminal_art(io.BytesIO(read_login_qr(config, qr["path"]))))
            else:
                print(qr_terminal_art(qr["path"]))
            last_qr_digest = qr["digest"]
        elif screenshot and not screenshot_printed:
            print("登录界面截图：" + styled(screenshot["path"], "link"))
            screenshot_printed = True
        time.sleep(0.5)


def print_help(method_name=None):
    methods = capabilities()["methods"]
    if method_name:
        selected = [item for item in methods if item["method"] == method_name]
    else:
        selected = []
    if method_name and not selected:
        print_error(f"Unknown method: {method_name}")
        return
    if not method_name:
        print(styled("Common commands:", "heading"))
        commands = [
            ("/start | /login | /logout", "Start WeChat; auto-click login and show QR; log out"),
            ("/status | /maximize | /reset", "Check session, maximize, or restore chat view"),
            ("/target [local|woc CONTAINER]", "Show or switch the live automation target"),
            ("/chat CHAT TEXT", "Send a message, e.g. /chat False 111"),
            ("/open CHAT | /read CHAT [LIMIT]", "Open a chat or read up to 30 messages"),
            ("/find CHAT TEXT | /recall CHAT TEXT", "Search recent messages or recall one"),
            ("/contact CHAT | /pat CHAT [self|other]", "Read contact details or pat an avatar"),
            ("/moments CHAT | /like CHAT POST", "Open Moments or like a text post"),
            ("/unlike CHAT POST | /feed", "Remove a like or open the Moments feed"),
            ("/url | /doctor | /mcp | /exit", ""),
        ]
        for usage, description in commands:
            print(f"  {styled(usage, 'command')}{' ' * max(2, 40 - len(usage))}{description}".rstrip())
        print(styled("Use /methods to list advanced APIs, or /help METHOD for its parameters.", "muted"))
        return
    for item in selected:
        schema = item.get("params_schema", {})
        required = ", ".join(schema.get("required", ())) or "none"
        properties = schema.get("properties", {})
        parameters = ", ".join(
            f"{name}:{definition.get('type', 'value')}"
            for name, definition in properties.items()
        ) or "none"
        flags = []
        if item.get("idempotency_required"):
            flags.append("key")
        if item.get("destructive"):
            flags.append("confirm")
        suffix = f"; flags: {', '.join(flags)}" if flags else ""
        print(f"{styled(item['method'], 'command')}: {item.get('description', '')}")
        print(f"  required: {required}; parameters: {parameters}{suffix}")


def build_request(method, params, methods, key=None, confirm=None):
    if method not in methods:
        raise ValueError(f"Unknown method: {method}. Use /help.")
    if not isinstance(params, dict):
        raise ValueError("Parameters must be an object")
    metadata = methods[method]
    if metadata.get("idempotency_required") and not key:
        key = f"console-{uuid.uuid4()}"
        print(styled(f"Generated idempotency key: {key}", "muted", sys.stderr), file=sys.stderr)
    request = {"id": f"console-{uuid.uuid4()}", "method": method, "params": params}
    if key:
        request["idempotency_key"] = key
    if confirm:
        request["confirm_token"] = confirm
    return request


def parse_invocation(tokens, methods):
    if len(tokens) < 2:
        raise ValueError("Usage: METHOD JSON [--key KEY] [--confirm TOKEN]")
    method = tokens[0]
    try:
        params = json.loads(tokens[1])
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON parameters: {error.msg}") from error
    key = None
    confirm = None
    index = 2
    while index < len(tokens):
        option = tokens[index]
        if option not in ("--key", "--idempotency-key", "--confirm", "--confirm-token"):
            raise ValueError(f"Unknown option: {option}")
        if index + 1 >= len(tokens):
            raise ValueError(f"Missing value for {option}")
        if option in ("--key", "--idempotency-key"):
            key = tokens[index + 1]
        else:
            confirm = tokens[index + 1]
        index += 2
    return build_request(method, params, methods, key, confirm)


def parse_shortcut(command, tokens, methods):
    if command in ("/start", "/login", "/logout", "/maximize", "/reset"):
        if tokens:
            raise ValueError(f"Usage: {command}")
        method = {"/start": "session.start", "/login": "session.login", "/logout": "session.logout",
                  "/maximize": "ui.maximize", "/reset": "ui.reset"}[command]
        return build_request(method, {}, methods)
    if command == "/target":
        if not tokens:
            return build_request("target.status", {}, methods)
        if tokens == ["local"]:
            return build_request("target.select", {"target": "local"}, methods)
        if len(tokens) == 2 and tokens[0] == "woc":
            return build_request("target.select", {"target": "woc", "container": tokens[1]}, methods)
        raise ValueError("Usage: /target [local|woc CONTAINER]")
    if command == "/chat":
        if len(tokens) < 2:
            raise ValueError("Usage: /chat CHAT TEXT")
        return build_request("message.send", {"chat": tokens[0], "text": " ".join(tokens[1:])}, methods)
    if command == "/open":
        if len(tokens) != 1:
            raise ValueError("Usage: /open CHAT")
        return build_request("chat.open", {"chat": tokens[0]}, methods)
    if command == "/read":
        if not 1 <= len(tokens) <= 2:
            raise ValueError("Usage: /read CHAT [LIMIT]")
        params = {"chat": tokens[0]}
        if len(tokens) == 2:
            try:
                params["limit"] = int(tokens[1])
            except ValueError as error:
                raise ValueError("LIMIT must be an integer between 1 and 30") from error
            if not 1 <= params["limit"] <= 30:
                raise ValueError("LIMIT must be an integer between 1 and 30")
        return build_request("message.read", params, methods)
    if command == "/find":
        if len(tokens) < 2:
            raise ValueError("Usage: /find CHAT TEXT")
        return build_request("message.search", {"chat": tokens[0], "query": " ".join(tokens[1:])}, methods)
    if command == "/recall":
        if len(tokens) < 2:
            raise ValueError("Usage: /recall CHAT TEXT")
        return build_request("message.revoke", {"chat": tokens[0], "text": " ".join(tokens[1:])}, methods)
    if command == "/contact":
        if len(tokens) != 1:
            raise ValueError("Usage: /contact CHAT")
        return build_request("contact.info", {"chat": tokens[0]}, methods)
    if command == "/pat":
        if not 1 <= len(tokens) <= 2:
            raise ValueError("Usage: /pat CHAT [self|other]")
        return build_request("message.pat", {"chat": tokens[0], "target": tokens[1] if len(tokens) == 2 else "other"}, methods)
    if command == "/moments":
        if len(tokens) != 1:
            raise ValueError("Usage: /moments CHAT")
        return build_request("moments.open", {"chat": tokens[0]}, methods)
    if command in ("/like", "/unlike"):
        if len(tokens) < 2:
            raise ValueError(f"Usage: {command} CHAT POST_TEXT")
        method = "moments.like" if command == "/like" else "moments.unlike"
        return build_request(method, {"chat": tokens[0], "post_text": " ".join(tokens[1:])}, methods)
    if command == "/feed":
        if tokens:
            raise ValueError("Usage: /feed")
        return build_request("moments.feed.open", {}, methods)
    return None


def main():
    config = Config.from_env()
    metadata = capabilities()["methods"]
    methods = {item["method"]: item for item in metadata if item["status"] == "implemented"}
    port = os.environ.get("WECHAT_DEMO_PORT", "8765")
    print(styled("wechatcli console", "heading"))
    print("Operation URL: " + styled(f"http://127.0.0.1:{port}", "link"))
    print(styled("Use /help to list WeChat commands. Services remain running after /exit.", "muted") + "\n")
    while True:
        try:
            line = input(styled("wechatcli> ", "command")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as error:
            print_error(f"Input error: {error}")
            continue
        command, *rest = tokens
        if command in ("/exit", "/quit"):
            return
        if command in ("/help", "help"):
            print_help(rest[0] if rest else None)
            continue
        if command == "/methods":
            prefix = rest[0] if rest else ""
            for name in sorted(name for name in methods if name.startswith(prefix)):
                print(styled(name, "command"))
            continue
        if command == "/url":
            print(styled(f"http://127.0.0.1:{port}", "link"))
            continue
        if command in ("/status", "/doctor"):
            method = "session.status" if command == "/status" else "doctor"
            print_json(service.call(config, {"id": f"console-{uuid.uuid4()}", "method": method, "params": {}}))
            continue
        if command == "/login":
            if rest:
                print_error("Error: Usage: /login")
                continue
            try:
                run_login(config, methods)
            except KeyboardInterrupt:
                print("\n" + styled("已停止等待登录。", "warning"))
            except AutomationError as error:
                print_error(f"Error: {error}")
            continue
        if command == "/mcp":
            print("MCP launcher: " + styled("mcp.sh", "command"))
            print("Windows launcher: " + styled("mcp-wsl.bat", "command"))
            continue
        try:
            request = parse_shortcut(command, rest, methods)
            if request is None:
                request = parse_invocation(rest if command == "/call" else tokens, methods)
            print_json(service.call(config, request))
        except (AutomationError, ValueError) as error:
            print_error(f"Error: {error}")


if __name__ == "__main__":
    main()

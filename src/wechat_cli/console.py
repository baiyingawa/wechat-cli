import json
import os
import shlex
import sys
import time
import uuid

from PIL import Image

from .config import Config
from .errors import AutomationError
from .registry import capabilities
from . import service


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def qr_terminal_art(path, columns=48):
    with Image.open(path) as source:
        image = source.convert("L")
        side = min(columns, image.width, image.height)
        image = image.resize((side, side), Image.Resampling.NEAREST)
        pixels = image.load()
        return "\n".join("".join("██" if pixels[x, y] < 150 else "  " for x in range(side))
                         for y in range(side))


def run_login(config, methods):
    print("正在启动并识别登录界面；扫码后会自动检测登录完成。按 Ctrl-C 返回控制台。")
    last_qr_digest = None
    screenshot_printed = False
    while True:
        request = build_request("session.login", {}, methods)
        response = service.call(config, request)
        if not response.get("ok"):
            print_json(response)
            return
        result = response["result"]
        if result["state"] == "logged_in":
            print("登录完成。")
            return
        screenshot = result.get("screenshot")
        qr = result.get("qr")
        if qr and qr["digest"] != last_qr_digest:
            print(f"二维码截图：{qr['path']}")
            print(qr_terminal_art(qr["path"]))
            last_qr_digest = qr["digest"]
        elif screenshot and not screenshot_printed:
            print(f"登录界面截图：{screenshot['path']}")
            screenshot_printed = True
        time.sleep(0.5)


def print_help(method_name=None):
    methods = capabilities()["methods"]
    if method_name:
        selected = [item for item in methods if item["method"] == method_name]
    else:
        selected = []
    if method_name and not selected:
        print(f"Unknown method: {method_name}", file=sys.stderr)
        return
    if not method_name:
        print("Common commands:")
        print("  /start | /login | /logout             Start WeChat; auto-click login and show QR; log out")
        print("  /status | /maximize | /reset          Check session, maximize, or restore chat view")
        print("  /gui on|off | /remote                 Toggle Windows VNC access or show remote VNC details")
        print("  /chat CHAT TEXT                        Send a message, e.g. /chat False 111")
        print("  /open CHAT | /read CHAT [LIMIT]        Open a chat or read up to 30 messages")
        print("  /find CHAT TEXT | /recall CHAT TEXT    Search recent messages or recall one")
        print("  /contact CHAT | /pat CHAT [self|other] Read contact details or pat an avatar")
        print("  /moments CHAT | /like CHAT POST        Open Moments or like a text post")
        print("  /unlike CHAT POST | /feed              Remove a like or open the Moments feed")
        print("  /url | /doctor | /mcp | /exit")
        print("Use /methods to list advanced APIs, or /help METHOD for its parameters.")
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
        print(f"{item['method']}: {item.get('description', '')}")
        print(f"  required: {required}; parameters: {parameters}{suffix}")


def build_request(method, params, methods, key=None, confirm=None):
    if method not in methods:
        raise ValueError(f"Unknown method: {method}. Use /help.")
    if not isinstance(params, dict):
        raise ValueError("Parameters must be an object")
    metadata = methods[method]
    if metadata.get("idempotency_required") and not key:
        key = f"console-{uuid.uuid4()}"
        print(f"Generated idempotency key: {key}", file=sys.stderr)
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
    if command in ("/start", "/login", "/logout", "/maximize", "/reset", "/remote"):
        if tokens:
            raise ValueError(f"Usage: {command}")
        method = {"/start": "session.start", "/login": "session.login", "/logout": "session.logout",
                  "/maximize": "ui.maximize", "/reset": "ui.reset", "/remote": "session.remote"}[command]
        return build_request(method, {}, methods)
    if command == "/gui":
        if len(tokens) != 1 or tokens[0] not in ("on", "off"):
            raise ValueError("Usage: /gui on|off")
        return build_request("session.gui", {"enabled": tokens[0] == "on"}, methods)
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
    print("wechatcli console")
    print(f"Operation URL: http://127.0.0.1:{port}")
    print("Use /help to list WeChat commands. Services remain running after /exit.\n")
    while True:
        try:
            line = input("wechatcli> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as error:
            print(f"Input error: {error}", file=sys.stderr)
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
                print(name)
            continue
        if command == "/url":
            print(f"http://127.0.0.1:{port}")
            continue
        if command in ("/status", "/doctor"):
            method = "session.status" if command == "/status" else "doctor"
            print_json(service.call(config, {"id": f"console-{uuid.uuid4()}", "method": method, "params": {}}))
            continue
        if command == "/login":
            if rest:
                print("Error: Usage: /login", file=sys.stderr)
                continue
            try:
                run_login(config, methods)
            except KeyboardInterrupt:
                print("\n已停止等待登录。")
            except AutomationError as error:
                print(f"Error: {error}", file=sys.stderr)
            continue
        if command == "/mcp":
            print("MCP launcher: mcp.sh")
            print("Windows launcher: mcp-wsl.bat")
            continue
        try:
            request = parse_shortcut(command, rest, methods)
            if request is None:
                request = parse_invocation(rest if command == "/call" else tokens, methods)
            print_json(service.call(config, request))
        except (AutomationError, ValueError) as error:
            print(f"Error: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()

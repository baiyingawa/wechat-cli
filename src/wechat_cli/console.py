import json
import os
import shlex
import sys
import uuid

from .config import Config
from .errors import AutomationError
from .registry import capabilities
from . import service


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def print_help(method_name=None):
    methods = capabilities()["methods"]
    selected = [item for item in methods if item["method"] == method_name] if method_name else methods
    if method_name and not selected:
        print(f"Unknown method: {method_name}", file=sys.stderr)
        return
    print("Commands:")
    print("  /help [METHOD]                         Show all methods or one method schema")
    print("  /methods [PREFIX]                      List methods, optionally filtered by prefix")
    print("  METHOD JSON [--key KEY] [--confirm TOKEN]")
    print("  /call METHOD JSON [--key KEY] [--confirm TOKEN]")
    print("  /url | /status | /doctor | /mcp | /exit")
    print("Human-friendly shortcuts:")
    print("  /chat CHAT TEXT                        Send a message, e.g. /chat False 111")
    print("  /open CHAT | /read CHAT [LIMIT]        Open a chat or read up to 30 messages")
    print("  /find CHAT TEXT | /recall CHAT TEXT    Search recent messages or recall one")
    print("  /contact CHAT | /pat CHAT [self|other] Read contact details or pat an avatar")
    print("  /moments CHAT | /like CHAT POST        Open Moments or like a text post")
    print("  /unlike CHAT POST | /feed              Remove a like or open the Moments feed")
    print("JSON containing spaces must be quoted. Example:")
    print("  message.read '{\"chat\":\"False\",\"limit\":30}'")
    print("  message.send '{\"chat\":\"False\",\"text\":\"111111\"}' --key send-false-001")
    print("Destructive methods first return a confirm_token. Repeat the identical command within 120 seconds")
    print("with --confirm TOKEN and the same --key KEY. A generated key is printed for convenience.\n")
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

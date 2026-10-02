import argparse
import json
import os
import sys
import uuid
from dataclasses import replace

from .config import Config
from .errors import AutomationError
from .protocol import MAX_REQUEST_BYTES, decode, encode, failure
from .registry import capabilities


def parser():
    result = argparse.ArgumentParser(prog="wechat-cli", description="JSON-first Linux WeChat automation")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="List methods and parameter schemas without a display")
    methods = commands.add_parser("methods", help="List callable WeChat methods and CLI syntax")
    methods.add_argument("method", nargs="?", help="Show details for one method")
    commands.add_parser("doctor", help="Inspect the connected desktop")
    call = commands.add_parser("call", help="Call a method with a JSON object")
    call.add_argument("method")
    call.add_argument("--params", default="{}")
    call.add_argument("--id")
    call.add_argument("--idempotency-key")
    call.add_argument("--confirm-token")
    commands.add_parser("stdio", help="Persistent newline-JSON request/response adapter")
    commands.add_parser("mcp", help="Start the Model Context Protocol stdio server")
    demo = commands.add_parser("demo", help="Start the local browser demo controller")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    service = commands.add_parser("service", help="Manage the local desktop service")
    service.add_argument("action", choices=("start", "serve", "stop", "status"))
    woc = commands.add_parser("woc", help="Install or inspect the optional WechatOnCloud backend")
    woc.add_argument("action", choices=("install", "status"))
    woc.add_argument("container", nargs="?", help="WechatOnCloud instance container name")
    target = commands.add_parser("target", help="Show or switch the runtime target without restarting")
    target.add_argument("mode", nargs="?", choices=("local", "woc"))
    target.add_argument("container", nargs="?")
    return result


def print_methods(method_name=None):
    methods = capabilities()["methods"]
    selected = [item for item in methods if item["method"] == method_name] if method_name else methods
    if method_name and not selected:
        raise AutomationError("UNKNOWN_METHOD", f"Unknown method: {method_name}")
    print("Usage: wechat-cli call METHOD --params JSON [--idempotency-key KEY] [--confirm-token TOKEN]")
    print("Interactive console: METHOD JSON [--key KEY] [--confirm TOKEN]")
    print("Destructive methods must first obtain a confirm_token, then repeat the identical request within 120 seconds.")
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
            flags.append("idempotency key")
        if item.get("destructive"):
            flags.append("confirmation")
        suffix = f"; requires {', '.join(flags)}" if flags else ""
        print(f"\n{item['method']}\n  {item.get('description', '')}\n  required: {required}\n  parameters: {parameters}{suffix}")


def emit(response):
    try:
        sys.stdout.buffer.write(encode(response))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        raise SystemExit(0) from None


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.command == "capabilities":
        emit({"protocol_version": 1, "ok": True, "result": capabilities()})
        return
    try:
        if arguments.command == "methods":
            print_methods(arguments.method)
            return
        config = Config.from_env()
        if sys.platform != "linux":
            raise AutomationError("LINUX_REQUIRED", "Run inside Linux or WSL")
        from . import service
        if arguments.command == "target":
            params = {"target": arguments.mode} if arguments.mode else {}
            if arguments.container:
                params["container"] = arguments.container
            response = service.call(config, {"method": "target.select" if arguments.mode else "target.status",
                                             "params": params})
        elif arguments.command == "woc":
            from . import woc
            if arguments.container:
                config = replace(config, woc_container=arguments.container)
            result = woc.install(config) if arguments.action == "install" else woc.inspect(config)
            response = {"protocol_version": 1, "ok": True, "result": result}
        elif arguments.command == "service":
            if arguments.action == "serve":
                if config.target == "woc" and os.environ.get("WECHAT_WOC_WORKER") != "1":
                    raise AutomationError("WOC_SERVICE_MANAGED", "Use service start; the worker serves inside the container")
                service.serve(config)
                return
            if arguments.action == "start":
                response = service.start(config)
            elif arguments.action == "stop":
                response = service.stop(config)
            else:
                response = service.status(config)
        elif arguments.command == "stdio":
            for raw in sys.stdin.buffer:
                try:
                    emit(service.call(config, decode(raw)))
                except AutomationError as error:
                    emit(failure(None, error))
            return
        elif arguments.command == "mcp":
            from .mcp import serve
            serve(config)
            return
        elif arguments.command == "demo":
            from .web import serve
            serve(config, arguments.host, arguments.port)
            return
        else:
            request = {"id": getattr(arguments, "id", None) or str(uuid.uuid4()),
                       "method": "doctor" if arguments.command == "doctor" else arguments.method}
            if arguments.command == "call":
                request["params"] = decode(arguments.params.encode())
                if arguments.idempotency_key:
                    request["idempotency_key"] = arguments.idempotency_key
                if arguments.confirm_token:
                    request["confirm_token"] = arguments.confirm_token
            response = service.call(config, request)
        emit(response)
        if not response.get("ok"):
            raise SystemExit(1)
    except AutomationError as error:
        emit(failure(None, error))
        raise SystemExit(1)

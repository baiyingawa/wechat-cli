import argparse
import json
import sys
import uuid

from .config import Config
from .errors import AutomationError
from .protocol import MAX_REQUEST_BYTES, decode, encode, failure
from .registry import capabilities


def parser():
    result = argparse.ArgumentParser(prog="wechat-cli", description="JSON-first Linux WeChat automation")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="List methods and parameter schemas without a display")
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
    return result


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
        config = Config.from_env()
        if sys.platform != "linux":
            raise AutomationError("LINUX_REQUIRED", "Run inside Linux or WSL")
        from . import service
        if arguments.command == "service":
            if arguments.action == "serve":
                service.serve(config)
                return
            if arguments.action == "start":
                response = service.start(config)
            elif arguments.action == "stop":
                response = service.stop(config)
            else:
                response = service.exchange(config, {"method": "ping"}, timeout=2)
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

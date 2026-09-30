"""Minimal stdio MCP adapter for the existing local JSON automation service."""

import json
import sys
import uuid

from .errors import AutomationError
from .protocol import MAX_REQUEST_BYTES, failure
from .registry import capabilities


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def tool_result(response):
    return {"content": [{"type": "text", "text": json.dumps(response, ensure_ascii=False)}],
            "isError": not response.get("ok", False)}


class MCPServer:
    def __init__(self, call):
        self.call = call

    @staticmethod
    def tools():
        return [
            {"name": "wechat_capabilities",
             "description": "List supported WeChat CLI operations, schemas, confirmation requirements, and exclusions.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
            {"name": "wechat_call",
             "description": "Execute one named WeChat CLI operation. Mutations require idempotency_key; destructive operations return a confirm_token that must be supplied in an identical second call.",
             "inputSchema": {"type": "object", "properties": {
                 "method": {"type": "string", "minLength": 1, "description": "Method from wechat_capabilities."},
                 "params": {"type": "object", "description": "Method parameter object.", "default": {}},
                 "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200},
                 "confirm_token": {"type": "string", "minLength": 1}},
                 "required": ["method"], "additionalProperties": False}},
        ]

    @staticmethod
    def error(request_id, code, message, data=None):
        result = {"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": code, "message": message}}
        if data is not None:
            result["error"]["data"] = data
        return result

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self.error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return self.error(request_id, -32600, "Invalid Request")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            requested_version = request.get("params", {}).get("protocolVersion") if isinstance(
                request.get("params", {}), dict) else None
            protocol_version = requested_version if requested_version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "wechat-cli", "version": "0.1.0"},
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools()}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
        if method != "tools/call":
            return self.error(request_id, -32601, "Method not found")
        params = request.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self.error(request_id, -32602, "Invalid params")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self.error(request_id, -32602, "Invalid tool arguments")
        try:
            if params["name"] == "wechat_capabilities":
                if arguments:
                    raise AutomationError("INVALID_PARAMS", "wechat_capabilities takes no arguments")
                response = {"protocol_version": 1, "ok": True, "result": capabilities()}
            elif params["name"] == "wechat_call":
                unknown = set(arguments) - {"method", "params", "idempotency_key", "confirm_token"}
                if unknown or not isinstance(arguments.get("method"), str):
                    raise AutomationError("INVALID_PARAMS", "Invalid wechat_call arguments",
                                          {"unknown": sorted(unknown)})
                call_params = arguments.get("params", {})
                if not isinstance(call_params, dict):
                    raise AutomationError("INVALID_PARAMS", "params must be an object")
                request_data = {"id": str(uuid.uuid4()), "method": arguments["method"],
                                "params": call_params}
                for name in ("idempotency_key", "confirm_token"):
                    if name in arguments:
                        request_data[name] = arguments[name]
                response = self.call(request_data)
            else:
                return self.error(request_id, -32602, "Unknown tool", {"name": params["name"]})
        except AutomationError as error:
            response = failure(None, error)
        return {"jsonrpc": "2.0", "id": request_id, "result": tool_result(response)}


def serve(config):
    from . import service

    server = MCPServer(lambda request: service.call(config, request))
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_REQUEST_BYTES:
            response = MCPServer.error(None, -32700, "Request exceeds 1 MiB")
        else:
            try:
                response = server.handle(json.loads(raw))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = MCPServer.error(None, -32700, "Parse error")
            except Exception as error:
                response = MCPServer.error(None, -32603, "Internal error", {"message": str(error)})
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

import json
import time

from .errors import AutomationError
from .registry import METHODS, PLANNED, capabilities, validate


MAX_REQUEST_BYTES = 1024 * 1024


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def failure(request_id, error, elapsed_ms=0):
    return {"protocol_version": 1, "id": request_id, "ok": False,
            "error": error.as_dict(), "elapsed_ms": round(elapsed_ms, 3)}


def decode(raw):
    if len(raw) > MAX_REQUEST_BYTES:
        raise AutomationError("REQUEST_TOO_LARGE", "Request exceeds 1 MiB")
    try:
        return json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, ValueError) as error:
        raise AutomationError("INVALID_JSON", "Request must be UTF-8 JSON") from error


class Dispatcher:
    def __init__(self, automation, state):
        self.automation = automation
        self.state = state

    def dispatch(self, request):
        started = time.monotonic()
        request_id = request.get("id") if isinstance(request, dict) else None
        key = None
        claimed = False
        try:
            if not isinstance(request, dict):
                raise AutomationError("INVALID_REQUEST", "Request must be an object")
            unknown = set(request) - {"id", "method", "params", "idempotency_key", "confirm_token", "protocol_version"}
            if unknown:
                raise AutomationError("INVALID_REQUEST", "Unknown request fields", {"fields": sorted(unknown)})
            if request.get("protocol_version", 1) != 1:
                raise AutomationError("PROTOCOL_VERSION", "Only protocol version 1 is supported")
            if request_id is not None and not isinstance(request_id, (str, int)):
                raise AutomationError("INVALID_REQUEST", "id must be a string or integer")
            name = request.get("method")
            if not isinstance(name, str):
                raise AutomationError("INVALID_REQUEST", "method must be a string")
            params = request.get("params", {})
            if name in ("ping", "capabilities", "metrics", "state.cleanup"):
                if params != {}:
                    raise AutomationError("INVALID_PARAMS", "This method takes no parameters")
                if name == "ping":
                    result = {"status": "ready"}
                elif name == "capabilities":
                    result = capabilities()
                elif name == "metrics":
                    result = self.automation.metrics()
                else:
                    result = self.state.cleanup()
            else:
                if name in PLANNED:
                    raise AutomationError("NOT_IMPLEMENTED", "Capability is planned, not implemented", {"method": name})
                if name not in METHODS:
                    raise AutomationError("METHOD_NOT_FOUND", "Unknown method", {"method": name})
                method = METHODS[name]
                validate(params, method)
                key = request.get("idempotency_key")
                if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 200):
                    raise AutomationError("INVALID_REQUEST", "idempotency_key must contain 1–200 characters")
                if (name in ("message.send", "message.send_file", "message.download", "message.reply", "message.forward", "message.revoke", "message.pat", "message.pat_revoke", "favorite.add", "moments.comment", "moments.publish", "contact.add", "contact.accept", "group.create", "group.invite", "group.rename", "group.announcement_set", "transfer.accept")
                        or method.destructive) and not key:
                    raise AutomationError("IDEMPOTENCY_REQUIRED", "This mutation requires a caller-generated idempotency_key")
                confirmation = request.get("confirm_token")
                if confirmation is not None and not isinstance(confirmation, str):
                    raise AutomationError("INVALID_REQUEST", "confirm_token must be a string")
                if confirmation is not None and not method.destructive:
                    raise AutomationError("INVALID_REQUEST", "confirm_token is only valid for destructive operations")
                if method.destructive:
                    cached = self.state.lookup(key, name, params)
                    if cached is not None:
                        return {**cached, "id": request_id, "replayed": True,
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
                    if not confirmation:
                        token = self.state.confirm_issue(name, params, key)
                        raise AutomationError("CONFIRMATION_REQUIRED", "Repeat this method with confirm_token within 120 seconds",
                                              {"method": name, "params": params, "confirm_token": token,
                                               "expires_in_seconds": 120})
                    self.state.confirm_consume(confirmation, name, params, key)
                if key is not None:
                    cached = self.state.claim(key, name, params)
                    if cached is not None:
                        return {**cached, "id": request_id, "replayed": True,
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
                    claimed = True
                result = getattr(self.automation, method.handler)(**params)
            response = {"protocol_version": 1, "id": request_id, "ok": True,
                        "result": result, "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
        except AutomationError as error:
            response = failure(request_id, error, (time.monotonic() - started) * 1000)
        except Exception as error:
            response = failure(request_id, AutomationError("INTERNAL_ERROR", str(error)),
                               (time.monotonic() - started) * 1000)
        if claimed:
            self.state.complete(key, response)
        return response

import json
import os
import tempfile
from dataclasses import replace

from .config import Config
from .errors import AutomationError
from .protocol import failure
from .registry import Method, validate


CONTROL_METHODS = frozenset({"target.status", "target.select"})


def is_worker():
    return os.environ.get("WECHAT_WOC_WORKER") == "1" or os.environ.get("WECHAT_SERVICE_WORKER") == "1"


def snapshot(config):
    return {"target": config.target, "container": config.woc_container if config.target == "woc" else "",
            "display": config.display, "python": config.woc_python, "woc_display": config.woc_display}


def apply(config, target):
    if not isinstance(target, dict) or target.get("target") not in ("local", "woc"):
        raise AutomationError("INVALID_TARGET", "Invalid runtime target")
    mode = target["target"]
    container = target.get("container", "")
    if mode == "woc":
        from .woc import CONTAINER_PATTERN
        if not isinstance(container, str) or not CONTAINER_PATTERN.fullmatch(container):
            raise AutomationError("WOC_CONTAINER_REQUIRED", "Specify a WechatOnCloud instance container")
    local_display = config.display if config.target == "local" else os.environ.get("WECHAT_LOCAL_DISPLAY", ":99")
    return replace(config, target=mode, woc_container=container,
                   display=target.get("display", config.woc_display if mode == "woc" else local_display),
                   woc_python=target.get("python", config.woc_python),
                   woc_display=target.get("woc_display", config.woc_display))


def resolve(config):
    if is_worker() or not isinstance(config, Config):
        return config
    try:
        target = json.loads((config.runtime_dir / "target.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (ValueError, OSError) as error:
        raise AutomationError("INVALID_TARGET", "Cannot read runtime target selection") from error
    return apply(config, target)


def select(config, params):
    if is_worker():
        raise AutomationError("HOST_REQUIRED", "Switch targets from the host CLI, Demo or MCP")
    validate(params, Method(None, "", {
        "target": {"type": "string", "enum": ["local", "woc"]},
        "container": {"type": "string", "minLength": 1, "maxLength": 128}}, ("target",)))
    if params["target"] == "local" and "container" in params:
        raise AutomationError("INVALID_PARAMS", "Local mode does not accept a container")
    chosen = apply(config, params)
    if chosen.target == "woc":
        from .woc import call
        ready = call(chosen, {"method": "ping"})
        if not ready.get("ok"):
            raise AutomationError("TARGET_UNAVAILABLE", "WOC worker is not ready", {"response": ready})
    config.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.runtime_dir.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="target-", dir=config.runtime_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(snapshot(chosen), stream)
        os.replace(temporary, config.runtime_dir / "target.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return snapshot(chosen)


def handle(config, request):
    try:
        if (set(request) - {"method", "params", "id", "protocol_version"}
                or request.get("protocol_version", 1) != 1
                or (request.get("id") is not None and not isinstance(request["id"], (str, int)))):
            raise AutomationError("INVALID_REQUEST", "Invalid target control request")
        params = request.get("params", {})
        if request["method"] == "target.select":
            result = select(config, params)
        else:
            if params != {}:
                raise AutomationError("INVALID_PARAMS", "target.status takes no parameters")
            result = snapshot(resolve(config))
        return {"protocol_version": 1, "id": request.get("id"), "ok": True, "result": result}
    except AutomationError as error:
        return failure(request.get("id"), error)


def queued_call(config, request):
    from .service import call
    clean = dict(request)
    target = clean.pop("_route", None)
    if target is not None:
        return call(apply(config, target), clean, follow_target=False)
    return call(config, clean)

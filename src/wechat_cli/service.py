import fcntl
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .errors import AutomationError
from .protocol import MAX_REQUEST_BYTES, Dispatcher, decode, encode, failure
from .state import State
from .wait import wait_until


FAST_METHODS = frozenset({"ping", "capabilities", "service.status"})


def request_timeout(config, request):
    method = request.get("method") if isinstance(request, dict) else None
    baseline = max(60, config.timeout * 12)
    if method in ("message.read", "message.search"):
        params = request.get("params", {})
        limit = params.get("limit", 30) if isinstance(params, dict) else 30
        pages = limit if type(limit) is int and 1 <= limit <= 30 else 30
        return max(baseline, pages * max(30, config.timeout * 4))
    if method in ("message.send_file", "message.download", "moments.publish", "message.forward"):
        return max(300, config.timeout * 30)
    if method in ("session.start", "session.login", "account.refresh"):
        return max(120, config.timeout * 20)
    return baseline


def exchange(config, request, timeout=None):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(request_timeout(config, request) if timeout is None else timeout)
    try:
        if (isinstance(request, dict) and isinstance(request.get("method"), str)
                and request["method"] in FAST_METHODS):
            try:
                connection.connect(str(config.control_socket_path))
            except (FileNotFoundError, ConnectionRefusedError):
                connection.connect(str(config.socket_path))
        else:
            connection.connect(str(config.socket_path))
        connection.sendall(encode(request))
        with connection.makefile("rb") as stream:
            raw = stream.readline(16 * MAX_REQUEST_BYTES + 1)
        if not raw or not raw.endswith(b"\n"):
            raise AutomationError("INVALID_RESPONSE", "Service returned an incomplete response")
        return decode_response(raw)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise AutomationError("SERVICE_UNAVAILABLE", "Start the local service first", retryable=True) from error
    except (socket.timeout, ConnectionResetError, BrokenPipeError) as error:
        raise AutomationError("TRANSPORT_UNKNOWN", "Request outcome is unknown; do not blindly retry mutations") from error
    finally:
        connection.close()


def decode_response(raw):
    import json
    try:
        return json.loads(raw)
    except ValueError as error:
        raise AutomationError("INVALID_RESPONSE", "Service returned invalid JSON") from error


def start(config):
    try:
        return exchange(config, {"method": "ping"}, timeout=1)
    except AutomationError as error:
        if error.code != "SERVICE_UNAVAILABLE":
            raise
    config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.runtime_dir.chmod(0o700)
    process = subprocess.Popen([sys.executable, "-m", "wechat_cli", "service", "serve"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ, "WECHAT_DISPLAY": config.display})
    ready, _, _ = select.select([process.stdout], [], [], 8)
    if not ready:
        process.terminate()
        raise AutomationError("SERVICE_START_FAILED", "Service did not become ready within 8 seconds")
    response = decode_response(process.stdout.readline())
    process.stdout.close()
    if not response.get("ok"):
        if response.get("error", {}).get("code") == "SERVICE_ALREADY_RUNNING":
            def ready_service():
                try:
                    return exchange(config, {"method": "ping"}, timeout=1)
                except AutomationError as error:
                    if error.code != "SERVICE_UNAVAILABLE":
                        raise
                    return None
            return wait_until(ready_service, lambda seconds: select.select([], [], [], seconds),
                              8, "the existing service to become ready").value
        raise AutomationError("SERVICE_START_FAILED", "Service failed to start", {"response": response})
    return response


def call(config, request):
    try:
        return exchange(config, request)
    except AutomationError as error:
        if error.code != "SERVICE_UNAVAILABLE":
            raise
        start(config)
        return exchange(config, request)


def serve(config):
    from .automation import Automation

    config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.runtime_dir.chmod(0o700)
    lock = (config.runtime_dir / "service.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(encode(failure(None, AutomationError("SERVICE_ALREADY_RUNNING", "Another service owns the desktop"))).decode(),
              end="", flush=True)
        return
    for path in (config.socket_path, config.control_socket_path):
        if path.is_symlink():
            raise AutomationError("UNSAFE_SOCKET", "Socket path must not be a symbolic link")
        path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(config.socket_path))
    config.socket_path.chmod(0o600)
    listener.listen(16)
    control_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control_listener.bind(str(config.control_socket_path))
    config.control_socket_path.chmod(0o600)
    control_listener.listen(16)
    state = State(config.state_dir)
    automation = Automation(config, state)
    running = True
    workload = {"status": "ready", "busy": False, "method": None, "request_id": None}
    dispatcher = Dispatcher(automation, state, service_status=lambda: dict(workload))
    last_request_completed = time.monotonic()

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def control_loop():
        while running:
            ready, _, _ = select.select([control_listener], [], [], 0.2)
            if not ready:
                continue
            connection, _ = control_listener.accept()
            with connection:
                connection.settimeout(3)
                try:
                    with connection.makefile("rb") as stream:
                        raw = stream.readline(MAX_REQUEST_BYTES + 1)
                    if not raw.endswith(b"\n"):
                        raise AutomationError("INVALID_REQUEST", "Request must be newline terminated")
                    request = decode(raw)
                    if (not isinstance(request, dict) or not isinstance(request.get("method"), str)
                            or request["method"] not in FAST_METHODS):
                        raise AutomationError("METHOD_NOT_FOUND", "Control socket accepts only service status queries")
                    response = dispatcher.dispatch(request)
                except AutomationError as error:
                    response = failure(None, error)
                except OSError:
                    continue
                try:
                    connection.sendall(encode(response))
                except OSError:
                    pass

    control_thread = threading.Thread(target=control_loop, daemon=True)
    control_thread.start()
    (config.runtime_dir / "service.pid").write_text(str(os.getpid()))
    print(encode({"ok": True, "result": {"status": "ready", "pid": os.getpid(),
                                         "socket": str(config.socket_path)}}).decode(), end="", flush=True)
    try:
        state.cleanup(config.retention_days)
        while running:
            ready, _, _ = select.select([listener], [], [], 0.5)
            if not ready:
                if (time.monotonic() - last_request_completed >= 300
                        and state.account_refresh_due()):
                    try:
                        workload = {"status": "ready", "busy": True, "method": "account.refresh", "request_id": None}
                        automation.refresh_account_profile()
                    except AutomationError:
                        pass
                    finally:
                        workload = {"status": "ready", "busy": False, "method": None, "request_id": None}
                        last_request_completed = time.monotonic()
                continue
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                try:
                    with connection.makefile("rb") as stream:
                        raw = stream.readline(MAX_REQUEST_BYTES + 1)
                        if not raw.endswith(b"\n"):
                            raise AutomationError("INVALID_REQUEST", "Request must be newline terminated")
                        request = decode(raw)
                    workload = {"status": "ready", "busy": True,
                                "method": request.get("method") if isinstance(request, dict) else None,
                                "request_id": request.get("id") if isinstance(request, dict) else None}
                    try:
                        response = dispatcher.dispatch(request)
                    finally:
                        workload = {"status": "ready", "busy": False, "method": None, "request_id": None}
                except AutomationError as error:
                    response = failure(None, error)
                except (TimeoutError, OSError):
                    continue
                try:
                    connection.sendall(encode(response))
                except OSError:
                    pass
                last_request_completed = time.monotonic()
    finally:
        running = False
        control_thread.join()
        control_listener.close()
        config.control_socket_path.unlink(missing_ok=True)
        automation.close()
        state.close()
        listener.close()
        config.socket_path.unlink(missing_ok=True)
        (config.runtime_dir / "service.pid").unlink(missing_ok=True)
        lock.close()


def stop(config):
    path = config.runtime_dir / "service.pid"
    if not path.exists():
        return {"ok": True, "result": {"status": "not_running"}}
    process_id = int(path.read_text())
    command = Path(f"/proc/{process_id}/cmdline")
    if not command.exists():
        return {"ok": True, "result": {"status": "not_running"}}
    if b"wechat_cli" not in command.read_bytes():
        raise AutomationError("PID_MISMATCH", "Refusing to signal an unrelated process")
    try:
        descriptor = os.pidfd_open(process_id)
    except ProcessLookupError:
        return {"ok": True, "result": {"status": "not_running"}}
    try:
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        ready, _, _ = select.select([descriptor], [], [], 10)
        if not ready:
            raise AutomationError("SERVICE_BUSY", "Service is finishing an in-flight operation")
    finally:
        os.close(descriptor)
    return {"ok": True, "result": {"status": "stopped", "pid": process_id}}

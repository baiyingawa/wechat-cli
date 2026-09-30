import os
import base64
import pty
import secrets
import select
import signal
import shutil
import socket
import subprocess
import time
import threading
import uuid
from pathlib import Path

from Xlib.error import DisplayConnectionError

from .errors import AutomationError
from .wait import wait_until


def process_running(name):
    result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    if result.returncode:
        return False
    for candidate in result.stdout.split():
        try:
            status = open(f"/proc/{candidate}/stat", encoding="utf-8").read()
            if status.rsplit(") ", 1)[1][0] != "Z":
                return True
        except (OSError, IndexError):
            continue
    return False


def display_ready(name):
    from Xlib import display
    try:
        client = display.Display(name)
        client.close()
        return True
    except (DisplayConnectionError, OSError, RuntimeError):
        return False


def launch(command, display=None):
    environment = {**os.environ, **({"DISPLAY": display} if display else {})}
    if Path(command[0]).name == "x11vnc":
        environment.pop("WAYLAND_DISPLAY", None)
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            env=environment)


def session_start(config):
    if not shutil.which("Xvfb"):
        raise AutomationError("DISPLAY_UNAVAILABLE", "Install Xvfb before starting a session")
    launched = {}
    if not display_ready(config.display):
        launch(["Xvfb", config.display, "-screen", "0",
                f"{config.width}x{config.height}x24", "-nolisten", "tcp"])
        wait_until(lambda: display_ready(config.display), lambda timeout: time.sleep(timeout),
                   5, "virtual X display")
        launched["display"] = True
    if not process_running("wechat"):
        client = shutil.which("wechat") or "/opt/wechat/wechat"
        if not os.path.isfile(client):
            raise AutomationError("CLIENT_UNAVAILABLE", "Set up the Linux WeChat client first",
                                  {"searched": ["PATH", "/opt/wechat/wechat"]})
        launch([client], config.display)
        wait_until(lambda: process_running("wechat"), lambda timeout: time.sleep(timeout),
                   5, "WeChat process")
        launched["client"] = True
    return {"display": config.display, "resolution": [config.width, config.height],
            "launched": launched, "manual_login_required": True}


def start_remote(config):
    if not shutil.which("x11vnc"):
        raise AutomationError("VNC_UNAVAILABLE", "Install x11vnc first")
    password_file = config.state_dir / "vnc.passwd"
    password_plain = config.state_dir / "vnc.secret"
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.state_dir.chmod(0o700)
    if password_file.is_symlink() or password_plain.is_symlink():
        raise AutomationError("UNSAFE_CREDENTIAL", "VNC credentials must not be symlinks")
    if not password_file.exists():
        password = password_plain.read_text(encoding="ascii") if password_plain.exists() else secrets.token_urlsafe(6)[:8]
        create_vnc_password(password, password_file)
        if not password_plain.exists():
            with password_plain.open("x", encoding="ascii") as file:
                file.write(password)
        password_plain.chmod(0o600)
        password_file.chmod(0o600)
    if not password_plain.exists():
        raise AutomationError("VNC_PASSWORD_MISSING", "Credential file exists without a readable secret")
    password_file.chmod(0o600)
    password_plain.chmod(0o600)
    port = config.vnc_port if isinstance(getattr(config, "vnc_port", None), int) else 5909
    if local_port_ready(port) and not remote_process_running(config.display, password_file, port):
        raise AutomationError("VNC_PORT_IN_USE", f"Port {port} belongs to another service")
    if not local_port_ready(port):
        launch(["x11vnc", "-display", config.display, "-localhost", "-forever", "-shared",
                "-rfbauth", str(password_file), "-rfbport", str(port), "-quiet"], config.display)
    wait_until(lambda: local_port_ready(port), lambda timeout: time.sleep(timeout), 5, "local VNC port")
    if not remote_process_running(config.display, password_file, port):
        raise AutomationError("VNC_PORT_IN_USE", f"Port {port} does not match this display and credential")
    return {"enabled": True, "transport": "vnc", "bind": "127.0.0.1", "port": port,
            "windows_endpoint": f"127.0.0.1:{port}",
            "password": password_plain.read_text(encoding="ascii"),
            "tunnel": f"ssh -L {port}:127.0.0.1:{port} USER@HOST",
            "security_note": "VNC authentication is legacy; use an SSH tunnel over the network"}


def find_free_port(requested=0):
    if requested:
        if local_port_ready(requested):
            raise AutomationError("VNC_PORT_IN_USE", f"Port {requested} is already in use")
        return requested
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_link(config, ttl=300, scale_percent=50):
    if not shutil.which("x11vnc"):
        raise AutomationError("VNC_UNAVAILABLE", "Install x11vnc before creating a link")
    if ttl < 30 or ttl > 3600:
        raise AutomationError("INVALID_PARAMS", "Link lifetime must be between 30 and 3600 seconds")
    if type(scale_percent) is not int or not 25 <= scale_percent <= 100:
        raise AutomationError("INVALID_PARAMS", "scale_percent must be between 25 and 100")
    state_dir = config.state_dir / "links"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    link_id = uuid.uuid4().hex
    password = secrets.token_urlsafe(8)[:8]
    password_file = state_dir / f"{link_id}.passwd"
    port = find_free_port(getattr(config, "link_port", 0))
    try:
        create_vnc_password(password, password_file)
        password_file.chmod(0o600)
        encoded_password = base64.b64encode(password_file.read_bytes()).decode("ascii")
        log_dir = config.state_dir / "logs"
        log_dir.mkdir(mode=0o700, exist_ok=True)
        log_dir.chmod(0o700)
        log_file = log_dir / f"link-{link_id}.log"
        with log_file.open("x"):
            pass
        log_file.chmod(0o600)
        process = launch(["x11vnc", "-display", config.display, "-localhost", "-listen", "127.0.0.1", "-no6", "-once",
                          "-nevershared", "-rfbauth", str(password_file), "-rfbport", str(port),
                          "-timeout", str(ttl), "-o", str(log_file), "-noxrecord",
                          "-scale", str(scale_percent / 100)], config.display)
        def ready():
            if process.poll() is not None:
                raise AutomationError("VNC_START_FAILED", "One-time VNC server exited; inspect the local log",
                                      {"log_path": str(log_file)})
            return local_port_ready(port) and remote_process_running(config.display, password_file, port)

        wait_until(ready, lambda timeout: time.sleep(timeout),
                   5, "one-time VNC port")
    except Exception:
        if "process" in locals() and process.poll() is None:
            process.terminate()
        password_file.unlink(missing_ok=True)
        raise

    def reap():
        try:
            process.wait()
        finally:
            password_file.unlink(missing_ok=True)

    threading.Thread(target=reap, name=f"wechat-link-{link_id}", daemon=True).start()
    return {"link_id": link_id, "transport": "vnc", "bind": "127.0.0.1", "port": port,
            "windows_endpoint": f"127.0.0.1:{port}", "password": password,
            "password_file_base64": encoded_password,
            "expires_in_seconds": ttl, "one_time": True,
            "log_path": str(log_file),
            "scale_percent": scale_percent,
            "security_note": "VNC accepts one viewer and listens on localhost only; use SSH forwarding for servers",
            "paste_method": "ui.paste_text"}


def stop_link(config, link_id):
    if not isinstance(link_id, str) or len(link_id) != 32 or any(character not in "0123456789abcdef" for character in link_id):
        raise AutomationError("INVALID_PARAMS", "link_id must be a 32-character lowercase hex identifier")
    password_file = config.state_dir / "links" / f"{link_id}.passwd"
    if password_file.is_symlink():
        raise AutomationError("UNSAFE_CREDENTIAL", "VNC credentials must not be symlinks")
    processes = subprocess.run(["pgrep", "-x", "x11vnc"], capture_output=True, text=True)
    stopped = []
    for candidate in processes.stdout.split():
        try:
            process_id = int(candidate)
            arguments = Path(f"/proc/{process_id}/cmdline").read_bytes().split(b"\0")
            if (os.fsencode(password_file) in arguments and config.display.encode() in arguments
                    and b"-localhost" in arguments and b"-once" in arguments):
                os.kill(process_id, signal.SIGTERM)
                stopped.append(process_id)
        except (OSError, ValueError):
            continue
    password_file.unlink(missing_ok=True)
    return {"link_id": link_id, "status": "closed", "stopped_processes": stopped}


def local_port_ready(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


def remote_process_running(display, password_file, port=5909):
    return bool(remote_process_ids(display, password_file, port))


def remote_process_ids(display, password_file, port=5909):
    matches = []
    processes = subprocess.run(["pgrep", "-x", "x11vnc"], capture_output=True, text=True)
    for process_id in processes.stdout.split():
        try:
            arguments = Path(f"/proc/{int(process_id)}/cmdline").read_bytes().split(b"\0")
        except (OSError, ValueError):
            continue
        if (b"-display" in arguments and display.encode() in arguments
                and b"-rfbauth" in arguments and os.fsencode(password_file) in arguments
                and b"-rfbport" in arguments and str(port).encode() in arguments
                and b"-localhost" in arguments):
            matches.append(int(process_id))
    return matches


def stop_remote(config):
    password_file = config.state_dir / "vnc.passwd"
    if password_file.is_symlink():
        raise AutomationError("UNSAFE_CREDENTIAL", "VNC credentials must not be symlinks")
    port = config.vnc_port if isinstance(getattr(config, "vnc_port", None), int) else 5909
    processes = remote_process_ids(config.display, password_file, port) if password_file.exists() else []
    if not processes:
        if local_port_ready(port):
            raise AutomationError("VNC_PORT_IN_USE", f"Port {port} belongs to another service")
        return {"enabled": False, "status": "already_disabled"}
    for process_id in processes:
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            continue
    wait_until(lambda: not local_port_ready(port), lambda timeout: time.sleep(timeout),
               5, "local VNC server to stop")
    return {"enabled": False, "status": "disabled", "stopped_processes": processes}


def create_vnc_password(password, path):
    master, slave = pty.openpty()
    process = None
    completed = False
    try:
        process = subprocess.Popen(["x11vnc", "-storepasswd", str(path)],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True)
        os.close(slave)
        slave = -1
        output = b""
        stage = 0
        deadline = time.monotonic() + 5
        prompts = (b"Enter VNC password:", b"Verify password:", b"[y]/n")
        replies = (password.encode() + b"\n", password.encode() + b"\n", b"y\n")
        while time.monotonic() < deadline and stage < len(prompts):
            if not select.select([master], [], [], min(0.2, deadline - time.monotonic()))[0]:
                continue
            try:
                output += os.read(master, 4096)
            except OSError:
                break
            if prompts[stage] in output:
                os.write(master, replies[stage])
                stage += 1
                output = b""
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0 or not Path(path).is_file():
            raise AutomationError("VNC_PASSWORD_FAILED", "Could not create VNC credentials")
        completed = True
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AutomationError("VNC_PASSWORD_FAILED", "Could not create VNC credentials") from error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if slave >= 0:
            os.close(slave)
        os.close(master)
        if not completed:
            Path(path).unlink(missing_ok=True)

"""Optional Docker relay for a WechatOnCloud instance.

The worker runs inside the WOC container so X11 automation talks to the same
display as the browser viewer. The normal local service remains unchanged.
"""

import json
import os
import re
import shutil
import subprocess
import io
import tarfile
from pathlib import Path

from .errors import AutomationError
from .protocol import encode


CONTAINER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_USER = "abc"


def container_name(config):
    name = getattr(config, "woc_container", "") or os.environ.get("WECHAT_WOC_CONTAINER", "")
    if not name or not CONTAINER_PATTERN.fullmatch(name):
        raise AutomationError(
            "WOC_CONTAINER_REQUIRED",
            "Set WECHAT_WOC_CONTAINER to a running WechatOnCloud container such as woc-wx-abc123",
        )
    return name


def docker_path():
    path = shutil.which("docker")
    if not path:
        raise AutomationError("DOCKER_UNAVAILABLE", "Install Docker or expose the WSL Docker CLI")
    return path


def _command(config, *arguments):
    return [docker_path(), "exec", *arguments]


def _worker_command(config, *arguments):
    return _command(
        config,
        "-i",
        "--user",
        os.environ.get("WECHAT_WOC_USER", DEFAULT_USER),
        "--env",
        "WECHAT_TARGET=woc",
        "--env",
        "WECHAT_WOC_WORKER=1",
        "--env",
        f"WECHAT_DISPLAY={getattr(config, 'woc_display', ':1') or ':1'}",
        "--env",
        "XDG_STATE_HOME=/config/.local/state",
        "--env",
        "XDG_DATA_HOME=/config/.local/share",
        "--env",
        "XDG_RUNTIME_DIR=/tmp/wechat-cli-runtime",
        "--env",
        f"WECHAT_TIMEOUT={config.timeout}",
        "--env",
        f"WECHAT_RETENTION_DAYS={config.retention_days}",
        "--workdir",
        "/config/.wechat-cli",
        container_name(config),
        getattr(config, "woc_python", "") or "/config/.wechat-cli/venv/bin/python",
        "-m",
        "wechat_cli",
        *(arguments or ("stdio",)),
    )


def _run(config, command, request=None, timeout=None):
    try:
        completed = subprocess.run(
            command,
            input=request if isinstance(request, bytes) else encode(request) if request is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AutomationError(
            "TRANSPORT_UNKNOWN",
            "Timed out waiting for the WechatOnCloud worker; do not blindly retry mutations",
            {"container": container_name(config), "timeout_s": timeout},
            retryable=False,
        ) from error
    except OSError as error:
        raise AutomationError("WOC_TRANSPORT_FAILED", str(error), retryable=True) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AutomationError(
            "WOC_WORKER_FAILED",
            detail or "WechatOnCloud worker exited with a failure",
            {"container": container_name(config), "exit_code": completed.returncode},
            retryable=False,
        )
    return completed.stdout


def call(config, request):
    from .service import request_timeout

    raw = _run(config, _worker_command(config), request, request_timeout(config, request))
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AutomationError(
            "INVALID_RESPONSE",
            "WechatOnCloud worker returned unexpected output",
            {"container": container_name(config), "line_count": len(lines)},
        )
    try:
        response = json.loads(lines[0])
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise ValueError("Missing response envelope")
        return response
    except (ValueError, UnicodeDecodeError) as error:
        raise AutomationError(
            "INVALID_RESPONSE",
            "WechatOnCloud worker returned invalid JSON",
            {"container": container_name(config)},
        ) from error


def service_action(config, action):
    if action == "start":
        return call(config, {"method": "ping"})
    if action == "status":
        return call(config, {"method": "service.status"})
    if action == "stop":
        raw = _run(config, _worker_command(config, "service", "stop"), timeout=15)
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) != 1:
            raise AutomationError("INVALID_RESPONSE", "WechatOnCloud service returned unexpected output")
        try:
            return json.loads(lines[0])
        except ValueError as error:
            raise AutomationError("INVALID_RESPONSE", "WechatOnCloud service returned invalid JSON") from error
    raise AutomationError("INVALID_PARAMS", "Unknown WechatOnCloud service action")


def inspect(config):
    raw = _run(config, [docker_path(), "inspect", "-f", "{{.State.Running}}", container_name(config)], timeout=10)
    running = raw.decode("utf-8", errors="replace").strip().lower() == "true"
    return {"target": "woc", "container": container_name(config), "running": running,
            "display": getattr(config, "woc_display", ":1") or ":1"}


def install(config):
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file():
        raise AutomationError("WOC_SOURCE_REQUIRED", "Run woc install from an editable source checkout")
    status = inspect(config)
    if not status["running"]:
        raise AutomationError("WOC_NOT_RUNNING", "Start the WechatOnCloud instance before installing")
    user = os.environ.get("WECHAT_WOC_USER", DEFAULT_USER)
    setup = """set -eu
missing=''
for package in python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng xclip; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
        missing="$missing $package"
    fi
done
if [ -n "$missing" ]; then
    apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
fi
mkdir -p /config/.wechat-cli/project
chown -R "$1" /config/.wechat-cli
"""
    _run(config, _command(config, "--user", "root", container_name(config),
                         "sh", "-c", setup, "wechat-cli-install", user), timeout=600)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        bundle.add(root / "pyproject.toml", arcname="pyproject.toml")
        bundle.add(root / "src/wechat_cli", arcname="src/wechat_cli",
                   filter=lambda entry: None if "__pycache__" in entry.name or entry.name.endswith(".pyc") else entry)
    _run(config, _command(config, "-i", "--user", user, container_name(config),
                         "tar", "-xf", "-", "-C", "/config/.wechat-cli/project"), archive.getvalue(), 30)
    setup_python = """set -eu
python3 -m venv /config/.wechat-cli/venv
/config/.wechat-cli/venv/bin/python -m pip install --prefer-binary --timeout 20 --retries 2 /config/.wechat-cli/project
"""
    pip_environment = []
    if os.environ.get("WECHAT_WOC_PIP_INDEX_URL"):
        pip_environment = ["--env", f"PIP_INDEX_URL={os.environ['WECHAT_WOC_PIP_INDEX_URL']}"]
    _run(config, _command(config, "--user", user, *pip_environment, container_name(config),
                         "sh", "-c", setup_python), timeout=600)
    service_action(config, "stop")
    return {**status, "installed": True, "service": service_action(config, "start")}


def read_login_qr(config, path):
    if not re.fullmatch(r"/config/\.local/state/wechat-cli/screenshots/login-qr-[0-9]+\.png", path):
        raise AutomationError("INVALID_ARTIFACT", "Unexpected login QR artifact path")
    return _run(config, _command(config, "--user", os.environ.get("WECHAT_WOC_USER", DEFAULT_USER),
                                container_name(config), "cat", "--", path), timeout=10)

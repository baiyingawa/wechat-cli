import os
import shutil
import subprocess
import time
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
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            env=environment)


def session_start(config):
    if getattr(config, "target", "local") == "woc":
        if not display_ready(config.display):
            raise AutomationError("WOC_DISPLAY_UNAVAILABLE", "Start the desktop in WechatOnCloud first")
        return {"display": config.display, "managed_by": "WechatOnCloud", "launched": {},
                "manual_login_required": True}
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

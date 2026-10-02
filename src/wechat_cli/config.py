import os
from dataclasses import dataclass
from pathlib import Path

from .errors import AutomationError


@dataclass(frozen=True)
class Config:
    display: str = ":99"
    timeout: float = 5.0
    retention_days: int = 15
    width: int = 3840
    height: int = 2160
    target: str = "local"
    woc_container: str = ""
    woc_python: str = "/config/.wechat-cli/venv/bin/python"
    woc_display: str = ":1"

    @property
    def state_dir(self):
        base = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
        return Path(base) / "wechat-cli"

    @property
    def data_dir(self):
        base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
        return Path(base) / "wechat-cli"

    @property
    def quality_tessdata_dir(self):
        return self.data_dir / "tessdata_best"

    @property
    def runtime_dir(self):
        base = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/wechat-cli-{os.getuid()}")
        return Path(base) / "wechat-cli"

    @property
    def socket_path(self):
        return self.runtime_dir / "service.sock"

    @property
    def control_socket_path(self):
        return self.runtime_dir / "control.sock"

    @classmethod
    def from_env(cls):
        try:
            width = int(os.environ.get("WECHAT_WIDTH", "3840"))
            height = int(os.environ.get("WECHAT_HEIGHT", "2160"))
            timeout = float(os.environ.get("WECHAT_TIMEOUT", "5"))
            retention_days = int(os.environ.get("WECHAT_RETENTION_DAYS", "15"))
        except ValueError as error:
            raise AutomationError("INVALID_CONFIG", "WeChat CLI numeric environment setting is invalid") from error
        if width < 800 or height < 600 or timeout <= 0 or retention_days < 0:
            raise AutomationError("INVALID_CONFIG", "Invalid WeChat CLI dimensions, timeout, or retention period")
        target = os.environ.get("WECHAT_TARGET", "local").strip().lower()
        if target not in ("local", "woc"):
            raise AutomationError("INVALID_CONFIG", "WECHAT_TARGET must be local or woc")
        default_display = os.environ.get("WECHAT_WOC_DISPLAY", ":1") if target == "woc" else ":99"
        return cls(target=target, display=os.environ.get("WECHAT_DISPLAY", default_display), timeout=timeout,
                   retention_days=retention_days, width=width, height=height,
                   woc_container=os.environ.get("WECHAT_WOC_CONTAINER", "").strip(),
                   woc_python=os.environ.get("WECHAT_WOC_PYTHON", "/config/.wechat-cli/venv/bin/python").strip(),
                   woc_display=os.environ.get("WECHAT_WOC_DISPLAY", ":1").strip())

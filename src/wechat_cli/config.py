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
        return cls(display=os.environ.get("WECHAT_DISPLAY", ":99"), timeout=timeout,
                   retention_days=retention_days, width=width, height=height)

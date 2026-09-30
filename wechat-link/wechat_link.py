from __future__ import annotations

import json
import base64
import os
import queue
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk


APP_NAME = "wechat-link"
DEFAULT_PROJECT = ""


@dataclass
class Target:
    name: str
    kind: str = "wsl"
    distro: str = "Ubuntu-24.04"
    address: str = ""
    project_dir: str = DEFAULT_PROJECT
    display: str = ":99"
    scale_percent: int = 50


def config_path():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
    return Path(base) / APP_NAME / "targets.json"


def load_targets():
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        targets = [Target(**item) for item in data if isinstance(item, dict)]
        if targets:
            return targets
    except (OSError, ValueError, TypeError):
        pass
    project_dir = ""
    if os.name == "nt":
        try:
            project_dir = subprocess.run(["wsl.exe", "--", "wslpath", "-a", Path(__file__).resolve().parents[1].as_posix()],
                                         capture_output=True, text=True, timeout=10,
                                         creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return [Target(name="WSL Ubuntu-24.04", project_dir=project_dir)]


def save_targets(targets):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps([asdict(item) for item in targets], ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(path)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def cli_command(target, method, params):
    payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    prefix = f"cd -- {shlex.quote(target.project_dir)} && PYTHONPATH=src " if target.project_dir else ""
    remote = (prefix + f"WECHAT_DISPLAY={shlex.quote(target.display)} python3 -m wechat_cli call {shlex.quote(method)} "
              f"--params {shlex.quote(payload)}")
    if target.kind == "wsl":
        return ["wsl.exe", "-d", target.distro, "--", "bash", "-lc", remote]
    if target.kind == "ssh":
        if not target.address.strip():
            raise ValueError("SSH 目标需要 user@host")
        if target.address.startswith("-") or any(character.isspace() for character in target.address):
            raise ValueError("SSH 地址必须是主机名、IP 或 user@host，不能包含空格或选项")
        return ["ssh.exe", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target.address, remote]
    raise ValueError("目标类型必须是 wsl 或 ssh")


def run_remote(target, method, params, timeout=30):
    process = subprocess.run(cli_command(target, method, params), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        try:
            response = json.loads(process.stdout.splitlines()[-1])
            detail = response.get("error", {}).get("message", "远端命令失败")
        except (ValueError, IndexError):
            detail = process.stderr.strip() or "远端命令失败"
        raise RuntimeError(detail)
    lines = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if not response.get("ok"):
            raise RuntimeError(response.get("error", {}).get("message", "wechat-cli 调用失败"))
        return response.get("result", {})
    raise RuntimeError("远端没有返回有效 JSON；请检查项目路径和 Python 环境")


def find_executable(names):
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "TigerVNC" / "vncviewer.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "TigerVNC" / "vncpasswd.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "TigerVNC" / "vncviewer.exe",
    ]
    for candidate in candidates:
        if candidate.name in names and candidate.is_file():
            return str(candidate)
    return None


class LinkSession:
    def __init__(self, target, log):
        self.target = target
        self.log = log
        self.viewer = None
        self.tunnel = None
        self.password_file = None
        self.credential_dir = None
        self.link_id = None
        self.closed = False
        self.lock = threading.Lock()

    def connect(self):
        viewer = find_executable(["vncviewer.exe", "vncviewer"])
        if viewer is None:
            raise RuntimeError("找不到 TigerVNC vncviewer.exe，请先安装 TigerVNC 或指定其路径")
        result = run_remote(self.target, "session.link", {"scale_percent": self.target.scale_percent}, timeout=120)
        self.link_id = result["link_id"]
        remote_port = int(result["port"])
        local_port = remote_port
        if self.target.kind == "ssh":
            local_port = free_port()
            self.tunnel = subprocess.Popen(
                ["ssh.exe", "-N", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes",
                 "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", self.target.address],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if self.tunnel.poll() is not None:
                raise RuntimeError("SSH 端口转发启动失败，请检查密钥认证和目标地址")
            deadline = time.monotonic() + 10
            while True:
                if self.tunnel.poll() is not None:
                    raise RuntimeError("SSH 端口转发退出，请检查密钥认证和 known_hosts")
                try:
                    with socket.create_connection(("127.0.0.1", local_port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("SSH 端口转发就绪超时")
                    time.sleep(0.03)
        endpoint = f"127.0.0.1::{local_port}"
        self.credential_dir = Path(tempfile.mkdtemp(prefix="wechat-link-"))
        if os.name == "nt":
            identity = subprocess.run(["whoami.exe"], capture_output=True, text=True,
                                      creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout.strip()
            subprocess.run(["icacls.exe", str(self.credential_dir), "/inheritance:r", "/grant:r",
                            f"{identity}:(OI)(CI)F"], capture_output=True, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            self.credential_dir.chmod(0o700)
        self.password_file = self.credential_dir / "vnc.passwd"
        credential = base64.b64decode(result["password_file_base64"], validate=True)
        if len(credential) != 8:
            raise RuntimeError("远端返回的 VNC 凭据格式错误")
        with self.password_file.open("xb") as stream:
            stream.write(credential)
        viewer_args = [viewer, "-PasswordFile", str(self.password_file), "-RemoteResize=0",
                       "-ViewOnly=0", "-SendClipboard=1", "-AcceptClipboard=1"]
        viewer_args.append(endpoint)
        self.viewer = subprocess.Popen(viewer_args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result["local_port"] = local_port
        self.log(f"已启动查看器 {endpoint}；等待首次连接上限 {result.get('expires_in_seconds')} 秒")
        threading.Thread(target=self._watch_viewer, daemon=True).start()
        return result

    def _watch_viewer(self):
        if self.viewer:
            self.viewer.wait()
        self.disconnect()
        self.log("查看器已关闭，连接通道已释放")

    def disconnect(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        if self.viewer and self.viewer.poll() is None:
            self.viewer.terminate()
            self.viewer.wait(timeout=5)
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
            self.tunnel.wait(timeout=5)
        if self.password_file:
            self.password_file.unlink(missing_ok=True)
        if self.credential_dir:
            self.credential_dir.rmdir()
        if self.link_id:
            try:
                run_remote(self.target, "session.link_close", {"link_id": self.link_id})
            except Exception:
                self.log("未能撤销远端连接；一次性 VNC 会在查看器断开后退出")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("wechat-link")
        self.root.geometry("620x470")
        self.targets = load_targets()
        self.session = None
        self.events = queue.Queue()
        self.connecting = False
        self.kind = tk.StringVar(value=self.targets[0].kind)
        self.target_name = tk.StringVar(value=self.targets[0].name)
        self.distro = tk.StringVar(value=self.targets[0].distro)
        self.address = tk.StringVar(value=self.targets[0].address)
        self.project_dir = tk.StringVar(value=self.targets[0].project_dir)
        self.scale = tk.StringVar(value=str(self.targets[0].scale_percent))
        self.status = tk.StringVar(value="未连接")
        self._build()
        self._load_target()
        self.root.after(100, self._drain_events)
        self.root.after(500, self._check_session)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._tray()

    def _build(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="wechat-link", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="WSL / SSH 一次性连接 · 原型使用共享输入，自动化期间请勿操作").grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))
        self._field(frame, 2, "目标类型", self.kind, combo=("wsl", "ssh"))
        self._field(frame, 3, "名称", self.target_name)
        self.target_picker = ttk.Combobox(frame, textvariable=self.target_name,
                                          values=[target.name for target in self.targets])
        self.target_picker.grid(row=3, column=1, sticky="ew", pady=3)
        self.target_picker.bind("<<ComboboxSelected>>", self._select_target)
        self.endpoint_label = ttk.Label(frame)
        self.endpoint_label.grid(row=4, column=0, sticky="w", pady=3)
        self.endpoint_entry = ttk.Entry(frame)
        self.endpoint_entry.grid(row=4, column=1, sticky="ew", pady=3)
        self._field(frame, 5, "项目目录", self.project_dir)
        scale_box = ttk.Combobox(frame, textvariable=self.scale, values=("25", "50", "75", "100"), state="readonly", width=5)
        scale_box.grid(row=6, column=1, sticky="w", pady=3)
        ttk.Label(frame, text="显示比例（%，自动化保持 4K）").grid(row=6, column=0, sticky="w")
        self.kind.trace_add("write", lambda *_: self._load_target())
        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=14)
        self.connect_button = ttk.Button(actions, text="连接并打开查看器", command=self.connect)
        self.connect_button.pack(side="left")
        ttk.Button(actions, text="测试中文粘贴", command=self.paste_test).pack(side="left", padx=8)
        ttk.Button(actions, text="断开", command=self.disconnect).pack(side="left")
        ttk.Button(actions, text="保存目标", command=self.save_target).pack(side="right")
        ttk.Label(frame, textvariable=self.status, foreground="#176b3a", wraplength=520).grid(row=8, column=0, columnspan=2, sticky="w")
        self.log_box = tk.Text(frame, height=8, state="disabled", font=("Consolas", 9))
        self.log_box.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(9, weight=1)

    def _field(self, parent, row, label, variable, combo=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        if combo:
            ttk.Combobox(parent, textvariable=variable, values=combo, state="readonly").grid(row=row, column=1, sticky="ew", pady=3)
        else:
            ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)

    def _load_target(self):
        is_wsl = self.kind.get() == "wsl"
        self.endpoint_label.configure(text="WSL 发行版（127.0.0.1）" if is_wsl else "SSH 地址 / IP")
        self.endpoint_entry.configure(textvariable=self.distro if is_wsl else self.address)

    def _select_target(self, event=None):
        target = next(item for item in self.targets if item.name == self.target_name.get())
        self.kind.set(target.kind)
        self.distro.set(target.distro)
        self.address.set(target.address)
        self.project_dir.set(target.project_dir)
        self.scale.set(str(target.scale_percent))

    def save_target(self):
        item = Target(self.target_name.get().strip() or "未命名", self.kind.get(), self.distro.get().strip(),
                      self.address.get().strip(), self.project_dir.get().strip(), ":99", int(self.scale.get()))
        self.targets = [old for old in self.targets if old.name != item.name] + [item]
        save_targets(self.targets)
        self.target_picker.configure(values=[target.name for target in self.targets])
        self.log("目标配置已保存")

    def connect(self):
        if self.session or self.connecting:
            self.log("已有连接")
            return
        self.save_target()
        target = Target(self.target_name.get(), self.kind.get(), self.distro.get(), self.address.get(), self.project_dir.get(),
                        scale_percent=int(self.scale.get()))
        self.status.set("正在取得一次性凭据并启动查看器…")
        self.connecting = True
        self.connect_button.configure(state="disabled")
        threading.Thread(target=self._connect_worker, args=(target,), daemon=True).start()

    def _connect_worker(self, target):
        try:
            session = LinkSession(target, self.log)
            result = session.connect()
            self.events.put(("connected", session, result))
        except Exception as error:
            if "session" in locals():
                session.disconnect()
            self.events.put(("error", str(error)))

    def paste_test(self):
        if not self.session or self.session.closed:
            messagebox.showinfo("wechat-link", "请先连接查看器，并点击其中的微信输入框")
            return
        text = simpledialog.askstring("中文输入验证", "先点击查看器中的微信输入框，再输入测试文本：",
                                      initialvalue="wechat-link 中文输入测试 你好世界")
        if not text:
            return
        target = self.session.target
        self.status.set("正在写入远端剪贴板并粘贴…")
        threading.Thread(target=self._paste_worker, args=(target, text), daemon=True).start()

    def _paste_worker(self, target, text):
        try:
            run_remote(target, "ui.paste_text", {"text": text})
            self.events.put(("status", "已粘贴中文测试文本，请在微信中核对字符是否完整"))
        except Exception as error:
            self.events.put(("error", str(error)))

    def disconnect(self):
        if self.connecting:
            self.log("连接正在建立，请稍候后断开")
            return
        if self.session:
            session = self.session
            threading.Thread(target=session.disconnect, daemon=True).start()
            self.session = None
        self.status.set("未连接")
        self.connect_button.configure(state="normal")

    def _check_session(self):
        if self.session and self.session.closed:
            self.session = None
            self.status.set("查看器已关闭")
        self.root.after(500, self._check_session)

    def log(self, message):
        self.events.put(("log", message))

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "connected":
                    self.connecting = False
                    self.session, result = event[1], event[2]
                    if self.session.closed:
                        self.session = None
                        self.status.set("查看器已关闭")
                    else:
                        self.status.set(f"查看器：127.0.0.1::{result['local_port']}（共享输入原型）")
                    self.connect_button.configure(state="normal")
                elif event[0] == "status":
                    self.status.set(event[1])
                elif event[0] == "error":
                    self.connecting = False
                    self.status.set("连接失败")
                    self.connect_button.configure(state="normal")
                    messagebox.showerror("wechat-link", event[1])
                elif event[0] == "quit":
                    self._finish_quit()
                    return
                else:
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", event[1] + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        image = Image.new("RGB", (32, 32), "#1677ff")
        ImageDraw.Draw(image).text((8, 7), "W", fill="white")
        menu = pystray.Menu(pystray.MenuItem("显示窗口", lambda: self.root.after(0, self.show)),
                            pystray.MenuItem("断开", lambda: self.root.after(0, self.disconnect)),
                            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit)))
        self.tray = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def hide(self):
        if getattr(self, "tray", None):
            self.root.withdraw()
        else:
            self.quit()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def quit(self):
        if self.connecting:
            self.log("正在连接，请等待完成后退出")
            return
        session = self.session
        if session:
            self.status.set("正在断开并撤销凭据…")
            def close_and_exit():
                try:
                    session.disconnect()
                finally:
                    self.events.put(("quit",))
            threading.Thread(target=close_and_exit, daemon=True).start()
            return
        self._finish_quit()

    def _finish_quit(self):
        if getattr(self, "tray", None):
            self.tray.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

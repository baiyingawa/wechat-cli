import argparse
import json
import subprocess
import time
from pathlib import Path

from pywinauto import Application, keyboard
import win32clipboard
import win32gui
import win32process

from wechat_link import LinkSession, Target


def read_probe(distro):
    result = subprocess.run(["wsl.exe", "-d", distro, "--", "cat", "/tmp/wechat-link-probe/input.txt"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout


def wait_text(distro, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        actual = read_probe(distro)
        if actual == expected:
            return True
        time.sleep(0.05)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--distro", default="Ubuntu-24.04")
    parser.add_argument("--project", default="/mnt/e/PROJECT/wechat-cli")
    parser.add_argument("--report", required=True)
    parser.add_argument("--scale", type=int, default=100)
    args = parser.parse_args()
    target = Target("WSL probe", distro=args.distro, project_dir=args.project, scale_percent=args.scale)
    session = LinkSession(target, lambda message: print(message))
    results = {}
    try:
        link = session.connect()
        app = Application(backend="win32").connect(process=session.viewer.pid, timeout=15)
        deadline = time.monotonic() + 15
        window = None
        while time.monotonic() < deadline:
            candidates = app.windows(visible_only=True)
            candidates = [candidate for candidate in candidates if candidate.rectangle().width() > 100]
            if candidates:
                window = max(candidates, key=lambda candidate: candidate.rectangle().width())
                if window.window_text():
                    break
            time.sleep(0.1)
        if window is None:
            raise RuntimeError("TigerVNC did not open a visible desktop window")
        time.sleep(1)
        print("viewer_title:", window.window_text())
        window.capture_as_image().save(Path(args.report).with_suffix(".png"))
        window.set_focus()
        if win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[1] != session.viewer.pid:
            raise RuntimeError("Windows refused to focus the test viewer; no keyboard input sent")
        subprocess.run(["wsl.exe", "-d", args.distro, "--", "env", "DISPLAY=:99", "python3", "-c",
                        "from Xlib import display,X; client=display.Display(); root=client.screen().root; "
                        "windows=root.query_tree().children; target=next(window for window in windows "
                        "if window.get_wm_name()=='wechat-link Qt input probe'); "
                        "target.configure(stack_mode=X.Above); target.set_input_focus(X.RevertToParent,X.CurrentTime); client.sync()"],
                       check=True)
        time.sleep(0.5)
        results["viewer_title"] = window.window_text()
        window.click_input(coords=(100, 100))
        keyboard.send_keys("^a{BACKSPACE}abc", pause=0.05, vk_packet=False)
        results["ascii_to_qt"] = wait_text(args.distro, "abc")
        results["ascii_observed"] = read_probe(args.distro)
        keyboard.send_keys("^a{BACKSPACE}", vk_packet=False)
        sample = "你好世界中文输入"
        keyboard.send_keys(sample, vk_packet=True, pause=0.03)
        results["windows_unicode_to_qt"] = wait_text(args.distro, sample)
        results["unicode_observed"] = read_probe(args.distro)
        keyboard.send_keys("^a{BACKSPACE}", vk_packet=False)
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(sample, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        win32gui.SetForegroundWindow(win32gui.GetDesktopWindow())
        time.sleep(0.1)
        window.set_focus()
        time.sleep(0.3)
        keyboard.send_keys("^v", vk_packet=False)
        results["windows_clipboard_to_qt"] = wait_text(args.distro, sample)
        results["clipboard_observed"] = read_probe(args.distro)
        results["linux_clipboard"] = subprocess.run(["wsl.exe", "-d", args.distro, "--", "env", "DISPLAY=:99",
            "xclip", "-selection", "clipboard", "-out"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        keyboard.send_keys("^a{BACKSPACE}", vk_packet=False)
        command = "import sys; sys.path.insert(0,'src'); from wechat_cli.x11 import Desktop; desktop=Desktop(':99'); desktop.begin_input(); desktop.paste('" + sample + "'); desktop.close()"
        subprocess.run(["wsl.exe", "-d", args.distro, "--cd", args.project, "--", "python3", "-c", command], check=True)
        results["command_clipboard_to_qt"] = wait_text(args.distro, sample)
        results["fallback_observed"] = read_probe(args.distro)
    finally:
        session.disconnect()
    Path(args.report).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()

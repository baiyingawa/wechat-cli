import hashlib
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from .accessibility import Accessibility
from .errors import AutomationError
from .ocr import OCR, RapidOCRReader
from .private_files import save_private_image
from .vision import (adjacent_bubble, avatar_template_match, bubble_regions, editor_has_content,
                     image_digest, merge_message_pages, runs, text_identity)
from .wait import wait_until
from .x11 import Desktop, Rect


class Automation:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        self.desktop = None
        self.ocr = OCR()
        self.ascii_ocr = OCR(language="eng")
        rapid_reader = RapidOCRReader()
        quality_data = config.quality_tessdata_dir
        self.content_ocr = rapid_reader if rapid_reader.model_available else (
            OCR(tessdata_dir=quality_data, engine_mode=1, profile="best")
            if all((quality_data / f"{language}.traineddata").is_file()
                   for language in ("chi_sim", "chi_sim_vert", "eng"))
            and (quality_data / "configs" / "tsv").is_file()
            else OCR(engine_mode=1, profile="standard_fallback"))
        self.accessibility = Accessibility()
        self.last_wait = None
        self._login_view = None
        self._login_qr = None

    def close(self):
        if self.desktop:
            self.desktop.close()

    def state_resolve(self, key, outcome):
        return self.state.resolve(key, outcome)

    def connect(self):
        if self.desktop is None:
            self.desktop = Desktop(self.config.display)
        return self.desktop

    def prepare(self):
        desktop = self.connect()
        desktop.begin_input()
        main = desktop.main_window()
        if (main.rect.width < 700 or main.rect.height < 600
                or self.login_screen_visible(main)):
            raise AutomationError("LOGIN_REQUIRED", "Complete login on the phone first")
        for window in desktop.windows():
            if window.title == "打开":
                raise AutomationError("UI_BUSY", "Close the existing file chooser before automating")
            if window.id != main.id and window.title in ("朋友圈", "设置"):
                desktop.close_window(window)
        desktop.focus(main)
        main = desktop.main_window()
        if main.rect != desktop.bounds:
            desktop.maximize(main)
            main = desktop.main_window()
        return main

    def panel_open(self, main):
        screen = self.connect().capture(Rect(main.rect.x + main.rect.width - 240,
                                              main.rect.y + 400, 8, 8))
        return min(screen.getpixel((3, 3))) >= 248

    def header_region(self, main):
        return Rect(main.rect.x + 300, main.rect.y + 25, min(600, main.rect.width - 650), 40)

    def composer_visible(self, main):
        toolbar = Rect(main.rect.x + 300, main.rect.y + main.rect.height - 147, 22, 30)
        pixels = np.asarray(self.connect().capture(toolbar))
        return int((pixels.max(axis=2) < 145).sum()) > 10

    def current_chat(self, main):
        image = self.connect().capture(self.header_region(main))
        lines = self.ocr.lines(image.resize((image.width * 2, image.height * 2)), psm=7)
        return text_identity(" ".join(line["text"] for line in lines))

    def chat_header_matches(self, observed, chat):
        normalized = text_identity(observed)
        requested = text_identity(chat)
        if normalized == requested:
            return True
        expected_group = re.fullmatch(r"(.+?)\s*[（(](\d+)[)）]", requested)
        observed_group = re.fullmatch(r"(.+?)\s*[（(](\d+)[)）]", normalized)
        if expected_group and observed_group:
            return re.sub(r"[\[\]]", "", expected_group.group(1)) == re.sub(
                r"[\[\]]", "", observed_group.group(1))
        return False

    @staticmethod
    def chat_search_label(chat):
        return re.sub(r"\s*[（(]\d+[)）]$", "", text_identity(chat))

    def semantic_wait(self, region, predicate, description, timeout=None):
        desktop = self.connect()
        previous = None
        result_value = None

        def check():
            nonlocal previous, result_value
            image = desktop.capture(region)
            digest = image_digest(image)
            if digest != previous:
                previous = digest
                result_value = predicate(image)
            return result_value

        result = wait_until(check, desktop.wake, timeout or self.config.timeout, description)
        self.last_wait = asdict(result)
        return result.value

    def doctor(self):
        desktop = self.connect()
        accessible = self.accessibility.snapshot()
        return {"display": self.config.display, "screen": desktop.bounds.as_list(),
                "windows": [window.as_dict() for window in desktop.windows()],
                "accessibility": {key: value for key, value in accessible.items() if key != "nodes"},
                "accessible_nodes": len(accessible["nodes"]),
                "tools": {name: shutil.which(name) for name in
                          ("tesseract", "xclip", "Xvfb", "openbox")},
                "performance": self.metrics()}

    def metrics(self):
        return {"desktop": self.desktop.metrics() if self.desktop else None,
                "ocr": {"fast": self.ocr.metrics(), "content": self.content_ocr.metrics(),
                        "ascii": self.ascii_ocr.metrics()},
                "last_wait": self.last_wait}

    def content_reader(self):
        return getattr(self, "content_ocr", self.ocr)

    @staticmethod
    def content_scale(reader, tesseract_scale):
        return 1 if reader.profile == "rapidocr" else tesseract_scale

    def history_manage(self):
        return {"retention_days": self.config.retention_days,
                "cleanup_method": "state.cleanup",
                "message_read_limit": 30,
                "export_supported": False,
                "migration_supported": False,
                "scope": "local_automation_state"}

    def session_status(self):
        windows = self.connect().windows()
        mains = [window for window in windows if window.title == "微信"]
        main = max(mains, key=lambda window: window.rect.width * window.rect.height) if mains else None
        if main is None:
            state = "not_running"
        elif main.rect.width < 700 or self.login_screen_visible(main):
            state = "login_required"
        else:
            state = "logged_in"
        return {"state": state,
                "window": main.as_dict() if main else None,
                "display": self.config.display}

    def login_screen_visible(self, main):
        sample = self.connect().capture(Rect(main.rect.x, main.rect.y,
                                             max(1, main.rect.width // 3), main.rect.height))
        pixels = np.asarray(sample.resize((160, 90)), dtype=np.int16)
        non_white = (pixels.min(axis=2) < 242) | ((pixels.max(axis=2) - pixels.min(axis=2)) > 12)
        return non_white.mean() < 0.025

    def session_start(self):
        from .deployment import session_start
        return session_start(self.config)

    def session_login(self):
        try:
            status = self.session_status()
        except AutomationError as error:
            if error.code != "DISPLAY_UNAVAILABLE":
                raise
            self.session_start()
            status = {"state": "not_running"}
        if status["state"] == "not_running":
            self.session_start()
            status = wait_until(
                lambda: current if (current := self.session_status())["state"] != "not_running" else None,
                lambda timeout: self.connect().wake(timeout), self.config.timeout,
                "WeChat login window").value
        if status["state"] == "not_running":
            raise AutomationError("CLIENT_NOT_RUNNING", "Could not open the WeChat login window")
        action = "none"
        if status["state"] == "login_required":
            desktop = self.connect()
            main = desktop.main_window()
            image = desktop.capture(main.rect)
            lines = self.ocr.lines(image.resize((image.width * 2, image.height * 2)), 12)
            labels = [(line["text"].replace(" ", ""), line) for line in lines]
            buttons = [line for label, line in labels if label in ("登录", "进入微信")]
            if len(buttons) == 1:
                left, top, width, height = buttons[0]["rect"]
                before = image_digest(image)
                desktop.click(main.rect.x + (left + width // 2) // 2,
                              main.rect.y + (top + height // 2) // 2)
            else:
                button = self.login_primary_button(image)
                if button is not None:
                    before = image_digest(image)
                    desktop.click(main.rect.x + button.x + button.width // 2,
                                  main.rect.y + button.y + button.height // 2)
                else:
                    before = None
            if before is not None:
                try:
                    self.semantic_wait(main.rect, lambda current: image_digest(current) != before,
                                       "login prompt response", timeout=min(2, self.config.timeout))
                except AutomationError as error:
                    if error.code != "TIMEOUT":
                        raise
                action = "requested"
            elif any("手机上完成登录" in label for label, _ in labels):
                action = "awaiting_phone"
            status = self.session_status()
        artifacts = self.login_artifacts(status["window"]["id"]) if status["state"] == "login_required" else {}
        return {**status, "login_action": action, **artifacts}

    @staticmethod
    def login_primary_button(image):
        pixels = np.asarray(image, dtype=np.int16)
        green = ((pixels[:, :, 1] > pixels[:, :, 0] + 35)
                 & (pixels[:, :, 1] > pixels[:, :, 2] + 10)
                 & (pixels[:, :, 1] > 120))
        rows = runs(green.sum(axis=1) >= image.width * 0.35, minimum=max(12, image.height // 30))
        candidates = []
        for top, bottom in rows:
            columns = runs(green[top:bottom].mean(axis=0) >= 0.35, minimum=max(40, image.width // 4))
            candidates.extend(Rect(left, top, right - left, bottom - top) for left, right in columns)
        if len(candidates) != 1:
            return None
        button = candidates[0]
        if button.y < image.height * 0.35:
            return None
        return button

    @staticmethod
    def qr_rect_from_points(points, width, height):
        coordinates = np.asarray(points, dtype=float).reshape(-1, 2)
        if coordinates.shape[0] < 4:
            return None
        left, top = coordinates.min(axis=0)
        right, bottom = coordinates.max(axis=0)
        side = max(right - left, bottom - top)
        if side < max(80, min(width, height) * 0.12):
            return None
        padding = max(8, round(side * 0.12))
        x0 = max(0, round(left - padding))
        y0 = max(0, round(top - padding))
        x1 = min(width, round(right + padding))
        y1 = min(height, round(bottom + padding))
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def detect_login_qr(self, image):
        try:
            import cv2
        except ImportError:
            return None
        width, height = image.size
        search = Rect(round(width * 0.18), round(height * 0.08),
                      round(width * 0.64), round(height * 0.84))
        candidate = np.asarray(image.crop((search.x, search.y, search.x + search.width,
                                           search.y + search.height)))
        found, points = cv2.QRCodeDetector().detect(cv2.cvtColor(candidate, cv2.COLOR_RGB2BGR))
        if not found or points is None:
            return None
        relative = self.qr_rect_from_points(points, search.width, search.height)
        if relative is None:
            return None
        return Rect(search.x + relative.x, search.y + relative.y, relative.width, relative.height)

    def save_capture(self, image, rect, prefix="screen"):
        directory = self.state.directory / "screenshots"
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / f"{prefix}-{time.time_ns()}.png"
        save_private_image(image, path)
        return {"path": str(path), "region": rect.as_list()}

    def login_artifacts(self, window_id):
        desktop = self.connect()
        window = next((item for item in desktop.windows() if item.id == window_id), None)
        if window is None:
            raise AutomationError("WINDOW_NOT_FOUND", "Login window is no longer visible")
        image = desktop.capture(window.rect)
        digest = image_digest(image)
        cached = getattr(self, "_login_view", None)
        if cached and cached["digest"] == digest:
            return {"screenshot": {**cached["screenshot"], "changed": False},
                    "qr": ({**self._login_qr, "changed": False}
                           if getattr(self, "_login_qr", None) else None)}
        screenshot = self.save_capture(image, window.rect, "login")
        screenshot["changed"] = True
        qr = None
        qr_rect = self.detect_login_qr(image)
        if qr_rect is not None:
            crop = image.crop((qr_rect.x, qr_rect.y, qr_rect.x + qr_rect.width, qr_rect.y + qr_rect.height))
            qr_digest = image_digest(crop)
            previous_qr = getattr(self, "_login_qr", None)
            if previous_qr and previous_qr["digest"] == qr_digest:
                qr = {**previous_qr, "changed": False}
            else:
                qr = self.save_capture(crop, Rect(window.rect.x + qr_rect.x, window.rect.y + qr_rect.y,
                                                   qr_rect.width, qr_rect.height), "login-qr")
                qr.update({"digest": qr_digest, "changed": True})
        self._login_view = {"digest": digest, "screenshot": screenshot}
        self._login_qr = qr
        return {"screenshot": screenshot, "qr": qr}

    def session_logout(self):
        main = self.prepare()
        desktop = self.connect()
        previous = {window.id for window in desktop.windows()}
        desktop.click(main.rect.x + 32, main.rect.y + main.rect.height - 35)
        self.choose_menu(["退出登录"], previous)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
                            if window.id != main.id and window.title == "wechat"
                            and 240 <= window.rect.width <= 500
                            and 110 <= window.rect.height <= 300), None),
                            desktop.wake, self.config.timeout, "logout confirmation dialog").value
        desktop.settle(dialog.rect)
        dialog_lines = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 3, dialog.rect.height * 3)), 11)
        observed = "".join(line["text"] for line in dialog_lines).replace(" ", "")
        if "退出登录" not in observed:
            desktop.key("Escape")
            raise AutomationError("LOGOUT_DIALOG_UNVERIFIED", "Unexpected logout confirmation dialog",
                                  {"observed": observed})
        self.click_dialog_action(dialog, dialog_lines, ["退出", "确定"])
        try:
            wait_until(lambda: self.session_status()["state"] == "login_required",
                       desktop.wake, self.config.timeout, "client logout")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Logout was confirmed but not visually verified",
                                  {"cause": error.as_dict()}) from error
        return {"state": "login_required", "status": "confirmed"}

    def screenshot(self, region=None, window_id=None):
        desktop = self.connect()
        if region is not None and window_id is not None:
            raise AutomationError("INVALID_PARAMS", "Choose region or window_id, not both")
        if window_id is not None:
            window = next((item for item in desktop.windows() if item.id == window_id), None)
            if window is None:
                raise AutomationError("WINDOW_NOT_FOUND", "Window is no longer visible")
            region = window.rect.as_list()
        rect = Rect(*region) if region is not None else desktop.bounds
        return self.save_capture(desktop.capture(rect), rect)

    def ui_windows(self):
        return {"windows": [window.as_dict() for window in self.connect().windows()]}

    def ui_tree(self):
        return self.accessibility.snapshot()

    def ui_maximize(self):
        desktop = self.connect()
        return desktop.maximize(desktop.main_window())

    def ui_reset(self):
        main = self.prepare()
        desktop = self.connect()
        panel_closed = False
        if self.panel_open(main):
            desktop.click(main.rect.x + main.rect.width - 40, main.rect.y + 50)
            wait_until(lambda: not self.panel_open(main), desktop.wake,
                       self.config.timeout, "details panel to close")
            panel_closed = True
        self.select_sidebar_tab(main, 100)
        return {"status": "reset", "panel_closed": panel_closed,
                "surface": "chat_list"}

    def account_profile(self):
        profile = self.state.account_profile()
        if profile is None or not profile.get("display_name"):
            raise AutomationError("PROFILE_UNAVAILABLE", "No cached account profile; call account.refresh")
        return {**profile, "source": "cache"}

    @staticmethod
    def extract_account_identity(lines, ascii_lines):
        name_rows = [line["text"] for line in lines
                     if line["rect"][1] < 130 and line["rect"][0] > 200
                     and "微信号" not in line["text"].replace(" ", "")]
        display_name = name_rows[0].strip() if name_rows else ""
        display_name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+$", "", display_name).strip()
        candidates = []
        for line in [*lines, *ascii_lines]:
            candidate = text_identity(line["text"]).replace(" ", "")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{5,20}", candidate):
                candidates.append(candidate)
        identifier = max(candidates, key=len) if candidates else ""
        return display_name, identifier

    def account_refresh(self):
        now = time.time()
        cached = self.state.account_profile()
        if not self.state.account_refresh_due(now):
            return {**cached, "source": "cache", "refresh_skipped": "rate_limited",
                    "next_refresh_after": cached["last_attempt_at"] + 7200}
        return self.refresh_account_profile(now)

    def refresh_account_profile(self, now=None):
        desktop = self.connect()
        previous = {window.id for window in desktop.windows()}
        try:
            return self._refresh_account_profile(now)
        finally:
            try:
                opened = [window for window in desktop.windows() if window.id not in previous]
                for window in opened:
                    if window.title == "图片和视频":
                        desktop.close_window(window)
                if any(window.title == "wechat" and 240 <= window.rect.width <= 450
                       and 180 <= window.rect.height <= 400 for window in opened):
                    desktop.key("Escape")
            except Exception:
                pass

    def _refresh_account_profile(self, now=None):
        now = time.time() if now is None else now
        desktop = self.connect()
        windows = desktop.windows()
        main = desktop.main_window()
        if any(window.id != main.id for window in windows):
            raise AutomationError("UI_BUSY", "Close secondary WeChat windows before refreshing the account profile",
                                  {"windows": [window.title for window in windows if window.id != main.id]}, retryable=True)
        self.state.account_record_attempt(now)
        desktop.begin_input()
        desktop.focus(main)
        if main.rect != desktop.bounds:
            desktop.maximize(main)
            main = desktop.main_window()
        desktop.click(main.rect.x + 33, main.rect.y + 43)

        def profile_card():
            return next((window for window in desktop.windows()
                         if window.title == "wechat" and 240 <= window.rect.width <= 450
                         and 180 <= window.rect.height <= 400), None)

        card = wait_until(profile_card, desktop.wake, self.config.timeout, "account profile card").value
        image = desktop.capture(card.rect)
        lines = self.ocr.lines(image.resize((image.width * 3, image.height * 3)), psm=11)
        ascii_lines = self.ascii_ocr.lines(image.resize((image.width * 3, image.height * 3)), psm=11)
        display_name, identifier = self.extract_account_identity(lines, ascii_lines)
        if not display_name or not identifier:
            desktop.key("Escape")
            raise AutomationError("PROFILE_UNRECOGNIZED", "Could not uniquely recognize account name and WeChat ID",
                                  {"rows": lines, "ascii_rows": ascii_lines})
        desktop.click(card.rect.x + 58, card.rect.y + 55)
        avatar_window = wait_until(lambda: next((window for window in desktop.windows()
            if window.title == "图片和视频"), None), desktop.wake, self.config.timeout,
            "opened account avatar").value
        avatar_rect = Rect(avatar_window.rect.x, avatar_window.rect.y + 44,
                           avatar_window.rect.width, avatar_window.rect.height - 44)
        avatar = desktop.capture(avatar_rect)
        directory = self.state.directory / "account"
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        destination = directory / "avatar.png"
        temporary = directory / f".avatar-{time.time_ns()}.png"
        save_private_image(avatar, temporary)
        temporary.replace(destination)
        destination.chmod(0o600)
        desktop.close_window(avatar_window)
        desktop.key("Escape")
        wait_until(lambda: profile_card() is None, desktop.wake, self.config.timeout,
                   "account profile card to close")
        profile = {"display_name": display_name, "wechat_id": identifier,
                   "avatar_path": str(destination), "source": "opened_avatar"}
        stored = self.state.account_store(profile, now)
        return {**stored, "source": "opened_avatar", "refresh_skipped": False}

    def select_sidebar_tab(self, main, offset_y):
        desktop = self.connect()
        indicator = Rect(main.rect.x + 24, main.rect.y + offset_y - 8, 19, 18)

        def selected():
            pixels = np.asarray(desktop.capture(indicator), dtype=np.int16)
            green = (pixels[:, :, 1] > pixels[:, :, 0] + 35) & (pixels[:, :, 1] > pixels[:, :, 2] + 10)
            return green.sum() > 12

        if not selected():
            desktop.click(main.rect.x + 32, main.rect.y + offset_y)
            wait_until(selected, desktop.wake, self.config.timeout, "sidebar tab")

    def global_search_box(self, main):
        region = Rect(main.rect.x + 32, main.rect.y + 8,
                      min(260, main.rect.width - 32), min(64, main.rect.height - 8))
        image = self.connect().capture(region)
        scale = 4
        rows = self.ocr.lines(image.resize((image.width * scale, image.height * scale)), psm=11)
        labels = [row for row in rows if text_identity(row["text"]) in ("搜索", "Search")]
        if len(labels) == 1:
            left, top, width, height = labels[0]["rect"]
            return Rect(region.x + left // scale, region.y + top // scale,
                        max(1, width // scale), max(1, height // scale))
        return Rect(main.rect.x + 42, main.rect.y + 14, 90, 24)

    def chat_open(self, chat):
        if not isinstance(chat, str) or not chat.strip():
            raise AutomationError("INVALID_PARAMS", "chat must be a nonempty display name")
        if chat in ("公众号", "微信支付"):
            raise AutomationError("EXCLUDED_FEATURE", "This conversation is outside the configured scope")
        main = self.prepare()
        desktop = self.connect()
        self.select_sidebar_tab(main, 100)
        if self.chat_header_matches(self.current_chat(main), chat) and self.composer_visible(main):
            return {"chat": chat, "matched_by": "verified_header", "already_open": True}
        search_label = self.chat_search_label(chat)
        search = self.global_search_box(main)
        results = Rect(main.rect.x + 120, main.rect.y + 94, 150, 58)
        desktop.click(search.x + search.width // 2, search.y + search.height // 2)
        desktop.key("Control_L+a")
        desktop.key("BackSpace")
        desktop.paste(search_label)
        if desktop.selected_text() != search_label:
            desktop.key("Escape")
            raise AutomationError("SEARCH_UNVERIFIED", "Search input differs from requested chat",
                                  {"chat": chat})
        try:
            self.semantic_wait(results, self.search_result_ready, "first search result highlight")
            desktop.key("Return")
            self.semantic_wait(self.header_region(main),
                lambda image: self.chat_header_matches(" ".join(item["text"] for item in
                    self.ocr.lines(image.resize((image.width * 2, image.height * 2)), 7)), chat),
                f"conversation header {chat}")
            wait_until(lambda: self.composer_visible(main), desktop.wake,
                       self.config.timeout, "conversation composer")
        except AutomationError as error:
            desktop.key("Escape")
            raise AutomationError("CHAT_OPEN_UNVERIFIED", "Search Enter did not open the requested chat",
                                  {"chat": chat, "cause": error.as_dict()}) from error
        return {"chat": chat, "matched_by": "search_enter_and_header", "already_open": False}

    @staticmethod
    def search_result_ready(image):
        pixels = np.asarray(image, dtype=np.int16)
        green = ((pixels[:, :, 1] > pixels[:, :, 0] + 25)
                 & (pixels[:, :, 1] > pixels[:, :, 2] + 10)
                 & (pixels[:, :, 1] > 80))
        return int(green.sum()) >= 5

    def visible_chat_row(self, main, chat):
        region = Rect(main.rect.x + 70, main.rect.y + 74, 210, main.rect.height - 80)
        rows = self.ocr.lines(self.connect().capture(region), 11)
        requested = text_identity(chat)
        candidates = []
        for row in rows:
            observed = text_identity(row["text"])
            if requested.isascii():
                matched = bool(re.search(r"(?<![\w])" + re.escape(requested) + r"(?![\w])", observed,
                                         re.IGNORECASE))
            else:
                matched = requested in observed
            if matched:
                candidates.append(row)
        if len(candidates) == 1:
            return {"rect": candidates[0]["rect"], "region": region}
        return None

    def chat_list(self):
        main = self.prepare()
        self.select_sidebar_tab(main, 100)
        region = Rect(main.rect.x + 70, main.rect.y + 74, 210, main.rect.height - 80)
        self.connect().settle(region)
        return {"scope": "visible", "recognition": "ocr", "rows": self.ocr.lines(self.connect().capture(region)),
                "warning": "OCR rows are visual observations, not stable conversation IDs"}

    def contact_list(self):
        main = self.prepare()
        desktop = self.connect()
        region = Rect(main.rect.x + 70, main.rect.y + 74, 210, main.rect.height - 80)
        self.select_sidebar_tab(main, 150)
        desktop.settle(region)
        return {"scope": "visible", "recognition": "ocr", "rows": self.ocr.lines(desktop.capture(region)),
                "warning": "Scroll the address-book panel for additional contacts; rows are not stable IDs"}

    def chat_setting_menu(self, chat):
        desktop = self.connect()
        wait_until(lambda: any(window.title == "微信" for window in desktop.windows()),
                   desktop.wake, self.config.timeout, "main WeChat window")
        self.chat_open(chat)
        main = desktop.main_window()
        region = Rect(main.rect.x + 70, main.rect.y + 74, 210, main.rect.height - 80)
        rows = self.ocr.lines(desktop.capture(region), 11)
        matches = [row for row in rows if text_identity(row["text"]) == text_identity(chat)]
        if len(matches) != 1:
            raise AutomationError("CHAT_ROW_NOT_UNIQUE", "One visible sidebar row must exactly match the chat",
                                  {"matches": len(matches)})
        left, top, width, height = matches[0]["rect"]
        previous = {window.id for window in desktop.windows()}
        desktop.click(region.x + left + width // 2, region.y + top + height // 2, button=3)
        try:
            menu = wait_until(lambda: next((window for window in desktop.windows()
                                   if window.id not in previous and window.rect.width <= 500
                                   and 80 <= window.rect.height <= 900), None),
                              desktop.wake, self.config.timeout, "chat context menu").value
            desktop.settle(menu.rect)
            image = desktop.capture(menu.rect)
            lines = self.ocr.lines(image.resize((image.width * 3, image.height * 3)), 11)
            return menu, lines
        except Exception:
            desktop.key("Escape")
            raise

    def chat_setting(self, chat, enabled, setting):
        desktop = self.connect()
        enable_label = "置顶" if setting == "pin" else "消息免打扰"
        disable_labels = ("取消置顶", "取消置顶聊天") if setting == "pin" else (
            "取消消息免打扰", "关闭消息免打扰", "开启消息提醒", "允许消息通知")

        def state_and_target(lines):
            candidates = []
            for line in lines:
                label = line["text"].replace(" ", "").replace("网顶", "置顶")
                if label in disable_labels:
                    candidates.append((True, line))
                elif label == enable_label:
                    candidates.append((False, line))
            if len(candidates) != 1:
                raise AutomationError("MENU_ACTION_UNAVAILABLE", "Setting menu label is not unique",
                                      {"setting": setting, "observed": [line["text"] for line in lines]})
            return candidates[0]

        menu, lines = self.chat_setting_menu(chat)
        try:
            current, target = state_and_target(lines)
        except AutomationError:
            desktop.key("Escape")
            raise
        if current == enabled:
            desktop.key("Escape")
            return {"chat": chat, "setting": setting, "enabled": enabled, "status": "unchanged"}
        left, top, width, height = target["rect"]
        desktop.click(menu.rect.x + (left + width // 2) // 3,
                      menu.rect.y + (top + height // 2) // 3)
        try:
            wait_until(lambda: all(window.id != menu.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "setting menu to close")
            for attempt in range(3):
                verification_menu, verification = self.chat_setting_menu(chat)
                try:
                    actual, _ = state_and_target(verification)
                    if actual == enabled:
                        return {"chat": chat, "setting": setting, "enabled": enabled, "status": "confirmed"}
                finally:
                    desktop.key("Escape")
                desktop.wake(0.15)
            raise AutomationError("OUTCOME_UNKNOWN", "Setting changed but did not verify",
                                  {"observed": [line["text"] for line in verification], "actual": actual})
        except AutomationError as error:
            if error.code == "OUTCOME_UNKNOWN":
                raise
            raise AutomationError("OUTCOME_UNKNOWN", "Setting was clicked but not verified",
                                  {"cause": error.as_dict()}) from error

    def chat_pin(self, chat, enabled):
        return self.chat_setting(chat, enabled, "pin")

    def chat_mute(self, chat, enabled):
        return self.chat_setting(chat, enabled, "mute")

    def favorite_list(self, category="全部收藏"):
        categories = {"全部收藏": 145, "链接": 185, "图片与视频": 225, "文件": 265,
                      "音乐与音频": 305, "聊天记录": 345, "语音": 385}
        if category not in categories:
            raise AutomationError("INVALID_PARAMS", "Unsupported favorites category",
                                  {"categories": list(categories)})
        main = self.prepare()
        desktop = self.connect()
        self.select_sidebar_tab(main, 200)
        desktop.click(main.rect.x + 150, main.rect.y + categories[category])
        title_region = Rect(main.rect.x + 290, main.rect.y + 24, 250, 48)
        self.semantic_wait(title_region,
            lambda image: category.replace(" ", "") in "".join(
                line["text"] for line in self.ocr.lines(image.resize((750, 144)), 7)).replace(" ", ""),
            "favorites category heading")
        content = Rect(main.rect.x + 310, main.rect.y + 90,
                       min(1350, main.rect.width - 350), main.rect.height - 115)
        desktop.settle(content)
        reader = self.content_reader()
        return {"category": category, "scope": "visible", "recognition": reader.profile,
                "rows": reader.lines(desktop.capture(content), 11,
                                     scale=self.content_scale(reader, 2)),
                "region": content.as_list()}

    def favorite_search(self, query):
        if not query.strip():
            raise AutomationError("INVALID_PARAMS", "query must not be blank")
        self.favorite_list()
        main = self.connect().main_window()
        desktop = self.connect()
        field = Rect(main.rect.x + 80, main.rect.y + 28, 190, 32)
        desktop.click(field.x + 60, field.y + 15)
        desktop.key("Control_L+a")
        desktop.paste(query)
        if desktop.selected_text() != query:
            raise AutomationError("SEARCH_UNVERIFIED", "Favorites search box differs from requested query")
        desktop.key("End")
        heading = Rect(main.rect.x + 295, main.rect.y + 18, 440, 54)
        self.semantic_wait(heading, lambda image: "搜索结果" in "".join(
            line["text"] for line in self.ocr.lines(image.resize((880, 108)), 7)).replace(" ", ""),
            "favorites search result heading")
        content = Rect(main.rect.x + 80, main.rect.y + 65, min(1050, main.rect.width - 100),
                       min(1300, main.rect.height - 100))
        desktop.settle(content)
        reader = self.content_reader()
        return {"query": query, "scope": "visible", "recognition": reader.profile,
                "rows": reader.lines(desktop.capture(content), 11,
                                     scale=self.content_scale(reader, 2)),
                "region": content.as_list()}

    def favorite_add(self, chat, text):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        target = self.find_message(main, text)
        if target is None:
            raise AutomationError("MESSAGE_NOT_FOUND", "One exact outgoing text bubble must be visible")
        previous = {window.id for window in desktop.windows()}
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        self.choose_menu(["收藏"], previous)
        try:
            searched = self.favorite_search(text)
            observed = "".join(row["text"] for row in searched["rows"]).replace(" ", "")
            if text.replace(" ", "") not in observed:
                raise AutomationError("FAVORITE_UNVERIFIED", "The expected text is absent from visible favorites")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Favorite was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "confirmed", "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def favorite_delete(self, text):
        searched = self.favorite_search(text)
        desktop = self.connect()
        region = Rect(*searched["region"])
        matches = [row for row in searched["rows"]
                   if text_identity(row["text"]) == text_identity(text)]
        if len(matches) != 1:
            raise AutomationError("FAVORITE_NOT_UNIQUE", "One exact visible favorite must match",
                                  {"matches": len(matches)})
        left, top, width, height = matches[0]["rect"]
        previous = {window.id for window in desktop.windows()}
        desktop.click(region.x + left + width // 2, region.y + top + height // 2, button=3)
        self.choose_menu(["删除"], previous)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
                            if window.id not in previous and window.title == "wechat"
                            and 240 <= window.rect.width <= 450
                            and 110 <= window.rect.height <= 280), None),
                            desktop.wake, self.config.timeout, "favorite delete confirmation").value
        desktop.settle(dialog.rect)
        dialog_lines = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 3, dialog.rect.height * 3)), 11)
        observed = "".join(line["text"] for line in dialog_lines).replace(" ", "")
        if "删除" not in observed or "收藏" not in observed:
            desktop.key("Escape")
            raise AutomationError("DELETE_DIALOG_UNVERIFIED", "Unexpected favorite deletion dialog",
                                  {"observed": observed})
        self.click_dialog_action(dialog, dialog_lines, ["删除", "确定"])
        try:
            wait_until(lambda: all(window.id != dialog.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "favorite delete dialog to close")
            refreshed = self.favorite_search(text)
            remaining = [row for row in refreshed["rows"]
                         if text_identity(row["text"]) == text_identity(text)]
            if remaining:
                raise AutomationError("FAVORITE_STILL_VISIBLE", "Deleted favorite is still visible")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Favorite deletion was submitted but not verified",
                                  {"cause": error.as_dict()}) from error
        return {"status": "deleted", "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "favorite_absent": True}

    def group_panel(self, chat):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        observed = self.current_chat(main)
        if not re.search(r"[（(]\d+[)）]$", observed):
            raise AutomationError("NOT_A_GROUP", "Visible chat header has no group member count",
                                  {"header": observed})
        if not self.panel_open(main):
            desktop.click(main.rect.x + main.rect.width - 40, main.rect.y + 50)
            wait_until(lambda: self.panel_open(main), desktop.wake,
                       self.config.timeout, "group details panel")
        return main

    def group_members(self, chat):
        main = self.group_panel(chat)
        desktop = self.connect()
        member_count = int(re.search(r"[（(](\d+)[)）]$", self.current_chat(main)).group(1))
        expansion = Rect(main.rect.x + main.rect.width - 240, main.rect.y + 445, 230, 125)

        def expanded():
            pixels = np.asarray(desktop.capture(expansion), dtype=np.int16)
            return int(((pixels.max(axis=2) - pixels.min(axis=2)) > 40).sum()) > 300

        if member_count > 15 and not expanded():
            desktop.click(main.rect.x + main.rect.width - 125, main.rect.y + 402)
            wait_until(expanded, desktop.wake, self.config.timeout, "expanded group members")
        region = Rect(main.rect.x + main.rect.width - 255, main.rect.y + 130,
                      245, min(1500, main.rect.height - 200))
        desktop.settle(region)
        grid = desktop.capture(region)
        visible_rows = min((member_count + 3) // 4, (region.height - 50) // 66)
        names = Image.new("RGB", (region.width, visible_rows * 32), "white")
        for row in range(visible_rows):
            top = 47 + 66 * row
            names.paste(grid.crop((0, top, region.width, top + 22)), (0, row * 32))
        enlarged = names.resize((names.width * 2, names.height * 2))
        cells = {}
        for word in self.ocr.words(enlarged, 11):
            left, top, _, _ = word["rect"]
            key = (min(visible_rows - 1, top // 64), min(3, left // (region.width // 2)))
            cells.setdefault(key, []).append(word["text"])
        members = [{"row": row, "column": column, "display_name": " ".join(words)}
                   for (row, column), words in sorted(cells.items()) if "".join(words).strip("·. ,")]
        return {"chat": chat, "scope": "visible", "recognition": "ocr",
                "members": members, "visible_count": len(members),
                "region": region.as_list(),
                "warning": "Truncated display names are visual observations, not stable member IDs"}

    def group_announcement(self, chat):
        main = self.group_panel(chat)
        region, rows = self.group_detail_rows(main)
        labels = [line for line in rows if line["text"].replace(" ", "") == "群公告"]
        if len(labels) != 1:
            raise AutomationError("ANNOUNCEMENT_NOT_VISIBLE", "Group announcement could not be visually confirmed")
        _, top, _, height = labels[0]["rect"]
        center = top + height // 2
        nearby = [line for line in rows if abs((line["rect"][1] + line["rect"][3] // 2) - center) <= 260]
        return {"chat": chat, "scope": "visible_summary", "recognition": "ocr",
                "rows": nearby, "region": region.as_list()}

    def group_detail_rows(self, main):
        desktop = self.connect()
        region = Rect(main.rect.x + main.rect.width - 270, main.rect.y + 74, 265,
                      main.rect.height - 100)
        rows = self.ocr.lines(desktop.capture(region).resize((region.width * 2, region.height * 2)), 11)
        return region, rows

    def group_rename(self, chat, name):
        if not name.strip() or len(name) > 64 or "\n" in name or "\r" in name:
            raise AutomationError("INVALID_PARAMS", "name must be one line of 1-64 characters")
        main = self.group_panel(chat)
        desktop = self.connect()
        region, rows = self.group_detail_rows(main)
        labels = [line for line in rows if "群聊名称" in line["text"].replace(" ", "")]
        if len(labels) != 1:
            raise AutomationError("GROUP_NAME_UNAVAILABLE", "Group-name field is not uniquely visible")
        _, top, _, height = labels[0]["rect"]
        field = Rect(region.x + 80, region.y + top // 2 - 15, region.width - 95,
                     max(30, height // 2 + 20))
        desktop.click(field.x + 20, field.y + field.height // 2)
        desktop.key("Control_L+a")
        desktop.paste(name)
        if desktop.selected_text() != name:
            desktop.key("Escape")
            raise AutomationError("DRAFT_UNVERIFIED", "Group name editor differs from requested name")
        desktop.key("Return")
        try:
            wait_until(lambda: any(text_identity(line["text"]) == text_identity(name)
                       for line in self.group_detail_rows(main)[1]), desktop.wake,
                       self.config.timeout, "saved group name")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Group name was submitted but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "name": name, "status": "confirmed"}

    def group_announcement_set(self, chat, text):
        if not text.strip() or len(text) > 2000:
            raise AutomationError("INVALID_PARAMS", "text must contain 1-2000 characters")
        main = self.group_panel(chat)
        desktop = self.connect()
        region, rows = self.group_detail_rows(main)
        labels = [line for line in rows if "群公告" in line["text"].replace(" ", "")]
        if len(labels) != 1:
            raise AutomationError("ANNOUNCEMENT_NOT_VISIBLE", "Group announcement entry is not uniquely visible")
        left, top, width, height = labels[0]["rect"]
        desktop.click(region.x + (left + width // 2) // 2, region.y + (top + height // 2) // 2)
        editor = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and item.rect.width >= 350 and item.rect.height >= 250), None),
            desktop.wake, self.config.timeout, "group announcement editor").value
        desktop.settle(editor.rect)
        editor_rows = self.ocr.lines(desktop.capture(editor.rect).resize(
            (editor.rect.width * 2, editor.rect.height * 2)), 11)
        publish = [line for line in editor_rows if line["text"].replace(" ", "") in ("发布", "保存", "完成")]
        if len(publish) != 1:
            desktop.key("Escape")
            raise AutomationError("ANNOUNCEMENT_UNAVAILABLE", "Announcement editor has no unique publish action")
        field = Rect(editor.rect.x + 25, editor.rect.y + 80, editor.rect.width - 50,
                     editor.rect.height - 155)
        desktop.click(field.x + 15, field.y + 15)
        desktop.key("Control_L+a")
        desktop.paste(text)
        if desktop.selected_text() != text:
            desktop.key("Escape")
            raise AutomationError("DRAFT_UNVERIFIED", "Announcement editor differs from requested text")
        left, top, width, height = publish[0]["rect"]
        desktop.click(editor.rect.x + (left + width // 2) // 2,
                      editor.rect.y + (top + height // 2) // 2)
        try:
            wait_until(lambda: not any(item.id == editor.id for item in desktop.windows()), desktop.wake,
                       self.config.timeout, "announcement editor to close")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Announcement publish was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "status": "submitted"}

    def group_contacts_validate(self, contacts, minimum):
        if not isinstance(contacts, list) or not minimum <= len(contacts) <= 200:
            raise AutomationError("INVALID_PARAMS", f"contacts must contain {minimum}-200 unique display names")
        if any(not isinstance(contact, str) or not contact.strip() for contact in contacts):
            raise AutomationError("INVALID_PARAMS", "contacts must be nonempty display names")
        if len(contacts) != len(set(text_identity(contact) for contact in contacts)):
            raise AutomationError("INVALID_PARAMS", "contacts must not contain duplicate display names")

    @staticmethod
    def group_selector_identity(text):
        return re.sub(r"[.。·,，:：;；!?！？]+$", "", text_identity(text))

    @classmethod
    def group_selector_match(cls, observed, contact):
        expected = cls.group_selector_identity(contact)
        candidate = cls.group_selector_identity(observed)
        if candidate == expected:
            return True
        return expected.isascii() and bool(re.search(
            r"(?<![A-Za-z0-9_])" + re.escape(expected) + r"(?![A-Za-z0-9_])", candidate))

    def group_selector_contacts(self, desktop, dialog, contacts):
        search = Rect(dialog.rect.x + 25, dialog.rect.y + 20,
                      dialog.rect.width // 2 - 45, 35)
        choices = Rect(dialog.rect.x + 18, dialog.rect.y + 60,
                       dialog.rect.width // 2 - 30, dialog.rect.height - 125)
        selected = Rect(dialog.rect.x + dialog.rect.width // 2 + 10, dialog.rect.y + 50,
                        dialog.rect.width // 2 - 25, dialog.rect.height - 115)
        for contact in contacts:
            reader = self.ascii_ocr if contact.isascii() else self.ocr
            desktop.click(search.x + 55, search.y + search.height // 2)
            desktop.key("Control_L+a")
            desktop.paste(contact)
            if desktop.selected_text() != contact:
                desktop.key("Escape")
                raise AutomationError("SEARCH_UNVERIFIED", "Group selector search differs from requested contact")

            def matched_choice():
                rows = reader.lines(desktop.capture(choices).resize(
                    (choices.width * 2, choices.height * 2)), 11)
                matches = [line for line in rows
                           if self.group_selector_match(line["text"], contact)]
                return matches if len(matches) == 1 else None

            try:
                matches = wait_until(matched_choice, desktop.wake, self.config.timeout,
                                     f"unique group selector contact {contact}").value
            except AutomationError as error:
                desktop.key("Escape")
                if error.code == "TIMEOUT":
                    raise AutomationError("CONTACT_NOT_FOUND", "No unique visible selector contact matched",
                                          {"contact": contact}) from error
                raise
            _, top, _, height = matches[0]["rect"]
            desktop.click(choices.x + 18, choices.y + (top + height // 2) // 2)

            def selected_contact():
                rows = reader.lines(desktop.capture(selected).resize(
                    (selected.width * 2, selected.height * 2)), 11)
                return sum(self.group_selector_match(line["text"], contact) for line in rows) == 1

            wait_until(selected_contact, desktop.wake, self.config.timeout,
                       f"selected group contact {contact}")

    def group_selector_complete(self, desktop, dialog):
        rows = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 2, dialog.rect.height * 2)), 11)
        complete = [line for line in rows if line["text"].replace(" ", "") == "完成"]
        if len(complete) == 1:
            left, top, width, height = complete[0]["rect"]
            desktop.click(dialog.rect.x + (left + width // 2) // 2,
                          dialog.rect.y + (top + height // 2) // 2)
            return
        image = desktop.capture(dialog.rect)
        pixels = np.asarray(image)
        green = ((pixels[:, :, 0] < 50) & (pixels[:, :, 1] > 140) & (pixels[:, :, 2] < 150))
        bottom = green[max(0, image.height - 100):, image.width // 2:image.width - 20]
        ys, xs = np.where(bottom)
        if len(xs) >= 100:
            left, right = int(xs.min()) + image.width // 2, int(xs.max()) + image.width // 2
            top, lower = int(ys.min()) + max(0, image.height - 100), int(ys.max()) + max(0, image.height - 100)
            if 80 <= right - left + 1 <= 200 and 20 <= lower - top + 1 <= 70:
                desktop.click(dialog.rect.x + (left + right) // 2, dialog.rect.y + (top + lower) // 2)
                return
        desktop.key("Escape")
        raise AutomationError("GROUP_SELECTOR_UNRECOGNIZED", "Group selector has no unique completion action",
                              {"observed": [line["text"] for line in rows]})

    def group_create(self, contacts):
        self.group_contacts_validate(contacts, 2)
        main = self.prepare()
        desktop = self.connect()
        prior = {window.id for window in desktop.windows()}
        desktop.click(main.rect.x + 257, main.rect.y + 46)
        menu = wait_until(lambda: next((window for window in desktop.windows()
            if window.id not in prior and window.title == "wechat" and window.rect.width <= 350
            and window.rect.height <= 300), None), desktop.wake, self.config.timeout,
            "new chat menu").value
        desktop.settle(menu.rect)
        rows = self.ocr.lines(desktop.capture(menu.rect).resize((menu.rect.width * 3, menu.rect.height * 3)), 11)
        actions = [line for line in rows if "发起群聊" in line["text"].replace(" ", "")]
        if len(actions) != 1:
            desktop.key("Escape")
            raise AutomationError("GROUP_CREATE_UNAVAILABLE", "New chat menu has no unique start-group action",
                                  {"observed": [line["text"] for line in rows]})
        left, top, width, height = actions[0]["rect"]
        desktop.click(menu.rect.x + (left + width // 2) // 3,
                      menu.rect.y + (top + height // 2) // 3)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
            if window.id not in prior and window.title == "微信发起群聊"), None), desktop.wake,
            self.config.timeout, "group contact selector").value
        desktop.settle(dialog.rect)
        self.group_selector_contacts(desktop, dialog, contacts)
        self.group_selector_complete(desktop, dialog)
        try:
            wait_until(lambda: not any(window.id == dialog.id for window in desktop.windows()), desktop.wake,
                       self.config.timeout, "group selector to close")
            def group_header():
                header = self.current_chat(main)
                return header if re.search(r"[（(]\d+[)）]$", header) else None
            header = wait_until(group_header, desktop.wake, self.config.timeout,
                                "new group chat header").value
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Group creation was submitted but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"contacts": contacts, "chat": header, "status": "created"}

    def group_member_control(self, main, symbol):
        region, rows = self.group_detail_rows(main)
        label = {"+": "添加", "-": "移出"}[symbol]
        matches = [line for line in rows if line["text"].replace(" ", "") == label]
        if len(matches) != 1:
            raise AutomationError("GROUP_CONTROL_UNAVAILABLE", "Group member control is not uniquely visible",
                                  {"symbol": symbol, "matches": len(matches)})
        left, top, width, height = matches[0]["rect"]
        return (region.x + (left + width // 2) // 2,
                region.y + max(20, top // 2 - 25))

    def group_invite(self, chat, contacts):
        self.group_contacts_validate(contacts, 1)
        main = self.group_panel(chat)
        desktop = self.connect()
        control = self.group_member_control(main, "+")
        prior = {window.id for window in desktop.windows()}
        desktop.click(*control)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
            if window.id not in prior and window.title in ("微信添加群成员", "微信邀请群成员")), None),
            desktop.wake, self.config.timeout, "group invite selector").value
        desktop.settle(dialog.rect)
        self.group_selector_contacts(desktop, dialog, contacts)
        self.group_selector_complete(desktop, dialog)
        try:
            wait_until(lambda: not any(window.id == dialog.id for window in desktop.windows()), desktop.wake,
                       self.config.timeout, "group invite selector to close")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Group invite was submitted but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "contacts": contacts, "status": "submitted"}

    def group_remove(self, chat, member):
        main = self.group_panel(chat)
        desktop = self.connect()
        control = self.group_member_control(main, "-")
        desktop.click(*control)
        region = Rect(main.rect.x + main.rect.width - 255, main.rect.y + 120, 245, 300)

        def target_member():
            rows = self.ocr.lines(desktop.capture(region).resize((region.width * 2, region.height * 2)), 11)
            matches = [line for line in rows if text_identity(line["text"]) == text_identity(member)]
            return matches if len(matches) == 1 else None

        matches = wait_until(target_member, desktop.wake, self.config.timeout,
                             "unique removable group member").value
        left, top, width, height = matches[0]["rect"]
        prior = {window.id for window in desktop.windows()}
        desktop.click(region.x + (left + width // 2) // 2,
                      region.y + (top + height // 2) // 2)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
            if window.id not in prior and window.title == "wechat" and window.rect.width >= 240
            and window.rect.height >= 110), None), desktop.wake, self.config.timeout,
            "group remove confirmation").value
        desktop.settle(dialog.rect)
        rows = self.ocr.lines(desktop.capture(dialog.rect).resize((dialog.rect.width * 3, dialog.rect.height * 3)), 11)
        observed = "".join(line["text"] for line in rows).replace(" ", "")
        if "删除" not in observed or text_identity(member) not in text_identity(observed):
            desktop.key("Escape")
            raise AutomationError("DELETE_DIALOG_UNVERIFIED", "Unexpected group member removal dialog",
                                  {"observed": observed})
        self.click_dialog_action(dialog, rows, ["删除", "确定"])
        try:
            wait_until(lambda: not any(window.id == dialog.id for window in desktop.windows()), desktop.wake,
                       self.config.timeout, "group remove confirmation to close")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Group member removal was submitted but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "member": member, "status": "submitted"}

    def group_leave(self, chat):
        main = self.group_panel(chat)
        desktop = self.connect()
        action_region, lines = self.group_detail_rows(main)
        matches = [line for line in lines if line["text"].replace(" ", "") == "退出群聊"]
        if len(matches) != 1:
            raise AutomationError("LEAVE_UNAVAILABLE", "Exit group action is not uniquely visible",
                                  {"observed": [line["text"] for line in lines]})
        left, top, width, height = matches[0]["rect"]
        previous = {window.id for window in desktop.windows()}
        desktop.click(action_region.x + (left + width // 2) // 2,
                      action_region.y + (top + height // 2) // 2)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
                            if window.id not in previous and window.title == "wechat"
                            and 240 <= window.rect.width <= 400 and 140 <= window.rect.height <= 250), None),
                            desktop.wake, self.config.timeout, "group exit confirmation").value
        desktop.settle(dialog.rect)
        dialog_lines = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 3, dialog.rect.height * 3)), 11)
        observed = "".join(line["text"] for line in dialog_lines)
        expected = text_identity(chat).replace(" ", "")
        normalized = observed.replace(" ", "")
        if "退出群聊" not in normalized or expected not in normalized or "清空聊天记录" not in normalized:
            desktop.key("Escape")
            raise AutomationError("LEAVE_DIALOG_UNVERIFIED", "Group exit prompt differs from requested chat",
                                  {"observed": observed})
        checkbox = Rect(dialog.rect.x + 89, dialog.rect.y + 77, 21, 21)
        pixels = np.asarray(desktop.capture(checkbox), dtype=np.int16)
        green = ((pixels[:, :, 1] > pixels[:, :, 0] + 25)
                 & (pixels[:, :, 1] > pixels[:, :, 2] + 10))
        if green.sum() > 10:
            desktop.key("Escape")
            raise AutomationError("HISTORY_CLEAR_SELECTED", "Refusing to leave with Clear history selected")
        self.click_dialog_action(dialog, dialog_lines, ["退出", "确定退出"])
        try:
            wait_until(lambda: all(window.id != dialog.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "group exit dialog to close")
            wait_until(lambda: not self.chat_header_matches(self.current_chat(main), chat),
                       desktop.wake, self.config.timeout, "group conversation to close")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Group exit was confirmed; inspect account before retrying",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "submitted", "history_clear_selected": False,
                "membership_verified": False}

    def settings_voice_text(self):
        main = self.prepare()
        desktop = self.connect()
        settings = next((window for window in desktop.windows() if window.title == "设置"), None)
        opened = settings is None
        if opened:
            previous = {window.id for window in desktop.windows()}
            desktop.click(main.rect.x + 32, main.rect.y + main.rect.height - 35)
            self.choose_menu(["设置", "设罪"], previous)
            settings = wait_until(lambda: next((window for window in desktop.windows()
                                  if window.title == "设置"), None),
                                  desktop.wake, self.config.timeout, "settings window").value
        try:
            def rows():
                image = desktop.capture(settings.rect)
                return self.ocr.lines(image.resize((settings.rect.width * 2, settings.rect.height * 2)), 11)

            general = [line for line in rows() if "通用" in line["text"].replace(" ", "")]
            if len(general) != 1:
                raise AutomationError("SETTINGS_UNAVAILABLE", "General settings tab is not uniquely visible")
            left, top, width, height = general[0]["rect"]
            desktop.click(settings.rect.x + (left + width // 2) // 2,
                          settings.rect.y + (top + height // 2) // 2)

            def voice_setting():
                matches = [line for line in rows()
                           if "语音消息自动转成文字" in line["text"].replace(" ", "")]
                return matches[0] if len(matches) == 1 else None

            voice = wait_until(voice_setting, desktop.wake, self.config.timeout,
                               "automatic voice text setting").value
            _, top, _, height = voice["rect"]
            toggle_y = settings.rect.y + (top + height // 2) // 2
            toggle = Rect(settings.rect.x + settings.rect.width - 105, toggle_y - 20, 65, 40)

            def enabled():
                pixels = np.asarray(desktop.capture(toggle), dtype=np.int16)
                green = ((pixels[:, :, 1] > pixels[:, :, 0] + 25)
                         & (pixels[:, :, 1] > pixels[:, :, 2] + 15))
                return int(green.sum()) >= 30

            if enabled():
                return {"enabled": True, "status": "unchanged", "recognition": "visual_toggle"}
            desktop.click(toggle.x + toggle.width // 2, toggle.y + toggle.height // 2)
            wait_until(enabled, desktop.wake, self.config.timeout, "automatic voice text toggle")
            return {"enabled": True, "status": "confirmed", "recognition": "visual_toggle"}
        finally:
            if opened:
                desktop.close_window(settings)

    def content_region(self, main):
        return Rect(main.rect.x + 280, main.rect.y + 74, main.rect.width - 280, main.rect.height - 234)

    def clamp_region(self, region):
        screen = self.connect().bounds
        x = max(screen.x, min(region.x, screen.x + screen.width - 1))
        y = max(screen.y, min(region.y, screen.y + screen.height - 1))
        width = min(region.width, screen.x + screen.width - x)
        height = min(region.height, screen.y + screen.height - y)
        return Rect(x, y, max(1, width), max(1, height))

    def visible_messages(self, main):
        region = self.content_region(main)
        image = self.connect().capture(region)
        reader = self.content_reader()
        messages = []
        for bubble in bubble_regions(image):
            left, top, width, height = bubble["rect"]
            bubble_crop = image.crop((left, top, left + width, top + height))
            padding = 18 if reader.profile == "rapidocr" else 0
            crop = image.crop((max(0, left - padding), max(0, top - padding),
                               min(image.width, left + width + padding),
                               min(image.height, top + height + padding)))
            lines = reader.lines(crop, psm=7 if height <= 72 else 6,
                                 scale=self.content_scale(reader, 3))
            text = "\n".join(line["text"] for line in lines)
            messages.append({"direction": bubble["direction"], "text": text,
                             "kind": "visual_bubble", "recognition": reader.profile,
                             "visual_digest": image_digest(bubble_crop),
                             "rect": [region.x + left, region.y + top, width, height]})
        return messages

    def find_message(self, main, text, direction="outgoing"):
        region = self.content_region(main)
        image = self.connect().capture(region)
        matches = []
        for bubble in reversed(bubble_regions(image)):
            if bubble["direction"] != direction:
                continue
            left, top, width, height = bubble["rect"]
            if "\n" not in text and width < max(20, len(text) * 4):
                continue
            crop = image.crop((left, top, left + width, top + height))
            reader = self.ascii_ocr if text.isascii() else self.content_reader()
            recognized = text_identity(" ".join(line["text"] for line in reader.lines(
                crop, 7, scale=self.content_scale(reader, 3))))
            if recognized == text_identity(text):
                matches.append(Rect(region.x + left, region.y + top, width, height))
        if len(matches) > 1:
            raise AutomationError("AMBIGUOUS_MESSAGE", "More than one visible message has this text")
        return matches[0] if matches else None

    def avatar_candidates(self, main, direction):
        region = self.content_region(main)
        image = self.connect().capture(region)
        pixels = np.asarray(image, dtype=np.int16)
        height, width = pixels.shape[:2]
        if direction == "incoming":
            left, right = max(0, 70), min(width, 170)
        else:
            left, right = max(0, width - 110), max(0, width - 10)
        if right - left < 20:
            return []
        stripe = pixels[:, left:right]
        brightness = stripe.mean(axis=2)
        saturation = stripe.max(axis=2) - stripe.min(axis=2)
        mask = (brightness < 225) | (saturation > 28)
        row_mask = mask.sum(axis=1) >= 8
        padded = np.pad(row_mask.astype(np.int8), (1, 1))
        starts = np.flatnonzero(np.diff(padded) == 1)
        ends = np.flatnonzero(np.diff(padded) == -1)
        candidates = []
        for top, bottom in zip(starts, ends):
            if not 16 <= bottom - top <= 90 or top < 8 or bottom > height - 8:
                continue
            active = mask[top:bottom]
            columns = np.flatnonzero(active.sum(axis=0) >= 2)
            if len(columns) < 12:
                continue
            candidates.append(Rect(region.x + left + int(columns[0]), region.y + int(top),
                                   int(columns[-1] - columns[0] + 1), int(bottom - top)))
        return candidates

    def recent_avatar(self, main, target, max_pages=3):
        if target not in ("other", "self"):
            raise AutomationError("INVALID_PARAMS", "target must be other or self")
        desktop = self.connect()
        direction = "incoming" if target == "other" else "outgoing"
        region = self.content_region(main)
        for page in range(max_pages + 1):
            candidates = self.avatar_candidates(main, direction)
            if candidates:
                return max(candidates, key=lambda candidate: candidate.y + candidate.height // 2), page
            if page == max_pages:
                break
            before = desktop.fingerprint(region)
            desktop.click(region.x + region.width // 2, region.y + region.height // 2, button=4)
            desktop.click(region.x + region.width // 2, region.y + region.height // 2, button=4)
            try:
                desktop.changed(region, before, timeout=0.8)
            except AutomationError as error:
                if error.code == "TIMEOUT":
                    break
                raise
            desktop.settle(region)
        raise AutomationError("AVATAR_NOT_VISIBLE", "No matching recent message avatar was found",
                              {"target": target, "pages_checked": max_pages + 1})

    def pat_notice_lines(self, main):
        region = self.content_region(main)
        reader = self.content_reader()
        lines = reader.lines(self.connect().capture(region), psm=11,
                             scale=self.content_scale(reader, 2))
        return region, [line for line in lines if self.is_pat_notice(line["text"])]

    @staticmethod
    def is_pat_notice(text):
        normalized = text_identity(text).lower()
        return ("拍了拍" in normalized or " patted " in f" {normalized} "
                or bool(re.search(r"\btick(?:l|i)ed\b", normalized)))

    @staticmethod
    def is_own_pat_notice(text):
        normalized = text_identity(text).lower()
        return (normalized.startswith("你拍了拍") or normalized.startswith("you patted")
                or bool(re.match(r"^(?:i\s+|[|l]\s*)?tick(?:l|i)ed\b", normalized)))

    def message_pat(self, chat, target="other"):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        avatar, pages_scrolled = self.recent_avatar(main, target)
        region, notices = self.pat_notice_lines(main)
        previous_notice_count = len(notices)
        previous_windows = {window.id for window in desktop.windows()}
        desktop.click(avatar.x + avatar.width // 2, avatar.y + avatar.height // 2, button=3)
        self.choose_menu(["拍一拍", "Pat"], previous_windows)
        try:
            self.semantic_wait(region, lambda image: len([line for line in self.content_reader().lines(
                image, psm=11, scale=self.content_scale(self.content_reader(), 2))
                if self.is_pat_notice(line["text"])]) > previous_notice_count, "Pat notice")
            notice_confirmed = True
        except AutomationError:
            notice_confirmed = False
        return {"chat": chat, "target": target, "status": "submitted", "avatar": avatar.as_list(),
                "pages_scrolled": pages_scrolled, "notice_confirmed": notice_confirmed}

    def message_pat_revoke(self, chat):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        region, notices = self.pat_notice_lines(main)
        own = [line for line in notices if self.is_own_pat_notice(line["text"])]
        if not own:
            raise AutomationError("PAT_NOT_FOUND", "No visible own Pat notice is available to recall")
        notice = max(own, key=lambda line: line["rect"][1] + line["rect"][3] // 2)
        left, top, width, height = notice["rect"]
        point = (region.x + left + width // 2, region.y + top + height // 2)
        desktop.move(*point)
        action_area = self.clamp_region(Rect(region.x + left - 150, region.y + top - 35,
                                             width + 300, height + 70))
        reader = self.content_reader()
        try:
            actions = wait_until(lambda: [line for line in reader.lines(desktop.capture(action_area), psm=11,
                                                                         scale=self.content_scale(reader, 2))
                                                if line["text"].replace(" ", "") in ("撤回", "Recall", "Revoke")] or None,
                                 desktop.wake, self.config.timeout, "Pat recall action").value
        except AutomationError as error:
            if error.code != "TIMEOUT":
                raise
            previous_windows = {window.id for window in desktop.windows()}
            desktop.click(*point, button=3)
            try:
                self.choose_menu(["撤回", "Recall", "Revoke"], previous_windows)
            except AutomationError as menu_error:
                raise AutomationError("PAT_REVOKE_UNAVAILABLE", "Pat recall action is not uniquely visible",
                                      {"hover": error.as_dict(), "menu": menu_error.as_dict()}) from menu_error
            actions = None
        if actions is not None and len(actions) != 1:
            desktop.key("Escape")
            raise AutomationError("PAT_REVOKE_UNAVAILABLE", "Pat recall action is ambiguous",
                                  {"observed": [line["text"] for line in actions]})
        if actions:
            action = actions[0]
            left, top, width, height = action["rect"]
            desktop.click(action_area.x + left + width // 2, action_area.y + top + height // 2)
        before_count = len(notices)
        try:
            self.semantic_wait(region, lambda image: len([line for line in self.content_reader().lines(
                image, psm=11, scale=self.content_scale(self.content_reader(), 2))
                if self.is_pat_notice(line["text"])]) < before_count, "Pat notice to disappear")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Pat recall was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "recalled", "notice_confirmed": True}

    def choose_menu(self, labels, previous_windows):
        desktop = self.connect()

        def candidate():
            return next((window for window in desktop.windows()
                         if window.id not in previous_windows and window.rect.width <= 500
                         and 40 <= window.rect.height <= 900), None)

        menu = wait_until(candidate, desktop.wake, self.config.timeout, "context menu").value
        desktop.settle(menu.rect)
        screenshot = desktop.capture(menu.rect)
        lines = self.ocr.lines(screenshot.resize((screenshot.width * 3, screenshot.height * 3)), psm=11)
        targets = [line for line in lines if line["text"].replace(" ", "").replace("撒回", "撤回") in labels]
        if len(targets) != 1:
            desktop.key("Escape")
            raise AutomationError("MENU_ACTION_UNAVAILABLE", "Requested menu action was not found",
                                  {"labels": labels, "observed": [line["text"] for line in lines]})
        left, top, width, height = targets[0]["rect"]
        desktop.click(menu.rect.x + (left + width // 2) // 3,
                      menu.rect.y + (top + height // 2) // 3)
        wait_until(lambda: not any(window.id == menu.id for window in desktop.windows()),
                   desktop.wake, self.config.timeout, "context menu to close")
        return {"label": targets[0]["text"]}

    def click_dialog_action(self, dialog, lines, labels, scale=3):
        normalized_labels = {label.replace(" ", "") for label in labels}
        targets = [line for line in lines
                   if line["text"].replace(" ", "") in normalized_labels]
        if len(targets) != 1:
            self.connect().key("Escape")
            raise AutomationError("DIALOG_ACTION_UNVERIFIED", "Confirmation button is not uniquely visible",
                                  {"labels": sorted(normalized_labels),
                                   "observed": [line["text"] for line in lines]})
        left, top, width, height = targets[0]["rect"]
        self.connect().click(dialog.rect.x + (left + width // 2) // scale,
                             dialog.rect.y + (top + height // 2) // scale)

    def message_read(self, chat, limit=None):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 30):
            raise AutomationError("INVALID_PARAMS", "limit must be an integer between 1 and 30")
        self.chat_open(chat)
        main = self.connect().main_window()
        desktop = self.connect()
        region = self.content_region(main)
        desktop.settle(region)
        messages = self.visible_messages(main)
        pages = 1
        stop_reason = "visible_only"
        if limit is not None:
            while len(messages) < limit and pages < 10:
                before = desktop.fingerprint(region)
                desktop.click(region.x + region.width // 2, region.y + region.height // 2, button=4)
                desktop.click(region.x + region.width // 2, region.y + region.height // 2, button=4)
                try:
                    desktop.changed(region, before, timeout=0.8)
                except AutomationError as error:
                    if error.code != "TIMEOUT":
                        raise
                    stop_reason = "history_boundary_or_no_response"
                    break
                desktop.settle(region)
                older = self.visible_messages(main)
                merged, added = merge_message_pages(older, messages)
                if not added:
                    stop_reason = "history_boundary_or_no_new_messages"
                    break
                messages = merged
                pages += 1
                stop_reason = "limit_reached" if len(messages) >= limit else "page_budget"
            messages = messages[-limit:]
        previous = self.state.cursor(chat)
        cursor = {"tail_digests": [message["visual_digest"] for message in messages[-5:]],
                  "observed_at": time.time(), "version": 1}
        self.state.cursor(chat, cursor)
        return {"chat": chat, "messages": messages, "count": len(messages), "pages": pages,
                "stop_reason": stop_reason, "cursor": cursor, "previous_cursor": previous,
                "limitations": ["Only detected visual bubbles are returned; OCR is not authoritative",
                                "Sender names, timestamps and attachment types are not yet resolved",
                                "Identical historical pages may be ambiguous without stable message IDs"]}

    def message_search(self, chat, query, limit=30):
        if not query.strip():
            raise AutomationError("INVALID_PARAMS", "query must not be blank")
        result = self.message_read(chat, limit)
        needle = text_identity(query).casefold()
        matches = [message for message in result["messages"]
                   if needle in text_identity(message.get("text", "")).casefold()]
        return {"chat": chat, "query": query, "matches": matches, "count": len(matches),
                "searched_count": result["count"], "requested_limit": limit,
                "pages": result["pages"], "stop_reason": result["stop_reason"],
                "limitations": result["limitations"]}

    def editor_region(self, main):
        return Rect(main.rect.x + 400, main.rect.y + main.rect.height - 95,
                    min(1200, main.rect.width - 550), 45)

    def draft_empty(self, main):
        region = Rect(main.rect.x + 300, main.rect.y + main.rect.height - 115,
                      main.rect.width - 330, 65)
        pixels = np.asarray(self.connect().capture(region))
        return bool((pixels.min(axis=2) >= 220).all())

    def message_send(self, chat, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise AutomationError("INVALID_PARAMS", "text must contain 1–10000 characters")
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        editor = self.editor_region(main)
        desktop.click(editor.x + 10, editor.y + 15)
        try:
            existing = desktop.selected_text(empty_check=lambda: self.draft_empty(main))
        except AutomationError as error:
            raise AutomationError("DRAFT_UNVERIFIED", "Could not verify the existing editor content",
                                  {"cause": error.as_dict()}) from error
        if existing:
            desktop.key("Escape")
            raise AutomationError("DRAFT_PRESENT", "Existing draft will not be sent or overwritten",
                                  {"matches_requested_text": existing == text})
        if not existing:
            desktop.paste(text)
            try:
                copied = desktop.selected_text()
            except AutomationError as error:
                raise AutomationError("DRAFT_UNVERIFIED", "Paste may have populated the editor; inspect before retrying",
                                      {"cause": error.as_dict()}) from error
            if copied != text:
                raise AutomationError("DRAFT_UNVERIFIED", "Pasted editor text differs from the requested message")
        if not self.chat_header_matches(self.current_chat(main), chat):
            raise AutomationError("TARGET_CHANGED", "Conversation changed before submission")
        desktop.click(main.rect.x + main.rect.width - 80, main.rect.y + main.rect.height - 35)
        try:
            self.semantic_wait(editor, lambda image: not editor_has_content(image), "editor to clear")
            wait_until(lambda: self.find_message(main, text), desktop.wake,
                       self.config.timeout, "outgoing message bubble")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Submit was attempted; do not automatically resend",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "submitted", "bubble_confirmed": True, "delivery_confirmed": False,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def message_send_file(self, chat, path):
        requested = Path(path).expanduser()
        if not requested.is_absolute() or not requested.is_file():
            raise AutomationError("FILE_NOT_FOUND", "path must identify an existing local regular file")
        resolved = requested.resolve(strict=True)
        if any(window.title == "打开" for window in self.connect().windows()):
            raise AutomationError("UI_BUSY", "An existing file chooser is still open; close it before sending")
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        if editor_has_content(desktop.capture(self.editor_region(main))):
            raise AutomationError("DRAFT_PRESENT", "Existing draft will not be overwritten")
        if not self.chat_header_matches(self.current_chat(main), chat) or not self.composer_visible(main):
            raise AutomationError("TARGET_CHANGED", "Conversation is not ready for a file")
        content = self.content_region(main)
        recent = Rect(max(content.x, main.rect.x + main.rect.width - 1000),
                      max(content.y, main.rect.y + main.rect.height - 1000),
                      min(content.width - 10, 990), min(content.height, 840))
        preview = Rect(main.rect.x + 300, main.rect.y + main.rect.height - 115,
                       min(1200, main.rect.width - 600), 90)

        def attachment_preview_visible():
            pixels = np.asarray(desktop.capture(preview), dtype=np.int16)
            orange = ((pixels[:, :, 0] > 210) & (pixels[:, :, 0] > pixels[:, :, 1] + 40)
                      & (pixels[:, :, 1] > pixels[:, :, 2] + 35))
            return int(orange.sum()) > 80

        def matching_file_cards():
            text = "".join(line["text"] for line in self.ocr.lines(desktop.capture(recent), 11))
            return text.replace(" ", "").replace("\n", "").count(resolved.name.replace(" ", ""))

        previous_count = matching_file_cards()
        previous_windows = {window.id for window in desktop.windows()}
        desktop.click(main.rect.x + 397, main.rect.y + main.rect.height - 131)
        chooser = wait_until(lambda: next((window for window in desktop.windows()
                             if window.id not in previous_windows and window.title == "打开"), None),
                             desktop.wake, self.config.timeout, "file chooser").value
        filename = Rect(chooser.rect.x + 80, chooser.rect.y + chooser.rect.height - 72,
                        chooser.rect.width - 180, 34)
        try:
            desktop.click(filename.x + 70, filename.y + filename.height // 2)
            desktop.key("Control_L+a")
            desktop.key("BackSpace")
            desktop.paste(str(resolved))
            if desktop.selected_text() != str(resolved):
                raise AutomationError("PATH_UNVERIFIED", "Chooser path differs from requested file")
            desktop.key("End")
        except Exception:
            desktop.key("Escape")
            raise
        desktop.key("Return")
        try:
            wait_until(lambda: all(window.id != chooser.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "file chooser to close")
            wait_until(attachment_preview_visible, desktop.wake,
                       self.config.timeout, "attachment preview")
            if not self.chat_header_matches(self.current_chat(main), chat):
                raise AutomationError("TARGET_CHANGED", "Conversation changed before sending attachment")
            desktop.click(main.rect.x + main.rect.width - 80,
                          main.rect.y + main.rect.height - 35)
            wait_until(lambda: not attachment_preview_visible(), desktop.wake,
                       self.config.timeout, "attachment preview to clear")
            wait_until(lambda: matching_file_cards() > previous_count, desktop.wake,
                       self.config.timeout, "new outgoing file card")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "File selection was submitted; do not retry automatically",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "filename": resolved.name, "size_bytes": resolved.stat().st_size,
                "status": "submitted", "card_confirmed": True, "delivery_confirmed": False}

    def message_download(self, chat, visual_digest, path):
        if not re.fullmatch(r"[0-9a-f]{32}", visual_digest):
            raise AutomationError("INVALID_PARAMS", "visual_digest must be a 32-character lowercase hex digest")
        requested = Path(path).expanduser()
        if not requested.is_absolute() or not requested.parent.is_dir():
            raise AutomationError("INVALID_PATH", "path must be absolute and its parent directory must exist")
        destination = requested.parent.resolve(strict=True) / requested.name
        if destination.exists() or destination.is_symlink():
            raise AutomationError("FILE_EXISTS", "Refusing to overwrite an existing path",
                                  {"path": str(destination)})
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        region = self.content_region(main)
        image = desktop.capture(region)
        matches = []
        for bubble in bubble_regions(image):
            left, top, width, height = bubble["rect"]
            crop = image.crop((left, top, left + width, top + height))
            if image_digest(crop) == visual_digest:
                matches.append(Rect(region.x + left, region.y + top, width, height))
        if len(matches) != 1:
            raise AutomationError("MESSAGE_NOT_FOUND", "Exactly one visible bubble must match visual_digest",
                                  {"matches": len(matches)})
        previous = {window.id for window in desktop.windows()}
        target = matches[0]
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        self.choose_menu(["另存为", "保存为"], previous)
        chooser = wait_until(lambda: next((window for window in desktop.windows()
                             if window.id not in previous and "保存" in window.title), None),
                             desktop.wake, self.config.timeout, "attachment save dialog").value
        filename = Rect(chooser.rect.x + 80, chooser.rect.y + chooser.rect.height - 72,
                        chooser.rect.width - 180, 34)
        try:
            desktop.click(filename.x + 70, filename.y + filename.height // 2)
            desktop.key("Control_L+a")
            desktop.key("BackSpace")
            desktop.paste(str(destination))
            if desktop.selected_text() != str(destination):
                raise AutomationError("PATH_UNVERIFIED", "Save path differs from requested destination")
            desktop.key("End")
            desktop.key("Return")
        except Exception:
            desktop.key("Escape")
            raise
        try:
            wait_until(lambda: all(window.id != chooser.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "save dialog to close")
            wait_until(lambda: destination.is_file(), desktop.wake,
                       max(30, self.config.timeout), "downloaded attachment file")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Save was submitted but the file was not verified",
                                  {"path": str(destination), "cause": error.as_dict()}) from error
        return {"chat": chat, "visual_digest": visual_digest, "path": str(destination),
                "filename": destination.name, "size_bytes": destination.stat().st_size,
                "status": "saved"}

    def message_reply(self, chat, quote_text, text):
        if not text.strip():
            raise AutomationError("INVALID_PARAMS", "text must not be blank")
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        editor = self.editor_region(main)
        quote_region = Rect(main.rect.x + 300, main.rect.y + main.rect.height - 85, 400, 32)
        if editor_has_content(desktop.capture(editor)) or editor_has_content(desktop.capture(quote_region)):
            raise AutomationError("DRAFT_PRESENT", "Existing text or quote will not be overwritten")
        target = self.find_message(main, quote_text)
        if target is None:
            raise AutomationError("MESSAGE_NOT_FOUND", "Exactly one outgoing quote target must be visible")
        previous = {window.id for window in desktop.windows()}
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        self.choose_menu(["引用"], previous)
        try:
            wait_until(lambda: editor_has_content(desktop.capture(quote_region)),
                       desktop.wake, self.config.timeout, "quoted message preview")
            desktop.click(editor.x + 45, editor.y + 18)
            desktop.paste(text)
            draft_line = Rect(editor.x, editor.y + 6, editor.width, 40)
            self.semantic_wait(draft_line, lambda image: text_identity(" ".join(
                line["text"] for line in self.ocr.lines(image, 7))) == text_identity(text),
                "reply draft")
            if not self.chat_header_matches(self.current_chat(main), chat):
                raise AutomationError("TARGET_CHANGED", "Conversation changed before replying")
        except AutomationError as error:
            raise AutomationError("DRAFT_UNVERIFIED", "A quote or draft may remain; inspect before retrying",
                                  {"cause": error.as_dict()}) from error
        desktop.click(main.rect.x + main.rect.width - 80, main.rect.y + main.rect.height - 35)
        try:
            wait_until(lambda: not editor_has_content(desktop.capture(quote_region)),
                       desktop.wake, self.config.timeout, "quote preview to clear")
            wait_until(lambda: self.find_message(main, text), desktop.wake,
                       self.config.timeout, "outgoing quoted reply bubble")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Reply submission attempted; do not retry automatically",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "submitted", "bubble_confirmed": True,
                "delivery_confirmed": False, "quoted_text_sha256": hashlib.sha256(quote_text.encode()).hexdigest(),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def message_forward(self, chat, text, to):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        target = self.find_message(main, text)
        if target is None:
            raise AutomationError("MESSAGE_NOT_FOUND", "One exact visible outgoing message must match")
        previous_count = sum(message["direction"] == "outgoing" and
                             text_identity(message["text"]) == text_identity(text)
                             for message in self.visible_messages(main)) if chat == to else None
        previous_windows = {window.id for window in desktop.windows()}
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        menu = wait_until(lambda: next((window for window in desktop.windows()
                          if window.id not in previous_windows and window.rect.width < 500
                          and 60 < window.rect.height < 900), None),
                          desktop.wake, self.config.timeout, "forward menu").value
        desktop.settle(menu.rect)
        lines = self.ocr.lines(desktop.capture(menu.rect).resize(
            (menu.rect.width * 3, menu.rect.height * 3)), 11)
        options = [line for line in lines if line["text"].replace(" ", "").startswith("转发")]
        if len(options) != 1:
            desktop.key("Escape")
            raise AutomationError("MENU_ACTION_UNAVAILABLE", "Forward item is not uniquely visible")
        left, top, width, height = options[0]["rect"]
        desktop.click(menu.rect.x + (left + width // 2) // 3,
                      menu.rect.y + (top + height // 2) // 3)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
                            if window.title == "微信发送给"), None),
                            desktop.wake, self.config.timeout, "forward recipient dialog").value
        selected = False
        try:
            desktop.settle(dialog.rect)
            rows = Rect(dialog.rect.x + 90, dialog.rect.y + 140,
                        235, min(370, dialog.rect.height - 175))

            def matching_rows():
                image = desktop.capture(rows)
                matches = [line for line in self.ocr.lines(image.resize(
                    (rows.width * 2, rows.height * 2)), 11)
                    if self.chat_header_matches(line["text"], to)]
                return matches

            matches = matching_rows()
            if not matches:
                search = (dialog.rect.x + 95, dialog.rect.y + 34)
                desktop.click(*search)
                desktop.paste(to)
                if desktop.selected_text() != to:
                    raise AutomationError("TARGET_UNVERIFIED", "Recipient search differs from requested name")
                desktop.key("End")
                matches = wait_until(lambda: matching_rows() or None, desktop.wake,
                                     self.config.timeout, "exact forward recipient").value
            if len(matches) != 1:
                raise AutomationError("AMBIGUOUS_TARGET", "Recipient is not uniquely identified",
                                      {"matches": len(matches)})
            left, top, width, height = matches[0]["rect"]
            desktop.click(rows.x + (left + width // 2) // 2,
                          rows.y + (top + height // 2) // 2)
            selected_region = Rect(dialog.rect.x + 410, dialog.rect.y + 55, 220, 55)
            self.semantic_wait(selected_region,
                lambda image: sum(self.chat_header_matches(line["text"], to)
                    for line in self.ocr.lines(image.resize((440, 110)), 7)) == 1,
                "selected forward recipient")
            selected = True
        except Exception:
            desktop.click(dialog.rect.x + dialog.rect.width - 100,
                          dialog.rect.y + dialog.rect.height - 45)
            raise
        if not selected:
            raise AutomationError("TARGET_UNVERIFIED", "No recipient was selected")
        desktop.click(dialog.rect.x + 445, dialog.rect.y + dialog.rect.height - 45)
        try:
            wait_until(lambda: all(window.id != dialog.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "forward dialog to close")
            if chat == to:
                wait_until(lambda: sum(message["direction"] == "outgoing" and
                    text_identity(message["text"]) == text_identity(text)
                    for message in self.visible_messages(desktop.main_window())) > previous_count,
                    desktop.wake, self.config.timeout, "forwarded outgoing bubble")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Forward was clicked; inspect before retrying",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "to": to, "status": "submitted",
                "bubble_confirmed": chat == to, "delivery_confirmed": False,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def message_revoke(self, chat, text):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        target = self.find_message(main, text)
        if target is None:
            raise AutomationError("MESSAGE_NOT_FOUND", "Exact outgoing message is not visible")
        content = self.content_region(main)
        notice = self.clamp_region(Rect(content.x + content.width // 2 - 400,
                                       max(content.y, target.y - 180), 800, 340))
        def notice_count(image):
            enlarged = image.resize((image.width * 2, image.height * 2))
            text_now = "".join(line["text"] for line in self.ocr.lines(enlarged, 11)).replace(" ", "")
            return len(re.findall(r"[\u4e00-\u9fff]回了一条消息|recalledamessage", text_now, re.I))

        previous_count = notice_count(desktop.capture(notice))
        previous_windows = {window.id for window in desktop.windows()}
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        self.choose_menu(["撤回", "Recall"], previous_windows)

        def recalled(image):
            if self.find_message(main, text) is not None:
                return False
            return notice_count(image) > previous_count

        try:
            self.semantic_wait(notice, recalled, "new recall notice")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Recall was attempted; inspect before retrying",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "recalled", "notice_confirmed": True,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def message_delete(self, chat, text):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        target = self.find_message(main, text)
        if target is None:
            raise AutomationError("MESSAGE_NOT_FOUND", "One exact outgoing text bubble must be visible")
        before = image_digest(desktop.capture(target))
        previous_windows = {window.id for window in desktop.windows()}
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        self.choose_menu(["删除", "刷除"], previous_windows)
        dialog = wait_until(lambda: next((window for window in desktop.windows()
                            if window.id not in previous_windows and window.title == "wechat"
                            and 240 <= window.rect.width <= 420 and 110 <= window.rect.height <= 260), None),
                            desktop.wake, self.config.timeout, "message delete confirmation dialog").value
        desktop.settle(dialog.rect)
        dialog_lines = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 3, dialog.rect.height * 3)), 11)
        dialog_text = "".join(line["text"] for line in dialog_lines)
        if "删除该条消息" not in dialog_text.replace(" ", ""):
            desktop.key("Escape")
            raise AutomationError("DELETE_DIALOG_UNVERIFIED", "Unexpected confirmation dialog",
                                  {"observed": dialog_text})
        self.click_dialog_action(dialog, dialog_lines, ["删除", "确定"])
        try:
            wait_until(lambda: all(window.id != dialog.id for window in desktop.windows()),
                       desktop.wake, self.config.timeout, "message delete dialog to close")
            wait_until(lambda: image_digest(desktop.capture(target)) != before
                       and self.find_message(main, text) is None, desktop.wake,
                       self.config.timeout, "deleted message bubble to disappear")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Delete was clicked; inspect before retrying",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "deleted", "bubble_absent": True,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}

    def message_voice_text(self, chat, visual_digest):
        if not re.fullmatch(r"[0-9a-f]{32}", visual_digest):
            raise AutomationError("INVALID_PARAMS", "visual_digest must be a 32-character lowercase hex digest")
        self.chat_open(chat)
        desktop = self.connect()
        region = self.content_region(desktop.main_window())

        def find_voice_and_text():
            image = desktop.capture(region)
            bubbles = bubble_regions(image)
            matches = [bubble for bubble in bubbles if image_digest(image.crop((
                bubble["rect"][0], bubble["rect"][1],
                bubble["rect"][0] + bubble["rect"][2],
                bubble["rect"][1] + bubble["rect"][3]))) == visual_digest]
            if len(matches) != 1:
                raise AutomationError("MESSAGE_NOT_FOUND", "Exactly one visible bubble must match visual_digest",
                                      {"matches": len(matches)})
            transcript = adjacent_bubble(bubbles, matches[0])
            return image, matches[0], transcript

        def recognized_text(image, bubble):
            left, top, width, height = bubble["rect"]
            crop = image.crop((left, top, left + width, top + height))
            return "\n".join(line["text"] for line in self.ocr.lines(crop, 6)).strip()

        image, voice, transcript = find_voice_and_text()
        voice_label = text_identity(recognized_text(image, voice))
        if transcript is not None:
            text = recognized_text(image, transcript)
            if text:
                return {"chat": chat, "visual_digest": visual_digest,
                        "status": "already_visible", "text": text, "recognition": "ocr"}
        left, top, width, height = voice["rect"]
        previous = {window.id for window in desktop.windows()}
        desktop.click(region.x + left + width // 2, region.y + top + height // 2, button=3)
        self.choose_menu(["语音转文字"], previous)

        def transcription_ready():
            current = desktop.capture(region)
            bubbles = bubble_regions(current)
            candidates = []
            for candidate in bubbles:
                candidate_left, _, candidate_width, _ = candidate["rect"]
                if (candidate["direction"] != voice["direction"]
                        or abs(candidate_left - left) > 3 or abs(candidate_width - width) > 3):
                    continue
                adjacent = adjacent_bubble(bubbles, candidate)
                if adjacent is not None and text_identity(recognized_text(current, candidate)) == voice_label:
                    text = recognized_text(current, adjacent)
                    if text:
                        candidates.append(text)
            return candidates[0] if len(candidates) == 1 else None

        try:
            text = wait_until(transcription_ready, desktop.wake, self.config.timeout,
                              "voice transcription").value
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Conversion was clicked but not verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "visual_digest": visual_digest,
                "status": "converted", "text": text, "recognition": "ocr"}

    def contact_info(self, chat):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        panel = Rect(main.rect.x + main.rect.width - 270, main.rect.y + 74, 265, 560)
        if not self.panel_open(main):
            desktop.click(main.rect.x + main.rect.width - 32, main.rect.y + 50)
            wait_until(lambda: self.panel_open(main), desktop.wake,
                       self.config.timeout, "contact panel to open")
        avatar = Rect(panel.x + 18, panel.y + 10, 50, 50)

        def avatar_ready():
            pixels = np.asarray(desktop.capture(avatar), dtype=np.int16)
            return int(((pixels.max(axis=2) - pixels.min(axis=2)) > 30).sum()) > 80

        wait_until(avatar_ready, desktop.wake, self.config.timeout, "contact avatar")

        def profile_window():
            return next((window for window in desktop.windows()
                         if window.title == "wechat" and window.rect.x < panel.x
                         and 250 <= window.rect.width <= 450 and window.rect.height >= 400), None)

        def profile_visible(window):
            probe = Rect(window.rect.x + 15, window.rect.y + 180, 6, 6)
            return min(desktop.capture(probe).getpixel((2, 2))) >= 248

        profile = profile_window()
        if profile is not None:
            desktop.settle(profile.rect, quiet_ms=80)
            if profile_window() is None or not profile_visible(profile):
                profile = None
        for attempt in range(3):
            if profile is not None:
                break
            desktop.click(panel.x + 40, panel.y + 30)
            try:
                profile = wait_until(lambda: (window if profile_visible(window) else None)
                                     if (window := profile_window()) else None,
                                     desktop.wake, 1 if attempt < 2 else self.config.timeout,
                                     "contact profile window").value
            except AutomationError as error:
                if error.code != "TIMEOUT" or attempt == 2:
                    raise
        desktop.settle(profile.rect)
        reader = self.content_reader()
        return {"chat": chat, "recognition": reader.profile,
                "fields": reader.lines(desktop.capture(profile.rect),
                                       scale=self.content_scale(reader, 2)),
                "profile_region": profile.rect.as_list()}

    def contact_remark(self, chat, remark):
        if len(remark) > 64 or "\n" in remark or "\r" in remark:
            raise AutomationError("INVALID_PARAMS", "remark must fit one line of at most 64 characters")
        profile = self.contact_info(chat)
        desktop = self.connect()
        card = Rect(*profile["profile_region"])
        image = desktop.capture(card)
        lines = self.ocr.lines(image.resize((card.width * 2, card.height * 2)), 11)
        labels = [line for line in lines if line["text"].replace(" ", "") == "备注"]
        if len(labels) != 1:
            raise AutomationError("REMARK_UNAVAILABLE", "Profile remark label is not uniquely visible")
        top = card.y + labels[0]["rect"][1] // 2
        field = Rect(card.x + 100, top - 12, card.width - 115, 33)
        existing = "".join(line["text"] for line in self.ocr.lines(
            desktop.capture(field).resize((field.width * 3, field.height * 3)), 7)).replace(" ", "")
        desktop.click(field.x + 40, field.y + field.height // 2)
        old_remark = "" if "添加备注名" in existing else desktop.selected_text()
        if old_remark == remark:
            desktop.key("Escape")
            return {"chat": chat, "remark": remark, "status": "unchanged"}
        desktop.key("Control_L+a")
        desktop.key("BackSpace")
        if remark:
            desktop.paste(remark)
            if desktop.selected_text() != remark:
                raise AutomationError("DRAFT_UNVERIFIED", "Remark editor differs from requested text")
        desktop.key("Return")
        try:
            def saved_remark():
                window = next((item for item in desktop.windows()
                               if item.title == "wechat" and item.rect.x == card.x
                               and 250 <= item.rect.width <= 450 and item.rect.height >= 400), None)
                if window is None:
                    return False
                enlarged = desktop.capture(window.rect).resize(
                    (window.rect.width * 2, window.rect.height * 2))
                rows = self.ocr.lines(enlarged, 11)
                labels_now = [line for line in rows if line["text"].replace(" ", "") == "备注"]
                if len(labels_now) != 1:
                    return False
                row_y = labels_now[0]["rect"][1]
                observed = "".join(line["text"] for line in rows
                                   if abs(line["rect"][1] - row_y) < 12
                                   and line["rect"][0] > 175).replace(" ", "")
                return (observed == remark.replace(" ", "") if remark
                        else "添加备注名" in observed)

            wait_until(saved_remark, desktop.wake, self.config.timeout, "saved contact remark")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Remark was submitted but verification failed",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "remark": remark, "previous_remark": old_remark,
                "status": "confirmed"}

    def contact_add(self, identifier, message=""):
        if not identifier.strip() or len(identifier) > 200 or "\n" in identifier:
            raise AutomationError("INVALID_PARAMS", "identifier must be a one-line value of at most 200 characters")
        if len(message) > 200 or "\n" in message or "\r" in message:
            raise AutomationError("INVALID_PARAMS", "message must be one line of at most 200 characters")
        main = self.prepare()
        desktop = self.connect()
        search = Rect(main.rect.x + 80, main.rect.y + 28, 155, 32)
        desktop.click(search.x + 55, search.y + 15)
        desktop.key("Control_L+a")
        desktop.paste(identifier)
        if desktop.selected_text() != identifier:
            desktop.key("Escape")
            raise AutomationError("SEARCH_UNVERIFIED", "Global search differs from requested identifier")
        results = Rect(main.rect.x + 70, main.rect.y + 72, 310, min(820, main.rect.height - 250))

        def result_rows():
            image = desktop.capture(results)
            rows = self.ocr.lines(image.resize((image.width * 2, image.height * 2)), 11)
            matches = [line for line in rows if text_identity(line["text"]) == text_identity(identifier)]
            return matches if len(matches) == 1 else None

        try:
            rows = wait_until(result_rows, desktop.wake, self.config.timeout,
                              "unique contact search result").value
        except AutomationError as error:
            desktop.key("Escape")
            if error.code == "TIMEOUT":
                raise AutomationError("CONTACT_NOT_FOUND", "No unique visible person matched identifier") from error
            raise
        left, top, width, height = rows[0]["rect"]
        desktop.click(results.x + (left + width // 2) // 2, results.y + (top + height // 2) // 2)
        profile = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and 250 <= item.rect.width <= 500 and item.rect.height >= 300), None),
            desktop.wake, self.config.timeout, "searched profile").value
        desktop.settle(profile.rect)
        rows = self.ocr.lines(desktop.capture(profile.rect).resize(
            (profile.rect.width * 2, profile.rect.height * 2)), 11)
        added = [line for line in rows if "添加到通讯录" in line["text"].replace(" ", "")]
        if not added:
            desktop.key("Escape")
            if any("发消息" in line["text"].replace(" ", "") for line in rows):
                return {"identifier": identifier, "status": "already_contact"}
            raise AutomationError("ADD_CONTACT_UNAVAILABLE", "Profile does not expose one add-contact action",
                                  {"observed": [line["text"] for line in rows]})
        if len(added) != 1:
            desktop.key("Escape")
            raise AutomationError("ADD_CONTACT_UNAVAILABLE", "Profile add-contact action is ambiguous")
        left, top, width, height = added[0]["rect"]
        desktop.click(profile.rect.x + (left + width // 2) // 2,
                      profile.rect.y + (top + height // 2) // 2)
        request = wait_until(lambda: next((item for item in desktop.windows()
            if item.id != profile.id and item.title == "wechat" and item.rect.width >= 300
            and item.rect.height >= 180), None), desktop.wake, self.config.timeout,
            "friend request editor").value
        desktop.settle(request.rect)
        request_rows = self.ocr.lines(desktop.capture(request.rect).resize(
            (request.rect.width * 2, request.rect.height * 2)), 11)
        submit = [line for line in request_rows if line["text"].replace(" ", "") in ("发送", "发送请求")]
        if len(submit) != 1:
            desktop.key("Escape")
            raise AutomationError("ADD_CONTACT_UNAVAILABLE", "Friend request editor has no unique send action")
        if message:
            editor = Rect(request.rect.x + 35, request.rect.y + 80,
                          request.rect.width - 70, max(30, request.rect.height - 155))
            desktop.click(editor.x + 15, editor.y + 15)
            desktop.key("Control_L+a")
            desktop.paste(message)
            if desktop.selected_text() != message:
                desktop.key("Escape")
                raise AutomationError("DRAFT_UNVERIFIED", "Friend request message differs from requested text")
        left, top, width, height = submit[0]["rect"]
        desktop.click(request.rect.x + (left + width // 2) // 2,
                      request.rect.y + (top + height // 2) // 2)
        try:
            wait_until(lambda: not any(item.id == request.id for item in desktop.windows()), desktop.wake,
                       self.config.timeout, "friend request editor to close")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Friend request was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"identifier": identifier, "message": message, "status": "submitted"}

    def contact_accept(self, requester):
        main = self.prepare()
        desktop = self.connect()
        self.select_sidebar_tab(main, 150)
        sidebar = Rect(main.rect.x + 70, main.rect.y + 72, 210, min(700, main.rect.height - 100))
        rows = self.ocr.lines(desktop.capture(sidebar).resize((sidebar.width * 2, sidebar.height * 2)), 11)
        new_friends = [line for line in rows if "新的朋友" in line["text"].replace(" ", "")]
        if len(new_friends) != 1:
            raise AutomationError("FRIEND_REQUEST_UNAVAILABLE", "New Friends entry is not uniquely visible")
        left, top, width, height = new_friends[0]["rect"]
        desktop.click(sidebar.x + (left + width // 2) // 2, sidebar.y + (top + height // 2) // 2)

        def requester_row():
            observed = self.ocr.lines(desktop.capture(sidebar).resize((sidebar.width * 2, sidebar.height * 2)), 11)
            matches = [line for line in observed if text_identity(line["text"]) == text_identity(requester)]
            return matches if len(matches) == 1 else None

        try:
            matches = wait_until(requester_row, desktop.wake, self.config.timeout,
                                 "unique new-friend request").value
        except AutomationError as error:
            if error.code == "TIMEOUT":
                raise AutomationError("FRIEND_REQUEST_NOT_FOUND", "No unique visible friend request matched requester") from error
            raise
        left, top, width, height = matches[0]["rect"]
        desktop.click(sidebar.x + (left + width // 2) // 2, sidebar.y + (top + height // 2) // 2)
        detail = Rect(main.rect.x + 300, main.rect.y + 25, main.rect.width - 600,
                      min(700, main.rect.height - 50))

        def accept_action():
            rows = self.ocr.lines(desktop.capture(detail).resize((detail.width * 2, detail.height * 2)), 11)
            matches = [line for line in rows if line["text"].replace(" ", "") in ("接受", "通过")]
            return matches if len(matches) == 1 else None

        actions = wait_until(accept_action, desktop.wake, self.config.timeout,
                             "unique accept-friend action").value
        left, top, width, height = actions[0]["rect"]
        desktop.click(detail.x + (left + width // 2) // 2, detail.y + (top + height // 2) // 2)
        try:
            wait_until(lambda: not accept_action(), desktop.wake, self.config.timeout,
                       "friend request acceptance")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Accept was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"requester": requester, "status": "accepted"}

    def contact_delete(self, chat):
        profile = self.contact_info(chat)
        desktop = self.connect()
        card = Rect(*profile["profile_region"])
        menu_button = (card.x + card.width - 20, card.y + 20)
        previous = {item.id for item in desktop.windows()}
        desktop.click(*menu_button)
        menu = wait_until(lambda: next((item for item in desktop.windows()
            if item.id not in previous and item.title == "wechat" and item.rect.width <= 350
            and item.rect.height <= 400), None), desktop.wake, self.config.timeout,
            "contact action menu").value
        desktop.settle(menu.rect)
        rows = self.ocr.lines(desktop.capture(menu.rect).resize((menu.rect.width * 3, menu.rect.height * 3)), 11)
        actions = [line for line in rows if "删除" in line["text"].replace(" ", "")]
        if len(actions) != 1:
            desktop.key("Escape")
            raise AutomationError("DELETE_UNAVAILABLE", "Contact menu does not expose one delete action",
                                  {"observed": [line["text"] for line in rows]})
        left, top, width, height = actions[0]["rect"]
        desktop.click(menu.rect.x + (left + width // 2) // 3,
                      menu.rect.y + (top + height // 2) // 3)
        dialog = wait_until(lambda: next((item for item in desktop.windows()
            if item.id not in previous and item.id != menu.id and item.title == "wechat"
            and item.rect.width >= 240 and item.rect.height >= 110), None), desktop.wake,
            self.config.timeout, "contact delete confirmation").value
        desktop.settle(dialog.rect)
        rows = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 3, dialog.rect.height * 3)), 11)
        observed = "".join(line["text"] for line in rows).replace(" ", "")
        if "删除" not in observed or text_identity(chat) not in text_identity(observed):
            desktop.key("Escape")
            raise AutomationError("DELETE_DIALOG_UNVERIFIED", "Unexpected contact deletion dialog",
                                  {"observed": observed})
        self.click_dialog_action(dialog, rows, ["删除", "确定"])
        try:
            wait_until(lambda: not any(item.id == dialog.id for item in desktop.windows()), desktop.wake,
                       self.config.timeout, "contact delete dialog to close")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Contact delete was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "status": "submitted"}

    def transfer_accept(self, chat, text):
        self.chat_open(chat)
        desktop = self.connect()
        main = desktop.main_window()
        target = self.find_message(main, text, direction="incoming")
        if target is None:
            raise AutomationError("TRANSFER_NOT_FOUND", "One exact visible incoming transfer bubble must match")
        before = image_digest(desktop.capture(target))
        desktop.click(target.x + target.width // 2, target.y + target.height // 2)
        dialog = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and item.rect.width >= 280 and item.rect.height >= 260), None),
            desktop.wake, self.config.timeout, "transfer detail").value
        desktop.settle(dialog.rect)
        rows = self.ocr.lines(desktop.capture(dialog.rect).resize(
            (dialog.rect.width * 2, dialog.rect.height * 2)), 11)
        received = any("已收款" in line["text"].replace(" ", "") for line in rows)
        actions = [line for line in rows if line["text"].replace(" ", "") in ("确认收款", "收款")]
        if received:
            desktop.key("Escape")
            return {"chat": chat, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "status": "already_received"}
        if len(actions) != 1:
            desktop.key("Escape")
            raise AutomationError("TRANSFER_UNAVAILABLE", "Transfer detail does not expose one receive action",
                                  {"observed": [line["text"] for line in rows]})
        left, top, width, height = actions[0]["rect"]
        desktop.click(dialog.rect.x + (left + width // 2) // 2,
                      dialog.rect.y + (top + height // 2) // 2)
        try:
            wait_until(lambda: image_digest(desktop.capture(target)) != before, desktop.wake,
                       self.config.timeout, "received transfer bubble update")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Receive was clicked but the transfer bubble did not verify",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "status": "received"}

    def moments_open(self, chat):
        profile = self.contact_info(chat)
        desktop = self.connect()
        card = Rect(*profile["profile_region"])
        thumbnail = Rect(card.x + 110, card.y + 240, 55, 55)
        pixels = np.asarray(desktop.capture(thumbnail), dtype=np.int16)
        colored = (pixels.max(axis=2) - pixels.min(axis=2) > 20).sum()
        if colored < 50:
            raise AutomationError("FEATURE_UNAVAILABLE", "No recognizable Moments thumbnail in the profile")
        desktop.click(thumbnail.x + thumbnail.width // 2, thumbnail.y + thumbnail.height // 2)
        result = wait_until(lambda: next((item for item in desktop.windows() if item.title == "朋友圈"), None),
                            desktop.wake, self.config.timeout, "Moments window")
        screen = desktop.bounds
        width = min(1800, screen.width)
        window = desktop.resize(result.value, Rect((screen.width - width) // 2, 0, width, screen.height))
        content = Rect(window.rect.x + 110, window.rect.y + 320,
                       window.rect.width - 180, window.rect.height - 390)

        def content_ready():
            pixels = np.asarray(desktop.capture(content), dtype=np.int16)
            visible = (pixels.min(axis=2) < 238) | ((pixels.max(axis=2) - pixels.min(axis=2)) > 18)
            return int(visible.sum()) >= max(350, pixels.shape[0] * pixels.shape[1] // 5000)

        wait_until(content_ready, desktop.wake, max(12, self.config.timeout), "Moments content")
        desktop.settle(content, timeout=3, quiet_ms=80)
        return {"chat": chat, "window": window.as_dict(), "screenshot": self.screenshot(window_id=window.id)}

    def moments_open_self(self, wide=True):
        main = self.prepare()
        desktop = self.connect()
        desktop.click(main.rect.x + 33, main.rect.y + 43)
        card = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and 240 <= item.rect.width <= 350
            and 200 <= item.rect.height <= 350), None), desktop.wake, self.config.timeout,
            "current account card").value
        desktop.click(card.rect.x + 156, card.rect.y + 139)
        moment = wait_until(lambda: next((item for item in desktop.windows() if item.title == "朋友圈"), None),
                            desktop.wake, self.config.timeout, "own Moments window").value
        if wide:
            screen = desktop.bounds
            width = min(1800, screen.width)
            window = desktop.resize(moment, Rect((screen.width - width) // 2, 0, width, screen.height))
        else:
            window = moment
        content = Rect(window.rect.x + 40, window.rect.y + 300, window.rect.width - 80,
                       window.rect.height - 350)
        wait_until(lambda: np.asarray(desktop.capture(content)).min() < 238, desktop.wake,
                   max(12, self.config.timeout), "own Moments content")
        return {"window": window.as_dict(), "scope": "own"}

    def moments_composer_rows(self, desktop, window):
        image = desktop.capture(window.rect)
        return self.ocr.lines(image.resize((image.width * 2, image.height * 2)), psm=11)

    def moments_choose_audience(self, desktop, window, selector_rows, mode, tags, contacts):
        label = "谁可以看" if mode == "visible" else "不给谁看"
        options = [line for line in selector_rows if label in line["text"].replace(" ", "")
                   and line["rect"][0] // 2 < 250]
        if len(options) != 1:
            raise AutomationError("VISIBILITY_UNRECOGNIZED", "Audience option was not uniquely visible",
                                  {"mode": mode})
        left, top, width, height = options[0]["rect"]
        desktop.click(window.rect.x + (left + width // 2) // 2,
                      window.rect.y + (top + height // 2) // 2)
        chooser = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == f"微信{label}"), None), desktop.wake, self.config.timeout,
            f"Moments {label} selector").value

        def rows():
            return self.ocr.lines(desktop.capture(chooser.rect).resize(
                (chooser.rect.width * 2, chooser.rect.height * 2)), psm=11)

        def choose(name, tab):
            current = rows()
            tabs = [line for line in current if text_identity(line["text"]) == tab]
            if len(tabs) != 1:
                raise AutomationError("AUDIENCE_UNRECOGNIZED", "Audience selector tab was not uniquely visible",
                                      {"tab": tab})
            left, top, width, height = tabs[0]["rect"]
            desktop.click(chooser.rect.x + (left + width // 2) // 2,
                          chooser.rect.y + (top + height // 2) // 2)
            if tab == "朋友":
                search = Rect(chooser.rect.x + 25, chooser.rect.y + 54, 295, 35)
                desktop.click(search.x + 70, search.y + search.height // 2)
                desktop.key("Control_L+a")
                desktop.paste(name)
                def result():
                    items = rows()
                    matches = [line for line in items if text_identity(line["text"]) == text_identity(name)]
                    return matches if len(matches) == 1 else None
                matches = wait_until(result, desktop.wake, self.config.timeout,
                                     f"audience friend {name}").value
            else:
                matches = [line for line in rows() if text_identity(line["text"]) == text_identity(name)]
                if len(matches) != 1:
                    raise AutomationError("AUDIENCE_NOT_UNIQUE", "Audience label must match one visible tag",
                                          {"name": name, "matches": len(matches)})
            _, top, _, height = matches[0]["rect"]
            desktop.click(chooser.rect.x + 33, chooser.rect.y + top // 2 + height // 4)

        for tag in tags:
            choose(tag, "标签")
        for contact in contacts:
            choose(contact, "朋友")
        done = [line for line in rows() if text_identity(line["text"]) == "完成"]
        if len(done) != 1:
            raise AutomationError("AUDIENCE_UNRECOGNIZED", "Audience selector completion button was not uniquely visible")
        left, top, width, height = done[0]["rect"]
        desktop.click(chooser.rect.x + (left + width // 2) // 2,
                      chooser.rect.y + (top + height // 2) // 2)
        wait_until(lambda: not any(item.id == chooser.id for item in desktop.windows()), desktop.wake,
                   self.config.timeout, f"Moments {label} selector to close")

    def moments_publish(self, images, text="", visibility="contacts", visible_tags=None,
                        visible_contacts=None, hidden_contacts=None):
        visible_tags = visible_tags or []
        visible_contacts = visible_contacts or []
        hidden_contacts = hidden_contacts or []
        if visible_contacts and hidden_contacts:
            raise AutomationError("INVALID_PARAMS", "visible_contacts and hidden_contacts cannot both be set")
        if visible_tags and hidden_contacts:
            raise AutomationError("INVALID_PARAMS", "visible_tags cannot be combined with hidden_contacts")
        if visibility not in ("private", "contacts", "public"):
            raise AutomationError("INVALID_PARAMS", "visibility must be private, contacts, or public")
        if len(images) != 1:
            raise AutomationError("INVALID_PARAMS", "Exactly one image is currently supported")
        requested = Path(images[0]).expanduser()
        if not requested.is_absolute() or not requested.is_file():
            raise AutomationError("FILE_NOT_FOUND", "images[0] must identify an existing local regular file")
        resolved = requested.resolve(strict=True)
        if resolved.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            raise AutomationError("INVALID_MEDIA", "Moments image must use a supported image extension")
        opened = self.moments_open_self(wide=False)
        desktop = self.connect()
        window = next(item for item in desktop.windows() if item.id == opened["window"]["id"])
        previous_windows = {item.id for item in desktop.windows()}
        desktop.click(window.rect.x + 78, window.rect.y + 22)
        chooser = wait_until(lambda: next((item for item in desktop.windows()
            if item.id not in previous_windows and item.title == "打开"), None), desktop.wake,
            self.config.timeout, "Moments image chooser").value
        filename = Rect(chooser.rect.x + 110, chooser.rect.y + chooser.rect.height - 56,
                        chooser.rect.width - 210, 32)
        try:
            desktop.click(filename.x + 20, filename.y + filename.height // 2)
            desktop.key("Control_L+a")
            desktop.paste(str(resolved))
            desktop.key("Return")
        except Exception:
            desktop.key("Escape")
            raise
        wait_until(lambda: not any(item.id == chooser.id for item in desktop.windows()), desktop.wake,
                   self.config.timeout, "Moments image chooser to close")
        def composer_rows():
            rows = self.moments_composer_rows(desktop, window)
            return rows if any("谁可以看" in line["text"].replace(" ", "") for line in rows) else None
        rows = wait_until(composer_rows, desktop.wake, self.config.timeout, "Moments publish editor").value
        visible_row = next(line for line in rows if "谁可以看" in line["text"].replace(" ", ""))
        prompt = next((line for line in rows if "这一刻" in line["text"].replace(" ", "")), None)
        if text:
            if prompt is not None:
                left, top, _, height = prompt["rect"]
                editor = (window.rect.x + left // 2 + 30,
                          window.rect.y + top // 2 + max(35, height // 2))
            else:
                editor = (window.rect.x + 190,
                          window.rect.y + visible_row["rect"][1] // 2 - 280)
            desktop.click(*editor)
            desktop.paste(text)
            wait_until(lambda: any(text_identity(line["text"]) == text_identity(text)
                       for line in self.ascii_ocr.lines(desktop.capture(window.rect).resize(
                           (window.rect.width * 2, window.rect.height * 2)), psm=11)),
                       desktop.wake, self.config.timeout, "Moments draft text")
        _, top, _, height = visible_row["rect"]
        desktop.click(window.rect.x + 380, window.rect.y + top // 2)
        def visibility_rows():
            observed = self.moments_composer_rows(desktop, window)
            private = [line for line in observed if "私密" in line["text"].replace(" ", "")
                       and line["rect"][0] // 2 < 250]
            public = [line for line in observed if "公开" in line["text"].replace(" ", "")
                      and line["rect"][0] // 2 < 250]
            return observed if len(private) == 1 and len(public) == 1 else None
        selection = wait_until(visibility_rows, desktop.wake, self.config.timeout,
                               "Moments visibility selector").value
        if visible_tags or visible_contacts:
            self.moments_choose_audience(desktop, window, selection, "visible", visible_tags, visible_contacts)
            expected = None
        if hidden_contacts:
            self.moments_choose_audience(desktop, window, selection, "hidden", [], hidden_contacts)
            expected = None
        if not (visible_tags or visible_contacts or hidden_contacts):
            label = {"private": "私密", "contacts": "公开", "public": "公开"}[visibility]
            chosen = [line for line in selection if label in line["text"].replace(" ", "")
                      and line["rect"][0] // 2 < 250]
            if len(chosen) != 1:
                desktop.key("Escape")
                raise AutomationError("VISIBILITY_UNRECOGNIZED", "Requested visibility option was not uniquely visible",
                                      {"visibility": visibility})
            left, top, width, height = chosen[0]["rect"]
            desktop.click(window.rect.x + (left + width // 2) // 2,
                          window.rect.y + (top + height // 2) // 2)
            confirm_y = window.rect.y + top // 2 + 205
            desktop.click(window.rect.x + 220, confirm_y)
            expected = "私密" if visibility == "private" else "公开"
        def outer_composer_rows():
            observed = self.moments_composer_rows(desktop, window)
            visible = [line for line in observed if "谁可以看" in line["text"].replace(" ", "")
                       and line["rect"][1] // 2 > 1100]
            selected = ([line for line in observed if expected in line["text"].replace(" ", "")
                         and line["rect"][0] // 2 > 300] if expected else [True])
            return observed if len(visible) == 1 and len(selected) == 1 else None
        rows = wait_until(outer_composer_rows, desktop.wake, self.config.timeout,
                          "selected Moments visibility").value
        visible_row = next(line for line in rows if "谁可以看" in line["text"].replace(" ", "")
                           and line["rect"][1] // 2 > 1100)
        publish_y = window.rect.y + visible_row["rect"][1] // 2 + 93
        desktop.click(window.rect.x + 210, publish_y)
        try:
            wait_until(lambda: not any("谁可以看" in line["text"].replace(" ", "")
                       for line in self.moments_composer_rows(desktop, window)), desktop.wake,
                       self.config.timeout, "Moments composer to close")
            if text:
                self.moments_find_text(desktop, window, text, exact=False, max_pages=3)
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Moments publish was submitted but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"text": text, "images": [str(resolved)], "visibility": visibility,
                "status": "published", "scope": "own"}

    def moments_read(self, chat):
        opened = self.moments_open(chat)
        rect = Rect(**opened["window"]["rect"])
        reader = self.content_reader()
        lines = reader.lines(self.connect().capture(rect), psm=11,
                             scale=self.content_scale(reader, 2))
        return {**opened, "scope": "visible", "recognition": reader.profile, "rows": lines,
                "limitations": ["Visibility follows contact privacy settings", "Post structure is not yet resolved"]}

    def moments_feed_open(self):
        main = self.prepare()
        desktop = self.connect()
        before = {item.id for item in desktop.windows()}
        desktop.click(main.rect.x + 18, main.rect.y + 244)
        window = wait_until(lambda: next((item for item in desktop.windows()
            if item.id not in before and item.title == "朋友圈"), None), desktop.wake,
            self.config.timeout, "global Moments window").value
        screen = desktop.bounds
        width = min(1800, screen.width)
        window = desktop.resize(window, Rect((screen.width - width) // 2, 0, width, screen.height))
        content = Rect(window.rect.x + 30, window.rect.y + 180,
                       window.rect.width - 60, window.rect.height - 220)
        wait_until(lambda: np.asarray(desktop.capture(content)).min() < 238, desktop.wake,
                   max(12, self.config.timeout), "global Moments content")
        desktop.settle(content, timeout=3, quiet_ms=80)
        return {"window": window.as_dict(), "scope": "feed",
                "screenshot": self.screenshot(window_id=window.id)}

    def moments_feed_read(self):
        opened = self.moments_feed_open()
        window = Rect(**opened["window"]["rect"])
        reader = self.content_reader()
        lines = reader.lines(self.connect().capture(window), psm=11,
                             scale=self.content_scale(reader, 2))
        return {**opened, "recognition": reader.profile, "rows": lines,
                "limitations": ["Only currently visible feed content is returned",
                                "Post structure is not yet resolved"]}

    def moments_pinned_read(self):
        opened = self.moments_feed_open()
        desktop = self.connect()
        window = next(item for item in desktop.windows() if item.id == opened["window"]["id"])
        top = Rect(window.rect.x + 20, window.rect.y + 160, window.rect.width - 40,
                   min(300, window.rect.height - 160))
        rows = self.ocr.lines(desktop.capture(top).resize((top.width * 2, top.height * 2)), psm=11)
        matches = [line for line in rows if "置顶" in line["text"].replace(" ", "")]
        if len(matches) != 1:
            raise AutomationError("FEATURE_UNAVAILABLE", "Global Moments does not expose one visible pinned-post row",
                                  {"matches": len(matches)})
        left, row_top, width, height = matches[0]["rect"]
        before = image_digest(desktop.capture(window.rect))
        desktop.click(top.x + (left + width // 2) // 2, top.y + (row_top + height // 2) // 2)
        desktop.changed(window.rect, before, timeout=self.config.timeout)
        desktop.settle(window.rect, timeout=3, quiet_ms=80)
        reader = self.content_reader()
        lines = reader.lines(desktop.capture(window.rect), psm=11,
                             scale=self.content_scale(reader, 2))
        return {**opened, "scope": "pinned", "recognition": reader.profile, "rows": lines}

    def moments_find_text(self, desktop, window, text, exact=False, max_pages=10):
        region = self.clamp_region(Rect(window.rect.x + 30, window.rect.y + 70,
                                        window.rect.width - 60, window.rect.height - 100))
        reader = self.ascii_ocr if text.isascii() else self.content_reader()
        needle = text_identity(text)
        seen = set()
        for page in range(1, max_pages + 1):
            image = desktop.capture(region)
            matches = []
            for line in reader.lines(image.resize((image.width * 2, image.height * 2)), psm=11):
                observed = text_identity(line["text"])
                if (observed == needle if exact else needle in observed):
                    matches.append(line)
            if len(matches) == 1:
                left, top, width, height = matches[0]["rect"]
                return Rect(region.x + left // 2, region.y + top // 2,
                            max(1, width // 2), max(1, height // 2)), page
            if len(matches) > 1:
                raise AutomationError("POST_NOT_UNIQUE", "More than one visible Moments item matches text",
                                      {"matches": len(matches), "page": page})
            before = image_digest(image)
            if before in seen:
                break
            seen.add(before)
            desktop.click(region.x + region.width // 2, region.y + region.height // 2, button=5)
            try:
                desktop.changed(region, before, timeout=min(1, self.config.timeout))
            except AutomationError as error:
                if error.code != "TIMEOUT":
                    raise
                break
        raise AutomationError("MOMENTS_TEXT_NOT_FOUND", "Text was not found in the visible Moments pages",
                              {"text": text, "pages": len(seen)})

    def moments_post_detail(self, chat, post_text):
        if not post_text.strip():
            raise AutomationError("INVALID_PARAMS", "post_text must identify a visible post")
        opened = self.moments_open(chat)
        desktop = self.connect()
        window = next(item for item in desktop.windows() if item.id == opened["window"]["id"])
        target, pages = self.moments_find_text(desktop, window, post_text)
        desktop.click(target.x + target.width // 2, target.y + target.height // 2)
        title = Rect(window.rect.x + window.rect.width // 2 - 80, window.rect.y, 160, 60)
        self.semantic_wait(title, lambda image: "详情" in
                           "".join(line["text"] for line in self.ocr.lines(
                               image.resize((image.width * 4, image.height * 4)), 7)).replace(" ", ""),
                           "Moments post detail")
        return {**opened, "post_pages_scanned": pages}, desktop, window

    def moments_like_state(self, desktop, window):
        menu_button = (window.rect.x + window.rect.width - 40, window.rect.y + 280)
        desktop.click(*menu_button)

        popup_window = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and 100 <= item.rect.width <= 300
            and 25 <= item.rect.height <= 100), None), desktop.wake, self.config.timeout,
            "Moments action menu").value
        popup = popup_window.rect
        desktop.settle(popup, timeout=min(1, self.config.timeout), quiet_ms=40)

        text = "".join(line["text"] for line in self.ocr.lines(
            desktop.capture(popup).resize((popup.width * 4, popup.height * 4)), 11)).replace(" ", "")
        if "取消" in text:
            status = "liked"
        elif "赞" in text:
            status = "unliked"
        else:
            status = "unknown"
        return menu_button, popup, status

    def moments_comment_editor(self, desktop, window):
        search = Rect(window.rect.x + 35, window.rect.y + 300,
                      min(700, window.rect.width - 70), min(900, window.rect.height - 330))

        def editor():
            pixels = np.asarray(desktop.capture(search), dtype=np.int16)
            green = ((pixels[:, :, 1] > pixels[:, :, 0] + 35)
                     & (pixels[:, :, 1] > pixels[:, :, 2] + 10))
            rows = np.flatnonzero(green.sum(axis=1) >= 30)
            if len(rows) < 2:
                return None
            top, bottom = int(rows[0]), int(rows[-1])
            columns = np.flatnonzero(green[top:bottom + 1].sum(axis=0) >= 2)
            if len(columns) < 2 or bottom - top < 35:
                return None
            return Rect(search.x + int(columns[0]), search.y + top,
                        int(columns[-1] - columns[0] + 1), bottom - top + 1)

        return wait_until(editor, desktop.wake, self.config.timeout, "Moments comment editor").value

    def moments_own_avatar(self, desktop, window):
        main = desktop.main_window()
        template = desktop.capture(Rect(main.rect.x + 18, main.rect.y + 25, 40, 40))
        likes = self.clamp_region(Rect(window.rect.x + 50, window.rect.y + 280,
                                       min(900, window.rect.width - 100), 300))
        score, rect = avatar_template_match(desktop.capture(likes), template)
        return {"present": score <= 22.0, "score": round(score, 3),
                "rect": [likes.x + rect[0], likes.y + rect[1], rect[2], rect[3]] if rect else None}

    def moments_set_like(self, chat, post_text, desired):
        _, desktop, window = self.moments_post_detail(chat, post_text)
        avatar_before = self.moments_own_avatar(desktop, window)
        _, popup, status = self.moments_like_state(desktop, window)
        current = avatar_before["present"]
        if current == desired:
            desktop.key("Escape")
            return {"chat": chat, "post_text": post_text,
                    "status": "already_liked" if desired else "already_unliked",
                    "avatar": avatar_before}
        desktop.click(popup.x + popup.width // 4, popup.y + popup.height // 2)
        try:
            avatar_after = wait_until(lambda: (item if item["present"] == desired else None)
                if (item := self.moments_own_avatar(desktop, window)) else None,
                desktop.wake, self.config.timeout, "own avatar in Moments likes").value
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Like setting was clicked but not verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "post_text": post_text,
                "status": "liked" if desired else "unliked", "avatar": avatar_after,
                "menu_initial_state": status}

    def moments_like(self, chat, post_text):
        return self.moments_set_like(chat, post_text, True)

    def moments_unlike(self, chat, post_text):
        return self.moments_set_like(chat, post_text, False)

    def moments_comment(self, chat, post_text, text):
        _, desktop, window = self.moments_post_detail(chat, post_text)
        _, popup, _ = self.moments_like_state(desktop, window)
        desktop.click(popup.x + popup.width * 3 // 4, popup.y + popup.height // 2)
        editor = self.moments_comment_editor(desktop, window)
        before = desktop.fingerprint(Rect(window.rect.x + 35, editor.y - 80,
                                          min(900, window.rect.width - 70), 300))
        desktop.click(editor.x + 35, editor.y + 25)
        desktop.paste(text)
        if not editor_has_content(desktop.capture(editor)):
            raise AutomationError("DRAFT_UNVERIFIED", "Moments comment editor did not retain requested text")
        desktop.click(editor.x + editor.width - 38, editor.y + editor.height - 24)
        observed = Rect(window.rect.x + 35, editor.y - 80, min(900, window.rect.width - 70), 300)
        try:
            self.semantic_wait(observed, lambda image: any(text_identity(line["text"]) == text_identity(text)
                for line in self.ocr.lines(image.resize((image.width * 2, image.height * 2)), psm=11)),
                "published Moments comment")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Comment was submitted but could not be verified",
                                  {"cause": error.as_dict(), "before": before}) from error
        return {"chat": chat, "post_text": post_text, "text": text, "status": "published"}

    def moments_comment_delete(self, chat, post_text, text):
        _, desktop, window = self.moments_post_detail(chat, post_text)
        target, pages = self.moments_find_text(desktop, window, text, exact=True)
        desktop.click(target.x + target.width // 2, target.y + target.height // 2, button=3)
        menu = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and 80 <= item.rect.width <= 220
            and 70 <= item.rect.height <= 180), None), desktop.wake, self.config.timeout,
            "own Moments comment menu").value
        menu_image = desktop.capture(menu.rect)
        lines = self.ocr.lines(menu_image.resize((menu.rect.width * 4, menu.rect.height * 4)), psm=11)
        delete_rows = [line for line in lines if "除" in line["text"]]
        if len(delete_rows) != 1:
            desktop.key("Escape")
            raise AutomationError("DELETE_MENU_UNVERIFIED", "Own comment menu did not expose one delete item",
                                  {"rows": lines})
        row = delete_rows[0]["rect"]
        desktop.click(menu.rect.x + (row[0] + row[2] // 2) // 4,
                      menu.rect.y + (row[1] + row[3] // 2) // 4)
        try:
            self.semantic_wait(window.rect, lambda current: not any(
                text_identity(line["text"]) == text_identity(text)
                for line in self.ascii_ocr.lines(current.resize((current.width * 2, current.height * 2)), psm=11)),
                "deleted Moments comment")
        except AutomationError as error:
            raise AutomationError("OUTCOME_UNKNOWN", "Comment deletion was clicked but could not be verified",
                                  {"cause": error.as_dict()}) from error
        return {"chat": chat, "post_text": post_text, "text": text, "status": "deleted",
                "comment_pages_scanned": pages}

    def moments_pin(self, post_text, enabled):
        opened = self.moments_open_self()
        desktop = self.connect()
        window = next(item for item in desktop.windows() if item.id == opened["window"]["id"])
        target, pages = self.moments_find_text(desktop, window, post_text)
        desktop.click(target.x + target.width // 2, target.y + target.height // 2)
        title = Rect(window.rect.x + window.rect.width // 2 - 80, window.rect.y, 160, 60)
        self.semantic_wait(title, lambda image: "详情" in "".join(
            line["text"] for line in self.ocr.lines(image.resize((image.width * 4, image.height * 4)), 7)
        ).replace(" ", ""), "own Moments post detail")
        menu_button = (window.rect.x + window.rect.width // 2 + 165, window.rect.y + 22)
        desktop.click(*menu_button)
        menu = wait_until(lambda: next((item for item in desktop.windows()
            if item.title == "wechat" and 90 <= item.rect.width <= 300
            and 25 <= item.rect.height <= 200), None), desktop.wake, self.config.timeout,
            "own Moments action menu").value
        desktop.settle(menu.rect, timeout=min(1, self.config.timeout), quiet_ms=40)
        rows = self.ocr.lines(desktop.capture(menu.rect).resize((menu.rect.width * 4, menu.rect.height * 4)), psm=11)
        labels = "".join(row["text"] for row in rows).replace(" ", "")
        current = (True if "取消" in labels and "顶" in labels
                   else False if "顶此朋友" in labels else None)
        if current is None:
            desktop.key("Escape")
            raise AutomationError("PIN_MENU_UNVERIFIED", "Own post menu did not expose pin state", {"rows": rows})
        if current == enabled:
            desktop.key("Escape")
            return {"post_text": post_text, "enabled": enabled, "status": "unchanged", "pages_scanned": pages}
        matches = ([row for row in rows if "顶此朋友" in row["text"].replace(" ", "")]
                   if enabled else [row for row in rows
                                    if "取消" in row["text"].replace(" ", "")
                                    and "顶" in row["text"].replace(" ", "")])
        if len(matches) != 1:
            desktop.key("Escape")
            raise AutomationError("PIN_MENU_UNVERIFIED", "Pin action was not uniquely visible", {"rows": rows})
        left, top, width, height = matches[0]["rect"]
        desktop.click(menu.rect.x + (left + width // 2) // 4,
                      menu.rect.y + (top + height // 2) // 4)
        return {"post_text": post_text, "enabled": enabled, "status": "submitted", "pages_scanned": pages}

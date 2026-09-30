import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from wechat_cli.config import Config
from wechat_cli.errors import AutomationError
from wechat_cli.mcp import MCPServer
from wechat_cli.automation import Automation
from wechat_cli.deployment import create_vnc_password, display_ready, launch, start_remote, stop_remote
from wechat_cli.protocol import Dispatcher, decode
from wechat_cli.registry import METHODS, capabilities, validate
from wechat_cli.state import State
from wechat_cli.vision import (adjacent_bubble, avatar_template_match, bubble_regions, merge_message_pages,
                               runs, text_identity)
from wechat_cli.wait import wait_until
from wechat_cli.web import TaskRunner, TaskStore
from wechat_cli.x11 import Rect


class DestructiveDialogTests(unittest.TestCase):
    def setUp(self):
        self.automation = Automation.__new__(Automation)
        self.automation.config = SimpleNamespace(timeout=0.01)
        self.desktop = Mock()
        self.automation.connect = Mock(return_value=self.desktop)
        self.main = SimpleNamespace(rect=Rect(0, 0, 1200, 1200))
        self.desktop.main_window.return_value = self.main
        self.dialog = SimpleNamespace(id=2, title="wechat", rect=Rect(200, 200, 300, 170))
        self.desktop.windows.side_effect = [[], [self.dialog]]
        self.desktop.capture.return_value = Image.new("RGB", (300, 170), "white")
        self.automation.ocr = Mock()

    def test_delete_refuses_unverified_dialog_and_cancels(self):
        self.automation.chat_open = Mock()
        self.automation.find_message = Mock(return_value=Rect(800, 400, 200, 45))
        self.automation.choose_menu = Mock()
        self.automation.ocr.lines.return_value = [{"text": "未知操作"}]
        with patch("wechat_cli.automation.wait_until", side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            with self.assertRaises(AutomationError) as error:
                self.automation.message_delete("False", "hello")
        self.assertEqual(error.exception.code, "DELETE_DIALOG_UNVERIFIED")
        self.desktop.key.assert_called_once_with("Escape")
        self.assertEqual(self.desktop.click.call_count, 1)

    def test_leave_refuses_wrong_group_name_and_cancels(self):
        self.automation.group_panel = Mock(return_value=self.main)
        self.automation.ocr.lines.side_effect = [
            [{"text": "退出群聊", "rect": [10, 5, 60, 20]}],
            [{"text": "退出群聊 其他群 清空聊天记录"}]]
        with patch("wechat_cli.automation.wait_until", side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            with self.assertRaises(AutomationError) as error:
                self.automation.group_leave("目标群")
        self.assertEqual(error.exception.code, "LEAVE_DIALOG_UNVERIFIED")
        self.desktop.key.assert_called_once_with("Escape")
        self.assertEqual(self.desktop.click.call_count, 1)

    def test_leave_refuses_clear_history_checkbox_and_cancels(self):
        self.automation.group_panel = Mock(return_value=self.main)
        self.automation.ocr.lines.side_effect = [
            [{"text": "退出群聊", "rect": [10, 5, 60, 20]}],
            [{"text": "退出群聊 目标群 清空聊天记录"}]]
        self.desktop.capture.side_effect = [Image.new("RGB", (245, 90), "white"),
                                             Image.new("RGB", (300, 170), "white"),
                                             Image.new("RGB", (21, 21), (0, 255, 0))]
        with patch("wechat_cli.automation.wait_until", side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            with self.assertRaises(AutomationError) as error:
                self.automation.group_leave("目标群")
        self.assertEqual(error.exception.code, "HISTORY_CLEAR_SELECTED")
        self.desktop.key.assert_called_once_with("Escape")

    def test_leave_finds_action_anywhere_in_the_panel_lower_section(self):
        self.automation.group_panel = Mock(return_value=self.main)
        self.automation.ocr.lines.side_effect = [
            [{"text": "退 出 群 聊", "rect": [194, 90, 98, 40]}],
            [{"text": "退出群聊 其他群 清空聊天记录"}],
        ]
        with patch("wechat_cli.automation.wait_until", side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            with self.assertRaises(AutomationError) as error:
                self.automation.group_leave("目标群")
        self.assertEqual(error.exception.code, "LEAVE_DIALOG_UNVERIFIED")
        self.desktop.click.assert_called_once_with(1051, 129)
        self.assertEqual(self.desktop.click.call_count, 1)

    def test_dialog_action_clicks_only_unique_exact_label(self):
        lines = [{"text": "删除该条消息？", "rect": [20, 20, 100, 20]},
                 {"text": "取消", "rect": [300, 300, 50, 20]},
                 {"text": "删除", "rect": [100, 300, 50, 20]}]
        self.automation.click_dialog_action(self.dialog, lines, ["删除", "确定"])
        self.desktop.click.assert_called_once_with(241, 303)

    def test_dialog_action_refuses_missing_or_ambiguous_label(self):
        lines = [{"text": "删除", "rect": [100, 300, 50, 20]},
                 {"text": "确定", "rect": [200, 300, 50, 20]}]
        with self.assertRaises(AutomationError) as error:
            self.automation.click_dialog_action(self.dialog, lines, ["删除", "确定"])
        self.assertEqual(error.exception.code, "DIALOG_ACTION_UNVERIFIED")
        self.desktop.key.assert_called_once_with("Escape")


class SessionStatusTests(unittest.TestCase):
    def setUp(self):
        self.automation = Automation.__new__(Automation)
        self.automation.config = SimpleNamespace(display=":99")
        self.desktop = Mock()
        self.automation.connect = Mock(return_value=self.desktop)
        self.window = SimpleNamespace(id=1, title="微信", rect=Rect(0, 0, 3840, 2160),
                                      as_dict=lambda: {})
        self.desktop.windows.return_value = [self.window]

    def test_fullscreen_login_page_is_not_treated_as_logged_in(self):
        self.desktop.capture.return_value = Image.new("RGB", (1280, 2160), "white")
        self.assertEqual(self.automation.session_status()["state"], "login_required")

    def test_normal_client_chrome_is_treated_as_logged_in(self):
        image = Image.new("RGB", (1280, 2160), "white")
        image.paste((45, 45, 45), (0, 0, 180, 2160))
        self.desktop.capture.return_value = image
        self.assertEqual(self.automation.session_status()["state"], "logged_in")

    def test_prepare_refuses_fullscreen_login_page(self):
        self.desktop.main_window.return_value = self.window
        self.desktop.capture.return_value = Image.new("RGB", (1280, 2160), "white")
        with self.assertRaises(AutomationError) as error:
            self.automation.prepare()
        self.assertEqual(error.exception.code, "LOGIN_REQUIRED")

    def test_login_clicks_unique_enter_wechat_button(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        desktop = Mock()
        window = SimpleNamespace(id=1, rect=Rect(0, 0, 900, 900))
        desktop.main_window.return_value = window
        desktop.capture.return_value = Image.new("RGB", (300, 360), "white")
        automation.connect = Mock(return_value=desktop)
        automation.session_status = Mock(side_effect=[
            {"state": "login_required", "window": {"id": 1}},
            {"state": "login_required", "window": {"id": 1}},
        ])
        automation.ocr = Mock()
        automation.ocr.lines.return_value = [{"text": "进入微信", "rect": [100, 120, 80, 30]}]
        automation.semantic_wait = Mock(return_value=True)
        automation.login_artifacts = Mock(return_value={"screenshot": {}, "qr": None})

        result = automation.session_login()

        self.assertEqual(result["login_action"], "requested")
        desktop.click.assert_called_once_with(70, 67)

    def test_login_falls_back_to_one_large_green_primary_button(self):
        image = Image.new("RGB", (292, 396), "white")
        image.paste((7, 193, 96), (52, 281, 240, 319))
        button = Automation.login_primary_button(image)
        self.assertEqual(button, Rect(52, 281, 188, 38))

    def test_history_management_reports_local_only_scope(self):
        self.automation.config = SimpleNamespace(display=":99", retention_days=15)
        result = self.automation.history_manage()
        self.assertEqual(result["retention_days"], 15)
        self.assertFalse(result["export_supported"])

    def test_voice_setting_detects_green_toggle_from_its_full_region(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.prepare = Mock(return_value=SimpleNamespace(rect=Rect(0, 0, 1200, 900)))
        desktop = Mock()
        automation.connect = Mock(return_value=desktop)
        settings = SimpleNamespace(id=2, title="设置", rect=Rect(100, 100, 573, 709))
        desktop.windows.return_value = [settings]
        image = Image.new("RGB", (573, 709), "white")
        desktop.capture.return_value = image
        automation.ocr = Mock()
        automation.ocr.lines.side_effect = [
            [{"text": "通 用", "rect": [40, 214, 118, 42]}],
            [{"text": "聊 天 中 的 语 音 消 息 自 动 转 成 文 字", "rect": [443, 924, 405, 43]}],
        ]
        toggle = Image.new("RGB", (65, 40), (247, 247, 247))
        toggle.paste((7, 193, 96), (20, 10, 46, 30))
        desktop.capture.side_effect = [image, image, toggle]

        with patch("wechat_cli.automation.wait_until", side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            result = automation.settings_voice_text()

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(desktop.click.call_count, 1)


class LayoutTests(unittest.TestCase):
    def test_editor_region_excludes_the_fixed_composer_toolbar(self):
        automation = Automation.__new__(Automation)
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))

        self.assertEqual(automation.editor_region(main), Rect(400, 2065, 1200, 45))

    def test_chat_header_matching_requires_the_exact_display_name(self):
        self.assertTrue(Automation.chat_header_matches(None, "False", "False"))
        self.assertFalse(Automation.chat_header_matches(None, "False (2)", "False"))

    def test_group_header_search_uses_base_name_but_keeps_exact_verification(self):
        self.assertEqual(Automation.chat_search_label("testgroup1 (2)"), "testgroup1")
        self.assertTrue(Automation.chat_header_matches(None, "testgroup1 (2)", "testgroup1 (2)"))
        self.assertTrue(Automation.chat_header_matches(None, "testgroup]1 (2)", "testgroup1 (2)"))
        self.assertTrue(Automation.chat_header_matches(None, "testgroup]1 (3)", "testgroup1 (2)"))
        self.assertFalse(Automation.chat_header_matches(None, "testgroup2 (2)", "testgroup1 (2)"))

    def test_group_selector_uses_ascii_ocr_for_ascii_contact(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.ocr = Mock()
        automation.ascii_ocr = Mock()
        automation.ascii_ocr.lines.side_effect = [
            [{"text": "False", "rect": [10, 20, 40, 16]}],
            [{"text": "False", "rect": [10, 20, 40, 16]}],
        ]
        desktop = Mock()
        desktop.capture.return_value = Image.new("RGB", (500, 500), "white")
        desktop.selected_text.return_value = "False"
        dialog = SimpleNamespace(rect=Rect(100, 100, 600, 500))

        with patch("wechat_cli.automation.wait_until",
                   side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            automation.group_selector_contacts(desktop, dialog, ["False"])

        self.assertEqual(automation.ascii_ocr.lines.call_count, 2)
        automation.ocr.lines.assert_not_called()

    def test_group_selector_ignores_trailing_ocr_punctuation(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.ocr = Mock()
        automation.ascii_ocr = Mock()
        automation.ascii_ocr.lines.side_effect = [
            [{"text": "O yuo.", "rect": [10, 20, 40, 16]}],
            [{"text": "O yuo.", "rect": [10, 20, 40, 16]}],
        ]
        desktop = Mock()
        desktop.capture.return_value = Image.new("RGB", (500, 500), "white")
        desktop.selected_text.return_value = "yuo"
        dialog = SimpleNamespace(rect=Rect(100, 100, 600, 500))

        with patch("wechat_cli.automation.wait_until",
                   side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            automation.group_selector_contacts(desktop, dialog, ["yuo"])

        self.assertEqual(desktop.click.call_count, 2)

    def test_group_selector_complete_falls_back_to_verified_green_button(self):
        automation = Automation.__new__(Automation)
        automation.ocr = Mock()
        automation.ocr.lines.return_value = []
        desktop = Mock()
        image = Image.new("RGB", (600, 500), "white")
        image.paste((7, 193, 96), (330, 420, 460, 455))
        desktop.capture.return_value = image
        dialog = SimpleNamespace(rect=Rect(100, 200, 600, 500))

        automation.group_selector_complete(desktop, dialog)

        desktop.click.assert_called_once_with(494, 637)
        desktop.key.assert_not_called()

    def test_group_member_control_uses_unique_chinese_action_label(self):
        automation = Automation.__new__(Automation)
        automation.group_detail_rows = Mock(return_value=(Rect(935, 74, 265, 726),
            [{"text": "添 加", "rect": [100, 220, 90, 60]}]))
        main = SimpleNamespace(rect=Rect(0, 0, 1200, 900))

        self.assertEqual(automation.group_member_control(main, "+"), (1007, 159))

    def test_moments_find_text_advances_when_first_page_has_no_match(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.clamp_region = lambda value: value
        automation.ocr = Mock()
        automation.ocr.lines.side_effect = [[], [{"text": "目标朋友圈", "rect": [20, 40, 100, 20]}]]
        automation.ascii_ocr = Mock()
        desktop = Mock()
        desktop.capture.side_effect = [Image.new("RGB", (500, 500), "white"),
                                       Image.new("RGB", (500, 500), "black")]
        desktop.changed.return_value = True
        window = SimpleNamespace(rect=Rect(0, 0, 600, 700))
        target, pages = automation.moments_find_text(desktop, window, "目标朋友圈")
        self.assertEqual((target.x, target.y, target.width, target.height), (40, 90, 50, 10))
        self.assertEqual(pages, 2)
        self.assertEqual(desktop.click.call_args.kwargs["button"], 5)

    def test_account_identity_uses_ascii_fallback_and_removes_name_icon(self):
        lines = [{"text": "uu &", "rect": [316, 86, 120, 53]},
                 {"text": "微信号", "rect": [317, 144, 107, 48]}]
        ascii_lines = [{"text": "uu_TwT", "rect": [466, 150, 132, 36]}]
        self.assertEqual(Automation.extract_account_identity(lines, ascii_lines), ("uu", "uu_TwT"))

    def test_composer_visibility_checks_chat_toolbar_not_sidebar(self):
        automation = Automation.__new__(Automation)
        desktop = Mock()
        automation.connect = Mock(return_value=desktop)
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.capture.return_value = Image.new("RGB", (22, 30), (40, 40, 40))
        self.assertTrue(automation.composer_visible(main))
        self.assertEqual(desktop.capture.call_args.args[0], Rect(300, 2013, 22, 30))

    def test_visible_chat_row_matches_ascii_name_in_row_preview(self):
        automation = Automation.__new__(Automation)
        desktop = Mock()
        automation.connect = Mock(return_value=desktop)
        automation.ocr = Mock()
        automation.ocr.lines.return_value = [{"text": "WS False 18:33", "rect": [3, 223, 197, 45]}]
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        result = automation.visible_chat_row(main, "False")
        self.assertEqual(result["rect"], [3, 223, 197, 45])
        self.assertEqual(result["region"], Rect(70, 74, 210, 2080))

    def test_chat_open_uses_search_clear_paste_enter_without_result_ocr(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        main = SimpleNamespace(rect=Rect(0, 0, 1200, 900))
        desktop = Mock()
        desktop.selected_text.return_value = "False"
        automation.prepare = Mock(return_value=main)
        automation.connect = Mock(return_value=desktop)
        automation.select_sidebar_tab = Mock()
        automation.global_search_box = Mock(return_value=Rect(42, 14, 90, 24))
        automation.current_chat = Mock(return_value="Other")
        automation.composer_visible = Mock(return_value=True)
        automation.semantic_wait = Mock(return_value=True)

        result = automation.chat_open("False")

        self.assertEqual(result["matched_by"], "search_enter_and_header")
        desktop.click.assert_called_once_with(87, 26)
        self.assertEqual([call.args[0] for call in desktop.key.call_args_list],
                         ["Control_L+a", "BackSpace", "Return"])
        desktop.paste.assert_called_once_with("False")
        desktop.changed.assert_not_called()
        self.assertEqual(automation.semantic_wait.call_args_list[0].args[2],
                         "first search result highlight")

    def test_chat_open_does_not_press_enter_when_input_verification_fails(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        main = SimpleNamespace(rect=Rect(0, 0, 1200, 900))
        desktop = Mock()
        desktop.selected_text.return_value = "wrong query"
        automation.prepare = Mock(return_value=main)
        automation.connect = Mock(return_value=desktop)
        automation.select_sidebar_tab = Mock()
        automation.global_search_box = Mock(return_value=Rect(42, 14, 90, 24))
        automation.current_chat = Mock(return_value="Other")
        automation.semantic_wait = Mock()

        with self.assertRaises(AutomationError) as error:
            automation.chat_open("False")

        self.assertEqual(error.exception.code, "SEARCH_UNVERIFIED")
        self.assertNotIn("Return", [called.args[0] for called in desktop.key.call_args_list])
        automation.semantic_wait.assert_not_called()

    def test_search_result_readiness_detects_green_text_not_white_background(self):
        image = Image.new("RGB", (150, 58), "white")
        self.assertFalse(Automation.search_result_ready(image))
        image.paste((0, 180, 80), (5, 10, 15, 20))
        self.assertTrue(Automation.search_result_ready(image))

    def test_global_search_box_uses_ocr_label_location(self):
        automation = Automation.__new__(Automation)
        desktop = Mock()
        desktop.capture.return_value = Image.new("RGB", (260, 64), "white")
        automation.connect = Mock(return_value=desktop)
        automation.ocr = Mock()
        automation.ocr.lines.return_value = [{"text": "搜 索", "rect": [295, 120, 89, 62]}]
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))

        result = automation.global_search_box(main)

        self.assertEqual(result, Rect(105, 38, 22, 15))

    def test_moments_like_wrapper_selects_desired_state(self):
        automation = Automation.__new__(Automation)
        automation.moments_set_like = Mock(return_value={"status": "liked"})
        self.assertEqual(automation.moments_like("False", "post"), {"status": "liked"})
        automation.moments_set_like.assert_called_once_with("False", "post", True)
        automation.moments_unlike("False", "post")
        self.assertEqual(automation.moments_set_like.call_args.args, ("False", "post", False))

    def test_group_contacts_refuses_duplicate_display_names_before_ui(self):
        automation = Automation.__new__(Automation)
        with self.assertRaises(AutomationError) as error:
            automation.group_contacts_validate(["False", "False"], 2)
        self.assertEqual(error.exception.code, "INVALID_PARAMS")

    def test_pin_menu_accepts_ocr_missing_zhitop_character(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.moments_open_self = Mock(return_value={"window": {"id": 2}})
        automation.connect = Mock()
        desktop = automation.connect.return_value
        window = SimpleNamespace(id=2, rect=Rect(0, 0, 1800, 1200))
        menu = SimpleNamespace(id=3, title="wechat", rect=Rect(1300, 0, 200, 80))
        desktop.windows.side_effect = [[window], [menu]]
        desktop.capture.return_value = Image.new("RGB", (200, 80), "white")
        desktop.settle.return_value = {"stable": True}
        automation.moments_find_text = Mock(return_value=(Rect(100, 100, 50, 20), 1))
        automation.semantic_wait = Mock()
        automation.ocr = Mock()
        automation.ocr.lines.return_value = [{"text": "香 顶 此 朋 友 圈", "rect": [40, 20, 100, 20]}]
        with patch("wechat_cli.automation.wait_until", side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            result = automation.moments_pin("sample", True)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(desktop.click.call_args.args, (1322, 7))

    def test_pinned_moments_refuses_absent_or_ambiguous_row(self):
        automation = Automation.__new__(Automation)
        automation.moments_feed_open = Mock(return_value={"window": {"id": 2}})
        automation.connect = Mock()
        desktop = automation.connect.return_value
        desktop.windows.return_value = [SimpleNamespace(id=2, rect=Rect(0, 0, 1800, 1200))]
        desktop.capture.return_value = Image.new("RGB", (1760, 300), "white")
        automation.ocr = Mock()
        automation.ocr.lines.return_value = []
        with self.assertRaises(AutomationError) as error:
            automation.moments_pinned_read()
        self.assertEqual(error.exception.code, "FEATURE_UNAVAILABLE")


class DownloadSafetyTests(unittest.TestCase):
    def test_download_never_overwrites_existing_path(self):
        automation = Automation.__new__(Automation)
        automation.chat_open = Mock()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "existing.bin"
            destination.write_bytes(b"keep")
            with self.assertRaises(AutomationError) as error:
                automation.message_download("False", "0" * 32, str(destination))
        self.assertEqual(error.exception.code, "FILE_EXISTS")
        automation.chat_open.assert_not_called()


class MessageSearchTests(unittest.TestCase):
    def test_avatar_candidates_are_found_in_the_message_edge_strip(self):
        automation = Automation.__new__(Automation)
        automation.connect = Mock()
        desktop = automation.connect.return_value
        main = SimpleNamespace(rect=Rect(0, 0, 1000, 1000))
        image = Image.new("RGB", (720, 766), "#f5f5f5")
        image.paste((92, 65, 43), (105, 500, 137, 532))
        desktop.capture.return_value = image

        candidates = automation.avatar_candidates(main, "incoming")

        self.assertEqual(candidates, [Rect(385, 574, 32, 32)])

    def test_pat_uses_the_verified_avatar_context_menu(self):
        automation = Automation.__new__(Automation)
        automation.chat_open = Mock()
        automation.connect = Mock()
        desktop = automation.connect.return_value
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.main_window.return_value = main
        desktop.windows.return_value = []
        avatar = Rect(380, 900, 30, 30)
        automation.recent_avatar = Mock(return_value=(avatar, 1))
        automation.pat_notice_lines = Mock(return_value=(Rect(280, 74, 3560, 1852), []))
        automation.choose_menu = Mock()
        automation.semantic_wait = Mock()

        result = automation.message_pat("False")

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["pages_scrolled"], 1)
        desktop.click.assert_called_once_with(395, 915, button=3)
        automation.choose_menu.assert_called_once_with(["拍一拍", "Pat"], set())

    def test_pat_revoke_refuses_a_notice_not_sent_by_self(self):
        automation = Automation.__new__(Automation)
        automation.chat_open = Mock()
        automation.connect = Mock()
        desktop = automation.connect.return_value
        desktop.main_window.return_value = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        automation.pat_notice_lines = Mock(return_value=(Rect(280, 74, 3560, 1852), [
            {"text": "False 拍了拍你", "rect": [300, 800, 120, 25]}]))

        with self.assertRaises(AutomationError) as error:
            automation.message_pat_revoke("False")

        self.assertEqual(error.exception.code, "PAT_NOT_FOUND")
        desktop.move.assert_not_called()

    def test_pat_notice_recognizes_english_tickled_wording(self):
        self.assertTrue(Automation.is_pat_notice("I tickled False's head"))
        self.assertTrue(Automation.is_own_pat_notice("I tickled False's head"))
        self.assertTrue(Automation.is_own_pat_notice("tickied False's head"))
        self.assertFalse(Automation.is_own_pat_notice("False tickled you"))

    def test_send_verifies_pasted_draft_through_clipboard_before_submission(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.chat_open = Mock()
        automation.connect = Mock()
        desktop = automation.connect.return_value
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.main_window.return_value = main
        desktop.capture.return_value = Image.new("RGB", (1200, 70), "white")
        desktop.selected_text.side_effect = ["", "hello"]
        automation.current_chat = Mock(return_value="False")
        automation.semantic_wait = Mock()
        automation.find_message = Mock(return_value=Rect(1, 1, 1, 1))

        with patch("wechat_cli.automation.wait_until",
                   side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            result = automation.message_send("False", "hello")

        self.assertEqual(result["status"], "submitted")
        desktop.paste.assert_called_once_with("hello")
        self.assertEqual(desktop.selected_text.call_count, 2)
        automation.semantic_wait.assert_called_once()

    def test_send_preserves_existing_draft(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.chat_open = Mock()
        automation.connect = Mock()
        desktop = automation.connect.return_value
        desktop.main_window.return_value = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.selected_text.return_value = "do not overwrite"

        with self.assertRaises(AutomationError) as error:
            automation.message_send("False", "hello")

        self.assertEqual(error.exception.code, "DRAFT_PRESENT")
        desktop.paste.assert_not_called()
        desktop.key.assert_called_once_with("Escape")

    def test_draft_empty_checks_full_width_and_rejects_even_small_text(self):
        automation = Automation.__new__(Automation)
        desktop = Mock()
        automation.connect = Mock(return_value=desktop)
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        image = Image.new("RGB", (3510, 65), (237, 237, 237))
        desktop.capture.return_value = image
        self.assertTrue(automation.draft_empty(main))
        image.putpixel((3000, 5), (30, 30, 30))
        self.assertFalse(automation.draft_empty(main))
        desktop.capture.assert_called_with(Rect(300, 2045, 3510, 65))

    def test_send_keeps_draft_when_copy_fails_and_does_not_submit(self):
        automation = Automation.__new__(Automation)
        automation.chat_open = Mock()
        desktop = Mock()
        automation.connect = Mock(return_value=desktop)
        desktop.main_window.return_value = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.selected_text.side_effect = AutomationError("TIMEOUT", "copy timeout")
        with self.assertRaises(AutomationError) as error:
            automation.message_send("False", "hello")
        self.assertEqual(error.exception.code, "DRAFT_UNVERIFIED")
        desktop.paste.assert_not_called()
        self.assertEqual(desktop.click.call_count, 1)

    def test_send_does_not_submit_when_pasted_draft_cannot_be_verified(self):
        automation = Automation.__new__(Automation)
        automation.chat_open = Mock()
        desktop = Mock()
        automation.connect = Mock(return_value=desktop)
        desktop.main_window.return_value = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.selected_text.side_effect = ["", AutomationError("TIMEOUT", "copy timeout")]
        with self.assertRaises(AutomationError) as error:
            automation.message_send("False", "hello")
        self.assertEqual(error.exception.code, "DRAFT_UNVERIFIED")
        desktop.paste.assert_called_once_with("hello")
        self.assertEqual(desktop.click.call_count, 1)

    def test_send_reuses_an_exact_matching_draft(self):
        automation = Automation.__new__(Automation)
        automation.config = SimpleNamespace(timeout=0.1)
        automation.chat_open = Mock()
        automation.connect = Mock()
        desktop = automation.connect.return_value
        main = SimpleNamespace(rect=Rect(0, 0, 3840, 2160))
        desktop.main_window.return_value = main
        desktop.selected_text.return_value = "hello"
        automation.current_chat = Mock(return_value="False")
        automation.semantic_wait = Mock()
        automation.find_message = Mock(return_value=Rect(1, 1, 1, 1))

        with patch("wechat_cli.automation.wait_until",
                   side_effect=lambda predicate, *args: SimpleNamespace(value=predicate())):
            result = automation.message_send("False", "hello")

        self.assertEqual(result["status"], "submitted")
        desktop.paste.assert_not_called()
        self.assertEqual(desktop.selected_text.call_count, 1)

    def test_search_filters_recent_messages_case_insensitively(self):
        automation = Automation.__new__(Automation)
        automation.message_read = Mock(return_value={
            "messages": [{"text": "Hello World", "visual_digest": "a"},
                         {"text": "other", "visual_digest": "b"}],
            "count": 2, "pages": 1, "stop_reason": "limit_reached", "limitations": []})
        result = automation.message_search("False", "hello")
        self.assertEqual([item["visual_digest"] for item in result["matches"]], ["a"])
        automation.message_read.assert_called_once_with("False", 30)


class WaitTests(unittest.TestCase):
    def test_ready_does_not_wait(self):
        wake = Mock()
        result = wait_until(lambda: {"ready": True}, wake, 5, "ready")
        self.assertEqual(result.checks, 1)
        wake.assert_not_called()

    def test_event_is_not_completion(self):
        clock = Mock(side_effect=[0, 0, 0.1, 0.2, 0.3, 0.4, 0.5])
        predicate = Mock(side_effect=[False, False, "complete"])
        wake = Mock()
        result = wait_until(predicate, wake, 1, "target", clock=clock)
        self.assertEqual(result.value, "complete")
        self.assertEqual(wake.call_count, 2)

    def test_timeout(self):
        with self.assertRaises(AutomationError) as context:
            wait_until(lambda: False, lambda timeout: None, 0, "target")
        self.assertEqual(context.exception.code, "TIMEOUT")


class SchemaTests(unittest.TestCase):
    def test_all_registered_methods_have_automation_handlers(self):
        for name, method in METHODS.items():
            with self.subTest(name=name):
                self.assertIsNotNone(method.handler)
                self.assertTrue(hasattr(Automation, method.handler))

    def test_capabilities_expose_all_demo_callable_system_methods(self):
        methods = {method["method"]: method for method in capabilities()["methods"]}
        for name in ("metrics", "state.cleanup"):
            with self.subTest(name=name):
                self.assertEqual(methods[name]["status"], "implemented")
                self.assertEqual(methods[name]["params_schema"]["properties"], {})

    def test_limit_bounds_and_boolean(self):
        for limit in (0, 31, True, "10"):
            with self.assertRaises(AutomationError):
                validate({"chat": "False", "limit": limit}, METHODS["message.read"])

    def test_reject_unknown_parameter(self):
        with self.assertRaises(AutomationError):
            validate({"chat": "False", "typo": True}, METHODS["chat.open"])

    def test_reject_nan(self):
        with self.assertRaises(AutomationError):
            decode(b'{"id":NaN}')

    def test_new_method_schemas_reject_invalid_arguments(self):
        for name, params in (("message.send_file", {"chat": "False", "path": 7}),
                             ("message.download", {"chat": "False", "visual_digest": "short",
                                                   "path": "/tmp/out"}),
                             ("favorite.add", {"chat": "False", "text": ""}),
                             ("group.members", {"chat": False}),
                             ("moments.comment", {"chat": "False", "post_text": "post", "text": ""}),
                             ("moments.pin", {"post_text": "post", "enabled": "yes"}),
                             ("contact.add", {"identifier": "False", "message": "x" * 201}),
                             ("contact.accept", {"requester": ""}),
                             ("group.rename", {"chat": "群", "name": ""}),
                             ("group.announcement_set", {"chat": "群", "text": ""}),
                             ("group.create", {"contacts": ["only-one"]}),
                             ("group.invite", {"chat": "群", "contacts": []}),
                             ("group.remove", {"chat": "群", "member": ""}),
                             ("message.voice_text", {"chat": "False", "visual_digest": "short"})):
            with self.subTest(name=name), self.assertRaises(AutomationError):
                validate(params, METHODS[name])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(self.directory.name)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_pending_is_not_reexecuted(self):
        self.assertIsNone(self.state.claim("key", "message.send", {"text": "hello"}))
        with self.assertRaises(AutomationError) as context:
            self.state.claim("key", "message.send", {"text": "hello"})
        self.assertEqual(context.exception.code, "OUTCOME_UNKNOWN")

    def test_replay_and_conflict(self):
        self.state.claim("key", "message.send", {"text": "hello"})
        self.state.complete("key", {"ok": True})
        self.assertEqual(self.state.claim("key", "message.send", {"text": "hello"}), {"ok": True})
        with self.assertRaises(AutomationError) as context:
            self.state.claim("key", "message.send", {"text": "other"})
        self.assertEqual(context.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_cursor_survives_reopen(self):
        self.state.cursor("False", {"tail_digests": ["a", "a"]})
        self.state.close()
        self.state = State(self.directory.name)
        self.assertEqual(self.state.cursor("False")["tail_digests"], ["a", "a"])

    def test_cleanup_does_not_follow_symlinks(self):
        directory = Path(self.directory.name) / "screenshots"
        directory.mkdir()
        external = Path(self.directory.name) / "user-file"
        external.write_text("preserve")
        (directory / "link").symlink_to(external)
        self.state.cleanup(0)
        self.assertTrue(external.exists())
        self.assertTrue((directory / "link").is_symlink())

    def test_account_profile_persists_and_daily_refresh_respects_two_hour_floor(self):
        self.assertTrue(self.state.account_refresh_due(now=1_000))
        profile = self.state.account_store({"display_name": "uu", "wechat_id": "uu_TwT",
                                            "avatar_path": "/tmp/avatar.png"}, now=1_000)
        self.assertEqual(profile["display_name"], "uu")
        self.assertFalse(self.state.account_refresh_due(now=1_000 + 86_399))
        self.assertTrue(self.state.account_refresh_due(now=1_000 + 86_400))

    def test_failed_account_attempt_waits_two_hours_before_another_try(self):
        self.state.account_record_attempt(now=1_000)
        self.assertFalse(self.state.account_refresh_due(now=8_199))
        self.assertTrue(self.state.account_refresh_due(now=8_200))


class DemoTaskStoreTests(unittest.TestCase):
    def test_queue_generates_key_and_links_duplicates_to_root_task(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "demo.sqlite3", {"message.send"})
            try:
                first, fresh = store.enqueue({"method": "message.send",
                                              "params": {"chat": "False", "text": "hello"}})
                second, fresh_second = store.enqueue({"method": "message.send",
                                                       "params": {"chat": "False", "text": "hello"}})
                third, fresh_third = store.enqueue({"method": "message.send",
                                                     "params": {"chat": "False", "text": "hello"}})
                self.assertTrue(fresh)
                self.assertFalse(fresh_second)
                self.assertFalse(fresh_third)
                self.assertTrue(first["request"]["idempotency_key"].startswith("demo-"))
                self.assertEqual(second["duplicate_of"], first["id"])
                self.assertEqual(third["duplicate_of"], first["id"])
            finally:
                store.close()

    def test_claim_only_starts_the_requested_queued_task(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "demo.sqlite3")
            try:
                first, _ = store.enqueue({"method": "session.status", "params": {}})
                second, _ = store.enqueue({"method": "doctor", "params": {}})
                self.assertEqual(store.claim(second["id"])["id"], second["id"])
                self.assertEqual(store.get(first["id"])["status"], "queued")
                self.assertEqual(store.get(second["id"])["status"], "running")
            finally:
                store.close()

    def test_confirmation_reuses_prior_task_idempotency_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "demo.sqlite3", {"contact.delete"})
            try:
                first, _ = store.enqueue({"method": "contact.delete", "params": {"chat": "False"}})
                token = "local-confirmation-token"
                store.complete(first["id"], {"ok": False, "error": {"details": {"confirm_token": token}}})
                second, fresh = store.enqueue({"method": "contact.delete", "params": {"chat": "False"},
                                               "confirm_token": token})
                self.assertTrue(fresh)
                self.assertEqual(second["request"]["idempotency_key"], first["request"]["idempotency_key"])
            finally:
                store.close()

    def test_duplicate_window_expires_after_fifteen_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            current_time = [1_000.0]
            store = TaskStore(Path(directory) / "demo.sqlite3", {"message.send"},
                              clock=lambda: current_time[0])
            try:
                first, first_fresh = store.enqueue({"method": "message.send",
                                                    "params": {"chat": "False", "text": "hello"}})
                current_time[0] = 1_014.9
                duplicate, duplicate_fresh = store.enqueue({"method": "message.send",
                                                            "params": {"chat": "False", "text": "hello"}})
                current_time[0] = 1_015.1
                later, later_fresh = store.enqueue({"method": "message.send",
                                                    "params": {"chat": "False", "text": "hello"}})
                self.assertTrue(first_fresh)
                self.assertFalse(duplicate_fresh)
                self.assertEqual(duplicate["duplicate_of"], first["id"])
                self.assertTrue(later_fresh)
                self.assertIsNone(later["duplicate_of"])
            finally:
                store.close()


class DemoTaskRunnerTests(unittest.TestCase):
    def runner(self, directory):
        current_time = [1_000.0]
        requests = []

        def execute(request):
            requests.append(request)
            return {"protocol_version": 1, "id": request["id"], "ok": True,
                    "result": {"method": request["method"]}, "elapsed_ms": 0}

        clock = lambda: current_time[0]
        store = TaskStore(Path(directory) / "demo.sqlite3", {"message.send"}, clock=clock)
        return store, TaskRunner(store, execute, clock=clock), current_time, requests

    def test_reuses_same_chat_then_resets_after_three_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            store, runner, current_time, requests = self.runner(directory)
            try:
                first, _ = store.enqueue({"method": "message.send",
                                          "params": {"chat": "False", "text": "one"}})
                runner.run_task(first["id"])
                self.assertEqual([request["method"] for request in requests],
                                 ["chat.open", "message.send"])
                self.assertEqual(store.get(first["id"])["phases"]["reset"]["status"], "stalled")

                current_time[0] = 1_010.0
                second, _ = store.enqueue({"method": "message.send",
                                           "params": {"chat": "False", "text": "two"}})
                runner.run_task(second["id"])
                self.assertEqual([request["method"] for request in requests],
                                 ["chat.open", "message.send", "message.send"])
                self.assertEqual(store.get(second["id"])["phases"]["enter"]["status"], "skipped")
                self.assertEqual(store.get(first["id"])["phases"]["reset"]["status"], "superseded")

                current_time[0] = 1_190.1
                runner.reset_due()
                self.assertEqual(requests[-1]["method"], "ui.reset")
                self.assertEqual(store.get(second["id"])["phases"]["reset"]["status"], "succeeded")
                self.assertIsNone(store.active_context())
            finally:
                store.close()

    def test_switching_chat_resets_before_entering_new_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store, runner, current_time, requests = self.runner(directory)
            try:
                first, _ = store.enqueue({"method": "message.send",
                                          "params": {"chat": "False", "text": "one"}})
                runner.run_task(first["id"])
                current_time[0] = 1_010.0
                second, _ = store.enqueue({"method": "message.send",
                                           "params": {"chat": "yuo", "text": "two"}})
                runner.run_task(second["id"])
                self.assertEqual([request["method"] for request in requests],
                                 ["chat.open", "message.send", "ui.reset", "chat.open", "message.send"])
                self.assertEqual(store.get(first["id"])["phases"]["reset"]["status"], "succeeded")
                self.assertEqual(store.get(second["id"])["phases"]["enter"]["status"], "succeeded")
            finally:
                store.close()


class ProtocolTests(StateTests):
    def test_pat_methods_require_idempotency_keys(self):
        automation = Mock()
        dispatcher = Dispatcher(automation, self.state)
        for name, params in (("message.pat", {"chat": "False"}),
                             ("message.pat_revoke", {"chat": "False"})):
            with self.subTest(name=name):
                response = dispatcher.dispatch({"method": name, "params": params})
                self.assertEqual(response["error"]["code"], "IDEMPOTENCY_REQUIRED")

    def test_destructive_action_requires_two_distinct_calls(self):
        automation = Mock()
        automation.message_delete.return_value = {"status": "deleted"}
        dispatcher = Dispatcher(automation, self.state)
        request = {"id": "first", "method": "message.delete", "idempotency_key": "delete-1",
                   "params": {"chat": "False", "text": "one"}}
        challenge = dispatcher.dispatch(request)
        self.assertEqual(challenge["error"]["code"], "CONFIRMATION_REQUIRED")
        token = challenge["error"]["details"]["confirm_token"]
        automation.message_delete.assert_not_called()
        wrong = dispatcher.dispatch({**request, "id": "wrong", "params": {"chat": "False", "text": "two"},
                                     "confirm_token": token})
        self.assertEqual(wrong["error"]["code"], "CONFIRMATION_INVALID")
        accepted = dispatcher.dispatch({**request, "id": "confirmed", "confirm_token": token})
        self.assertTrue(accepted["ok"])
        automation.message_delete.assert_called_once_with(chat="False", text="one")
        replay = dispatcher.dispatch(request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], "first")
        automation.message_delete.assert_called_once()

    def test_confirmation_token_cannot_be_reused_with_new_key(self):
        automation = Mock()
        dispatcher = Dispatcher(automation, self.state)
        request = {"method": "message.delete", "idempotency_key": "a",
                   "params": {"chat": "False", "text": "one"}}
        challenge = dispatcher.dispatch(request)
        token = challenge["error"]["details"]["confirm_token"]
        result = dispatcher.dispatch({**request, "idempotency_key": "b", "confirm_token": token})
        self.assertEqual(result["error"]["code"], "CONFIRMATION_INVALID")
        automation.message_delete.assert_not_called()

    def test_send_requires_idempotency_key(self):
        automation = Mock()
        response = Dispatcher(automation, self.state).dispatch(
            {"method": "message.send", "params": {"chat": "False", "text": "test"}})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "IDEMPOTENCY_REQUIRED")
        automation.message_send.assert_not_called()

    def test_cached_send_is_not_sent_again(self):
        automation = Mock()
        automation.message_send.return_value = {"status": "submitted"}
        dispatcher = Dispatcher(automation, self.state)
        request = {"id": "first", "method": "message.send", "idempotency_key": "key",
                   "params": {"chat": "False", "text": "hello"}}
        first = dispatcher.dispatch(request)
        second = dispatcher.dispatch({**request, "id": "second"})
        self.assertTrue(first["ok"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["id"], "second")
        self.assertEqual(automation.message_send.call_count, 1)

    def test_file_and_favorite_require_idempotency_keys(self):
        automation = Mock()
        dispatcher = Dispatcher(automation, self.state)
        for name, params in (("message.send_file", {"chat": "False", "path": "/tmp/sample.txt"}),
                             ("message.download", {"chat": "False", "visual_digest": "0" * 32,
                                                   "path": "/tmp/out"}),
                             ("favorite.add", {"chat": "False", "text": "one"})):
            with self.subTest(name=name):
                response = dispatcher.dispatch({"method": name, "params": params})
                self.assertEqual(response["error"]["code"], "IDEMPOTENCY_REQUIRED")
        automation.message_send_file.assert_not_called()
        automation.message_download.assert_not_called()
        automation.favorite_add.assert_not_called()

    def test_new_mutations_require_idempotency_keys(self):
        automation = Mock()
        dispatcher = Dispatcher(automation, self.state)
        for name, params in (("contact.add", {"identifier": "not-a-user"}),
                             ("contact.accept", {"requester": "requester"}),
                             ("group.rename", {"chat": "group (3)", "name": "renamed"}),
                             ("group.announcement_set", {"chat": "group (3)", "text": "notice"}),
                             ("group.create", {"contacts": ["False", "other"]}),
                             ("group.invite", {"chat": "group (3)", "contacts": ["False"]}),
                             ("transfer.accept", {"chat": "False", "text": "transfer"})):
            with self.subTest(name=name):
                response = dispatcher.dispatch({"method": name, "params": params})
                self.assertEqual(response["error"]["code"], "IDEMPOTENCY_REQUIRED")

    def test_failed_file_send_is_not_automatically_retried(self):
        automation = Mock()
        automation.message_send_file.side_effect = AutomationError("OUTCOME_UNKNOWN", "Inspect the draft")
        dispatcher = Dispatcher(automation, self.state)
        request = {"method": "message.send_file", "idempotency_key": "file-one",
                   "params": {"chat": "False", "path": "/tmp/sample.txt"}}
        first = dispatcher.dispatch(request)
        replay = dispatcher.dispatch(request)
        self.assertEqual(first["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertTrue(replay["replayed"])
        automation.message_send_file.assert_called_once()

    def test_group_remove_never_executes_without_confirmation(self):
        automation = Mock()
        response = Dispatcher(automation, self.state).dispatch({
            "method": "group.remove", "params": {"chat": "test group (3)", "member": "False"},
            "idempotency_key": "remove-test"})
        self.assertEqual(response["error"]["code"], "CONFIRMATION_REQUIRED")
        automation.group_remove.assert_not_called()

    def test_leave_group_never_executes_without_confirmation(self):
        automation = Mock()
        response = Dispatcher(automation, self.state).dispatch({
            "method": "group.leave", "params": {"chat": "程序定制接单群"},
            "idempotency_key": "leave-test"})
        self.assertEqual(response["error"]["code"], "CONFIRMATION_REQUIRED")
        automation.group_leave.assert_not_called()

    def test_favorite_delete_never_executes_without_confirmation(self):
        automation = Mock()
        response = Dispatcher(automation, self.state).dispatch({
            "method": "favorite.delete", "params": {"text": "test favorite"},
            "idempotency_key": "favorite-delete-test"})
        self.assertEqual(response["error"]["code"], "CONFIRMATION_REQUIRED")
        automation.favorite_delete.assert_not_called()


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.call = Mock(return_value={"protocol_version": 1, "ok": True,
                                       "result": {"status": "ready"}})
        self.server = MCPServer(self.call)

    def test_initialize_and_tools_list_follow_mcp_shape(self):
        initialized = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                          "params": {"protocolVersion": "2025-03-26"}})
        listed = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]},
                         {"wechat_capabilities", "wechat_call"})

    def test_call_forwards_existing_protocol_request(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": "call", "method": "tools/call",
                                       "params": {"name": "wechat_call", "arguments": {
                                           "method": "message.read", "params": {"chat": "False"}}}})

        request = self.call.call_args.args[0]
        self.assertEqual(request["method"], "message.read")
        self.assertEqual(request["params"], {"chat": "False"})
        self.assertTrue(response["result"]["content"][0]["text"].startswith("{"))
        self.assertFalse(response["result"]["isError"])

    def test_call_returns_protocol_errors_as_tool_errors(self):
        self.call.return_value = {"protocol_version": 1, "ok": False,
                                  "error": {"code": "IDEMPOTENCY_REQUIRED"}}
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "wechat_call", "arguments": {
                                           "method": "message.send", "params": {"chat": "False", "text": "one"}}}})

        self.assertTrue(response["result"]["isError"])

    def test_invalid_tool_arguments_return_tool_error_without_calling_service(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "wechat_call", "arguments": {"params": {}}}})

        self.assertTrue(response["result"]["isError"])
        self.call.assert_not_called()


class VisionTests(unittest.TestCase):
    def test_avatar_template_match_finds_embedded_avatar(self):
        avatar = Image.new("RGB", (8, 8), (40, 80, 120))
        avatar.putpixel((3, 3), (255, 0, 0))
        image = Image.new("RGB", (50, 20), "white")
        image.paste(avatar, (28, 6))
        score, rect = avatar_template_match(image, avatar, size=8, stride=1)
        self.assertEqual(rect, (28, 6, 8, 8))
        self.assertEqual(score, 0)

    def test_chinese_ocr_spacing_is_normalized_without_joining_english_words(self):
        self.assertEqual(text_identity("程 序 定 制 接 单 群 (75)"), "程序定制接单群 (75)")
        self.assertEqual(text_identity("hello world"), "hello world")
    def test_adjacent_transcript_ignores_next_separate_message(self):
        voice = {"direction": "incoming", "rect": [10, 50, 100, 40]}
        caption = {"direction": "incoming", "rect": [9, 92, 240, 35]}
        separate = {"direction": "incoming", "rect": [10, 150, 100, 40]}
        self.assertIs(adjacent_bubble([voice, caption, separate], voice), caption)
        self.assertIsNone(adjacent_bubble([voice, separate], voice))

    def test_page_overlap_preserves_repeated_real_messages(self):
        older = [{"visual_digest": item} for item in ("same", "same", "middle")]
        newer = [{"visual_digest": item} for item in ("same", "middle", "new")]
        merged, added = merge_message_pages(older, newer)
        self.assertEqual([item["visual_digest"] for item in merged],
                         ["same", "same", "middle", "new"])
        self.assertEqual(added, 1)

    def test_identical_page_does_not_advance(self):
        page = [{"visual_digest": "a"}, {"visual_digest": "b"}]
        merged, added = merge_message_pages(page, page)
        self.assertEqual(added, 0)
        self.assertEqual(merged, page)

    def test_runs(self):
        self.assertEqual(runs([False, True, True, False, True]), [(1, 3), (4, 5)])

    def test_repeated_bubbles_not_collapsed(self):
        pixels = np.full((300, 1200, 3), 237, dtype=np.uint8)
        pixels[30:65, 75:220] = 255
        pixels[90:125, 75:220] = 255
        pixels[150:185, 1000:1125] = [149, 236, 105]
        bubbles = bubble_regions(Image.fromarray(pixels))
        self.assertEqual(len(bubbles), 3)
        self.assertEqual([item["direction"] for item in bubbles], ["incoming", "incoming", "outgoing"])

    def test_text_at_old_scan_column_does_not_split_bubble(self):
        pixels = np.full((200, 1200, 3), 237, dtype=np.uint8)
        pixels[30:65, 850:1125] = [149, 236, 105]
        pixels[40:55, 865:1105] = 20
        bubbles = bubble_regions(Image.fromarray(pixels))
        self.assertEqual(len(bubbles), 1)
        self.assertGreater(bubbles[0]["rect"][2], 270)


class ClipboardTests(unittest.TestCase):
    def test_selected_text_accepts_empty_only_with_positive_empty_check(self):
        from wechat_cli.x11 import Desktop
        desktop = Desktop.__new__(Desktop)
        desktop.connection = Mock()
        desktop.key = Mock()
        desktop.wake = Mock()
        desktop.claim_clipboard = Mock(return_value=100)
        desktop.selection_owner_id = Mock(return_value=100)
        check = Mock(return_value=True)
        with patch("wechat_cli.x11.subprocess.run") as read:
            self.assertEqual(desktop.selected_text(empty_check=check), "")
        check.assert_called_once()
        read.assert_not_called()

    def test_repeated_selected_text_claims_fresh_clipboard_without_pasting_marker(self):
        from wechat_cli.x11 import Desktop
        desktop = Desktop.__new__(Desktop)
        desktop.connection = Mock()
        desktop.connection.intern_atom.return_value = 1
        desktop.display_name = ":99"
        desktop.key = Mock()
        desktop.wake = Mock()
        desktop.claim_clipboard = Mock(side_effect=[100, 101])
        desktop.selection_owner_id = Mock(return_value=200)
        copied = SimpleNamespace(returncode=0, stdout=b"existing draft")
        with patch("wechat_cli.x11.subprocess.run", return_value=copied):
            self.assertEqual(desktop.selected_text(), "existing draft")
            self.assertEqual(desktop.selected_text(), "existing draft")
        markers = [called.args[0] for called in desktop.claim_clipboard.call_args_list]
        self.assertNotEqual(markers[0], markers[1])
        self.assertEqual([called.args[0] for called in desktop.key.call_args_list],
                         ["Control_L+a", "Control_L+c", "Control_L+a", "Control_L+c"])

    def test_selected_text_does_not_read_stale_clipboard_on_copy_timeout(self):
        from wechat_cli.x11 import Desktop
        desktop = Desktop.__new__(Desktop)
        desktop.connection = Mock()
        desktop.key = Mock()
        desktop.wake = Mock()
        desktop.claim_clipboard = Mock(return_value=100)
        desktop.selection_owner_id = Mock(return_value=100)
        with patch("wechat_cli.x11.wait_until", side_effect=AutomationError("TIMEOUT", "copy timeout")), \
             patch("wechat_cli.x11.subprocess.run") as read:
            with self.assertRaises(AutomationError):
                desktop.selected_text()
        read.assert_not_called()

    def test_selected_text_rejects_unchanged_marker_even_if_owner_changes(self):
        from wechat_cli.x11 import Desktop
        desktop = Desktop.__new__(Desktop)
        desktop.connection = Mock()
        desktop.display_name = ":99"
        desktop.key = Mock()
        desktop.wake = Mock()
        desktop.claim_clipboard = Mock(return_value=100)
        desktop.selection_owner_id = Mock(return_value=200)
        copied = SimpleNamespace(returncode=0, stdout=b"wechat-cli-copy-fixed-marker")
        with patch("wechat_cli.x11.uuid.uuid4", return_value="fixed-marker"), \
             patch("wechat_cli.x11.subprocess.run", return_value=copied):
            with self.assertRaises(AutomationError) as error:
                desktop.selected_text()
        self.assertEqual(error.exception.code, "CLIPBOARD_MISMATCH")

    def test_no_selection_owner_is_integer_zero(self):
        from wechat_cli.x11 import Desktop
        desktop = Desktop.__new__(Desktop)
        desktop.connection = Mock()
        desktop.connection.get_selection_owner.return_value = 0
        self.assertEqual(desktop.selection_owner_id(1), 0)

    def test_selection_owner_is_window(self):
        from wechat_cli.x11 import Desktop
        desktop = Desktop.__new__(Desktop)
        desktop.connection = Mock()
        desktop.connection.get_selection_owner.return_value = Mock(id=123)
        self.assertEqual(desktop.selection_owner_id(1), 123)


class OCRTests(unittest.TestCase):
    def test_best_profile_uses_its_data_dir_and_lstm_engine(self):
        from wechat_cli.ocr import OCR
        reader = OCR(tessdata_dir="/models/best", engine_mode=1, profile="best")
        response = SimpleNamespace(
            returncode=0,
            stdout=b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n",
            stderr=b"",
        )
        with patch("wechat_cli.ocr.subprocess.run", return_value=response) as run:
            reader.words(Image.new("RGB", (4, 4), "white"))
        command = run.call_args.args[0]
        self.assertIn("--tessdata-dir", command)
        self.assertIn("/models/best", command)
        self.assertEqual(command[command.index("--oem") + 1], "1")

    def test_model_availability_requires_every_language(self):
        from wechat_cli.ocr import OCR
        with tempfile.TemporaryDirectory() as directory:
            reader = OCR(tessdata_dir=directory)
            self.assertFalse(reader.model_available)
            for language in ("chi_sim", "eng"):
                (Path(directory) / f"{language}.traineddata").touch()
            self.assertTrue(reader.model_available)

    def test_scaled_lines_restore_source_coordinates(self):
        from wechat_cli.ocr import OCR
        reader = OCR()
        response = SimpleNamespace(
            returncode=0,
            stdout=(b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                    b"5\t1\t1\t1\t1\t1\t8\t12\t20\t10\t96\ttest\n"),
            stderr=b"",
        )
        with patch("wechat_cli.ocr.subprocess.run", return_value=response):
            rows = reader.lines(Image.new("RGB", (40, 30), "white"), scale=2)
        self.assertEqual(rows, [{"text": "test", "rect": [4, 6, 10, 5]}])

    def test_content_reader_prefers_rapidocr_and_falls_back_to_complete_best_models(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            config = SimpleNamespace(quality_tessdata_dir=data)
            with patch("wechat_cli.automation.RapidOCRReader") as rapid:
                rapid.return_value.model_available = True
                automation = Automation(config, Mock())
            self.assertIs(automation.content_ocr, rapid.return_value)
            with patch("wechat_cli.automation.RapidOCRReader") as rapid:
                rapid.return_value.model_available = False
                automation = Automation(config, Mock())
            self.assertEqual(automation.content_ocr.profile, "standard_fallback")
            for language in ("chi_sim", "chi_sim_vert", "eng"):
                (data / f"{language}.traineddata").touch()
            (data / "configs").mkdir()
            (data / "configs" / "tsv").touch()
            with patch("wechat_cli.automation.RapidOCRReader") as rapid:
                rapid.return_value.model_available = False
                automation = Automation(config, Mock())
            self.assertEqual(automation.content_ocr.profile, "best")

    def test_rapid_reader_converts_quadrilateral_to_source_rect(self):
        from wechat_cli.ocr import RapidOCRReader
        reader = RapidOCRReader()
        reader.factory = Mock()
        reader.engine = Mock(return_value=([
            [[[8, 12], [30, 12], [30, 24], [8, 24]], "长得像一位故人", 0.99],
            [[[2, 2], [4, 2], [4, 4], [2, 4]], "噪声", 0.2],
        ], [0.1, 0.2, 0.3]))
        rows = reader.lines(Image.new("RGB", (40, 30), "white"), scale=2)
        self.assertEqual(rows, [{"text": "长得像一位故人", "rect": [4, 6, 11, 6]}])

    def test_visible_messages_uses_single_line_segmentation_for_short_bubbles(self):
        automation = Automation.__new__(Automation)
        automation.content_reader = Mock()
        reader = automation.content_reader.return_value
        reader.profile = "best"
        reader.lines.return_value = []
        automation.connect = Mock()
        automation.connect.return_value.capture.return_value = Image.new("RGB", (600, 300), "white")
        main = SimpleNamespace(rect=Rect(0, 0, 1000, 700))
        with patch("wechat_cli.automation.bubble_regions", return_value=[{
                "direction": "outgoing", "rect": [200, 80, 160, 40]}]):
            automation.visible_messages(main)
        self.assertEqual(reader.lines.call_args.kwargs["psm"], 7)
        self.assertEqual(reader.lines.call_args.kwargs["scale"], 3)

    def test_visible_messages_adds_context_for_rapidocr(self):
        automation = Automation.__new__(Automation)
        automation.content_reader = Mock()
        reader = automation.content_reader.return_value
        reader.profile = "rapidocr"
        reader.lines.return_value = []
        desktop = Mock()
        desktop.capture.return_value = Image.new("RGB", (600, 300), "white")
        automation.connect = Mock(return_value=desktop)
        main = SimpleNamespace(rect=Rect(0, 0, 1000, 700))
        with patch("wechat_cli.automation.bubble_regions", return_value=[{
                "direction": "incoming", "rect": [200, 80, 160, 40]}]):
            automation.visible_messages(main)
        self.assertEqual(reader.lines.call_args.args[0].size, (196, 76))


class DeploymentTests(unittest.TestCase):
    def test_dead_display_is_reported_unavailable(self):
        from Xlib.error import DisplayConnectionError
        with patch("Xlib.display.Display", side_effect=DisplayConnectionError("x", "dead")):
            self.assertFalse(display_ready(":99"))

    def test_screen_dimensions_are_configurable(self):
        environment = {"WECHAT_WIDTH": "2560", "WECHAT_HEIGHT": "1440",
                       "WECHAT_TIMEOUT": "2.5", "WECHAT_RETENTION_DAYS": "7"}
        with patch.dict("os.environ", environment, clear=True):
            config = Config.from_env()
        self.assertEqual((config.width, config.height), (2560, 1440))
        self.assertEqual(config.timeout, 2.5)
        self.assertEqual(config.retention_days, 7)

    def test_invalid_screen_dimensions_are_rejected(self):
        with patch.dict("os.environ", {"WECHAT_WIDTH": "100"}, clear=True), self.assertRaises(AutomationError) as error:
            Config.from_env()
        self.assertEqual(error.exception.code, "INVALID_CONFIG")

    def test_vnc_launch_drops_wayland_environment(self):
        with patch("wechat_cli.deployment.os.environ", {"WAYLAND_DISPLAY": "wayland-0"}), \
             patch("wechat_cli.deployment.subprocess.Popen") as spawned:
            launch(["x11vnc", "-display", ":99"], ":99")
        environment = spawned.call_args.kwargs["env"]
        self.assertNotIn("WAYLAND_DISPLAY", environment)
        self.assertEqual(environment["DISPLAY"], ":99")

    def test_refuses_symlink_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "vnc.secret").symlink_to(state_dir / "outside")
            config = Mock(state_dir=state_dir)
            with patch("wechat_cli.deployment.shutil.which", return_value="/usr/bin/x11vnc"):
                with self.assertRaises(AutomationError) as context:
                    start_remote(config)
            self.assertEqual(context.exception.code, "UNSAFE_CREDENTIAL")

    def test_refuses_unrelated_remote_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "vnc.secret").write_text("testpass")
            (state_dir / "vnc.passwd").write_bytes(b"existing")
            config = Mock(state_dir=state_dir, display=":99")
            with patch("wechat_cli.deployment.shutil.which", return_value="/usr/bin/x11vnc"), \
                 patch("wechat_cli.deployment.local_port_ready", return_value=True), \
                 patch("wechat_cli.deployment.remote_process_running", return_value=False):
                with self.assertRaises(AutomationError) as context:
                    start_remote(config)
            self.assertEqual(context.exception.code, "VNC_PORT_IN_USE")
            self.assertEqual((state_dir / "vnc.secret").stat().st_mode & 0o777, 0o600)

    def test_gui_disable_only_stops_its_matching_vnc_process(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            password_file = state_dir / "vnc.passwd"
            password_file.write_bytes(b"existing")
            config = Mock(state_dir=state_dir, display=":99")
            with patch("wechat_cli.deployment.remote_process_ids", return_value=[1234]), \
                 patch("wechat_cli.deployment.local_port_ready", side_effect=[True, False]), \
                 patch("wechat_cli.deployment.os.kill") as terminate:
                result = stop_remote(config)
        self.assertEqual(result["status"], "disabled")
        terminate.assert_called_once()

    def test_gui_disable_refuses_an_unowned_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Mock(state_dir=Path(directory), display=":99")
            with patch("wechat_cli.deployment.local_port_ready", return_value=True):
                with self.assertRaises(AutomationError) as context:
                    stop_remote(config)
        self.assertEqual(context.exception.code, "VNC_PORT_IN_USE")

    @unittest.skipUnless(__import__("shutil").which("x11vnc"), "x11vnc not installed")
    def test_vnc_password_file_created_with_pty(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "vnc.passwd"
            create_vnc_password("testpass", destination)
            self.assertTrue(destination.exists())


if __name__ == "__main__":
    unittest.main()

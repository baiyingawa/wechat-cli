import io
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from wechat_cli.automation import Automation
from wechat_cli.console import main, parse_shortcut, print_error, print_help, print_json, qr_terminal_art, styled
from wechat_cli.registry import capabilities


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class ConsoleStyleTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("NO_COLOR", None)
        os.environ["TERM"] = "xterm-256color"

    def test_terminal_styles_are_bold_and_reset(self):
        self.assertEqual(styled("wechatcli> ", "command", TerminalOutput()),
                         "\033[1;36mwechatcli> \033[0m")

    def test_redirected_output_has_no_escape_codes(self):
        self.assertEqual(styled("plain", "error", io.StringIO()), "plain")

    def test_no_color_and_dumb_terminal_disable_styles(self):
        with patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertEqual(styled("plain", "heading", TerminalOutput()), "plain")
        with patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertEqual(styled("plain", "heading", TerminalOutput()), "plain")

    def test_json_styles_preserve_escaped_strings_and_values(self):
        response = {"ok": False, "error": {"code": "TIMEOUT", "message": '中文 "true" \\ 123'},
                    "elapsed_ms": -12.5, "result": None}
        terminal = TerminalOutput()
        with patch("sys.stdout", terminal):
            print_json(response)
        output = terminal.getvalue()
        self.assertIn("\033[1;31mfalse\033[0m", output)
        plain = re.sub(r"\033\[[0-9;]*m", "", output)
        self.assertEqual(json.loads(plain), response)
        self.assertEqual(plain, json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        redirected = io.StringIO()
        with patch("sys.stdout", redirected):
            print_json(response)
        self.assertEqual(redirected.getvalue(), plain)

    def test_help_and_errors_use_their_own_output_stream(self):
        terminal = TerminalOutput()
        redirected = io.StringIO()
        with patch("sys.stdout", terminal), patch("sys.stderr", redirected):
            print_help()
            print_error("Error: example")
        self.assertIn("\033[1;36m/chat CHAT TEXT\033[0m", terminal.getvalue())
        self.assertEqual(redirected.getvalue(), "Error: example\n")
        with patch("sys.stderr", terminal):
            print_error("Error: example")
        self.assertIn("\033[1;31mError: example\033[0m", terminal.getvalue())

    def test_console_startup_and_prompt_are_styled(self):
        terminal = TerminalOutput()
        with patch("sys.stdout", terminal), patch("builtins.input", return_value="/exit") as read_input:
            main()
        read_input.assert_called_once_with("\033[1;36mwechatcli> \033[0m")
        self.assertIn("\033[1;36mwechatcli console\033[0m", terminal.getvalue())


class ConsoleShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.methods = {item["method"]: item for item in capabilities()["methods"]
                       if item["status"] == "implemented"}

    def test_chat_shortcut_sends_plain_text(self):
        request = parse_shortcut("/chat", ["False", "hello", "world"], self.methods)
        self.assertEqual(request["method"], "message.send")
        self.assertEqual(request["params"], {"chat": "False", "text": "hello world"})
        self.assertTrue(request["idempotency_key"].startswith("console-"))

    def test_login_shortcut_starts_manual_login(self):
        request = parse_shortcut("/login", [], self.methods)
        self.assertEqual(request["method"], "session.login")
        self.assertEqual(request["params"], {})

    def test_target_shortcut_switches_live_target(self):
        self.assertEqual(parse_shortcut("/target", ["local"], self.methods)["params"], {"target": "local"})
        self.assertEqual(parse_shortcut("/target", ["woc", "woc-wx-test"], self.methods)["params"],
                         {"target": "woc", "container": "woc-wx-test"})
        self.assertEqual(parse_shortcut("/target", [], self.methods)["method"], "target.status")

    def test_read_shortcut_keeps_optional_limit_numeric(self):
        request = parse_shortcut("/read", ["False", "30"], self.methods)
        self.assertEqual(request["method"], "message.read")
        self.assertEqual(request["params"], {"chat": "False", "limit": 30})

    def test_read_shortcut_rejects_limit_outside_visible_history_bound(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 30"):
            parse_shortcut("/read", ["False", "31"], self.methods)

    def test_like_shortcut_preserves_multi_word_post_text(self):
        request = parse_shortcut("/like", ["False", "post", "text"], self.methods)
        self.assertEqual(request["method"], "moments.like")
        self.assertEqual(request["params"], {"chat": "False", "post_text": "post text"})

    def test_feed_shortcut_opens_global_moments(self):
        request = parse_shortcut("/feed", [], self.methods)
        self.assertEqual(request["method"], "moments.feed.open")

    def test_terminal_qr_art_uses_block_characters(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as file:
            image = Image.new("L", (2, 2), 255)
            image.putpixel((0, 0), 0)
            image.save(file.name)
            art = qr_terminal_art(file.name, columns=2)
        self.assertEqual(art.splitlines()[0], "██  ")
        self.assertEqual(art.splitlines()[1], "    ")

    def test_qr_points_become_a_padded_crop(self):
        rect = Automation.qr_rect_from_points(
            [[[100, 120], [300, 120], [300, 320], [100, 320]]], 600, 500)
        self.assertEqual(rect.as_list(), [76, 96, 248, 248])


if __name__ == "__main__":
    unittest.main()

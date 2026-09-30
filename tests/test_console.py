import tempfile
import unittest

from PIL import Image

from wechat_cli.automation import Automation
from wechat_cli.console import parse_shortcut, qr_terminal_art
from wechat_cli.registry import capabilities


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

    def test_gui_shortcut_accepts_on_and_off(self):
        self.assertTrue(parse_shortcut("/gui", ["on"], self.methods)["params"]["enabled"])
        self.assertFalse(parse_shortcut("/gui", ["off"], self.methods)["params"]["enabled"])

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

import unittest

from wechat_cli.console import parse_shortcut
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


if __name__ == "__main__":
    unittest.main()

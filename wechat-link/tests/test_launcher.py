import json
import unittest
from unittest.mock import patch

from wechat_link import Target, cli_command, run_remote


class LauncherTests(unittest.TestCase):
    def test_wsl_command_preserves_chinese_and_shell_characters(self):
        text = "你好 ' $() ` echo"
        command = cli_command(Target("test", project_dir="/tmp/project with space"), "ui.paste_text", {"text": text})
        self.assertEqual(command[:6], ["wsl.exe", "-d", "Ubuntu-24.04", "--", "bash", "-lc"])
        import shlex
        tokens = shlex.split(command[-1])
        self.assertEqual(json.loads(tokens[-1]), {"text": text})

    def test_ssh_requires_key_authentication_and_rejects_option_injection(self):
        command = cli_command(Target("ssh", kind="ssh", address="user@host"), "session.link", {})
        self.assertIn("BatchMode=yes", command)
        for address in ("-oProxyCommand=bad", "host -p 22", ""):
            with self.subTest(address=address), self.assertRaises(ValueError):
                cli_command(Target("ssh", kind="ssh", address=address), "session.link", {})

    def test_error_does_not_include_credentials_in_exception(self):
        with patch("wechat_link.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = ""
            run.return_value.stdout = '{"ok":false,"error":{"message":"failed"},"password":"secret"}'
            with self.assertRaisesRegex(RuntimeError, "^failed$"):
                run_remote(Target("test"), "session.link", {})


if __name__ == "__main__":
    unittest.main()

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_cli.deployment import start_link, stop_link
from wechat_cli.errors import AutomationError
from wechat_cli.automation import Automation


class LinkDeploymentTests(unittest.TestCase):
    def test_link_uses_one_viewer_loopback_and_fresh_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory), display=":99", link_port=0)
            process = Mock()
            process.poll.return_value = None
            def save_password(password, path):
                path.write_bytes(b"12345678")
            with patch("wechat_cli.deployment.shutil.which", return_value="x11vnc"), \
                 patch("wechat_cli.deployment.find_free_port", return_value=50123), \
                 patch("wechat_cli.deployment.create_vnc_password", side_effect=save_password), \
                 patch("wechat_cli.deployment.launch", return_value=process) as launch, \
                 patch("wechat_cli.deployment.local_port_ready", return_value=True), \
                 patch("wechat_cli.deployment.remote_process_running", return_value=True), \
                 patch("wechat_cli.deployment.threading.Thread"):
                first = start_link(config)
                second = start_link(config)
                command = launch.call_args.args[0]
                self.assertIn("-once", command)
                self.assertIn("-nevershared", command)
                self.assertIn("-localhost", command)
                self.assertIn("127.0.0.1", command)
                self.assertNotEqual(first["link_id"], second["link_id"])
                self.assertEqual(base64.b64decode(first["password_file_base64"]), b"12345678")
                self.assertEqual(first["port"], 50123)
                self.assertEqual((Path(directory) / "links" / f"{first['link_id']}.passwd").stat().st_mode & 0o777, 0o600)

    def test_failed_start_revokes_credentials_and_stops_its_process(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory), display=":99", link_port=0)
            process = Mock()
            process.poll.return_value = None
            with patch("wechat_cli.deployment.shutil.which", return_value="x11vnc"), \
                 patch("wechat_cli.deployment.find_free_port", return_value=50123), \
                 patch("wechat_cli.deployment.create_vnc_password", side_effect=lambda password, path: path.write_bytes(b"12345678")), \
                 patch("wechat_cli.deployment.launch", return_value=process), \
                 patch("wechat_cli.deployment.wait_until", side_effect=AutomationError("TIMEOUT", "start failed")):
                with self.assertRaises(AutomationError):
                    start_link(config)
            process.terminate.assert_called_once()
            self.assertEqual(list((Path(directory) / "links").iterdir()), [])

    def test_close_rejects_path_traversal_and_invalid_identifier(self):
        config = SimpleNamespace(state_dir=Path("/tmp"), display=":99")
        for identifier in ("../../vnc.passwd", "g" * 32, "", "a" * 33):
            with self.subTest(identifier=identifier), self.assertRaises(AutomationError):
                stop_link(config, identifier)


class PasteFocusTests(unittest.TestCase):
    def test_paste_requires_a_visible_wechat_focus(self):
        automation = Automation.__new__(Automation)
        desktop = Mock()
        desktop.root.id = 1
        desktop.connection.get_input_focus.return_value.focus = SimpleNamespace(id=1)
        desktop.windows.return_value = [SimpleNamespace(id=2)]
        automation.connect = Mock(return_value=desktop)
        with self.assertRaises(AutomationError) as error:
            automation.ui_paste_text("中文")
        self.assertEqual(error.exception.code, "FOCUS_UNVERIFIED")
        desktop.paste.assert_not_called()

    def test_paste_into_verified_wechat_focus_never_sends_return(self):
        automation = Automation.__new__(Automation)
        desktop = Mock()
        desktop.connection.get_input_focus.return_value.focus = SimpleNamespace(id=2)
        desktop.windows.return_value = [SimpleNamespace(id=2)]
        automation.connect = Mock(return_value=desktop)
        self.assertEqual(automation.ui_paste_text("中文")["status"], "pasted")
        desktop.paste.assert_called_once_with("中文")
        desktop.key.assert_not_called()

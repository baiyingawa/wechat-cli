import json
import io
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from wechat_cli.config import Config
from wechat_cli.errors import AutomationError
from wechat_cli import deployment, service, woc


class WocTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"XDG_RUNTIME_DIR": directory.name}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = Config(target="woc", woc_container="woc-wx-test", display=":1")

    def test_local_defaults_and_woc_display(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Config.from_env().target, "local")
            self.assertEqual(Config.from_env().display, ":99")
        with patch.dict("os.environ", {"WECHAT_TARGET": "woc"}, clear=True):
            self.assertEqual(Config.from_env().display, ":1")

    def test_container_rejects_options_and_shell_text(self):
        for name in ("", "-bad", "test;id", "a/b", "x\n"):
            with self.subTest(name=name), patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(AutomationError):
                    woc.container_name(Config(woc_container=name))

    def test_worker_environment_and_stop_share_runtime(self):
        with patch("wechat_cli.woc.docker_path", return_value="docker"):
            command = woc._worker_command(self.config, "service", "stop")
        self.assertIn("WECHAT_TARGET=woc", command)
        self.assertIn("WECHAT_WOC_WORKER=1", command)
        self.assertIn("WECHAT_DISPLAY=:1", command)
        self.assertIn("XDG_RUNTIME_DIR=/tmp/wechat-cli-runtime", command)
        self.assertEqual(command[-2:], ["service", "stop"])

    def test_worker_does_not_relay_recursively(self):
        with patch.dict("os.environ", {"WECHAT_WOC_WORKER": "1"}), \
             patch("wechat_cli.service.exchange", return_value={"ok": True}) as exchange, \
             patch("wechat_cli.woc.call") as relay:
            service.call(self.config, {"method": "ping"})
        exchange.assert_called_once()
        relay.assert_not_called()

    def test_local_does_not_use_docker(self):
        with patch("wechat_cli.service.exchange", return_value={"ok": True}), \
             patch("wechat_cli.woc.call") as relay:
            service.call(Config(), {"method": "ping"})
        relay.assert_not_called()

    def test_status_uses_relay(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch("wechat_cli.woc.service_action", return_value={"ok": True}) as relay:
            service.status(self.config)
        relay.assert_called_once_with(self.config, "status")

    def test_inspect_is_docker_inspect(self):
        with patch("wechat_cli.woc.docker_path", return_value="docker"), \
             patch("wechat_cli.woc._run", return_value=b"true\n") as run:
            self.assertTrue(woc.inspect(self.config)["running"])
        self.assertEqual(run.call_args.args[1],
                         ["docker", "inspect", "-f", "{{.State.Running}}", "woc-wx-test"])

    def test_large_response_and_utf8_request(self):
        request = {"method": "chat.open", "params": {"chat": "测试"}}
        response = {"ok": True, "result": "中" * 400000}
        completed = Mock(returncode=0, stdout=json.dumps(response).encode(), stderr=b"")
        with patch("wechat_cli.woc.docker_path", return_value="docker"), \
             patch("wechat_cli.woc.subprocess.run", return_value=completed) as run:
            self.assertEqual(woc.call(self.config, request), response)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), request)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_timeout_does_not_retry(self):
        with patch("wechat_cli.woc.docker_path", return_value="docker"), \
             patch("wechat_cli.woc.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 60)) as run:
            with self.assertRaises(AutomationError) as error:
                woc.call(self.config, {"method": "message.send"})
        self.assertEqual(error.exception.code, "TRANSPORT_UNKNOWN")
        run.assert_called_once()

    def test_woc_never_launches_or_stops_desktop(self):
        with patch("wechat_cli.deployment.display_ready", return_value=True), \
             patch("wechat_cli.deployment.launch") as launch:
            self.assertEqual(deployment.session_start(self.config)["managed_by"], "WechatOnCloud")
        launch.assert_not_called()

    def test_woc_missing_display_does_not_create_xvfb(self):
        with patch("wechat_cli.deployment.display_ready", return_value=False), \
             patch("wechat_cli.deployment.launch") as launch:
            with self.assertRaises(AutomationError) as error:
                deployment.session_start(self.config)
        self.assertEqual(error.exception.code, "WOC_DISPLAY_UNAVAILABLE")
        launch.assert_not_called()

    def test_qr_download_rejects_other_files(self):
        with patch("wechat_cli.woc._run") as run:
            for path in ("/etc/passwd", "/config/.local/state/wechat-cli/screenshots/../secret",
                         "/config/.local/state/wechat-cli/screenshots/login-qr-1.png;id"):
                with self.assertRaises(AutomationError):
                    woc.read_login_qr(self.config, path)
        run.assert_not_called()

    def test_install_packages_source_without_caches_and_restarts_only_worker(self):
        with patch("wechat_cli.woc.inspect", return_value={"running": True}), \
             patch("wechat_cli.woc.docker_path", return_value="docker"), \
             patch("wechat_cli.woc._run", return_value=b"") as run, \
             patch("wechat_cli.woc.service_action", return_value={"ok": True}) as action:
            self.assertTrue(woc.install(self.config)["installed"])
        self.assertEqual(run.call_count, 3)
        archive = run.call_args_list[1].args[2]
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            names = bundle.getnames()
        self.assertIn("pyproject.toml", names)
        self.assertIn("src/wechat_cli/static/index.html", names)
        self.assertFalse(any("__pycache__" in name for name in names))
        self.assertEqual([entry.args[1] for entry in action.call_args_list], ["stop", "start"])

    def test_console_login_reads_qr_through_relay(self):
        from wechat_cli.console import run_login
        from wechat_cli.registry import capabilities
        methods = {item["method"]: item for item in capabilities()["methods"]}
        responses = [{"ok": True, "result": {"state": "login_required", "qr": {
            "path": "/config/.local/state/wechat-cli/screenshots/login-qr-1.png", "digest": "new"}}},
            {"ok": True, "result": {"state": "logged_in"}}]
        with patch.dict("os.environ", {}, clear=True), \
             patch("wechat_cli.console.service.call", side_effect=responses), \
             patch("wechat_cli.console.time.sleep"), patch("sys.stdout", io.StringIO()), \
             patch("wechat_cli.woc.read_login_qr", return_value=b"image") as download, \
             patch("wechat_cli.console.qr_terminal_art", return_value="QR") as render:
            run_login(self.config, methods)
        download.assert_called_once()
        self.assertEqual(render.call_args.args[0].getvalue(), b"image")

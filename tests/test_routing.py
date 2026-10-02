import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_cli import routing, service
from wechat_cli.config import Config
from wechat_cli.registry import capabilities
from wechat_cli.task_queue import TaskStore, TaskRunner


class RoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory.name}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = Config()

    def select_woc(self, container="woc-wx-test"):
        with patch("wechat_cli.woc.call", return_value={"ok": True}):
            return routing.select(self.config, {"target": "woc", "container": container})

    def test_existing_configs_follow_shared_selection_without_restart(self):
        existing = Config()
        self.select_woc()
        with patch("wechat_cli.woc.call", return_value={"ok": True}) as relay:
            service.call(existing, {"method": "ping"})
        self.assertEqual(relay.call_args.args[0].woc_container, "woc-wx-test")
        routing.select(self.config, {"target": "local"})
        with patch("wechat_cli.service.exchange", return_value={"ok": True}) as exchange:
            service.call(existing, {"method": "ping"})
        self.assertEqual(exchange.call_args.args[0].display, ":99")

    def test_failed_switch_keeps_previous_target(self):
        self.select_woc()
        with patch("wechat_cli.woc.call", return_value={"ok": False, "error": {}}):
            response = service.call(self.config, {"method": "target.select", "params": {
                "target": "woc", "container": "woc-wx-unavailable"}})
        self.assertFalse(response["ok"])
        self.assertEqual(routing.resolve(self.config).woc_container, "woc-wx-test")

    def test_target_file_private_and_worker_ignores_host_selection(self):
        self.select_woc()
        self.assertEqual((self.config.runtime_dir / "target.json").stat().st_mode & 0o777, 0o600)
        with patch.dict(os.environ, {"WECHAT_SERVICE_WORKER": "1"}):
            self.assertEqual(routing.resolve(self.config).target, "local")
            response = service.call(self.config, {"method": "target.select", "params": {"target": "local"}})
        self.assertEqual(response["error"]["code"], "HOST_REQUIRED")

    def test_pinned_request_does_not_follow_new_selection(self):
        local = routing.snapshot(self.config)
        self.select_woc()
        with patch("wechat_cli.service.exchange", return_value={"ok": True}) as exchange:
            routing.queued_call(self.config, {"method": "ping", "_route": local})
        self.assertEqual(exchange.call_args.args[0].target, "local")
        self.assertNotIn("_route", exchange.call_args.args[1])

    def test_invalid_target_params_and_request_rejected(self):
        for params in ({"target": "wrong"}, {"target": "woc", "container": "x;id"},
                       {"target": "local", "container": "woc-wx-test"}, {"target": "woc"}):
            response = service.call(self.config, {"method": "target.select", "params": params})
            self.assertFalse(response["ok"])
        response = service.call(self.config, {"method": "target.status", "unknown": 1})
        self.assertEqual(response["error"]["code"], "INVALID_REQUEST")

    def test_vnc_removed_from_capabilities(self):
        names = {method["method"] for method in capabilities()["methods"]}
        self.assertNotIn("session.gui", names)
        self.assertNotIn("session.remote", names)
        self.assertIn("target.select", names)

    def test_queue_separates_targets_and_pins_enter_operate_reset(self):
        store = TaskStore(self.directory / "tasks.sqlite3")
        self.addCleanup(store.close)
        local = routing.snapshot(self.config)
        woc = self.select_woc()
        request = {"method": "message.read", "params": {"chat": "False"}, "_route": local}
        first, _ = store.enqueue(request)
        second, fresh = store.enqueue({**request, "_route": woc})
        self.assertTrue(fresh)
        calls = []

        def execute(request):
            calls.append(request)
            return {"ok": True, "result": {}}

        runner = TaskRunner(store, execute)
        runner.run_task(first["id"])
        runner.run_task(second["id"])
        self.assertEqual([request["_route"]["target"] for request in calls],
                         ["local", "local", "local", "woc", "woc"])
        self.assertEqual([request["method"] for request in calls],
                         ["chat.open", "message.read", "ui.reset", "chat.open", "message.read"])

    def test_legacy_queued_tasks_get_pinned_once(self):
        store = TaskStore(self.directory / "tasks.sqlite3")
        self.addCleanup(store.close)
        task, _ = store.enqueue({"method": "message.read", "params": {"chat": "False"}})
        store.bind_queued(routing.snapshot(self.config))
        store.bind_queued(self.select_woc())
        self.assertEqual(store.get(task["id"])["request"]["_route"]["target"], "local")

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_cli.web import create_server


class DemoSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(state_dir=Path(self.directory.name))
        self.server = create_server(self.config, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        _, headers, _ = self.request("GET", "/")
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_page_establishes_private_session_and_restrictive_csp(self):
        status, headers, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(b"__CSP_NONCE__", page)

    def test_all_api_reads_require_a_session(self):
        for path in ("/api/capabilities", "/api/tasks", "/api/tasks/missing", "/api/target"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 403)
                self.assertEqual(self.request("GET", path, headers={"Cookie": "wechat_demo_token=wrong"})[0], 403)

    def test_external_origin_and_rebinding_host_are_rejected(self):
        for method, path in (("GET", "/"), ("GET", "/api/tasks"), ("POST", "/api/tasks"),
                             ("GET", "/api/target"), ("POST", "/api/target")):
            for extra in ({"Origin": "https://attacker.example"}, {"Origin": "null"},
                          {"Host": f"attacker.example:{self.server.server_port}"},
                          {"Host": "127.0.0.1:1"}, {"Host": "127.0.0.1"}):
                with self.subTest(method=method, headers=extra):
                    headers = {"Cookie": self.cookie, "Content-Type": "application/json", **extra}
                    self.assertEqual(self.request(method, path, '{}', headers)[0], 403)
        self.assertEqual(self.server.store.list(), [])

    def test_csrf_simple_request_never_enters_queue(self):
        body = json.dumps({"method": "message.send", "params": {"chat": "False", "text": "csrf"}})
        self.assertEqual(self.request("POST", "/api/tasks", body, {"Content-Type": "text/plain"})[0], 403)
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded"):
            headers = {"Cookie": self.cookie}
            if content_type:
                headers["Content-Type"] = content_type
            self.assertEqual(self.request("POST", "/api/tasks", body, headers)[0], 415)
        status, headers, _ = self.request("OPTIONS", "/api/tasks", headers={
            "Origin": "https://attacker.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.server.store.list(), [])

    def test_authenticated_same_origin_requests_work(self):
        body = json.dumps({"method": "message.send", "params": {
            "chat": '<img src=x onerror="alert(1)">', "text": "test"}})
        headers = {"Cookie": self.cookie, "Content-Type": "application/json; charset=utf-8",
                   "Origin": f"http://127.0.0.1:{self.server.server_port}"}
        status, _, raw = self.request("POST", "/api/tasks", body, headers)
        self.assertEqual(status, 201)
        task = json.loads(raw)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(self.server.work.get_nowait(), task["id"])
        status, _, raw = self.request("GET", "/api/tasks", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["tasks"][0]["id"], task["id"])

    def test_localhost_authority_and_same_origin_are_allowed(self):
        headers = {"Cookie": self.cookie, "Host": f"localhost:{self.server.server_port}",
                   "Origin": f"http://localhost:{self.server.server_port}"}
        self.assertEqual(self.request("GET", "/api/tasks", headers=headers)[0], 200)
        headers["Origin"] = f"http://localhost:{self.server.server_port + 1}"
        self.assertEqual(self.request("GET", "/api/tasks", headers=headers)[0], 403)

    def test_invalid_method_and_json_do_not_crash_handler(self):
        for body in ('{"method": [], "params": {}}', '{"method": null}',
                     '{"method":"session.status","params":{"value":NaN}}', '{}', 'null'):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/tasks", body, {
                    "Cookie": self.cookie, "Content-Type": "application/json"})[0], 400)

    def test_target_switch_bypasses_queue_but_requires_authenticated_json(self):
        headers = {"Cookie": self.cookie, "Content-Type": "application/json"}
        body = '{"target":"local"}'
        response = {"ok": True, "result": {"target": "local"}}
        with patch("wechat_cli.web.service.call", return_value=response) as execute:
            self.assertEqual(self.request("POST", "/api/target", body)[0], 403)
            self.assertEqual(self.request("POST", "/api/target", body, {"Cookie": self.cookie})[0], 415)
            self.assertEqual(self.request("POST", "/api/target", body, headers)[0], 200)
            execute.assert_called_once_with(self.config, {"method": "target.select", "params": {"target": "local"}})
        self.assertEqual(self.server.store.list(), [])

    def test_client_cannot_inject_queue_target(self):
        self.assertEqual(self.request("POST", "/api/tasks", json.dumps({
            "method": "session.status", "_route": {"target": "woc", "container": "woc-wx-test"}}), {
                "Cookie": self.cookie, "Content-Type": "application/json"})[0], 400)

    def test_token_is_rotated_on_restart(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.server = create_server(self.config, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.assertEqual(self.request("GET", "/api/tasks", headers={"Cookie": self.cookie})[0], 403)


if __name__ == "__main__":
    unittest.main()

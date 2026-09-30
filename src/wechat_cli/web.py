import hmac
import json
import queue
import secrets
import socket
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from . import service
from .errors import AutomationError
from .registry import capabilities
from .task_queue import TaskRunner, TaskStore


COOKIE_NAME = "wechat_demo_token"


class DemoServer(ThreadingHTTPServer):
    daemon_threads = False

    def server_close(self):
        super().server_close()
        self.store.close()


def create_server(config, host, port):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise AutomationError("UNSAFE_BIND", "Demo may bind only to localhost")
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.state_dir.chmod(0o700)
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    page = files("wechat_cli").joinpath("static/index.html").read_text(encoding="utf-8")
    page = page.replace("__CSP_NONCE__", nonce).encode()
    metadata = capabilities()["methods"]
    available_methods = {item["method"] for item in metadata if item["status"] == "implemented"}
    idempotent_methods = {item["method"] for item in metadata if item.get("idempotency_required")}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def respond(self, raw, status=200, content_type="application/json; charset=utf-8", cookie=False):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                             "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            if cookie:
                self.send_header("Set-Cookie", f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(raw)

        def json(self, value, status=200):
            self.respond(json.dumps(value, ensure_ascii=False).encode(), status)

        def authorized(self, require_token=True):
            authorities = {f"127.0.0.1:{self.server.server_port}",
                           f"localhost:{self.server.server_port}", f"[::1]:{self.server.server_port}"}
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if len(hosts) != 1 or hosts[0] not in authorities:
                self.json({"error": "invalid Host"}, 403)
                return False
            if origins and (len(origins) != 1 or origins[0] != f"http://{hosts[0]}"):
                self.json({"error": "external Origin is forbidden"}, 403)
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site" and require_token:
                self.json({"error": "cross-site request is forbidden"}, 403)
                return False
            if require_token:
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                    supplied = cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else ""
                    valid = hmac.compare_digest(supplied, token)
                except (CookieError, ValueError, TypeError):
                    valid = False
                if not valid:
                    self.json({"error": "open the local Demo page to establish a session"}, 403)
                    return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.authorized(require_token=path != "/"):
                return
            if path == "/":
                self.respond(page, content_type="text/html; charset=utf-8", cookie=True)
            elif path == "/api/capabilities":
                self.json(capabilities())
            elif path == "/api/tasks":
                self.json({"tasks": self.server.store.list(),
                           "active_context": self.server.store.active_context()})
            elif path.startswith("/api/tasks/"):
                try:
                    self.json(self.server.store.get(path.rsplit("/", 1)[1]))
                except KeyError:
                    self.json({"error": "not found"}, 404)
            else:
                self.json({"error": "not found"}, 404)

        def do_OPTIONS(self):
            self.json({"error": "cross-origin access is forbidden"}, 403)

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/api/tasks":
                return self.json({"error": "not found"}, 404)
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
                return self.json({"error": "Content-Type must be application/json"}, 415)
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                return self.json({"error": "a single Content-Length is required"}, 400)
            try:
                length = int(self.headers["Content-Length"])
                if length < 1 or length > 1024 * 1024:
                    raise ValueError("request body must be 1 byte to 1 MiB")
                request = json.loads(self.rfile.read(length),
                                     parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if (not isinstance(request, dict) or not isinstance(request.get("method"), str)
                        or request["method"] not in available_methods
                        or not isinstance(request.get("params", {}), dict)):
                    raise ValueError("invalid method or params")
                task, fresh = self.server.store.enqueue(request)
                if fresh:
                    self.server.work.put(task["id"])
                self.json(task, 201)
            except (ValueError, UnicodeDecodeError) as error:
                self.json({"error": str(error) or "invalid request"}, 400)

        def log_message(self, *args):
            pass

    server_type = DemoServer
    if host == "::1":
        class IPv6DemoServer(DemoServer):
            address_family = socket.AF_INET6
        server_type = IPv6DemoServer
    server = server_type((host, port), Handler)
    server.store = TaskStore(config.state_dir / "demo.sqlite3", idempotent_methods)
    server.store.recover_interrupted()
    server.work = queue.Queue()
    for task_id in server.store.queued_ids():
        server.work.put(task_id)
    return server


def serve(config, host, port):
    server = create_server(config, host, port)
    runner = TaskRunner(server.store, lambda request: service.call(config, request))
    stopping = threading.Event()

    def worker():
        while not stopping.is_set():
            try:
                idle_timeout = runner.idle_timeout()
                task_id = server.work.get(timeout=0.5 if idle_timeout is None else min(0.5, idle_timeout))
            except queue.Empty:
                runner.reset_due()
                continue
            runner.reset_due()
            runner.run_task(task_id)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    address = f"[{host}]" if ":" in host else host
    print(json.dumps({"ok": True, "result": {"url": f"http://{address}:{server.server_port}",
                                             "status": "ready"}}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    finally:
        stopping.set()
        thread.join()
        server.server_close()

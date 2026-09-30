import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from .errors import AutomationError


DUPLICATE_WINDOW_SECONDS = 15
RESET_HOLD_SECONDS = 180
_UNSET = object()


@dataclass(frozen=True)
class InterfacePlan:
    context: str | None
    enter_method: str | None = None
    enter_params: dict | None = None
    retain_after_success: bool = True


REUSABLE_CHAT_METHODS = frozenset({
    "chat.open",
    "chat.pin",
    "chat.mute",
    "message.read",
    "message.search",
    "message.send",
    "message.send_file",
    "message.download",
    "message.reply",
    "message.revoke",
    "message.pat",
    "message.pat_revoke",
    "message.delete",
    "message.voice_text",
    "group.members",
    "group.announcement",
    "group.invite",
    "group.remove",
    "group.announcement_set",
    "group.leave",
})


def interface_plan(request):
    method = request.get("method")
    params = request.get("params", {})
    chat = params.get("chat") if isinstance(params, dict) else None
    if method not in REUSABLE_CHAT_METHODS or not isinstance(chat, str) or not chat.strip():
        return InterfacePlan(None)
    context = f"chat:{chat.strip()}"
    if method == "chat.open":
        return InterfacePlan(context)
    return InterfacePlan(context, "chat.open", {"chat": chat},
                         retain_after_success=method != "group.leave")


def initial_phases(plan, duplicate=False):
    if duplicate:
        return {name: {"status": "duplicate"} for name in ("enter", "operate", "reset")}
    if plan.context is None:
        enter = {"status": "skipped", "reason": "no_reusable_interface"}
        reset = {"status": "skipped", "reason": "no_reusable_interface"}
    elif plan.enter_method is None:
        enter = {"status": "skipped", "reason": "operation_enters_interface"}
        reset = {"status": "pending"}
    else:
        enter = {"status": "pending"}
        reset = {"status": "pending"}
    return {"enter": enter, "operate": {"status": "pending"}, "reset": reset}


class TaskStore:
    def __init__(self, path, idempotent_methods=(), duplicate_window_seconds=DUPLICATE_WINDOW_SECONDS,
                 clock=time.time):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS web_tasks ("
            "id TEXT PRIMARY KEY, digest TEXT NOT NULL, method TEXT NOT NULL, request TEXT NOT NULL, "
            "status TEXT NOT NULL, result TEXT, duplicate_of TEXT, created REAL NOT NULL, updated REAL NOT NULL, "
            "context TEXT, phases TEXT NOT NULL DEFAULT '{}')")
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(web_tasks)")}
        if "context" not in columns:
            self.connection.execute("ALTER TABLE web_tasks ADD COLUMN context TEXT")
        if "phases" not in columns:
            self.connection.execute("ALTER TABLE web_tasks ADD COLUMN phases TEXT NOT NULL DEFAULT '{}'")
        self.connection.execute("UPDATE web_tasks SET phases='{}' WHERE phases IS NULL")
        legacy_tasks = self.connection.execute(
            "SELECT id,request,status,phases FROM web_tasks").fetchall()
        for task_id, request, status, phases in legacy_tasks:
            if self._decode_phases(phases):
                continue
            try:
                plan = interface_plan(json.loads(request))
            except (TypeError, ValueError):
                plan = InterfacePlan(None)
            self.connection.execute("UPDATE web_tasks SET phases=? WHERE id=?",
                                    (json.dumps(initial_phases(plan, duplicate=status == "duplicate"),
                                                ensure_ascii=False), task_id))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS web_queue_context ("
            "slot INTEGER PRIMARY KEY CHECK(slot = 1), task_id TEXT NOT NULL, context TEXT NOT NULL, "
            "reset_due_at REAL NOT NULL)")
        self.connection.commit()
        self.lock = threading.Lock()
        self.idempotent_methods = set(idempotent_methods)
        self.duplicate_window_seconds = duplicate_window_seconds
        self.clock = clock

    def close(self):
        self.connection.close()

    def now(self):
        return self.clock()

    @staticmethod
    def request_digest(request):
        return hashlib.sha256(json.dumps(
            [request.get("method"), request.get("params", {}), request.get("confirm_token")],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _decode_phases(raw):
        try:
            return json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return {}

    def enqueue(self, request):
        request = dict(request)
        if request.get("confirm_token") and not request.get("idempotency_key"):
            request["idempotency_key"] = self.confirmation_key(request["confirm_token"])
        if request["method"] in self.idempotent_methods and not request.get("idempotency_key"):
            request["idempotency_key"] = f"demo-{uuid.uuid4()}"
        digest = self.request_digest(request)
        now = self.now()
        plan = interface_plan(request)
        with self.lock:
            row = self.connection.execute(
                "SELECT id FROM web_tasks WHERE digest=? AND created>? AND status!='duplicate' "
                "ORDER BY created DESC LIMIT 1",
                (digest, now - self.duplicate_window_seconds)).fetchone()
            task_id = str(uuid.uuid4())
            duplicate = row[0] if row else None
            status = "duplicate" if duplicate else "queued"
            phases = initial_phases(plan, duplicate=bool(duplicate))
            self.connection.execute(
                "INSERT INTO web_tasks "
                "(id,digest,method,request,status,result,duplicate_of,created,updated,context,phases) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, digest, request["method"], json.dumps(request, ensure_ascii=False), status,
                 None, duplicate, now, now, plan.context, json.dumps(phases, ensure_ascii=False)))
            self.connection.commit()
        return self.get(task_id), not duplicate

    def confirmation_key(self, token):
        with self.lock:
            rows = self.connection.execute(
                "SELECT request,result FROM web_tasks WHERE result IS NOT NULL ORDER BY created DESC").fetchall()
        for request, result in rows:
            response = json.loads(result)
            if response.get("error", {}).get("details", {}).get("confirm_token") == token:
                key = json.loads(request).get("idempotency_key")
                if key:
                    return key
        raise ValueError("confirm_token does not match a prior local task; provide its idempotency_key")

    def get(self, task_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT id,method,request,status,result,duplicate_of,created,updated,context,phases "
                "FROM web_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        request = json.loads(row[2])
        return {"id": row[0], "method": row[1], "request": request, "status": row[3],
                "result": json.loads(row[4]) if row[4] else None, "duplicate_of": row[5],
                "created_at": row[6], "updated_at": row[7],
                "context": row[8] if row[8] is not None else interface_plan(request).context,
                "phases": self._decode_phases(row[9])}

    def list(self):
        with self.lock:
            rows = self.connection.execute("SELECT id FROM web_tasks ORDER BY created DESC LIMIT 100").fetchall()
        return [self.get(row[0]) for row in rows]

    def claim(self, task_id):
        now = self.now()
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE web_tasks SET status='running',updated=? WHERE id=? AND status='queued'",
                (now, task_id))
            self.connection.commit()
        return self.get(task_id) if cursor.rowcount == 1 else None

    def complete(self, task_id, response, now=None):
        status = "succeeded" if response.get("ok") else "failed"
        now = self.now() if now is None else now
        with self.lock:
            self.connection.execute("UPDATE web_tasks SET status=?,result=?,updated=? WHERE id=?",
                                    (status, json.dumps(response, ensure_ascii=False), now, task_id))
            self.connection.commit()

    def _set_phase_locked(self, task_id, phase, status, result=_UNSET, due_at=_UNSET, now=None):
        row = self.connection.execute("SELECT phases FROM web_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        now = self.now() if now is None else now
        phases = self._decode_phases(row[0])
        current = dict(phases.get(phase, {}))
        current["status"] = status
        current["updated_at"] = now
        if result is not _UNSET:
            current["result"] = result
        if due_at is not _UNSET:
            if due_at is None:
                current.pop("due_at", None)
            else:
                current["due_at"] = due_at
        phases[phase] = current
        self.connection.execute("UPDATE web_tasks SET phases=?,updated=? WHERE id=?",
                                (json.dumps(phases, ensure_ascii=False), now, task_id))

    def update_phase(self, task_id, phase, status, result=_UNSET, due_at=_UNSET, now=None):
        with self.lock:
            self._set_phase_locked(task_id, phase, status, result=result, due_at=due_at, now=now)
            self.connection.commit()

    def active_context(self):
        with self.lock:
            return self._active_context_locked()

    def _active_context_locked(self):
        row = self.connection.execute(
            "SELECT task_id,context,reset_due_at FROM web_queue_context WHERE slot=1").fetchone()
        if row is None:
            return None
        return {"task_id": row[0], "context": row[1], "reset_due_at": row[2]}

    def hold_context(self, task_id, context, reset_due_at, now=None):
        now = self.now() if now is None else now
        with self.lock:
            active = self._active_context_locked()
            if active is not None and active["task_id"] != task_id:
                self._set_phase_locked(active["task_id"], "reset", "superseded",
                                       result={"reason": "context_reused", "next_context": context}, now=now)
            self.connection.execute(
                "INSERT OR REPLACE INTO web_queue_context (slot,task_id,context,reset_due_at) VALUES (1,?,?,?)",
                (task_id, context, reset_due_at))
            self._set_phase_locked(task_id, "reset", "stalled",
                                   result={"reason": "waiting_for_next_task", "context": context},
                                   due_at=reset_due_at, now=now)
            self.connection.commit()

    def clear_active_context(self, task_id=None):
        with self.lock:
            active = self._active_context_locked()
            if active is not None and (task_id is None or active["task_id"] == task_id):
                self.connection.execute("DELETE FROM web_queue_context WHERE slot=1")
                self.connection.commit()
            return active

    def abandon_active_context(self, reason, next_context=None, now=None):
        now = self.now() if now is None else now
        with self.lock:
            active = self._active_context_locked()
            if active is None:
                return None
            result = {"reason": reason, "context": active["context"]}
            if next_context is not None:
                result["next_context"] = next_context
            self._set_phase_locked(active["task_id"], "reset", "superseded", result=result, now=now)
            self.connection.execute("DELETE FROM web_queue_context WHERE slot=1")
            self.connection.commit()
            return active


class TaskRunner:
    def __init__(self, store, execute, hold_seconds=RESET_HOLD_SECONDS, clock=time.time):
        self.store = store
        self.execute = execute
        self.hold_seconds = hold_seconds
        self.clock = clock

    def idle_timeout(self):
        active = self.store.active_context()
        if active is None:
            return None
        return max(0, active["reset_due_at"] - self.clock())

    def reset_due(self):
        active = self.store.active_context()
        if active is None or active["reset_due_at"] > self.clock():
            return None
        return self._reset_active(active, "idle_timeout")

    def run_task(self, task_id):
        task = self.store.claim(task_id)
        if task is None:
            return None
        active = self.store.active_context()
        if active is not None and active["reset_due_at"] <= self.clock():
            reset = self._reset_active(active, "hold_expired")
            if not reset.get("ok"):
                return self._blocked_by_reset(task, active, reset)
            active = None
        plan = interface_plan(task["request"])
        if active is not None and active["context"] != plan.context:
            reset = self._reset_active(active, "context_changed")
            if not reset.get("ok"):
                return self._blocked_by_reset(task, active, reset)
            active = None

        context_verified = False
        if plan.context is None:
            self.store.update_phase(task["id"], "enter", "skipped",
                                    result={"reason": "no_reusable_interface"})
        elif active is not None:
            context_verified = True
            self.store.update_phase(task["id"], "enter", "skipped",
                                    result={"reason": "context_reused", "context": plan.context})
        elif plan.enter_method is None:
            self.store.update_phase(task["id"], "enter", "skipped",
                                    result={"reason": "operation_enters_interface", "context": plan.context})
        else:
            self.store.update_phase(task["id"], "enter", "running")
            entered = self._execute({"id": f"{task['id']}:enter", "method": plan.enter_method,
                                     "params": dict(plan.enter_params or {})})
            if not entered.get("ok"):
                self.store.update_phase(task["id"], "enter", "failed", result=entered)
                self.store.update_phase(task["id"], "operate", "skipped",
                                        result={"reason": "enter_failed"})
                self.store.update_phase(task["id"], "reset", "skipped",
                                        result={"reason": "enter_failed"})
                response = self._phase_failure(task, "ENTER_FAILED", "Could not enter task interface",
                                               {"context": plan.context, "response": entered})
                self.store.complete(task["id"], response)
                return self.store.get(task["id"])
            context_verified = True
            self.store.update_phase(task["id"], "enter", "succeeded", result=entered)

        self.store.update_phase(task["id"], "operate", "running")
        response = self._execute({**task["request"], "id": task["id"]})
        self.store.update_phase(task["id"], "operate",
                                "succeeded" if response.get("ok") else "failed", result=response)
        self.store.complete(task["id"], response)

        if plan.context is not None and (context_verified or response.get("ok")):
            if response.get("ok") and not plan.retain_after_success:
                self.store.update_phase(task["id"], "reset", "skipped",
                                        result={"reason": "operation_left_interface"})
                self.store.abandon_active_context("operation_left_interface")
            else:
                self.store.hold_context(task["id"], plan.context,
                                        self.clock() + self.hold_seconds)
        elif plan.context is not None:
            self.store.update_phase(task["id"], "reset", "skipped",
                                    result={"reason": "interface_not_verified"})
        return self.store.get(task["id"])

    def _reset_active(self, active, reason):
        self.store.update_phase(active["task_id"], "reset", "running",
                                result={"reason": reason, "context": active["context"]})
        response = self._execute({"id": f"{active['task_id']}:reset", "method": "ui.reset", "params": {}})
        self.store.update_phase(active["task_id"], "reset",
                                "succeeded" if response.get("ok") else "failed",
                                result={"reason": reason, "context": active["context"],
                                        "response": response}, due_at=None)
        self.store.clear_active_context(active["task_id"])
        return response

    def _blocked_by_reset(self, task, active, reset):
        self.store.update_phase(task["id"], "enter", "skipped",
                                result={"reason": "previous_reset_failed"})
        self.store.update_phase(task["id"], "operate", "skipped",
                                result={"reason": "previous_reset_failed"})
        self.store.update_phase(task["id"], "reset", "skipped",
                                result={"reason": "previous_reset_failed"})
        response = self._phase_failure(task, "CONTEXT_RESET_FAILED",
                                       "The prior task interface could not be reset",
                                       {"context": active["context"], "response": reset})
        self.store.complete(task["id"], response)
        return self.store.get(task["id"])

    def _execute(self, request):
        try:
            response = self.execute(request)
        except AutomationError as error:
            response = {"protocol_version": 1, "id": request["id"], "ok": False,
                        "error": error.as_dict(), "elapsed_ms": 0}
        except Exception as error:
            response = {"protocol_version": 1, "id": request["id"], "ok": False,
                        "error": {"code": "INTERNAL_ERROR", "message": str(error)}, "elapsed_ms": 0}
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            return {"protocol_version": 1, "id": request["id"], "ok": False,
                    "error": {"code": "INVALID_RESPONSE", "message": "Task executor returned invalid response"},
                    "elapsed_ms": 0}
        return response

    @staticmethod
    def _phase_failure(task, code, message, details):
        return {"protocol_version": 1, "id": task["id"], "ok": False,
                "error": {"code": code, "message": message, "details": details}, "elapsed_ms": 0}

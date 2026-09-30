import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from .errors import AutomationError


class State:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.connection = sqlite3.connect(self.directory / "state.sqlite3")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                request_key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                state TEXT NOT NULL, response TEXT, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursors (
                chat TEXT PRIMARY KEY, cursor TEXT NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS confirmations (
                token TEXT PRIMARY KEY, digest TEXT NOT NULL,
                expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS account_profile (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                profile_json TEXT NOT NULL, updated REAL NOT NULL,
                attempted REAL NOT NULL
            );
        """)
        (self.directory / "state.sqlite3").chmod(0o600)

    def close(self):
        self.connection.close()

    def claim(self, key, method, params):
        cached = self.lookup(key, method, params)
        if cached is not None:
            return cached
        encoded = json.dumps([method, params], sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        self.connection.execute("INSERT INTO requests VALUES (?,?, 'pending', NULL, ?)",
                                (key, digest, time.time()))
        self.connection.commit()
        return None

    def lookup(self, key, method, params):
        encoded = json.dumps([method, params], sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        row = self.connection.execute(
            "SELECT digest,state,response FROM requests WHERE request_key=?", (key,)).fetchone()
        if row:
            if row[0] != digest:
                raise AutomationError("IDEMPOTENCY_CONFLICT", "Key already used with different parameters")
            if row[1] != "completed":
                raise AutomationError("OUTCOME_UNKNOWN", "Earlier execution did not complete; inspect before retrying")
            return json.loads(row[2])
        return None

    def confirm_issue(self, method, params, key, ttl=120):
        digest = hashlib.sha256(json.dumps([method, params, key], sort_keys=True,
                              ensure_ascii=False).encode()).hexdigest()
        token = secrets.token_urlsafe(24)
        self.connection.execute("INSERT INTO confirmations VALUES (?,?,?,0)",
                                (token, digest, time.time() + ttl))
        self.connection.commit()
        return token

    def confirm_consume(self, token, method, params, key):
        digest = hashlib.sha256(json.dumps([method, params, key], sort_keys=True,
                              ensure_ascii=False).encode()).hexdigest()
        cursor = self.connection.execute(
            "UPDATE confirmations SET consumed=1 WHERE token=? AND digest=? AND expires>? AND consumed=0",
            (token, digest, time.time()))
        self.connection.commit()
        if cursor.rowcount != 1:
            raise AutomationError("CONFIRMATION_INVALID", "Confirmation expired, used, or for other parameters")

    def complete(self, key, response):
        self.connection.execute("UPDATE requests SET state='completed',response=? WHERE request_key=?",
                                (json.dumps(response, ensure_ascii=False), key))
        self.connection.commit()

    def resolve(self, key, outcome):
        row = self.connection.execute(
            "SELECT state,response FROM requests WHERE request_key=?", (key,)).fetchone()
        if row is None:
            raise AutomationError("KEY_NOT_FOUND", "No execution is recorded for this key")
        response = json.loads(row[1]) if row[1] else None
        if row[0] == "completed" and (response or {}).get("error", {}).get("code") not in (
                "OUTCOME_UNKNOWN", "TRANSPORT_UNKNOWN", "INTERNAL_ERROR"):
            raise AutomationError("RESOLUTION_UNAVAILABLE", "Only unknown executions may be resolved")
        if outcome == "executed":
            self.complete(key, {"protocol_version": 1, "ok": True, "result": {
                "status": "manually_resolved", "outcome": outcome, "delivery_confirmed": False}})
        elif outcome == "not_executed":
            self.connection.execute("DELETE FROM requests WHERE request_key=?", (key,))
            self.connection.commit()
        else:
            raise AutomationError("INVALID_PARAMS", "outcome must be executed or not_executed")
        return {"key": key, "outcome": outcome, "retry_allowed": outcome == "not_executed"}

    def cursor(self, chat, value=None):
        if value is not None:
            self.connection.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)",
                                    (chat, json.dumps(value), time.time()))
            self.connection.commit()
        row = self.connection.execute("SELECT cursor FROM cursors WHERE chat=?", (chat,)).fetchone()
        return json.loads(row[0]) if row else None

    def account_profile(self):
        row = self.connection.execute(
            "SELECT profile_json,updated,attempted FROM account_profile WHERE singleton=1").fetchone()
        if row is None:
            return None
        profile = json.loads(row[0])
        profile["refreshed_at"] = row[1]
        profile["last_attempt_at"] = row[2]
        return profile

    def account_refresh_due(self, now=None, minimum_interval=7200, daily_interval=86400):
        now = time.time() if now is None else now
        profile = self.account_profile()
        if profile is None:
            return True
        if not profile.get("display_name"):
            return now - profile["last_attempt_at"] >= minimum_interval
        return (now - profile["last_attempt_at"] >= minimum_interval
                and now - profile["refreshed_at"] >= daily_interval)

    def account_store(self, profile, now=None):
        now = time.time() if now is None else now
        encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        self.connection.execute(
            "INSERT INTO account_profile(singleton,profile_json,updated,attempted) VALUES (1,?,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET profile_json=excluded.profile_json, "
            "updated=excluded.updated, attempted=excluded.attempted",
            (encoded, now, now))
        self.connection.commit()
        return self.account_profile()

    def account_record_attempt(self, now=None):
        now = time.time() if now is None else now
        profile = self.account_profile()
        if profile is None:
            self.connection.execute(
                "INSERT INTO account_profile(singleton,profile_json,updated,attempted) VALUES (1,?,?,?)",
                (json.dumps({}), 0, now))
        else:
            self.connection.execute("UPDATE account_profile SET attempted=? WHERE singleton=1", (now,))
        self.connection.commit()

    def cleanup(self, days=15):
        now = time.time()
        cutoff = now - days * 86400
        self.connection.execute("DELETE FROM confirmations WHERE expires<?", (now,))
        cursors_removed = self.connection.execute(
            "DELETE FROM cursors WHERE updated<?", (cutoff,)).rowcount
        rows = self.connection.execute(
            "SELECT request_key,state,response FROM requests WHERE created<?", (cutoff,)).fetchall()
        expired_keys = []
        retained_keys = []
        for key, status, raw in rows:
            response = json.loads(raw) if raw else {}
            if status != "completed" or response.get("error", {}).get("code") in (
                    "OUTCOME_UNKNOWN", "TRANSPORT_UNKNOWN", "INTERNAL_ERROR"):
                retained_keys.append((key,))
            else:
                expired_keys.append((key,))
        self.connection.executemany("DELETE FROM requests WHERE request_key=?", expired_keys)
        unknown = json.dumps({"protocol_version": 1, "ok": False, "error": {
            "code": "OUTCOME_UNKNOWN", "retryable": False,
            "message": "Expired execution remains uncertain; inspect and use state.resolve"}})
        self.connection.executemany("UPDATE requests SET response=? WHERE request_key=? AND state='completed'",
                                    [(unknown, key[0]) for key in retained_keys])
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        removed = 0
        for name in ("screenshots", "logs"):
            directory = self.directory / name
            if not directory.exists():
                continue
            for entry in directory.iterdir():
                if entry.is_file() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
        return {"removed": removed, "requests_removed": len(expired_keys), "cursors_removed": cursors_removed,
                "unknown_keys_retained": len(retained_keys), "retention_days": days}

"""Durable local task queue, fenced leases, artifacts, and append-only events.

SQLite is a same-host boundary. No connection or Agent instance is shared by workers.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from enterprise_agent.domain.runtime import SCHEMA_VERSION, REVIEW_MODULES, TaskConflict, TaskLease


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


class TaskStore:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("unsupported task database version")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, request_key TEXT UNIQUE, input_hash TEXT NOT NULL,
                  kind TEXT NOT NULL, mode TEXT NOT NULL, input_json TEXT NOT NULL,
                  status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                  max_attempts INTEGER NOT NULL, timeout_seconds REAL NOT NULL,
                  available_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                  lease_token TEXT, lease_owner TEXT, lease_until REAL,
                  error TEXT, result_json TEXT, parent_id TEXT);
                CREATE TABLE IF NOT EXISTS events (
                  cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                  schema_version INTEGER NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
                  attempt INTEGER NOT NULL, at REAL NOT NULL, producer TEXT NOT NULL,
                  recipient TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS task_events ON events(task_id,cursor);
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,available_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                  task_id TEXT NOT NULL REFERENCES tasks(id), attempt INTEGER NOT NULL,
                  name TEXT NOT NULL, sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
                  PRIMARY KEY(task_id,attempt,name));
                CREATE TABLE IF NOT EXISTS reviews (
                  task_id TEXT NOT NULL REFERENCES tasks(id), attempt INTEGER NOT NULL,
                  module TEXT NOT NULL, binding TEXT NOT NULL, status TEXT NOT NULL,
                  reviewer TEXT, comment TEXT, decided_at REAL,
                  PRIMARY KEY(task_id,attempt,module));
            """)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # Short transactions with the default rollback journal avoid dependence on
        # newer WAL fixes in the Python-bundled SQLite. Only one writer at a time.
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, task, kind, payload, producer="runtime", recipient="audit"):
        db.execute("INSERT INTO events(event_id,schema_version,task_id,attempt,at,producer,recipient,type,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                   (uuid4().hex, SCHEMA_VERSION, task["id"], task["attempt"], time.time(), producer, recipient, kind, encode(payload)))

    @staticmethod
    def _row(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    @staticmethod
    def _owned(db, lease):
        task = TaskStore._row(db, lease.task_id)
        if (task["status"] != "RUNNING" or task["lease_token"] != lease.token
                or task["attempt"] != lease.attempt or task["lease_until"] <= time.time()):
            raise TaskConflict("worker lease is stale")
        return task

    @staticmethod
    def _public(row):
        row = dict(row)
        row["input"] = json.loads(row.pop("input_json"))
        raw = row.pop("result_json")
        row["result"] = json.loads(raw) if raw else None
        for field in ("lease_token", "lease_owner", "input_hash", "request_key"):
            row.pop(field, None)
        return row

    def submit(self, data, *, mode="llm", kind="orchestrate.monitor", request_key=None,
               timeout_seconds=120, max_attempts=2, parent_id=None):
        if kind != "orchestrate.monitor" or mode not in {"llm", "protocol_fixture"}:
            raise ValueError("unsupported task kind or mode")
        if not 0.05 <= timeout_seconds <= 600 or not 1 <= max_attempts <= 3:
            raise ValueError("invalid task execution limits")
        signature = digest([data, mode, kind, timeout_seconds, max_attempts, parent_id])
        now, task_id = time.time(), uuid4().hex
        with self.connection(True) as db:
            if request_key:
                prior = db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
                if prior:
                    if prior["input_hash"] != signature:
                        raise TaskConflict("idempotency key reused with different request")
                    return self._public(prior), False
            db.execute("INSERT INTO tasks(id,request_key,input_hash,kind,mode,input_json,status,max_attempts,timeout_seconds,available_at,created_at,updated_at,parent_id) VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,?,?,?)",
                       (task_id, request_key, signature, kind, mode, encode(data), max_attempts, timeout_seconds, now, now, now, parent_id))
            task = self._row(db, task_id)
            self._event(db, task, "task.queued", {"status": "QUEUED", "mode": mode, "kind": kind})
            return self._public(task), True

    def get(self, task_id):
        with self.connection() as db:
            task = self._public(self._row(db, task_id))
            task["artifacts"] = [{**dict(r), "payload": json.loads(r["payload_json"])} for r in
                                 db.execute("SELECT attempt,name,sha256,payload_json FROM artifacts WHERE task_id=? ORDER BY attempt,name", (task_id,))]
            for artifact in task["artifacts"]:
                artifact.pop("payload_json")
            task["reviews"] = [dict(r) for r in db.execute("SELECT * FROM reviews WHERE task_id=? AND attempt=? ORDER BY module", (task_id, task["attempt"]))]
            return task

    def list(self, limit=100):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
            return [self._public(r) for r in rows]

    def claim(self, owner, lease_seconds=10):
        now = time.time()
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM tasks WHERE status='QUEUED' AND available_at<=? ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            token = uuid4().hex
            db.execute("UPDATE tasks SET status='RUNNING',attempt=attempt+1,lease_token=?,lease_owner=?,lease_until=?,updated_at=? WHERE id=?",
                       (token, owner, now + lease_seconds, now, task["id"]))
            task = self._row(db, task["id"])
            self._event(db, task, "task.started", {"status": "RUNNING", "attempt": task["attempt"]})
            return TaskLease(task["id"], token, task["attempt"])

    def heartbeat(self, lease, seconds=10):
        with self.connection(True) as db:
            self._owned(db, lease)
            db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() + seconds, lease.task_id))

    def publish(self, lease, kind, payload, *, artifact=None, producer="orchestrate", recipient="audit"):
        with self.connection(True) as db:
            task = self._owned(db, lease)
            if artifact:
                db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (lease.task_id, lease.attempt, artifact, digest(payload), encode(payload)))
            self._event(db, task, kind, payload, producer, recipient)

    def complete(self, lease, report):
        with self.connection(True) as db:
            task = self._owned(db, lease)
            binding = digest([lease.task_id, lease.attempt, report])
            active = {"orchestrate"} | set(report["summary"]["initialized_modules"])
            for module in REVIEW_MODULES:
                db.execute("INSERT INTO reviews(task_id,attempt,module,binding,status) VALUES(?,?,?,?,?)",
                           (lease.task_id, lease.attempt, module, binding, "PENDING" if module in active else "NOT_REQUESTED"))
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (lease.task_id, lease.attempt, "report", digest(report), encode(report)))
            self._event(db, task, "artifact.report", report, "orchestrate")
            db.execute("UPDATE tasks SET status='WAITING_REVIEW',result_json=?,lease_token=NULL,lease_owner=NULL,lease_until=NULL,error=NULL,updated_at=? WHERE id=?",
                       (encode(report), time.time(), lease.task_id))
            self._event(db, task, "task.awaiting_review", {"status": "WAITING_REVIEW", "binding": binding,
                        "modules": sorted(active), "execution_authorized": False})

    def fail(self, lease, code, *, retry=True, delay=1):
        with self.connection(True) as db:
            task = self._owned(db, lease)
            self._fail(db, task, code, retry=retry, delay=delay)

    def _fail(self, db, task, code, *, retry=True, delay=1):
        # Error codes only: no raw model responses, exception messages, or credentials.
        allowed = {"WORKER_FAILED", "TIMEOUT", "LEASE_EXPIRED", "SHUTDOWN", "PREDICTION_UNAVAILABLE", "NOT_ADMITTED", "OUT_OF_SCOPE"}
        if code not in allowed:
            code = "WORKER_FAILED"
        status = "QUEUED" if retry and task["attempt"] < task["max_attempts"] else "FAILED"
        db.execute("UPDATE tasks SET status=?,available_at=?,error=?,lease_token=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                   (status, time.time() + delay, code, time.time(), task["id"]))
        self._event(db, task, "task.retry_scheduled" if status == "QUEUED" else "task.failed", {"status": status, "error": code})

    def recover_expired(self):
        with self.connection(True) as db:
            rows = db.execute("SELECT * FROM tasks WHERE status='RUNNING' AND lease_until<=?", (time.time(),)).fetchall()
            for row in rows:
                self._fail(db, dict(row), "LEASE_EXPIRED", delay=0)
            return len(rows)

    def cancel(self, task_id):
        with self.connection(True) as db:
            task = self._row(db, task_id)
            if task["status"] == "CANCELLED":
                return
            if task["status"] not in {"QUEUED", "RUNNING", "WAITING_REVIEW"}:
                raise TaskConflict("completed task cannot be cancelled")
            db.execute("UPDATE tasks SET status='CANCELLED',lease_token=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (time.time(), task_id))
            self._event(db, task, "task.cancelled", {"status": "CANCELLED", "execution_authorized": False})

    def review(self, task_id, module, binding, decision, reviewer, comment=""):
        if decision not in {"APPROVED", "REJECTED"} or module not in REVIEW_MODULES or not reviewer.strip():
            raise ValueError("invalid review decision")
        with self.connection(True) as db:
            task = self._row(db, task_id)
            row = db.execute("SELECT * FROM reviews WHERE task_id=? AND attempt=? AND module=?", (task_id, task["attempt"], module)).fetchone()
            if not row or row["binding"] != binding:
                raise TaskConflict("review is not bound to the current report")
            if row["status"] == decision and row["reviewer"] == reviewer and row["comment"] == comment:
                return  # Exact retry is idempotent, adds no audit event.
            if task["status"] != "WAITING_REVIEW" or row["status"] != "PENDING":
                raise TaskConflict("review is immutable or task no longer awaits review")
            db.execute("UPDATE reviews SET status=?,reviewer=?,comment=?,decided_at=? WHERE task_id=? AND attempt=? AND module=?",
                       (decision, reviewer, comment, time.time(), task_id, task["attempt"], module))
            status = "REJECTED" if decision == "REJECTED" else "WAITING_REVIEW"
            pending = db.execute("SELECT count(*) FROM reviews WHERE task_id=? AND attempt=? AND status='PENDING'", (task_id, task["attempt"])).fetchone()[0]
            if not pending and status != "REJECTED":
                status = "READY"
            db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, time.time(), task_id))
            self._event(db, task, "review.decided", {"module": module, "binding": binding, "decision": decision,
                        "reviewer": reviewer, "comment": comment, "status": status, "execution_authorized": False}, "human", module)

    def events(self, task_id, after=0, limit=500):
        with self.connection() as db:
            self._row(db, task_id)
            return [{**{k: r[k] for k in r.keys() if k != "payload_json"}, "payload": json.loads(r["payload_json"])}
                    for r in db.execute("SELECT * FROM events WHERE task_id=? AND cursor>? ORDER BY cursor LIMIT ?", (task_id, after, limit))]

    def replay(self, task_id, upto=None):
        state = {"task_id": task_id, "status": None, "attempt": 0, "reviews": {}, "artifacts": {}, "cursor": 0,
                 "execution_authorized": False}
        after = 0
        while True:
            batch = self.events(task_id, after)
            if not batch:
                return state
            for event in batch:
                if upto is not None and event["cursor"] > upto:
                    return state
                payload = event["payload"]
                if event["attempt"] > state["attempt"]:
                    state["reviews"], state["artifacts"] = {}, {}
                state.update(attempt=event["attempt"], cursor=event["cursor"])
                if "status" in payload and (event["type"].startswith("task.") or event["type"] == "review.decided"):
                    state["status"] = payload["status"]
                if event["type"] == "task.awaiting_review":
                    state["reviews"] = {module: {"module": module, "binding": payload["binding"],
                                        "status": "PENDING" if module in payload["modules"] else "NOT_REQUESTED"}
                                        for module in REVIEW_MODULES}
                if event["type"] == "review.decided":
                    state["reviews"][payload["module"]] = {key: payload[key] for key in
                        ("module", "binding", "reviewer", "comment")}
                    state["reviews"][payload["module"]]["status"] = payload["decision"]
                if event["type"].startswith("artifact."):
                    state["artifacts"][event["type"].split(".", 1)[1]] = payload
                after = event["cursor"]

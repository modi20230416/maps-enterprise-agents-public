"""Atomic local state, event idempotency and auditable decision persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from enterprise_agent.domain.models import Event, Plan, Snapshot
from enterprise_agent.experiments.rule_orchestrator import RuleExperimentOrchestrator


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StateStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                initial_snapshot TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                plan TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                initial_plan TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                result TEXT NOT NULL,
                PRIMARY KEY (session_id, event_id)
            );
        """)
        # Upgrade databases created by the first prototype revision.
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(sessions)")}
        if "initial_plan" not in columns:
            self.connection.execute("ALTER TABLE sessions ADD COLUMN initial_plan TEXT")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def initialize(self, session_id: str, snapshot: Snapshot, engine: RuleExperimentOrchestrator) -> Plan:
        initial = encode(snapshot.to_dict())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT initial_snapshot, initial_plan FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row:
                if row[0] != initial:
                    raise ValueError("session already exists with different initial snapshot")
                if row[1] is None:
                    plan = engine.initialize(snapshot)
                    self.connection.execute("UPDATE sessions SET initial_plan=? WHERE id=?",
                                            (encode(plan.to_dict()), session_id))
                else:
                    plan = Plan.parse(json.loads(row[1]))
            else:
                plan = engine.initialize(snapshot)
                serialized = encode(plan.to_dict())
                self.connection.execute(
                    "INSERT INTO sessions (id, initial_snapshot, snapshot, plan, version, initial_plan) "
                    "VALUES (?, ?, ?, ?, 0, ?)", (session_id, initial, initial, serialized, serialized))
            self.connection.execute("COMMIT")
            return plan
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def read(self, session_id: str) -> tuple[Snapshot, Plan, int]:
        row = self.connection.execute(
            "SELECT snapshot, plan, version FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("unknown session")
        return Snapshot.parse(json.loads(row[0])), Plan.parse(json.loads(row[1])), row[2]

    def process(self, session_id: str, event: Event, engine: RuleExperimentOrchestrator) -> dict:
        fingerprint = hashlib.sha256(encode(asdict(event)).encode("utf-8")).hexdigest()
        def recorded():
            row = self.connection.execute(
                "SELECT fingerprint, result FROM decisions WHERE session_id=? AND event_id=?",
                (session_id, event.id)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("event id reused with different content")
                return json.loads(row[1])
            return None

        cached = recorded()
        if cached is not None:
            return cached
        snapshot, baseline, version = self.read(session_id)
        # Remote LLM calls must not hold a database write lock. Recheck the
        # parent version before committing so a stale candidate cannot win.
        snapshot, plan, result = engine.process(snapshot, baseline, event)
        result["version"] = version + 1
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cached = recorded()
            if cached is not None:
                result = cached
            else:
                current_version = self.read(session_id)[2]
                if current_version != version:
                    raise ValueError("state changed during planning; retry the event against the new version")
                self.connection.execute(
                    "UPDATE sessions SET snapshot=?, plan=?, version=? WHERE id=?",
                    (encode(snapshot.to_dict()), encode(plan.to_dict()), version + 1, session_id))
                self.connection.execute("INSERT INTO decisions VALUES (?, ?, ?, ?)",
                                        (session_id, event.id, fingerprint, encode(result)))
            self.connection.execute("COMMIT")
            return result
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

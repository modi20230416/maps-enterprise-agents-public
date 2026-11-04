"""Durability and process-boundary tests; no provider calls or production writes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest

from enterprise_agent.application.task_runtime import TaskRuntime
from enterprise_agent.domain.runtime import TaskConflict, TERMINAL
from enterprise_agent.infrastructure.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / "examples/orchestrate_llm.json").read_text(encoding="utf-8"))


def slow_handler(store, task, lease):
    import os
    store.publish(lease, "test.started", {"pid": os.getpid()})
    time.sleep(1)
    store.complete(lease, {"summary": {"initialized_modules": []}})


def crash_handler(store, task, lease):
    import os
    os._exit(7)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "tasks.sqlite3")
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.stop()
        self.tmp.cleanup()

    def submit(self, **kwargs):
        return self.store.submit(DATA, mode="protocol_fixture", **kwargs)[0]["id"]

    def start(self, **kwargs):
        runtime = TaskRuntime(self.store, **kwargs)
        self.runtimes.append(runtime)
        runtime.start()
        return runtime

    def wait(self, task_id, statuses=TERMINAL | {"WAITING_REVIEW"}, seconds=12):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            task = self.store.get(task_id)
            if task["status"] in statuses:
                return task
            time.sleep(.04)
        self.fail(f"task did not reach {statuses}: {self.store.get(task_id)}")

    def test_idempotency_and_conflict(self):
        task, created = self.store.submit(DATA, request_key="one")
        retry, created_again = self.store.submit(DATA, request_key="one")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(task["id"], retry["id"])
        with self.assertRaises(TaskConflict):
            self.store.submit(DATA, request_key="one", mode="protocol_fixture")
        self.assertEqual(len(self.store.events(task["id"])), 1)

    def test_atomic_claim_only_one_owner(self):
        self.submit()
        def claim(i):
            return TaskStore(self.store.path).claim(str(i))
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(claim, range(6)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_expired_lease_recovery_and_old_worker_fencing(self):
        task_id = self.submit()
        old = self.store.claim("old", lease_seconds=.03)
        time.sleep(.05)
        reopened = TaskStore(self.store.path)
        self.assertEqual(reopened.recover_expired(), 1)
        new = reopened.claim("new")
        self.assertEqual(new.attempt, 2)
        for action in (lambda: self.store.publish(old, "stale", {}),
                       lambda: self.store.complete(old, {"summary": {"initialized_modules": []}}),
                       lambda: self.store.heartbeat(old), lambda: self.store.fail(old, "TIMEOUT")):
            with self.assertRaises(TaskConflict):
                action()
        reopened.complete(new, {"summary": {"initialized_modules": []}})
        self.assertEqual(self.store.get(task_id)["status"], "WAITING_REVIEW")

    def test_protocol_fixture_persists_every_stage_and_replay(self):
        task_id = self.submit()
        self.start()
        task = self.wait(task_id)
        self.assertEqual(task["status"], "WAITING_REVIEW")
        artifacts = {a["name"]: a["payload"] for a in task["artifacts"]}
        self.assertEqual(set(artifacts), {"observation", "prediction", "preparation", "report"})
        summary = task["result"]["summary"]
        self.assertTrue(summary["mes_state_unchanged"])
        self.assertFalse(summary["live_call_verified"])
        self.assertEqual(summary["response_origin"], "protocol_fixture")
        self.assertEqual(set(summary["initialized_modules"]), {"planning", "scheduling", "warehouse"})
        reopened = TaskStore(self.store.path)
        projection = reopened.replay(task_id)
        self.assertEqual(projection["status"], task["status"])
        self.assertTrue(all(r["status"] == "PENDING" for r in projection["reviews"].values()))
        self.assertEqual(projection["artifacts"]["report"], task["result"])
        prediction_cursor = next(e["cursor"] for e in reopened.events(task_id) if e["type"] == "artifact.prediction")
        before_preparation = reopened.replay(task_id, prediction_cursor)
        self.assertIn("prediction", before_preparation["artifacts"])
        self.assertNotIn("preparation", before_preparation["artifacts"])
        self.assertEqual(reopened.replay(task_id, 1)["status"], "QUEUED")
        events = reopened.events(task_id)
        self.assertEqual(len({e["event_id"] for e in events}), len(events))
        self.assertTrue(all(e["task_id"] == task_id and e["schema_version"] == 1 for e in events))

    def test_four_bound_reviews_survive_restart_without_execution(self):
        task_id = self.submit()
        self.start()
        task = self.wait(task_id)
        for review in task["reviews"]:
            with self.assertRaises(TaskConflict):
                self.store.review(task_id, review["module"], "0"*64, "APPROVED", "tester")
            self.store.review(task_id, review["module"], review["binding"], "APPROVED", "tester", "checked")
            count = len(self.store.events(task_id))
            self.store.review(task_id, review["module"], review["binding"], "APPROVED", "tester", "checked")
            self.assertEqual(len(self.store.events(task_id)), count)
            with self.assertRaises(TaskConflict):
                self.store.review(task_id, review["module"], review["binding"], "REJECTED", "tester")
        task = TaskStore(self.store.path).get(task_id)
        self.assertEqual(task["status"], "READY")
        self.assertFalse(task["result"]["summary"]["execution_authorized"])
        self.assertEqual(self.store.replay(task_id)["status"], "READY")
        self.assertEqual(len(self.store.replay(task_id)["reviews"]), 4)

    def test_parallel_processes_overlap_and_do_not_share_context(self):
        first, second = self.submit(), self.submit()
        self.start(workers=2, handler=slow_handler)
        self.wait(first); self.wait(second)
        a, b = self.store.events(first), self.store.events(second)
        start_a = next(e for e in a if e["type"] == "test.started")
        start_b = next(e for e in b if e["type"] == "test.started")
        end_a = next(e for e in a if e["type"] == "task.awaiting_review")
        end_b = next(e for e in b if e["type"] == "task.awaiting_review")
        self.assertNotEqual(start_a["payload"]["pid"], start_b["payload"]["pid"])
        self.assertLess(max(start_a["at"], start_b["at"]), min(end_a["at"], end_b["at"]))

    def test_hard_timeout_is_bounded_and_other_task_can_run(self):
        task_id = self.submit(timeout_seconds=.1, max_attempts=2)
        other = self.submit()
        self.start(workers=1, handler=slow_handler)
        task = self.wait(task_id)
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["error"], "TIMEOUT")
        self.assertEqual(task["attempt"], 2)
        self.assertEqual(self.wait(other)["status"], "WAITING_REVIEW")

    def test_crashed_worker_retries_then_fails(self):
        task_id = self.submit(max_attempts=2)
        self.start(handler=crash_handler)
        task = self.wait(task_id)
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(task["attempt"], 2)
        self.assertEqual(task["error"], "WORKER_FAILED")

    def test_cancel_running_process_and_fence_late_result(self):
        task_id = self.submit()
        runtime = self.start(handler=slow_handler)
        self.wait(task_id, {"RUNNING"})
        self.store.cancel(task_id)
        runtime.stop()
        self.assertEqual(self.store.get(task_id)["status"], "CANCELLED")
        self.assertFalse(any(e["type"] == "task.awaiting_review" for e in self.store.events(task_id)))

    def test_graceful_restart_requeues_inflight_attempt(self):
        task_id = self.submit()
        runtime = self.start(handler=slow_handler)
        self.wait(task_id, {"RUNNING"})
        runtime.stop()
        self.assertEqual(self.store.get(task_id)["status"], "QUEUED")
        second = TaskRuntime(TaskStore(self.store.path))
        self.runtimes.append(second); second.start()
        task = self.wait(task_id)
        self.assertEqual(task["status"], "WAITING_REVIEW")
        self.assertEqual(task["attempt"], 2)

    def test_rejection_is_terminal_and_inactive_module_needs_no_review(self):
        task_id = self.submit()
        lease = self.store.claim("manual")
        self.store.complete(lease, {"summary": {"initialized_modules": []}})
        task = self.store.get(task_id)
        self.assertEqual(sum(r["status"] == "NOT_REQUESTED" for r in task["reviews"]), 3)
        review = next(r for r in task["reviews"] if r["module"] == "orchestrate")
        self.store.review(task_id, "orchestrate", review["binding"], "REJECTED", "tester")
        self.assertEqual(self.store.get(task_id)["status"], "REJECTED")


if __name__ == "__main__":
    unittest.main()

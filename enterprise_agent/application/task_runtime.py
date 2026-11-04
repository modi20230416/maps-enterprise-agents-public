"""Bounded same-host process workers with durable queue and hard task timeouts."""
import logging
import multiprocessing
import os
from threading import Event, Thread
import time
from uuid import uuid4

from enterprise_agent.domain.runtime import TaskConflict, TaskHandler
from enterprise_agent.infrastructure.task_store import TaskStore

log = logging.getLogger(__name__)


def monitor_handler(store, task, lease):
    from enterprise_agent.agents.prediction import LLMPrediction
    from enterprise_agent.application.llm_monitor_demo import run_llm_monitor_data
    from enterprise_agent.application.protocol_fixture import fixture_prediction
    prediction = fixture_prediction() if task["mode"] == "protocol_fixture" else LLMPrediction()
    store.publish(lease, "artifact.observation", task["input"], artifact="observation", producer="mes_reader", recipient="orchestrate")
    store.publish(lease, "worker.context_created", {"pid": os.getpid(), "isolated_context": True})

    def on_stage(stage, data):
        store.publish(lease, "artifact." + stage, data, artifact=stage,
                      producer="prediction" if stage == "prediction" else "orchestrate")

    report = run_llm_monitor_data(task["input"], prediction, on_stage=on_stage)
    status = report["summary"]["status"]
    if status in {"PREDICTION_UNAVAILABLE", "NOT_ADMITTED", "OUT_OF_SCOPE"}:
        store.fail(lease, status, retry=status == "PREDICTION_UNAVAILABLE", delay=task["attempt"])
        return
    # Trace is persisted under the task/attempt envelope. It is audit data, not
    # a claim that the current downstream modules are separate remote services.
    for message in report["observation"].get("trace", []):
        store.publish(lease, "agent.trace", message, producer=message["sender"], recipient=message["receiver"])
    store.complete(lease, report)


HANDLERS = {"orchestrate.monitor": monitor_handler}


def execute_attempt(db_path, lease, handler=None):
    store = TaskStore(db_path)
    try:
        task = store.get(lease.task_id)
        (handler or HANDLERS[task["kind"]])(store, task, lease)
    except TaskConflict:
        pass  # Lost lease: old worker must not publish or revive a cancelled task.
    except Exception:
        try:
            store.fail(lease, "WORKER_FAILED", delay=lease.attempt)
        except TaskConflict:
            pass


class TaskRuntime:
    def __init__(self, store, *, workers=2, lease_seconds=10, poll_seconds=0.15, handler: TaskHandler | None = None):
        if not 1 <= workers <= 8 or lease_seconds < 0.2:
            raise ValueError("invalid worker configuration")
        self.store, self.workers = store, workers
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self.handler = handler  # Internal injection seam for process-boundary tests.
        self.owner = uuid4().hex
        self.context = multiprocessing.get_context("spawn")
        self.stop_event = Event()
        self.active = {}
        self.thread = None
        self.healthy = True

    def start(self):
        if self.thread is not None:
            return
        self.store.recover_expired()
        self.thread = Thread(target=self._loop, name="maps-scheduler", daemon=True)
        self.thread.start()

    @staticmethod
    def _stop_process(process):
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        process.close()

    def _tick(self):
        self.store.recover_expired()
        for task_id, (lease, process, deadline) in list(self.active.items()):
            task = self.store.get(task_id)
            finished = task["status"] != "RUNNING" or task["attempt"] != lease.attempt
            code = "TIMEOUT" if time.monotonic() >= deadline else "WORKER_FAILED" if not process.is_alive() else None
            if finished or code:
                self._stop_process(process)
                self.active.pop(task_id)
                if not finished:
                    try:
                        self.store.fail(lease, code, delay=lease.attempt)
                    except TaskConflict:
                        pass
            else:
                try:
                    self.store.heartbeat(lease, self.lease_seconds)
                except TaskConflict:
                    self._stop_process(process)
                    self.active.pop(task_id)
        while len(self.active) < self.workers:
            lease = self.store.claim(self.owner, self.lease_seconds)
            if lease is None:
                break
            task = self.store.get(lease.task_id)
            process = self.context.Process(target=execute_attempt, args=(self.store.path, lease, self.handler), daemon=True)
            try:
                process.start()
            except Exception:
                self.store.fail(lease, "WORKER_FAILED", delay=lease.attempt)
                continue
            self.active[lease.task_id] = (lease, process, time.monotonic() + task["timeout_seconds"])

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self._tick()
                    self.healthy = True
                except Exception:
                    self.healthy = False
                    log.error("Task scheduler tick failed; inspect local database/runtime health")
                self.stop_event.wait(self.poll_seconds)
        finally:
            for lease, process, _ in list(self.active.values()):
                self._stop_process(process)
                try:
                    self.store.fail(lease, "SHUTDOWN", delay=0)
                except TaskConflict:
                    pass
            self.active.clear()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("task runtime did not shut down")

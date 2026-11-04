"""Versioned task/event contracts shared by the API and local worker runtime."""
from dataclasses import dataclass
from typing import Protocol

SCHEMA_VERSION = 1
TERMINAL = frozenset({"READY", "REJECTED", "FAILED", "CANCELLED"})
REVIEW_MODULES = ("orchestrate", "planning", "scheduling", "warehouse")


class TaskConflict(ValueError):
    """Stale ownership, idempotency conflict, or immutable review conflict."""


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    token: str
    attempt: int


class TaskHandler(Protocol):
    def __call__(self, store, task: dict, lease: TaskLease) -> None:
        """Execute one isolated attempt; publish using the fenced lease."""

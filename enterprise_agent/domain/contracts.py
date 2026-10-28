"""Versioned contracts for the MAPS framework; scheduling time is still minutes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from enterprise_agent.domain.models import Event, Plan, Snapshot, identifier, integer


class Priority(str, Enum):
    EMERGENCY = "emergency"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Configurable demonstration weights; not measured production priorities.
PRIORITY_WEIGHTS = {Priority.EMERGENCY: 100, Priority.HIGH: 10,
                    Priority.MEDIUM: 3, Priority.LOW: 1}
RESPONSE_TARGET_SECONDS = {Priority.EMERGENCY: 300, Priority.HIGH: 30,
                           Priority.MEDIUM: 300, Priority.LOW: 1800}
EVENT_OBJECTS = {
    "planning.tick": "plan_execution", "order.created": "inserted_order",
    "machine.unavailable": "equipment", "receipt.delayed": "inventory",
    "inventory.corrected": "inventory",
}
SAFETY_ACTIONS = {"preempt", "changeover", "process_parameter_change"}


@dataclass(frozen=True)
class MapsEvent:
    event: Event
    priority: Priority
    safety_actions: tuple[str, ...] = ()

    def __post_init__(self):
        if self.event.kind not in EVENT_OBJECTS:
            raise ValueError("event kind is not in the MAPS event dictionary")
        if not isinstance(self.priority, Priority):
            raise ValueError("priority must use the four-level dictionary")
        if not set(self.safety_actions) <= SAFETY_ACTIONS:
            raise ValueError("unknown safety action")

    @classmethod
    def parse(cls, data: dict) -> MapsEvent:
        actions = data.get("safety_actions", [])
        if not isinstance(actions, list) or not all(isinstance(x, str) for x in actions):
            raise ValueError("safety_actions must be a string array")
        return cls(Event.parse(data), Priority(data["priority"]), tuple(actions))

    @property
    def monitored_object(self) -> str:
        return EVENT_OBJECTS[self.event.kind]


@dataclass(frozen=True)
class WorkOrder:
    """Explicit MO -> WO identity; core Order.id maps to wo_id, ops to OP."""
    mo_id: str
    wo_id: str
    priority: Priority
    status: str = "planned"
    interruptible: bool = False

    def __post_init__(self):
        identifier(self.mo_id, "mo_id")
        identifier(self.wo_id, "wo_id")
        if not isinstance(self.priority, Priority):
            raise ValueError("work-order priority must use the dictionary")
        if self.status not in {"planned", "running", "completed"}:
            raise ValueError("invalid work-order status")
        if type(self.interruptible) is not bool:
            raise ValueError("interruptible must be boolean")


@dataclass
class MESBaseline:
    version: int
    snapshot: Snapshot
    plan: Plan
    work_orders: tuple[WorkOrder, ...]

    def __post_init__(self):
        integer(self.version, "MES version", 1)
        ids = [w.wo_id for w in self.work_orders]
        if len(ids) != len(set(ids)) or set(ids) != {o.id for o in self.snapshot.orders}:
            raise ValueError("MO/WO catalog must exactly cover snapshot work orders")
        for order in self.snapshot.orders:
            wo = next(w for w in self.work_orders if w.wo_id == order.id)
            if order.priority != PRIORITY_WEIGHTS[wo.priority]:
                raise ValueError("numeric priority must match configured dictionary weight")


@dataclass
class DecisionContext:
    baseline: MESBaseline
    snapshot: Snapshot
    work_orders: tuple[WorkOrder, ...]
    event: MapsEvent

    @property
    def frozen_ids(self) -> frozenset[str]:
        # Conservative phase-one contract: freeze the whole started WO.
        return frozenset(w.wo_id for w in self.work_orders if w.status != "planned")


@dataclass(frozen=True)
class GateDecision:
    need_replan: bool
    scope: tuple[str, ...]
    reasons: tuple[str, ...]
    elapsed_seconds: float
    budget_seconds: float = 5.0


@dataclass(frozen=True)
class Proposal:
    action: str
    scope: tuple[str, ...]
    rationale: str
    source: str = "rules"


@dataclass(frozen=True)
class WorkPackage:
    work_orders: tuple[WorkOrder, ...]
    scope: tuple[str, ...]
    frozen_ids: frozenset[str]
    forecasts: tuple[dict, ...]


@dataclass
class ScheduleCandidate:
    plan: Plan
    messages: list[dict]
    solver: str
    conflicts: list[str] = field(default_factory=list)


@dataclass
class MESPatch:
    patch_id: str
    expected_version: int
    context: DecisionContext
    scope: tuple[str, ...]
    plan: Plan


@dataclass
class DecisionResult:
    event_id: str
    status: str
    base_version: int
    mes_version: int
    reason: str
    trace: list[dict]
    plan: dict | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

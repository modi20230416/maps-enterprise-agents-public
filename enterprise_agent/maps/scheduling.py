"""Scheduling <-> warehouse negotiation; optional learning remains gated."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import math

from ..models import Machine, Plan
from ..scheduler import OptimizationAgent, measure, score
from ..warehouse import MessageBus, WarehouseAgent
from .contracts import DecisionContext, ScheduleCandidate, WorkPackage
from .ports import ConstraintSolver, MARLPolicy


class LocalSearchSolver:
    """Concrete scaffold solver. Bounded heuristics, explicitly not CP/MILP.

    Retains all assignments outside scope and freezes whole started WOs. Fixed
    kits are reserved first; their machine intervals become occupied windows.
    """

    def solve(self, context: DecisionContext, package: WorkPackage) -> ScheduleCandidate:
        movable = set(package.scope) - context.frozen_ids
        baseline, snapshot = context.baseline.plan, context.snapshot
        fixed = [a for a in baseline.assignments if a.order_id not in movable]
        fixed_ids = {a.order_id for a in fixed}
        bus = MessageBus(context.event.event.id)
        warehouse = WarehouseAgent(snapshot, bus)
        reservations = []
        first_starts = {o.id: next(a.start for a in fixed if a.order_id == o.id
                                  and a.operation_id == o.operations[0].id)
                        for o in snapshot.orders if o.id in fixed_ids}
        for order in sorted((o for o in snapshot.orders if o.id in fixed_ids),
                            key=lambda o: (first_starts[o.id], o.id)):
            ready, allocations, _ = warehouse.reserve(order)
            if ready is None or ready > first_starts[order.id]:
                return ScheduleCandidate(deepcopy(baseline), bus.messages, "bounded_local_search",
                                         [f"fixed work order material conflict: {order.id}"])
            reservations.extend({**a, "consume_at": first_starts[order.id]} for a in allocations)
        remaining = {lot["lot"]: lot["remaining"]
                     for lots in warehouse.lots.values() for lot in lots}
        reduced = replace(snapshot,
            orders=tuple(o for o in snapshot.orders if o.id in movable),
            stock={m: remaining.get(f"stock:{m}", 0) for m in snapshot.stock},
            receipts=tuple(replace(r, quantity=remaining[r.id]) for r in snapshot.receipts
                           if remaining[r.id] > 0),
            machines=tuple(Machine(m.id, tuple(sorted((*m.unavailable,
                *((a.start, a.end) for a in fixed if a.machine_id == m.id)))))
                for m in snapshot.machines))
        local, messages, _ = OptimizationAgent().solve(reduced, baseline, context.event.event.id)
        merged = Plan([*fixed, *local.assignments],
                      {**{k: v for k, v in baseline.blocked.items() if k not in movable}, **local.blocked},
                      [*reservations, *local.reservations], strategy="bounded_local_search")
        merged.metrics = measure(snapshot, merged, baseline)
        return ScheduleCandidate(merged, [*bus.messages, *messages], "bounded_local_search")


class WarehouseCoordinator:
    """One direct confirmation round, including lead time and shared inventory.

    Confirmations are simulated reservations. No stock mutation occurs in WMS.
    """

    def confirm(self, context: DecisionContext, plan: Plan) -> tuple[list[dict], list[dict], list[str]]:
        bus = MessageBus(context.event.event.id)
        warehouse = WarehouseAgent(context.snapshot, bus)
        starts = {o.id: next((a.start for a in plan.assignments if a.order_id == o.id
                             and a.operation_id == o.operations[0].id), None)
                  for o in context.snapshot.orders}
        reservations, conflicts = [], []
        for order in sorted((o for o in context.snapshot.orders if starts[o.id] is not None),
                            key=lambda o: (starts[o.id], -o.priority, o.id)):
            ready, allocations, reason = warehouse.reserve(order)
            if ready is None or ready > starts[order.id]:
                conflicts.append(f"{order.id}: {reason or 'kit ready after planned start'}")
                bus.send("warehouse", "scheduling", "material.warning", {
                    "order_id": order.id, "ready": ready, "required_at": starts[order.id],
                    "fallback": "hold work order; request confirmed supply or local replan"})
            else:
                reservations.extend({**a, "consume_at": starts[order.id]} for a in allocations)
                bus.send("warehouse", "scheduling", "material.confirmed", {
                    "order_id": order.id, "ready": ready, "required_at": starts[order.id],
                    "scope": "simulation_only"})
        conflicts.extend(f"blocked work order: {oid}" for oid in plan.blocked)
        return reservations, bus.messages, conflicts


@dataclass(frozen=True)
class ReplayQualification:
    """Evidence supplied by an offline evaluator; not proof of production safety."""
    policy_id: str
    source: str
    cases: int
    baseline: dict[str, float]
    candidate: dict[str, float]

    def eligible(self, policy_id: str) -> bool:
        metrics = {"blocked_priority", "weighted_tardiness", "schedule_churn_minutes"}
        return (self.policy_id == policy_id and self.source in {"simulation", "historical_replay"}
                and type(self.cases) is int and self.cases > 0
                and all(type(x.get(k)) in {int, float} and math.isfinite(x[k]) and x[k] >= 0
                        for x in (self.baseline, self.candidate) for k in metrics)
                and all(self.candidate[k] <= self.baseline[k] for k in metrics))


class SchedulingAgent:
    def __init__(self, solver: ConstraintSolver | None = None,
                 policy: MARLPolicy | None = None, qualification: ReplayQualification | None = None):
        self.solver = solver or LocalSearchSolver()
        self.warehouse = WarehouseCoordinator()
        self.policy, self.qualification = policy, qualification
        if policy and (not qualification or not qualification.eligible(policy.policy_id)):
            raise ValueError("MARL requires matching, non-degrading replay evidence before activation")

    def schedule(self, context: DecisionContext, package: WorkPackage,
                 planning_candidate: ScheduleCandidate | None = None) -> ScheduleCandidate:
        result = planning_candidate if planning_candidate is not None else self.solver.solve(context, package)
        if self.policy and not result.conflicts:
            from .validation import validate_candidate
            try:
                learned = self.policy.propose(context, package)
                errors = validate_candidate(context, learned.plan, package.scope)
                learned.plan.metrics = measure(context.snapshot, learned.plan, context.baseline.plan)
                result.plan.metrics = measure(context.snapshot, result.plan, context.baseline.plan)
                if not errors and not learned.conflicts and score(learned.plan.metrics) <= score(result.plan.metrics):
                    result = learned
                else:
                    result.messages.append({"sender": "scheduling", "receiver": "orchestrate",
                                            "kind": "policy.rejected", "payload": {"reason": "invalid or degrading candidate"}})
            except (ValueError, RuntimeError, OSError):
                result.messages.append({"sender": "scheduling", "receiver": "orchestrate",
                                        "kind": "policy.fallback", "payload": {"reason": "policy unavailable"}})
        if not result.conflicts:
            allocations, messages, conflicts = self.warehouse.confirm(context, result.plan)
            result.plan.reservations = allocations
            result.messages.extend(messages)
            result.conflicts.extend(conflicts)
        return result

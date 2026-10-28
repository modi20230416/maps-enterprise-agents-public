from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from enterprise_agent.domain.models import Machine, Plan
from enterprise_agent.capabilities.scheduling import BoundedSearchOptimizer, measure
from enterprise_agent.infrastructure.trace import TraceLog
from enterprise_agent.capabilities.materials import MaterialAllocator
from enterprise_agent.domain.contracts import DecisionContext, ScheduleCandidate, WorkPackage


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
        bus = TraceLog(context.event.event.id)
        warehouse = MaterialAllocator(snapshot, bus)
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
        local, messages, _ = BoundedSearchOptimizer().solve(reduced, baseline, context.event.event.id)
        merged = Plan([*fixed, *local.assignments],
                      {**{k: v for k, v in baseline.blocked.items() if k not in movable}, **local.blocked},
                      [*reservations, *local.reservations], strategy="bounded_local_search")
        merged.metrics = measure(snapshot, merged, baseline)
        return ScheduleCandidate(merged, [*bus.messages, *messages], "bounded_local_search")

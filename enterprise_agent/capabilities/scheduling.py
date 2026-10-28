"""Bounded dispatch-rule search and independent feasibility checks."""

from __future__ import annotations

from itertools import permutations

from enterprise_agent.domain.models import Assignment, Plan, Snapshot
from enterprise_agent.infrastructure.trace import TraceLog
from enterprise_agent.capabilities.materials import MaterialAllocator


def earliest_slot(ready: int, duration: int, occupied: list[tuple[int, int]]) -> int:
    start = ready
    for left, right in sorted(occupied):
        if start + duration <= left:
            break
        if start < right and start + duration > left:
            start = right
    return start


def measure(snapshot: Snapshot, plan: Plan, baseline: Plan | None = None) -> dict:
    completion = {}
    for a in plan.assignments:
        completion[a.order_id] = max(completion.get(a.order_id, 0), a.end)
    tardiness = {o.id: max(0, completion[o.id] - o.due)
                 for o in snapshot.orders if o.id in completion}
    weights = {o.id: o.priority for o in snapshot.orders}
    old = {(a.order_id, a.operation_id): a for a in baseline.assignments} if baseline else {}
    churn = sum(abs(a.start - old[(a.order_id, a.operation_id)].start)
                + (10 if a.machine_id != old[(a.order_id, a.operation_id)].machine_id else 0)
                for a in plan.assignments if (a.order_id, a.operation_id) in old)
    return {"weighted_tardiness": sum(weights[k] * v for k, v in tardiness.items()),
            "total_tardiness": sum(tardiness.values()),
            "late_orders": sum(v > 0 for v in tardiness.values()),
            "scheduled_orders": len(completion), "blocked_orders": len(plan.blocked),
            "blocked_priority": sum(weights.get(k, 0) for k in plan.blocked),
            "makespan": max(completion.values(), default=snapshot.now) - snapshot.now,
            "schedule_churn_minutes": churn, "completion": completion,
            "tardiness": tardiness}


def score(metrics: dict, churn_weight: int = 1) -> tuple[int, int, int]:
    # Unscheduled priority is lexicographically more expensive than late work.
    return (metrics["blocked_priority"],
            10 * metrics["weighted_tardiness"] + churn_weight * metrics["schedule_churn_minutes"],
            metrics["makespan"])


def validate_plan(snapshot: Snapshot, plan: Plan) -> list[str]:
    """Validate constraints independently of the search and reservation logic."""
    errors = []
    orders = {o.id: o for o in snapshot.orders}
    machines = {m.id: m for m in snapshot.machines}
    indexed: dict[tuple[str, str], Assignment] = {}
    calendars: dict[str, list[Assignment]] = {m: [] for m in machines}
    for a in plan.assignments:
        key = (a.order_id, a.operation_id)
        if key in indexed:
            errors.append(f"duplicate assignment {key}")
        indexed[key] = a
        order = orders.get(a.order_id)
        op = next((op for op in order.operations if op.id == a.operation_id), None) if order else None
        if op is None or a.machine_id not in machines:
            errors.append(f"unknown assignment {key}")
            continue
        if (a.machine_id not in op.machines or a.end - a.start != op.duration
                or type(a.start) is not int or type(a.end) is not int):
            errors.append(f"invalid duration or machine {key}")
        if a.start < max(snapshot.now, order.release):
            errors.append(f"before release {key}")
        if any(a.start < end and a.end > start
               for start, end in machines[a.machine_id].unavailable):
            errors.append(f"machine unavailable {key}")
        calendars[a.machine_id].append(a)
    for machine, assignments in calendars.items():
        ordered = sorted(assignments, key=lambda a: a.start)
        if any(a.end > b.start for a, b in zip(ordered, ordered[1:])):
            errors.append(f"machine overlap {machine}")
    consumption: dict[str, list[tuple[int, int]]] = {}
    for order in snapshot.orders:
        assigned = [indexed.get((order.id, op.id)) for op in order.operations]
        if order.id in plan.blocked:
            if any(assigned):
                errors.append(f"blocked order has assignments {order.id}")
            continue
        if not all(assigned):
            errors.append(f"missing operation {order.id}")
            continue
        if any(a.end > b.start for a, b in zip(assigned, assigned[1:])):
            errors.append(f"precedence violation {order.id}")
        for material, quantity in order.materials.items():
            consumption.setdefault(material, []).append((assigned[0].start, quantity))
    if not set(plan.blocked) <= set(orders):
        errors.append("unknown blocked order")
    for material, uses in consumption.items():
        used = 0
        for at, quantity in sorted(uses):
            used += quantity
            available = snapshot.stock.get(material, 0) if (
                at >= snapshot.now + snapshot.preparation_minutes) else 0
            available += sum(r.quantity for r in snapshot.receipts
                             if r.material == material and at >= max(snapshot.now, r.eta)
                             + snapshot.preparation_minutes)
            if used > available:
                errors.append(f"material overdraw {material} at {at}")
    return sorted(set(errors))


class BoundedSearchOptimizer:
    """Search is exact only over the selected dispatch sequences, not all schedules."""

    def __init__(self, candidate_limit: int = 120):
        if type(candidate_limit) is not int or candidate_limit < 1:
            raise ValueError("candidate_limit must be a positive integer")
        self.candidate_limit = candidate_limit

    def dispatch_sequences(self, snapshot: Snapshot) -> list[tuple[str, tuple]]:
        orders = snapshot.orders
        rules = [
            ("priority_edd", tuple(sorted(orders, key=lambda o: (-o.priority, o.due, o.id)))),
            ("edd", tuple(sorted(orders, key=lambda o: (o.due, o.id)))),
            ("weighted_slack", tuple(sorted(orders, key=lambda o: (
                (o.due - max(snapshot.now, o.release) - sum(op.duration for op in o.operations))
                / o.priority, -o.priority, o.id)))),
        ]
        if len(orders) <= 5:
            rules.extend(("bounded_permutation", p) for p in permutations(orders))
        else:
            initial = rules[0][1]
            for i in range(len(initial) - 1):
                seq = list(initial)
                seq[i], seq[i + 1] = seq[i + 1], seq[i]
                rules.append(("adjacent_swap", tuple(seq)))
        unique = []
        seen = set()
        for name, seq in rules:
            ids = tuple(o.id for o in seq)
            if ids not in seen:
                unique.append((name, seq))
                seen.add(ids)
            if len(unique) >= self.candidate_limit:
                break
        return unique

    def construct(self, snapshot: Snapshot, orders: tuple, strategy: str,
                  correlation_id: str) -> tuple[Plan, list[dict]]:
        bus = TraceLog(correlation_id)
        warehouse = MaterialAllocator(snapshot, bus)
        occupied = {m.id: list(m.unavailable) for m in snapshot.machines}
        plan = Plan(strategy=strategy)
        for order in orders:
            ready, allocations, reason = warehouse.reserve(order)
            if ready is None:
                plan.blocked[order.id] = reason
                continue
            first_start = None
            for op in order.operations:
                start, machine = min((earliest_slot(ready, op.duration, occupied[m]), m)
                                     for m in op.machines)
                assignment = Assignment(order.id, op.id, machine, start, start + op.duration)
                plan.assignments.append(assignment)
                occupied[machine].append((assignment.start, assignment.end))
                ready = assignment.end
                if first_start is None:
                    first_start = start
            for allocation in allocations:
                allocation["consume_at"] = first_start
            plan.reservations.extend(allocations)
        return plan, bus.messages

    def solve(self, snapshot: Snapshot, baseline: Plan | None = None,
              correlation_id: str = "initial") -> tuple[Plan, list[dict], list[dict]]:
        best = None
        best_messages = []
        candidates = []
        for name, seq in self.dispatch_sequences(snapshot):
            plan, messages = self.construct(snapshot, seq, name, correlation_id)
            errors = validate_plan(snapshot, plan)
            if errors:
                raise RuntimeError(f"optimizer generated invalid plan: {errors}")
            plan.metrics = measure(snapshot, plan, baseline)
            candidates.append({"strategy": name, "order_sequence": [o.id for o in seq],
                               "score": list(score(plan.metrics)), "metrics": plan.metrics})
            if best is None or score(plan.metrics) < score(best.metrics):
                best, best_messages = plan, messages
        return best, best_messages, candidates

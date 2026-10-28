"""Collection, prediction and fast response policy for one decision epoch."""

from __future__ import annotations

from copy import deepcopy

from .models import Event, Plan, Snapshot, integer
from .scheduler import OptimizationAgent, measure, score, validate_plan
from .warehouse import MessageBus, WarehouseAgent


class CollectionAgent:
    """Normalize ERP/WMS/MES-style fixture events into a canonical snapshot."""

    @staticmethod
    def apply(snapshot: Snapshot, event: Event) -> Snapshot:
        if event.at != snapshot.now:
            raise ValueError("event.at must equal snapshot.now; this prototype evaluates one "
                             "decision epoch. Submit a new complete scenario for another epoch")
        data = deepcopy(snapshot.to_dict())
        payload = event.payload
        if event.kind == "order.created":
            data["orders"].append(payload["order"])
        elif event.kind == "machine.unavailable":
            machine = next((m for m in data["machines"] if m["id"] == payload["machine_id"]), None)
            if machine is None:
                raise ValueError("unknown machine in event")
            start = integer(payload["start"], "start", snapshot.now)
            end = integer(payload["end"], "end", start + 1)
            machine["unavailable"].append([start, end])
        elif event.kind == "receipt.delayed":
            receipt = next((r for r in data["receipts"] if r["id"] == payload["receipt_id"]), None)
            if receipt is None:
                raise ValueError("unknown receipt in event")
            eta = integer(payload["eta"], "eta", snapshot.now)
            if eta < receipt["eta"]:
                raise ValueError("receipt.delayed cannot move eta earlier")
            receipt["eta"] = eta
        elif event.kind == "inventory.corrected":
            material = payload["material"]
            if not isinstance(material, str) or not material.strip():
                raise ValueError("material must be a nonempty string")
            data["stock"][material] = integer(payload["quantity"], "quantity")
        elif event.kind != "planning.tick":
            raise ValueError(f"unsupported event kind: {event.kind}")
        return Snapshot.parse(data)


class PredictionAgent:
    """Deterministic forward check of known downtime, supply ETA and deadlines.

    This is a lower-bound/rule forecaster, not a trained failure prediction model.
    It only knows the provided snapshot and events; no future ground truth leaks.
    """

    @staticmethod
    def predict(snapshot: Snapshot, baseline: Plan) -> list[dict]:
        risks = []
        horizon = snapshot.now + snapshot.lookahead_minutes
        machines = {m.id: m for m in snapshot.machines}
        for a in baseline.assignments:
            for start, end in machines[a.machine_id].unavailable:
                if a.start < end and a.end > start and start <= horizon:
                    risks.append({"kind": "capacity_conflict", "order_id": a.order_id,
                                  "machine_id": a.machine_id, "risk_at": start,
                                  "lead_minutes": max(0, start - snapshot.now),
                                  "proactive": start > snapshot.now,
                                  "reason": "known future unavailability intersects baseline"})
        # Check cumulative kit consumption against known supply, not per-order
        # availability alone, so shared stock contention is visible in forecasts.
        consumption = {}
        for order in snapshot.orders:
            first = next((a for a in baseline.assignments
                          if a.order_id == order.id and a.operation_id == order.operations[0].id), None)
            if first:
                for material, qty in order.materials.items():
                    consumption.setdefault(material, []).append((first.start, order.id, qty))
        for material, demands in consumption.items():
            cumulative = 0
            supplies = [(snapshot.now + snapshot.preparation_minutes, snapshot.stock.get(material, 0))]
            supplies.extend((max(snapshot.now, r.eta) + snapshot.preparation_minutes, r.quantity)
                            for r in snapshot.receipts if r.material == material)
            for at, order_id, qty in sorted(demands):
                cumulative += qty
                available = sum(amount for ready, amount in supplies if ready <= at)
                if available < cumulative and at <= horizon:
                    total = 0
                    available_at = None
                    for ready, amount in sorted(supplies):
                        total += amount
                        if total >= cumulative:
                            available_at = ready
                            break
                    risks.append({"kind": "material_wait", "order_id": order_id,
                                  "material": material, "risk_at": at,
                                  "available_at": available_at,
                                  "lead_minutes": max(0, at - snapshot.now),
                                  "proactive": at > snapshot.now,
                                  "reason": "known supply cannot cover baseline kit consumption"})
        for order in snapshot.orders:
            ready = max(order.release, snapshot.now)
            missing = {}
            for material, quantity in order.materials.items():
                lots = [(snapshot.now + snapshot.preparation_minutes,
                         snapshot.stock.get(material, 0))]
                lots.extend((max(snapshot.now, r.eta) + snapshot.preparation_minutes, r.quantity)
                            for r in snapshot.receipts if r.material == material)
                total = 0
                for at, amount in sorted(lots):
                    total += amount
                    if total >= quantity:
                        ready = max(ready, at)
                        break
                if total < quantity:
                    missing[material] = quantity - total
            if missing:
                risks.append({"kind": "material_shortage", "order_id": order.id,
                              "shortages": missing, "proactive": True,
                              "reason": "confirmed stock and receipts cannot cover order"})
            earliest_completion = ready + sum(op.duration for op in order.operations)
            if not missing and earliest_completion > order.due:
                risks.append({"kind": "delivery_lower_bound", "order_id": order.id,
                              "earliest_completion": earliest_completion, "due": order.due,
                              "proactive": order.due > snapshot.now,
                              "reason": "deadline impossible even without machine contention"})
        return risks


class ResponseAgent:
    """Cheap impact gate: invalid/new work triggers search; unchanged state is kept."""

    @staticmethod
    def assess(snapshot: Snapshot, baseline: Plan, event: Event) -> dict:
        errors = validate_plan(snapshot, baseline)
        reassess_blocked = bool(baseline.blocked) and event.kind != "planning.tick"
        triggers = list(errors)
        if reassess_blocked:
            triggers.append("reassess blocked orders after state change")
        return {"should_optimize": bool(triggers), "reasons": triggers or [
            "baseline remains feasible; no new order or blocked-work state change"],
            "baseline_feasible": not errors}


class OrchestratorAgent:
    def __init__(self, optimizer: OptimizationAgent | None = None):
        self.optimizer = optimizer or OptimizationAgent()

    def initialize(self, snapshot: Snapshot) -> Plan:
        return self.optimizer.solve(snapshot)[0]

    def process(self, snapshot: Snapshot, baseline: Plan, event: Event) -> tuple[Snapshot, Plan, dict]:
        updated = CollectionAgent.apply(snapshot, event)
        bus = MessageBus(event.id)
        bus.send("collection", "orchestrator", "state.updated", {
            "event_id": event.id, "kind": event.kind, "now": updated.now})
        risks = PredictionAgent.predict(updated, baseline)
        bus.send("prediction", "orchestrator", "risk.forecast", {"risks": risks})
        assessment = ResponseAgent.assess(updated, baseline, event)
        bus.send("response", "orchestrator", "change.assessed", assessment)
        before = measure(updated, baseline, baseline)
        selected = deepcopy(baseline)
        selected.metrics = before
        candidates = []
        action = "KEEP"
        reason = "baseline remains feasible; avoid unnecessary schedule changes"
        if assessment["should_optimize"]:
            bus.send("orchestrator", "optimization", "plan.requested", {
                "objective": "blocked_priority, 10*weighted_tardiness + churn, makespan",
                "priority_policy": "positive integer weight; larger is more urgent"})
            candidate, messages, candidates = self.optimizer.solve(updated, baseline, event.id)
            for message in messages:
                bus.send(message["sender"], message["receiver"], message["kind"], message["payload"])
            if not assessment["baseline_feasible"] or score(candidate.metrics) < score(before):
                selected = candidate
                action = "REPLAN"
                reason = ("repair infeasible baseline" if not assessment["baseline_feasible"]
                          else "candidate improves objective including schedule stability")
            else:
                reason = "candidate does not improve the feasible baseline"
        violations = validate_plan(updated, selected)
        if violations:
            raise RuntimeError(f"selected plan failed feasibility validation: {violations}")
        if action == "KEEP":
            # Supply may change without invalidating timing. Refresh lot bindings
            # instead of retaining references to depleted or delayed old lots.
            warehouse = WarehouseAgent(updated, bus)
            reservations = []
            first_starts = {a.order_id: a.start for a in selected.assignments
                            if any(o.id == a.order_id and o.operations[0].id == a.operation_id
                                   for o in updated.orders)}
            for order in sorted((o for o in updated.orders if o.id in first_starts),
                                key=lambda o: (first_starts[o.id], o.id)):
                ready, allocations, _ = warehouse.reserve(order)
                if ready is None or ready > first_starts[order.id]:
                    raise RuntimeError("cannot reconcile retained plan material reservations")
                for allocation in allocations:
                    allocation["consume_at"] = first_starts[order.id]
                reservations.extend(allocations)
            selected.reservations = reservations
        if action == "REPLAN":
            bus.send("orchestrator", "warehouse", "reservation.selected", {
                "reservations": selected.reservations,
                "scope": "local simulation; no ERP/WMS writeback"})
        bus.send("orchestrator", "execution", "plan.selected", {
            "action": action, "blocked": selected.blocked, "reason": reason,
            "status": "simulation_only"})
        result = {"event": {"id": event.id, "kind": event.kind, "at": event.at},
                  "action": action, "reason": reason, "assessment": assessment,
                  "risks": risks, "before_metrics": before,
                  "before_feasible": assessment["baseline_feasible"],
                  "after_metrics": selected.metrics, "validation_errors": violations,
                  "plan": selected.to_dict(), "messages": bus.messages,
                  "candidate_count": len(candidates), "candidates": candidates}
        return updated, selected, result

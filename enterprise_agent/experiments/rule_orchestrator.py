from __future__ import annotations
from copy import deepcopy
from enterprise_agent.domain.models import Event, Plan, Snapshot
from enterprise_agent.capabilities.scheduling import BoundedSearchOptimizer, measure, score, validate_plan
from enterprise_agent.infrastructure.trace import TraceLog
from enterprise_agent.capabilities.materials import MaterialAllocator

from enterprise_agent.capabilities.collection import EventCollector
from enterprise_agent.capabilities.prediction import KnownRiskPredictor

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


class RuleExperimentOrchestrator:
    def __init__(self, optimizer: BoundedSearchOptimizer | None = None):
        self.optimizer = optimizer or BoundedSearchOptimizer()

    def initialize(self, snapshot: Snapshot) -> Plan:
        return self.optimizer.solve(snapshot)[0]

    def process(self, snapshot: Snapshot, baseline: Plan, event: Event) -> tuple[Snapshot, Plan, dict]:
        updated = EventCollector.apply(snapshot, event)
        bus = TraceLog(event.id)
        bus.send("collection", "orchestrator", "state.updated", {
            "event_id": event.id, "kind": event.kind, "now": updated.now})
        risks = KnownRiskPredictor.predict(updated, baseline)
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
            warehouse = MaterialAllocator(updated, bus)
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

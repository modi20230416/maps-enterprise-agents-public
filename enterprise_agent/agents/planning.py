"""Planning: read-only collection, fast gate, forecast and WO work package."""

from __future__ import annotations

from dataclasses import replace
from time import monotonic

from enterprise_agent.capabilities.collection import EventCollector
from enterprise_agent.capabilities.prediction import KnownRiskPredictor
from enterprise_agent.capabilities.scheduling import measure, validate_plan
from enterprise_agent.domain.contracts import DecisionContext, GateDecision, MapsEvent, MESBaseline, PRIORITY_WEIGHTS, Priority, WorkOrder, WorkPackage
from enterprise_agent.domain.ports import ConstraintSolver, ForecastProvider


class DataCollect:
    def collect(self, baseline: MESBaseline, event: MapsEvent) -> DecisionContext:
        normalized = event.event
        catalog = list(baseline.work_orders)
        if normalized.kind == "order.created":
            payload = dict(normalized.payload)
            order = dict(payload["order"])
            order["priority"] = PRIORITY_WEIGHTS[event.priority]
            payload["order"] = order
            normalized = replace(normalized, payload=payload)
            catalog.append(WorkOrder(payload["mo_id"], order["id"], event.priority))
        snapshot = EventCollector.apply(baseline.snapshot, normalized)
        return DecisionContext(baseline, snapshot, tuple(catalog), event)


class QuickRespond:
    """No network/LLM on the <5s gate path. SLA is measured, not assumed."""

    def assess(self, context: DecisionContext) -> GateDecision:
        started = monotonic()
        snapshot, baseline = context.snapshot, context.baseline.plan
        event = context.event.event
        reasons = list(validate_plan(snapshot, baseline))
        if baseline.blocked and event.kind != "planning.tick":
            reasons.append("reassess blocked work after state change")
        if measure(snapshot, baseline)["late_orders"]:
            reasons.append("MES baseline has delivery risk")
        if event.kind == "order.created" and context.event.priority in {Priority.HIGH, Priority.EMERGENCY}:
            reasons.append("high or emergency MO insertion")
        if context.event.safety_actions:
            reasons.append("safety action requested")
        # Bounded closure through shared machines/materials. Unrelated WOs stay fixed.
        orders = {o.id: o for o in snapshot.orders}
        if event.kind == "order.created":
            scope = {event.payload["order"]["id"]}
        elif event.kind == "machine.unavailable":
            scope = {o.id for o in orders.values() if any(
                event.payload["machine_id"] in op.machines for op in o.operations)}
        elif event.kind in {"inventory.corrected", "receipt.delayed"}:
            material = event.payload.get("material")
            if event.kind == "receipt.delayed":
                material = next(r.material for r in snapshot.receipts if r.id == event.payload["receipt_id"])
            scope = {o.id for o in orders.values() if material in o.materials}
        else:
            risks = KnownRiskPredictor.predict(snapshot, baseline)
            scope = {r["order_id"] for r in risks if "order_id" in r}
            scope.update(baseline.blocked)
            if reasons and not scope:
                scope.update(orders)
        while True:
            machines = {m for oid in scope for op in orders[oid].operations for m in op.machines}
            materials = {m for oid in scope for m in orders[oid].materials}
            expanded = scope | {o.id for o in orders.values() if
                                materials.intersection(o.materials) or any(
                                    machines.intersection(op.machines) for op in o.operations)}
            if expanded == scope:
                break
            scope = expanded
        scope -= context.frozen_ids
        elapsed = monotonic() - started
        return GateDecision(bool(reasons), tuple(sorted(scope)),
                            tuple(reasons or ["baseline feasible; no effective change"]), elapsed)


class KnownWindowPrediction:
    """Known calendar/ETA evidence, not trained probabilistic forecasts."""

    def forecast(self, context: DecisionContext) -> list[dict]:
        return [{**risk, "source": "known_calendar_and_eta", "confidence_interval": None}
                for risk in KnownRiskPredictor.predict(context.snapshot, context.baseline.plan)]


class PlanningAgent:
    def __init__(self, prediction: ForecastProvider | None = None,
                 optimizer: ConstraintSolver | None = None):
        from enterprise_agent.capabilities.local_search import LocalSearchSolver
        self.prediction = prediction or KnownWindowPrediction()
        self.optimizer = optimizer or LocalSearchSolver()

    def prepare(self, context: DecisionContext, scope: tuple[str, ...]) -> WorkPackage:
        return WorkPackage(tuple(w for w in context.work_orders if w.wo_id in scope),
                           scope, context.frozen_ids, tuple(self.prediction.forecast(context)))

    def optimize(self, context: DecisionContext, package: WorkPackage):
        """Planning produces a local WO proposal with a detailed OP seed plan."""
        return self.optimizer.solve(context, package)

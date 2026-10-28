from __future__ import annotations
from enterprise_agent.domain.models import Plan, Snapshot


class KnownRiskPredictor:
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

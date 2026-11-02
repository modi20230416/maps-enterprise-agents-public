"""Orchestrate-owned lookahead of factory calendars. No supply prediction."""

class OperationsPrediction:
    def forecast(self, context):
        snapshot, baseline = context.snapshot, context.baseline.plan
        horizon = snapshot.now + snapshot.lookahead_minutes
        machines = {m.id: m for m in snapshot.machines}
        risks = []
        for a in baseline.assignments:
            for start, end in machines[a.machine_id].unavailable:
                if snapshot.now < start <= horizon and a.start < end and a.end > start:
                    risks.append({"kind": "future_capacity_conflict", "order_id": a.order_id,
                                  "machine_id": a.machine_id, "risk_at": start,
                                  "lead_minutes": start - snapshot.now,
                                  "source": "known_future_calendar", "confidence_interval": None})
        return risks

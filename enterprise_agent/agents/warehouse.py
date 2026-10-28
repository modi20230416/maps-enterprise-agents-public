from __future__ import annotations
from enterprise_agent.domain.models import Plan
from enterprise_agent.infrastructure.trace import TraceLog
from enterprise_agent.capabilities.materials import MaterialAllocator
from enterprise_agent.domain.contracts import DecisionContext


class WarehouseAgent:
    """One direct confirmation round, including lead time and shared inventory.

    Confirmations are simulated reservations. No stock mutation occurs in WMS.
    """

    def confirm(self, context: DecisionContext, plan: Plan) -> tuple[list[dict], list[dict], list[str]]:
        bus = TraceLog(context.event.event.id)
        warehouse = MaterialAllocator(context.snapshot, bus)
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

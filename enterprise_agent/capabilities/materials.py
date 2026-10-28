from __future__ import annotations
from enterprise_agent.domain.models import Order, Snapshot

from enterprise_agent.infrastructure.trace import TraceLog

class MaterialAllocator:
    """Reservations belong to one candidate, never to global inventory.

    All order materials are issued as one kit before its first operation. Supply
    lots include preparation/transport lead time. A rejected kit consumes nothing.
    """

    def __init__(self, snapshot: Snapshot, bus: TraceLog):
        self.snapshot = snapshot
        self.bus = bus
        self.lots: dict[str, list[dict]] = {}
        for material, quantity in snapshot.stock.items():
            self.lots.setdefault(material, []).append({
                "lot": f"stock:{material}", "remaining": quantity,
                "ready": snapshot.now + snapshot.preparation_minutes})
        for receipt in snapshot.receipts:
            self.lots.setdefault(receipt.material, []).append({
                "lot": receipt.id, "remaining": receipt.quantity,
                "ready": max(snapshot.now, receipt.eta) + snapshot.preparation_minutes})
        for lots in self.lots.values():
            lots.sort(key=lambda lot: (lot["ready"], lot["lot"]))

    def reserve(self, order: Order) -> tuple[int | None, list[dict], str]:
        self.bus.send("scheduling", "warehouse", "material.request", {
            "order_id": order.id, "priority": order.priority,
            "materials": order.materials, "release": order.release})
        shortages = {material: quantity - sum(lot["remaining"]
                     for lot in self.lots.get(material, []))
                     for material, quantity in order.materials.items()}
        shortages = {k: v for k, v in shortages.items() if v > 0}
        if shortages:
            reason = "insufficient confirmed supply: " + ", ".join(
                f"{material}={qty}" for material, qty in sorted(shortages.items()))
            self.bus.send("warehouse", "scheduling", "material.rejected", {
                "order_id": order.id, "shortages": shortages,
                "action": "request procurement confirmation"})
            return None, [], reason
        ready = max(self.snapshot.now, order.release)
        allocations = []
        for material, quantity in sorted(order.materials.items()):
            left = quantity
            for lot in self.lots[material]:
                take = min(left, lot["remaining"])
                if take:
                    lot["remaining"] -= take
                    left -= take
                    ready = max(ready, lot["ready"])
                    allocations.append({"order_id": order.id, "material": material,
                                        "quantity": take, "lot": lot["lot"],
                                        "ready": lot["ready"]})
                if not left:
                    break
        self.bus.send("warehouse", "scheduling", "material.offered", {
            "order_id": order.id, "ready": ready, "allocations": allocations,
            "reservation_status": "tentative_candidate_only"})
        return ready, allocations, ""

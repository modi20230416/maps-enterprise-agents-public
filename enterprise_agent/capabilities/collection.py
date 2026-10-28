from __future__ import annotations
from copy import deepcopy
from enterprise_agent.domain.models import Event, Snapshot, integer


class EventCollector:
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

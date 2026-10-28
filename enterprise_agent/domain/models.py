"""Canonical contracts. Time is an integer minute relative to a scenario epoch."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


@dataclass(frozen=True)
class Operation:
    id: str
    machines: tuple[str, ...]
    duration: int

    @classmethod
    def parse(cls, data: dict) -> Operation:
        machines = data["machines"]
        if not isinstance(machines, list) or not machines:
            raise ValueError("operation.machines must be a nonempty list")
        return cls(identifier(data["id"], "operation.id"),
                   tuple(identifier(m, "machine id") for m in machines),
                   integer(data["duration"], "duration", 1))


@dataclass(frozen=True)
class Order:
    id: str
    priority: int
    due: int
    release: int
    materials: dict[str, int]
    operations: tuple[Operation, ...]

    @classmethod
    def parse(cls, data: dict) -> Order:
        ops = tuple(Operation.parse(op) for op in data["operations"])
        if not ops or len({op.id for op in ops}) != len(ops):
            raise ValueError("order operations must be nonempty with unique ids")
        materials = {identifier(k, "material id"): integer(v, "quantity", 1)
                     for k, v in data.get("materials", {}).items()}
        return cls(identifier(data["id"], "order.id"),
                   integer(data["priority"], "priority", 1),
                   integer(data["due"], "due"),
                   integer(data.get("release", 0), "release"), materials, ops)


@dataclass(frozen=True)
class Receipt:
    id: str
    material: str
    quantity: int
    eta: int

    @classmethod
    def parse(cls, data: dict) -> Receipt:
        return cls(identifier(data["id"], "receipt.id"),
                   identifier(data["material"], "material"),
                   integer(data["quantity"], "quantity", 1),
                   integer(data["eta"], "eta"))


@dataclass(frozen=True)
class Machine:
    id: str
    unavailable: tuple[tuple[int, int], ...] = ()

    @classmethod
    def parse(cls, data: dict) -> Machine:
        windows = []
        for start, end in data.get("unavailable", []):
            integer(start, "unavailable.start")
            integer(end, "unavailable.end", start + 1)
            windows.append((start, end))
        return cls(identifier(data["id"], "machine.id"), tuple(sorted(windows)))


@dataclass(frozen=True)
class Snapshot:
    now: int
    machines: tuple[Machine, ...]
    orders: tuple[Order, ...]
    stock: dict[str, int]
    receipts: tuple[Receipt, ...]
    preparation_minutes: int = 10
    lookahead_minutes: int = 120

    @classmethod
    def parse(cls, data: dict) -> Snapshot:
        machines = tuple(Machine.parse(m) for m in data["machines"])
        orders = tuple(Order.parse(o) for o in data["orders"])
        receipts = tuple(Receipt.parse(r) for r in data.get("receipts", []))
        for label, entities in (("machines", machines), ("orders", orders),
                                ("receipts", receipts)):
            if len({v.id for v in entities}) != len(entities):
                raise ValueError(f"duplicate {label} ids")
        machine_ids = {m.id for m in machines}
        for order in orders:
            for op in order.operations:
                if not set(op.machines) <= machine_ids:
                    raise ValueError(f"unknown machine in {order.id}/{op.id}")
        if not machine_ids:
            raise ValueError("at least one machine is required")
        return cls(integer(data.get("now", 0), "now"), machines, orders,
                   {identifier(k, "stock material"): integer(v, "stock")
                    for k, v in data.get("stock", {}).items()}, receipts,
                   integer(data.get("preparation_minutes", 10), "preparation_minutes"),
                   integer(data.get("lookahead_minutes", 120), "lookahead_minutes", 1))

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self)))


@dataclass(frozen=True)
class Assignment:
    order_id: str
    operation_id: str
    machine_id: str
    start: int
    end: int


@dataclass
class Plan:
    assignments: list[Assignment] = field(default_factory=list)
    blocked: dict[str, str] = field(default_factory=dict)
    reservations: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    strategy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, data: dict) -> Plan:
        return cls([Assignment(**a) for a in data["assignments"]],
                   data.get("blocked", {}), data.get("reservations", []),
                   data.get("metrics", {}), data.get("strategy", ""))


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    at: int
    payload: dict

    @classmethod
    def parse(cls, data: dict) -> Event:
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("event.payload must be an object")
        return cls(identifier(data["id"], "event.id"),
                   identifier(data["kind"], "event.kind"),
                   integer(data["at"], "event.at"), payload)

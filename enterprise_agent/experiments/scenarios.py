"""Small explicit synthetic fixtures; none are real enterprise data."""

from copy import deepcopy


def order(order_id, priority, due, duration, material="steel", quantity=1):
    return {"id": order_id, "priority": priority, "due": due, "release": 0,
            "materials": {material: quantity}, "operations": [
                {"id": "cut", "machines": ["CUT-1", "CUT-2"], "duration": duration},
                {"id": "assemble", "machines": ["ASSEMBLY-1"], "duration": 20}]}


def base_snapshot():
    return {"now": 0, "preparation_minutes": 10, "lookahead_minutes": 180,
            "machines": [{"id": "CUT-1"}, {"id": "CUT-2"}, {"id": "ASSEMBLY-1"}],
            "stock": {"steel": 3}, "receipts": [],
            "orders": [order("NORMAL-1", 2, 110, 45), order("NORMAL-2", 1, 150, 40)]}


def scenarios():
    base = base_snapshot()
    material = deepcopy(base)
    material["stock"] = {"steel": 1}
    material["receipts"] = [{"id": "PO-1", "material": "steel", "quantity": 1, "eta": 40}]
    return {
        "rush_order": {"description": "高优先级急单插入，比较加权延期与排程变动",
                       "snapshot": deepcopy(base), "events": [
                           {"id": "rush-001", "kind": "order.created", "at": 0,
                            "payload": {"order": order("URGENT-1", 10, 70, 35)}}]},
        "future_downtime": {"description": "t=0 获知 t=60 的设备不可用，提前调整",
                            "snapshot": deepcopy(base), "events": [
                                {"id": "machine-001", "kind": "machine.unavailable", "at": 0,
                                 "payload": {"machine_id": "ASSEMBLY-1", "start": 60, "end": 130}}]},
        "receipt_delay": {"description": "到货从 t=40 延迟到 t=120，重新协商齐套时间",
                          "snapshot": material, "events": [
                              {"id": "receipt-001", "kind": "receipt.delayed", "at": 0,
                               "payload": {"receipt_id": "PO-1", "eta": 120}}]},
        "no_change": {"description": "周期检查没有有效变化，保持排程",
                      "snapshot": deepcopy(base), "events": [
                          {"id": "tick-001", "kind": "planning.tick", "at": 0, "payload": {}}]},
        "material_shortage": {"description": "库存盘点下降，明确阻塞订单而不透支物料",
                              "snapshot": deepcopy(base), "events": [
                                  {"id": "stock-001", "kind": "inventory.corrected", "at": 0,
                                   "payload": {"material": "steel", "quantity": 1}}]},
    }

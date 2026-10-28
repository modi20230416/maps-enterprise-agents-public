"""Load an explicit synthetic MES baseline, rather than inventing one in Agent."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .adapters import InMemoryMES, parse_baseline
from .contracts import MapsEvent
from .orchestrate import OrchestrateAgent


DEMO_CASES = ("high_order", "emergency_review", "material_conflict", "no_change")


def run_demo(path: str, scenario: str = "high_order", proposer=None) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    event = deepcopy(data["event"])
    if scenario == "emergency_review":
        event.update(id="emergency-review", priority="emergency", safety_actions=["preempt"])
    elif scenario == "material_conflict":
        event["payload"]["order"]["materials"]["steel"] = 100
        event["id"] = "material-conflict"
    elif scenario == "no_change":
        event = {"id": "no-change", "kind": "planning.tick", "at": data["mes"]["snapshot"]["now"],
                 "priority": "low", "payload": {}}
    elif scenario != "high_order":
        raise ValueError("unknown framework demo case")
    mes = InMemoryMES(parse_baseline(data["mes"]))
    engine = OrchestrateAgent(mes, mes, proposer=proposer)
    result = engine.process(MapsEvent.parse(event))
    return {"mode": "framework_in_memory_mes", "scenario": scenario,
            "real_mes_connected": False, "marl_implemented": False,
            "cp_milp_implemented": False, "decision": result.to_dict(),
            "final_mes_plan": mes.read_baseline().plan.to_dict()}

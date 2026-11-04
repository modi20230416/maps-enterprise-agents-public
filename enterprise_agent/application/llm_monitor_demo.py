"""LLM foresight from observed progress, without supplying future risk answers."""
from dataclasses import asdict
import json
from pathlib import Path

from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.domain.contracts import MapsEvent
from enterprise_agent.infrastructure.mes import InMemoryMES, parse_baseline


def run_llm_monitor_demo(path, prediction):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return run_llm_monitor_data(data, prediction)


def run_llm_monitor_data(data, prediction, on_stage=None):
    """Application entry shared by CLI and API workers; no file round trip."""
    mes = InMemoryMES(parse_baseline(data["mes"]))
    before = mes.read_baseline()
    result = OrchestrateAgent(mes, mes, prediction=prediction).monitor(
        MapsEvent.parse(data["event"]), initialize=True, on_stage=on_stage)
    after = mes.read_baseline()
    preparation = result["preparation"]
    call = result.get("prediction_call") or {}
    return {"summary": {
        "status": result["status"], "prediction_source": result["prediction_source"],
        "response_origin": call.get("response_origin", "no_valid_response"),
        "live_call_verified": call.get("live_call_verified", False),
        "model": call.get("model"), "risk_count": len(result["risks"]),
        "requested_modules": result["requested_modules"],
        "initialized_modules": list(preparation.get("module_initialization", {})),
        "preparation_status": preparation["status"],
        "mes_state_unchanged": asdict(before) == asdict(after),
        "mes_version": after.version, "execution_authorized": False},
        "observation": result,
        "scope": {"input": "current state and past progress observations; no future downtime answer",
                  "prediction": "LLM-generated risk hypotheses and module requests, not calibrated probabilities",
                  "validation": "references, timestamps, modules and factory constraints; not forecast correctness",
                  "execution": "read-only preparation, no solver decision or MES writeback"}}


def display_llm_monitor(report):
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    call = report["observation"].get("prediction_call") or {}
    if call.get("abstention_reason"):
        print(f"LLM abstention: {call['abstention_reason']}")
    for risk in report["observation"]["risks"]:
        print(f"\nLLM hypothesis: {risk['kind']} / {risk['machine_id']}")
        print(f"  estimated_impact=[{risk['earliest_impact_minute']}, {risk['latest_impact_minute']}]; "
              f"uncertainty={risk['uncertainty']}; evidence={risk['evidence_ids']}")
        print(f"  rationale: {risk['rationale']}")
        for request in risk["module_requests"]:
            print(f"  prepare {request['module']}: {request['purpose']}")
    initialized = report["observation"]["preparation"].get("module_initialization", {})
    for module, state in initialized.items():
        print(f"  {module}: {state}")

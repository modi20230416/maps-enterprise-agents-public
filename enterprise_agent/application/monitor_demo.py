"""Read-only, single-epoch observation replay for Orchestrate monitoring."""
import json
from dataclasses import asdict
from pathlib import Path

from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.agents.prediction import KnownCalendarLookahead
from enterprise_agent.domain.contracts import MapsEvent
from enterprise_agent.infrastructure.mes import InMemoryMES, fingerprint, parse_baseline


def run_monitor_demo(path, on_step=None):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    mes = InMemoryMES(parse_baseline(data["mes"]))
    before = mes.read_baseline()
    engine = OrchestrateAgent(mes, mes, prediction=KnownCalendarLookahead())
    results = []
    for index, item in enumerate(data["observations"], 1):
        observation = engine.monitor(MapsEvent.parse(item["event"]), initialize=True)
        result = {"step": index, "label": item["label"], **observation}
        results.append(result)
        if on_step and on_step(result) is False:
            break
    after = mes.read_baseline()
    prepared = [r["preparation"] for r in results if r["preparation"]["status"] == "PREPARED"]
    return {
        "summary": {
            "observations_processed": len(results),
            "replay_complete": len(results) == len(data["observations"]),
            "statuses": [r["status"] for r in results],
            "preparation_requests": len(prepared),
            "preparation_builds": engine.planning.optimizer.preparation_builds,
            "preparation_reuses": sum(p["planning"]["cache_hit"] for p in prepared),
            "mes_version_before": before.version, "mes_version_after": after.version,
            "mes_state_unchanged": asdict(before) == asdict(after),
            "baseline_plan_unchanged": fingerprint(before.plan.to_dict()) == fingerprint(after.plan.to_dict()),
            "execution_authorized": False},
        "steps": results,
        "scope": {
            "input": "synthetic normalized observations against an authoritative MES baseline",
            "clock": "all observations use the same snapshot.now; sequence is replay order",
            "forecast": "deterministic conflict checking of known future device windows",
            "execution": "monitoring and context preparation only; no optimization, approval or writeback"}}


def display_step(step):
    print(f"\n[{step['step']}] {step['label']} | {step['status']}")
    state = step.get("monitored_state", {})
    print(f"MES v{step['mes_version']}; now={step['now']}; "
          f"machines={state.get('machine_count', 0)}; work_orders={state.get('work_order_count', 0)}")
    for risk in step["risks"]:
        print(f"  risk: {risk['machine_id']}/{risk['order_id']}; "
              f"assignment={risk['assignment_window']}; unavailable={risk['unavailable_window']}; "
              f"overlap={risk['overlap_minutes']} min; lead={risk['lead_minutes']} min")
    print(f"  scope={step['impact_scope']}; should_prepare={step['should_prepare']}")
    preparation = step["preparation"]
    print(f"  preparation={preparation['status']}; execution_authorized=False")
    if preparation["status"] == "PREPARED":
        print("  Prediction -> Orchestrate -> downstream context initialization")
        for module, state in preparation["module_initialization"].items():
            counts = {k: v for k, v in state.items() if k.endswith("_count")}
            print(f"  {module}: {state['status']}; content={state['content']}; {counts}")
        audit = preparation["planning"]
        print(f"  prepared_key={audit['prepared_key'][:16]}; "
              f"cache_hit={audit['cache_hit']}; builds={audit['preparation_builds']}")

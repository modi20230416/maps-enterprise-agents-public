from __future__ import annotations
from time import perf_counter
from enterprise_agent.domain.models import Event, Snapshot
from enterprise_agent.experiments.rule_orchestrator import RuleExperimentOrchestrator
from enterprise_agent.experiments.store import StateStore

def run_scenario(data: dict, store: StateStore, session_id: str, engine=None) -> dict:
    snapshot = Snapshot.parse(data["snapshot"])
    events = [Event.parse(e) for e in data["events"]]
    engine = engine or RuleExperimentOrchestrator()
    initial = store.initialize(session_id, snapshot, engine)
    decisions = []
    for event in events:
        started = perf_counter()
        result = store.process(session_id, event, engine)
        decisions.append({**result, "observed_seconds": round(perf_counter() - started, 6)})
    final_snapshot, final_plan, version = store.read(session_id)
    return {"session_id": session_id, "description": data.get("description", ""),
            "initial_plan": initial.to_dict(), "decisions": decisions,
            "final_snapshot": final_snapshot.to_dict(), "final_plan": final_plan.to_dict(),
            "version": version, "mode": "offline_simulation", "time_unit": "minute"}

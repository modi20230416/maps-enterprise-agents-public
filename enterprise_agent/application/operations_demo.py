"""Executable key-technology validation, without simulated factory clock claims."""
import json
from pathlib import Path
from time import perf_counter

from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.agents.planning import DataCollect, QuickRespond, PlanningAgent
from enterprise_agent.capabilities.local_search import LocalSearchSolver
from enterprise_agent.capabilities.validation import validate_candidate
from enterprise_agent.domain.contracts import MapsEvent
from enterprise_agent.infrastructure.mes import InMemoryMES, parse_baseline
from enterprise_agent.infrastructure.review import ReviewBoard


def run_operations_demo(path, review_mode="manual", proposer=None):
    if review_mode not in {"manual", "pending", "scripted"}:
        raise ValueError("unknown review mode")
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    baseline = parse_baseline(data["mes"])
    event, notice = MapsEvent.parse(data["event"]), MapsEvent.parse(data["notice"])

    def manual(request):
        print(f"\nReview {request['module']}: MES v{request['summary']['mes_version']}, "
              f"scope={request['summary']['scope']}")
        print(json.dumps(request["summary"]["plan"], ensure_ascii=False, indent=2))
        print(json.dumps(request["summary"]["module_output"], ensure_ascii=False, indent=2))
        reviewer = input("Local reviewer name (blank leaves pending): ").strip()
        if not reviewer:
            return None
        answer = input("Approve this module output? [y/n, blank pending]: ").strip().lower()
        if answer not in {"y", "n"}:
            return None
        return {"approved": answer == "y", "reviewer": reviewer}

    scripted = lambda req: {"approved": True, "reviewer": "SIMULATED_PROTOCOL_FIXTURE"}
    board = ReviewBoard(manual if review_mode == "manual" else scripted if review_mode == "scripted" else None,
                        simulated=review_mode == "scripted")
    mes = InMemoryMES(baseline, reviews=board, allow_simulated_reviews=review_mode == "scripted")
    engine = OrchestrateAgent(mes, mes, reviews=board, proposer=proposer)

    context = DataCollect().collect(baseline, event)
    gate = QuickRespond().assess(context)
    package = PlanningAgent().prepare(context, gate.scope)
    cold = LocalSearchSolver()
    cold_started = perf_counter()
    cold_candidate = cold.solve(context, package)
    cold_seconds = perf_counter() - cold_started

    preparation = engine.prepare(notice)
    version_after_preparation = mes.read_baseline().version
    result = engine.process(event)
    consumed = next((m["payload"] for m in result.trace if m["kind"] == "preparation.consumed"), {})
    resource = next((m["payload"] for m in result.trace if m["kind"] == "resources.consumed"), {})
    warehouse = next((m["payload"] for m in result.trace if m["kind"] == "readiness.context_consumed"), {})
    warm_assignments = result.plan["assignments"] if result.plan else None
    same = warm_assignments == cold_candidate.plan.to_dict()["assignments"]
    return {"summary": {
                "status": result.status, "review_mode": review_mode,
                "simulated_approvals": board.simulated,
                "preparation_status": preparation["status"],
                "lead_minutes": max((r["lead_minutes"] for r in preparation.get("risks", [])), default=0),
                "mes_version_after_preparation": version_after_preparation,
                "mes_final_version": mes.read_baseline().version,
                "planning_preparation_reused": consumed.get("cache_hit", False),
                "scheduling_context_reused": resource.get("cache_hit", False),
                "warehouse_context_reused": warehouse.get("cache_hit", False),
                "same_assignments_as_cold_solver": same,
                "module_reviews": {r["module"]: r["status"] for r in board.requests.values()}},
            "preparation": preparation, "decision": result.to_dict(),
            "cold_solver": {"seconds": cold_seconds, "plan": cold_candidate.plan.to_dict(),
                            "validation_errors": validate_candidate(context, cold_candidate.plan, gate.scope)},
            "limitations": ["known-calendar lookahead, not trained failure prediction",
                            "single decision epoch; notice precedes confirmation in call order, no factory clock simulation",
                            "one small fixture; timing is descriptive, not an industrial speedup claim",
                            "local review ledger has no enterprise authentication or durable storage",
                            "read-only initialization never authorizes execution"]}

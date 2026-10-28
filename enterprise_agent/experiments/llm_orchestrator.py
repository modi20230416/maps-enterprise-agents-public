"""LLM-driven planning with actual tools and validated plan selection."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from enterprise_agent.infrastructure.llm import LLMClient, LLMError
from enterprise_agent.experiments.llm_tools import run_tool_loop
from enterprise_agent.domain.models import Event, Plan, Snapshot
from enterprise_agent.capabilities.collection import EventCollector
from enterprise_agent.experiments.rule_orchestrator import RuleExperimentOrchestrator, ResponseAgent
from enterprise_agent.capabilities.prediction import KnownRiskPredictor
from enterprise_agent.capabilities.scheduling import measure, score, validate_plan
from enterprise_agent.infrastructure.trace import TraceLog
from enterprise_agent.capabilities.materials import MaterialAllocator


class PlanningTools:
    def __init__(self, snapshot: Snapshot, baseline: Plan, event: Event, optimizer,
                 drl_checkpoint: str | None = None):
        self.snapshot, self.baseline, self.event = snapshot, baseline, event
        self.optimizer = optimizer
        self.drl_checkpoint = drl_checkpoint
        self.bus = TraceLog(event.id)
        self.bus.send("collection", "orchestrator", "state.updated", {
            "event_id": event.id, "kind": event.kind, "now": snapshot.now})
        self.risks = None
        self.assessment = None
        self.candidates = {}
        self.candidate_summaries = []
        self.selection = None
        self.before = measure(snapshot, baseline, baseline)

    def execute(self, name: str, args: dict) -> dict:
        if self.selection:
            raise ValueError("decision already finalized")
        if name == "inspect_state":
            return {"ok": True, "snapshot": self.snapshot.to_dict(),
                    "baseline": self.baseline.to_dict()}
        if name == "forecast_risks":
            self.risks = KnownRiskPredictor.predict(self.snapshot, self.baseline)
            self.bus.send("prediction", "orchestrator", "risk.forecast", {"risks": self.risks})
            return {"ok": True, "risks": self.risks}
        if name == "assess_impact":
            self.assessment = ResponseAgent.assess(self.snapshot, self.baseline, self.event)
            self.bus.send("response", "orchestrator", "change.assessed", self.assessment)
            return {"ok": True, **self.assessment,
                    "note": "This rule assessment is evidence; the LLM may explore improvements."}
        if name == "query_materials":
            order = next((o for o in self.snapshot.orders if o.id == args["order_id"]), None)
            if order is None:
                raise ValueError("unknown order")
            quote_bus = TraceLog(self.event.id)
            ready, allocations, reason = MaterialAllocator(self.snapshot, quote_bus).reserve(order)
            return {"ok": True, "order_id": order.id, "ready": ready, "allocations": allocations,
                    "shortage_reason": reason,
                    "scope": "isolated quote; shared competition checked by build_schedule"}
        if name == "build_schedule":
            return self.build(args["strategy"])
        if name == "compare_plans":
            return {"ok": True, "baseline_feasible": not validate_plan(self.snapshot, self.baseline),
                    "baseline_metrics": self.before, "baseline_score": list(score(self.before)),
                    "candidates": [{"candidate_id": key, "metrics": value[0].metrics,
                                    "score": list(score(value[0].metrics))}
                                   for key, value in self.candidates.items()]}
        if name == "finalize_decision":
            return self.finalize(args)
        raise ValueError("unknown tool")

    def build(self, strategy: str) -> dict:
        if len(self.candidates) >= 6:
            raise ValueError("candidate proposal limit reached")
        self.bus.send("llm_planning", "optimization", "plan.requested", {"strategy": strategy})
        if strategy == "search":
            plan, messages, summaries = self.optimizer.solve(self.snapshot, self.baseline, self.event.id)
        elif strategy == "drl":
            if not self.drl_checkpoint:
                return {"ok": False, "error": "No trained DRL checkpoint configured; use search or a rule."}
            from enterprise_agent.experiments.dqn import dispatch_with_checkpoint
            try:
                plan = dispatch_with_checkpoint(self.snapshot, self.drl_checkpoint, self.baseline)
            except (ImportError, RuntimeError, ValueError, OSError):
                return {"ok": False, "error": "DRL runtime/checkpoint unavailable or incompatible; use search."}
            messages, summaries = [], [{"strategy": "drl", "metrics": plan.metrics}]
        else:
            seq = next(seq for name, seq in self.optimizer.dispatch_sequences(self.snapshot)
                       if name == strategy) if strategy == "priority_edd" else None
            # Rules are defined independently of sequence deduplication.
            if strategy == "edd":
                seq = tuple(sorted(self.snapshot.orders, key=lambda o: (o.due, o.id)))
            elif strategy == "weighted_slack":
                seq = tuple(sorted(self.snapshot.orders, key=lambda o: (
                    (o.due - max(self.snapshot.now, o.release) - sum(p.duration for p in o.operations))
                    / o.priority, -o.priority, o.id)))
            if seq is None:
                raise ValueError("invalid scheduling strategy")
            plan, messages = self.optimizer.construct(self.snapshot, seq, strategy, self.event.id)
            plan.metrics = measure(self.snapshot, plan, self.baseline)
            summaries = [{"strategy": strategy, "order_sequence": [o.id for o in seq],
                          "score": list(score(plan.metrics)), "metrics": plan.metrics}]
        errors = validate_plan(self.snapshot, plan)
        if errors:
            raise RuntimeError("scheduling tool produced an invalid plan")
        candidate_id = f"candidate-{len(self.candidates) + 1}"
        self.candidates[candidate_id] = (deepcopy(plan), deepcopy(messages))
        self.candidate_summaries.extend(summaries)
        return {"ok": True, "candidate_id": candidate_id, "strategy": strategy,
                "metrics": plan.metrics, "score": list(score(plan.metrics)),
                "validation_errors": [], "blocked": plan.blocked,
                "reservations": plan.reservations, "evaluated_sequences": len(summaries)}

    def finalize(self, args: dict) -> dict:
        if self.risks is None or self.assessment is None:
            raise ValueError("forecast_risks and assess_impact required before finalization")
        if not args["rationale"].strip() or not args["evidence"]:
            raise ValueError("brief rationale and operational evidence are required")
        if args["action"] == "KEEP":
            if args["candidate_id"] is not None or validate_plan(self.snapshot, self.baseline):
                raise ValueError("cannot keep an invalid baseline")
            plan, messages = deepcopy(self.baseline), []
            plan.metrics = self.before
            warehouse = MaterialAllocator(self.snapshot, self.bus)
            starts = {a.order_id: a.start for a in plan.assignments
                      if any(o.id == a.order_id and o.operations[0].id == a.operation_id
                             for o in self.snapshot.orders)}
            reservations = []
            for order in sorted((o for o in self.snapshot.orders if o.id in starts),
                                key=lambda o: (starts[o.id], o.id)):
                ready, allocations, _ = warehouse.reserve(order)
                if ready is None or ready > starts[order.id]:
                    raise ValueError("retained plan material reconciliation failed")
                reservations.extend({**a, "consume_at": starts[order.id]} for a in allocations)
            plan.reservations = reservations
        else:
            if args["candidate_id"] not in self.candidates:
                raise ValueError("unknown candidate")
            plan, messages = self.candidates[args["candidate_id"]]
            if self.assessment["baseline_feasible"] and score(plan.metrics) > score(self.before):
                raise ValueError("candidate worsens feasible baseline objective")
        errors = validate_plan(self.snapshot, plan)
        if errors:
            raise ValueError("selected plan failed validation")
        self.selection = {"plan": deepcopy(plan), "messages": messages, **args}
        return {"ok": True, "action": args["action"], "candidate_id": args["candidate_id"],
                "metrics": plan.metrics, "blocked": plan.blocked, "validation_errors": []}


class LLMExperimentOrchestrator(RuleExperimentOrchestrator):
    def __init__(self, client: LLMClient, drl_checkpoint: str | None = None):
        super().__init__()
        self.client = client
        self.drl_checkpoint = drl_checkpoint

    def process(self, snapshot: Snapshot, baseline: Plan, event: Event):
        updated = EventCollector.apply(snapshot, event)
        session = PlanningTools(updated, baseline, event, self.optimizer, self.drl_checkpoint)
        context = {"mode": "single_epoch_offline_simulation", "event": asdict(event),
                   "snapshot": updated.to_dict(), "baseline": baseline.to_dict(),
                   "drl_checkpoint_available": bool(self.drl_checkpoint),
                   "task": "Use tools to assess, negotiate schedule candidates, and finalize a decision."}
        try:
            audit = run_tool_loop(self.client, context, session.execute)
        except LLMError as exc:
            audit = {"status": "fallback", "error": str(exc), "tool_trace": []}
        if audit["status"] != "completed" or session.selection is None:
            updated, plan, result = super().process(snapshot, baseline, event)
            result["agent_mode"] = "rules_fallback"
            result["llm"] = audit
            return updated, plan, result
        selection = session.selection
        plan = selection["plan"]
        for message in selection["messages"]:
            session.bus.send(message["sender"], message["receiver"], message["kind"], message["payload"])
        if selection["action"] == "REPLAN":
            session.bus.send("orchestrator", "warehouse", "reservation.selected", {
                "reservations": plan.reservations, "scope": "local simulation; no ERP/WMS writeback"})
        session.bus.send("llm_planning", "orchestrator", "decision.proposed", {
            "action": selection["action"], "candidate_id": selection["candidate_id"],
            "rationale": selection["rationale"], "evidence": selection["evidence"]})
        session.bus.send("orchestrator", "execution", "plan.selected", {
            "action": selection["action"], "status": "simulation_only", "blocked": plan.blocked})
        return updated, plan, {
            "event": {"id": event.id, "kind": event.kind, "at": event.at},
            "action": selection["action"], "reason": selection["rationale"],
            "assessment": session.assessment, "risks": session.risks,
            "before_metrics": session.before, "before_feasible": session.assessment["baseline_feasible"],
            "after_metrics": plan.metrics, "validation_errors": [], "plan": plan.to_dict(),
            "messages": session.bus.messages, "candidate_count": len(session.candidate_summaries),
            "candidates": session.candidate_summaries, "agent_mode": "llm", "llm": audit}

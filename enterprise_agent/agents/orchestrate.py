"""Coordinate ready production, prepare downstream context, and review changes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from threading import Lock
from time import monotonic

from enterprise_agent.infrastructure.llm import LLMError
from enterprise_agent.capabilities.scheduling import measure
from enterprise_agent.infrastructure.trace import TraceLog
from enterprise_agent.infrastructure.mes import fingerprint
from enterprise_agent.domain.contracts import DecisionResult, MapsEvent, MESPatch, RESPONSE_TARGET_SECONDS
from enterprise_agent.agents.planning import DataCollect, PlanningAgent, QuickRespond, expand_factory_scope
from enterprise_agent.domain.ports import DiagnosisProvider, MESReader, MESWriter, ProposalProvider, StaleMESVersion
from enterprise_agent.agents.proposals import RuleProposals
from enterprise_agent.agents.scheduling import SchedulingAgent
from enterprise_agent.capabilities.validation import safety_status, validate_candidate, validate_proposal
from enterprise_agent.capabilities.admission import admission_errors
from enterprise_agent.agents.prediction import KnownCalendarLookahead
from enterprise_agent.infrastructure.review import ReviewBoard, REVIEW_MODULES, decision_binding


class OrchestrateAgent:
    def __init__(self, reader: MESReader, writer: MESWriter, *,
                 proposer: ProposalProvider | None = None,
                 planning: PlanningAgent | None = None,
                 scheduling: SchedulingAgent | None = None,
                 diagnosis: DiagnosisProvider | None = None, prediction=None, reviews=None):
        self.reader, self.writer = reader, writer
        self.proposer = proposer or RuleProposals()
        self.planning = planning or PlanningAgent()
        self.scheduling = scheduling or SchedulingAgent()
        self.diagnosis = diagnosis
        # Legacy calendar scenarios use an explicitly labelled rules baseline.
        # The monitor-demo LLM entry injects LLMPrediction here.
        self.prediction = prediction or KnownCalendarLookahead()
        self.reviews = reviews or getattr(reader, "reviews", None) or ReviewBoard()
        self._results: dict[str, tuple[str, DecisionResult]] = {}
        self._lock = Lock()

    def process(self, event: MapsEvent) -> DecisionResult:
        # Per-instance serialization/idempotency; cross-instance CAS is in MES.
        with self._lock:
            signature = fingerprint(asdict(event))
            if event.event.id in self._results:
                prior, result = self._results[event.event.id]
                if prior != signature:
                    raise ValueError("event id reused with different content")
                return deepcopy(result)
            result = self._process(event)
            # Stale decisions are retryable against a fresh baseline.
            if result.status not in {"STALE", "PENDING_HITL"}:
                self._results[event.event.id] = signature, deepcopy(result)
            return result

    def prepare(self, notice: MapsEvent):
        """Read-only speculative preparation; no solver decision or MES write."""
        with self._lock:
            return self._prepare(notice)

    def monitor(self, event: MapsEvent, *, initialize=False):
        """Inspect an observation and optionally prepare context; never solve/write."""
        with self._lock:
            context, gate, observation = self._observe(event)
            observation["preparation"] = {"status": "SKIPPED"}
            if initialize and observation["should_prepare"]:
                observation["preparation"] = self._initialize(
                    context, gate, observation["risks"], observation["requested_modules"])
                bus = TraceLog(event.event.id)
                bus.messages = observation["trace"]
                bus.send("orchestrate", "audit", "monitor.preparation_completed",
                         observation["preparation"])
            return observation

    def _observe(self, event):
        baseline = self.reader.read_baseline()
        bus = TraceLog(event.event.id)
        bus.send("mes_reader", "orchestrate", "monitor.observed", {
            "event_id": event.event.id, "kind": event.event.kind,
            "object": event.monitored_object, "mes_version": baseline.version,
            "now": baseline.snapshot.now})
        observation = {
            "event_id": event.event.id, "event_kind": event.event.kind,
            "monitored_object": event.monitored_object,
            "mes_version": baseline.version, "now": baseline.snapshot.now,
            "status": "OUT_OF_SCOPE", "risks": [], "current_conflicts": [],
            "impact_scope": [], "should_prepare": False,
            "prediction_source": getattr(self.prediction, "source", "external"),
            "requested_modules": [],
            "execution_authorized": False, "trace": bus.messages}
        if event.event.kind in {"receipt.delayed", "inventory.corrected"}:
            return None, None, observation
        context = DataCollect().collect(baseline, event)
        snapshot = context.snapshot
        observation.update(
            monitored_state={"machine_count": len(snapshot.machines),
                             "work_order_count": len(context.work_orders),
                             "assignment_count": len(baseline.plan.assignments)},
            frozen_order_ids=sorted(context.frozen_ids),
            lookahead_end=snapshot.now + snapshot.lookahead_minutes)
        errors = admission_errors(context)
        if errors:
            observation.update(status="NOT_ADMITTED", errors=errors)
            bus.send("orchestrate", "human_review", "monitor.admission_rejected", {"errors": errors})
            return context, None, observation
        gate = QuickRespond().assess(context)
        machines = {m.id: m for m in snapshot.machines}
        current = []
        for assignment in baseline.plan.assignments:
            for start, end in machines[assignment.machine_id].unavailable:
                if start <= snapshot.now < end and assignment.start < end and assignment.end > max(start, snapshot.now):
                    current.append({"order_id": assignment.order_id,
                                    "machine_id": assignment.machine_id,
                                    "unavailable_window": [start, end]})
        try:
            risks = self.prediction.forecast(context)
        except (LLMError, ValueError, TypeError, KeyError, OSError):
            observation.update(status="PREDICTION_UNAVAILABLE", current_conflicts=current,
                               prediction_error="model call or forecast validation failed; no rules substitution")
            bus.send("prediction", "orchestrate", "monitor.prediction_unavailable", {
                "effect": "hold preparation", "rules_fallback": False})
            return context, gate, observation
        if observation["prediction_source"] == "llm":
            seeds = {oid for risk in risks for oid in risk["affected_order_ids"]}
            gate = replace(gate, scope=expand_factory_scope(context, seeds))
            requested = sorted({m["module"] for risk in risks for m in risk["module_requests"]})
            observation["prediction_call"] = getattr(self.prediction, "last_call", None)
        else:
            requested = ["planning", "scheduling", "warehouse"] if risks else []
        should_prepare = bool(risks) and not current and not event.safety_actions
        status = "CURRENT_CONFLICT" if current else "FORECAST_RISK" if risks else "NORMAL"
        observation.update(status=status, risks=risks, current_conflicts=current,
                           impact_scope=list(gate.scope), should_prepare=should_prepare,
                           requested_modules=requested)
        bus.send("prediction", "orchestrate", "monitor.lookahead_assessed", {
            "status": status, "risks": risks, "current_conflicts": current,
            "lookahead_end": observation["lookahead_end"],
            "source": observation["prediction_source"]})
        bus.send("orchestrate", "downstream_preparation", "monitor.preparation_decided", {
            "should_prepare": should_prepare, "scope": list(gate.scope),
            "execution_authorized": False})
        return context, gate, observation

    def _prepare(self, notice):
        if notice.event.kind in {"receipt.delayed", "inventory.corrected"}:
            return {"status": "OUT_OF_SCOPE", "risks": []}
        context = DataCollect().collect(self.reader.read_baseline(), notice)
        errors = admission_errors(context)
        if errors:
            return {"status": "NOT_ADMITTED", "errors": errors}
        risks = self.prediction.forecast(context)
        if not risks:
            return {"status": "NO_PREDICTED_CONFLICT", "risks": []}
        gate = QuickRespond().assess(context)
        return self._initialize(context, gate, risks)

    def _initialize(self, context, gate, risks, requested_modules=None):
        requested = set(["planning", "scheduling", "warehouse"] if requested_modules is None else requested_modules)
        if not requested or not requested <= {"planning", "scheduling", "warehouse"}:
            raise ValueError("invalid downstream preparation modules")
        # Scheduling's current preparation includes a Warehouse dependency.
        effective = requested | ({"warehouse"} if "scheduling" in requested else set())
        if "planning" in effective and not hasattr(self.planning.optimizer, "prepare"):
            return {"status": "PREPARATION_UNSUPPORTED", "risks": risks}
        audit, scheduling, states = {}, {}, {}
        try:
            if "planning" in effective:
                package = self.planning.prepare(context, gate.scope)
                audit = self.planning.optimizer.prepare(context, package)
                states["planning"] = {"status": "CONTEXT_READY", "content": "local_solver_problem",
                                      "work_order_count": len(package.work_orders),
                                      "fixed_assignment_count": audit["fixed_assignments"]}
        except ValueError as exc:
            return {"status": "PREPARATION_BLOCKED", "reason": str(exc),
                    "execution_authorized": False}
        # Warm actual resource maps, not fictitious model/solver latency.
        if "scheduling" in effective:
            scheduling = self.scheduling.prepare(context)
            states["scheduling"] = {"status": "CONTEXT_READY", "content": "resource_calendar",
                                    "resource_count": scheduling["resource_count"]}
        elif "warehouse" in effective:
            self.scheduling.warehouse.prepare(context)
        if "warehouse" in effective:
            states["warehouse"] = {"status": "CONTEXT_READY", "content": "current_order_index",
                                   "order_count": len(self.scheduling.warehouse._prepared_orders)}
        return {"status": "PREPARED", "risks": risks, "planning": audit,
                "scheduling": scheduling,
                "initialization_scope": "computation_context",
                "requested_modules": sorted(requested),
                "dependency_added_modules": sorted(effective - requested),
                "module_initialization": states,
                "mes_version": context.baseline.version, "execution_authorized": False}

    def _process(self, event: MapsEvent) -> DecisionResult:
        started = monotonic()
        baseline = self.reader.read_baseline()
        context = DataCollect().collect(baseline, event)
        bus = TraceLog(event.event.id)
        bus.send("data_collect", "orchestrate", "monitor.observed", {
            "object": event.monitored_object, "priority": event.priority.value,
            "mes_version": baseline.version, "source": "MES baseline + normalized event"})

        def finish(status, reason, version=None, plan=None, errors=None):
            bus.send("orchestrate", "audit", "decision.completed", {
                "status": status, "elapsed_seconds": round(monotonic() - started, 6),
                "response_target_seconds": RESPONSE_TARGET_SECONDS[event.priority],
                "target_is_sla_claim": False})
            return DecisionResult(event.event.id, status, baseline.version,
                                  baseline.version if version is None else version,
                                  reason, bus.messages, plan.to_dict() if plan else None, errors or [])

        gate = QuickRespond().assess(context)
        bus.send("quick_respond", "orchestrate", "gate.assessed", asdict(gate))
        if event.event.kind in {"receipt.delayed", "inventory.corrected"}:
            return finish("OUT_OF_SCOPE", "current route covers already-ready factory work")
        errors = admission_errors(context)
        if errors:
            return finish("NOT_ADMITTED", "production requirements must be confirmed upstream", errors=errors)
        if gate.elapsed_seconds >= gate.budget_seconds:
            return finish("ESCALATED", "gate exceeded its budget; no automatic writeback")
        safety = safety_status(context)
        if safety:
            bus.send("orchestrate", "human_review", "safety.review_required", {
                "actions": event.safety_actions, "status": safety[0],
                "requirements": ["verified human approval", "interruptible process",
                                 "tooling and materials ready", "validated resume plan"]})
            return finish(*safety)
        if not gate.need_replan:
            return finish("KEEP", gate.reasons[0], plan=baseline.plan)
        package = self.planning.prepare(context, gate.scope)
        evidence = self.prediction.forecast(context)
        bus.send("prediction", "orchestrate", "operations.forecast", {"risks": evidence})
        if self.diagnosis:
            try:
                observations = self.diagnosis.observations(context)
                if not isinstance(observations, list) or not all(isinstance(o, dict) for o in observations):
                    raise ValueError("invalid diagnosis observations")
                evidence.extend(observations)
                bus.send("diagnosis", "orchestrate", "diagnosis.evidence", {"observations": observations})
            except (ValueError, RuntimeError, OSError):
                bus.send("diagnosis", "orchestrate", "diagnosis.unavailable", {"effect": "continue deterministic evidence"})
        bus.send("planning", "orchestrate", "planning.work_package", {
            "scope": package.scope, "frozen": sorted(package.frozen_ids),
            "work_orders": [asdict(w) for w in package.work_orders]})

        def propose(items):
            try:
                proposal = self.proposer.propose(context, gate, items)
                validate_proposal(proposal, gate)
            except (LLMError, ValueError, TypeError, KeyError, OSError):
                proposal = RuleProposals().propose(context, gate, items)
                bus.send("orchestrate", "audit", "llm.fallback", {
                    "reason": "provider unavailable or invalid proposal", "fallback": "rules"})
            bus.send("orchestrate", "planning", "coordination.proposed", asdict(proposal))
            return proposal

        proposal = propose(evidence)
        if proposal.action == "ESCALATE":
            return finish("ESCALATED", proposal.rationale)
        try:
            planning_candidate = self.planning.optimize(context, package)
        except ValueError:
            return finish("ESCALATED", "fixed production work cannot be preserved under updated constraints")
        wo_windows = []
        for wo in package.work_orders:
            assignments = [a for a in planning_candidate.plan.assignments if a.order_id == wo.wo_id]
            if assignments:
                wo_windows.append({"mo_id": wo.mo_id, "wo_id": wo.wo_id,
                                   "start": min(a.start for a in assignments),
                                   "end": max(a.end for a in assignments)})
        bus.send("planning", "scheduling", "planning.optimized", {
            "solver": planning_candidate.solver, "work_order_windows": wo_windows,
            "scope": package.scope})
        candidate = self.scheduling.schedule(context, package, planning_candidate)
        for message in candidate.messages:
            bus.send(message["sender"], message["receiver"], message["kind"], message["payload"])
        errors = [*candidate.conflicts, *validate_candidate(context, candidate.plan, package.scope)]
        if errors:
            # Bounded negotiation: one direct round, then arbitration, never a
            # model-driven infinite retry or silent hard-constraint relaxation.
            conflict = {"kind": "coordination.conflict", "errors": sorted(set(errors))}
            bus.send("scheduling", "orchestrate", "coordination.conflict", conflict)
            arbitration = propose([*evidence, conflict])
            bus.send("orchestrate", "human_review", "arbitration.recorded", {
                "proposal": asdict(arbitration), "disposition": "hold MES baseline",
                "priority": event.priority.value})
            return finish("ESCALATED", "unresolved constraint/material conflict", errors=sorted(set(errors)))
        candidate.plan.metrics = measure(context.snapshot, candidate.plan, baseline.plan)
        binding = decision_binding(context, candidate.plan)
        review_ids = []
        role_outputs = {
            "orchestrate": {"risks": evidence, "proposal": asdict(proposal)},
            "planning": {"gate": {"scope": gate.scope, "reasons": gate.reasons},
                         "work_order_windows": wo_windows},
            "scheduling": {"assignments": candidate.plan.to_dict()["assignments"],
                           "solver": candidate.solver},
            "warehouse": {"confirmed_current_allocations": candidate.plan.reservations}}
        for module in REVIEW_MODULES:
            review = self.reviews.request(module, binding, {
                "event_id": event.event.id, "mes_version": baseline.version,
                "scope": list(package.scope), "plan": candidate.plan.to_dict(),
                "module_output": role_outputs[module],
                "note": "review this module's output; all approvals required for writeback"})
            bus.send(module, "human_review", "module.review", review)
            review_ids.append(review["id"])
            if review["status"] == "rejected":
                return finish("REJECTED", f"human reviewer rejected {module} output")
        if not self.reviews.verify(review_ids, binding, allow_simulated=True):
            return finish("PENDING_HITL", "all module outputs require human review", plan=candidate.plan)
        patch = MESPatch(f"{event.event.id}:v{baseline.version}", baseline.version,
                         context, package.scope, candidate.plan, tuple(review_ids))
        bus.send("orchestrate", "mes_gateway", "patch.validated", {
            "patch_id": patch.patch_id, "expected_version": baseline.version,
            "scope": package.scope, "solver": candidate.solver})
        try:
            version = self.writer.submit_patch(patch)
        except StaleMESVersion:
            return finish("STALE", "MES changed during decision; recollect before retrying")
        except (ValueError, OSError):
            return finish("WRITE_REJECTED", "MES gateway rejected patch; baseline not accepted as changed")
        bus.send("mes_gateway", "orchestrate", "patch.accepted", {"version": version})
        return finish("APPLIED", proposal.rationale, version=version, plan=candidate.plan)

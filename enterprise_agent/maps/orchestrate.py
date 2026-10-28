"""MAPS entrypoint: monitor, organize, arbitrate conflicts and validate writeback."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from threading import Lock
from time import monotonic

from ..llm import LLMError
from ..scheduler import measure
from ..warehouse import MessageBus
from .adapters import fingerprint
from .contracts import (DecisionResult, MapsEvent, MESPatch, RESPONSE_TARGET_SECONDS)
from .planning import DataCollect, PlanningAgent, QuickRespond
from .ports import DiagnosisProvider, MESReader, MESWriter, ProposalProvider, StaleMESVersion
from .proposals import RuleProposals
from .scheduling import SchedulingAgent
from .validation import safety_status, validate_candidate, validate_proposal


class OrchestrateAgent:
    def __init__(self, reader: MESReader, writer: MESWriter, *,
                 proposer: ProposalProvider | None = None,
                 planning: PlanningAgent | None = None,
                 scheduling: SchedulingAgent | None = None,
                 diagnosis: DiagnosisProvider | None = None):
        self.reader, self.writer = reader, writer
        self.proposer = proposer or RuleProposals()
        self.planning = planning or PlanningAgent()
        self.scheduling = scheduling or SchedulingAgent()
        self.diagnosis = diagnosis
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
            if result.status != "STALE":
                self._results[event.event.id] = signature, deepcopy(result)
            return result

    def _process(self, event: MapsEvent) -> DecisionResult:
        started = monotonic()
        baseline = self.reader.read_baseline()
        context = DataCollect().collect(baseline, event)
        bus = MessageBus(event.event.id)
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
        evidence = list(package.forecasts)
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
            "work_orders": [asdict(w) for w in package.work_orders], "forecasts": evidence})

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
        planning_candidate = self.planning.optimize(context, package)
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
        patch = MESPatch(f"{event.event.id}:v{baseline.version}", baseline.version,
                         context, package.scope, candidate.plan)
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

from __future__ import annotations
from enterprise_agent.capabilities.scheduling import measure, score
from enterprise_agent.domain.contracts import DecisionContext, ScheduleCandidate, WorkPackage
from enterprise_agent.domain.ports import ConstraintSolver, MARLPolicy

from enterprise_agent.capabilities.local_search import LocalSearchSolver
from enterprise_agent.agents.warehouse import WarehouseAgent
from enterprise_agent.domain.policy import ReplayQualification

class SchedulingAgent:
    def __init__(self, solver: ConstraintSolver | None = None,
                 policy: MARLPolicy | None = None, qualification: ReplayQualification | None = None):
        self.solver = solver or LocalSearchSolver()
        self.warehouse = WarehouseAgent()
        self.policy, self.qualification = policy, qualification
        if policy and (not qualification or not qualification.eligible(policy.policy_id)):
            raise ValueError("MARL requires matching, non-degrading replay evidence before activation")

    def schedule(self, context: DecisionContext, package: WorkPackage,
                 planning_candidate: ScheduleCandidate | None = None) -> ScheduleCandidate:
        result = planning_candidate if planning_candidate is not None else self.solver.solve(context, package)
        if self.policy and not result.conflicts:
            from enterprise_agent.capabilities.validation import validate_candidate
            try:
                learned = self.policy.propose(context, package)
                errors = validate_candidate(context, learned.plan, package.scope)
                learned.plan.metrics = measure(context.snapshot, learned.plan, context.baseline.plan)
                result.plan.metrics = measure(context.snapshot, result.plan, context.baseline.plan)
                if not errors and not learned.conflicts and score(learned.plan.metrics) <= score(result.plan.metrics):
                    result = learned
                else:
                    result.messages.append({"sender": "scheduling", "receiver": "orchestrate",
                                            "kind": "policy.rejected", "payload": {"reason": "invalid or degrading candidate"}})
            except (ValueError, RuntimeError, OSError):
                result.messages.append({"sender": "scheduling", "receiver": "orchestrate",
                                        "kind": "policy.fallback", "payload": {"reason": "policy unavailable"}})
        if not result.conflicts:
            allocations, messages, conflicts = self.warehouse.confirm(context, result.plan)
            result.plan.reservations = allocations
            result.messages.extend(messages)
            result.conflicts.extend(conflicts)
        return result

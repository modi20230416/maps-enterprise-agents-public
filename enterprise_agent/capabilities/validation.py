"""Independent acceptance checks for every proposal and MES patch."""

from enterprise_agent.domain.models import Plan
from enterprise_agent.capabilities.scheduling import measure, score, validate_plan
from enterprise_agent.domain.contracts import DecisionContext, GateDecision, Priority, Proposal


def safety_status(context: DecisionContext) -> tuple[str, str] | None:
    actions = context.event.safety_actions
    if "preempt" in actions and context.event.priority != Priority.EMERGENCY:
        return "REJECTED", "only emergency events may request preemption"
    if actions:
        # Phase one deliberately has no approval impersonation flag in the LLM
        # schema. A real approval identity + interruptibility/tooling/material
        # check and resume model are required before actuation can be implemented.
        return "PENDING_HITL", "safety action requires human approval and an execution adapter"
    return None


def validate_proposal(proposal: Proposal, gate: GateDecision) -> None:
    if proposal.action not in {"KEEP", "REPLAN", "ESCALATE"} or not proposal.rationale.strip():
        raise ValueError("invalid orchestration proposal")
    if set(proposal.scope) != set(gate.scope) or len(proposal.scope) != len(set(proposal.scope)):
        raise ValueError("proposal cannot widen or omit the rule-derived local scope")
    if proposal.action == "KEEP" and gate.need_replan:
        raise ValueError("LLM cannot suppress a mandatory gate trigger")


def validate_candidate(context: DecisionContext, plan: Plan, scope: tuple[str, ...]) -> list[str]:
    errors = validate_plan(context.snapshot, plan)
    known = {o.id for o in context.snapshot.orders}
    if not set(scope) <= known or set(scope).intersection(context.frozen_ids):
        errors.append("invalid or frozen work order in local scope")
    fixed_ids = known - set(scope)
    before = { (a.order_id, a.operation_id): a for a in context.baseline.plan.assignments
               if a.order_id in fixed_ids }
    after = { (a.order_id, a.operation_id): a for a in plan.assignments if a.order_id in fixed_ids }
    if before != after:
        errors.append("frozen or out-of-scope assignment changed")
    if {k: v for k, v in context.baseline.plan.blocked.items() if k in fixed_ids} != {
            k: v for k, v in plan.blocked.items() if k in fixed_ids}:
        errors.append("out-of-scope blocked state changed")
    metrics = measure(context.snapshot, plan, context.baseline.plan)
    if metrics["late_orders"]:
        errors.append("hard delivery deadline violated; escalate instead of silently relaxing")
    if plan.blocked:
        errors.append("unconfirmed work orders cannot be written back")
    if (not validate_plan(context.snapshot, context.baseline.plan)
            and score(metrics) > score(measure(context.snapshot, context.baseline.plan, context.baseline.plan))):
        errors.append("candidate degrades feasible MES baseline")
    return sorted(set(errors))

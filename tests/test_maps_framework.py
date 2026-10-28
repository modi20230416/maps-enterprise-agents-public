"""MAPS acceptance tests: authoritative MES, scope, WIP, approval and LLM guards."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from enterprise_agent.llm import LLMClient, LLMConfig, LLMError
from enterprise_agent.maps.adapters import InMemoryMES, parse_baseline
from enterprise_agent.maps.contracts import MapsEvent, MESPatch, Priority, Proposal
from enterprise_agent.maps.demo import run_demo
from enterprise_agent.maps.orchestrate import OrchestrateAgent
from enterprise_agent.maps.planning import DataCollect, PlanningAgent, QuickRespond
from enterprise_agent.maps.ports import StaleMESVersion
from enterprise_agent.maps.proposals import LLMProposals
from enterprise_agent.maps.scheduling import ReplayQualification, SchedulingAgent
from enterprise_agent.maps.validation import validate_candidate
from enterprise_agent.scheduler import validate_plan


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "maps_framework.json"


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.baseline = parse_baseline(self.data["mes"])
        self.event = MapsEvent.parse(self.data["event"])
        self.mes = InMemoryMES(self.baseline)

    def context_and_candidate(self):
        context = DataCollect().collect(self.baseline, self.event)
        gate = QuickRespond().assess(context)
        package = PlanningAgent().prepare(context, gate.scope)
        result = SchedulingAgent().schedule(context, package)
        return context, gate, result

    def patch(self):
        context, gate, candidate = self.context_and_candidate()
        return MESPatch("test-patch", 1, context, gate.scope, candidate.plan)

    def test_high_priority_replans_without_interrupting_wip_or_unrelated_work(self):
        result = OrchestrateAgent(self.mes, self.mes).process(self.event)
        self.assertEqual((result.status, result.mes_version), ("APPLIED", 2))
        current = self.mes.read_baseline()
        by_id = {a.order_id: a for a in current.plan.assignments}
        self.assertEqual((by_id["WO-RUN"].start, by_id["WO-RUN"].end), (0, 20))
        self.assertEqual((by_id["WO-OTHER"].start, by_id["WO-OTHER"].end), (80, 90))
        self.assertEqual((by_id["WO-HIGH"].start, by_id["WO-HIGH"].end), (20, 35))
        self.assertEqual(validate_plan(current.snapshot, current.plan), [])
        self.assertTrue(any(w.mo_id == "MO-300" and w.wo_id == "WO-HIGH" for w in current.work_orders))

    def test_warehouse_talks_directly_before_writeback_without_arbitration(self):
        result = OrchestrateAgent(self.mes, self.mes).process(self.event)
        kinds = [m["kind"] for m in result.trace]
        confirmation = next(m for m in result.trace if m["kind"] == "material.confirmed")
        self.assertEqual((confirmation["sender"], confirmation["receiver"]), ("warehouse", "scheduling"))
        self.assertLess(kinds.index("material.confirmed"), kinds.index("patch.accepted"))
        self.assertNotIn("arbitration.recorded", kinds)
        self.assertIn("planning.optimized", kinds)

    def test_keep_does_not_call_llm_or_increment_mes(self):
        class Never:
            def propose(self, *args):
                raise AssertionError("LLM must not run on no-change gate")
        tick = MapsEvent.parse({"id": "tick", "kind": "planning.tick", "at": 0, "priority": "low"})
        result = OrchestrateAgent(self.mes, self.mes, proposer=Never()).process(tick)
        self.assertEqual((result.status, result.mes_version), ("KEEP", 1))

    def test_nonemergency_preemption_rejected_at_every_priority(self):
        for priority in (Priority.HIGH, Priority.MEDIUM, Priority.LOW):
            result = OrchestrateAgent(self.mes, self.mes).process(
                replace(self.event, priority=priority, safety_actions=("preempt",)))
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(self.mes.read_baseline().version, 1)

    def test_emergency_and_changeover_wait_for_hitl_not_auto_execute(self):
        for priority, action in ((Priority.EMERGENCY, "preempt"), (Priority.HIGH, "changeover"),
                                 (Priority.MEDIUM, "process_parameter_change")):
            result = OrchestrateAgent(self.mes, self.mes).process(
                replace(self.event, priority=priority, safety_actions=(action,)))
            self.assertEqual((result.status, result.mes_version), ("PENDING_HITL", 1))

    def test_material_shortage_escalates_and_preserves_mes(self):
        result = run_demo(str(FIXTURE), "material_conflict")["decision"]
        self.assertEqual((result["status"], result["mes_version"]), ("ESCALATED", 1))
        self.assertIn("arbitration.recorded", [m["kind"] for m in result["trace"]])

    def test_unachievable_hard_due_date_does_not_silently_relax(self):
        event = deepcopy(self.data["event"])
        event["payload"]["order"]["due"] = 10
        result = OrchestrateAgent(self.mes, self.mes).process(MapsEvent.parse(event))
        self.assertEqual(result.status, "ESCALATED")
        self.assertTrue(any("hard delivery deadline" in error for error in result.errors))
        self.assertEqual(self.mes.read_baseline().version, 1)

    def test_candidate_cannot_move_wip_or_unrelated_assignment(self):
        context, gate, candidate = self.context_and_candidate()
        for oid in ("WO-RUN", "WO-OTHER"):
            plan = deepcopy(candidate.plan)
            plan.assignments = [replace(a, start=a.start + 1, end=a.end + 1)
                                if a.order_id == oid else a for a in plan.assignments]
            self.assertIn("frozen or out-of-scope assignment changed", validate_candidate(context, plan, gate.scope))

    def test_atomic_mes_cas_and_patch_idempotency(self):
        patch = self.patch()
        self.assertEqual(self.mes.submit_patch(patch), 2)
        self.assertEqual(self.mes.submit_patch(patch), 2)
        with self.assertRaises(StaleMESVersion):
            self.mes.submit_patch(replace(patch, patch_id="other-writer"))
        changed = deepcopy(patch)
        changed.plan.strategy = "different"
        with self.assertRaisesRegex(ValueError, "reused"):
            self.mes.submit_patch(changed)
        self.assertEqual(self.mes.read_baseline().version, 2)

    def test_gateway_rechecks_scope_and_frozen_context_against_mes(self):
        patch = self.patch()
        with self.assertRaisesRegex(ValueError, "scope"):
            self.mes.submit_patch(replace(patch, scope=(*patch.scope, "WO-RUN")))
        forged = deepcopy(patch)
        forged.context.work_orders = tuple(replace(w, status="planned") for w in forged.context.work_orders)
        with self.assertRaisesRegex(ValueError, "context"):
            self.mes.submit_patch(forged)
        self.assertEqual(self.mes.read_baseline().version, 1)

    def test_event_retry_does_not_write_twice_and_changed_payload_is_rejected(self):
        engine = OrchestrateAgent(self.mes, self.mes)
        first = engine.process(self.event)
        first.trace.clear()  # caller mutation must not corrupt cached audit
        again = engine.process(self.event)
        self.assertTrue(again.trace)
        self.assertEqual(self.mes.read_baseline().version, 2)
        with self.assertRaisesRegex(ValueError, "different content"):
            engine.process(replace(self.event, priority=Priority.LOW))

    def test_gateway_rejects_forged_material_confirmation(self):
        patch = self.patch()
        patch.plan.reservations[0]["quantity"] = 999
        with self.assertRaisesRegex(ValueError, "material confirmations"):
            self.mes.submit_patch(patch)
        self.assertEqual(self.mes.read_baseline().version, 1)

    def test_stale_write_is_retryable_without_false_success(self):
        class StaleWriter:
            def submit_patch(self, patch):
                raise StaleMESVersion("changed")
        engine = OrchestrateAgent(self.mes, StaleWriter())
        self.assertEqual(engine.process(self.event).status, "STALE")
        engine.writer = self.mes
        self.assertEqual(engine.process(self.event).status, "APPLIED")

    def test_priority_dictionary_and_unknown_event_rejected(self):
        event = deepcopy(self.data["event"])
        event["priority"] = "critical"
        with self.assertRaises(ValueError):
            MapsEvent.parse(event)
        event["priority"] = "high"
        event["kind"] = "run_shell"
        with self.assertRaises(ValueError):
            MapsEvent.parse(event)

    def test_all_four_monitor_objects_have_gate_paths(self):
        events = [self.event,
                  MapsEvent.parse({"id": "tick", "kind": "planning.tick", "at": 0, "priority": "low"}),
                  MapsEvent.parse({"id": "machine", "kind": "machine.unavailable", "at": 0,
                                   "priority": "high", "payload": {"machine_id": "M1", "start": 40, "end": 60}}),
                  MapsEvent.parse({"id": "inventory", "kind": "inventory.corrected", "at": 0,
                                   "priority": "medium", "payload": {"material": "steel", "quantity": 1}})]
        self.assertEqual({e.monitored_object for e in events},
                         {"plan_execution", "inserted_order", "equipment", "inventory"})
        for event in events:
            self.assertIsInstance(QuickRespond().assess(DataCollect().collect(self.baseline, event)).need_replan, bool)

    def llm_proposer(self, arguments=None, failure=False):
        def transport(url, payload, headers, timeout):
            if failure:
                raise LLMError("provider unavailable")
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "propose_coordination", "arguments": arguments}}]}}]}
        return LLMProposals(LLMClient(LLMConfig(model="protocol-fixture", api_key="", base_url="http://localhost",
                                               api="chat_completions"), transport))

    def test_llm_proposal_runs_deterministic_pipeline(self):
        args = json.dumps({"action": "REPLAN", "scope": ["WO-HIGH", "WO-NORMAL"],
                           "rationale": "保护在制品，局部插单"})
        result = OrchestrateAgent(self.mes, self.mes, proposer=self.llm_proposer(args)).process(self.event)
        self.assertEqual(result.status, "APPLIED")
        proposal = next(m["payload"] for m in result.trace if m["kind"] == "coordination.proposed")
        self.assertEqual(proposal["source"], "llm")

    def test_llm_cannot_widen_scope_override_gate_or_inject_approval(self):
        valid = {"action": "REPLAN", "scope": ["WO-HIGH", "WO-NORMAL"], "rationale": "test"}
        for bad in ({**valid, "scope": ["WO-RUN", "WO-HIGH", "WO-NORMAL"]},
                    {**valid, "action": "KEEP"}, {**valid, "approved": True}):
            result = OrchestrateAgent(self.mes, self.mes, proposer=self.llm_proposer(json.dumps(bad))).process(self.event)
            self.assertEqual(result.status, "APPLIED")
            self.assertIn("llm.fallback", [m["kind"] for m in result.trace])
            self.mes = InMemoryMES(self.baseline)

    def test_llm_failure_records_rules_fallback(self):
        result = OrchestrateAgent(self.mes, self.mes, proposer=self.llm_proposer(failure=True)).process(self.event)
        self.assertEqual(result.status, "APPLIED")
        self.assertIn("llm.fallback", [m["kind"] for m in result.trace])

    def test_marl_cannot_activate_without_non_degrading_matching_evidence(self):
        class Policy:
            policy_id = "marl-v1"
        zero = dict(blocked_priority=0, weighted_tardiness=0, schedule_churn_minutes=0)
        with self.assertRaises(ValueError):
            SchedulingAgent(policy=Policy())
        for evidence in (ReplayQualification("other", "simulation", 24, zero, zero),
                         ReplayQualification("marl-v1", "simulation", 0, zero, zero),
                         ReplayQualification("marl-v1", "simulation", 24, zero, {**zero, "weighted_tardiness": 1})):
            with self.assertRaises(ValueError):
                SchedulingAgent(policy=Policy(), qualification=evidence)

    def test_qualified_policy_still_cannot_change_frozen_assignments(self):
        context, gate, result = self.context_and_candidate()
        malicious = deepcopy(result)
        malicious.plan.assignments = [replace(a, start=1, end=21) if a.order_id == "WO-RUN" else a
                                      for a in malicious.plan.assignments]
        class Policy:
            policy_id = "fixture-policy"
            def propose(self, *args):
                return malicious
        zero = dict(blocked_priority=0, weighted_tardiness=0, schedule_churn_minutes=0)
        scheduling = SchedulingAgent(policy=Policy(), qualification=ReplayQualification(
            "fixture-policy", "simulation", 2, zero, zero))
        package = PlanningAgent().prepare(context, gate.scope)
        accepted = scheduling.schedule(context, package)
        self.assertEqual(validate_candidate(context, accepted.plan, gate.scope), [])
        self.assertIn("policy.rejected", [m["kind"] for m in accepted.messages])

    def test_cli_all_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            completed = subprocess.run([sys.executable, "-m", "enterprise_agent", "framework-demo",
                                        "--input", str(FIXTURE), "--output", str(output)],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual({k: v["decision"]["status"] for k, v in report.items()}, {
                "high_order": "APPLIED", "emergency_review": "PENDING_HITL",
                "material_conflict": "ESCALATED", "no_change": "KEEP"})


if __name__ == "__main__":
    unittest.main()

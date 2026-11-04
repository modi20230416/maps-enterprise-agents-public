import inspect
import json
from copy import deepcopy
from pathlib import Path
from dataclasses import replace
import unittest

from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.agents.planning import DataCollect, QuickRespond, PlanningAgent
from enterprise_agent.agents.prediction import KnownCalendarLookahead
from enterprise_agent.application.operations_demo import run_operations_demo
from enterprise_agent.capabilities.local_search import LocalSearchSolver
from enterprise_agent.domain.contracts import MapsEvent, MESPatch
from enterprise_agent.infrastructure.mes import InMemoryMES, parse_baseline
from enterprise_agent.infrastructure.review import ReviewBoard, REVIEW_MODULES, decision_binding

FIXTURE = Path(__file__).resolve().parents[1] / "examples/operations_ready.json"


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.baseline = parse_baseline(self.data["mes"])
        self.event = MapsEvent.parse(self.data["event"])
        self.notice = MapsEvent.parse(self.data["notice"])

    def test_preparation_reused_without_changing_the_solution(self):
        report = run_operations_demo(str(FIXTURE), "scripted")
        s = report["summary"]
        self.assertEqual(s["status"], "APPLIED")
        self.assertEqual(s["mes_version_after_preparation"], 1)
        self.assertEqual(s["lead_minutes"], 30)
        for name in ("planning_preparation_reused", "scheduling_context_reused",
                     "warehouse_context_reused", "same_assignments_as_cold_solver"):
            self.assertTrue(s[name])
        self.assertEqual(report["cold_solver"]["validation_errors"], [])
        self.assertEqual(len(s["module_reviews"]), 4)
        self.assertTrue(s["simulated_approvals"])

    def test_planning_has_no_prediction_dependency(self):
        self.assertNotIn("prediction", inspect.signature(PlanningAgent).parameters)
        source = inspect.getsource(PlanningAgent)
        self.assertNotIn("forecast", source)
        ctx = DataCollect().collect(self.baseline, self.event)
        self.assertEqual({r["kind"] for r in KnownCalendarLookahead().forecast(ctx)},
                         {"future_capacity_conflict"})

    def test_proposal_provider_receives_factory_risks_without_approval_authority(self):
        from enterprise_agent.agents.proposals import RuleProposals
        class RecordingProvider:
            def propose(self, context, gate, evidence):
                self.evidence = evidence
                return RuleProposals().propose(context, gate, evidence)
        provider = RecordingProvider()
        report = run_operations_demo(str(FIXTURE), "pending", provider)
        self.assertEqual(provider.evidence[0]["source"], "known_future_calendar")
        self.assertEqual(report["summary"]["status"], "PENDING_HITL")
        self.assertEqual(report["summary"]["mes_final_version"], 1)

    def test_default_missing_approvals_block_all_writeback(self):
        report = run_operations_demo(str(FIXTURE), "pending")
        self.assertEqual(report["summary"]["status"], "PENDING_HITL")
        self.assertEqual(report["summary"]["mes_final_version"], 1)
        self.assertEqual(set(report["summary"]["module_reviews"].values()), {"pending"})

    def test_every_module_can_reject(self):
        for denied in REVIEW_MODULES:
            board = ReviewBoard(lambda req: {"approved": req["module"] != denied,
                                             "reviewer": "SIMULATED_TEST"}, simulated=True)
            mes = InMemoryMES(self.baseline, reviews=board, allow_simulated_reviews=True)
            result = OrchestrateAgent(mes, mes).process(self.event)
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(mes.read_baseline().version, 1)

    def test_pending_can_be_reviewed_then_retried(self):
        board = ReviewBoard()
        mes = InMemoryMES(self.baseline, reviews=board)
        engine = OrchestrateAgent(mes, mes)
        self.assertEqual(engine.process(self.event).status, "PENDING_HITL")
        for key in list(board.requests):
            board.decide(key, approved=True, reviewer="local-test-reviewer")
        self.assertEqual(engine.process(self.event).status, "APPLIED")
        self.assertEqual(mes.read_baseline().version, 2)

    def test_cached_context_invalidates_on_different_window_or_version(self):
        ctx = DataCollect().collect(self.baseline, self.notice)
        gate = QuickRespond().assess(ctx)
        package = PlanningAgent().prepare(ctx, gate.scope)
        solver = LocalSearchSolver()
        self.assertFalse(solver.prepare(ctx, package)["cache_hit"])
        self.assertTrue(solver.prepare(ctx, package)["cache_hit"])
        changed = replace(self.event.event, payload={**self.event.event.payload, "end": 46})
        other = DataCollect().collect(self.baseline, replace(self.event, event=changed))
        self.assertFalse(solver.prepare(other, package)["cache_hit"])
        new_baseline = deepcopy(self.baseline)
        new_baseline.version += 1
        newer = DataCollect().collect(new_baseline, self.notice)
        self.assertFalse(solver.prepare(newer, package)["cache_hit"])

    def test_non_ready_work_not_admitted_even_if_the_scheduler_could_wait(self):
        bad = deepcopy(self.data["mes"])
        bad["work_orders"][1]["production_ready"] = False
        mes = InMemoryMES(parse_baseline(bad))
        engine = OrchestrateAgent(mes, mes)
        self.assertEqual(engine.process(self.event).status, "NOT_ADMITTED")
        self.assertEqual(engine.prepare(self.notice)["status"], "NOT_ADMITTED")
        self.assertEqual(mes.read_baseline().version, 1)

    def test_unrelated_future_receipts_do_not_generate_inventory_predictions(self):
        other = deepcopy(self.data["mes"])
        other["snapshot"]["receipts"] = [{"id": "unused-receipt", "material": "steel", "quantity": 99, "eta": 900}]
        ctx = DataCollect().collect(parse_baseline(other), self.event)
        risks = KnownCalendarLookahead().forecast(ctx)
        self.assertTrue(risks)
        self.assertFalse(any("material" in r["kind"] or "inventory" in r["kind"] for r in risks))

    def test_gateway_rejects_missing_reviews_or_reviews_for_another_plan(self):
        board = ReviewBoard(lambda req: {"approved": True, "reviewer": "SIMULATED_TEST"}, simulated=True)
        mes = InMemoryMES(self.baseline, reviews=board, allow_simulated_reviews=True)
        ctx = DataCollect().collect(self.baseline, self.event)
        gate = QuickRespond().assess(ctx)
        package = PlanningAgent().prepare(ctx, gate.scope)
        from enterprise_agent.agents.scheduling import SchedulingAgent
        candidate = SchedulingAgent().schedule(ctx, package)
        patch = MESPatch("missing", 1, ctx, gate.scope, candidate.plan)
        with self.assertRaisesRegex(ValueError, "human review"):
            mes.submit_patch(patch)
        ids = tuple(board.request(m, "another-plan", {})["id"] for m in REVIEW_MODULES)
        with self.assertRaisesRegex(ValueError, "human review"):
            mes.submit_patch(replace(patch, patch_id="stale-review", review_ids=ids))

    def test_simulated_approvals_need_explicit_simulation_gateway(self):
        board = ReviewBoard(lambda req: {"approved": True, "reviewer": "SIMULATED_TEST"}, simulated=True)
        mes = InMemoryMES(self.baseline, reviews=board)
        self.assertEqual(OrchestrateAgent(mes, mes).process(self.event).status, "WRITE_REJECTED")


if __name__ == "__main__":
    unittest.main()

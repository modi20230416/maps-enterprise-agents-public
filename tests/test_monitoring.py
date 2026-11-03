import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.application.monitor_demo import run_monitor_demo
from enterprise_agent.domain.contracts import MapsEvent
from enterprise_agent.infrastructure.mes import InMemoryMES, parse_baseline

FIXTURE = Path(__file__).resolve().parents[1] / "examples/orchestrate_monitor.json"


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.mes = InMemoryMES(parse_baseline(self.data["mes"]))
        self.engine = OrchestrateAgent(self.mes, self.mes)

    def event(self, index=1):
        return MapsEvent.parse(self.data["observations"][index]["event"])

    def test_baseline_monitoring_skips_preparation(self):
        observation = self.engine.monitor(self.event(0), initialize=True)
        self.assertEqual(observation["status"], "NORMAL")
        self.assertEqual(observation["risks"], [])
        self.assertEqual(observation["preparation"]["status"], "SKIPPED")
        self.assertEqual(observation["monitored_state"]["machine_count"], 2)

    def test_risk_proof_and_scope_are_explicit(self):
        result = self.engine.monitor(self.event(), initialize=True)
        self.assertEqual(result["status"], "FORECAST_RISK")
        self.assertEqual(result["impact_scope"], ["WO-NORMAL"])
        risk, = result["risks"]
        self.assertEqual(risk["assignment_window"], [20, 50])
        self.assertEqual(risk["unavailable_window"], [30, 45])
        self.assertEqual(risk["overlap_minutes"], 15)
        self.assertEqual(risk["lead_minutes"], 30)
        self.assertEqual(result["preparation"]["status"], "PREPARED")
        self.assertEqual([m["sequence"] for m in result["trace"]], [1, 2, 3, 4])

    def test_monitor_does_not_initialize_without_explicit_option(self):
        result = self.engine.monitor(self.event())
        self.assertTrue(result["should_prepare"])
        self.assertEqual(result["preparation"]["status"], "SKIPPED")
        self.assertEqual(self.engine.planning.optimizer.preparation_builds, 0)

    def test_prediction_evidence_triggers_actual_preparation_of_three_modules(self):
        with patch.object(self.engine.prediction, "forecast", return_value=[]), \
             patch.object(self.engine.planning.optimizer, "prepare") as planning:
            result = self.engine.monitor(self.event(), initialize=True)
            planning.assert_not_called()
            self.assertEqual(result["preparation"]["status"], "SKIPPED")
        with patch.object(self.engine.planning.optimizer, "prepare", wraps=self.engine.planning.optimizer.prepare) as planning, \
             patch.object(self.engine.scheduling, "prepare", wraps=self.engine.scheduling.prepare) as scheduling, \
             patch.object(self.engine.scheduling.warehouse, "prepare", wraps=self.engine.scheduling.warehouse.prepare) as warehouse:
            result = self.engine.monitor(self.event(), initialize=True)
            planning.assert_called_once()
            scheduling.assert_called_once()
            warehouse.assert_called_once()
        initialized = result["preparation"]["module_initialization"]
        self.assertEqual(set(initialized), {"planning", "scheduling", "warehouse"})
        self.assertEqual({m["status"] for m in initialized.values()}, {"CONTEXT_READY"})
        self.assertEqual(initialized["planning"]["work_order_count"], 1)
        self.assertEqual(initialized["scheduling"]["resource_count"], 2)
        self.assertEqual(initialized["warehouse"]["order_count"], 3)

    def test_repeated_notice_reuses_and_changed_window_rebuilds(self):
        first = self.engine.monitor(self.event(1), initialize=True)["preparation"]["planning"]
        repeat = self.engine.monitor(self.event(2), initialize=True)["preparation"]["planning"]
        changed = self.engine.monitor(self.event(3), initialize=True)["preparation"]["planning"]
        self.assertFalse(first["cache_hit"])
        self.assertTrue(repeat["cache_hit"])
        self.assertEqual(first["prepared_key"], repeat["prepared_key"])
        self.assertFalse(changed["cache_hit"])
        self.assertNotEqual(first["prepared_key"], changed["prepared_key"])
        self.assertEqual(changed["preparation_builds"], 2)

    def test_current_conflict_is_not_labelled_as_future_warning(self):
        data = deepcopy(self.data["observations"][1]["event"])
        data["payload"].update(start=0, end=15)
        result = self.engine.monitor(MapsEvent.parse(data), initialize=True)
        self.assertEqual(result["status"], "CURRENT_CONFLICT")
        self.assertTrue(result["current_conflicts"])
        self.assertFalse(result["should_prepare"])
        self.assertEqual(result["preparation"]["status"], "SKIPPED")

    def test_lookahead_horizon_and_half_open_interval_boundary(self):
        baseline = deepcopy(self.data["mes"])
        baseline["snapshot"]["lookahead_minutes"] = 29
        mes = InMemoryMES(parse_baseline(baseline))
        self.assertEqual(OrchestrateAgent(mes, mes).monitor(self.event())["risks"], [])
        baseline["snapshot"]["lookahead_minutes"] = 30
        mes = InMemoryMES(parse_baseline(baseline))
        self.assertEqual(len(OrchestrateAgent(mes, mes).monitor(self.event())["risks"]), 1)
        event = deepcopy(self.data["observations"][1]["event"])
        event["payload"].update(start=50, end=60)
        self.assertEqual(self.engine.monitor(MapsEvent.parse(event))["risks"], [])

    def test_non_ready_task_and_inventory_events_do_not_initialize(self):
        baseline = deepcopy(self.data["mes"])
        baseline["work_orders"][1]["production_ready"] = False
        mes = InMemoryMES(parse_baseline(baseline))
        self.assertEqual(OrchestrateAgent(mes, mes).monitor(self.event(), initialize=True)["status"], "NOT_ADMITTED")
        event = MapsEvent.parse({"id": "excluded-inventory", "kind": "inventory.corrected",
                                 "at": 0, "priority": "high", "payload": {"material": "steel", "quantity": 0}})
        self.assertEqual(self.engine.monitor(event, initialize=True)["status"], "OUT_OF_SCOPE")

    def test_preparation_failure_is_reported_without_writeback(self):
        with patch.object(self.engine.planning.optimizer, "prepare", side_effect=ValueError("fixed-work conflict")):
            result = self.engine.monitor(self.event(), initialize=True)
        self.assertEqual(result["preparation"]["status"], "PREPARATION_BLOCKED")
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(self.mes.read_baseline().version, 1)

    def test_demo_never_calls_solver_decision_or_mes_write(self):
        with patch("enterprise_agent.capabilities.local_search.LocalSearchSolver.solve", side_effect=AssertionError("unexpected solve")), \
             patch.object(InMemoryMES, "submit_patch", side_effect=AssertionError("unexpected write")):
            report = run_monitor_demo(FIXTURE)
        summary = report["summary"]
        self.assertEqual(summary["statuses"], ["NORMAL", "FORECAST_RISK", "FORECAST_RISK", "FORECAST_RISK"])
        self.assertEqual(summary["preparation_builds"], 2)
        self.assertEqual(summary["preparation_reuses"], 1)
        self.assertTrue(summary["mes_state_unchanged"])
        self.assertTrue(summary["baseline_plan_unchanged"])
        self.assertFalse(summary["execution_authorized"])

    def test_cli_writes_report_and_callback_can_stop_replay(self):
        from enterprise_agent.cli import main
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "monitor.json"
            with patch("builtins.print"):
                self.assertEqual(main(["monitor-demo", "--input", str(FIXTURE), "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["summary"]["observations_processed"], 4)
        partial = run_monitor_demo(FIXTURE, lambda step: False)
        self.assertFalse(partial["summary"]["replay_complete"])
        self.assertEqual(partial["summary"]["observations_processed"], 1)


if __name__ == "__main__":
    unittest.main()

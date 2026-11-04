from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.agents.planning import DataCollect
from enterprise_agent.agents.prediction import LLMPrediction, KnownCalendarLookahead
from enterprise_agent.application.llm_monitor_demo import run_llm_monitor_demo
from enterprise_agent.domain.contracts import MapsEvent
from enterprise_agent.infrastructure.llm import LLMClient, LLMConfig, LLMError
from enterprise_agent.infrastructure.mes import InMemoryMES, parse_baseline

FIXTURE = Path(__file__).resolve().parents[1] / "examples/orchestrate_llm.json"


def forecast_fixture(modules=("planning", "scheduling", "warehouse")):
    """Protocol response fixture, never evidence of model inference quality."""
    return {"risks": [{"kind": "operation_overrun", "machine_id": "M1",
             "affected_order_ids": ["WO-RUN", "WO-NORMAL"],
             "earliest_impact_minute": 50, "latest_impact_minute": 90,
             "evidence_ids": ["OBS-02", "OBS-03"],
             "module_requests": [{"module": m, "purpose": "准备厂内作业调整上下文"} for m in modules],
             "rationale": "已观测的加工进度偏慢，可能影响后续工单。",
             "uncertainty": "high"}], "abstention_reason": ""}


class LLMPredictionTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.baseline = parse_baseline(self.data["mes"])
        self.event = MapsEvent.parse(self.data["event"])
        self.context = DataCollect().collect(self.baseline, self.event)

    def predictor(self, response=None, *, api="chat_completions", raw_arguments=None):
        captured = []
        def transport(url, payload, headers, timeout):
            captured.append({"url": url, "payload": deepcopy(payload)})
            arguments = raw_arguments if raw_arguments is not None else json.dumps(response if response is not None else forecast_fixture())
            if api == "responses":
                return {"output": [{"type": "function_call", "call_id": "fixture", "name": "forecast_factory_risks", "arguments": arguments}]}
            return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
                {"id": "fixture", "function": {"name": "forecast_factory_risks", "arguments": arguments}}]}}]}
        client = LLMClient(LLMConfig(model="protocol-fixture", api_key="", base_url="http://127.0.0.1:1234/v1", api=api), transport)
        return LLMPrediction(client), captured

    def test_model_protocol_receives_only_current_and_past_observations(self):
        predictor, calls = self.predictor()
        risks = predictor.forecast(self.context)
        payload = calls[0]["payload"]
        supplied = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(payload["model"], "protocol-fixture")
        self.assertTrue(all(o["at"] <= supplied["now"] for o in supplied["runtime_observations"]))
        self.assertTrue(all(not m["unavailable"] for m in supplied["machines"]))
        self.assertNotIn("risks", supplied)
        self.assertEqual(risks[0]["source"], "llm")
        self.assertTrue(risks[0]["prediction_is_hypothesis"])
        self.assertEqual(predictor.last_call["response_origin"], "protocol_fixture")
        self.assertFalse(predictor.last_call["live_call_verified"])

    def test_llm_risk_drives_preparation_when_calendar_baseline_has_no_signal(self):
        self.assertEqual(KnownCalendarLookahead().forecast(self.context), [])
        predictor, _ = self.predictor()
        with patch.object(InMemoryMES, "submit_patch", side_effect=AssertionError("write forbidden")), \
             patch("enterprise_agent.capabilities.local_search.LocalSearchSolver.solve", side_effect=AssertionError("solve forbidden")):
            report = run_llm_monitor_demo(FIXTURE, predictor)
        summary = report["summary"]
        self.assertEqual(summary["status"], "FORECAST_RISK")
        self.assertEqual(set(summary["initialized_modules"]), {"planning", "scheduling", "warehouse"})
        self.assertEqual(report["observation"]["impact_scope"], ["WO-NORMAL"])
        self.assertTrue(summary["mes_state_unchanged"])
        self.assertFalse(summary["live_call_verified"])

    def test_model_module_selection_is_respected(self):
        predictor, _ = self.predictor(forecast_fixture(("warehouse",)))
        mes = InMemoryMES(self.baseline)
        engine = OrchestrateAgent(mes, mes, prediction=predictor)
        with patch.object(engine.planning, "prepare", side_effect=AssertionError("Planning not requested")), \
             patch.object(engine.scheduling, "prepare", side_effect=AssertionError("Scheduling not requested")):
            result = engine.monitor(self.event, initialize=True)
        self.assertEqual(set(result["preparation"]["module_initialization"]), {"warehouse"})

    def test_scheduling_dependency_is_added_transparently(self):
        predictor, _ = self.predictor(forecast_fixture(("scheduling",)))
        result = run_llm_monitor_demo(FIXTURE, predictor)["observation"]["preparation"]
        self.assertEqual(result["requested_modules"], ["scheduling"])
        self.assertEqual(result["dependency_added_modules"], ["warehouse"])
        self.assertEqual(set(result["module_initialization"]), {"scheduling", "warehouse"})

    def test_supported_abstention_does_not_trigger_preparation(self):
        predictor, _ = self.predictor({"risks": [], "abstention_reason": "观测不足，保留观察。"})
        report = run_llm_monitor_demo(FIXTURE, predictor)
        self.assertEqual(report["summary"]["status"], "NORMAL")
        self.assertEqual(report["summary"]["initialized_modules"], [])
        self.assertEqual(report["observation"]["prediction_call"]["abstention_reason"], "观测不足，保留观察。")

    def test_invalid_references_times_and_execution_fields_are_rejected(self):
        mutations = [lambda r: r.update(machine_id="UNKNOWN"),
                     lambda r: r.update(evidence_ids=["INVENTED"]),
                     lambda r: r.update(affected_order_ids=["UNKNOWN"]),
                     lambda r: r.update(earliest_impact_minute=20),
                     lambda r: r.update(latest_impact_minute=999),
                     lambda r: r.update(approved=True),
                     lambda r: r.update(module_requests=[{"module": "mes_write", "purpose": "write"}])]
        for mutate in mutations:
            fixture = forecast_fixture(); mutate(fixture["risks"][0])
            predictor, _ = self.predictor(fixture)
            report = run_llm_monitor_demo(FIXTURE, predictor)
            self.assertEqual(report["summary"]["status"], "PREDICTION_UNAVAILABLE")
            self.assertEqual(report["summary"]["initialized_modules"], [])
            self.assertTrue(report["summary"]["mes_state_unchanged"])

    def test_future_observation_is_rejected_before_any_model_call(self):
        context = deepcopy(self.context)
        context.event.event.payload["runtime_observations"][0]["at"] = 21
        predictor, calls = self.predictor()
        with self.assertRaisesRegex(ValueError, "future"):
            predictor.forecast(context)
        self.assertEqual(calls, [])

    def test_duplicate_json_fields_are_rejected(self):
        predictor, _ = self.predictor(raw_arguments='{"risks": [], "risks": [], "abstention_reason": "none"}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            predictor.forecast(self.context)

    def test_provider_failure_never_falls_back_to_calendar_rules(self):
        predictor, _ = self.predictor()
        predictor.client.transport = lambda *args: (_ for _ in ()).throw(LLMError("provider network failure"))
        with patch.object(KnownCalendarLookahead, "forecast", side_effect=AssertionError("rules substitution forbidden")):
            report = run_llm_monitor_demo(FIXTURE, predictor)
        self.assertEqual(report["summary"]["status"], "PREDICTION_UNAVAILABLE")
        self.assertEqual(report["summary"]["initialized_modules"], [])
        self.assertFalse(report["summary"]["live_call_verified"])

    def test_responses_protocol_and_default_cli_are_llm(self):
        predictor, _ = self.predictor(api="responses")
        self.assertEqual(len(predictor.forecast(self.context)), 1)
        from enterprise_agent.cli import main
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "llm.json"
            with patch("enterprise_agent.infrastructure.llm.LLMConfig.from_env", return_value=predictor.client.config), \
                 patch("enterprise_agent.infrastructure.llm.LLMClient", return_value=predictor.client), patch("builtins.print"):
                self.assertEqual(main(["monitor-demo", "--input", str(FIXTURE), "--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["prediction_source"], "llm")
            self.assertEqual(report["summary"]["response_origin"], "protocol_fixture")


if __name__ == "__main__":
    unittest.main()

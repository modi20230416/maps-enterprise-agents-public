import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from enterprise_agent.cli import run_scenario
from enterprise_agent.llm import LLMClient, LLMConfig, LLMError, check_arguments
from enterprise_agent.llm_planning import LLMOrchestratorAgent
from enterprise_agent.models import Event, Snapshot
from enterprise_agent.planning import OrchestratorAgent
from enterprise_agent.scenarios import scenarios
from enterprise_agent.scheduler import validate_plan
from enterprise_agent.store import StateStore
from enterprise_agent.warehouse import MessageBus


class ScriptedTransport:
    """Protocol fixture, explicitly NOT a real model or live provider test."""
    def __init__(self, calls, api="responses"):
        self.calls = iter(calls)
        self.api = api
        self.requests = []

    def __call__(self, url, payload, headers, timeout):
        self.requests.append(deepcopy({"url": url, "payload": payload, "timeout": timeout}))
        name, args = next(self.calls)
        call_id = f"call-{len(self.requests)}"
        arguments = args if isinstance(args, str) else json.dumps(args)
        if self.api == "responses":
            return {"status": "completed", "output": [
                {"type": "reasoning", "id": f"r-{call_id}", "encrypted_content": "opaque", "summary": []},
                {"type": "function_call", "id": f"f-{call_id}", "call_id": call_id,
                 "name": name, "arguments": arguments}], "usage": {"input_tokens": 10, "output_tokens": 5}}
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}]}}], "usage": {"total_tokens": 15}}


def calls_for_replan():
    return [("forecast_risks", {}), ("assess_impact", {}),
            ("query_materials", {"order_id": "NORMAL-2"}),
            ("build_schedule", {"strategy": "search"}), ("compare_plans", {}),
            ("finalize_decision", {"action": "REPLAN", "candidate_id": "candidate-1",
                                   "rationale": "根据实际工具结果重新排程", "evidence": ["baseline_feasible=false"]})]


class LLMIntegrationTests(unittest.TestCase):
    def engine(self, calls, api="responses", **limits):
        transport = ScriptedTransport(calls, api)
        cfg = LLMConfig(model="test-model", api_key="test-secret-not-a-real-key", api=api, **limits)
        return LLMOrchestratorAgent(LLMClient(cfg, transport)), transport

    def test_responses_tool_loop_drives_real_scheduling_tools(self):
        engine, transport = self.engine(calls_for_replan())
        with StateStore(":memory:") as store:
            report = run_scenario(scenarios()["rush_order"], store, "llm-test", engine)
            decision = report["decisions"][0]
            self.assertEqual(decision["agent_mode"], "llm")
            self.assertEqual(decision["llm"]["status"], "completed")
            self.assertEqual(decision["after_metrics"]["tardiness"]["URGENT-1"], 0)
            self.assertEqual(len(decision["llm"]["tool_trace"]), 6)
            self.assertNotIn("test-secret", json.dumps(report))
            self.assertEqual(validate_plan(Snapshot.parse(report["final_snapshot"]),
                                           store.read("llm-test")[1]), [])
        first = transport.requests[0]["payload"]
        self.assertFalse(first["store"])
        self.assertTrue(all(t["strict"] for t in first["tools"]))
        second_history = transport.requests[1]["payload"]["input"]
        self.assertTrue(any(item.get("type") == "reasoning" for item in second_history))
        self.assertTrue(any(item.get("type") == "function_call_output" for item in second_history))

    def test_chat_completions_compatibility(self):
        engine, transport = self.engine(calls_for_replan(), api="chat_completions")
        with StateStore(":memory:") as store:
            decision = run_scenario(scenarios()["rush_order"], store, "chat", engine)["decisions"][0]
        self.assertEqual(decision["agent_mode"], "llm")
        self.assertTrue(transport.requests[0]["url"].endswith("/chat/completions"))
        body = transport.requests[1]["payload"]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertTrue(any(m["role"] == "tool" for m in body["messages"]))

    def test_model_can_keep_feasible_baseline(self):
        calls = [("forecast_risks", {}), ("assess_impact", {}),
                 ("finalize_decision", {"action": "KEEP", "candidate_id": None,
                                        "rationale": "未发现有效变化", "evidence": ["baseline_feasible=true"]})]
        engine, _ = self.engine(calls)
        with StateStore(":memory:") as store:
            decision = run_scenario(scenarios()["no_change"], store, "keep", engine)["decisions"][0]
        self.assertEqual(decision["action"], "KEEP")
        self.assertEqual(decision["candidate_count"], 0)

    def test_llm_can_call_drl_candidate_tool_with_a_checkpoint(self):
        from enterprise_agent.rl_env import ACTION_NAMES, ENV_VERSION
        layers = [74, 32, 32, 4]
        model = {"format": "maps-dqn-v1", "environment_version": ENV_VERSION,
                 "actions": list(ACTION_NAMES), "max_orders": 8, "layers": layers,
                 "weights": [[[0.0] * b for _ in range(a)] for a, b in zip(layers, layers[1:])],
                 "biases": [[0.0] * b for b in layers[1:]]}
        calls = [("forecast_risks", {}), ("assess_impact", {}),
                 ("build_schedule", {"strategy": "drl"}),
                 ("finalize_decision", {"action": "REPLAN", "candidate_id": "candidate-1",
                                        "rationale": "调用神经网络派工候选", "evidence": ["validation_errors=[]"]})]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "fixture.json"
            target.write_text(json.dumps(model), encoding="utf-8")
            engine, _ = self.engine(calls)
            engine.drl_checkpoint = str(target)
            with StateStore(":memory:") as store:
                d = run_scenario(scenarios()["rush_order"], store, "drl-tool", engine)["decisions"][0]
        self.assertEqual(d["agent_mode"], "llm")
        self.assertEqual(d["plan"]["strategy"], "dqn_checkpoint_dispatch")
        self.assertEqual(d["validation_errors"], [])

    def test_model_cannot_select_nonexistent_candidate(self):
        calls = calls_for_replan()
        calls.insert(2, ("finalize_decision", {"action": "REPLAN", "candidate_id": "invented-plan",
                                             "rationale": "不可信引用", "evidence": ["none"]}))
        engine, _ = self.engine(calls)
        with StateStore(":memory:") as store:
            d = run_scenario(scenarios()["rush_order"], store, "fake-plan", engine)["decisions"][0]
        self.assertFalse(d["llm"]["tool_trace"][2]["result"]["ok"])
        self.assertEqual(d["agent_mode"], "llm")

    def test_invalid_keep_is_rejected_and_model_can_repair(self):
        calls = calls_for_replan()
        calls.insert(2, ("finalize_decision", {"action": "KEEP", "candidate_id": None,
                                             "rationale": "错误尝试", "evidence": ["invalid"]}))
        engine, _ = self.engine(calls)
        with StateStore(":memory:") as store:
            d = run_scenario(scenarios()["rush_order"], store, "reject", engine)["decisions"][0]
        self.assertEqual(d["agent_mode"], "llm")
        self.assertFalse(d["llm"]["tool_trace"][2]["result"]["ok"])
        self.assertEqual(d["action"], "REPLAN")

    def test_unknown_tool_and_order_are_recoverable(self):
        calls = [("run_shell", {"cmd": "anything"}),
                 ("query_materials", {"order_id": "UNKNOWN"}), *calls_for_replan()]
        engine, _ = self.engine(calls)
        with StateStore(":memory:") as store:
            d = run_scenario(scenarios()["rush_order"], store, "bad-tool", engine)["decisions"][0]
        self.assertEqual(d["agent_mode"], "llm")
        self.assertFalse(d["llm"]["tool_trace"][0]["result"]["ok"])
        self.assertFalse(d["llm"]["tool_trace"][1]["result"]["ok"])

    def test_round_budget_and_network_failure_fall_back_explicitly(self):
        engine, _ = self.engine([("forecast_risks", {})], max_rounds=1)
        with StateStore(":memory:") as store:
            d = run_scenario(scenarios()["rush_order"], store, "budget", engine)["decisions"][0]
        self.assertEqual(d["agent_mode"], "rules_fallback")
        self.assertIn("budget", d["llm"]["error"])
        def fail(*args):
            raise LLMError("provider HTTP 401; check endpoint, model and credentials")
        engine.client.transport = fail
        with StateStore(":memory:") as store:
            d = run_scenario(scenarios()["rush_order"], store, "network", engine)["decisions"][0]
        self.assertEqual(d["agent_mode"], "rules_fallback")
        self.assertIn("401", d["llm"]["error"])
        self.assertEqual(d["validation_errors"], [])

    def test_arguments_reject_extra_keys_duplicates_and_wrong_types(self):
        for value in ['{"order_id":1}', '{"order_id":"x","extra":true}',
                      '{"order_id":"x","order_id":"y"}', '["x"]']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_arguments("query_materials", value)

    def test_config_does_not_expose_key_and_requires_explicit_model(self):
        cfg = LLMConfig(model="test", api_key="secret-value")
        self.assertNotIn("secret-value", repr(cfg))
        with patch.dict(os.environ, {}, clear=True), patch("enterprise_agent.llm.load_llm_profile", return_value={}):
            with self.assertRaisesRegex(ValueError, "MODEL"):
                LLMConfig.from_env()
        with self.assertRaises(ValueError):
            LLMConfig(model="test", api_key="k", base_url="http://remote.example/v1")
        LLMConfig(model="local", api_key="", base_url="http://127.0.0.1:1234/v1")

    def test_openai_key_is_not_implicitly_forwarded_to_other_providers(self):
        with patch.dict(os.environ, {"MAPS_LLM_MODEL": "local", "OPENAI_API_KEY": "official-key",
                                     "MAPS_LLM_BASE_URL": "http://127.0.0.1:1234/v1"}, clear=True):
            self.assertEqual(LLMConfig.from_env().api_key, "")
        with patch.dict(os.environ, {"MAPS_LLM_MODEL": "remote", "OPENAI_API_KEY": "official-key",
                                     "MAPS_LLM_BASE_URL": "https://other.example/v1"}, clear=True):
            with self.assertRaisesRegex(ValueError, "API_KEY"):
                LLMConfig.from_env()

    def test_local_profile_and_environment_precedence(self):
        # Provider-neutral, self-contained fixture; no user profile is required.
        profile = {"provider": "example", "base_url": "https://llm.example.invalid/v1",
                   "model": "example-chat-model", "api": "chat_completions",
                   "chat_token_parameter": "max_tokens", "strict_tools": False,
                   "send_parallel_tool_calls": False}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "profile.json"
            target.write_text(json.dumps(profile), encoding="utf-8")
            with patch.dict(os.environ, {"MAPS_LLM_API_KEY": "provider-test-key"}, clear=True):
                cfg = LLMConfig.from_env(str(target))
            self.assertEqual(cfg.base_url, profile["base_url"])
            self.assertEqual(cfg.model, "example-chat-model")
            self.assertEqual(cfg.api, "chat_completions")
            self.assertEqual(cfg.chat_token_parameter, "max_tokens")
            self.assertFalse(cfg.strict_tools)
            with patch.dict(os.environ, {"MAPS_LLM_API_KEY": "provider-test-key",
                                         "MAPS_LLM_MODEL": "override-model"}, clear=True):
                self.assertEqual(LLMConfig.from_env(str(target)).model, "override-model")

    def test_chat_compatibility_profile_omits_optional_provider_fields(self):
        transport = ScriptedTransport([("forecast_risks", {})], "chat_completions")
        cfg = LLMConfig(model="example-chat-model", api_key="test", api="chat_completions",
                        chat_token_parameter="max_tokens", strict_tools=False,
                        send_parallel_tool_calls=False)
        client = LLMClient(cfg, transport)
        client.complete("test instructions", client.history({}), [], 20)
        payload = transport.requests[0]["payload"]
        self.assertIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("parallel_tool_calls", payload)
        from enterprise_agent.llm import TOOLS
        self.assertTrue(all(t["strict"] for t in TOOLS))
        transport = ScriptedTransport([("forecast_risks", {})], "chat_completions")
        LLMClient(cfg, transport).complete("test", client.history({}), TOOLS, 20)
        self.assertTrue(all("strict" not in t["function"] for t in transport.requests[0]["payload"]["tools"]))

    def test_deepseek_reasoning_is_replayed_but_not_written_to_audit(self):
        engine, transport = self.engine(calls_for_replan(), api="chat_completions")
        def with_reasoning(url, payload, headers, timeout):
            reply = transport(url, payload, headers, timeout)
            reply["choices"][0]["message"]["reasoning_content"] = "private-transient-reasoning-marker"
            return reply
        engine.client.transport = with_reasoning
        with StateStore(":memory:") as store:
            report = run_scenario(scenarios()["rush_order"], store, "thinking", engine)
        self.assertEqual(report["decisions"][0]["agent_mode"], "llm")
        self.assertTrue(any(m.get("reasoning_content") == "private-transient-reasoning-marker"
                            for m in transport.requests[1]["payload"]["messages"]))
        self.assertNotIn("private-transient-reasoning-marker", json.dumps(report))

    def test_profile_rejects_credentials_and_invalid_flag_types(self):
        from enterprise_agent.llm import load_llm_profile
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config.json"
            target.write_text(json.dumps({"api_key": "do-not-store-here"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "credentials"):
                load_llm_profile(str(target))
            target.write_text(json.dumps({"strict_tools": "false"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "type"):
                load_llm_profile(str(target))

    def test_audit_payload_is_a_snapshot_not_a_mutable_reference(self):
        bus = MessageBus("e")
        payload = {"items": [{"qty": 1}]}
        bus.send("a", "b", "x", payload)
        payload["items"][0]["qty"] = 99
        self.assertEqual(bus.messages[0]["payload"]["items"][0]["qty"], 1)

    def test_remote_planning_does_not_hold_write_lock_and_stale_commit_rejected(self):
        case = scenarios()["no_change"]
        initial = Snapshot.parse(case["snapshot"])
        rules = OrchestratorAgent()
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite3"
            with StateStore(db) as first, StateStore(db) as second:
                first.initialize("s", initial, rules)
                class InterleavedEngine(OrchestratorAgent):
                    def process(self, snapshot, baseline, event):
                        second.process("s", Event("other", "planning.tick", 0, {}), rules)
                        return super().process(snapshot, baseline, event)
                with self.assertRaisesRegex(ValueError, "state changed"):
                    first.process("s", Event("original", "planning.tick", 0, {}), InterleavedEngine())
                self.assertEqual(first.read("s")[2], 1)
                self.assertEqual(first.connection.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

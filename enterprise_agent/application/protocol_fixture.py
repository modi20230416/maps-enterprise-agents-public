"""Explicit offline protocol fixture, only for the bundled synthetic scenario.

This exercises LLM tool parsing and preparation; it does not perform inference.
"""
import json
from enterprise_agent.agents.prediction import LLMPrediction
from enterprise_agent.infrastructure.llm import LLMClient, LLMConfig


def fixture_prediction():
    def transport(url, payload, headers, timeout):
        result = {"risks": [{"kind": "operation_overrun", "machine_id": "M1",
            "affected_order_ids": ["WO-RUN", "WO-NORMAL"],
            "earliest_impact_minute": 50, "latest_impact_minute": 90,
            "evidence_ids": ["OBS-02", "OBS-03"],
            "module_requests": [{"module": m, "purpose": "提前准备厂内作业调整上下文"}
                                for m in ("planning", "scheduling", "warehouse")],
            "rationale": "模拟回复（预先写好）：加工进度偏慢，可能影响后续工单。未调用大模型。",
            "uncertainty": "high"}], "abstention_reason": ""}
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
            "id": "fixture", "function": {"name": "forecast_factory_risks", "arguments": json.dumps(result)}}]}}]}
    config = LLMConfig(model="protocol-fixture", api_key="", base_url="http://127.0.0.1:1/v1", api="chat_completions")
    return LLMPrediction(LLMClient(config, transport))

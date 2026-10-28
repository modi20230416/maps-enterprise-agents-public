"""Run offline fixtures and persist reports without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from .models import Event, Snapshot
from .experiments import benchmark
from .planning import OrchestratorAgent
from .reporting import benchmark_markdown, demo_markdown
from .scenarios import scenarios
from .store import StateStore


def write_json(path: str | Path, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_scenario(data: dict, store: StateStore, session_id: str, engine=None) -> dict:
    snapshot = Snapshot.parse(data["snapshot"])
    events = [Event.parse(e) for e in data["events"]]
    engine = engine or OrchestratorAgent()
    initial = store.initialize(session_id, snapshot, engine)
    decisions = []
    for event in events:
        started = perf_counter()
        result = store.process(session_id, event, engine)
        decisions.append({**result, "observed_seconds": round(perf_counter() - started, 6)})
    final_snapshot, final_plan, version = store.read(session_id)
    return {"session_id": session_id, "description": data.get("description", ""),
            "initial_plan": initial.to_dict(), "decisions": decisions,
            "final_snapshot": final_snapshot.to_dict(), "final_plan": final_plan.to_dict(),
            "version": version, "mode": "offline_simulation", "time_unit": "minute"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="企业多 Agent 计划与排程原型（离线仿真）")
    commands = parser.add_subparsers(dest="command", required=True)
    framework = commands.add_parser("framework-demo", help="运行 MES 协同三层框架")
    framework.add_argument("--input", default="examples/maps_framework.json")
    framework.add_argument("--scenario", choices=["all", "high_order", "emergency_review", "material_conflict", "no_change"], default="all")
    framework.add_argument("--agent", choices=["rules", "llm"], default="rules")
    framework.add_argument("--output", default="artifacts/maps-framework-report.json")
    demo = commands.add_parser("demo", help="运行内置场景")
    demo.add_argument("--scenario", choices=["all", *scenarios()], default="all")
    demo.add_argument("--output", default="artifacts/demo-report.json")
    demo.add_argument("--db", default="artifacts/agent.sqlite3")
    demo.add_argument("--summary", default="artifacts/demo-summary.md")
    demo.add_argument("--agent", choices=["rules", "llm"], default="rules")
    run = commands.add_parser("run", help="读取 JSON 场景")
    run.add_argument("input")
    run.add_argument("--session", default=None, help="指定会话标识可验证幂等重放")
    run.add_argument("--output", default="artifacts/scenario-report.json")
    run.add_argument("--db", default="artifacts/agent.sqlite3")
    run.add_argument("--summary", default="artifacts/scenario-summary.md")
    run.add_argument("--agent", choices=["rules", "llm"], default="rules")
    commands.add_parser("llm-status", help="检查 LLM 配置是否齐备，不显示密钥或发起付费调用")
    rl_demo = commands.add_parser("rl-demo", help="运行派工环境；可选择固定规则或 DQN 权重推理")
    rl_demo.add_argument("--scenario", choices=list(scenarios()), default="rush_order")
    rl_demo.add_argument("--output", default="artifacts/rl-environment-demo.json")
    rl_demo.add_argument("--checkpoint", default=None, help="可选 DQN JSON 权重；未指定时使用固定优先级规则")
    rl_train = commands.add_parser("train-drl", help="使用可选 NumPy 训练派工 DQN")
    rl_train.add_argument("--episodes", type=int, default=400)
    rl_train.add_argument("--seed", type=int, default=20261014)
    rl_train.add_argument("--checkpoint", default="artifacts/dispatch-dqn.json")
    rl_train.add_argument("--report", default="artifacts/drl-training-report.json")
    experiment = commands.add_parser("benchmark", help="固定随机种子的合成数据基线实验")
    experiment.add_argument("--cases", type=int, default=30)
    experiment.add_argument("--seed", type=int, default=20261014)
    experiment.add_argument("--output", default="artifacts/benchmark.json")
    experiment.add_argument("--summary", default="artifacts/benchmark-summary.md")
    export = commands.add_parser("export-examples", help="导出可修改的示例场景")
    export.add_argument("--directory", default="examples")
    args = parser.parse_args(argv)
    try:
        if args.command == "framework-demo":
            from .maps.demo import DEMO_CASES, run_demo
            proposer = None
            if args.agent == "llm":
                from .llm import LLMClient, LLMConfig
                from .maps.proposals import LLMProposals
                proposer = LLMProposals(LLMClient(LLMConfig.from_env()))
            names = DEMO_CASES if args.scenario == "all" else [args.scenario]
            report = {name: run_demo(args.input, name, proposer) for name in names}
            write_json(args.output, report)
            for name, item in report.items():
                decision = item["decision"]
                print(f"{name}: {decision['status']}, MES v{decision['base_version']} -> v{decision['mes_version']}")
            print(f"Report: {Path(args.output).resolve()}")
            return 0
        if args.command == "llm-status":
            from .llm import LLMConfig
            try:
                config = LLMConfig.from_env()
                print(json.dumps({"configured": True, "api": config.api, "model": config.model,
                                  "credential_present": bool(config.api_key),
                                  "live_call_verified": False}, ensure_ascii=False))
            except ValueError as exc:
                print(json.dumps({"configured": False, "reason": str(exc), "live_call_verified": False}))
                return 1
            return 0
        if args.command == "rl-demo":
            from .rl_env import DispatchEnv
            from .planning import CollectionAgent
            case = scenarios()[args.scenario]
            snapshot = Snapshot.parse(case["snapshot"])
            for event in case["events"]:
                snapshot = CollectionAgent.apply(snapshot, Event.parse(event))
            policy = None
            if args.checkpoint:
                from .drl import NeuralDispatchPolicy
                policy = NeuralDispatchPolicy.load(args.checkpoint)
            env = DispatchEnv(snapshot, policy.max_orders if policy else 8)
            observation, info = env.reset()
            steps = []
            while not info["terminated"]:
                action = policy.action(observation) if policy else 0
                observation, reward, terminated, truncated, info = env.step(action)
                steps.append({"action": action, "reward": reward, "terminated": terminated, "info": info})
            write_json(args.output, {"policy": "checkpoint_dqn" if policy else "fixed_priority_rule_not_trained", "steps": steps,
                                     "plan": env.plan.to_dict(), "environment": "static_dispatch_sequence"})
            print(f"RL environment demo: {len(steps)} steps, valid plan, policy={'DQN checkpoint' if policy else 'fixed rule'}")
            return 0
        if args.command == "train-drl":
            from .drl import train_dqn
            report = train_dqn(args.episodes, args.seed, args.checkpoint)
            write_json(args.report, report)
            print(json.dumps({"episodes": report["episodes"], "gradient_updates": report["gradient_updates"],
                              "evaluation": report["evaluation"]["summary"],
                              "checkpoint": report["checkpoint"]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "export-examples":
            for name, data in scenarios().items():
                write_json(Path(args.directory) / f"{name}.json", data)
            print(f"Exported {len(scenarios())} scenarios to {args.directory}")
            return 0
        if args.command == "benchmark":
            report = benchmark(args.cases, args.seed)
            write_json(args.output, report)
            target = Path(args.summary)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(benchmark_markdown(report), encoding="utf-8")
            print(benchmark_markdown(report))
            return 0
        engine = None
        if args.agent == "llm":
            from .llm import LLMClient, LLMConfig
            from .llm_planning import LLMOrchestratorAgent
            engine = LLMOrchestratorAgent(LLMClient(LLMConfig.from_env()),
                                          os.getenv("MAPS_DRL_CHECKPOINT") or None)
        with StateStore(args.db) as store:
            if args.command == "demo":
                cases = scenarios()
                names = list(cases) if args.scenario == "all" else [args.scenario]
                report = {name: run_scenario(cases[name], store, f"{name}-{uuid4().hex[:12]}", engine)
                          for name in names}
            else:
                data = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
                report = {"custom": run_scenario(data, store, args.session or uuid4().hex, engine)}
            write_json(args.output, report)
            target = Path(args.summary)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(demo_markdown(report), encoding="utf-8")
            for name, result in report.items():
                for decision in result["decisions"]:
                    m = decision["after_metrics"]
                    print(f"{name}: {decision['action']}, weighted_tardiness={m['weighted_tardiness']}, "
                          f"blocked={m['blocked_orders']}, candidates={decision['candidate_count']}, "
                          f"feasible={not decision['validation_errors']}, mode={decision.get('agent_mode', 'rules')}")
                    if decision.get("llm", {}).get("status") == "fallback":
                        print(f"LLM fallback: {decision['llm'].get('error', 'no valid decision')}")
            print(f"Report: {Path(args.output).resolve()}")
            return 0
    except (ValueError, KeyError, TypeError, OSError, ImportError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())

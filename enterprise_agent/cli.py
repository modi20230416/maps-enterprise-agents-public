"""Run offline fixtures and persist reports without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from enterprise_agent.experiments.scenarios import scenarios


def write_json(path: str | Path, value: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="企业多 Agent 计划与排程原型（离线仿真）")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("platform-api", help="启动独立API与持久化异步任务运行时（本机）")
    api.add_argument("--db", default="artifacts/platform.sqlite3")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--workers", type=int, default=2)
    api.add_argument("--allow-protocol-fixture", action="store_true")
    ui = commands.add_parser("platform-ui", help="启动独立Web前端（本机）")
    ui.add_argument("--port", type=int, default=5173)
    monitor = commands.add_parser("monitor-demo", help="LLM前瞻推理与按需模块准备；日历规则仅作显式对照")
    monitor.add_argument("--prediction", choices=["llm", "calendar"], default="llm")
    monitor.add_argument("--input", default=None)
    monitor.add_argument("--pause", action="store_true", help="每次观察后按回车继续，q停止")
    monitor.add_argument("--output", default=None)
    operations = commands.add_parser("operations-demo", help="已就绪任务的前瞻预初始化与全模块人工复核")
    operations.add_argument("--input", default="examples/operations_ready.json")
    operations.add_argument("--review-mode", choices=["manual", "pending", "scripted"], default="manual")
    operations.add_argument("--agent", choices=["rules", "llm"], default="rules")
    operations.add_argument("--output", default="artifacts/operations-demo.json")
    framework = commands.add_parser("framework-demo", help="运行MES 多智能体协同框架")
    framework.add_argument("--input", default="examples/maps_framework.json")
    framework.add_argument("--scenario", choices=["all", "high_order", "emergency_review", "material_conflict", "no_change"], default="all")
    framework.add_argument("--agent", choices=["rules", "llm"], default="rules")
    framework.add_argument("--output", default="artifacts/maps-framework-report.json")
    demo = commands.add_parser("demo", help="运行早期单时刻实验场景")
    demo.add_argument("--scenario", choices=["all", *scenarios()], default="all")
    demo.add_argument("--output", default="artifacts/demo-report.json")
    demo.add_argument("--db", default="artifacts/agent.sqlite3")
    demo.add_argument("--summary", default="artifacts/demo-summary.md")
    demo.add_argument("--agent", choices=["rules", "llm"], default="rules")
    run = commands.add_parser("run", help="读取早期实验 JSON 场景")
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
        if args.command == "platform-api":
            from enterprise_agent.web.serve import serve_api
            serve_api(args.db, args.port, args.workers, args.allow_protocol_fixture)
            return 0
        if args.command == "platform-ui":
            from enterprise_agent.web.serve import serve_ui
            serve_ui(args.port)
            return 0
        if args.command == "monitor-demo":
            if args.prediction == "llm":
                from enterprise_agent.agents.prediction import LLMPrediction
                from enterprise_agent.infrastructure.llm import LLMClient, LLMConfig
                from enterprise_agent.application.llm_monitor_demo import run_llm_monitor_demo, display_llm_monitor
                prediction = LLMPrediction(LLMClient(LLMConfig.from_env()))
                if args.pause:
                    input("Enter: call the configured LLM using current/past observations > ")
                report = run_llm_monitor_demo(args.input or "examples/orchestrate_llm.json", prediction)
                output = args.output or "artifacts/orchestrate-llm-live.json"
                write_json(output, report)
                display_llm_monitor(report)
                print(f"Report: {Path(output).resolve()}")
                return 1 if report["summary"]["status"] == "PREDICTION_UNAVAILABLE" else 0
            from enterprise_agent.application.monitor_demo import run_monitor_demo, display_step

            def show_step(step):
                display_step(step)
                if args.pause:
                    try:
                        return input("Enter: next observation; q: stop > ").strip().lower() != "q"
                    except EOFError:
                        return False
                return True

            report = run_monitor_demo(args.input or "examples/orchestrate_monitor.json", show_step)
            output = args.output or "artifacts/orchestrate-calendar-baseline.json"
            report["summary"]["prediction_source"] = "known_calendar_rules"
            write_json(output, report)
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            print(f"Report: {Path(output).resolve()}")
            return 0
        if args.command == "operations-demo":
            from enterprise_agent.application.operations_demo import run_operations_demo
            proposer = None
            if args.agent == "llm":
                from enterprise_agent.infrastructure.llm import LLMClient, LLMConfig
                from enterprise_agent.agents.proposals import LLMProposals
                proposer = LLMProposals(LLMClient(LLMConfig.from_env()))
            report = run_operations_demo(args.input, args.review_mode, proposer)
            write_json(args.output, report)
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            print(f"Report: {Path(args.output).resolve()}")
            return 0
        if args.command == "framework-demo":
            from enterprise_agent.application.demo import DEMO_CASES, run_demo
            proposer = None
            if args.agent == "llm":
                from enterprise_agent.infrastructure.llm import LLMClient, LLMConfig
                from enterprise_agent.agents.proposals import LLMProposals
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
            from enterprise_agent.infrastructure.llm import LLMConfig
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
            from enterprise_agent.domain.models import Snapshot, Event
            from enterprise_agent.experiments.dispatch_env import DispatchEnv
            from enterprise_agent.capabilities.collection import EventCollector
            case = scenarios()[args.scenario]
            snapshot = Snapshot.parse(case["snapshot"])
            for event in case["events"]:
                snapshot = EventCollector.apply(snapshot, Event.parse(event))
            policy = None
            if args.checkpoint:
                from enterprise_agent.experiments.dqn import NeuralDispatchPolicy
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
            from enterprise_agent.experiments.dqn import train_dqn
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
            from enterprise_agent.experiments.benchmark import benchmark
            from enterprise_agent.experiments.reporting import benchmark_markdown
            report = benchmark(args.cases, args.seed)
            write_json(args.output, report)
            target = Path(args.summary)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(benchmark_markdown(report), encoding="utf-8")
            print(benchmark_markdown(report))
            return 0
        from enterprise_agent.experiments.runner import run_scenario
        from enterprise_agent.experiments.reporting import demo_markdown
        from enterprise_agent.experiments.store import StateStore
        engine = None
        if args.agent == "llm":
            from enterprise_agent.infrastructure.llm import LLMClient, LLMConfig
            from enterprise_agent.experiments.llm_orchestrator import LLMExperimentOrchestrator
            engine = LLMExperimentOrchestrator(LLMClient(LLMConfig.from_env()),
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

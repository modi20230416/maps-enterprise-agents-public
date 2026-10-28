"""Human-readable local reports derived from actual run results."""

SCENARIO_NAMES = {"rush_order": "急单插入", "future_downtime": "未来停机",
                  "receipt_delay": "到货延迟", "no_change": "无有效变化",
                  "material_shortage": "物料不足"}
REASONS = {
    "repair infeasible baseline": "原计划在更新后的状态下已不可行，重新生成满足约束的计划",
    "baseline remains feasible; avoid unnecessary schedule changes": "原计划仍可行，保持安排以避免无效变动",
    "candidate improves objective including schedule stability": "候选方案综合延期与计划稳定性后更优",
    "candidate does not improve the feasible baseline": "候选没有优于当前可行计划，保持原计划",
}
RISK_NAMES = {"capacity_conflict": "设备可用性冲突", "material_wait": "物料等待风险",
              "material_shortage": "确认物料不足", "delivery_lower_bound": "交付下界已超过交期"}


def demo_markdown(report: dict) -> str:
    lines = ["# 原型场景运行报告", "", "全部使用合成数据；时间单位为分钟，t=0 为决策时刻。",
             "五个场景分别独立运行，不构成生产执行时间推进。", "",
             "| 场景 | 决策来源 | 决策 | 重排前可行 | 加权延期（后） | 阻塞订单 | 候选数 | 约束校验 |",
             "|---|---|---|---|---:|---:|---:|---|"]
    for name, result in report.items():
        for d in result["decisions"]:
            m = d["after_metrics"]
            mode = {"llm": "LLM", "rules_fallback": "LLM 失败后规则回退"}.get(d.get("agent_mode"), "规则")
            lines.append(f"| {SCENARIO_NAMES.get(name, name)} | {mode} | {d['action']} | {'是' if d['before_feasible'] else '否'} "
                         f"| {m['weighted_tardiness']} | {m['blocked_orders']} "
                         f"| {d['candidate_count']} | {'通过' if not d['validation_errors'] else '失败'} |")
    lines.extend(["", "重排前不可行时，前后延期值不构成有效收益对比。阻塞订单不计入延期，须同时查看阻塞优先级。", ""])
    for name, result in report.items():
        lines.extend([f"## {SCENARIO_NAMES.get(name, name)}（{name}）", "", result["description"], ""])
        for d in result["decisions"]:
            lines.extend([f"决策：{d['action']}。原因：{REASONS.get(d['reason'], d['reason'])}。", ""])
            if "llm" in d:
                audit = d["llm"]
                lines.extend([f"LLM 状态：{audit['status']}；模型：{audit.get('model', '未返回')}。", ""])
                if audit.get("error"):
                    lines.extend([f"回退原因：{audit['error']}。", ""])
                if audit.get("tool_trace"):
                    lines.extend(["模型实际选择的工具顺序：" + " → ".join(
                        call["tool"] for call in audit["tool_trace"]) + "。", ""])
            if d["risks"]:
                for risk in d["risks"]:
                    lead = f"，提前量 {risk['lead_minutes']} 分钟" if "lead_minutes" in risk else ""
                    lines.append(f"- {RISK_NAMES.get(risk['kind'], risk['kind'])}：{risk.get('order_id', '')}{lead}。")
                lines.append("")
        lines.extend(["最终排程：", "", "| 订单 | 工序 | 设备 | 开始 | 完成 |",
                      "|---|---|---|---:|---:|"])
        for a in sorted(result["final_plan"]["assignments"], key=lambda a: (a["start"], a["machine_id"])):
            lines.append(f"| {a['order_id']} | {a['operation_id']} | {a['machine_id']} | {a['start']} | {a['end']} |")
        lines.append("")
        for order_id, reason in result["final_plan"]["blocked"].items():
            reason = reason.replace("insufficient confirmed supply:", "确认供给缺口：")
            lines.extend([f"阻塞订单 {order_id}：{reason}。", ""])
    return "\n".join(lines)


def benchmark_markdown(report: dict) -> str:
    lines = ["# 合成数据基线实验", "", f"随机种子：{report['seed']}；独立算例：{report['cases']}。",
             "每个算例包含 3–5 个订单、三台设备、两道工序、库存与预计到货，以及一个已知停机窗口。", "",
             "| 方法 | 可行算例 | 平均阻塞优先级 | 平均加权延期 | 平均工期 | P95 求解秒数 |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, s in report["summary"].items():
        lines.append(f"| {name} | {s['feasible_cases']} | {s['mean_blocked_priority']:.2f} "
                     f"| {s['mean_weighted_tardiness']:.2f} | {s['mean_makespan']:.2f} | {s['p95_seconds']:.6f} |")
    lines.extend(["", "EDD 按交期排序；Priority_EDD 按优先级后按交期排序；Multi_candidate 在有界候选中按统一目标选解。",
                  "所有方法使用相同物料约束与设备日历。目标按“阻塞优先级、10×加权延期+变动代价、工期”依次比较。",
                  "Multi_candidate 包含两个基线序列，逐算例检查其目标不差于所含基线；这属于实现正确性检查，不是研究贡献证明。", "",
                  "结果只说明当前小规模合成数据上的行为；不代表全局最优、真实工厂收益或统计显著性。",
                  "若方法阻塞了不同订单，单独比较加权延期会误导，必须同时查看阻塞优先级与逐算例目标。",
                  "本实验不评估真实滚动执行、提前预测相对纯反应策略的收益，也不评估 Agent 架构相对同算法单体实现的收益。", ""])
    return "\n".join(lines)

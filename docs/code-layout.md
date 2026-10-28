# 代码目录与阅读顺序

## 主框架入口

从 `enterprise_agent/agents/orchestrate.py` 的 `OrchestrateAgent` 开始阅读。
`application/demo.py` 负责组装依赖，`cli.py` 只负责命令入口。

```text
enterprise_agent/
  agents/
    orchestrate.py       中枢：组织、协调、仲裁、写回
    planning.py          计划：采集、门卫、预测、工单优化
    scheduling.py        排单：候选策略、工序安排、直接协商
    warehouse.py         仓库：齐套确认与缺料反馈
    proposals.py         LLM / 规则协调提案
  capabilities/
    collection.py        事件应用与规范化
    prediction.py        已知日历、物料与交期风险
    scheduling.py        排程构造、评分、可行性校验
    local_search.py      保留冻结工单的局部优化
    materials.py         物料批次与数量分配
    validation.py        提案、审批边界与写回条件
  domain/
    models.py            工单、工序、设备、计划
    contracts.py         事件、优先级、上下文、补丁
    ports.py             企业系统、预测、策略协议
    policy.py            学习策略回放资格
  infrastructure/
    llm.py               HTTP 模型适配与工具 schema
    mes.py               内存 MES、版本校验
    trace.py             同步调用审计记录
  application/
    demo.py              主框架依赖组装与合成演示
  experiments/
    rule_orchestrator.py 早期规则编排实验
    llm_orchestrator.py  早期模型编排实验
    llm_tools.py         实验工具集与调用循环
    runner.py            单时刻实验场景执行
    store.py             实验 SQLite 存储
    benchmark.py         基线比较
    dqn.py               单智能体 DQN
    dispatch_env.py      静态派工环境
    scenarios.py         合成实验数据
    reporting.py         实验报告
  cli.py                 命令行入口
```

Agent 使用共享能力完成计算。底层的 `EventCollector`、`KnownRiskPredictor`、
`BoundedSearchOptimizer` 和 `MaterialAllocator` 按实际职责命名，避免把每个计算类都叫 Agent。
`TraceLog` 记录同步调用轨迹，不承担消息队列或服务调度职责。

主框架的 agents/capabilities/domain/infrastructure/application 不依赖 experiments。
实验使用同一套共享能力，计算逻辑只维护一份。MES 演示网关会反向调用 Agent 的采集、门卫和仓库确认来复核补丁，当前没有把整个项目宣称为严格单向的分层架构。

## 迁移说明

| 原入口 | 现入口 |
|---|---|
| `maps/orchestrate.py` | `agents/orchestrate.py` |
| `maps/planning.py` | `agents/planning.py` |
| `maps/scheduling.py` | `agents/scheduling.py`，局部搜索、仓库与回放资格已提取 |
| 外层 `planning.py` | `experiments/rule_orchestrator.py`，共享采集与预测已提取 |
| `llm_planning.py` | `experiments/llm_orchestrator.py` |
| 外层 `scheduler.py` / `warehouse.py` | `capabilities/scheduling.py` / `materials.py` |
| 外层 `llm.py` | `infrastructure/llm.py`，实验工具循环放入 `experiments/llm_tools.py` |
| `drl.py` / `rl_env.py` | `experiments/dqn.py` / `dispatch_env.py` |

内部 Python 导入路径已统一迁移，没有保留同名转发文件。外部脚本若直接导入旧模块，需要相应修改。
命令行名称保持兼容：主框架使用 `framework-demo`；`demo`、`run`、`benchmark`、`rl-demo`、`train-drl` 属于独立实验。
本次调整组织结构与命名，不改变排程算法、模型接口配置或业务验收标准。

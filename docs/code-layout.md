# 代码目录与阅读顺序

## 主框架入口

从 `enterprise_agent/agents/orchestrate.py` 的 `OrchestrateAgent` 开始阅读。
`application/demo.py` 负责组装依赖，`cli.py` 只负责命令入口。

```text
enterprise_agent/
  agents/
    orchestrate.py       中枢：组织、协调、仲裁、写回
    planning.py          计划：采集、门卫、工单优化
    scheduling.py        排单：候选策略、工序安排、直接协商
    warehouse.py         就绪复核：当前分配一致性与人工审核
    prediction.py        LLM前瞻推理；已知日历规则仅作显式对照
    proposals.py         LLM / 规则协调提案
  capabilities/
    admission.py         已就绪任务准入
    collection.py        事件应用与规范化
    prediction.py        独立实验使用的风险计算
    scheduling.py        排程构造、评分、可行性校验
    local_search.py      保留冻结工单的局部优化
    materials.py         物料批次与数量分配
    validation.py        提案、审批边界与写回条件
  domain/
    models.py            工单、工序、设备、计划
    contracts.py         事件、优先级、上下文、补丁
    ports.py             企业系统、预测、策略协议
    policy.py            学习策略回放资格
    runtime.py           任务租约、事件版本与处理器协议
  infrastructure/
    llm.py               HTTP 模型适配与工具 schema
    mes.py               内存 MES、版本校验
    review.py            四模块审核与方案绑定
    trace.py             同步调用审计记录
    task_store.py        SQLite任务队列、事件、产物、复核与回放
  application/
    operations_demo.py   前瞻预初始化与人机在环验证
    demo.py              主框架依赖组装与合成演示
    llm_monitor_demo.py  CLI/API共用的只读前瞻应用入口
    protocol_fixture.py 显式离线预先编写的模拟回复，不代表模型效果
    task_runtime.py      有界并发进程、租约、超时与重试
  web/
    api.py               独立HTTP API与事件流
    serve.py             API/静态前端独立启动入口
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
frontend/                HTML/CSS/JS独立Web客户端
```

Agent 使用共享能力完成计算。底层的 `EventCollector`、`KnownRiskPredictor`、
`BoundedSearchOptimizer` 和 `MaterialAllocator` 按实际职责命名，避免把每个计算类都叫 Agent。
`TraceLog` 记录同步调用轨迹，不承担消息队列或服务调度职责。

主框架的 agents/capabilities/domain/infrastructure/application 不依赖 experiments。
实验使用同一套共享能力，计算逻辑只维护一份。MES 演示网关会反向调用 Agent 的采集、门卫和仓库确认来复核补丁，当前没有把整个项目宣称为严格单向的分层架构。

## 运行入口

当前Web工作台分别使用 `platform-api` 和 `platform-ui`；参见 [平台运行说明](platform-runtime.md)。当前并发隔离单位是完整监控任务，初始化模块仍在该任务内调用，独立Agent任务调度待后续扩展。
命令行前瞻使用 `monitor-demo`，其他链路使用 `operations-demo` 或 `framework-demo`。
`demo`、`run`、`benchmark`、`rl-demo`、`train-drl` 用于独立实验。

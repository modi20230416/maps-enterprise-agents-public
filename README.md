# MAPS：多智能体生产计划与排程框架

MAPS 是一个面向离散制造的 Python 原型，展示 LLM 协调、确定性局部排程和仓库备料如何协作。框架围绕 **Orchestrate → Planning → Scheduling ↔ Warehouse** 组织，以 MES 基线和版本作为计划来源。

这是可运行的工程框架与合成数据演示。当前没有接入实际工厂系统，也未验证生产响应时限。所有示例订单、设备、库存和业务事件均为人工构造的数据。

## 快速开始

需要 Python 3.11 或更新版本。规则模式使用 Python 标准库，无须 API 密钥。

```bash
python -m enterprise_agent framework-demo
python -m unittest discover -s tests -v
```

输出位于 `artifacts/maps-framework-report.json`，默认忽略运行报告和数据库。

| 场景 | 结果 |
|---|---|
| `high_order` | 保护在制品和范围外工单，局部插入高优先级工单，模拟 MES 版本从 1 更新为 2 |
| `emergency_review` | 抢占申请停在 `PENDING_HITL`，等待人工审批和执行能力 |
| `material_conflict` | 缺料方案升级到 Orchestrate 仲裁，保持原计划 |
| `no_change` | 保留原计划，不调用 LLM，不增加 MES 版本 |

## Agent 如何分工

- **Orchestrate** 读取基线和事件，组织处理流程，在冲突时仲裁并记录原因。
- **Planning** 完成采集、规则门卫、已知窗口预测和局部工单优化。
- **Scheduling** 接收工单方案，确定工序安排并向 Warehouse 直接请求备料确认。
- **Warehouse** 核查共享物料数量、到货时间与备料提前量，返回齐套确认或冲突。

LLM 只能提出结构化协调建议。局部范围、在制品保护、交期、物料和版本检查由程序独立执行；模型没有审批权限或 MES 写入工具。

## 可选 LLM

支持 Chat Completions 与 Responses 协议的配置接口，实际兼容性取决于所选服务的工具调用支持。先把示例复制为本机配置，填入自己的 HTTPS 接口和模型 ID：

```powershell
Copy-Item llm.config.example.json llm.config.json
# 编辑 llm.config.json 中的 base_url、api、model 与兼容选项
.\run_llm.ps1
```

启动脚本隐藏读取 API 密钥，仅放入当前进程环境，不保存密钥。也可用环境变量配置后运行：

```bash
python -m enterprise_agent llm-status
python -m enterprise_agent framework-demo --agent llm --scenario high_order
```

`llm.config.json` 和 `.env` 被 Git 忽略；`.env.example` 只作说明，程序不会自动加载。示例接口使用保留占位域名，须替换后才能联网。调用模型时，相关事件、工单标识和局部证据会发送到所配置服务。测试中的模型回复均为协议夹具，不能作为真实服务联调成功的证据。

## 实验入口

旧的单时刻实验可用于比较规则、候选搜索和 DQN，独立于主框架：

```bash
python -m enterprise_agent demo
python -m enterprise_agent benchmark --cases 30 --seed 42
python -m enterprise_agent rl-demo
# DQN 训练需要可选 NumPy：
python -m pip install ".[drl]"
python -m enterprise_agent train-drl --episodes 400 --seed 42
```

没有附带训练权重或历史运行报告；可在本机重新生成。DQN 为单智能体实验，未证明优于规则与搜索，也未接入框架默认策略。MARL 目前只有接口和准入检查。

## 文档与目录

- [架构和决策流程](docs/architecture.md)
- [数据契约、示例与运行说明](docs/data-and-running.md)
- [LLM、学习策略与实现边界](docs/capabilities.md)
- [公开发布说明](docs/public-release.md)

```text
enterprise_agent/maps/   三层框架、企业接口协议、局部约束与模拟 MES
enterprise_agent/        排程核心、LLM 协议、持久化与独立实验
examples/                合成场景与显式 MES 基线
tests/                   约束、事务、协议、框架与训练验证
docs/                    面向外部读者的技术说明
artifacts/               本机生成的运行输出
```

## 实现边界

当前使用有界启发式搜索与内存 MES，不是 CP/MILP 或实际企业连接器。已开工工单整单冻结；未实现跨时刻执行、部分完工、消耗库存结转、抢占恢复、持久化审批或消息总线。Planning 和 Scheduling 已划分职责，但没有两个独立的生产调度周期。真实设备诊断、RAG、概率预测和 MARL 训练均待实现。

本仓库暂未附加开源许可证。

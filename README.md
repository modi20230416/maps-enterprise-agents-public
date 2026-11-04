# MAPS：多智能体生产计划与排程框架

MAPS 是一个面向离散制造的 Python 原型，展示 编排层前瞻、确定性局部排程和当前就绪复核如何协作。框架围绕 **Orchestrate → Planning → Scheduling ↔ Warehouse** 组织，以 MES 基线和版本作为计划来源。

这是可运行的工程框架与合成数据演示。当前没有接入实际工厂系统，也未验证生产响应时限。所有示例订单、设备、库存和业务事件均为人工构造的数据。

## Web 工作台

新增独立前端、HTTP API、有界并发任务进程，以及 SQLite 持久化、逐模块复核和事件回放。配置与演示步骤见 [平台运行说明](docs/platform-runtime.md)。

按步骤操作与现场讲解词见 [Trae 演示流程](docs/trae-demo.md)。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web,web-test,drl]"
# 两个终端分别启动：
.\run_platform.ps1 -Role api -WithFixtures
.\run_platform.ps1 -Role ui
# http://127.0.0.1:5173
```

模拟演示模式验证工程链路，不代表真实模型推断效果。真实 LLM 需要后端本机配置；当前并发单位是完整 Orchestrate 任务，下游初始化尚未拆成独立 Agent 服务。

## 快速开始

需要 Python 3.11 或更新版本。日历规则对照使用 Python 标准库，无须 API 密钥。monitor-demo 默认走LLM前瞻，需配置接口、模型和本机密钥。

```bash
python -m enterprise_agent monitor-demo --prediction calendar
python -m enterprise_agent operations-demo
python -m enterprise_agent framework-demo
python -m unittest discover -s tests -v
```

operations-demo 默认交互审核；可用 `--review-mode pending` 展示待审核，或用 `--review-mode scripted` 复现明确标记的模拟批准。报告显示三类上下文复用、冷启动结果一致和 MES 版本；单个合成案例不证明工业加速。

framework-demo 输出位于 `artifacts/maps-framework-report.json`，默认忽略运行报告和数据库。

| 场景 | 结果 |
|---|---|
| `high_order` | 生成局部插单候选，默认停在 PENDING_HITL，等待四模块审核 |
| `emergency_review` | 抢占申请停在 `PENDING_HITL`，等待人工审批和执行能力 |
| `material_conflict` | 不满足当前就绪条件，返回 NOT_ADMITTED，保持原计划 |
| `no_change` | 保留原计划，不调用 LLM，不增加 MES 版本 |

## 代码入口

从 [Orchestrate](enterprise_agent/agents/orchestrate.py) 开始阅读。完整目录和模块职责见 [代码结构](docs/code-layout.md)。

## Agent 如何分工

- **Orchestrate** 读取基线和事件，通过 LLM 对运行观测开展前瞻推理、准备下游上下文，协调并记录原因。
- **Planning** 完成采集、规则门卫和已就绪任务局部工单优化，不承担库存或到货预测。
- **Scheduling** 接收工单方案，确定工序安排并向 Warehouse 直接请求备料确认。
- **Warehouse** 复核当前已确认就绪状态及共享物料分配一致性。

涉及生产变更的候选必须获得四个模块对同一上下文和最终方案的人工批准；预初始化只读，不取得执行权限。新 Web 工作台的准备结果复核已持久化，尚无企业身份认证；旧命令行生产变更演示仍使用内存审核账本。

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

`llm.config.json` 和 `.env` 被 Git 忽略；`.env.example` 只作说明，程序不会自动加载。示例接口使用保留占位域名，须替换后才能联网。调用模型时，相关事件、工单标识和局部证据会发送到所配置服务。测试中的模型回复均为预先编写的模拟回复，不能作为真实服务联调成功的证据。

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

- [LLM前瞻推理与按需初始化](docs/llm-foresight.md)
- [已知日历规则对照](docs/orchestrate-monitoring.md)
- [当前技术路线与可执行验证](docs/operations-route.md)
- [关键技术验证记录与演示顺序](docs/validation.md)
- [架构和决策流程](docs/architecture.md)
- [数据契约、示例与运行说明](docs/data-and-running.md)
- [LLM、学习策略与实现边界](docs/capabilities.md)
- [公开发布说明](docs/public-release.md)

```text
enterprise_agent/agents/          Orchestrate、Planning、Scheduling、Warehouse
enterprise_agent/capabilities/    共享算法与业务校验
enterprise_agent/domain/          数据契约与接口协议
enterprise_agent/infrastructure/  模型通信、模拟 MES 与审计记录
enterprise_agent/application/     主框架组装
enterprise_agent/experiments/     早期编排、DQN 与基线实验
examples/                        合成输入
tests/                           行为与架构边界测试
docs/                            设计与运行说明
```

## 实现边界

当前使用有界启发式搜索与内存 MES，不是 CP/MILP 或实际企业连接器。已开工工单整单冻结；未实现跨时刻执行、部分完工、消耗库存结转、抢占恢复、持久化审批或消息总线。Planning 和 Scheduling 已划分职责，但没有两个独立的生产调度周期。真实设备诊断、RAG、概率预测和 MARL 训练均待实现。

本仓库暂未附加开源许可证。

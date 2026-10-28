> 当前目录与模块路径见 [代码结构](code-layout.md)。

# 数据与运行

## 业务对象

- MO 为制造需求标识，一个 MO 可对应多个 WO。
- WO 是框架接收的工单，核心模型 `Order.id` 对应 `wo_id`。
- OP 是工单内按顺序执行的工序，`Operation.id` 对应工序标识。

框架示例 `examples/maps_framework.json` 包含 `mes` 与 `event`。`mes` 提供版本、生产快照、已有计划及 MO/WO 目录；Agent 不自行生成这份基线。目录必须完整覆盖快照订单，并使用一致的优先级权重。自动 BOM 展开与 MO 拆批尚未实现。

时间使用相对场景起点的整数分钟，区间为左闭右开。设备不可用区间、工时、工序顺序、订单释放时间和物料齐套均参与排程校验。时间单位不代表已验证的计算响应速度。

## 事件

| 事件类型 | 监控对象 | payload |
|---|---|---|
| `order.created` | 插入工单 | `mo_id`、完整 `order` |
| `machine.unavailable` | 设备 | `machine_id`、`start`、`end` |
| `receipt.delayed` | 库存/到货 | `receipt_id`、`eta` |
| `inventory.corrected` | 库存 | `material`、`quantity` |
| `planning.tick` | 计划/执行检查 | 可为空 |

每个框架事件还包含 `id`、`at`、`priority`，以及可选 `safety_actions`。安全动作只接受 preempt/changeover/process_parameter_change。优先级标签通过固定字典转换为订单权重，不能用任意字符串覆盖。

当前一个事件的 `at` 必须等于快照 `now`。演示在 t=0 标记一个正在加工的工单，并冻结其全部安排，不模拟它随后完工。跨时刻执行进度、历史物料消耗和恢复加工尚未实现。

## 本地演示

```bash
python -m enterprise_agent framework-demo
python -m enterprise_agent framework-demo --scenario high_order
python -m enterprise_agent framework-demo --input examples/maps_framework.json --scenario no_change
```

每个内置场景重新装载合成 MES 基线，互不继承状态。高优先级插单演示结果：

| 工单 | 机台 | 原计划 | 新计划 |
|---|---|---|---|
| WO-RUN，在制 | M1 | 0–20 | 0–20 |
| WO-NORMAL，常规 | M1 | 20–50 | 35–65 |
| WO-HIGH，新插单 | M1 | 无 | 20–35 |
| WO-OTHER，范围外 | M2 | 80–90 | 80–90 |

`APPLIED` 只表示内存 MES 接受补丁。其他结果包括 KEEP、PENDING_HITL、REJECTED、ESCALATED、STALE、WRITE_REJECTED。报告的 `trace` 包含事件、门卫、提案、工单优化、物料协商、冲突、校验及版本确认。

KEEP 或拒绝分支不在内存 MES 中持久化外部观测，实际连接器每次必须读取最新上游状态。当前没有连续订阅或定时采集服务。

## 配置 LLM

复制 `llm.config.example.json` 为被忽略的 `llm.config.json`，填入服务提供方给出的接口和模型 ID。API 密钥只通过环境变量 `MAPS_LLM_API_KEY` 或启动脚本输入，不放入配置文件。配置文件只能存放非敏感字段。

环境变量优先于配置文件。第三方服务不会隐式使用 `OPENAI_API_KEY`；只有接口主机明确为 `api.openai.com` 时才允许该回退。两个协议与模型工具调用能力均应单独联调，`llm-status` 只检查配置，不会证明联网成功。

```powershell
Copy-Item llm.config.example.json llm.config.json
# 填写接口和模型后，隐藏输入密钥：
.\run_llm.ps1
```

规则场景和测试无需联网。模型调用失败或输出不合规时，框架记录 `llm.fallback`，再走规则流程。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试覆盖独立约束校验、局部范围、在制品保护、人工审批边界、过期写入、幂等、伪造物料确认、LLM 越权提案与候选回退。训练测试需要可选 NumPy，没有该依赖时明确跳过；不需要真实 API 密钥。

# LLM前瞻推理与按需初始化

当前Prediction的定位为：LLM根据当前运行状态与近期观测，推断可能发生的厂内扰动及需要提前准备的模块。旧版已知设备日历相交检查标记为`known_calendar_rules`，仅作为显式对照。

Orchestrate默认持有LLMPrediction；仅在显式日历对照入口注入KnownCalendarLookahead。默认构造延迟读取模型配置，准备与监控均遵循模型选择。

## 技术路线

运行状态与近期观测 → LLMPrediction调用已配置模型 → 结构化风险假设与模块请求 → 引用及范围校验 → Orchestrate按需初始化 → 记录准备结果与不确定性。

输入包括MES版本、当前工单与设备状态、剩余作业计划、截至当前时刻的工序进度观测。模型输入不包含预先写好的未来停机窗口或标准答案。

模型输出包括风险类型、受影响设备与工单、预计影响时间区间、观测依据ID、定性不确定性，以及各准备模块的用途。模型可返回空风险列表并说明证据不足。输出时间与风险均为推理假设，不作为已确认设备状态或经校准的概率。

Orchestrate检查对象ID、观测时间、未来时域和允许模块，按共享设备扩展影响范围并保护在制品。规则校验回答输出是否合规，不代替LLM生成风险。模型不可用或输出不合规时返回PREDICTION_UNAVAILABLE，停止准备，不用日历规则伪装为模型预测。

## 模块选择与初始化

LLM选择Planning、Scheduling、Warehouse的一个或多个模块，并为每项给出用途。Orchestrate只初始化被请求模块及其当前实现所需依赖：

- Planning：建立局部求解问题及固定占用。
- Scheduling：准备设备日历映射；其准备流程依赖Warehouse，依赖补充单独记录。
- Warehouse：建立当前工单索引。

请求只选Warehouse时，Planning和Scheduling的准备接口不调用。报告同时记录requested_modules、dependency_added_modules及实际module_initialization。所有准备均为只读，不产生优化决定、人工批准或MES写回。

## 演示输入

`examples/orchestrate_llm.json`提供合成工序进度：时刻0、10、20的已观测总进度分别为0、12%、24%。当前时刻20，基线表达从当前时刻开始的剩余作业：在制工单计划20–50，后续工单50–80。快照工时为计划剩余工时，模型上下文明确这一含义。

该输入没有未来设备不可用窗口。LLM需要自行判断进度趋势是否支持后续产能占用或延期风险，并选择准备模块。是否判断出风险、影响区间和理由，以真实调用结果为准，不在演示程序中写死。

## 本机调用

将接口及模型配置到本机llm.config.json，密钥通过环境变量或启动脚本隐藏输入；不随仓库分发密钥。

```powershell
.\run_llm.ps1 -Monitor
# 或已配置本机环境密钥时直接调用：
python -m enterprise_agent monitor-demo --prediction llm
```

真实调用报告默认保存到`artifacts/orchestrate-llm-live.json`。关注prediction_source、response_origin、live_call_verified、风险依据、requested_modules及实际初始化结果。

日历规则对照须明确指定：

```powershell
python -m enterprise_agent monitor-demo --prediction calendar
```

## 接入验证结果

已通过10项LLM专项测试和96项全套回归。专项测试覆盖Chat Completions及Responses协议、仅提供当前/历史观测、引用与时间校验、模型按需选择模块、Scheduling依赖补充、主动弃答及调用失败不转规则。

协议测试采用构造回复，标记为response_origin=protocol_fixture及live_call_verified=false。它们验证接口和执行控制，不证明模型的预测效果或准确率。按用户安排，本次未进行真实服务调用，实调将在本机提供密钥后进行。

实调评价应分别检查风险与观测的对应关系、模块选择是否合理、准备是否实际发生；风险是否实现、预计影响时间是否准确，需要后续实际结果或独立回放标签验证。

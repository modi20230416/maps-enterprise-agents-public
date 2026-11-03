# Orchestrate监控与前瞻只读演示

本演示聚焦Orchestrate的监控、已知设备窗口前瞻及准备触发。演示结束于下游上下文准备，不调用工单优化、人工批准或MES写回。研究对象仍为已确认满足生产前置条件的厂内任务。

## 如何体现Prediction驱动其他模块提前初始化

Orchestrate构造时持有`OperationsPrediction`。`_observe`调用`self.prediction.forecast(context)`取得未来冲突证据，`monitor`依据`should_prepare`调用`_initialize`；该方法实际调用Planning的局部问题准备接口，以及Scheduling的准备接口，后者继续调用Warehouse准备接口。

这里的初始化指求解与校验所需的计算上下文准备。Agent对象在程序启动时已创建，风险出现时再提前准备具体事件的上下文。演示报告的`preparation.module_initialization`逐模块列出真实准备结果：

| 模块 | 提前准备内容 | 首次风险观察后的实际结果 |
|---|---|---|
| Planning | 局部求解子问题 | 1个可移动工单、2项固定安排 |
| Scheduling | 设备日历映射 | 2台设备上下文就绪 |
| Warehouse | 当前工单索引 | 3张工单上下文就绪 |

新增因果验证：屏蔽Prediction风险证据时不调用准备；恢复风险证据后，三个模块的真实准备接口均被调用。当前监控专项11项、全套回归86项均通过。

## 具体工作与验收点

| 工作 | 实现内容 | 可核对的结果 |
|---|---|---|
| 监控入口 | `OrchestrateAgent.monitor`接收规范化事件，读取MES版本、设备日历、工单和原计划 | 事件ID、监控对象、2台设备、3张工单、在制冻结集合 |
| 状态判断 | 区分正常、未来风险、当前冲突、未准入和业务范围外事件 | NORMAL / FORECAST_RISK / CURRENT_CONFLICT / NOT_ADMITTED / OUT_OF_SCOPE |
| 窗口前瞻 | 前瞻时域内，检查已知设备不可用窗口与原计划占用是否相交 | 受影响机台与工序、两个窗口、重叠分钟数、提前量 |
| 准备触发 | 有可准备的未来风险时初始化局部问题、设备日历映射、任务索引 | should_prepare与PREPARED；正常观察为SKIPPED |
| 重复与变化处理 | 状态键相同复用已准备问题，窗口或版本变化时重新构建 | 首次build=1，重复cache_hit=true，变化后build=2 |
| 原理验证 | 4次合成观察回放、11个监控专项测试及86项全套回归 | MES版本与原计划保持不变，不调用求解决策或写回 |

上述内容已在本机实现并验证。监控采用事件回放入口，四次观察均基于同一个`now=0`的MES基线；观察序号表示输入次序。预告信息由输入明确给出，前瞻是确定性冲突检查。

## 前瞻规则与判断依据

记当前时刻为t，前瞻时长为H，已知设备不可用窗口为[u,v)，原计划工序窗口为[s,e)。本次检查条件是：t < u ≤ t+H，且s < v、e > u。

重叠时长为min(e,v)−max(s,u)，提前量为u−t。若设备窗口已在当前时刻生效并与未完成安排冲突，则报告CURRENT_CONFLICT，停止自动触发前瞻准备，交由后续处置流程处理。

案例：t=0、H=120、M1不可用窗口[30,45)、WO-NORMAL原计划[20,50)，重叠15分钟、场景提前量30分钟。影响范围为WO-NORMAL；WO-RUN已开工，属于冻结工单。预测结果仅为风险证据，监控和准备均不获得执行权限。

## 四步展示与预期结果

| 步骤 | 输入观察 | 监控状态 | 准备结果 |
|---|---|---|---|
| 1 | 正常基线检查 | NORMAL | SKIPPED，不构建问题 |
| 2 | 收到M1的[30,45)窗口 | FORECAST_RISK，重叠15分钟，提前30分钟 | PREPARED，cache_hit=false，builds=1 |
| 3 | 再次观察同一窗口 | FORECAST_RISK | PREPARED，cache_hit=true，builds仍为1 |
| 4 | 窗口改为[30,46) | FORECAST_RISK，重叠16分钟 | PREPARED，cache_hit=false，builds=2 |

重复通知只展示准备问题的复用，不代表已经实现消息系统的跨进程去重。四次观察结束后，MES仍为v1，原计划内容完全一致。

## 运行

```bash
python -m enterprise_agent monitor-demo
python -m enterprise_agent monitor-demo --pause
python -m unittest discover -s tests -p test_monitoring.py -v
```

规则模式无需联网或API密钥。按回车继续逐步演示，输入q可提前结束并保存部分结果。报告默认保存到`artifacts/orchestrate-monitor-demo.json`。

## 源码与断点

| 文件 | 重点位置 | 讲解内容 |
|---|---|---|
| enterprise_agent/application/monitor_demo.py | `engine.monitor(..., initialize=True)` | 观察输入和结果记录，末尾比较MES前后状态 |
| enterprise_agent/agents/orchestrate.py | `monitor`、`_observe` | 监控对象、当前/未来判断、准备触发 |
| enterprise_agent/agents/prediction.py | `forecast` | 前瞻时域、窗口相交、证据和提前量 |
| enterprise_agent/capabilities/local_search.py | `key`、`prepare` | 重复观察命中，窗口变化时失效 |

## 验证范围

本演示为同一决策时刻的合成事件回放，只验证监控判断、准备触发和只读边界。30分钟是场景窗口距离，不能作为计算加速或未知故障预测准确率。

# examples/ 验证材料索引

本目录存放每个验收场景的**输入、实际输出与运行记录（Trace）**。

- 生成模式：`real`，模型 `claude-opus-5-5`（默认），effort `medium`
- 每个场景两个文件：`<key>.json`（机器可读完整结果）与 `<key>.md`（人类可读执行路径）
- 生成命令：`python scripts/generate_examples.py --mode real`

> **Mock 边界说明**：`--mode mock` 产出的记录只验证「循环、工具、守卫、重规划链路」能跑通，**不能证明真实模型的决策效果**。本目录提交的记录由真实模型（`--mode real`）产出。

## 验收点覆盖

| 文件 | 场景 | 覆盖的验收点 | 实际终止状态 |
| --- | --- | --- | --- |
| [normal.md](normal.md) / [normal.json](normal.json) | 正常完成（预算充足） | 正常完成 | `completed` |
| [over_budget.md](over_budget.md) / [over_budget.json](over_budget.json) | 预算超限 -> 重规划选替代方案 | 预算超限（对应题目参考实例 12 -> 8） | `completed` |
| [tool_failure.md](tool_failure.md) / [tool_failure.json](tool_failure.json) | 工具失败 -> 调整计划且保留已成功结果 | 工具失败（注入一次失败） | `completed` |
| [missing_budget.md](missing_budget.md) / [missing_budget.json](missing_budget.json) | 缺预算 | 缺预算 | `incomplete_missing_input` |
| [missing_deadline.md](missing_deadline.md) / [missing_deadline.json](missing_deadline.json) | 缺截止时间 | 缺截止时间 | `incomplete_missing_input` |
| [infeasible_all.md](infeasible_all.md) / [infeasible_all.json](infeasible_all.json) | 无可行方案 -> 返回未完成及缺口 | 预算超限且无替代方案（返回缺口，不得虚报成功） | `incomplete_no_feasible_plan` |
| [tight_deadline.md](tight_deadline.md) / [tight_deadline.json](tight_deadline.json) | 截止时间过紧 -> 超期被拒 | 截止约束（超期方案不能保存） | `incomplete_no_feasible_plan` |
| [step_limit.md](step_limit.md) / [step_limit.json](step_limit.json) | 达到步数上限 -> 明确未完成状态 | 执行步数/工具调用上限 | `incomplete_step_limit` |
| [repeat_same.md](repeat_same.md) / [repeat_same_run1.json](repeat_same_run1.json) / [repeat_same_run2.json](repeat_same_run2.json) | 重复执行同一需求 -> 幂等命中，不重复计费 | 重复执行 | `run1=completed, run2=completed` |

## 至少两条执行路径的对比

考核要求提供至少两条执行路径的运行记录，体现模型根据不同工具结果调整动作。
本项目提供 9 组，其中最能说明「动态决策」的是这一对（下表数字取自本次实际运行）：

| | [normal.md](normal.md) | [over_budget.md](over_budget.md) |
| --- | --- | --- |
| 预算 | 20（宽裕） | 10（紧张） |
| 是否出现不可行估算 | **否** | **是** |
| `estimate_cost` 调用次数 | 1 | 3 |
| 被淘汰的组合 | — | m_short_video_pro, m_kol_top -> **不可行，缺口 2.0** |
| 步数 | 4 | 6 |
| 最终方案成本 | 18.0 / 20 | 8.0 / 10 |
| 终止状态 | `completed` | `completed` |

两条路径使用**同一份系统提示、同一套工具、同一个模型**，唯一差别是输入的预算，
执行路径却因工具返回结果不同而分岔 —— 这正是 ReAct 决策，而不是预设流程。

另外四组对照：
- [tool_failure.md](tool_failure.md)：注入一次 `generate_script` 失败，模型识别为瞬时错误后重试，仍完成，且成功的检索结果被保留
- [infeasible_all.md](infeasible_all.md)：无任何可行方案，模型探测出成本下界后主动停止（并避免无谓的脚本计费），报告缺口，没有把失败步骤记为成功
- [tight_deadline.md](tight_deadline.md)：预算充足但周期不可行，模型证明最小周期后停止，报告超出天数
- [step_limit.md](step_limit.md)：撞上步数上限，返回明确的未完成状态并保留已成功步骤

## 与评估文档的对应

本目录的文件被 [docs/03-evaluation.md](../docs/03-evaluation.md) 引用，
该文档逐条列出测试预期、实际结果与失败/限制。

## 测试结果

任务要求「Trace 与测试结果存放于 examples/」，因此测试结果也在本目录：

- [test-results.md](test-results.md) —— 离线断言与真实模型端到端测试的逐条结果、断言口径说明（具体条数以该文件为准）
- 重新生成：`python scripts/generate_test_results.py`（加 `--network` 可同时重跑真实模型套件）

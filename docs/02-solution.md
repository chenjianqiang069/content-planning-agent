# 02 · 方案设计

## 一、总体架构

```
┌─────────────────────────────────────────────────────────────┐
│  cli.py            运行方式（CLI）                            │
│  scenarios.py      9 个验收场景定义（CLI/测试/examples 共用）  │
│  runner.py         场景执行器（含 repeat 双运行）              │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  agent.py   ContentPlanningAgent —— ReAct 主循环              │
│                                                               │
│   while True:                                                 │
│     turn = llm.start(...) / llm.observe(tool_results)         │
│     if not turn.wants_tools:  → _finalize() 判定终止状态       │
│     if 达到上限:              → incomplete_step_limit          │
│     for call in turn.tool_calls:                              │
│         outcome = _execute(ctx, call)   ← 失败注入在这层拦截    │
│         _record_step(...)               ← Trace / 状态 / 计费  │
└──────┬──────────────────────────────────────┬────────────────┘
       │                                      │
┌──────▼───────────────┐          ┌───────────▼────────────────┐
│ llm.py               │          │ tools.py   ToolContext      │
│  AnthropicLLM (真实)  │          │  search_materials          │
│  MockLLM (离线)       │          │  generate_script           │
│  同一 LLMClient 接口   │          │  estimate_cost             │
│                      │          │  save_plan + 四类守卫        │
│ tool_schemas.py      │          │  FailureInjector           │
└──────────────────────┘          └───────────┬────────────────┘
                                              │
                                  ┌───────────▼────────────────┐
                                  │ data_store.py ← data/*.json │
                                  │ domain.py  数据结构与状态枚举 │
                                  │ trace.py   JSON + Markdown   │
                                  └─────────────────────────────┘
```

分层原则：**决策在模型，约束在代码**。模型决定做什么，代码决定什么不许做。

## 二、调用流程（一次完整运行）

以 `over_budget` 场景（预算 10，初始方案估算 12）为例，真实模型（`claude-opus-5-5`）的实际执行路径 —— 下表逐行摘自 [examples/over_budget.md](../examples/over_budget.md)：

| 步骤 | 工具 | 参数 | 返回摘要 | 模型决策依据（摘自 Trace） |
| --- | --- | --- | --- | --- |
| 1 | `search_materials` | `{}` | 命中 5 条素材 | 「先并行检索全部可用素材，并对运营给出的初始组合做成本与周期估算，判断是否需要降级」 |
| 2 | `estimate_cost` | `[m_short_video_pro, m_kol_top]` | 预计 **12.0** / 预算 10 → **不可行，缺口 2** | （同上，与检索并行发起） |
| 3 | `estimate_cost` | `[m_ugc_lite, m_kol_mid]` | 预计 **8.0** / 预算 10 → 可行 | 「初始方案 12.0 超预算 2.0，改用 economy 档组合；并行估算两套降级候选以择优，估算只读不计费」 |
| 4 | `estimate_cost` | `[m_live_studio, m_kol_mid]` | 预计 10.0 / 预算 10 → 可行 | （同上，并行候选之二） |
| 5 | `generate_script` | `[m_ugc_lite, m_kol_mid]` | 脚本成功，计费 1，累计 1 | 「两套均可行，优选 m_ugc_lite + m_kol_mid：8.0 留 2.0 缓冲、6 天上线、UGC 主题加成 5 最契合『新品种草』」 |
| 6 | `save_plan` | `[m_ugc_lite, m_kol_mid]` | **已保存 plan_791f6f9eae47，总成本 8.0，剩余 2.0** | 「脚本已成功生成且周期 6 天与估算一致，现保存同一组素材的方案」 |

终止状态 `completed`，共 6 步，花费 1.0 / 预算 10。

**这与题目参考实例完全一致**：预算 10、初始方案估算 12、替代方案估算 8，模型观察到超预算后改选 8 的方案，再估算和保存。

> **不要把这个 8.0 当成固定输出**。代码里没有任何"选最便宜组合"的逻辑，模型每次运行可能选到不同的可行组合 —— 早先一次运行就选了 9.0 的 `m_kol_top`+`m_ugc_lite`（在预算内留住更高曝光质量），同样正确。断言因此只校验**不变量**：`total_cost ≤ budget`、尝试过 ≥2 个组合、出现过超预算判定，不校验具体选了哪组素材。
>
> 同理，`examples/README.md` 的对比表由 `scripts/generate_examples.py` 从实际产出的 JSON 读取数字填充，而不是手写 —— 否则文档会与 Trace 打架（这个坑我踩过，见 [05-retrospective.md](05-retrospective.md) 教训 8）。本表是人工摘录的说明性走查，重跑真实模型后需与 `examples/over_budget.md` 核对。

### 循环的四个环节如何对应考核要求

| 考核要求的环节 | 代码位置 |
| --- | --- |
| 模型选择动作 | `LLMTurn.tool_calls` —— 来自模型输出的 `tool_use` blocks，参数也由模型给出 |
| 执行工具 | `ContentPlanningAgent._execute()` → `TOOL_IMPL[name](ctx, input)` |
| 观察结果 | 结果 JSON 编码后以 `tool_result` 回传，`llm.observe()` 交给模型 |
| 模型调整下一步或结束 | 下一轮 `turn`：要么新的 `tool_use`（调整），要么无工具调用的文本（结束） |

## 三、动态决策：为什么这不是预设流程

考核明确排除「仅有固定条件分支、自动重试或预设决策日志」。本项目的证据：

### 1. 代码里没有任何「选哪个方案」的分支

在 `src/` 全量搜索，不存在形如「if 超预算 then 选 economy」的逻辑。`tools.py` 的守卫只做**拒绝**：

```python
if not b.within_budget:
    return ToolOutcome(ok=False, payload={"rejected_by": "over_budget", ...},
                       error="预算超限，拒绝保存：... 请改用更便宜的素材组合后重新估算。")
```

拒绝之后走哪条路 —— 换哪组素材、要不要重新检索、是否放弃 —— 全由模型在下一轮决定。

### 2. 素材目录刻意不放进系统提示

初版我把素材清单直接写进系统提示，结果模型**跳过检索**、心算出可行性，一次都不撞超预算。这让工具形同虚设，也无法体现「观察工具结果再决策」。

修正后系统提示只保留**工具费用与耗时表**（规则层信息），并明确写：

> **素材目录不在本提示中**：有哪些素材、各自的 cost 与 lead_days，必须调用 `search_materials` 才能知道。不要凭猜测假设素材的成本或交期；任何成本结论都要以 `estimate_cost` 的返回为准。

修正后模型必须先检索、必须用 `estimate_cost` 核实，超预算路径才真实发生（见上表步骤 1-2）。

### 3. 同一提示、同一工具集，不同输入产生不同路径

`normal`（预算 20）与 `over_budget`（预算 10）使用完全相同的系统提示与工具集，唯一差别是预算输入，执行路径却分岔：

| | normal | over_budget |
| --- | --- | --- |
| 估算次数 | 1 次（首选即可行） | 3 次（初始被拒后并行估算两个替代候选） |
| 是否出现不可行判定 | 否 | 是（缺口 2） |
| 最终方案 | 3 条 premium 素材，成本 18.0 | economy 组合（UGC + 腰部 KOL），成本 8.0 |
| 步数 | 4 | 6 |

（表中数字取自本次真实运行；`examples/README.md` 的对比表由生成脚本从实际产出的 JSON 自动填充，不会与 Trace 漂移。）`tests/test_scenarios.py::test_two_execution_paths_differ` 把这个对比固化成断言。

### 4. 模型会自行并行探索

从 Trace 时间戳看，上表 6 个工具调用只用了 **4 轮**：第 1 轮并行发起 `search_materials` + `estimate_cost(初始方案)`（同一秒），第 2 轮并行发起两个 `estimate_cost`（两套降级候选，同一秒），第 3、4 轮分别是 `generate_script` 与 `save_plan`。模型主动把"检索"和"估算初始方案"、以及两个候选的估算并发出来以节省往返 —— 这不是我编排的，代码只是遍历 `turn.tool_calls` 并把同一轮的所有结果放进**同一条** user 消息回传（并行工具调用的正确姿势；拆开回传会让模型停止并行）。

代码用 `max_tool_calls` 而非仅 `max_steps` 作上限，正是因为一轮可能包含多个调用；`test_parallel_calls_over_budget_rejected_mid_turn` 验证同轮调用超出预算时多余的那些以 `is_error` 回传、不执行。

### 5. 失败处理有四条不同路径

| 触发 | 模型可观察到的信号 | 典型反应 |
| --- | --- | --- |
| 预算超限 | `feasible: false` + `over_budget_gap` + `hint` | 换更便宜素材组合重新估算 |
| 超出截止 | `feasible: false` + 超期天数 | 换 `lead_days` 更小的组合 |
| 工具报错 | `is_error: true` + 错误文本 | 重试 / 换参数 / 换替代方案 |
| 部分成功 | `partial: true` + `problems` 列表 | 替换问题素材后重新估算 |

## 四、状态与守卫设计（审核要点）

### 1. 预算计算只有一处口径

`ToolContext.breakdown()` 是唯一的成本计算入口，`estimate_cost` 和 `save_plan` 共用，避免「估算说可行、保存说不行」这类口径分裂：

```
projected_spend = already_spent + materials_cost + 仍需支付的脚本费
```

关键细节：**脚本费不重复计算**。为某组合成功生成脚本后，那 1 个单位已进入 `spent`，此时 `script_fee_remaining = 0`；反之若还没生成脚本，估算必须把这 1 个单位算进去（因为保存前必然要生成）。

对照题目参考实例：预算 10，premium 组合 = 素材 11 + 脚本 1 = **12**，economy 组合 = 素材 7 + 脚本 1 = **8**。测试 `test_breakdown_budget_math_matches_reference_instance` 与 `test_breakdown_economy_pair_is_feasible` 精确断言这两个数字。

### 2. 步骤依赖是真实校验

`save_plan` 要求存在「**同一组素材**」的成功 `generate_script`：

```python
sig = self.material_signature(material_ids)   # "|".join(sorted(set(ids))) —— 与顺序无关
script = self.scripts.get(sig)
if script is None:
    return ToolOutcome(ok=False, payload={"rejected_by": "missing_script_dependency", ...})
```

- `scripts` 只在**成功**时写入 —— 失败步骤不留下依赖凭据（`test_failed_script_leaves_no_dependency_credential`）
- 签名与素材顺序无关 —— `[A,B]` 与 `[B,A]` 是同一方案（`test_material_order_does_not_change_plan_id`）
- 为 A 组生成的脚本不能满足 B 组的保存（`test_script_for_different_materials_does_not_satisfy_dependency`）

### 3. 终止状态按真实执行结果判定

`_finalize()` 只认「执行时记录下来的 `plan_id` 且确实存在于方案库」：

```python
pid = self._saved_plan_id            # 来自 save_plan 成功返回的 payload
if pid and pid in ctx.saved_plans:   # 双重校验
    return ctx.saved_plans[pid]
```

不解析摘要字符串，也不采信模型文本里的「已完成」声明。测试 `test_model_claiming_success_without_save_is_not_completed` 让模型在没有保存的情况下宣称「方案已完成并保存」，断言状态**不是** `completed`。

### 4. 失败步骤的状态绝不会被美化

`StepRecord.status` 完全由 `ToolOutcome.ok` 推导；失败步骤 `charge` 强制为 0（失败不计费）。未保存时的原因分类也基于**结构化证据**（`step.detail` 里的 `feasible` / `rejected_by` / `over_budget_gap`），而不是匹配错误文本 —— 初版我用字符串匹配，导致模型「没调 save_plan 就放弃」的情况被误判为 `incomplete_not_saved`，实际应为 `incomplete_no_feasible_plan`。

### 5. 上限是硬约束

`max_steps=12` / `max_tool_calls=16`（`data/rules.json`）。循环在**每轮开头**检查：

```python
if len(result.steps) >= self.max_steps or tool_call_budget <= 0:
    result.status = RunStatus.INCOMPLETE_STEP_LIMIT
    result.reason = "达到执行上限...未保存方案，因此不计为完成。"
```

达到上限返回明确的未完成状态，并保留全部已成功步骤。同一轮内并行调用若超出预算，多余调用以 `is_error` 回传而不执行（`test_parallel_calls_over_budget_rejected_mid_turn`）。

### 6. 幂等（重复执行）

`plan_id = sha1(theme + 素材签名 + script_id)[:12]`。方案库 `saved_plans` 跨运行持久（由 `runner._run_repeat` 共享同一 dict 注入）。相同方案二次保存：返回既有记录、`duplicate=True`、**不重复计费、不重复写入**。

## 五、技术取舍

### 取舍 1：原生实现，不用 LangGraph / ADK / AgentScope

**决定**：原生 Python + `anthropic` SDK，手写 agentic loop。

**理由**：考核说「框架名称不加分，能解释取舍与修改结果才有价值」。本题的难点不在编排复杂度（单 Agent、四个工具、线性推进），而在**业务守卫的正确性**与**状态可核对性**。引入框架会带来三个具体代价：

1. 循环控制权旁落 —— 我必须精确控制步数上限、失败注入时机、逐步 Trace，而框架的内置循环会把这些吞进去；
2. 评审需要额外理解框架概念，而考核只给了 3 小时建议投入；
3. 版本与依赖面变大，离线可复现性变差。

原生实现的循环体不到 60 行，全部逻辑可直读，测试可以完全离线运行（56 个断言 0.16 秒）。

### 取舍 2：手写 loop 而非 SDK 的 `tool_runner`

**决定**：`client.messages.create()` + 自己维护 `messages`。

**理由**：`tool_runner` 是官方推荐的省事路径，但它把「调用工具 → 回传结果 → 继续」封在 SDK 内部。本题必须在这条链路上插入四件事：失败注入、步数/调用计数、逐步 Trace（含决策依据）、上限触发时的未完成返回。SDK 文档本身也指出：需要 SDK 未暴露的控制时应退回手写循环。这里正是那种情况。

**代价**：需要自己保证「assistant 消息原样回传（含 thinking / tool_use blocks）」，否则下一轮 400。已在 `_call()` 里处理。

### 依赖的 SDK：版本、使用的功能与承担的职责

题目要求「采用框架时，注明版本、使用的功能及其承担的职责」。本项目**未使用 LangGraph / ADK / AgentScope 等编排框架**（见取舍 1），只依赖官方 `anthropic` Python SDK 作为 API 客户端：

| 项 | 说明 |
| --- | --- |
| 包 | `anthropic`，`requirements.txt` 声明 `>=0.94.0`，开发验证版本 **0.94.1** |
| 使用的功能 | `anthropic.Anthropic()` 客户端、`client.messages.create()`（tool use：传 `tools` 与 `output_config={"effort":...}`；`tool_choice` 不显式传，用默认的 `auto` —— 让模型自主决定调不调工具、调哪个）、`response.content` 里的 `tool_use` / `text` blocks、`response.stop_reason`、`response.usage` |
| 承担的职责 | 只负责「HTTP 请求/响应、认证、超时与自动重试、把模型返回解析成 Python 对象」 |
| **没有**用它做的 | agentic 循环（自己写，见取舍 2）、工具执行、状态管理、守卫、Trace —— 全部是本项目代码 |

其余依赖只有 `pytest`（测试）。没有引入任何编排框架，因此不存在「框架承担的职责」需要交代；循环、重规划、守卫、幂等均由本项目自己实现，可完整审阅。

### 取舍 3：模型选择 `claude-opus-5-5` + `effort=medium`

**决定**：默认 Opus 5.5，effort `medium`，可用 `--model` / `--effort` 覆盖。

**理由**：本题需要模型做**多约束权衡**（成本 vs 交期 vs 质量 vs 主题契合）和**失败后重规划**，这是推理密集型任务而非分类任务，因此不降到更小的模型。`effort=medium` 是 Opus 5.5 的默认档位，实测足以稳定完成重规划（6 步内收敛），且比 `high` 省 token；`--effort` 保留给评审复现更高档位。

### 取舍 4：规则与数据外置到 `data/*.json`

**决定**：素材库、工具费用表、业务规则、上限、状态码全部放 JSON，代码只读取。

**理由**：审核要点是「核对预算计算、步骤依赖与实际状态」。阈值写在代码里，评审要读代码才知道口径；写成数据文件，评审打开 `rules.json` 就能看到 `formula`、`feasible_when`、`save_guards`。同时让「改预算规则」不需要改代码。

### 取舍 5：场景定义单一来源

**决定**：`scenarios.py` 里的 `Scenario` 对象同时被 CLI、`tests/test_scenarios.py`、`scripts/generate_examples.py` 使用。

**理由**：避免「文档写的用例」和「测试跑的用例」漂移。每个场景自带 `acceptance_point`（对应哪个验收点）和 `expect`（行为断言），改一处三处同步。

### 取舍 6：MockLLM 保留但明确降级

**决定**：提供离线 `MockLLM`（规则策略），但在代码、README、examples/README 里**反复标注边界**。

**理由**：题目要求「模型不可用时提供离线 Mock 入口并标明边界；Mock 只验证流程，不证明真实模型决策效果」。MockLLM 能跑通全部 9 个场景（含重规划链路），对离线测试有价值；但如果拿它的记录冒充模型决策效果就是误导。因此 `examples/` 提交的记录全部由真实模型产出，`RunResult.llm_mode` 字段（`real` / `mock` / `rejected_before_model`）会写进每份 Trace，Mock 产出的文件自带声明。

### 取舍 7：测试分三层，真实模型测试默认跳过

**决定**：
- 工具层单元（确定性，25 个）
- Agent 循环（`ScriptedLLM` 精确控制模型动作，确定性，20 个）
- 场景层（`MockLLM`，11 个）
- 真实模型端到端（6 个，标记 `network`，`pytest.ini` 默认 `-m "not network"` 排除）

**理由**：真实模型有不确定性与 API 成本。把循环行为用 `ScriptedLLM` 固化成确定性断言，才能既验证「模型观察到 X 后选择了 Y 会被正确处理」，又让默认 `pytest` 在 0.10 秒内离线跑完、不消耗 API。真实模型测试单独用 `-m network` 显式触发。

**代价**：`ScriptedLLM` 测的是「给定这串动作，循环是否正确处理」，不是「模型会不会产生这串动作」—— 后者只有 `-m network` 能验证。这个分工在 `tests/conftest.py` 的 docstring 里写明了。

## 六、已知设计限制

1. **交付周期是串行假设**：`max(lead_days) + 脚本 3 天`。真实场景中脚本撰写可与素材制作部分并行，模型会更短。选串行是为了让口径简单可核对。
2. **脚本费一次性**：同一组合重新生成脚本会再计费（`spent` 累加），这是有意的 —— 返工有成本。但跨运行的重复执行里，第二次运行会重新产生脚本费，因此幂等验收对比的是「方案库记录数」与「`duplicate` 标记」，而非两次运行的 `spent` 相等。这一点在 `scenarios.REPEAT_SCENARIO.expect_notes` 里写明。
3. **`infeasible_all` 场景的终止状态有弹性**：模型可能在估算阶段就判定所有组合不可行而直接停止（→ `incomplete_no_feasible_plan`），也可能反复尝试保存直到被守卫拒绝。两种都算正确处理，因此测试用 `status_in` 而非单一 `status`。
4. **上限检查在轮次开头**，因此实际步数可能等于上限但不会超过；同一轮内的并行调用若超预算，多余调用以错误回传（不执行），保证 `tool_call_count <= max_tool_calls`。
5. **`today` 固定为 2026-10-28**（`scenarios.FIXED_TODAY`）以保证截止天数可复现。自定义 `plan` 命令同样使用该固定日期，因此若评审在不同日期运行，`available_days` 不会漂移。

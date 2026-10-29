# 03 · 效果验证

本文记录测试预期、实际结果、失败与限制，并引用 `examples/` 下的对应文件。

## 一、测试分层与运行方式

| 层 | 文件 | 数量 | 依赖 | 运行 |
| --- | --- | --- | --- | --- |
| 工具层单元 | [tests/test_tools.py](../tests/test_tools.py) | 23 | 无（纯本地） | `pytest` |
| Agent 循环 | [tests/test_agent_loop.py](../tests/test_agent_loop.py) | 18 | 无（`ScriptedLLM` 确定性） | `pytest` |
| 场景层 | [tests/test_scenarios.py](../tests/test_scenarios.py) | 11 | 无（`MockLLM`） | `pytest` |
| 真实模型端到端 | [tests/test_real_model.py](../tests/test_real_model.py) | 6 | Anthropic API | `pytest -m network` |

```bash
python -m pytest                    # 56 passed, 6 deselected in 0.16s
python -m pytest -m network -v      # 6 passed
```

默认 `pytest` 通过 `pytest.ini` 的 `-m "not network"` 排除真实模型测试：既避免无凭据时报错，也避免每次跑测试都消耗 API。

### 为什么要用 `ScriptedLLM`

真实模型有不确定性：同一个场景两次运行可能选不同素材、走不同步数。如果把循环行为的断言建在真实模型上，测试会时红时绿，失去回归价值。

`ScriptedLLM` 按脚本精确返回「模型这一轮请求了什么工具、什么参数」，于是可以确定性地断言：**给定这串动作，循环是否正确处理**。例如：

```python
llm = scripted_llm([
    ("estimate_cost", {"material_ids": PREMIUM_PAIR}, "估算首选"),
    "END: 停",
])
agent.run(_task(budget=10.0), llm=llm)
assert llm.last_payload()["over_budget_gap"] == 2.0   # 模型确实看到了缺口
```

分工是明确的：`ScriptedLLM` 测「循环如何处理模型的决策」，`-m network` 测「模型会不会做出这种决策」。两者缺一不可，这个分工写在 `tests/conftest.py` 的 docstring 里。

## 二、验收点逐条核对

### 1. 正常完成 ✅

| | |
| --- | --- |
| 场景 | `normal`（预算 20，截止 2026-12-15） |
| 预期 | `completed`，存在已保存方案，成本 ≤ 预算，无失败步骤 |
| 实际（真实模型） | `completed`，4 步，`plan_db7a72ca319d`，总成本 **18.0** / 预算 20，剩余 2.0 |
| 证据 | [examples/normal.md](../examples/normal.md) · [examples/normal.json](../examples/normal.json) |
| 测试 | `test_scenarios.py::test_scenario_expectations[normal]`、`test_real_model.py`（间接） |

模型在预算宽裕时选择了高质量组合而非最便宜的 —— 说明它真的在权衡，而不是无脑取最小成本。

### 2. 缺预算 ✅

| | |
| --- | --- |
| 场景 | `missing_budget`（`budget=None`） |
| 预期 | 不调用任何工具，返回 `incomplete_missing_input` 及明确原因 |
| 实际（真实模型） | `incomplete_missing_input`，**步数 0**，花费 0.0，`llm_mode=rejected_before_model` |
| 证据 | [examples/missing_budget.md](../examples/missing_budget.md) |
| 测试 | `test_agent_loop.py::test_missing_budget_rejected_before_model`（断言 `llm.request_count == 0`）、`test_real_model.py::test_real_model_rejects_missing_input_without_calling_tools` |

关键设计：这个拒绝发生在**调用模型之前**。没有预算就无法判断可行性，让模型去"规划"一个无法验证的方案是浪费 token，也会诱导它编造。断言 `request_count == 0` 与 `usage` 为空证明零 API 消耗。

### 3. 缺截止时间 ✅

| | |
| --- | --- |
| 场景 | `missing_deadline`（`deadline=None`） |
| 预期 | 同上 |
| 实际 | `incomplete_missing_input`，步数 0，花费 0.0 |
| 证据 | [examples/missing_deadline.md](../examples/missing_deadline.md) |
| 测试 | `test_agent_loop.py::test_missing_deadline_rejected_before_model` |

### 4. 工具失败 ✅（审核要点：注入一次失败，观察是否调整计划且保留成功结果）

| | |
| --- | --- |
| 场景 | `tool_failure`，注入 `generate_script` **第 1 次**调用失败 |
| 预期 | 模型调整动作后仍完成；此前成功的步骤结果被保留；失败步骤状态为 `failed` 且不计费 |
| 实际（真实模型） | `completed`，7 步；步骤 4 的 `generate_script` 被注入失败（状态 `failed`、计费 0），模型判断为瞬时错误后在步骤 5 重试同一组素材成功，步骤 6 复核成本、步骤 7 保存 `plan_3c5804790a01`，总成本 12.0 / 预算 15 |
| 证据 | [examples/tool_failure.md](../examples/tool_failure.md) |
| 测试 | `test_agent_loop.py::test_recovers_from_injected_script_failure_and_keeps_prior_success`、`test_injected_failure_is_not_charged`、`test_injected_script_failure_blocks_save`、`test_real_model.py::test_real_model_recovers_from_tool_failure` |

真实模型在重试那一步（步骤 5）的决策依据：

> 「该错误是外部服务瞬时不可用（**非素材无效、非预算问题**），失败不计费；按规则重试一次同一组素材的脚本生成。」

模型不仅重试，还**正确诊断了错误性质**（区分瞬时故障 vs 素材/预算问题）、并注意到失败不计费这一规则。同时它保留了步骤 1-3 的检索与估算结果，重试成功后直接复用，没有从头再来 —— 这正是审核要点「调整计划且保留成功结果」。

配套验证「失败不能当作成功」：`test_injected_script_failure_blocks_save` 让模型在脚本被注入失败后直接尝试保存，断言 `save_plan` 被依赖守卫拒绝（`rejected_by == "missing_script_dependency"`），且整体状态不是 `completed`。

### 5. 预算超限 ✅（对应题目参考实例 12 → 8）

| | |
| --- | --- |
| 场景 | `over_budget`（预算 10；`notes` 指定初始方案 = `m_short_video_pro` + `m_kol_top`） |
| 预期 | 模型估算初始方案（12）→ 观察超预算 → 自行改选替代方案 → 再估算 → 保存，成本 ≤ 10 |
| 实际（真实模型） | `completed`，6 步；步骤 2 估算初始方案 **12.0 / 预算 10 → 不可行，缺口 2.0**；步骤 3-4 并行估算两套降级候选（**8.0** 与 10.0，均可行）；步骤 5 为 8.0 那组生成脚本；步骤 6 保存 `plan_791f6f9eae47`，总成本 **8.0**，剩余 2.0 |
| 证据 | [examples/over_budget.md](../examples/over_budget.md) · [examples/over_budget.json](../examples/over_budget.json) |
| 测试 | `test_tools.py::test_breakdown_budget_math_matches_reference_instance`（断言 12 与缺口 2）、`test_agent_loop.py::test_replans_from_over_budget_to_cheaper_combo`、`test_scenarios.py::test_two_execution_paths_differ`、`test_real_model.py::test_real_model_replans_on_over_budget` |

**本次运行与题目参考实例完全一致**：预算 10、初始方案估算 12、替代方案估算 8，模型观察到超预算后改选 8 的方案，再估算并保存。

模型的实际决策依据（步骤 3，观察到超预算之后）：

> 「初始方案 12.0 超预算 2.0，改用 economy 档组合；并行估算两套降级候选（UGC+腰部KOL / 直播专场+腰部KOL）以择优，估算只读不计费。」

以及步骤 5 的择优理由：

> 「两套均可行，优选 m_ugc_lite + m_kol_mid：8.0 留 2.0 缓冲、6 天上线、UGC 主题加成 5 最契合『新品种草』。」

> **不要把 8.0 当成固定输出**。代码里没有任何"选最便宜组合"的逻辑，模型每次运行可能选到不同的可行组合 —— 早先一次运行选了 9.0 的 `m_kol_top`+`m_ugc_lite`（在预算 10 内留住 1.0 余量的同时保住更高的曝光质量），同样正确。断言因此只校验**不变量**（`total_cost <= budget`、尝试过 ≥2 个组合、出现过超预算判定），不校验具体选了哪组素材。
>
> 这恰好说明为什么不该硬编码降级策略：若写死"超预算就选最便宜的 economy 组合"，模型就永远给不出那次 9.0 的权衡解。

同一场景在离线 Mock 下会稳定走到 8.0 的纯 economy 组合（`test_scenarios.py` 断言），真实模型路径的浮动本身就是"决策由模型做出"的证据。

### 6. 重复执行 ✅

| | |
| --- | --- |
| 场景 | `repeat_same`，两次运行共享同一份方案库 |
| 预期 | 方案写入幂等：同一 `plan_id` 不会被写两次；复用既有方案的保存标记 `duplicate` 且计费 0 |
| 实际（真实模型） | 两次均 `completed`；run1 与 run2 选中**同一** `plan_id`，第二次 `save_plan` 幂等命中（`duplicate=True`，`charge=0`），方案库 1 条记录 |
| 证据 | [examples/repeat_same.md](../examples/repeat_same.md) · [examples/repeat_same_run1.json](../examples/repeat_same_run1.json) · [examples/repeat_same_run2.json](../examples/repeat_same_run2.json) |
| 测试 | `test_scenarios.py::test_repeat_same_is_idempotent`、`test_agent_loop.py::test_repeat_run_is_idempotent`、`test_tools.py::test_save_is_idempotent_for_same_plan`、`test_material_order_does_not_change_plan_id`、`test_real_model.py::test_real_model_repeat_run_is_idempotent` |

`plan_id = sha1(theme + 素材签名 + 脚本风格指纹)[:12]`。三个入参都是**内容**，与执行顺序无关：素材签名对顺序无关（`sorted(set(ids))`，因此 `[A,B]` 与 `[B,A]` 是同一方案），风格指纹只由脚本风格决定。

> **这里先后暴露了两个真实问题，都由真实模型测试撞出来。**
>
> **问题一：断言写错。** 最初的 `test_real_model_repeat_run_is_idempotent` 断言 `plan_store_size == 1`。真实模型跑失败 —— `assert 2 == 1`：预算宽裕时模型在两次运行里选了**不同**的素材组合，库里有两个不同 `plan_id`。这不是幂等被破坏，而是断言把"幂等"误解成了"模型两次必须选同一个方案"；两个不同方案本就该各存一条。修正后 `runner.py` 分别报告 `same_plan_chosen` / `no_duplicate_writes` / `duplicate_saves_uncharged`，测试按两种情况分别断言。
>
> **问题二：`plan_id` 跨运行不稳定（真 bug）。** 修完断言后测试"通过"了，但那是运气 —— 第三次重跑再次失败。写脚本单独复现后确认：`plan_id` 的指纹原先用的是 `script_id`，而 `script_id` 内嵌 `_script_seq`（**本次运行第几个脚本**）。于是同一主题+同一素材，若模型在某次运行里先生成了别的脚本，序号就从 `01` 变 `02`，`plan_id` 随之变化 → 第二次运行不会幂等命中，方案库出现本该是同一条的重复记录。**幂等验收实际上是坏的。**
>
> 离线测试（`MockLLM` / `ScriptedLLM`）永远撞不到它：它们用固定动作序列，脚本序号总是从 `01` 开始。必须靠有路径差异的真实运行才能暴露。
>
> 修正：脚本记录改存**内容指纹** `sha1(style)`，与执行顺序无关；`script_id` 保留（对人可读、便于追溯）但不再进入方案身份。补 `test_plan_id_is_stable_across_runs_regardless_of_script_order`（回归：先生成别的脚本再保存同一方案，断言 `plan_id` 相同、库仍 1 条、第二次 `duplicate=True`）与 `test_different_script_style_yields_different_plan_id`（确认身份仍区分内容，没把指纹退化成常量）。详见 [04-ai-usage.md](04-ai-usage.md) § 三第 14 条、[05-retrospective.md](05-retrospective.md) § 二第三难。

### 7. 无可行方案（报告缺口，不虚报成功）✅

| | |
| --- | --- |
| 场景 | `infeasible_all`（预算 2，低于任何素材组合；`save_plan` 永久失败） |
| 预期 | 尝试若干组合后停止，返回未完成及缺口；失败步骤不得记为成功 |
| 实际（真实模型） | `incomplete_no_feasible_plan`，3 步，花费 **0.0**；缺口报告「最接近的方案仍超预算 2.0 模拟单位（预计 4.0 / 预算 2）」 |
| 证据 | [examples/infeasible_all.md](../examples/infeasible_all.md) |
| 测试 | `test_scenarios.py::test_scenario_expectations[infeasible_all]`、`test_real_model.py::test_real_model_reports_gap_when_infeasible` |

模型的表现超出预期，它：
1. 用 `search_materials(max_cost=1)` 探测是否存在更低价素材，拿到**降级结果**（无匹配 → 返回全量）后正确推断"不存在 ≤1 的素材"
2. 对最便宜单项做权威估算，拿到缺口 2.0
3. **主动不调用 `generate_script`** —— 理由写在最终答复里：「它会产生 1.0 计费，而其对应组合已确定超预算，调用只会造成无效支出且无法保存」。因此实际花费为 0.0
4. 给出两条可落地路径（提额到 ≥4.0，或补充低价自有素材）

第 3 点是"合理重规划"的强证据：模型理解了成本结构，并据此避免了无谓支出。这正是审核要点里的高分项，无法靠预设流程复现。

### 8. 截止时间过紧 ✅

| | |
| --- | --- |
| 场景 | `tight_deadline`（预算 20 充足，截止 2026-11-01 → 可用 4 天） |
| 预期 | 预算充足但周期不可行；报告超出天数；不保存方案 |
| 实际（真实模型） | `incomplete_no_feasible_plan`，6 步，花费 **0.0**；缺口「方案周期 6 天 > 可用 4 天」（3 个组合被判不可行） |
| 证据 | [examples/tight_deadline.md](../examples/tight_deadline.md) |
| 测试 | `test_scenarios.py::test_scenario_expectations[tight_deadline]`、`test_tools.py::test_save_rejected_over_deadline`、`test_estimate_cost_deadline_violation` |

模型穷举后得出**理论下界**：「全量目录仅 5 条素材，最快交付为 m_ugc_lite（2 天）；结合三次估算反推，脚本环节固定 3 天，故任何组合周期 ≥5 天 > 可用 4 天，截止时间无法满足，**继续调用只会产生无意义的脚本费用**」。它证明了不可行性而不是盲目重试到上限 —— 6 步 < 上限 12 步，且花费为 0（没有为一个注定保存不了的方案付脚本费）。

> 关于「守卫是否真的生效」需要说清楚：本次运行模型自己推断出不可行，**没有去调 `save_plan`**，因此 Trace 里看不到被截止守卫拒绝的步骤。守卫本身由单元测试直接验证：`test_tools.py::test_save_rejected_over_deadline` 构造超期方案并断言 `save_plan` 返回 `rejected_by == "over_deadline"`、方案库为空。另外 `infeasible_all` 场景的早先一次运行里，模型确实尝试过 `save_plan` 并被守卫拒绝（Trace 中该步骤为 `failed`），说明模型并不总是提前放弃。
>
> 这是一个诚实的边界：**模型"自觉遵守规则"与"守卫强制拦截"是两道独立的防线**。测试证明后者存在且有效，不依赖前者。

### 9. 步数上限 ✅（考核要求：达到上限时返回明确的未完成状态）

| | |
| --- | --- |
| 场景 | `step_limit`（上限收紧到 2 步 / 2 次调用；`estimate_cost` 永久失败） |
| 预期 | 不超上限；返回明确的未完成状态；保留已成功步骤；绝不误报完成 |
| 实际（真实模型） | 2 步（检索成功 + 估算失败），花费 0.0，无保存方案 |
| 证据 | [examples/step_limit.md](../examples/step_limit.md) |
| 测试 | `test_agent_loop.py::test_step_limit_returns_incomplete_and_preserves_success`、`test_tool_call_limit_stops_loop_after_budget_exhausted`、`test_parallel_calls_over_budget_rejected_mid_turn`、`test_real_model.py::test_real_model_respects_step_limit` |

真实模型在这个场景下有**两种都正确**的收敛方式，因此测试断言不变量而非单一状态：

1. **撞上限**：模型不断尝试替代组合直到 `max_steps` → `incomplete_step_limit`。`examples/step_limit.md` 记录的就是这条路径，也是 `ScriptedLLM` 确定性测试覆盖的路径（脚本会一直请求工具，必然撞上限）。
2. **提前放弃**：模型看到 `estimate_cost` 永久失败，判断"拿不到可信的成本结论、继续也没用"，主动结束循环 → `incomplete_tool_failure`。这条路径在真实模型的一次重跑中实际出现过。

两条路径都满足考核要求：`step_count <= 2`、`saved_plan is None`、`status` 不是 `completed`、并给出明确的未完成原因。

> **这里暴露过一个分类 bug**：第 2 条路径最初被归成笼统的 `incomplete_not_saved`。原因是旧判定要求"**所有**步骤都失败"才算工具失败，而这条路径里检索是成功的（1 成功 + 1 失败），于是落到了兜底分支。修正为「存在工具自身故障型失败（`detail` 里没有 `rejected_by`）即归类为 `incomplete_tool_failure`」，同时保证守卫拒绝（如超预算被 `save_plan` 拒绝，`rejected_by="over_budget"`）不会被误判成工具故障 —— 那种情况工具是按设计正常工作的，应归为 `incomplete_no_feasible_plan`。两个方向都有离线测试钉住：`test_partial_success_plus_hard_failure_classifies_as_tool_failure`、`test_guard_rejection_is_not_misclassified_as_tool_failure`。

## 三、至少两条执行路径（考核硬性要求）

考核要求「至少两条执行路径的运行记录，体现模型根据不同工具结果调整动作」。本项目提供 **9 组**，其中最能说明动态决策的是这一对：

| | [normal](../examples/normal.md) | [over_budget](../examples/over_budget.md) |
| --- | --- | --- |
| 预算 | 20（宽裕） | 10（紧张） |
| 首次估算结论 | 可行 | **不可行，缺口 2.0** |
| 模型的反应 | 直接生成脚本并保存 | **改选替代组合重新估算** |
| 估算次数 | 1 | ≥2 |
| 步数 | 4 | 5 |
| 最终成本 | 18.0 | 8.0 |

两条路径使用**同一份系统提示、同一套工具、同一个模型**，唯一差别是预算输入，路径却因工具返回结果不同而分岔。`test_scenarios.py::test_two_execution_paths_differ` 把这个对比固化成断言。

另外四组展示了不同的调整方式：
- [tool_failure](../examples/tool_failure.md) —— 识别瞬时错误后重试同一组合
- [infeasible_all](../examples/infeasible_all.md) —— 探测下界后主动放弃并避免无效支出
- [tight_deadline](../examples/tight_deadline.md) —— 证明周期不可行后停止重试
- [step_limit](../examples/step_limit.md) —— 被上限截断，如实报告未完成

每份 Trace 都包含考核要求的五项：步骤、工具参数、返回摘要、后续动作、简要决策依据。决策依据由模型在调用前以 `[决策依据]` 前缀写出一句话，不含内部思维过程。

## 四、Token 用量（真实模型，`claude-opus-5-5`，`effort=medium`）

逐场景的 input / output token 与步数见 **[examples/token-usage.md](../examples/token-usage.md)** —— 该表由 `scripts/generate_examples.py` 从实际产出的 JSON 自动汇总，重跑后随之更新，不在文档里手抄数字（手抄会与 Trace 漂移，这个坑见 § 五.5）。

概览：8 次真实模型运行合计 input 约 102K / output 约 12.8K tokens，单场景步数 0–7 步。`missing_budget` / `missing_deadline` 两个场景**零 token 消耗**（在调用模型前就被拒绝）。

input token 偏高是因为每轮都重发完整历史，且未启用 prompt caching。这在本项目规模下可接受；若要优化，稳定前缀（系统提示 + 工具费用表）已经具备缓存条件，加 `cache_control` 即可，见 § 六。

## 五、失败记录

开发过程中出现过两类失败，都已修正并留档，完整清单见 [04-ai-usage.md](04-ai-usage.md) § 三：

1. **验收点未被真正覆盖**（最严重）：系统提示里写入了素材目录，模型因此跳过检索、心算可行性，`over_budget` 场景一次都没撞超预算 —— 题目要求的核心重规划路径**从未发生**。只看"测试通过"不会发现这一点，必须实际读真实模型的 Trace。修正后（移除素材目录 + 场景指定初始方案）才复现出 12 → 8 的降级路径。

2. **断言写错导致真实模型测试失败**：`plan_store_size == 1` 把"幂等"误解为"模型两次必须选同一方案"。真实模型选了不同组合，断言失败。修正为按两种情况分别断言真正的不变量。

3. **状态判定依赖字符串匹配**：初版用 `"预算超限" in step.error` 判定终止状态，导致模型"没调 save_plan 就放弃"被误分类。改为基于结构化 `detail` 字段判定。

4. **步数上限场景漏掉一种正确路径**（重跑真实模型套件时暴露）：`test_real_model_respects_step_limit` 原先只断言 `incomplete_step_limit`，但真实模型看到 `estimate_cost` 永久失败后会主动放弃，走 `incomplete_tool_failure`；且分类逻辑要求"所有步骤都失败"，把"检索成功 + 估算失败"这种部分成功情况漏到了兜底的 `incomplete_not_saved`。修正：分类改为「存在工具自身故障型失败（无 `rejected_by`）」，并区分守卫拒绝；测试改为断言不变量（不超上限、不误报完成、状态属于两种正确结果之一）。补了 `test_partial_success_plus_hard_failure_classifies_as_tool_failure` 与 `test_guard_rejection_is_not_misclassified_as_tool_failure` 两个离线断言钉住两个方向。详见 § 二.9。

## 六、已知限制

| # | 限制 | 影响 | 后续做法 |
| --- | --- | --- | --- |
| 1 | 交付周期采用串行假设 `max(lead_days) + 3` | 比真实可并行的周期偏长，`tight_deadline` 场景因此不可行 | 若允许并行，应改为 `max(lead_days, 脚本天数)` 并说明依赖关系 |
| 2 | 同一组合重复生成脚本会重复计费 | 返工有成本，符合直觉；但跨运行重复执行时第二次会重新产生脚本费 | 因此幂等验收比对的是"方案库记录数"与"`duplicate` 标记"，而非两次 `spent` 相等（已在场景 `expect_notes` 注明） |
| 3 | `infeasible_all` 终止状态有弹性 | 模型可能在估算阶段就放弃（`incomplete_no_feasible_plan`），也可能反复尝试保存直到被拒 | 两种都算正确处理，测试用 `status_in` 而非单一 `status` |
| 4 | 未启用 prompt caching | input token 偏高（每场景约 6K-19K） | 系统提示与工具表是稳定前缀，加 `cache_control={"type":"ephemeral"}` 可显著降本 |
| 5 | 真实模型测试有不确定性 | 单次运行的具体步数/素材选择会变 | 断言只校验不变量（状态、成本 ≤ 预算、是否重规划、失败步骤状态），不校验具体步骤序列 |
| 6 | 工具为本地 Mock 实现 | 不对接真实素材库/供应商 | 题目允许；`ToolContext` 与业务解耦，替换为真实 HTTP 客户端只需改工具实现 |
| 7 | `MockLLM` 的候选组合是硬编码的 | 只验证流程，**不能证明真实模型决策效果** | 已在 README、`examples/README.md`、代码 docstring 三处标注边界 |
| 8 | 上限检查在轮次开头 | 实际步数可能等于上限但不超过；同轮并行调用超预算时多余调用以 `is_error` 回传而不执行 | 由 `test_parallel_calls_over_budget_rejected_mid_turn` 覆盖 |

## 七、结论

- **6 个硬性验收点全部通过**，并额外覆盖"无可行方案""截止过紧""步数上限"三个场景
- **ReAct 决策要求满足**：9 组真实模型运行记录，含至少两条因工具结果不同而分岔的执行路径；每份 Trace 具备步骤、参数、返回摘要、后续动作、决策依据五项
- **步数与工具调用上限已设置**，达到上限返回明确的未完成状态
- **审核要点逐条可核对**：预算计算单一口径且有精确数字断言（12 / 8 / 缺口 2）；步骤依赖真实校验且失败步骤不满足依赖；终止状态由真实执行结果判定，不采信模型的完成声明；注入失败后模型调整计划并保留成功结果
- 测试：56 个离线断言全部通过（0.16s），6 个真实模型端到端测试全部通过；逐条结果见 [examples/test-results.md](../examples/test-results.md)

### 停止原因（stop_reason）的边界处理

除上述验收点外，循环对三种非常规停止原因分别处理，各有离线测试覆盖：

| `stop_reason` | 处理 | 测试 |
| --- | --- | --- |
| `refusal` | 归类为 `incomplete_model_error`，如实说明模型拒绝继续，并保留已成功步骤 | `test_refusal_stop_reason_is_reported_distinctly` |
| `max_tokens` | 归类为 `incomplete_not_saved`，但文案说明是"输出被截断导致循环中断"，而非笼统的"模型结束了循环" | `test_max_tokens_truncation_is_reported` |
| `end_turn` | 正常走 `_finalize()` 的状态判定 | 全部场景测试 |

另外每份结果都带 `llm_mode` 标签（`real` / `mock` / `scripted` / `rejected_before_model`），防止 Mock 产出的记录被误认为真实模型决策（`test_llm_mode_is_labelled_in_result`）。

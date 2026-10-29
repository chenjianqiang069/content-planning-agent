# 测试结果记录

生成时间：2026-10-29 11:05 · 生成命令：`python scripts/generate_test_results.py --network`

本文件是任务要求的「测试结果」部分（`任务要求.md`：*Trace 与测试结果存放于 `examples/`*）。运行记录（Trace）见同目录下的各场景 `.md` / `.json`，
索引见 [README.md](README.md)，逐条核对见 [docs/03-evaluation.md](../docs/03-evaluation.md)。

## 一、离线套件（默认，不消耗 API）

```bash
python -m pytest
```

**结果：`====================== 56 passed, 6 deselected in 0.12s =======================`**

分三层，全部为确定性断言，不依赖网络与模型：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_tools.py` | 预算计算口径、四类守卫（预算/截止/依赖/幂等）、失败注入、部分成功 |
| `tests/test_agent_loop.py` | ReAct 循环行为：重规划、失败恢复并保留成功结果、步数与调用上限、同轮并行超限、终止状态判定、缺输入前置拒绝、stop_reason 边界、llm_mode 标注 |
| `tests/test_scenarios.py` | 9 个验收场景（MockLLM）+「两条执行路径必须不同」+「验收点全覆盖」元断言 |

> 默认 `pytest` 通过 `pytest.ini` 的 `-m "not network"` 排除真实模型测试，因此无凭据环境也能完整跑通、且不会消耗 API。

<details>
<summary>展开：56 条离线断言逐条结果</summary>

```text
============================= test session starts =============================
collecting ... collected 62 items / 6 deselected / 56 selected

tests/test_agent_loop.py::test_replans_from_over_budget_to_cheaper_combo PASSED [  1%]
tests/test_agent_loop.py::test_replan_observed_over_budget_payload PASSED [  3%]
tests/test_agent_loop.py::test_recovers_from_injected_script_failure_and_keeps_prior_success PASSED [  5%]
tests/test_agent_loop.py::test_injected_failure_is_not_charged PASSED    [  7%]
tests/test_agent_loop.py::test_injected_script_failure_blocks_save PASSED [  8%]
tests/test_agent_loop.py::test_step_limit_returns_incomplete_and_preserves_success PASSED [ 10%]
tests/test_agent_loop.py::test_tool_call_limit_stops_loop_after_budget_exhausted PASSED [ 12%]
tests/test_agent_loop.py::test_parallel_calls_over_budget_rejected_mid_turn PASSED [ 14%]
tests/test_agent_loop.py::test_model_claiming_success_without_save_is_not_completed PASSED [ 16%]
tests/test_agent_loop.py::test_all_tools_failed_classifies_as_tool_failure PASSED [ 17%]
tests/test_agent_loop.py::test_partial_success_plus_hard_failure_classifies_as_tool_failure PASSED [ 19%]
tests/test_agent_loop.py::test_guard_rejection_is_not_misclassified_as_tool_failure PASSED [ 21%]
tests/test_agent_loop.py::test_missing_budget_rejected_before_model PASSED [ 23%]
tests/test_agent_loop.py::test_missing_deadline_rejected_before_model PASSED [ 25%]
tests/test_agent_loop.py::test_repeat_run_is_idempotent PASSED           [ 26%]
tests/test_agent_loop.py::test_refusal_stop_reason_is_reported_distinctly PASSED [ 28%]
tests/test_agent_loop.py::test_max_tokens_truncation_is_reported PASSED  [ 30%]
tests/test_agent_loop.py::test_llm_mode_is_labelled_in_result PASSED     [ 32%]
tests/test_agent_loop.py::test_missing_input_does_not_consume_tokens PASSED [ 33%]
tests/test_agent_loop.py::test_decision_basis_recorded_in_steps PASSED   [ 35%]
tests/test_scenarios.py::test_scenario_expectations[normal] PASSED       [ 37%]
tests/test_scenarios.py::test_scenario_expectations[over_budget] PASSED  [ 39%]
tests/test_scenarios.py::test_scenario_expectations[tool_failure] PASSED [ 41%]
tests/test_scenarios.py::test_scenario_expectations[missing_budget] PASSED [ 42%]
tests/test_scenarios.py::test_scenario_expectations[missing_deadline] PASSED [ 44%]
tests/test_scenarios.py::test_scenario_expectations[infeasible_all] PASSED [ 46%]
tests/test_scenarios.py::test_scenario_expectations[tight_deadline] PASSED [ 48%]
tests/test_scenarios.py::test_scenario_expectations[step_limit] PASSED   [ 50%]
tests/test_scenarios.py::test_repeat_same_is_idempotent PASSED           [ 51%]
tests/test_scenarios.py::test_all_acceptance_points_covered PASSED       [ 53%]
tests/test_scenarios.py::test_two_execution_paths_differ PASSED          [ 55%]
tests/test_tools.py::test_breakdown_budget_math_matches_reference_instance PASSED [ 57%]
tests/test_tools.py::test_breakdown_economy_pair_is_feasible PASSED      [ 58%]
tests/test_tools.py::test_script_fee_not_double_counted_after_generation PASSED [ 60%]
tests/test_tools.py::test_estimate_cost_reports_verdict_and_gap PASSED   [ 62%]
tests/test_tools.py::test_estimate_cost_deadline_violation PASSED        [ 64%]
tests/test_tools.py::test_save_rejected_over_budget PASSED               [ 66%]
tests/test_tools.py::test_save_rejected_over_deadline PASSED             [ 67%]
tests/test_tools.py::test_save_rejected_without_script_dependency PASSED [ 69%]
tests/test_tools.py::test_script_for_different_materials_does_not_satisfy_dependency PASSED [ 71%]
tests/test_tools.py::test_failed_script_leaves_no_dependency_credential PASSED [ 73%]
tests/test_tools.py::test_unknown_material_rejected_on_save PASSED       [ 75%]
tests/test_tools.py::test_save_succeeds_when_feasible_and_dependency_met PASSED [ 76%]
tests/test_tools.py::test_save_is_idempotent_for_same_plan PASSED        [ 78%]
tests/test_tools.py::test_plan_id_is_stable_across_runs_regardless_of_script_order PASSED [ 80%]
tests/test_tools.py::test_different_script_style_yields_different_plan_id PASSED [ 82%]
tests/test_tools.py::test_material_order_does_not_change_plan_id PASSED  [ 83%]
tests/test_tools.py::test_failure_injector_fails_only_specified_call PASSED [ 85%]
tests/test_tools.py::test_failure_injector_fail_forever PASSED           [ 87%]
tests/test_tools.py::test_failed_tool_call_is_not_charged PASSED         [ 89%]
tests/test_tools.py::test_search_returns_all_when_no_filter PASSED       [ 91%]
tests/test_tools.py::test_search_filters_by_tier_and_cost PASSED         [ 92%]
tests/test_tools.py::test_search_degrades_to_partial_when_no_match PASSED [ 94%]
tests/test_tools.py::test_generate_script_partial_when_material_over_deadline PASSED [ 96%]
tests/test_tools.py::test_generate_script_empty_materials_is_error PASSED [ 98%]
tests/test_tools.py::test_summarize_marks_failure_clearly PASSED         [100%]

====================== 56 passed, 6 deselected in 0.12s =======================
```

</details>

## 二、真实模型端到端套件（需要 API 凭据）

```bash
python -m pytest -m network -v
```

捕获时间：2026-10-29 11:05

**结果：`======================== 6 passed in 353.45s (0:05:53) ========================`**

这一层验证的是**真实模型的决策效果**，不是流程本身 —— MockLLM 只能证明循环/工具/守卫跑通，只有这里能证明模型真的会根据工具结果重规划。

| 测试 | 验证的验收点 |
| --- | --- |
| `test_real_model_replans_on_over_budget` | 预算超限：估算初始方案 12 → 观察超限 → 改选替代方案 → 保存 |
| `test_real_model_recovers_from_tool_failure` | 工具失败：注入失败后调整动作、保留成功步骤、失败不计费 |
| `test_real_model_reports_gap_when_infeasible` | 无可行方案：尝试后停止并报告缺口，不虚报成功 |
| `test_real_model_respects_step_limit` | 步数上限：返回明确未完成状态 |
| `test_real_model_rejects_missing_input_without_calling_tools` | 缺预算：调用模型前拒绝，零 token 消耗 |
| `test_real_model_repeat_run_is_idempotent` | 重复执行：无重复写入、幂等保存不计费 |

<details>
<summary>展开：6 条端到端测试逐条结果</summary>

```text
============================= test session starts =============================
collecting ... collected 6 items

tests/test_real_model.py::test_real_model_replans_on_over_budget PASSED  [ 16%]
tests/test_real_model.py::test_real_model_recovers_from_tool_failure PASSED [ 33%]
tests/test_real_model.py::test_real_model_reports_gap_when_infeasible PASSED [ 50%]
tests/test_real_model.py::test_real_model_respects_step_limit PASSED     [ 66%]
tests/test_real_model.py::test_real_model_rejects_missing_input_without_calling_tools PASSED [ 83%]
tests/test_real_model.py::test_real_model_repeat_run_is_idempotent PASSED [100%]

======================== 6 passed in 353.45s (0:05:53) ========================
```

</details>

## 三、断言口径说明

真实模型有不确定性：同一场景两次运行可能选不同的可行素材组合、走不同步数。因此测试断言的是**不变量**而非某次具体行为：

- 终止状态正确（`completed` / 各类 `incomplete_*`）
- 已保存方案的成本 ≤ 预算、周期 ≤ 可用天数
- 出现过重规划（尝试 ≥2 个不同素材组合、且出现过 `feasible: false` 判定）
- 失败步骤的 `status` 恒为 `failed` 且 `charge` 恒为 0（失败绝不被记为成功、不计费）
- 步数与工具调用数不超上限
- 方案库无重复写入（同一 `plan_id` 不会被写两次）

这个取舍的由来见 [docs/05-retrospective.md](../docs/05-retrospective.md) § 二：最初把「幂等」误写成「模型两次必须选同一方案」，真实模型一跑就失败了 —— 预算宽裕时它有权在两次运行里选不同组合，那是两个不同方案，不是重复保存。

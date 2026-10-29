"""场景级测试：用离线 MockLLM 跑全部验收场景，断言每个验收点。

MockLLM 只验证流程（循环、工具、守卫、重规划链路能跑通），
不代表真实模型的决策效果 —— 真实模型的执行记录见 examples/ 与 tests/test_real_model.py。
"""

from __future__ import annotations

import pytest

from src.domain import RunStatus, StepStatus
from src.runner import run_scenario
from src.scenarios import SCENARIOS, REPEAT_SCENARIO, get_scenario


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_scenario_expectations(scenario):
    """每个场景按其 expect 定义断言行为，而不是写死具体步骤数。"""
    out = run_scenario(scenario, mode="mock")
    exp = scenario.expect
    result = out  # 非 repeat 场景返回 RunResult

    # --- 状态 ---
    if "status" in exp:
        assert result.status.value == exp["status"], (
            f"{scenario.key}: 期望 {exp['status']}，实际 {result.status.value}；{result.reason}"
        )
    if "status_in" in exp:
        assert result.status.value in exp["status_in"], (
            f"{scenario.key}: 期望 {exp['status_in']} 之一，实际 {result.status.value}"
        )

    # --- 无工具调用 ---
    if exp.get("no_tool_calls"):
        assert result.step_count == 0

    # --- 方案保存 ---
    if exp.get("has_saved_plan"):
        assert result.saved_plan is not None
    if exp.get("no_saved_plan"):
        assert result.saved_plan is None

    # --- 成本口径 ---
    if exp.get("spent_within_budget"):
        assert result.total_spent <= result.budget
    if exp.get("plan_cost_within_budget") and result.saved_plan:
        assert result.saved_plan.total_cost <= result.budget

    # --- 重规划：尝试过 >=2 个不同素材组合 ---
    if exp.get("replanned"):
        combos = {
            tuple(sorted(s.tool_input["material_ids"]))
            for s in result.steps
            if s.tool_input.get("material_ids")
        }
        assert len(combos) >= 2, f"{scenario.key}: 未观察到重规划，组合={combos}"
    if exp.get("tried_over_budget_combination"):
        # 至少有一次估算判定为不可行（超预算）
        assert any(s.detail.get("feasible") is False for s in result.steps), (
            f"{scenario.key}: 没有出现过超预算估算"
        )

    # --- 工具失败处理 ---
    if exp.get("has_failed_step"):
        assert any(s.status == StepStatus.FAILED for s in result.steps)
    if exp.get("failed_step_not_marked_success"):
        # 失败步骤的 status 必须是 failed，绝不能是 success
        for s in result.steps:
            if s.error and "注入" in (s.error or ""):
                assert s.status == StepStatus.FAILED
    if exp.get("successful_steps_preserved"):
        # 存在成功步骤（失败发生时，之前的成功结果没有被丢弃）
        assert any(s.status in (StepStatus.SUCCESS, StepStatus.PARTIAL) for s in result.steps)
    if exp.get("recovered_after_failure"):
        # 有失败但最终仍完成
        assert any(s.status == StepStatus.FAILED for s in result.steps)
        assert result.status == RunStatus.COMPLETED

    # --- 缺口报告 ---
    if exp.get("reports_gap"):
        assert "缺口" in result.reason or "超预算" in result.reason
    if exp.get("reports_deadline_gap"):
        assert "周期" in result.reason or "截止时间" in result.reason

    # --- 上限 ---
    if exp.get("respects_limits"):
        assert result.step_count <= result.limits["max_steps"]
        assert result.tool_call_count <= result.limits["max_tool_calls"]


def test_repeat_same_is_idempotent():
    """重复执行验收点：MockLLM 确定性，两次运行选同一方案，第二次幂等命中。

    幂等的真正不变量：同一 plan_id 不会被写入两次、复用既有方案的保存不重复计费。
    （真实模型可能两次选不同方案 —— 那是不同方案，不是重复保存；见 test_real_model.py）
    """
    out = run_scenario(REPEAT_SCENARIO, mode="mock")
    assert isinstance(out, dict)
    assert out["both_completed"] is True
    # MockLLM 确定性：两次选中同一 plan_id
    assert out["same_plan_chosen"] is True
    assert out["first"].saved_plan.plan_id == out["second"].saved_plan.plan_id
    # 幂等命中：第二次保存复用既有记录、方案库仍只有 1 条
    assert out["second_is_duplicate"] is True
    assert out["plan_store_size"] == 1
    assert out["no_duplicate_writes"] is True
    assert out["duplicate_saves_uncharged"] is True
    assert out["idempotent"] is True


def test_all_acceptance_points_covered():
    """确认题目要求的每个验收点都有对应场景（防止场景集漏项）。"""
    covered = {s.acceptance_point for s in SCENARIOS}
    covered |= {REPEAT_SCENARIO.acceptance_point}
    joined = " ".join(covered)
    for required in ["正常完成", "缺预算", "缺截止时间", "工具失败", "预算超限", "重复执行"]:
        assert required in joined, f"验收点未覆盖：{required}"


def test_two_execution_paths_differ():
    """考核要求「至少两条执行路径」：正常完成与预算超限重规划的路径必须不同。

    normal 一次通过（无不可行估算），over_budget 必须先撞超预算再降级，
    两者步骤序列不同，证明模型确实根据工具结果调整了动作，而非固定流程。
    """
    normal = run_scenario(get_scenario("normal"), mode="mock")
    over = run_scenario(get_scenario("over_budget"), mode="mock")

    normal_tools = [s.tool_name for s in normal.steps]
    over_tools = [s.tool_name for s in over.steps]

    # over_budget 估算了两次（首选 + 替代），normal 只估算一次
    assert over_tools.count("estimate_cost") >= 2
    assert normal_tools.count("estimate_cost") == 1
    # over_budget 出现过不可行判定，normal 没有
    assert any(s.detail.get("feasible") is False for s in over.steps)
    assert not any(s.detail.get("feasible") is False for s in normal.steps)

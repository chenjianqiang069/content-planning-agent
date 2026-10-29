"""真实模型端到端测试（默认跳过）。

运行方式：
    python -m pytest tests/test_real_model.py -m network -v

需要环境变量 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN。
这些测试验证的是「真实模型决策效果」，而不是流程本身：
MockLLM 只能证明循环/工具/守卫跑通，只有这里能证明模型会根据工具结果重规划。
"""

from __future__ import annotations

import os

import pytest

from src.domain import RunStatus, StepStatus
from src.llm import api_available
from src.runner import run_scenario
from src.scenarios import get_scenario

pytestmark = pytest.mark.network

requires_api = pytest.mark.skipif(
    not api_available(),
    reason="未检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，跳过真实模型测试",
)


@requires_api
def test_real_model_replans_on_over_budget():
    """核心验收：预算 10，首选组合估算 12，模型应观察到超限并改选估算 8 的方案。"""
    scenario = get_scenario("over_budget")
    result = run_scenario(scenario, mode="real", effort="medium")

    assert result.llm_mode == "real"
    assert result.status == RunStatus.COMPLETED, result.reason
    assert result.saved_plan is not None
    # 保存的方案成本必须在预算内
    assert result.saved_plan.total_cost <= result.budget
    # 真实模型必须尝试过至少两个组合（否则谈不上重规划）
    combos = {
        tuple(sorted(s.tool_input["material_ids"]))
        for s in result.steps
        if s.tool_input.get("material_ids")
    }
    assert len(combos) >= 2, f"模型没有重规划，只尝试了 {combos}"
    # 必须出现过一次超预算判定
    assert any(s.detail.get("feasible") is False for s in result.steps)
    # 每一步都在上限内
    assert result.step_count <= result.limits["max_steps"]


@requires_api
def test_real_model_recovers_from_tool_failure():
    """注入一次 generate_script 失败，模型应调整动作并最终完成，且保留成功步骤。"""
    scenario = get_scenario("tool_failure")
    result = run_scenario(scenario, mode="real", effort="medium")

    assert result.status == RunStatus.COMPLETED, result.reason
    failed = [s for s in result.steps if s.status == StepStatus.FAILED]
    assert len(failed) >= 1, "注入的失败没有出现在步骤里"
    # 失败步骤不能被记为成功，也不计费
    for s in failed:
        assert s.status == StepStatus.FAILED
        assert s.charge == 0.0
    # 成功步骤被保留
    assert any(s.status == StepStatus.SUCCESS for s in result.steps)


@requires_api
def test_real_model_reports_gap_when_infeasible():
    """预算 2 低于任何组合，模型应尝试后停止并报告缺口，不得虚报成功。"""
    scenario = get_scenario("infeasible_all")
    result = run_scenario(scenario, mode="real", effort="medium")

    assert result.status != RunStatus.COMPLETED
    assert result.saved_plan is None
    assert "缺口" in result.reason or "超预算" in result.reason


@requires_api
def test_real_model_respects_step_limit():
    """估算工具永久失败 + 上限 2 步：必须返回明确的未完成状态，且不超上限。

    真实模型有两种都正确的收敛方式，断言覆盖两者而非只认一种：
    - 撞上限：模型不断尝试替代组合直到 max_steps -> incomplete_step_limit
    - 提前放弃：模型看到 estimate_cost 永久失败，判断无法推进而主动结束
      -> incomplete_tool_failure（工具持续故障挡住了去路）
    离线确定性验证（ScriptedLLM 一定撞上限）见
    test_agent_loop.py::test_step_limit_returns_incomplete_and_preserves_success。
    这里验证真实模型的**不变量**：绝不超上限、绝不误报完成、给出明确未完成原因。
    """
    scenario = get_scenario("step_limit")
    result = run_scenario(scenario, mode="real", effort="medium")

    # 核心不变量
    assert result.step_count <= 2, f"超过上限：{result.step_count}"
    assert result.tool_call_count <= 2
    assert result.saved_plan is None  # 没保存就不算完成
    assert not result.status.is_completed
    assert result.status in (
        RunStatus.INCOMPLETE_STEP_LIMIT,
        RunStatus.INCOMPLETE_TOOL_FAILURE,
    )
    assert result.reason  # 有明确的未完成原因


@requires_api
def test_real_model_rejects_missing_input_without_calling_tools():
    """缺预算：即使有真实模型，也应在调用模型前拒绝（不产生 token 消耗）。"""
    scenario = get_scenario("missing_budget")
    result = run_scenario(scenario, mode="real", effort="medium")

    assert result.status == RunStatus.INCOMPLETE_MISSING_INPUT
    assert result.step_count == 0
    assert result.llm_mode == "rejected_before_model"


@requires_api
def test_real_model_repeat_run_is_idempotent():
    """重复执行：两次真实运行共享方案库，端到端幂等链路能跑通。

    真实模型在第二次运行里可能选出与第一次不同的素材组合（预算宽裕时它有权这么做），
    那会得到两个**不同**的 plan_id —— 这不是幂等被破坏，而是两个不同方案。
    幂等的真正不变量是：同一个 plan_id 绝不会被写入两次，复用既有方案的保存
    标记为 duplicate 且计费为 0。断言这两条，而不是断言「方案库只有 1 条」。

    诚实提示：本测试对「plan_id 是否跨运行稳定」没有鉴别力 —— bug 触发时（同组合
    但 plan_id 因运行内序号不同而不同）它会落入下面的 else 分支而照样通过。真正钉住
    plan_id 稳定性的是确定性单元测试：
    tests/test_tools.py::test_plan_id_is_stable_across_runs_regardless_of_script_order。
    本测试只负责验证真实模型端到端能完成重复执行链路。
    """
    from src.scenarios import REPEAT_SCENARIO

    out = run_scenario(REPEAT_SCENARIO, mode="real", effort="medium")
    first, second = out["first"], out["second"]
    # 该场景预算宽裕（20）、周期宽裕（48 天），两次运行都应当完成。
    # 断言带上各自的状态与原因，失败时能直接看出是哪一次、卡在哪，不必重跑排查。
    assert out["both_completed"] is True, (
        f"run1={first.status.value}（{first.reason[:200]}）; "
        f"run2={second.status.value}（{second.reason[:200]}）"
    )
    # 核心不变量：无重复写入（库大小 == 去重后的 plan_id 数）
    assert out["no_duplicate_writes"] is True
    # 任何复用既有方案的保存都不计费
    assert out["duplicate_saves_uncharged"] is True
    if out["same_plan_chosen"]:
        # 两次恰好选中同一方案时，必须观察到幂等命中，且库仍只有 1 条
        assert out["second_is_duplicate"] is True
        assert out["plan_store_size"] == 1
        assert out["idempotent"] is True
    else:
        # 两次选了不同方案：库里有 2 条不同记录，各自 plan_id 唯一
        assert out["plan_store_size"] == 2
        assert len(set(out["first_plan_ids"]) & set(out["second_plan_ids"])) == 0

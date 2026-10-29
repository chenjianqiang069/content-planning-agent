"""工具层单元测试：预算计算、四类守卫、失败注入、幂等、部分成功。

这一层不涉及模型，全部确定性断言，对应审核要点「核对预算计算、步骤依赖与实际状态」。
"""

from __future__ import annotations

import pytest

from src.domain import TaskInput
from src.tools import FailureInjector, summarize
from tests.conftest import make_context

# 素材库中的组合（cost）：
#   premium: m_short_video_pro=7, m_kol_top=4, m_live_studio=6
#   economy: m_ugc_lite=4, m_kol_mid=3
# generate_script fee=1（计入方案成本）
PREMIUM_PAIR = ["m_short_video_pro", "m_kol_top"]  # 11 + 1 脚本 = 12
ECONOMY_PAIR = ["m_ugc_lite", "m_kol_mid"]  # 7 + 1 脚本 = 8


def _task(budget=10.0, deadline="2026-12-15", theme="夏日气泡水"):
    return TaskInput(theme=theme, budget=budget, deadline=deadline)


# --------------------------------------------------------------------------
# 预算计算
# --------------------------------------------------------------------------
def test_breakdown_budget_math_matches_reference_instance(store, today):
    """参考实例：预算 10，premium 组合估算应为 12（含脚本费 1）且超预算。"""
    ctx = make_context(store, _task(budget=10.0), today)
    b = ctx.breakdown(PREMIUM_PAIR)
    assert b.materials_cost == 11.0
    assert b.tool_fees == 1.0  # 尚未生成脚本，脚本费仍需支付
    assert b.projected_spend == 12.0
    assert b.within_budget is False
    assert b.gap == 2.0


def test_breakdown_economy_pair_is_feasible(store, today):
    """替代方案：economy 组合估算应为 8，预算 10 下可行。"""
    ctx = make_context(store, _task(budget=10.0), today)
    b = ctx.breakdown(ECONOMY_PAIR)
    assert b.projected_spend == 8.0
    assert b.within_budget is True
    assert b.gap == 0.0


def test_script_fee_not_double_counted_after_generation(store, today):
    """生成脚本后，脚本费已计入 spent，估算时不应再重复加一次脚本费。"""
    ctx = make_context(store, _task(budget=10.0), today)
    # 先为 economy 组合生成脚本（花 1）
    out = ctx.generate_script({"material_ids": ECONOMY_PAIR})
    assert out.ok and out.charge == 1.0
    assert ctx.spent == 1.0
    # 再估算：projected = spent(1) + materials(7) + 待付脚本费(0，已生成) = 8
    b = ctx.breakdown(ECONOMY_PAIR)
    assert b.tool_fees == 0.0
    assert b.projected_spend == 8.0


def test_estimate_cost_reports_verdict_and_gap(store, today):
    ctx = make_context(store, _task(budget=10.0), today)
    out = ctx.estimate_cost({"material_ids": PREMIUM_PAIR})
    assert out.ok
    p = out.payload
    assert p["feasible"] is False
    assert p["over_budget_gap"] == 2.0
    assert "预算超限" in p["verdict"]
    assert "hint" in p  # 给模型的重规划提示


def test_estimate_cost_deadline_violation(store, today):
    """可用天数不足时报告超期，即使预算充足。"""
    ctx = make_context(store, _task(budget=20.0, deadline="2026-11-01"), today)
    out = ctx.estimate_cost({"material_ids": PREMIUM_PAIR})
    assert out.ok
    assert out.payload["within_budget"] is True
    assert out.payload["within_deadline"] is False
    assert out.payload["feasible"] is False


# --------------------------------------------------------------------------
# 保存守卫：预算
# --------------------------------------------------------------------------
def test_save_rejected_over_budget(store, today):
    """超预算时即使脚本已生成，保存也必须被拒，并给出缺口。"""
    ctx = make_context(store, _task(budget=10.0), today)
    ctx.generate_script({"material_ids": PREMIUM_PAIR})  # 先满足依赖
    out = ctx.save_plan({"material_ids": PREMIUM_PAIR})
    assert out.ok is False
    assert out.payload["rejected_by"] == "over_budget"
    assert out.payload["over_budget_gap"] > 0
    assert ctx.saved_plans == {}  # 确实没保存


def test_save_rejected_over_deadline(store, today):
    """周期超期时保存被拒。"""
    ctx = make_context(store, _task(budget=20.0, deadline="2026-11-01"), today)
    ctx.generate_script({"material_ids": ECONOMY_PAIR})
    out = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    assert out.ok is False
    assert out.payload["rejected_by"] == "over_deadline"


# --------------------------------------------------------------------------
# 保存守卫：步骤依赖
# --------------------------------------------------------------------------
def test_save_rejected_without_script_dependency(store, today):
    """没有成功脚本步骤时，保存必须被拒（步骤依赖守卫）。"""
    ctx = make_context(store, _task(budget=20.0), today)
    out = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    assert out.ok is False
    assert out.payload["rejected_by"] == "missing_script_dependency"
    assert ctx.saved_plans == {}


def test_script_for_different_materials_does_not_satisfy_dependency(store, today):
    """脚本是为 A 组生成的，保存 B 组时依赖不满足。"""
    ctx = make_context(store, _task(budget=20.0), today)
    ctx.generate_script({"material_ids": ECONOMY_PAIR})
    out = ctx.save_plan({"material_ids": PREMIUM_PAIR})  # 不同组合
    assert out.ok is False
    assert out.payload["rejected_by"] == "missing_script_dependency"


def test_failed_script_leaves_no_dependency_credential(store, today):
    """脚本生成失败后不留下依赖凭据 —— 失败步骤不能当作成功。

    注入型失败在 Agent 层拦截，其对应断言见
    tests/test_agent_loop.py::test_injected_script_failure_blocks_save；
    这里用参数非法导致的工具级失败验证同一性质：ctx.scripts 不写入。
    """
    ctx = make_context(store, _task(budget=20.0), today)
    out = ctx.generate_script({"material_ids": []})  # 参数非法 -> 工具级失败
    assert out.ok is False
    assert ctx.scripts == {}  # 失败没有留下依赖凭据
    # 直接保存 -> 依赖不满足
    save = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    assert save.ok is False
    assert save.payload["rejected_by"] == "missing_script_dependency"


def test_unknown_material_rejected_on_save(store, today):
    ctx = make_context(store, _task(budget=20.0), today)
    ctx.generate_script({"material_ids": ECONOMY_PAIR})
    out = ctx.save_plan({"material_ids": ECONOMY_PAIR + ["m_does_not_exist"]})
    assert out.ok is False
    assert out.payload["rejected_by"] == "unknown_material"


# --------------------------------------------------------------------------
# 正常保存
# --------------------------------------------------------------------------
def test_save_succeeds_when_feasible_and_dependency_met(store, today):
    ctx = make_context(store, _task(budget=10.0), today)
    ctx.generate_script({"material_ids": ECONOMY_PAIR})
    out = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    assert out.ok is True
    assert out.payload["duplicate"] is False
    plan = out.payload["saved_plan"]
    assert plan["total_cost"] == 8.0
    assert plan["remaining_budget"] == 2.0
    assert len(ctx.saved_plans) == 1


# --------------------------------------------------------------------------
# 幂等（重复执行）
# --------------------------------------------------------------------------
def test_save_is_idempotent_for_same_plan(store, today):
    """相同方案重复保存：返回既有记录、不重复计费、方案库仍只有 1 条。"""
    ctx = make_context(store, _task(budget=20.0), today)
    ctx.generate_script({"material_ids": ECONOMY_PAIR})
    first = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    assert first.ok and first.payload["duplicate"] is False
    spent_after_first = ctx.spent

    second = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    assert second.ok is True
    assert second.payload["duplicate"] is True
    assert second.payload["plan_id"] == first.payload["plan_id"]
    assert len(ctx.saved_plans) == 1  # 没有新增记录
    assert ctx.spent == spent_after_first  # 没有额外计费


def test_plan_id_is_stable_across_runs_regardless_of_script_order(store, today):
    """回归测试：同一方案的 plan_id 不能受「本次运行先生成了几个脚本」影响。

    曾经的 bug：plan_id 的指纹用了 script_id，而 script_id 内嵌 _script_seq
    （本次运行第几个脚本）。于是同一个主题+同一组素材，在两次运行里若模型
    先生成了别的脚本，序号就不同，plan_id 随之不同 -> 跨运行幂等失效，
    方案库出现重复记录。真实模型测试 test_real_model_repeat_run_is_idempotent
    就是这么暴露的。现在指纹只由脚本风格决定，与执行顺序无关。
    """
    shared: dict = {}

    def run(explore_first: bool, style: str = "种草") -> dict:
        ctx = make_context(
            store,
            _task(budget=20.0, deadline="2026-12-15", theme="夏日气泡水"),
            today,
            saved_plans=shared,
        )
        if explore_first:
            # 先为另一组素材生成脚本，占用序号（旧实现会让目标方案的序号变成 02）
            ctx.generate_script({"material_ids": ["m_short_video_pro"], "style": style})
        ctx.generate_script({"material_ids": ECONOMY_PAIR, "style": style})
        return ctx.save_plan({"material_ids": ECONOMY_PAIR}).payload

    a = run(explore_first=False)
    b = run(explore_first=True)

    assert a["plan_id"] == b["plan_id"], (
        f"同一方案的 plan_id 随执行顺序变化：{a['plan_id']} != {b['plan_id']}"
    )
    assert len(shared) == 1, f"方案库出现重复记录：{len(shared)} 条"
    assert b["duplicate"] is True  # 第二次是幂等命中


def test_different_script_style_yields_different_plan_id(store, today):
    """身份仍然区分内容：同素材不同脚本风格 -> 不同方案。"""
    shared: dict = {}

    def run(style: str) -> str:
        ctx = make_context(
            store, _task(budget=20.0, deadline="2026-12-15"), today, saved_plans=shared
        )
        ctx.generate_script({"material_ids": ECONOMY_PAIR, "style": style})
        return ctx.save_plan({"material_ids": ECONOMY_PAIR}).payload["plan_id"]

    assert run("种草") != run("促销")
    assert len(shared) == 2


def test_material_order_does_not_change_plan_id(store, today):
    """素材顺序不同但集合相同 -> 同一 plan_id（签名与顺序无关）。"""
    ctx = make_context(store, _task(budget=20.0), today)
    ctx.generate_script({"material_ids": ECONOMY_PAIR})
    a = ctx.save_plan({"material_ids": ECONOMY_PAIR})
    b = ctx.save_plan({"material_ids": list(reversed(ECONOMY_PAIR))})
    assert a.payload["plan_id"] == b.payload["plan_id"]
    assert b.payload["duplicate"] is True


# --------------------------------------------------------------------------
# 失败注入
# --------------------------------------------------------------------------
def test_failure_injector_fails_only_specified_call():
    inj = FailureInjector(fail_on_calls={"generate_script": [1]})
    assert inj.check("generate_script") is not None  # 第 1 次失败
    assert inj.check("generate_script") is None  # 第 2 次放行
    assert inj.check("generate_script") is None


def test_failure_injector_fail_forever():
    inj = FailureInjector(fail_forever={"save_plan": "配额用尽"})
    for _ in range(3):
        assert inj.check("save_plan") == "配额用尽"


def test_failed_tool_call_is_not_charged(store, today):
    """工具失败时不计费（参数错误导致 generate_script 失败的口径）。

    注：注入型失败在 Agent 层被拦截（impl 根本不会执行），其不计费口径由
    tests/test_agent_loop.py::test_injected_failure_is_not_charged 覆盖；
    这里验证工具自身返回失败时同样不计费。
    """
    ctx = make_context(store, _task(budget=20.0), today)
    assert ctx.spent == 0.0
    out = ctx.generate_script({"material_ids": []})  # 参数非法 -> 失败
    assert out.ok is False
    assert out.charge == 0.0
    assert ctx.spent == 0.0  # 失败没有计费
    # 对比：成功调用计费 1
    ok = ctx.generate_script({"material_ids": ECONOMY_PAIR})
    assert ok.ok and ctx.spent == 1.0


# --------------------------------------------------------------------------
# 检索 / 部分成功
# --------------------------------------------------------------------------
def test_search_returns_all_when_no_filter(store, today):
    ctx = make_context(store, _task(budget=20.0), today)
    out = ctx.search_materials({})
    assert out.ok
    assert out.payload["count"] == 5
    assert out.partial is False


def test_search_filters_by_tier_and_cost(store, today):
    ctx = make_context(store, _task(budget=20.0), today)
    out = ctx.search_materials({"tier": "economy", "max_cost": 4})
    ids = [m["id"] for m in out.payload["materials"]]
    assert set(ids) == {"m_ugc_lite", "m_kol_mid"}


def test_search_degrades_to_partial_when_no_match(store, today):
    """过滤无匹配时返回全部并标记 partial（部分成功），而非空结果。"""
    ctx = make_context(store, _task(budget=20.0), today)
    out = ctx.search_materials({"tags": ["完全不存在的标签xyz"]})
    assert out.ok
    assert out.partial is True
    assert out.payload["degraded"] is True
    assert out.payload["count"] == 5


def test_generate_script_partial_when_material_over_deadline(store, today):
    """某素材无法按期交付时，脚本部分成功并列出问题素材。"""
    ctx = make_context(store, _task(budget=20.0, deadline="2026-11-01"), today)
    # premium 组合 lead_days 较大，2026-11-01（可用 4 天）下会超期
    out = ctx.generate_script({"material_ids": PREMIUM_PAIR})
    assert out.ok is True
    assert out.partial is True
    assert out.payload["problems"]


def test_generate_script_empty_materials_is_error(store, today):
    ctx = make_context(store, _task(budget=20.0), today)
    out = ctx.generate_script({"material_ids": []})
    assert out.ok is False


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------
def test_summarize_marks_failure_clearly(store, today):
    ctx = make_context(store, _task(budget=10.0), today)
    out = ctx.save_plan({"material_ids": PREMIUM_PAIR})  # 无脚本依赖 -> 失败
    text = summarize("save_plan", out)
    assert text.startswith("失败")

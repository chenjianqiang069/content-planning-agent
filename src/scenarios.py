"""验收场景定义。

每个场景对应题目要求的一个验收点，可被 CLI、examples 生成脚本和测试共同引用，
保证「文档里写的用例」和「测试跑的用例」是同一份定义，不会漂移。

场景覆盖：
1. normal            正常完成
2. over_budget       预算超限 -> 重规划降级到替代方案
3. tool_failure      工具失败 -> 调整计划且保留已成功结果
4. missing_budget    缺预算
5. missing_deadline  缺截止时间
6. infeasible_all    无可行方案 -> 返回未完成及缺口
7. tight_deadline    截止时间过紧 -> 超期被拒
8. repeat_same       重复执行同一需求 -> 幂等命中
9. step_limit        步数上限 -> 明确未完成状态
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .domain import TaskInput

# 固定「今日」，让 deadline 场景可复现（否则天数随运行日期漂移）
FIXED_TODAY = "2026-10-28"


@dataclass
class Scenario:
    key: str
    name: str
    acceptance_point: str  # 对应的验收点
    task: TaskInput
    inject_fail_on_calls: dict[str, list[int]] = field(default_factory=dict)
    inject_fail_forever: dict[str, str] = field(default_factory=dict)
    # 覆盖默认步数/调用上限（None 表示用 rules.json 的默认值）
    max_steps: int | None = None
    max_tool_calls: int | None = None
    expect_status: str = ""
    expect_notes: str = ""
    # 期望的断言，供测试使用（不写死具体步骤数，只断言行为）
    expect: dict[str, Any] = field(default_factory=dict)


def _task(
    theme: str,
    budget: float | None,
    deadline: str | None,
    audience: str | None = None,
    notes: str | None = None,
) -> TaskInput:
    return TaskInput(
        theme=theme, budget=budget, deadline=deadline, audience=audience, notes=notes
    )


# 预算 10：premium 组合(7+4=11)+脚本费 1 = 12 > 10 -> 必须降级
# economy 组合(4+3=7)+脚本费 1 = 8 <= 10 -> 可行（对应题目参考实例 12 -> 8）
SCENARIOS: list[Scenario] = [
    Scenario(
        key="normal",
        name="正常完成（预算充足）",
        acceptance_point="正常完成",
        task=_task(
            theme="夏日气泡水新品种草",
            budget=20,
            deadline="2026-12-15",
            audience="18-30 岁都市白领",
            notes="希望用高质量素材，预算充足",
        ),
        expect_status="completed",
        expect_notes="预算宽裕，首选组合即可行，应一次通过：检索 -> 估算 -> 脚本 -> 保存。",
        expect={
            "status": "completed",
            "has_saved_plan": True,
            "no_failed_steps": True,
            "spent_within_budget": True,
        },
    ),
    Scenario(
        key="over_budget",
        name="预算超限 -> 重规划选替代方案",
        acceptance_point="预算超限（对应题目参考实例 12 -> 8）",
        task=_task(
            theme="夏日气泡水新品种草",
            budget=10,
            deadline="2026-12-15",
            audience="18-30 岁都市白领",
            notes=(
                "运营初步倾向的「初始方案」是：专业短视频定制 + 头部 KOL 矩阵投放"
                "（m_short_video_pro、m_kol_top）。请先评估这个初始方案；"
                "如果它超出预算，请自行寻找可行的替代方案，不要直接放弃任务。"
            ),
        ),
        expect_status="completed",
        expect_notes=(
            "对应题目参考实例：预算 10，初始方案估算 12（超预算），替代方案估算 8。"
            "模型必须先估算初始 premium 组合、观察到超预算，然后自行改选更便宜的组合，"
            "再估算并保存。关键看是否重规划，以及是否保留了前面成功的检索结果。"
        ),
        expect={
            "status": "completed",
            "has_saved_plan": True,
            "plan_cost_within_budget": True,
            "replanned": True,  # 至少尝试过两个不同素材组合
            "tried_over_budget_combination": True,
        },
    ),
    Scenario(
        key="tool_failure",
        name="工具失败 -> 调整计划且保留已成功结果",
        acceptance_point="工具失败（注入一次失败）",
        task=_task(
            theme="新品智能音箱发布",
            budget=15,
            deadline="2026-12-20",
            audience="科技数码爱好者",
            notes="需要短视频 + KOL 投放",
        ),
        inject_fail_on_calls={"generate_script": [1]},
        expect_status="completed",
        expect_notes=(
            "第 1 次 generate_script 被注入失败。模型应观察到失败、调整动作（重试或换组合），"
            "且此前成功的 search_materials 结果必须保留并继续使用；失败步骤不能被记为成功。"
        ),
        expect={
            "status": "completed",
            "has_saved_plan": True,
            "has_failed_step": True,
            "failed_step_not_marked_success": True,
            "successful_steps_preserved": True,
            "recovered_after_failure": True,
        },
    ),
    Scenario(
        key="missing_budget",
        name="缺预算",
        acceptance_point="缺预算",
        task=_task(
            theme="夏日气泡水新品种草",
            budget=None,
            deadline="2026-12-15",
            audience="18-30 岁都市白领",
        ),
        expect_status="incomplete_missing_input",
        expect_notes="没有预算就无法判断成本可行性，应在调用模型前就拒绝启动，返回明确未完成状态。",
        expect={
            "status": "incomplete_missing_input",
            "no_tool_calls": True,
            "no_saved_plan": True,
        },
    ),
    Scenario(
        key="missing_deadline",
        name="缺截止时间",
        acceptance_point="缺截止时间",
        task=_task(
            theme="夏日气泡水新品种草",
            budget=10,
            deadline=None,
            audience="18-30 岁都市白领",
        ),
        expect_status="incomplete_missing_input",
        expect_notes="没有截止时间就无法判断周期可行性，同样应拒绝启动并返回未完成状态。",
        expect={
            "status": "incomplete_missing_input",
            "no_tool_calls": True,
            "no_saved_plan": True,
        },
    ),
    Scenario(
        key="infeasible_all",
        name="无可行方案 -> 返回未完成及缺口",
        acceptance_point="预算超限且无替代方案（返回缺口，不得虚报成功）",
        task=_task(
            theme="全渠道品牌年度大事件",
            budget=2,
            deadline="2026-12-31",
            audience="全量用户",
            notes="希望做一场大型品牌活动",
        ),
        inject_fail_forever={"save_plan": "存储配额已用尽，方案无法写入（注入的模拟故障）"},
        expect_status="incomplete_no_feasible_plan",
        expect_notes=(
            "预算 2 低于最便宜素材组合，任何方案都超预算；save_plan 又永久失败。"
            "模型应尝试若干组合后停止，返回未完成及缺口，绝不能把失败步骤记为成功。"
        ),
        expect={
            "status_in": [
                "incomplete_no_feasible_plan",
                "incomplete_tool_failure",
                "incomplete_not_saved",
            ],
            "no_saved_plan": True,
            "reports_gap": True,
            "failed_step_not_marked_success": True,
        },
    ),
    Scenario(
        key="tight_deadline",
        name="截止时间过紧 -> 超期被拒",
        acceptance_point="截止约束（超期方案不能保存）",
        task=_task(
            theme="夏日气泡水快闪促销",
            budget=20,
            deadline="2026-11-01",
            audience="本地年轻人",
            notes="预算充足但要尽快上线",
        ),
        expect_status="incomplete_no_feasible_plan",
        expect_notes=(
            "可用天数 4 天，而任何素材组合的周期都是 lead_days + 脚本 3 天 >= 5 天，"
            "预算充足但周期不可行。模型应观察到超期并尝试更快素材，最终若无解则报告超出天数。"
        ),
        expect={
            "status_in": ["incomplete_no_feasible_plan", "incomplete_not_saved"],
            "no_saved_plan": True,
            "reports_deadline_gap": True,
        },
    ),
    Scenario(
        key="step_limit",
        name="达到步数上限 -> 明确未完成状态",
        acceptance_point="执行步数/工具调用上限",
        task=_task(
            theme="夏日气泡水新品种草",
            budget=10,
            deadline="2026-12-15",
            audience="18-30 岁都市白领",
        ),
        inject_fail_forever={"estimate_cost": "估算服务超时（注入的模拟故障）"},
        max_steps=2,
        max_tool_calls=2,
        expect_status="incomplete_step_limit",
        expect_notes=(
            "把上限调紧到 2 步 / 2 次调用，并让估算工具永久失败：模型检索成功后估算失败，"
            "再想继续时就会撞上上限。验证达到上限时返回明确的未完成状态，"
            "并保留已成功步骤（检索结果）；未保存方案，因此不计为完成。"
        ),
        expect={
            "status": "incomplete_step_limit",
            "no_saved_plan": True,
            "successful_steps_preserved": True,
            "respects_limits": True,
        },
    ),
]

SCENARIO_BY_KEY = {s.key: s for s in SCENARIOS}

# 重复执行验收：同一需求跑两次，共用 saved_plans 存储，第二次应幂等命中
REPEAT_SCENARIO = Scenario(
    key="repeat_same",
    name="重复执行同一需求 -> 幂等命中，不重复计费",
    acceptance_point="重复执行",
    task=_task(
        theme="夏日气泡水新品种草",
        budget=20,
        deadline="2026-12-15",
        audience="18-30 岁都市白领",
        notes="重复提交同一需求，验证幂等",
    ),
    expect_status="completed",
    expect_notes=(
        "两次运行使用同一份 saved_plans 存储。第二次保存应命中幂等：返回既有 plan_id、"
        "不重复计费、方案记录数仍为 1。第一次运行已花费的脚本费在第二次运行中会重新产生，"
        "因此对比的是「方案库记录数」与「第二次 save_plan 的 duplicate 标记」。"
    ),
    expect={
        "status": "completed",
        "has_saved_plan": True,
        "idempotent": True,
    },
)


def get_scenario(key: str) -> Scenario | None:
    return SCENARIO_BY_KEY.get(key)


def all_keys() -> list[str]:
    return [s.key for s in SCENARIOS] + [REPEAT_SCENARIO.key]

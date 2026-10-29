"""场景执行器：把 Scenario 定义跑成 RunResult，并处理「重复执行」这类多运行场景。

CLI、examples 生成脚本、测试都通过这里执行，保证三者跑的是同一条路径。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .agent import ContentPlanningAgent
from .data_store import DataStore
from .domain import PlanRecord, RunResult
from .llm import MockLLM, api_available, build_anthropic_llm
from .scenarios import FIXED_TODAY, REPEAT_SCENARIO, Scenario, get_scenario
from .tools import FailureInjector


def parse_fixed_today() -> date:
    return datetime.strptime(FIXED_TODAY, "%Y-%m-%d").date()


def build_llm(mode: str = "auto", model: str | None = None, effort: str = "medium") -> Any:
    """mode: real | mock | auto（auto=有密钥用真实模型，否则回退 mock）"""
    if mode == "real":
        if not api_available():
            raise RuntimeError(
                "要求使用真实模型，但未检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN。"
            )
        return build_anthropic_llm(model=model, effort=effort)
    if mode == "mock":
        return MockLLM()
    # auto
    if api_available():
        return build_anthropic_llm(model=model, effort=effort)
    return MockLLM()


def make_injector(scenario: Scenario) -> FailureInjector:
    return FailureInjector(
        fail_on_calls=dict(scenario.inject_fail_on_calls),
        fail_forever=dict(scenario.inject_fail_forever),
    )


def run_scenario(
    scenario: Scenario,
    mode: str = "auto",
    model: str | None = None,
    effort: str = "medium",
    store: DataStore | None = None,
    verbose: bool = False,
) -> RunResult | dict[str, Any]:
    """执行一个场景。

    普通场景返回 RunResult；repeat_same 场景执行两次运行并返回汇总 dict
    （因为重复执行验收点本身就是「两次运行之间的关系」）。
    """
    store = store or DataStore()
    today = parse_fixed_today()

    if scenario.key == REPEAT_SCENARIO.key:
        return _run_repeat(scenario, mode=mode, model=model, effort=effort, store=store, today=today, verbose=verbose)

    llm = build_llm(mode=mode, model=model, effort=effort)
    agent = ContentPlanningAgent(
        store=store,
        injector=make_injector(scenario),
        max_steps=scenario.max_steps,
        max_tool_calls=scenario.max_tool_calls,
        today=today,
    )
    if verbose:
        from .trace import print_trace  # 延迟导入避免循环

        agent.on_step = lambda s: print(
            f"  · 步骤 {s.index} {s.tool_name} -> {s.status.value}"
        )
    result = agent.run(scenario.task, llm=llm)
    return result


def _run_repeat(
    scenario: Scenario,
    mode: str,
    model: str | None,
    effort: str,
    store: DataStore,
    today: date,
    verbose: bool = False,
) -> dict[str, Any]:
    """重复执行：两次运行共用同一份 saved_plans 存储，验证幂等。"""
    shared_plans: dict[str, PlanRecord] = {}

    runs: list[RunResult] = []
    for i in (1, 2):
        # 每次运行都要新建 injector 和 llm（各自独立的一次 ReAct 会话），
        # 但共享 saved_plans —— 这模拟了「方案库已持久化」的真实情况。
        llm = build_llm(mode=mode, model=model, effort=effort)
        agent = ContentPlanningAgent(
            store=store,
            injector=make_injector(scenario),
            today=today,
            saved_plans=shared_plans,
        )
        runs.append(agent.run(scenario.task, llm=llm))

    first, second = runs[0], runs[1]

    def saved_ids(run: RunResult) -> list[str]:
        return [
            s.detail["plan_id"]
            for s in run.steps
            if s.tool_name == "save_plan" and s.detail.get("plan_id")
        ]

    def duplicate_hits(run: RunResult) -> list[str]:
        return [
            s.detail["plan_id"]
            for s in run.steps
            if s.tool_name == "save_plan" and s.detail.get("duplicate") is True
        ]

    first_ids, second_ids = saved_ids(first), saved_ids(second)
    second_duplicate = bool(duplicate_hits(second))
    same_plan = bool(set(first_ids) & set(second_ids))

    # 幂等的真正含义：**同一个 plan_id 不会被写入两次、不会被重复计费**。
    # 真实模型在第二次运行里完全可能选出另一组素材 —— 那是一个「不同的方案」，
    # 不是「同一方案的重复保存」，因此方案库里出现 2 条不同记录并不算幂等被破坏。
    # 真正的不变量是：库里每个 plan_id 唯一（无重复写入），且任何复用既有
    # plan_id 的保存都被标记 duplicate 且计费为 0。
    no_duplicate_writes = len(shared_plans) == len(
        {pid for pid in first_ids + second_ids}
    )
    duplicate_saves_uncharged = all(
        s.charge == 0.0
        for s in second.steps
        if s.tool_name == "save_plan" and s.detail.get("duplicate") is True
    )

    return {
        "scenario": scenario,
        "runs": runs,
        "first": first,
        "second": second,
        "plan_store_size": len(shared_plans),
        "first_plan_ids": first_ids,
        "second_plan_ids": second_ids,
        "same_plan_chosen": same_plan,
        "second_is_duplicate": second_duplicate,
        "both_completed": first.status.is_completed and second.status.is_completed,
        "no_duplicate_writes": no_duplicate_writes,
        "duplicate_saves_uncharged": duplicate_saves_uncharged,
        # 只有「两次选中同一方案」时，才能观察到幂等命中本身
        "idempotent": (same_plan and second_duplicate and no_duplicate_writes)
        if same_plan
        else no_duplicate_writes,
    }

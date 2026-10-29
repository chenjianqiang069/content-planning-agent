"""Agent 循环测试（离线、确定性）。

用 ScriptedLLM 精确复现「模型观察到 X 后选择了 Y」，验证 ReAct 循环行为，
不依赖真实模型的不确定性，也不需要网络。这一层对应考核的动态决策要求：
- 成本超限后重规划降级
- 工具失败后调整动作并保留已成功结果
- 达到步数/调用上限返回明确未完成状态
- 终止状态按真实执行结果判定（不把失败记为成功、不采信模型的完成声明）
- 缺预算/截止时间在调用模型前拒绝
- 重复执行幂等
"""

from __future__ import annotations

import pytest

from src.agent import ContentPlanningAgent
from src.domain import RunStatus, StepStatus, TaskInput
from src.tools import FailureInjector

PREMIUM_PAIR = ["m_short_video_pro", "m_kol_top"]  # 12 > 10
ECONOMY_PAIR = ["m_ugc_lite", "m_kol_mid"]  # 8 <= 10


def _agent(store, injector=None, max_steps=None, max_tool_calls=None, today=None, saved_plans=None):
    return ContentPlanningAgent(
        store=store,
        injector=injector or FailureInjector(),
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        today=today,
        saved_plans=saved_plans,
    )


def _task(budget=10.0, deadline="2026-12-15", theme="夏日气泡水"):
    return TaskInput(theme=theme, budget=budget, deadline=deadline)


# --------------------------------------------------------------------------
# ReAct：超预算 -> 重规划降级 -> 保存
# --------------------------------------------------------------------------
def test_replans_from_over_budget_to_cheaper_combo(store, today, scripted_llm):
    """模型先估算 premium（超预算），观察到超限后改选 economy，再脚本+保存。"""
    llm = scripted_llm([
        ("search_materials", {}, "先检索素材"),
        ("estimate_cost", {"material_ids": PREMIUM_PAIR}, "先估算首选高质量组合"),
        # 观察到超预算 -> 重规划
        ("estimate_cost", {"material_ids": ECONOMY_PAIR}, "premium 超预算 2，降级到 economy"),
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "economy 可行，生成脚本满足依赖"),
        ("save_plan", {"material_ids": ECONOMY_PAIR}, "依赖满足，保存"),
        "END: 方案已保存",
    ])
    agent = _agent(store, today=today)
    result = agent.run(_task(budget=10.0), llm=llm)

    assert result.status == RunStatus.COMPLETED
    assert result.saved_plan is not None
    assert result.saved_plan.total_cost == 8.0
    assert result.saved_plan.plan_id  # 真正保存
    # 至少尝试了两个不同素材组合（重规划）
    tried = {tuple(sorted(s.tool_input.get("material_ids", []))) for s in result.steps if s.tool_input.get("material_ids")}
    assert tuple(sorted(PREMIUM_PAIR)) in tried
    assert tuple(sorted(ECONOMY_PAIR)) in tried


def test_replan_observed_over_budget_payload(store, today, scripted_llm):
    """模型在 premium 估算后收到的工具结果里确实带 over_budget_gap，才据此降级。"""
    llm = scripted_llm([
        ("estimate_cost", {"material_ids": PREMIUM_PAIR}, "估算首选"),
        "END: 停",
    ])
    agent = _agent(store, today=today)
    agent.run(_task(budget=10.0), llm=llm)
    payload = llm.last_payload()
    assert payload["feasible"] is False
    assert payload["over_budget_gap"] == 2.0
    assert "hint" in payload  # 结果里带了重规划提示


# --------------------------------------------------------------------------
# 工具失败 -> 调整 + 保留已成功结果
# --------------------------------------------------------------------------
def test_recovers_from_injected_script_failure_and_keeps_prior_success(store, today, scripted_llm):
    """第 1 次 generate_script 被注入失败；模型重试后成功，且检索结果被保留。"""
    inj = FailureInjector(fail_on_calls={"generate_script": [1]})
    llm = scripted_llm([
        ("search_materials", {}, "检索"),
        ("estimate_cost", {"material_ids": ECONOMY_PAIR}, "估算 economy"),
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "生成脚本"),  # 第1次 -> 失败
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "失败是临时故障，重试"),  # 第2次 -> 成功
        ("save_plan", {"material_ids": ECONOMY_PAIR}, "保存"),
        "END: 完成",
    ])
    agent = _agent(store, injector=inj, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)

    assert result.status == RunStatus.COMPLETED
    # 恰好一步失败（被注入的脚本），且它没有被记为成功
    failed = [s for s in result.steps if s.status == StepStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].tool_name == "generate_script"
    # 失败步骤不计费
    assert failed[0].charge == 0.0
    # 成功的检索步骤被保留
    assert any(s.tool_name == "search_materials" and s.status == StepStatus.SUCCESS for s in result.steps)


def test_injected_failure_is_not_charged(store, today, scripted_llm):
    """被注入失败的调用不产生费用。"""
    inj = FailureInjector(fail_on_calls={"generate_script": [1]})
    llm = scripted_llm([
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "第1次失败"),
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "第2次成功"),
        "END: 停",
    ])
    agent = _agent(store, injector=inj, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    # 只有成功那次计费 1.0
    assert result.total_spent == 1.0


def test_injected_script_failure_blocks_save(store, today, scripted_llm):
    """脚本被注入失败后，若模型直接保存，会因依赖未满足被拒（失败不算成功）。"""
    inj = FailureInjector(fail_on_calls={"generate_script": [1]})
    llm = scripted_llm([
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "生成脚本"),  # 失败
        ("save_plan", {"material_ids": ECONOMY_PAIR}, "尝试直接保存"),  # 被拒
        "END: 停",
    ])
    agent = _agent(store, injector=inj, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    save_step = next(s for s in result.steps if s.tool_name == "save_plan")
    assert save_step.status == StepStatus.FAILED
    assert save_step.detail.get("rejected_by") == "missing_script_dependency"
    assert result.status != RunStatus.COMPLETED


# --------------------------------------------------------------------------
# 步数 / 调用上限
# --------------------------------------------------------------------------
def test_step_limit_returns_incomplete_and_preserves_success(store, today, scripted_llm):
    """达到上限时返回明确未完成状态，保留已成功步骤，不计为完成。"""
    llm = scripted_llm([
        ("search_materials", {}, "检索"),
        ("estimate_cost", {"material_ids": ECONOMY_PAIR}, "估算"),
        ("generate_script", {"material_ids": ECONOMY_PAIR}, "脚本"),
        ("save_plan", {"material_ids": ECONOMY_PAIR}, "保存"),
        "END: 完成",
    ])
    agent = _agent(store, max_steps=2, max_tool_calls=2, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    assert result.status == RunStatus.INCOMPLETE_STEP_LIMIT
    assert len(result.steps) == 2
    assert result.saved_plan is None  # 没保存就不算完成
    # 已成功的步骤被保留
    assert all(s.status == StepStatus.SUCCESS for s in result.steps)


def test_tool_call_limit_stops_loop_after_budget_exhausted(store, today, scripted_llm):
    """工具调用预算耗尽后，循环在下一轮开头判定上限并停止，不再执行工具。"""
    llm = scripted_llm([
        ("search_materials", {}, "检索"),  # 第 1 次调用，用完 max_tool_calls=1
        ("estimate_cost", {"material_ids": ECONOMY_PAIR}, "不该被执行"),
        "END: 完成",
    ])
    agent = _agent(store, max_tool_calls=1, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    # 只执行了 1 次工具调用（检索），随后达到上限
    assert result.tool_call_count == 1
    assert result.status == RunStatus.INCOMPLETE_STEP_LIMIT
    assert result.saved_plan is None


def test_parallel_calls_over_budget_rejected_mid_turn(store, today):
    """一轮里模型并行请求多个工具，超出调用预算的那些以错误回传、不执行。

    用 ParallelLLM 在一轮内同时请求 3 个工具，但 max_tool_calls=2，
    验证第 3 个被拒（is_error）且不落地为成功步骤。
    """
    from src.domain import ToolCall
    from src.llm import LLMTurn

    class ParallelLLM:
        name = "scripted:parallel"

        def __init__(self):
            self._usage = {"input_tokens": 0, "output_tokens": 0}
            self.request_count = 0

        def start(self, system, task_prompt):
            self.request_count += 1
            calls = [
                ToolCall(id="p1", name="search_materials", input={}),
                ToolCall(id="p2", name="estimate_cost", input={"material_ids": ECONOMY_PAIR}),
                ToolCall(id="p3", name="save_plan", input={"material_ids": ECONOMY_PAIR}),
            ]
            return LLMTurn(text="[决策依据] 并行试探", tool_calls=calls, stop_reason="tool_use", usage=self._usage)

        def observe(self, tool_results):
            self.request_count += 1
            return LLMTurn(text="END", stop_reason="end_turn", usage=self._usage)

        @property
        def usage(self):
            return dict(self._usage)

    agent = _agent(store, max_tool_calls=2, today=today)
    result = agent.run(_task(budget=20.0), llm=ParallelLLM())
    executed = [s for s in result.steps]
    # 前 2 个执行，第 3 个（save_plan）因超预算未执行
    assert len(executed) == 2
    assert {s.tool_name for s in executed} == {"search_materials", "estimate_cost"}
    assert result.status != RunStatus.COMPLETED


# --------------------------------------------------------------------------
# 终止状态判定：不采信模型的完成声明
# --------------------------------------------------------------------------
def test_model_claiming_success_without_save_is_not_completed(store, today, scripted_llm):
    """模型嘴上说完成，但没成功保存 -> 不能记为 completed。"""
    llm = scripted_llm([
        ("estimate_cost", {"material_ids": PREMIUM_PAIR}, "估算"),
        "END: 方案已完成并保存（这是模型的错误声明）",
    ])
    agent = _agent(store, today=today)
    result = agent.run(_task(budget=10.0), llm=llm)
    assert result.status != RunStatus.COMPLETED
    assert result.saved_plan is None
    # 因为估算判定过超预算不可行，应归类为无可行方案
    assert result.status == RunStatus.INCOMPLETE_NO_FEASIBLE_PLAN


def test_all_tools_failed_classifies_as_tool_failure(store, today, scripted_llm):
    """所有工具调用都失败（注入永久失败）-> incomplete_tool_failure。"""
    inj = FailureInjector(fail_forever={"search_materials": "检索服务宕机"})
    llm = scripted_llm([
        ("search_materials", {}, "检索"),
        "END: 停",
    ])
    agent = _agent(store, injector=inj, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    assert result.status == RunStatus.INCOMPLETE_TOOL_FAILURE
    assert all(s.status == StepStatus.FAILED for s in result.steps)


def test_partial_success_plus_hard_failure_classifies_as_tool_failure(
    store, today, scripted_llm
):
    """检索成功 + 估算永久失败 + 模型主动放弃 -> incomplete_tool_failure。

    这是真实模型在 step_limit 场景里实际出现的路径：它看到 estimate_cost
    永久失败后判断无法推进，主动结束循环（而不是继续撞上限）。
    旧实现要求「所有步骤都失败」，这种情况会落到笼统的 incomplete_not_saved；
    正确分类应是 incomplete_tool_failure，因为挡住去路的是工具故障。
    """
    inj = FailureInjector(fail_forever={"estimate_cost": "估算服务超时"})
    llm = scripted_llm([
        ("search_materials", {}, "先检索"),          # 成功
        ("estimate_cost", {"material_ids": ECONOMY_PAIR}, "估算"),  # 失败
        "END: 估算工具持续不可用，无法给出可信的成本结论，停止规划。",
    ])
    agent = _agent(store, injector=inj, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)

    assert result.status == RunStatus.INCOMPLETE_TOOL_FAILURE
    # 成功步骤被保留，且在原因里如实报告
    assert any(s.status == StepStatus.SUCCESS for s in result.steps)
    assert "成功 1 步" in result.reason
    assert result.saved_plan is None


def test_guard_rejection_is_not_misclassified_as_tool_failure(store, today, scripted_llm):
    """守卫拒绝（工具正常工作）不算「工具故障」，应归类为无可行方案。

    save_plan 因超预算被拒时 detail 里有 rejected_by，说明工具按设计工作了；
    这与注入故障/内部异常必须区分开，否则任何超预算场景都会被误报为工具失败。
    """
    llm = scripted_llm([
        ("estimate_cost", {"material_ids": PREMIUM_PAIR}, "估算"),  # feasible=false
        ("generate_script", {"material_ids": PREMIUM_PAIR}, "生成脚本"),
        ("save_plan", {"material_ids": PREMIUM_PAIR}, "尝试保存"),  # 被预算守卫拒绝
        "END: 停",
    ])
    agent = _agent(store, today=today)
    result = agent.run(_task(budget=10.0), llm=llm)

    assert result.status == RunStatus.INCOMPLETE_NO_FEASIBLE_PLAN
    save = next(s for s in result.steps if s.tool_name == "save_plan")
    assert save.status == StepStatus.FAILED
    assert save.detail.get("rejected_by") == "over_budget"  # 守卫拒绝，非工具故障
    assert "缺口" in result.reason


# --------------------------------------------------------------------------
# 缺输入：在调用模型前拒绝
# --------------------------------------------------------------------------
def test_missing_budget_rejected_before_model(store, today, scripted_llm):
    llm = scripted_llm([("search_materials", {}, "不该被调用")])
    agent = _agent(store, today=today)
    result = agent.run(TaskInput(theme="x", budget=None, deadline="2026-12-15"), llm=llm)
    assert result.status == RunStatus.INCOMPLETE_MISSING_INPUT
    assert result.step_count == 0
    assert llm.request_count == 0  # 根本没调用模型


def test_missing_deadline_rejected_before_model(store, today, scripted_llm):
    llm = scripted_llm([("search_materials", {}, "不该被调用")])
    agent = _agent(store, today=today)
    result = agent.run(TaskInput(theme="x", budget=10.0, deadline=None), llm=llm)
    assert result.status == RunStatus.INCOMPLETE_MISSING_INPUT
    assert result.step_count == 0


# --------------------------------------------------------------------------
# 重复执行幂等（共享方案库）
# --------------------------------------------------------------------------
def test_repeat_run_is_idempotent(store, today, scripted_llm):
    """两次运行共享 saved_plans；第二次保存幂等命中，方案库仍只有 1 条。"""
    shared: dict = {}

    def one_run():
        llm = scripted_llm([
            ("estimate_cost", {"material_ids": ECONOMY_PAIR}, "估算"),
            ("generate_script", {"material_ids": ECONOMY_PAIR}, "脚本"),
            ("save_plan", {"material_ids": ECONOMY_PAIR}, "保存"),
            "END: 完成",
        ])
        agent = _agent(store, today=today, saved_plans=shared)
        return agent.run(_task(budget=20.0), llm=llm)

    r1 = one_run()
    r2 = one_run()
    assert r1.status == RunStatus.COMPLETED
    assert r2.status == RunStatus.COMPLETED
    assert len(shared) == 1  # 方案库只有 1 条
    assert r1.saved_plan.plan_id == r2.saved_plan.plan_id
    # 第二次运行的 save_plan 是幂等命中
    dup = [s for s in r2.steps if s.tool_name == "save_plan"][-1]
    assert dup.detail.get("duplicate") is True


def test_refusal_stop_reason_is_reported_distinctly(store, today):
    """模型因安全分类拒绝（stop_reason=refusal）时，如实报告，不当作正常结束。"""
    from src.domain import ToolCall
    from src.llm import LLMTurn

    class RefusingLLM:
        name = "scripted:refusal"

        def __init__(self):
            self._usage = {"input_tokens": 0, "output_tokens": 0}

        def start(self, system, task_prompt):
            return LLMTurn(text="", stop_reason="refusal", usage=self._usage)

        def observe(self, tool_results):
            return LLMTurn(text="", stop_reason="refusal", usage=self._usage)

        @property
        def usage(self):
            return dict(self._usage)

    agent = _agent(store, today=today)
    result = agent.run(_task(budget=15.0), llm=RefusingLLM())
    assert result.status == RunStatus.INCOMPLETE_MODEL_ERROR
    assert "refusal" in result.reason
    assert result.saved_plan is None


def test_max_tokens_truncation_is_reported(store, today):
    """输出被 max_tokens 截断时如实说明，而不是笼统地报告「模型结束了循环」。"""
    from src.llm import LLMTurn

    class TruncatingLLM:
        name = "scripted:truncated"

        def __init__(self):
            self._usage = {"input_tokens": 0, "output_tokens": 0}

        def start(self, system, task_prompt):
            return LLMTurn(
                text="方案已", stop_reason="max_tokens", usage=self._usage
            )

        def observe(self, tool_results):
            return LLMTurn(text="", stop_reason="max_tokens", usage=self._usage)

        @property
        def usage(self):
            return dict(self._usage)

    agent = _agent(store, today=today)
    result = agent.run(_task(budget=15.0), llm=TruncatingLLM())
    assert result.status == RunStatus.INCOMPLETE_NOT_SAVED
    assert "max_tokens" in result.reason


def test_llm_mode_is_labelled_in_result(store, today, scripted_llm):
    """每份结果都标注决策来源，避免 Mock 记录被误认为真实模型决策。"""
    llm = scripted_llm(["END: 停"])
    agent = _agent(store, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    assert result.llm_mode == "scripted"

    from src.llm import MockLLM

    mock = MockLLM()
    agent2 = _agent(store, today=today)
    result2 = agent2.run(_task(budget=20.0), llm=mock)
    assert result2.llm_mode == "mock"


def test_missing_input_does_not_consume_tokens(store, today, scripted_llm):
    """缺输入时在调用模型前拒绝：零工具调用、零 token。"""
    llm = scripted_llm([("search_materials", {}, "不该被调用")])
    agent = _agent(store, today=today)
    result = agent.run(TaskInput(theme="x", budget=None, deadline=None), llm=llm)
    assert result.status == RunStatus.INCOMPLETE_MISSING_INPUT
    assert result.llm_mode == "rejected_before_model"
    assert result.usage == {}
    assert "预算" in result.reason and "截止时间" in result.reason


# --------------------------------------------------------------------------
# 决策依据进入 Trace
# --------------------------------------------------------------------------
def test_decision_basis_recorded_in_steps(store, today, scripted_llm):
    llm = scripted_llm([
        ("search_materials", {}, "先看有哪些素材可选"),
        "END: 停",
    ])
    agent = _agent(store, today=today)
    result = agent.run(_task(budget=15.0), llm=llm)
    assert result.steps[0].decision_basis == "先看有哪些素材可选"

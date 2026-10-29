"""内容任务规划 Agent 的核心循环（ReAct）。

循环形态：模型选择动作 → 执行工具 → 观察结果 → 模型调整下一步或结束。
本项目刻意采用「手写循环 + 预设工具集」作为执行骨架，而把决策权完全交给模型：
调用哪个工具、用什么参数、超预算后是否降级、工具失败后是否换方案、何时停止，
都由模型每一轮的输出决定，代码只做守卫、计费、Trace 与上限控制。

关键保证（对应审核要点）：
- 预算计算只有一处口径（ToolContext.breakdown），估算与保存守卫共用
- 步骤依赖真实校验：保存前必须有「同一组素材」的成功脚本步骤
- 失败步骤绝不会被记为成功（StepStatus 由 ToolOutcome.ok 决定）
- 达到步数/工具调用上限时返回明确的未完成状态，并保留已成功步骤
- 重复执行幂等：相同方案二次保存不重复计费
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Callable

from .data_store import DataStore
from .domain import (
    PlanRecord,
    RunResult,
    RunStatus,
    StepRecord,
    StepStatus,
    TaskInput,
    ToolCall,
    ToolOutcome,
)
from .llm import AnthropicLLM, LLMTurn, MockLLM, build_anthropic_llm
from .tools import TOOL_IMPL, FailureInjector, ToolContext, summarize

# Agent 在工具调用前，用这个前缀在文本里给出简要决策依据（写进 Trace）。
# 只要求模型写一句人类可读的理由，不要求提交内部思维过程。
DECISION_PREFIX = "[决策依据]"


class ContentPlanningAgent:
    """驱动一次内容活动方案规划。"""

    def __init__(
        self,
        store: DataStore | None = None,
        injector: FailureInjector | None = None,
        max_steps: int | None = None,
        max_tool_calls: int | None = None,
        today: date | None = None,
        saved_plans: dict[str, PlanRecord] | None = None,
        on_step: Callable[[StepRecord], None] | None = None,
    ) -> None:
        self.store = store or DataStore()
        self.injector = injector or FailureInjector()
        limits = self.store.step_limits
        self.max_steps = max_steps if max_steps is not None else limits["max_steps"]
        self.max_tool_calls = (
            max_tool_calls if max_tool_calls is not None else limits["max_tool_calls"]
        )
        self.today = today or date.today()
        self.saved_plans = saved_plans if saved_plans is not None else {}
        self.on_step = on_step

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def run(self, task: TaskInput, llm: Any = None) -> RunResult:
        """执行一次完整规划，返回 RunResult。"""
        ctx = ToolContext(
            task=task,
            store=self.store,
            injector=self.injector,
            today=self.today,
            saved_plans=self.saved_plans,
        )

        result = RunResult(
            status=RunStatus.INCOMPLETE_NOT_SAVED,
            task=task,
            budget=task.budget,
            limits={"max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls},
        )
        self._saved_plan_id: str | None = None  # 本次运行真正保存成功的方案 id

        # 1) 必需输入校验：缺预算或截止时间 -> 不启动规划，明确未完成
        missing = task.missing_constraints()
        if missing:
            label = {"budget": "预算", "deadline": "截止时间"}
            names = "、".join(label.get(m, m) for m in missing)
            result.status = RunStatus.INCOMPLETE_MISSING_INPUT
            result.reason = (
                f"缺少必需输入：{names}。缺少这些约束就无法判断方案是否可行，"
                "因此未启动规划（未调用任何工具，未产生费用）。请补全后重新执行。"
            )
            result.model = getattr(llm, "name", "none")
            result.llm_mode = "rejected_before_model"
            return result

        # 2) 构造模型客户端
        llm = llm or self._default_llm()
        if isinstance(llm, MockLLM) and llm.context is None:
            llm.context = ctx
        result.model = getattr(llm, "name", "unknown")
        result.llm_mode = self._llm_mode(llm)

        # 3) ReAct 主循环
        try:
            self._loop(ctx, llm, result)
        except Exception as exc:  # 模型或网络异常：返回未完成而不是抛给调用方
            result.status = RunStatus.INCOMPLETE_MODEL_ERROR
            result.reason = f"模型调用失败：{type(exc).__name__}: {exc}"

        result.total_spent = round(ctx.spent, 4)
        result.step_count = len(result.steps)
        # 一步记录一次工具调用，两个计数一致；分开保留是为了让「上限」语义可核对
        result.tool_call_count = len(result.steps)
        usage = getattr(llm, "usage", None)
        if usage:
            result.usage = dict(usage)
        return result

    def _default_llm(self) -> Any:
        from .llm import api_available

        if api_available():
            return build_anthropic_llm()
        return MockLLM()

    @staticmethod
    def _llm_mode(llm: Any) -> str:
        """标注决策来源，写进每份 Trace，避免 Mock 记录被误认为真实模型决策。"""
        if isinstance(llm, MockLLM):
            return "mock"
        if isinstance(llm, AnthropicLLM):
            return "real"
        return "scripted"  # 测试用的 ScriptedLLM 等

    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------
    def _system_prompt(self, ctx: ToolContext) -> str:
        return PromptBuilder(self.store).system(ctx)

    def _task_prompt(self, ctx: ToolContext) -> str:
        return PromptBuilder(self.store).task(ctx)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _loop(self, ctx: ToolContext, llm: Any, result: RunResult) -> None:
        system = self._system_prompt(ctx)
        task_prompt = self._task_prompt(ctx)

        turn: LLMTurn = llm.start(system, task_prompt)
        tool_call_budget = self.max_tool_calls

        while True:
            # --- 模型给出最终答复，结束 ---
            if not turn.wants_tools:
                self._finalize(
                    ctx, result, final_text=turn.text, stop_reason=turn.stop_reason
                )
                return

            # --- 上限检查：达到上限时返回明确的未完成状态 ---
            if len(result.steps) >= self.max_steps or tool_call_budget <= 0:
                result.status = RunStatus.INCOMPLETE_STEP_LIMIT
                result.reason = (
                    f"达到执行上限（max_steps={self.max_steps}, "
                    f"max_tool_calls={self.max_tool_calls}），任务未完成。"
                    f"已执行 {len(result.steps)} 步，已花费 {round(ctx.spent, 4)} 模拟单位。"
                    "已成功的步骤结果均已保留；未保存方案，因此不计为完成。"
                )
                self._record_attempts(ctx, result)
                return

            # --- 执行本轮所有工具调用 ---
            tool_results: list[dict[str, Any]] = []
            for call in turn.tool_calls:
                if tool_call_budget <= 0:
                    # 同一轮里超出预算的调用：直接以错误回传，让模型看到上限
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": json.dumps(
                                {"ok": False, "error": "已达到工具调用次数上限"},
                                ensure_ascii=False,
                            ),
                            "is_error": True,
                        }
                    )
                    continue
                tool_call_budget -= 1

                outcome = self._execute(ctx, call)
                step = self._record_step(ctx, result, call, outcome, turn.text)
                if self.on_step:
                    self.on_step(step)

                # 记录本次运行真正保存成功的 plan_id（不依赖解析摘要字符串）
                if call.name == "save_plan" and outcome.ok:
                    pid = (outcome.payload or {}).get("plan_id")
                    if pid and pid in ctx.saved_plans:
                        self._saved_plan_id = pid

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": json.dumps(
                            {
                                "ok": outcome.ok,
                                "partial": outcome.partial,
                                "error": outcome.error,
                                **(outcome.payload or {}),
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        "is_error": not outcome.ok,
                    }
                )

            # --- 观察结果后，模型调整下一步或结束 ---
            turn = llm.observe(tool_results)

    # ------------------------------------------------------------------
    # 工具执行
    # ------------------------------------------------------------------
    def _execute(self, ctx: ToolContext, call: ToolCall) -> ToolOutcome:
        impl = TOOL_IMPL.get(call.name)
        if impl is None:
            return ToolOutcome(
                ok=False,
                payload={"available_tools": sorted(TOOL_IMPL)},
                error=f"未知工具：{call.name}",
            )

        # 失败注入优先于业务逻辑；被注入的失败不计费
        injected = ctx.injector.check(call.name)
        if injected:
            return ToolOutcome(ok=False, payload={}, error=injected)

        try:
            return impl(ctx, call.input or {})
        except Exception as exc:  # 工具内部异常 -> 以失败回传，模型可重规划
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"{call.name} 执行异常：{type(exc).__name__}: {exc}",
            )

    def _record_step(
        self,
        ctx: ToolContext,
        result: RunResult,
        call: ToolCall,
        outcome: ToolOutcome,
        model_text: str,
    ) -> StepRecord:
        status = (
            StepStatus.PARTIAL
            if (outcome.ok and outcome.partial)
            else (StepStatus.SUCCESS if outcome.ok else StepStatus.FAILED)
        )
        step = StepRecord(
            index=len(result.steps) + 1,
            tool_name=call.name,
            tool_input=dict(call.input or {}),
            status=status,
            charge=outcome.charge if outcome.ok else 0.0,  # 失败不计费
            result_summary=summarize(call.name, outcome),
            error=outcome.error,
            decision_basis=extract_decision_basis(model_text),
            duration_hours=float(ctx.store.tool_spec(call.name)["duration_hours"]),
            timestamp=datetime.now().isoformat(timespec="seconds"),
            detail=self._extract_detail(call.name, outcome),
        )
        result.steps.append(step)
        return step

    @staticmethod
    def _extract_detail(tool_name: str, outcome: ToolOutcome) -> dict[str, Any]:
        """从工具返回里提取结构化的可行性证据，供终止状态判定使用。"""
        p = outcome.payload or {}
        detail: dict[str, Any] = {}
        if tool_name == "estimate_cost" and outcome.ok:
            detail["feasible"] = p.get("feasible")
            detail["violations"] = list(p.get("violations", []))
            detail["over_budget_gap"] = p.get("over_budget_gap")
            detail["projected_spend"] = p.get("projected_spend")
            detail["plan_lead_days"] = p.get("plan_lead_days")
            detail["available_days"] = p.get("available_days")
        elif tool_name == "save_plan":
            if p.get("rejected_by"):
                detail["rejected_by"] = p["rejected_by"]
            for key in ("over_budget_gap", "projected_spend", "plan_lead_days", "available_days"):
                if p.get(key) is not None:
                    detail[key] = p[key]
            if p.get("plan_id"):
                detail["plan_id"] = p["plan_id"]
                detail["duplicate"] = p.get("duplicate", False)
        return detail

    # ------------------------------------------------------------------
    # 收尾：判定终止状态
    # ------------------------------------------------------------------
    def _finalize(
        self,
        ctx: ToolContext,
        result: RunResult,
        final_text: str,
        stop_reason: str = "",
    ) -> None:
        """模型选择结束时，按真实执行结果判定状态。

        绝不因为「模型说完成了」就记为 completed —— 必须真的存在成功保存的方案。
        """
        saved = self._find_saved_plan(ctx, result)
        self._record_attempts(ctx, result)

        # 模型因安全分类拒绝作答，与「正常结束但没保存」是两回事，分开报告
        if saved is None and stop_reason == "refusal":
            result.status = RunStatus.INCOMPLETE_MODEL_ERROR
            result.reason = (
                "模型拒绝继续（stop_reason=refusal），任务未完成。"
                f"已执行 {len(result.steps)} 步，已花费 {round(ctx.spent, 4)} 模拟单位；"
                "已成功步骤的结果均已保留，但未保存方案。"
            )
            return

        # 输出被 max_tokens 截断：模型的最终答复不完整，如实说明
        truncated = saved is None and stop_reason == "max_tokens"

        if saved is not None:
            result.saved_plan = saved
            result.status = RunStatus.COMPLETED
            result.reason = (
                f"方案已保存：{saved.plan_id}，总成本 {saved.total_cost} / 预算 {saved.budget}，"
                f"交付周期 {saved.lead_days} 天。共执行 {len(result.steps)} 步，"
                f"成功 {sum(1 for s in result.steps if s.status is StepStatus.SUCCESS)} 步，"
                f"失败 {sum(1 for s in result.steps if s.status is StepStatus.FAILED)} 步。"
            )
            return

        # 未保存：按结构化证据区分原因（不靠解析错误文本）
        failed_steps = [s for s in result.steps if s.status is StepStatus.FAILED]
        ok_steps = [
            s
            for s in result.steps
            if s.status in (StepStatus.SUCCESS, StepStatus.PARTIAL)
        ]
        # 「工具自身故障」型失败：没有 rejected_by（守卫拒绝说明工具正常工作）。
        # 注入故障、内部异常、未知工具等都归这一类。
        hard_failures = [s for s in failed_steps if not s.detail.get("rejected_by")]

        # 证据 1：估算或保存守卫判定方案本身不可行（超预算 / 超期 / 缺约束）。
        # 注意：missing_script_dependency、unknown_material 属于「模型步骤顺序或参数用错」，
        # 不代表方案不可行，因此不计入这里 —— 那种情况归为 incomplete_not_saved 更准确。
        infeasible = [
            s
            for s in result.steps
            if s.detail.get("feasible") is False
            or s.detail.get("rejected_by") in ("over_budget", "over_deadline", "missing_budget")
        ]
        budget_gaps = [
            (s.detail.get("over_budget_gap"), s)
            for s in infeasible
            if s.detail.get("over_budget_gap")
        ]
        # 用结构化字段判定超期，避免「缺少截止时间」这类文案被误判为超期
        deadline_violations = [
            s
            for s in infeasible
            if s.detail.get("rejected_by") == "over_deadline"
            or (
                isinstance(s.detail.get("plan_lead_days"), int)
                and isinstance(s.detail.get("available_days"), int)
                and s.detail["plan_lead_days"] > s.detail["available_days"]
            )
        ]

        if infeasible:
            result.status = RunStatus.INCOMPLETE_NO_FEASIBLE_PLAN
            gaps: list[str] = []
            if budget_gaps:
                # 取缺口最小的方案，即「最接近可行」的那个
                gap, step = min(budget_gaps, key=lambda x: float(x[0]))
                gaps.append(
                    f"最接近的方案仍超预算 {gap} 模拟单位"
                    f"（预计 {step.detail.get('projected_spend')} / 预算 {result.budget}）"
                )
            if deadline_violations:
                s = deadline_violations[-1]
                gaps.append(
                    f"方案周期 {s.detail.get('plan_lead_days')} 天 > 可用 "
                    f"{s.detail.get('available_days')} 天"
                )
            gap_text = "；".join(gaps) if gaps else "所有候选组合均不满足预算或截止约束"
            result.reason = (
                f"没有找到可行方案，未保存任何方案。缺口：{gap_text}。"
                f"共尝试 {len(infeasible)} 个不可行组合。"
                f"模型最终答复：{final_text.strip() or '（无文本）'}"
            )
        elif hard_failures:
            # 存在「工具自身故障」型的失败（注入故障、内部异常），而不是守卫拒绝 ——
            # 守卫拒绝（rejected_by）说明工具按设计正常工作了，不算工具失败。
            # 也不要求「所有步骤都失败」：检索成功 + 估算永久失败同样属于这一类。
            result.status = RunStatus.INCOMPLETE_TOOL_FAILURE
            result.reason = (
                f"工具持续失败，无法推进：{len(hard_failures)} 个步骤因工具故障失败"
                f"（共 {len(result.steps)} 步，其中成功 {len(ok_steps)} 步，结果均已保留）。"
                f"首个错误：{hard_failures[0].error}。"
                f"模型最终答复：{final_text.strip() or '（无文本）'}"
            )
        else:
            result.status = RunStatus.INCOMPLETE_NOT_SAVED
            prefix = (
                "模型输出被 max_tokens 截断，循环中断，"
                if truncated
                else "模型结束了循环，"
            )
            result.reason = (
                f"{prefix}但没有成功保存的方案，因此任务未完成。"
                f"模型最终答复：{final_text.strip() or '（无文本）'}"
            )

    def _find_saved_plan(
        self, ctx: ToolContext, result: RunResult
    ) -> PlanRecord | None:
        """返回本次运行真正保存下来的方案。

        只认执行时记录下来的 plan_id（_saved_plan_id，来自 save_plan 成功返回的
        payload），并校验它确实存在于 saved_plans 中；不解析摘要字符串，也不采信
        模型文本里的「已完成」声明，避免守卫已拒绝的方案被记为完成。
        """
        pid = self._saved_plan_id
        if pid and pid in ctx.saved_plans:
            record = ctx.saved_plans[pid]
            # 把方案关联到产生它的步骤，便于审核追溯
            record.steps_included = [s.index for s in result.steps]
            return record
        return None

    def _record_attempts(self, ctx: ToolContext, result: RunResult) -> None:
        """记录尝试过的方案及淘汰原因，供审核核对「是否合理重规划」。

        只收录估算与保存两类步骤 —— 它们代表一次「方案尝试」，
        检索和脚本生成是支撑动作，不单独构成方案。
        """
        attempts: list[dict[str, Any]] = []
        for step in result.steps:
            if step.tool_name not in ("estimate_cost", "save_plan"):
                continue
            entry: dict[str, Any] = {
                "step": step.index,
                "tool": step.tool_name,
                "material_ids": step.tool_input.get("material_ids"),
                "status": step.status.value,
                "summary": step.result_summary,
            }
            if step.error:
                entry["rejection_reason"] = step.error
            attempts.append(entry)
        result.attempts = attempts


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------
def extract_decision_basis(model_text: str) -> str:
    """从模型文本里取出以 [决策依据] 开头的那一句，作为 Trace 里的决策依据。"""
    if not model_text:
        return ""
    for line in model_text.splitlines():
        line = line.strip()
        if line.startswith(DECISION_PREFIX):
            return line[len(DECISION_PREFIX) :].strip()
    return model_text.strip().splitlines()[0][:200] if model_text.strip() else ""


class PromptBuilder:
    """集中生成系统提示与任务提示，真实模型与 mock 共用同一份文本。"""

    def __init__(self, store: DataStore) -> None:
        self.store = store

    def system(self, ctx: ToolContext) -> str:
        rules = self.store.rules
        budget_rule = self.store.budget_rule
        deadline_rule = self.store.deadline_rule
        return f"""你是一名内容活动规划 Agent，帮助运营人员在预算与截止时间内产出一份可执行的内容活动方案。

# 你的工作方式（ReAct）
每一轮你只能做一件事：调用一个工具，或者给出最终答复。
调用工具前，先用一行以 `{DECISION_PREFIX}` 开头的文本说明你为什么选这个动作（一句话即可，不要输出内部推理过程）。
拿到工具结果后，你必须根据结果调整下一步：
- 成本超预算 -> 不要重复尝试同一组合，改用更便宜或交付更快的素材（例如 economy 档），重新估算后再保存
- 超出截止时间 -> 换 lead_days 更小的素材组合
- 工具报错或部分成功 -> 分析错误原因，换参数或换替代方案；不要因为一次失败就放弃整个任务
- 方案可行 -> 先为「同一组素材」生成脚本，再保存
不要凭猜测宣布完成。只有 save_plan 返回成功、拿到 plan_id，任务才算完成。

# 可用工具
{self.store.tool_fee_table_markdown()}

**素材目录不在本提示中**：有哪些素材、各自的 cost 与 lead_days，必须调用 `search_materials`
才能知道。不要凭猜测假设素材的成本或交期；任何成本结论都要以 `estimate_cost` 的返回为准。

# 业务规则（必须遵守，代码会强制校验）
成本口径：{budget_rule.get('formula')}
可行条件：{budget_rule.get('feasible_when')}；工具调用失败不计费。
周期口径：{deadline_rule.get('formula')}
可行条件：{deadline_rule.get('feasible_when')}
保存守卫：{'; '.join(self.store.rules.get('save_guards', []))}
步骤依赖：保存方案前，必须已经为「同一组素材」成功调用过 generate_script；失败或被跳过的步骤不算满足依赖。

# 执行上限
最多 {self.store.step_limits['max_steps']} 步 / {self.store.step_limits['max_tool_calls']} 次工具调用。
达到上限会直接以「未完成」结束，所以请尽快收敛：不要反复估算同一个组合。

# 结束要求
- 成功：给出 plan_id、总成本、剩余预算、交付周期。
- 失败：明确说明未完成原因和缺口（超预算多少 / 超期多少天），并列出哪些步骤已成功、其结果已保留。
- 绝不把失败的步骤描述为成功。
"""

    def task(self, ctx: ToolContext) -> str:
        t = ctx.task
        available = ctx.available_days()
        lines = [
            "请为以下需求完成内容活动方案：",
            f"- 主题：{t.theme}",
            f"- 预算：{t.budget} 模拟单位",
            f"- 截止时间：{t.deadline}（今日 {ctx.today.isoformat()}，可用 {available} 天）",
        ]
        if t.audience:
            lines.append(f"- 目标人群：{t.audience}")
        if t.notes:
            lines.append(f"- 补充说明：{t.notes}")
        lines.append("")
        lines.append(
            "请先检索素材，再估算成本与周期，确认可行后生成脚本并保存方案。"
            "如果首选组合超预算，请主动降级到更便宜的组合。"
        )
        return "\n".join(lines)

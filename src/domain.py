"""领域模型：内容任务规划 Agent 的数据结构。

所有金额单位为「模拟单位」，所有时长单位为「小时」，交付周期单位为「天」。
本模块不含业务规则，规则集中在 rules.json + tools/plan_tools.py 的守卫中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    """运行终止状态。

    completed 是唯一的成功状态；其余全部为「未完成」，并携带 reason。
    """

    COMPLETED = "completed"
    INCOMPLETE_MISSING_INPUT = "incomplete_missing_input"
    INCOMPLETE_NO_FEASIBLE_PLAN = "incomplete_no_feasible_plan"
    INCOMPLETE_STEP_LIMIT = "incomplete_step_limit"
    INCOMPLETE_TOOL_FAILURE = "incomplete_tool_failure"
    INCOMPLETE_NOT_SAVED = "incomplete_not_saved"
    INCOMPLETE_MODEL_ERROR = "incomplete_model_error"

    @property
    def is_completed(self) -> bool:
        return self is RunStatus.COMPLETED


class StepStatus(str, Enum):
    """单个工具调用步骤的状态。失败步骤绝不会被记为成功。"""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


@dataclass
class TaskInput:
    """运营人员给出的任务输入。

    budget / deadline 允许为 None，用于验证「缺预算或截止时间」场景：
    Agent 必须在缺少任一约束时拒绝启动规划并返回明确的未完成状态。
    """

    theme: str
    budget: float | None = None
    deadline: str | None = None  # ISO 日期，如 "2026-11-20"
    audience: str | None = None
    notes: str | None = None

    def missing_constraints(self) -> list[str]:
        missing: list[str] = []
        if self.budget is None:
            missing.append("budget")
        if self.deadline is None or not str(self.deadline).strip():
            missing.append("deadline")
        return missing

    def available_days(self, today: date | None = None) -> int | None:
        """从今日到截止日期的可用天数（含当日）。解析失败返回 None。"""
        if not self.deadline:
            return None
        try:
            end = datetime.strptime(str(self.deadline).strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
        today = today or date.today()
        return max((end - today).days, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "budget": self.budget,
            "deadline": self.deadline,
            "audience": self.audience,
            "notes": self.notes,
        }


@dataclass
class Material:
    """合成素材。"""

    id: str
    name: str
    tier: str
    cost: float
    lead_days: int
    quality: int
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tier": self.tier,
            "cost": self.cost,
            "lead_days": self.lead_days,
            "quality": self.quality,
            "tags": self.tags,
        }


@dataclass
class ToolCall:
    """模型请求的一次工具调用（来自 tool_use block 或 mock 策略）。"""

    id: str
    name: str
    input: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "input": self.input}


@dataclass
class ToolOutcome:
    """工具执行结果。

    ok=False 表示工具失败（异常或业务拒绝），会以 is_error 回传给模型，
    并计入 failed 步骤。charge 仅在 ok=True 且 billable 时累加到已花费。
    """

    ok: bool
    payload: dict[str, Any]
    charge: float = 0.0
    error: str | None = None
    partial: bool = False

    @property
    def status(self) -> StepStatus:
        if self.ok and self.partial:
            return StepStatus.PARTIAL
        return StepStatus.SUCCESS if self.ok else StepStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "partial": self.partial,
            "charge": self.charge,
            "error": self.error,
            "payload": self.payload,
        }


@dataclass
class StepRecord:
    """一步执行记录：模型选择动作 → 执行工具 → 观察结果。"""

    index: int
    tool_name: str
    tool_input: dict[str, Any]
    status: StepStatus
    charge: float = 0.0
    result_summary: str = ""
    error: str | None = None
    decision_basis: str = ""  # 模型在调用前给出的简要决策依据
    duration_hours: float = 0.0
    timestamp: str = ""
    # 结构化的可行性证据（来自 estimate_cost / save_plan 的守卫），
    # 用于终止状态判定，避免靠解析错误文本
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.index,
            "tool": self.tool_name,
            "input": self.tool_input,
            "status": self.status.value,
            "charge": self.charge,
            "result_summary": self.result_summary,
            "error": self.error,
            "decision_basis": self.decision_basis,
            "duration_hours": self.duration_hours,
            "timestamp": self.timestamp,
            "detail": self.detail,
        }


@dataclass
class EstimateBreakdown:
    """成本估算明细，供审核核对预算计算。"""

    material_ids: list[str]
    materials_cost: float
    tool_fees: float
    already_spent: float
    projected_spend: float
    budget: float | None
    within_budget: bool
    gap: float  # 超预算缺口，>0 表示不可行
    lead_days: int
    available_days: int | None
    within_deadline: bool
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_ids": self.material_ids,
            "materials_cost": self.materials_cost,
            "billable_tool_fees": self.tool_fees,
            "already_spent": self.already_spent,
            "projected_spend": self.projected_spend,
            "budget": self.budget,
            "within_budget": self.within_budget,
            "over_budget_gap": self.gap,
            "plan_lead_days": self.lead_days,
            "available_days": self.available_days,
            "within_deadline": self.within_deadline,
            "violations": self.violations,
            "feasible": self.within_budget and self.within_deadline,
        }


@dataclass
class PlanRecord:
    """已保存的方案。"""

    plan_id: str
    theme: str
    material_ids: list[str]
    script_summary: str
    total_cost: float
    lead_days: int
    budget: float
    saved_at: str
    duplicate: bool = False
    steps_included: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "theme": self.theme,
            "material_ids": self.material_ids,
            "script_summary": self.script_summary,
            "total_cost": self.total_cost,
            "lead_days": self.lead_days,
            "budget": self.budget,
            "remaining_budget": round(self.budget - self.total_cost, 4),
            "saved_at": self.saved_at,
            "duplicate_save": self.duplicate,
            "steps_included": self.steps_included,
        }


@dataclass
class RunResult:
    """一次 Agent 运行的完整结果。"""

    status: RunStatus
    reason: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    saved_plan: PlanRecord | None = None
    total_spent: float = 0.0
    budget: float | None = None
    task: TaskInput | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)  # 尝试过的方案及淘汰原因
    model: str = ""
    llm_mode: str = ""
    step_count: int = 0
    tool_call_count: int = 0
    limits: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "is_completed": self.status.is_completed,
            "reason": self.reason,
            "task": self.task.to_dict() if self.task else None,
            "budget": self.budget,
            "total_spent": self.total_spent,
            "step_count": self.step_count,
            "tool_call_count": self.tool_call_count,
            "limits": self.limits,
            "model": self.model,
            "llm_mode": self.llm_mode,
            "usage": self.usage,
            "steps": [s.to_dict() for s in self.steps],
            "attempts": self.attempts,
            "saved_plan": self.saved_plan.to_dict() if self.saved_plan else None,
        }

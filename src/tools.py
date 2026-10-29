"""业务工具集：素材检索、脚本生成、成本估算、方案保存。

工具是纯函数式的本地 Mock 业务实现（无外部依赖），全部守卫集中在这里：
- 预算守卫：projected_spend = 已花费 + 素材成本 + 仍需支付的脚本费，超过预算则拒绝保存
- 截止守卫：plan_lead_days = max(素材 lead_days) + 脚本天数，超过可用天数则拒绝保存
- 依赖守卫：保存前必须存在「同一组素材」的成功 generate_script 步骤；失败步骤不算依赖满足
- 幂等守卫：相同 plan_id 重复保存返回已有记录，不重复计费

失败注入（FailureInjector）用于验收「工具失败」与「重复执行」场景，
被注入失败的调用不计费，并以 is_error 形式回传给模型。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from .data_store import DataStore
from .domain import (
    EstimateBreakdown,
    Material,
    PlanRecord,
    StepStatus,
    TaskInput,
    ToolOutcome,
)


# --------------------------------------------------------------------------
# 失败注入
# --------------------------------------------------------------------------
@dataclass
class FailureInjector:
    """按「第 N 次调用某工具」注入失败，或让某工具永久失败。

    fail_on_calls: {"generate_script": [1]}  -> 该工具第 1 次调用失败，之后正常
    fail_forever: {"save_plan": "..."}       -> 每次调用都失败（用于无可行路径场景）
    """

    fail_on_calls: dict[str, list[int]] = field(default_factory=dict)
    fail_forever: dict[str, str] = field(default_factory=dict)
    _call_count: dict[str, int] = field(default_factory=dict)

    def check(self, tool_name: str) -> str | None:
        """返回错误信息（应失败）或 None（放行）。每次调用都推进计数器。"""
        self._call_count[tool_name] = self._call_count.get(tool_name, 0) + 1
        n = self._call_count[tool_name]
        if tool_name in self.fail_forever:
            return self.fail_forever[tool_name]
        if n in self.fail_on_calls.get(tool_name, []):
            return f"{tool_name} 第 {n} 次调用失败：外部服务暂时不可用（注入的模拟故障）"
        return None

    def call_count(self, tool_name: str) -> int:
        return self._call_count.get(tool_name, 0)


# --------------------------------------------------------------------------
# 工具执行上下文（有状态：已花费、脚本记录、已保存方案）
# --------------------------------------------------------------------------
class ToolContext:
    """一次运行内的共享状态。重复执行验收依赖 saved_plans 的跨运行持久化能力。"""

    def __init__(
        self,
        task: TaskInput,
        store: DataStore,
        injector: FailureInjector | None = None,
        today: date | None = None,
        saved_plans: dict[str, PlanRecord] | None = None,
    ) -> None:
        self.task = task
        self.store = store
        self.injector = injector or FailureInjector()
        self.today = today or date.today()
        # 跨运行持久（重复执行场景）
        self.saved_plans: dict[str, PlanRecord] = saved_plans if saved_plans is not None else {}
        # 本次运行内状态
        self.spent: float = 0.0
        self.charges: list[tuple[str, float]] = []
        # 脚本记录：material_signature -> {script_id, summary, step_index, fee_paid}
        self.scripts: dict[str, dict[str, Any]] = {}
        self._script_seq = 0
        self._save_seq = 0

    # --- helpers ---------------------------------------------------------
    @staticmethod
    def material_signature(material_ids: list[str]) -> str:
        """素材集合签名：与顺序无关，用于脚本依赖与幂等判定。"""
        return "|".join(sorted(set(material_ids)))

    def plan_id(self, material_ids: list[str], script_fingerprint: str) -> str:
        """方案唯一标识。

        身份 = 主题 + 素材集合 + 脚本风格指纹。三者都是**内容**，与执行顺序无关，
        因此同一方案在任意一次运行里都会得到同一个 id —— 这是跨运行幂等的前提。

        注意：绝不能把 script_id 之类的运行内序号放进指纹。序号取决于模型在本次运行里
        先生成了几个脚本，会让同一个方案在不同运行中得到不同 id，
        幂等守卫随之失效（方案库出现重复记录）。
        """
        raw = f"{self.task.theme}::{self.material_signature(material_ids)}::{script_fingerprint}"
        return "plan_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def available_days(self) -> int | None:
        return self.task.available_days(self.today)

    def script_fee(self) -> float:
        return float(self.store.tool_spec("generate_script")["fee"])

    def breakdown(self, material_ids: list[str]) -> EstimateBreakdown:
        """成本与周期的唯一计算入口，估算工具和保存守卫共用，避免两处口径不一致。"""
        materials, unknown = self.store.get_materials(material_ids)
        materials_cost = round(sum(m.cost for m in materials), 4)
        sig = self.material_signature(material_ids)
        script_done = self.scripts.get(sig)
        # 已为同一组素材成功生成脚本 -> 脚本费已付，无需再付；否则仍需支付
        script_fee_remaining = 0.0 if script_done else self.script_fee()
        lead_days = max((m.lead_days for m in materials), default=0)
        lead_days += self.store.script_days

        available = self.available_days()
        within_budget = True
        within_deadline = True
        violations: list[str] = []
        gap = 0.0

        projected = round(self.spent + materials_cost + script_fee_remaining, 4)
        if self.task.budget is None:
            within_budget = False
            violations.append("缺少预算约束，无法判定成本可行性")
        elif projected > self.task.budget:
            within_budget = False
            gap = round(projected - self.task.budget, 4)
            violations.append(
                f"预算超限：预计支出 {projected} > 预算 {self.task.budget}，缺口 {gap}"
            )

        if available is None:
            within_deadline = False
            violations.append("缺少截止时间，无法判定周期可行性")
        elif lead_days > available:
            within_deadline = False
            violations.append(
                f"超出截止时间：方案周期 {lead_days} 天 > 可用 {available} 天，超出 {lead_days - available} 天"
            )

        if unknown:
            violations.append(f"未知素材 id：{', '.join(unknown)}")

        return EstimateBreakdown(
            material_ids=list(material_ids),
            materials_cost=materials_cost,
            tool_fees=script_fee_remaining,
            already_spent=round(self.spent, 4),
            projected_spend=projected,
            budget=self.task.budget,
            within_budget=within_budget,
            gap=gap,
            lead_days=lead_days,
            available_days=available,
            within_deadline=within_deadline,
            violations=violations,
        )

    def charge(self, tool_name: str, amount: float) -> None:
        if amount > 0:
            self.spent = round(self.spent + amount, 4)
            self.charges.append((tool_name, amount))

    # --- tool implementations -------------------------------------------
    def search_materials(self, args: dict[str, Any]) -> ToolOutcome:
        """素材检索。支持 tags/tier/max_cost 过滤；无匹配时返回全部并标记 partial。"""
        tags = args.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        tier = args.get("tier")
        max_cost = args.get("max_cost")

        if isinstance(max_cost, str):
            try:
                max_cost = float(max_cost)
            except ValueError:
                max_cost = None

        matched = [m for m in self.store.materials if self._match(m, tags, tier, max_cost)]
        degraded = False
        if not matched:
            matched = list(self.store.materials)
            degraded = True

        # 主题相关度排序：命中主题提示的素材排前面，让检索结果随主题变化
        hints: dict[str, int] = {}
        for key, bonus in self.store.theme_hints.items():
            if key != "default" and key in (self.task.theme or ""):
                hints.update(bonus)

        def score(m: Material) -> tuple[int, int]:
            return (-(m.quality + hints.get(m.id, 0)), int(m.cost))

        matched = sorted(matched, key=score)
        rows = [
            {
                **m.to_dict(),
                "theme_bonus": hints.get(m.id, 0),
            }
            for m in matched
        ]

        budget = self.task.budget
        note = (
            "过滤条件无匹配，已返回全部素材（降级结果）"
            if degraded
            else f"命中 {len(rows)} 条素材"
        )
        if budget is not None:
            note += f"；当前预算 {budget} 模拟单位，素材成本需加上脚本费 {self.script_fee()}"
        else:
            note += "；注意：本次输入未提供预算，无法做成本可行性判断"

        payload = {
            "query": {"tags": tags, "tier": tier, "max_cost": max_cost},
            "count": len(rows),
            "materials": rows,
            "degraded": degraded,
            "note": note,
        }
        return ToolOutcome(ok=True, payload=payload, charge=0.0, partial=degraded)

    @staticmethod
    def _match(m: Material, tags: list[str], tier: str | None, max_cost: Any) -> bool:
        if tier and m.tier != tier:
            return False
        if max_cost is not None and m.cost > float(max_cost):
            return False
        if tags:
            blob = " ".join([m.name, *m.tags]).lower()
            if not any(str(t).lower() in blob for t in tags):
                return False
        return True

    def generate_script(self, args: dict[str, Any]) -> ToolOutcome:
        """脚本生成。计费（fee=1）。成功才写入 scripts 记录，失败不留下依赖凭据。"""
        material_ids = list(args.get("material_ids") or [])
        style = str(args.get("style") or "默认")
        if not material_ids:
            return ToolOutcome(
                ok=False,
                payload={},
                error="material_ids 不能为空：脚本必须绑定具体素材组合",
            )

        materials, unknown = self.store.get_materials(material_ids)
        if not materials:
            return ToolOutcome(
                ok=False,
                payload={},
                error=f"未找到任何有效素材：{', '.join(unknown)}",
            )

        available = self.available_days()
        blocked = [
            m.id for m in materials if available is not None and m.lead_days > available
        ]

        sig = self.material_signature(material_ids)
        self._script_seq += 1
        script_id = f"script_{self._script_seq:02d}_{sig.replace('|', '-')}"
        summary = (
            f"《{self.task.theme}》{style}脚本，覆盖 {len(materials)} 组素材："
            + "、".join(m.name for m in materials)
        )
        fee = self.script_fee()

        record = {
            "script_id": script_id,
            # 内容指纹：只由脚本风格决定，不含运行内序号，可安全用于 plan_id。
            # script_id 里的 _script_seq 是「本次运行第几个脚本」，跨运行不稳定，
            # 绝不能进入方案身份（见 plan_id 的 docstring）。
            "fingerprint": hashlib.sha1(f"style={style}".encode("utf-8")).hexdigest()[:8],
            "summary": summary,
            "material_ids": list(material_ids),
            "signature": sig,
            "style": style,
            "fee_paid": fee,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

        if unknown or blocked:
            # 部分成功：脚本已产出，但有素材无法按期交付 / id 无效，需模型决定是否替换
            self.scripts[sig] = record
            self.charge("generate_script", fee)
            problems = []
            if unknown:
                problems.append(f"无效素材 id：{', '.join(unknown)}（已从脚本中剔除）")
            if blocked:
                names = "、".join(
                    f"{mid}(lead={self.store.get_material(mid).lead_days}天>可用{available}天)"
                    for mid in blocked
                )
                problems.append(f"以下素材无法在截止时间前交付：{names}")
            return ToolOutcome(
                ok=True,
                partial=True,
                charge=fee,
                payload={
                    "script_id": script_id,
                    "summary": summary,
                    "material_ids": [m.id for m in materials],
                    "fee_charged": fee,
                    "total_spent": self.spent,
                    "problems": problems,
                    "note": "部分成功：脚本已生成但存在问题素材，请替换素材后重新估算，或确认可接受",
                },
            )

        self.scripts[sig] = record
        self.charge("generate_script", fee)
        return ToolOutcome(
            ok=True,
            charge=fee,
            payload={
                "script_id": script_id,
                "summary": summary,
                "material_ids": [m.id for m in materials],
                "fee_charged": fee,
                "total_spent": self.spent,
                "lead_days": max(m.lead_days for m in materials) + self.store.script_days,
                "note": "脚本生成成功，可进入成本估算与保存",
            },
        )

    def estimate_cost(self, args: dict[str, Any]) -> ToolOutcome:
        """成本估算。只读、不计费，但会把可行性结论明确告知模型。"""
        material_ids = list(args.get("material_ids") or [])
        if not material_ids:
            return ToolOutcome(
                ok=False, payload={}, error="material_ids 不能为空，无法估算成本"
            )
        found, unknown = self.store.get_materials(material_ids)
        if not found:
            return ToolOutcome(
                ok=False, payload={}, error=f"未找到任何有效素材：{', '.join(unknown)}"
            )

        b = self.breakdown(material_ids)
        payload = b.to_dict()
        payload["unknown_material_ids"] = unknown
        if b.within_budget and b.within_deadline:
            payload["verdict"] = (
                f"可行：预计支出 {b.projected_spend} ≤ 预算 {b.budget}，"
                f"周期 {b.lead_days} 天 ≤ 可用 {b.available_days} 天"
            )
        else:
            payload["verdict"] = "不可行：" + "；".join(b.violations)
            payload["hint"] = (
                "请改用成本更低 / 交付更快的素材组合（例如 economy 档），"
                "或调用 search_materials 寻找替代方案"
            )
        return ToolOutcome(ok=True, payload=payload, charge=0.0, partial=bool(unknown))

    def save_plan(self, args: dict[str, Any]) -> ToolOutcome:
        """方案保存。逐条执行守卫，任一不满足即拒绝，并返回明确缺口。"""
        material_ids = list(args.get("material_ids") or [])
        if not material_ids:
            return ToolOutcome(ok=False, payload={}, error="material_ids 不能为空")

        materials, unknown = self.store.get_materials(material_ids)
        if unknown:
            return ToolOutcome(
                ok=False,
                payload={"rejected_by": "unknown_material", "unknown_material_ids": unknown},
                error=f"方案包含未知素材 id：{', '.join(unknown)}",
            )
        if not materials:
            return ToolOutcome(ok=False, payload={}, error="方案素材列表为空")

        sig = self.material_signature(material_ids)
        script = self.scripts.get(sig)

        # 依赖守卫：必须有同一组素材的成功脚本步骤
        if script is None:
            other = sorted(self.scripts.keys())
            return ToolOutcome(
                ok=False,
                payload={
                    "rejected_by": "missing_script_dependency",
                    "material_signature": sig,
                    "existing_scripts": other,
                },
                error=(
                    f"步骤依赖未满足：素材组合 [{sig}] 没有成功的 generate_script 步骤，"
                    f"不能保存方案。已存在的脚本组合：{other or '无'}。"
                    "请先为该组合生成脚本（失败或被跳过的步骤不算依赖满足）。"
                ),
            )

        b = self.breakdown(material_ids)
        # 预算守卫
        if self.task.budget is None:
            return ToolOutcome(
                ok=False,
                payload={"rejected_by": "missing_budget"},
                error="缺少预算约束，拒绝保存方案：无法验证成本可行性",
            )
        if not b.within_budget:
            return ToolOutcome(
                ok=False,
                payload={
                    "rejected_by": "over_budget",
                    "projected_spend": b.projected_spend,
                    "budget": b.budget,
                    "over_budget_gap": b.gap,
                    "breakdown": b.to_dict(),
                },
                error=(
                    f"预算超限，拒绝保存：预计支出 {b.projected_spend} > 预算 {b.budget}，"
                    f"缺口 {b.gap} 模拟单位。请改用更便宜的素材组合后重新估算。"
                ),
            )
        # 截止守卫
        if not b.within_deadline:
            return ToolOutcome(
                ok=False,
                payload={
                    "rejected_by": "over_deadline",
                    "plan_lead_days": b.lead_days,
                    "available_days": b.available_days,
                    "breakdown": b.to_dict(),
                },
                error=(
                    f"超出截止时间，拒绝保存：方案周期 {b.lead_days} 天 > 可用 {b.available_days} 天"
                ),
            )

        # 幂等守卫：相同方案重复保存不重复计费
        plan_id = self.plan_id(material_ids, script["fingerprint"])
        existing = self.saved_plans.get(plan_id)
        self._save_seq += 1
        if existing is not None:
            dup = PlanRecord(
                plan_id=existing.plan_id,
                theme=existing.theme,
                material_ids=list(existing.material_ids),
                script_summary=existing.script_summary,
                total_cost=existing.total_cost,
                lead_days=existing.lead_days,
                budget=existing.budget,
                saved_at=existing.saved_at,
                duplicate=True,
                steps_included=list(existing.steps_included),
            )
            return ToolOutcome(
                ok=True,
                charge=0.0,
                payload={
                    "duplicate": True,
                    "plan_id": plan_id,
                    "saved_plan": dup.to_dict(),
                    "note": (
                        f"幂等命中：方案 {plan_id} 已于 {existing.saved_at} 保存过，"
                        "返回既有记录，不重复计费、不重复写入。"
                    ),
                },
            )

        record = PlanRecord(
            plan_id=plan_id,
            theme=self.task.theme,
            material_ids=[m.id for m in materials],
            script_summary=script["summary"],
            total_cost=b.projected_spend,
            lead_days=b.lead_days,
            budget=b.budget or 0.0,
            saved_at=datetime.now().isoformat(timespec="seconds"),
            duplicate=False,
        )
        self.saved_plans[plan_id] = record
        return ToolOutcome(
            ok=True,
            charge=0.0,
            payload={
                "duplicate": False,
                "plan_id": plan_id,
                "saved_plan": record.to_dict(),
                "budget_check": {
                    "projected_spend": b.projected_spend,
                    "budget": b.budget,
                    "remaining": round((b.budget or 0) - b.projected_spend, 4),
                },
                "note": "方案已保存",
            },
        )


# --------------------------------------------------------------------------
# 工具注册表
# --------------------------------------------------------------------------
TOOL_IMPL: dict[str, Callable[[ToolContext, dict[str, Any]], ToolOutcome]] = {
    "search_materials": ToolContext.search_materials,
    "generate_script": ToolContext.generate_script,
    "estimate_cost": ToolContext.estimate_cost,
    "save_plan": ToolContext.save_plan,
}


def summarize(tool_name: str, outcome: ToolOutcome) -> str:
    """把工具结果压成一行摘要，用于 Trace 与运行记录。"""
    p = outcome.payload or {}
    if not outcome.ok:
        return f"失败：{outcome.error}"
    if tool_name == "search_materials":
        ids = [m["id"] for m in p.get("materials", [])]
        tag = "部分成功" if outcome.partial else "成功"
        return f"{tag}：命中 {p.get('count')} 条 -> {', '.join(ids)}"
    if tool_name == "generate_script":
        tag = "部分成功" if outcome.partial else "成功"
        base = f"{tag}：{p.get('script_id')}，计费 {p.get('fee_charged')}，累计支出 {p.get('total_spent')}"
        if outcome.partial and p.get("problems"):
            base += f"；问题：{'；'.join(p['problems'])}"
        return base
    if tool_name == "estimate_cost":
        return (
            f"成功：预计支出 {p.get('projected_spend')}/预算 {p.get('budget')}，"
            f"周期 {p.get('plan_lead_days')}天/可用 {p.get('available_days')}天 -> "
            f"{'可行' if p.get('feasible') else '不可行'}"
        )
    if tool_name == "save_plan":
        if p.get("duplicate"):
            return f"成功（幂等命中）：{p.get('plan_id')} 已存在，未重复计费"
        sp = p.get("saved_plan", {})
        return (
            f"成功：已保存 {p.get('plan_id')}，总成本 {sp.get('total_cost')}，"
            f"剩余预算 {sp.get('remaining_budget')}"
        )
    return "成功" if outcome.ok else "失败"


def status_of(outcome: ToolOutcome) -> StepStatus:
    return outcome.status

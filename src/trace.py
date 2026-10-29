"""运行记录（Trace）输出。

产出两种格式，供 examples/ 与评审使用：
- JSON：机器可读的完整运行结果，含每一步的工具参数、返回摘要、状态、决策依据
- Markdown：人类可读的执行路径记录，满足「至少两条执行路径的运行记录」要求，
  包含步骤、工具参数、返回摘要、后续动作及简要决策依据（不含模型内部思维过程）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import RunResult, RunStatus, StepRecord, StepStatus

STATUS_LABEL = {
    RunStatus.COMPLETED: "已完成",
    RunStatus.INCOMPLETE_MISSING_INPUT: "未完成 · 缺少必需输入",
    RunStatus.INCOMPLETE_NO_FEASIBLE_PLAN: "未完成 · 无可行方案",
    RunStatus.INCOMPLETE_STEP_LIMIT: "未完成 · 达到步数上限",
    RunStatus.INCOMPLETE_TOOL_FAILURE: "未完成 · 工具持续失败",
    RunStatus.INCOMPLETE_NOT_SAVED: "未完成 · 未保存方案",
    RunStatus.INCOMPLETE_MODEL_ERROR: "未完成 · 模型调用异常",
}

STEP_ICON = {
    StepStatus.SUCCESS: "✅ 成功",
    StepStatus.PARTIAL: "⚠️ 部分成功",
    StepStatus.FAILED: "❌ 失败",
    StepStatus.SKIPPED: "⏭️ 跳过",
    StepStatus.PENDING: "⏳ 待执行",
}


def write_json(result: RunResult, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _fmt_input(tool_input: dict[str, Any]) -> str:
    if not tool_input:
        return "（无参数）"
    return json.dumps(tool_input, ensure_ascii=False)


def to_markdown(result: RunResult, case_name: str = "", notes: str = "") -> str:
    """把一次运行渲染成 Markdown 执行记录。"""
    d = result.to_dict()
    lines: list[str] = []
    title = case_name or f"运行记录：{result.status.value}"
    lines.append(f"# {title}")
    lines.append("")

    # --- 概要 ---
    lines.append("## 概要")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 终止状态 | `{result.status.value}` — {STATUS_LABEL.get(result.status, '')} |")
    lines.append(f"| 是否完成 | {'是' if result.status.is_completed else '否'} |")
    lines.append(f"| 模型 | `{result.model}`（{'真实模型决策' if result.llm_mode == 'real' else '离线 Mock，仅验证流程'}） |")
    lines.append(f"| 步数 / 工具调用 | {result.step_count} / {result.tool_call_count}（上限 {result.limits.get('max_steps')} / {result.limits.get('max_tool_calls')}） |")
    if result.usage:
        lines.append(
            f"| Token 用量 | input={result.usage.get('input_tokens', 0)}, "
            f"output={result.usage.get('output_tokens', 0)} |"
        )
    lines.append("")

    # --- 输入 ---
    task = d.get("task") or {}
    lines.append("## 输入")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(task, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")

    # --- 结论 ---
    lines.append("## 结论")
    lines.append("")
    lines.append(f"**{STATUS_LABEL.get(result.status, result.status.value)}**")
    lines.append("")
    lines.append(result.reason or "（无说明）")
    lines.append("")

    # --- 费用合计 ---
    lines.append("## 费用合计")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 预算 | {d.get('budget')} 模拟单位 |")
    lines.append(f"| 实际已花费 | {d.get('total_spent')} 模拟单位 |")
    if d.get("saved_plan"):
        sp = d["saved_plan"]
        lines.append(f"| 方案总成本 | {sp.get('total_cost')} 模拟单位 |")
        lines.append(f"| 剩余预算 | {sp.get('remaining_budget')} 模拟单位 |")
    lines.append("")
    lines.append("> 失败的工具调用不计费；只有成功的 `generate_script` 会计入方案成本。")
    lines.append("")

    # --- 执行路径 ---
    lines.append("## 执行路径")
    lines.append("")
    if not result.steps:
        lines.append("（未调用任何工具）")
        lines.append("")
    else:
        for i, s in enumerate(result.steps):
            nxt = result.steps[i + 1] if i + 1 < len(result.steps) else None
            lines.append(
                f"### 步骤 {s.index} · `{s.tool_name}` · {STEP_ICON.get(s.status, s.status.value)}"
            )
            lines.append("")
            lines.append(f"- **工具参数**：`{_fmt_input(s.tool_input)}`")
            lines.append(f"- **返回摘要**：{s.result_summary or '（无）'}")
            if s.error:
                lines.append(f"- **错误信息**：{s.error}")
            if s.decision_basis:
                lines.append(f"- **决策依据**：{s.decision_basis}")
            lines.append(f"- **计费**：{s.charge} 模拟单位")
            if nxt:
                lines.append(
                    f"- **后续动作**：调用 `{nxt.tool_name}`"
                    + (f"，参数 `{_fmt_input(nxt.tool_input)}`" if nxt.tool_input else "")
                )
            else:
                lines.append("- **后续动作**：模型给出最终答复并结束循环")
            lines.append("")

    # --- 方案尝试与重规划 ---
    if result.attempts:
        lines.append("## 方案尝试与重规划轨迹")
        lines.append("")
        lines.append("| 步骤 | 工具 | 素材组合 | 状态 | 结论 / 淘汰原因 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for a in result.attempts:
            ids = a.get("material_ids") or []
            reason = a.get("rejection_reason") or a.get("summary") or ""
            reason = str(reason).replace("\n", " ").replace("|", "\\|")
            lines.append(
                f"| {a.get('step')} | `{a.get('tool')}` | {', '.join(ids) or '-'} "
                f"| {a.get('status')} | {reason} |"
            )
        lines.append("")

    # --- 已保存方案 ---
    if d.get("saved_plan"):
        lines.append("## 已保存方案")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(d["saved_plan"], ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    # --- 步骤状态汇总 ---
    lines.append("## 步骤状态汇总")
    lines.append("")
    counts: dict[str, int] = {}
    for s in result.steps:
        counts[s.status.value] = counts.get(s.status.value, 0) + 1
    for k in ("success", "partial", "failed", "skipped"):
        if counts.get(k):
            lines.append(f"- {k}：{counts[k]} 步")
    if not counts:
        lines.append("- （无步骤）")
    lines.append("")

    if notes:
        lines.append("## 备注")
        lines.append("")
        lines.append(notes)
        lines.append("")

    return "\n".join(lines)


def write_markdown(result: RunResult, path: Path | str, case_name: str = "", notes: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(result, case_name=case_name, notes=notes), encoding="utf-8")
    return path


def print_trace(result: RunResult) -> None:
    """CLI 实时输出用：打印精简的执行路径。"""
    print(f"\n{'=' * 72}")
    print(f"终止状态：{result.status.value} — {STATUS_LABEL.get(result.status, '')}")
    print(f"模型：{result.model}（{result.llm_mode}）")
    print(f"步数：{result.step_count} / 上限 {result.limits.get('max_steps')}")
    print(f"已花费：{result.total_spent} / 预算 {result.budget} 模拟单位")
    print(f"{'=' * 72}")
    for s in result.steps:
        icon = STEP_ICON.get(s.status, s.status.value)
        print(f"\n[步骤 {s.index}] {s.tool_name} {icon}")
        print(f"  参数: {_fmt_input(s.tool_input)}")
        print(f"  结果: {s.result_summary}")
        if s.error:
            print(f"  错误: {s.error}")
        if s.decision_basis:
            print(f"  依据: {s.decision_basis}")
    print(f"\n{'-' * 72}")
    print(f"结论：{result.reason}")
    if result.saved_plan:
        print(f"已保存方案：{result.saved_plan.plan_id}")
    print(f"{'-' * 72}\n")

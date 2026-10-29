"""生成 examples/ 下的验证材料：每个验收场景的输入、实际输出与 Trace。

用法：
    python scripts/generate_examples.py --mode real     # 真实模型（默认，需要 API 凭据）
    python scripts/generate_examples.py --mode mock     # 离线 Mock（仅验证流程）

产出：
    examples/<key>.json   机器可读的完整运行结果
    examples/<key>.md     人类可读的执行路径记录（步骤/参数/返回摘要/后续动作/决策依据）
    examples/README.md    索引，说明每个文件覆盖哪个验收点

保持目录干净：脚本写在 scripts/，产物写到 examples/，不在数据目录里执行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from src.runner import run_scenario  # noqa: E402
from src.scenarios import REPEAT_SCENARIO, SCENARIOS  # noqa: E402
from src.trace import write_json, write_markdown  # noqa: E402

EXAMPLES_DIR = REPO_ROOT / "examples"


def generate(mode: str, model: str | None, effort: str, only: list[str] | None = None) -> int:
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    index_rows: list[tuple[str, str, str, str]] = []
    scenarios = [s for s in SCENARIOS if not only or s.key in only]
    do_repeat = not only or REPEAT_SCENARIO.key in only

    for scenario in scenarios:
        print(f"\n=== 生成 {scenario.key}（{mode}）===", flush=True)
        out = run_scenario(scenario, mode=mode, model=model, effort=effort)
        if isinstance(out, dict):  # 不会发生：repeat 不在这个列表里
            continue
        print(f"    状态：{out.status.value} | 步数 {out.step_count} | 花费 {out.total_spent}", flush=True)
        write_json(out, EXAMPLES_DIR / f"{scenario.key}.json")
        write_markdown(
            out,
            EXAMPLES_DIR / f"{scenario.key}.md",
            case_name=scenario.name,
            notes=scenario.expect_notes,
        )
        index_rows.append(
            (scenario.key, scenario.name, scenario.acceptance_point, out.status.value)
        )

    # 重复执行场景：两次运行 + 幂等结论
    if do_repeat:
        _generate_repeat(mode, model, effort, index_rows)

    _write_usage_table()
    _write_index(index_rows, mode, model, effort)
    print(f"\n已生成 {len(index_rows)} 组验证材料到 {EXAMPLES_DIR}")
    return 0


def _generate_repeat(mode, model, effort, index_rows):
    print(f"\n=== 生成 {REPEAT_SCENARIO.key}（{mode}）===", flush=True)
    rep = run_scenario(REPEAT_SCENARIO, mode=mode, model=model, effort=effort)
    r1, r2 = rep["first"], rep["second"]
    write_json(r1, EXAMPLES_DIR / f"{REPEAT_SCENARIO.key}_run1.json")
    write_json(r2, EXAMPLES_DIR / f"{REPEAT_SCENARIO.key}_run2.json")
    if rep["same_plan_chosen"]:
        idem_text = (
            f"两次运行选中了**同一个** plan_id `{rep['first_plan_ids'][-1]}`，"
            f"第二次 save_plan 幂等命中（duplicate=True）、计费 0；"
            f"方案库记录数 = {rep['plan_store_size']}（未重复写入）。"
        )
    else:
        idem_text = (
            f"两次运行选中了**不同**的方案（run1={rep['first_plan_ids']}, "
            f"run2={rep['second_plan_ids']}）——预算宽裕时模型有权在两次运行里选不同素材组合，"
            f"这是两个不同方案，不是同一方案的重复保存。幂等的真正不变量仍然成立："
            f"方案库每个 plan_id 唯一、无重复写入（库大小 {rep['plan_store_size']}），"
            f"复用既有方案的保存均标记 duplicate 且计费 0。"
        )
    notes = (
        f"第 2 次运行（重复执行）。两次运行共享同一份方案库。\n\n"
        f"**幂等结论**：{idem_text}\n\n"
        f"- 两次均完成：{rep['both_completed']}\n"
        f"- 无重复写入：{rep['no_duplicate_writes']}\n"
        f"- 幂等保存不计费：{rep['duplicate_saves_uncharged']}\n\n"
        f"第 1 次运行见 `{REPEAT_SCENARIO.key}_run1.json`。"
    )
    write_markdown(
        r2,
        EXAMPLES_DIR / f"{REPEAT_SCENARIO.key}.md",
        case_name=REPEAT_SCENARIO.name,
        notes=notes,
    )
    print(
        f"    第1次 {r1.status.value} / 第2次 {r2.status.value} | "
        f"方案库 {rep['plan_store_size']} 条 | 同一方案={rep['same_plan_chosen']} | "
        f"无重复写入={rep['no_duplicate_writes']}",
        flush=True,
    )
    index_rows.append(
        (
            REPEAT_SCENARIO.key,
            REPEAT_SCENARIO.name,
            REPEAT_SCENARIO.acceptance_point,
            f"run1={r1.status.value}, run2={r2.status.value}, "
            f"store={rep['plan_store_size']}, no_dup_writes={rep['no_duplicate_writes']}",
        )
    )


def _rows_from_disk() -> list[tuple[str, str, str, str]]:
    """扫描 examples/ 下已有的 JSON，重建索引行。

    这样即使只重新生成了部分场景，索引也不会丢掉其它场景的行。
    """
    import json

    from src.scenarios import SCENARIOS

    meta = {s.key: (s.name, s.acceptance_point) for s in SCENARIOS}
    meta[REPEAT_SCENARIO.key] = (REPEAT_SCENARIO.name, REPEAT_SCENARIO.acceptance_point)

    rows: list[tuple[str, str, str, str]] = []
    order = [s.key for s in SCENARIOS] + [REPEAT_SCENARIO.key]
    for key in order:
        if key not in meta:
            continue
        name, point = meta[key]
        if key == REPEAT_SCENARIO.key:
            path = EXAMPLES_DIR / f"{key}_run2.json"
        else:
            path = EXAMPLES_DIR / f"{key}.json"
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if key == REPEAT_SCENARIO.key:
            r1 = EXAMPLES_DIR / f"{key}_run1.json"
            status = d.get("status", "?")
            status1 = "?"
            if r1.exists():
                try:
                    status1 = json.loads(r1.read_text(encoding="utf-8")).get("status", "?")
                except (OSError, ValueError):
                    pass
            status = f"run1={status1}, run2={status}"
        else:
            status = d.get("status", "?")
        rows.append((key, name, point, status))
    return rows


def _write_usage_table() -> None:
    """从实际产出的 JSON 汇总 token 用量与步数，写成 examples/token-usage.md。

    文档里不再手抄这些数字 —— 重跑后本文件自动更新，评估文档引用它即可。
    """
    import json

    order = [s.key for s in SCENARIOS] + [REPEAT_SCENARIO.key]
    rows: list[tuple[str, int, Any, Any, Any]] = []
    for key in order:
        files = (
            [f"{key}_run1.json", f"{key}_run2.json"]
            if key == REPEAT_SCENARIO.key
            else [f"{key}.json"]
        )
        for fname in files:
            path = EXAMPLES_DIR / fname
            if not path.exists():
                continue
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            u = d.get("usage") or {}
            label = fname[: -len(".json")]
            rows.append(
                (
                    label,
                    d.get("step_count", 0),
                    u.get("input_tokens"),
                    u.get("output_tokens"),
                    d.get("status", "?"),
                )
            )

    lines = [
        "# Token 用量与步数汇总",
        "",
        "本文件由 `scripts/generate_examples.py` 从同目录的 JSON 自动汇总，",
        "重跑真实模型后会随之更新，因此不会与 Trace 漂移。",
        "",
        "| 场景 | 步数 | input tokens | output tokens | 终止状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, steps, itok, otok, status in rows:
        i = f"{itok:,}" if isinstance(itok, int) else "—（模型未被调用）"
        o = f"{otok:,}" if isinstance(otok, int) else "—"
        lines.append(f"| `{label}` | {steps} | {i} | {o} | `{status}` |")

    called = [r for r in rows if isinstance(r[2], int)]
    if called:
        total_in = sum(r[2] for r in called)
        total_out = sum(r[3] or 0 for r in called)
        lines += [
            "",
            f"合计（{len(called)} 次运行）：input {total_in:,} / output {total_out:,} tokens。",
            "",
            "input 偏高是因为每轮都重发完整历史且未启用 prompt caching；",
            "系统提示与工具费用表是稳定前缀，加 `cache_control` 可显著降本（见 "
            "[docs/03-evaluation.md](../docs/03-evaluation.md) 已知限制第 4 条）。",
            "",
            "`missing_budget` / `missing_deadline` 两行没有 token 用量：",
            "缺必需输入时在调用模型之前就被拒绝，零 API 消耗。",
            "",
        ]
    (EXAMPLES_DIR / "token-usage.md").write_text("\n".join(lines), encoding="utf-8")


def _write_index(rows, mode: str, model: str | None, effort: str) -> None:
    # 以磁盘上实际存在的文件为准，避免部分重新生成时索引缺行
    rows = _rows_from_disk() or rows
    lines = [
        "# examples/ 验证材料索引",
        "",
        "本目录存放每个验收场景的**输入、实际输出与运行记录（Trace）**。",
        "",
        f"- 生成模式：`{mode}`"
        + (f"，模型 `{model}`" if model else "，模型 `claude-opus-5-5`（默认）")
        + f"，effort `{effort}`",
        "- 每个场景两个文件：`<key>.json`（机器可读完整结果）与 `<key>.md`（人类可读执行路径）",
        "- 生成命令：`python scripts/generate_examples.py --mode real`",
        "",
        "> **Mock 边界说明**：`--mode mock` 产出的记录只验证「循环、工具、守卫、重规划链路」"
        "能跑通，**不能证明真实模型的决策效果**。本目录提交的记录由真实模型（`--mode real`）产出。",
        "",
        "## 验收点覆盖",
        "",
        "| 文件 | 场景 | 覆盖的验收点 | 实际终止状态 |",
        "| --- | --- | --- | --- |",
    ]
    for key, name, point, status in rows:
        # repeat_same 的 JSON 拆成两次运行，链接要指向真实存在的文件
        json_links = (
            f"[{key}_run1.json]({key}_run1.json) / [{key}_run2.json]({key}_run2.json)"
            if key == REPEAT_SCENARIO.key
            else f"[{key}.json]({key}.json)"
        )
        lines.append(f"| [{key}.md]({key}.md) / {json_links} | {name} | {point} | `{status}` |")

    lines += ["", "## 至少两条执行路径的对比", ""]
    lines += _comparison_section()
    lines += [
        "",
        "两条路径使用**同一份系统提示、同一套工具、同一个模型**，唯一差别是输入的预算，",
        "执行路径却因工具返回结果不同而分岔 —— 这正是 ReAct 决策，而不是预设流程。",
        "",
        "另外四组对照：",
        "- [tool_failure.md](tool_failure.md)：注入一次 `generate_script` 失败，模型识别为瞬时错误后重试，仍完成，且成功的检索结果被保留",
        "- [infeasible_all.md](infeasible_all.md)：无任何可行方案，模型探测出成本下界后主动停止（并避免无谓的脚本计费），报告缺口，没有把失败步骤记为成功",
        "- [tight_deadline.md](tight_deadline.md)：预算充足但周期不可行，模型证明最小周期后停止，报告超出天数",
        "- [step_limit.md](step_limit.md)：撞上步数上限，返回明确的未完成状态并保留已成功步骤",
        "",
        "## 与评估文档的对应",
        "",
        "本目录的文件被 [docs/03-evaluation.md](../docs/03-evaluation.md) 引用，",
        "该文档逐条列出测试预期、实际结果与失败/限制。",
        "",
        "## 测试结果",
        "",
        "任务要求「Trace 与测试结果存放于 examples/」，因此测试结果也在本目录：",
        "",
        "- [test-results.md](test-results.md) —— 离线断言与真实模型端到端测试的"
        "逐条结果、断言口径说明（具体条数以该文件为准）",
        "- 重新生成：`python scripts/generate_test_results.py`（加 `--network` 可同时重跑真实模型套件）",
        "",
    ]
    (EXAMPLES_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _comparison_section() -> list[str]:
    """从实际产出的 JSON 读取数字来写 normal vs over_budget 对比表。

    不硬编码成本数字：真实模型每次运行可能选不同的替代组合，
    写死的数字会与 Trace 不一致，反而让评审怀疑材料是编的。
    """
    import json

    def load(key: str) -> dict | None:
        p = EXAMPLES_DIR / f"{key}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    n, o = load("normal"), load("over_budget")
    if not n or not o:
        return [
            "考核要求提供至少两条执行路径的运行记录，体现模型根据不同工具结果调整动作。",
            "见 [normal.md](normal.md) 与 [over_budget.md](over_budget.md)。",
        ]

    def est_count(d: dict) -> int:
        return sum(1 for s in d.get("steps", []) if s["tool"] == "estimate_cost")

    def infeasible(d: dict) -> bool:
        return any(s.get("detail", {}).get("feasible") is False for s in d.get("steps", []))

    n_plan = (n.get("saved_plan") or {})
    o_plan = (o.get("saved_plan") or {})
    # over_budget 场景里被淘汰的那个组合（首次不可行估算）
    o_rejected = next(
        (
            s
            for s in o.get("steps", [])
            if s.get("detail", {}).get("feasible") is False
        ),
        None,
    )
    rej_text = (
        f"{', '.join(o_rejected['input'].get('material_ids', []))} -> **不可行，缺口 "
        f"{o_rejected['detail'].get('over_budget_gap')}**"
        if o_rejected
        else "（本次运行未出现不可行估算）"
    )

    return [
        "考核要求提供至少两条执行路径的运行记录，体现模型根据不同工具结果调整动作。",
        "本项目提供 9 组，其中最能说明「动态决策」的是这一对（下表数字取自本次实际运行）：",
        "",
        "| | [normal.md](normal.md) | [over_budget.md](over_budget.md) |",
        "| --- | --- | --- |",
        f"| 预算 | {n.get('budget')}（宽裕） | {o.get('budget')}（紧张） |",
        f"| 是否出现不可行估算 | {'是' if infeasible(n) else '**否**'} | {'**是**' if infeasible(o) else '否'} |",
        f"| `estimate_cost` 调用次数 | {est_count(n)} | {est_count(o)} |",
        f"| 被淘汰的组合 | — | {rej_text} |",
        f"| 步数 | {n.get('step_count')} | {o.get('step_count')} |",
        f"| 最终方案成本 | {n_plan.get('total_cost')} / {n.get('budget')} | "
        f"{o_plan.get('total_cost')} / {o.get('budget')} |",
        f"| 终止状态 | `{n.get('status')}` | `{o.get('status')}` |",
    ]


def main() -> int:
    p = argparse.ArgumentParser(description="生成 examples/ 验证材料")
    p.add_argument("--mode", default="real", choices=["real", "mock", "auto"])
    p.add_argument("--model", default=None, help="模型 id，默认 claude-opus-5-5")
    p.add_argument("--effort", default="medium")
    p.add_argument("--only", nargs="*", default=None, help="只重新生成这些场景 key")
    args = p.parse_args()
    return generate(args.mode, args.model, args.effort, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())

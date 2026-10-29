"""命令行入口。

用法示例：
  # 跑内置验收场景（真实模型）
  python -m src.cli run --scenario over_budget

  # 跑全部场景并把 Trace 写入 examples/
  python -m src.cli run --all --mode real --out examples

  # 离线 Mock（无密钥时验证流程，不代表真实模型决策效果）
  python -m src.cli run --scenario over_budget --mode mock

  # 自定义需求
  python -m src.cli plan --theme "夏日气泡水" --budget 10 --deadline 2026-12-15

  # 列出场景
  python -m src.cli list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK，会把 UTF-8 中文输出打乱；这里强制切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from .data_store import DataStore
from .domain import RunResult, TaskInput
from .runner import build_llm, parse_fixed_today, run_scenario
from .scenarios import REPEAT_SCENARIO, SCENARIOS, all_keys, get_scenario
from .agent import ContentPlanningAgent
from .trace import print_trace, write_json, write_markdown

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write_outputs(result: RunResult, out_dir: Path, key: str, name: str, notes: str) -> None:
    write_json(result, out_dir / f"{key}.json")
    write_markdown(result, out_dir / f"{key}.md", case_name=name, notes=notes)
    print(f"  已写入 {out_dir / (key + '.json')} 和 {out_dir / (key + '.md')}")


def cmd_list(args: argparse.Namespace) -> int:
    store = DataStore()
    print("内置验收场景：\n")
    for s in SCENARIOS:
        limits = ""
        if s.max_steps is not None:
            limits = f"（上限 {s.max_steps} 步）"
        print(f"  {s.key:<18} {s.name}{limits}")
        print(f"  {'':<18} 验收点：{s.acceptance_point}")
    s = REPEAT_SCENARIO
    print(f"  {s.key:<18} {s.name}")
    print(f"  {'':<18} 验收点：{s.acceptance_point}")
    print("\n素材与工具费用表：\n")
    print(store.cost_table_markdown())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else None
    keys = all_keys() if args.all else [args.scenario]

    failed = []
    for key in keys:
        scenario = get_scenario(key) or (REPEAT_SCENARIO if key == REPEAT_SCENARIO.key else None)
        if scenario is None:
            print(f"未知场景：{key}（用 `list` 查看可用场景）")
            return 2
        print(f"\n{'#' * 72}")
        print(f"# 场景 {key}：{scenario.name}")
        print(f"# 验收点：{scenario.acceptance_point}")
        print(f"{'#' * 72}")
        print(f"说明：{scenario.expect_notes}")

        out = run_scenario(
            scenario,
            mode=args.mode,
            model=args.model,
            effort=args.effort,
            verbose=args.verbose,
        )

        if isinstance(out, dict) and out.get("runs"):
            # 重复执行场景：打印两次运行并给出幂等结论
            r1, r2 = out["first"], out["second"]
            print("\n--- 第 1 次运行 ---")
            print_trace(r1)
            print("--- 第 2 次运行（重复执行）---")
            print_trace(r2)
            print(f"幂等结论：方案库记录数={out['plan_store_size']}，"
                  f"两次是否同一方案={out['same_plan_chosen']}，"
                  f"第二次是否幂等命中={out['second_is_duplicate']}，"
                  f"无重复写入={out['no_duplicate_writes']}")
            if out_dir:
                write_json(r1, out_dir / f"{key}_run1.json")
                write_json(r2, out_dir / f"{key}_run2.json")
                write_markdown(
                    r2,
                    out_dir / f"{key}.md",
                    case_name=scenario.name,
                    notes=(
                        f"第 2 次运行。幂等结论：方案库记录数={out['plan_store_size']}，"
                        f"两次同一方案={out['same_plan_chosen']}，"
                        f"第二次 save_plan 幂等命中={out['second_is_duplicate']}，"
                        f"无重复写入={out['no_duplicate_writes']}。第 1 次运行见 {key}_run1.json。"
                    ),
                )
                print(f"  已写入 {out_dir / (key + '_run1.json')} 等文件")
            # 不变量：绝不能出现同一 plan_id 被写入两次
            if not out["no_duplicate_writes"] or not out["duplicate_saves_uncharged"]:
                failed.append(key)
        else:
            print_trace(out)
            if out_dir:
                _write_outputs(out, out_dir, key, scenario.name, scenario.expect_notes)
            expected = scenario.expect.get("status") or scenario.expect_status
            if expected and out.status.value != expected:
                allowed = scenario.expect.get("status_in")
                if not (allowed and out.status.value in allowed):
                    failed.append(key)

    if failed:
        print(f"\n以下场景状态与预期不符：{', '.join(failed)}")
        return 1
    print("\n全部场景执行完成，状态符合预期。")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """自定义需求：不走内置场景，直接规划。"""
    store = DataStore()
    task = TaskInput(
        theme=args.theme,
        budget=args.budget,
        deadline=args.deadline,
        audience=args.audience,
        notes=args.notes,
    )
    llm = build_llm(mode=args.mode, model=args.model, effort=args.effort)
    agent = ContentPlanningAgent(store=store, today=parse_fixed_today())
    result = agent.run(task, llm=llm)
    print_trace(result)
    if args.out:
        out_dir = Path(args.out)
        _write_outputs(result, out_dir, "custom", f"自定义需求：{args.theme}", "")
    return 0 if result.status.is_completed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="content-planning-agent",
        description="内容任务规划 Agent（ReAct 循环 + 业务守卫）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出内置验收场景与素材/费用表")

    run = sub.add_parser("run", help="运行内置验收场景")
    run.add_argument("--scenario", default=None, help=f"场景 key，可选：{', '.join(all_keys())}")
    run.add_argument("--all", action="store_true", help="运行全部场景")
    run.add_argument("--mode", default="auto", choices=["auto", "real", "mock"],
                     help="real=真实模型，mock=离线规则策略，auto=有密钥用真实模型")
    run.add_argument("--model", default=None, help="模型 id，默认 claude-opus-5-5")
    run.add_argument("--effort", default="medium", help="推理力度，默认 medium")
    run.add_argument("--out", default=None, help="把 Trace 写入该目录（如 examples）")
    run.add_argument("--verbose", action="store_true", help="实时打印每步")

    plan = sub.add_parser("plan", help="自定义需求规划")
    plan.add_argument("--theme", required=True, help="活动主题")
    plan.add_argument("--budget", type=float, default=None, help="预算（模拟单位）")
    plan.add_argument("--deadline", default=None, help="截止日期 YYYY-MM-DD")
    plan.add_argument("--audience", default=None, help="目标人群")
    plan.add_argument("--notes", default=None, help="补充说明")
    plan.add_argument("--mode", default="auto", choices=["auto", "real", "mock"])
    plan.add_argument("--model", default=None)
    plan.add_argument("--effort", default="medium")
    plan.add_argument("--out", default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "run":
        if not args.all and not args.scenario:
            parser.error("run 需要 --scenario 或 --all")
        return cmd_run(args)
    if args.command == "plan":
        return cmd_plan(args)
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

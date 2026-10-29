"""生成 examples/test-results.md —— 测试结果记录。

任务要求第 50 行：「Trace 与测试结果存放于 examples/」。本脚本把 pytest 的
真实输出写进 examples/，与 Trace 放在一起，供评估文档引用。

用法：
    python scripts/generate_test_results.py            # 只跑离线套件（默认，不消耗 API）
    python scripts/generate_test_results.py --network  # 额外跑真实模型套件（消耗 API）

不重跑真实模型时，脚本会复用上次捕获的 examples/_network_raw.txt，
并在文档里写明捕获时间，避免用旧结果冒充当前代码的验证结果。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

# pytest.ini 的 addopts 里有 -q，会压掉逐条测试名；这里显式覆盖以保留证据。
OFFLINE_ADDOPTS = '-q --strict-markers -m "not network"'
NOISE = re.compile(
    r"PytestDeprecationWarning|warnings\.warn|event loop scope|asyncio_default_fixture"
)


def run_pytest(args: list[str], addopts: str) -> tuple[str, str]:
    """跑 pytest，返回 (逐条输出, 汇总行)。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-vv", "--no-header", "-o", f"addopts={addopts}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    raw = proc.stdout + proc.stderr
    lines = [ln.rstrip() for ln in raw.splitlines() if not NOISE.search(ln)]
    summary = next(
        (ln for ln in reversed(lines) if re.search(r"\d+ (passed|failed)", ln)),
        "（未找到汇总行）",
    )
    return "\n".join(lines), summary


def count_passed(summary: str) -> str:
    """从 pytest 汇总行里取出 passed 数量，避免文档里写死数字。"""
    m = re.search(r"(\d+) passed", summary)
    return m.group(1) if m else "?"


def fence(text: str) -> str:
    return f"```text\n{text.strip()}\n```"


def main() -> int:
    p = argparse.ArgumentParser(description="生成 examples/test-results.md")
    p.add_argument("--network", action="store_true", help="同时重跑真实模型套件（消耗 API）")
    args = p.parse_args()

    EXAMPLES.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    print("运行离线套件……", flush=True)
    offline_out, offline_summary = run_pytest([], OFFLINE_ADDOPTS)
    (EXAMPLES / "_offline_raw.txt").write_text(offline_out, encoding="utf-8")
    offline_n = count_passed(offline_summary)

    network_out = ""
    network_summary = ""
    network_when = ""
    if args.network:
        print("运行真实模型套件（消耗 API）……", flush=True)
        network_out, network_summary = run_pytest(
            ["tests/test_real_model.py", "-m", "network"], ""
        )
        (EXAMPLES / "_network_raw.txt").write_text(network_out, encoding="utf-8")
        network_when = now
    else:
        cached = EXAMPLES / "_network_raw.txt"
        if cached.exists():
            network_out = cached.read_text(encoding="utf-8")
            network_summary = next(
                (
                    ln
                    for ln in reversed(network_out.splitlines())
                    if re.search(r"\d+ (passed|failed)", ln)
                ),
                "",
            )
            mtime = datetime.fromtimestamp(cached.stat().st_mtime)
            network_when = mtime.strftime("%Y-%m-%d %H:%M") + "（复用上次捕获，未重跑）"
    network_n = count_passed(network_summary) if network_summary else "?"

    doc = [
        "# 测试结果记录",
        "",
        f"生成时间：{now} · 生成命令：`python scripts/generate_test_results.py"
        + (" --network`" if args.network else "`")
        + "",
        "",
        "本文件是任务要求的「测试结果」部分（`任务要求.md`：*Trace 与测试结果存放于 "
        "`examples/`*）。运行记录（Trace）见同目录下的各场景 `.md` / `.json`，",
        "索引见 [README.md](README.md)，逐条核对见 [docs/03-evaluation.md](../docs/03-evaluation.md)。",
        "",
        "## 一、离线套件（默认，不消耗 API）",
        "",
        "```bash",
        "python -m pytest",
        "```",
        "",
        f"**结果：`{offline_summary.strip()}`**",
        "",
        "分三层，全部为确定性断言，不依赖网络与模型：",
        "",
        "| 文件 | 覆盖 |",
        "| --- | --- |",
        "| `tests/test_tools.py` | 预算计算口径、四类守卫（预算/截止/依赖/幂等）、失败注入、部分成功 |",
        "| `tests/test_agent_loop.py` | ReAct 循环行为：重规划、失败恢复并保留成功结果、步数与调用上限、同轮并行超限、终止状态判定、缺输入前置拒绝、stop_reason 边界、llm_mode 标注 |",
        "| `tests/test_scenarios.py` | 9 个验收场景（MockLLM）+「两条执行路径必须不同」+「验收点全覆盖」元断言 |",
        "",
        "> 默认 `pytest` 通过 `pytest.ini` 的 `-m \"not network\"` 排除真实模型测试，"
        "因此无凭据环境也能完整跑通、且不会消耗 API。",
        "",
        "<details>",
        f"<summary>展开：{offline_n} 条离线断言逐条结果</summary>",
        "",
        fence(offline_out),
        "",
        "</details>",
        "",
    ]

    if network_out:
        doc += [
            "## 二、真实模型端到端套件（需要 API 凭据）",
            "",
            "```bash",
            "python -m pytest -m network -v",
            "```",
            "",
            f"捕获时间：{network_when}",
            "",
            f"**结果：`{network_summary.strip()}`**",
            "",
            "这一层验证的是**真实模型的决策效果**，不是流程本身 —— MockLLM 只能证明"
            "循环/工具/守卫跑通，只有这里能证明模型真的会根据工具结果重规划。",
            "",
            "| 测试 | 验证的验收点 |",
            "| --- | --- |",
            "| `test_real_model_replans_on_over_budget` | 预算超限：估算初始方案 12 → 观察超限 → 改选替代方案 → 保存 |",
            "| `test_real_model_recovers_from_tool_failure` | 工具失败：注入失败后调整动作、保留成功步骤、失败不计费 |",
            "| `test_real_model_reports_gap_when_infeasible` | 无可行方案：尝试后停止并报告缺口，不虚报成功 |",
            "| `test_real_model_respects_step_limit` | 步数上限：返回明确未完成状态 |",
            "| `test_real_model_rejects_missing_input_without_calling_tools` | 缺预算：调用模型前拒绝，零 token 消耗 |",
            "| `test_real_model_repeat_run_is_idempotent` | 重复执行：无重复写入、幂等保存不计费 |",
            "",
            "<details>",
            f"<summary>展开：{network_n} 条端到端测试逐条结果</summary>",
            "",
            fence(network_out),
            "",
            "</details>",
            "",
        ]
    else:
        doc += [
            "## 二、真实模型端到端套件",
            "",
            "未捕获。运行 `python scripts/generate_test_results.py --network` 生成"
            "（需要 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`，会消耗 API）。",
            "",
        ]

    doc += [
        "## 三、断言口径说明",
        "",
        "真实模型有不确定性：同一场景两次运行可能选不同的可行素材组合、走不同步数。"
        "因此测试断言的是**不变量**而非某次具体行为：",
        "",
        "- 终止状态正确（`completed` / 各类 `incomplete_*`）",
        "- 已保存方案的成本 ≤ 预算、周期 ≤ 可用天数",
        "- 出现过重规划（尝试 ≥2 个不同素材组合、且出现过 `feasible: false` 判定）",
        "- 失败步骤的 `status` 恒为 `failed` 且 `charge` 恒为 0（失败绝不被记为成功、不计费）",
        "- 步数与工具调用数不超上限",
        "- 方案库无重复写入（同一 `plan_id` 不会被写两次）",
        "",
        "这个取舍的由来见 [docs/05-retrospective.md](../docs/05-retrospective.md) § 二："
        "最初把「幂等」误写成「模型两次必须选同一方案」，真实模型一跑就失败了 —— "
        "预算宽裕时它有权在两次运行里选不同组合，那是两个不同方案，不是重复保存。",
        "",
    ]

    out = EXAMPLES / "test-results.md"
    out.write_text("\n".join(doc), encoding="utf-8")
    print(f"已写入 {out}")
    print(f"  离线：{offline_summary.strip()}")
    if network_summary:
        print(f"  端到端：{network_summary.strip()}（{network_when}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

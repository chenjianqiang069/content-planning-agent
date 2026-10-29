# Token 用量与步数汇总

本文件由 `scripts/generate_examples.py` 从同目录的 JSON 自动汇总，
重跑真实模型后会随之更新，因此不会与 Trace 漂移。

| 场景 | 步数 | input tokens | output tokens | 终止状态 |
| --- | --- | --- | --- | --- |
| `normal` | 4 | 12,087 | 901 | `completed` |
| `over_budget` | 6 | 13,961 | 1,743 | `completed` |
| `tool_failure` | 7 | 19,294 | 1,761 | `completed` |
| `missing_budget` | 0 | —（模型未被调用） | — | `incomplete_missing_input` |
| `missing_deadline` | 0 | —（模型未被调用） | — | `incomplete_missing_input` |
| `infeasible_all` | 3 | 9,268 | 1,998 | `incomplete_no_feasible_plan` |
| `tight_deadline` | 6 | 13,373 | 2,373 | `incomplete_no_feasible_plan` |
| `step_limit` | 2 | 3,955 | 816 | `incomplete_step_limit` |
| `repeat_same_run1` | 7 | 17,946 | 1,821 | `completed` |
| `repeat_same_run2` | 4 | 12,257 | 1,356 | `completed` |

合计（8 次运行）：input 102,141 / output 12,769 tokens。

input 偏高是因为每轮都重发完整历史且未启用 prompt caching；
系统提示与工具费用表是稳定前缀，加 `cache_control` 可显著降本（见 [docs/03-evaluation.md](../docs/03-evaluation.md) 已知限制第 4 条）。

`missing_budget` / `missing_deadline` 两行没有 token 用量：
缺必需输入时在调用模型之前就被拒绝，零 API 消耗。

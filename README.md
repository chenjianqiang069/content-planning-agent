# 内容任务规划 Agent（Content Task Planning Agent）

小影校招 AI Agent 开发岗位考核项目 · **题号：内容任务规划 Agent**

一个用 **ReAct 循环**驱动的内容活动方案规划 Agent：帮助运营人员在**预算与截止时间**内完成一份可执行方案。模型根据「成本超限、工具错误、部分成功结果」动态调整计划、选替代方案或停止，并保留已成功步骤的结果。

---

## 30 秒速览

- **模型真实决策**：调用哪个工具、用什么参数、超预算后是否降级、失败后是否换方案、何时停止 —— 全部由模型每一轮输出决定，代码只做守卫 / 计费 / Trace / 上限。
- **四个业务工具**（本地 Mock 实现）：`search_materials`（素材检索）、`generate_script`（脚本生成）、`estimate_cost`（成本估算）、`save_plan`（方案保存）。
- **强业务守卫**：预算计算只有一处口径；保存前必须满足「同一组素材已成功生成脚本」的步骤依赖；失败步骤绝不会被记为成功；重复保存幂等。
- **可复现的验收**：9 个内置场景覆盖全部验收点，`examples/` 存放真实模型产出的输入 / 输出 / Trace 与测试结果，`tests/` 提供 56 个离线断言 + 6 个真实模型端到端测试。

---

## 环境要求

- Python 3.10+（在 3.13 上开发验证）
- `anthropic` SDK
- 一个 Anthropic API 凭据（`ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`）—— **仅真实模型模式需要**；离线 Mock 模式无需任何凭据。

## 安装

```bash
# 在项目根目录
python -m pip install -r requirements.txt
cp .env.example .env      # 按需填入凭据；也可跳过用 --mode mock
```

## 运行方式（CLI，三选一中的 CLI）

```bash
# 1) 列出全部验收场景、素材目录与工具费用表
python -m src.cli list

# 2) 跑单个场景（真实模型）—— 以「预算超限重规划」为例
python -m src.cli run --scenario over_budget --mode real

# 3) 跑全部场景，并把 Trace 写入 examples/
python -m src.cli run --all --mode real --out examples

# 4) 离线 Mock（无凭据时验证流程；不代表真实模型决策效果）
python -m src.cli run --scenario over_budget --mode mock

# 5) 自定义需求
python -m src.cli plan --theme "夏日气泡水新品种草" --budget 10 --deadline 2026-12-15
```

参数说明：`--mode` = `real`（真实模型）/ `mock`（离线）/ `auto`（有凭据用真实，否则回退 mock，默认）；`--model` 覆盖模型 id（默认 `claude-opus-5-5`）；`--effort` 推理力度（默认 `medium`）。

## 测试

```bash
# 离线测试（默认，快速、无需网络、不消耗 API）：56 个断言
python -m pytest

# 真实模型端到端测试（需要凭据，会消耗 API）：6 个
python -m pytest -m network -v

# 重新生成 examples/ 验证材料（真实模型）
python scripts/generate_examples.py --mode real
```

## 评审入口（从这里开始看）

| 想了解 | 看这里 |
| --- | --- |
| 需求、用户、成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 测试预期、实际结果、失败与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助范围、个人修改与验证 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难问题、收获、下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| **输入 / 输出 / Trace 实例** | [examples/README.md](examples/README.md) |
| 核心代码 | [src/agent.py](src/agent.py)（循环）、[src/tools.py](src/tools.py)（守卫）、[src/llm.py](src/llm.py)（模型接入） |

## 目录结构

```
.
├── README.md              # 本文件：环境、安装、运行、评审入口
├── requirements.txt       # 依赖
├── .env.example           # 所需环境变量（无凭据也能跑 mock）
├── pytest.ini             # 测试配置（默认排除 network 标记）
├── src/                   # 项目代码与模型/工具接入
│   ├── agent.py           #   ReAct 主循环、上限控制、终止状态判定
│   ├── tools.py           #   四个业务工具 + 预算/截止/依赖/幂等守卫 + 失败注入
│   ├── llm.py             #   AnthropicLLM（真实）与 MockLLM（离线）
│   ├── tool_schemas.py    #   给模型的工具 schema（真实/mock 共用）
│   ├── domain.py          #   数据结构与状态枚举
│   ├── data_store.py      #   data/*.json 加载
│   ├── scenarios.py       #   9 个验收场景定义（CLI/测试/examples 共用）
│   ├── runner.py          #   场景执行器（含重复执行双运行）
│   ├── trace.py           #   JSON + Markdown 运行记录输出
│   └── cli.py             #   命令行入口
├── tests/                 # 可执行测试
│   ├── test_tools.py      #   工具层单元（预算计算、守卫、幂等、注入）
│   ├── test_agent_loop.py #   循环行为（离线、确定性）
│   ├── test_scenarios.py  #   全部验收场景（MockLLM）
│   └── test_real_model.py #   真实模型端到端（-m network）
├── examples/              # 输入、实际输出、Trace（真实模型产出）
├── data/                  # 合成资料与规则
│   ├── materials.json     #   素材库（cost / lead_days / quality / 主题加成）
│   ├── tool_costs.json    #   工具费用与耗时表
│   └── rules.json         #   业务规则、守卫、上限、状态码
├── docs/                  # 设计与评估文档
└── scripts/
    └── generate_examples.py
```

## 验收点覆盖一览

| 验收点 | 场景 | 说明 |
| --- | --- | --- |
| 正常完成 | `normal` | 预算充足，一次通过：检索 → 估算 → 脚本 → 保存 |
| 缺预算 | `missing_budget` | 在调用模型前拒绝，返回明确未完成状态，零工具调用 |
| 缺截止时间 | `missing_deadline` | 同上 |
| 工具失败 | `tool_failure` | 注入一次 `generate_script` 失败，模型调整后仍完成，保留成功的检索结果 |
| 预算超限 | `over_budget` | **对应题目参考实例**：预算 10，初始方案估算 12（缺口 2）→ 模型改选更便宜的替代方案估算 8 → 保存，总成本 8.0、剩余 2.0 |
| 无可行方案 | `infeasible_all` | 预算低于任何组合，模型尝试后停止并报告缺口，不虚报成功 |
| 截止过紧 | `tight_deadline` | 预算充足但周期不可行，报告超出天数 |
| 重复执行 | `repeat_same` | 两次运行共享方案库，第二次幂等命中、不重复计费、记录数仍为 1 |
| 步数上限 | `step_limit` | 估算永久失败 + 上限 2 步，返回明确未完成状态并保留成功步骤 |

> **失败步骤续跑（加分项）**：本项目的步骤依赖守卫使得「脚本失败 → 保存被拒」可被模型观察到并重试；`tool_failure` 场景演示了失败后的恢复。方案库 `saved_plans` 支持跨运行持久化，`repeat_same` 演示了在既有结果上续跑。

## Mock 边界声明

`--mode mock` 使用离线 `MockLLM`（规则策略），**只验证流程**：循环、工具、守卫、重规划链路能跑通。**它不能证明真实模型的决策效果**。`examples/` 下提交的运行记录一律由真实模型（`claude-opus-5-5`）产出；四个业务工具本身是本地函数实现（题目允许），不联网。

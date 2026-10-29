# 04 · AI 辅助使用说明

考核要求「允许使用 AI 辅助工具，须如实记录使用范围、个人贡献与验证结果」。本文如实记录。

## 一、使用的 AI 工具

| 工具 | 用途 |
| --- | --- |
| Claude Code（Anthropic CLI，模型 `claude-haiku-5-5`） | 项目设计、代码实现、测试编写、文档撰写 |
| Anthropic Messages API（`claude-opus-5-5`） | **项目运行时**的 Agent 决策模型 —— 这是被交付物本身，不是辅助工具 |

区分很重要：前者是**开发辅助**，后者是**项目组成部分**。`examples/` 里所有 Trace 都由后者产出，是真实模型决策记录。

## 二、AI 辅助的范围

### AI 产出的内容

- 全部 `src/` 代码：领域模型、数据存储、四个业务工具与守卫、真实/Mock 两套模型接入、ReAct 循环、Trace 输出、CLI、场景执行器
- 全部 `tests/` 代码：56 个离线断言 + 6 个真实模型端到端测试
- `data/` 合成数据：素材库、工具费用表、业务规则
- `docs/` 全部文档、`README.md`、`.env.example`、`requirements.txt`、`pytest.ini`
- `examples/` 的生成脚本与产出文件

### AI 未做、需要人做的内容

- **判断题目要求**：读 `任务要求.md` / `具体题目.md`，决定哪些是硬性验收、哪些是加分项、哪些明确非必需（界面、部署、多 Agent、生产级 Runtime）
- **技术取舍决策**：是否用框架、用手写 loop 还是 SDK `tool_runner`、模型与 effort 档位选择（理由见 [02-solution.md](02-solution.md) § 五）
- **验收与纠偏**：运行测试、检查真实模型 Trace 是否真的体现了重规划、发现并修正问题（见下节）
- **提交材料**：GitHub 仓库、commit、评审账号可读权限

## 三、开发过程中发现并修正的问题

这些是**验证环节**发现的实际缺陷，不是 AI 一次写对的。逐条记录以便评审判断质量把关是否到位：

| # | 问题 | 如何发现 | 修正 |
| --- | --- | --- | --- |
| 1 | 系统提示里直接写入了素材目录，模型因此**跳过 `search_materials`**、心算可行性，`over_budget` 场景一次都不撞超预算 —— 题目要求的核心重规划路径没有发生 | 运行真实模型看 Trace，发现步骤里没有超预算判定 | 把素材目录从系统提示移除，只保留工具费用表，并明确要求「素材必须通过 `search_materials` 检索，成本结论必须以 `estimate_cost` 返回为准」。修正后真实模型确实先估算初始方案（12）→ 观察超预算 → 改选替代（8），与题目参考实例一致 |
| 2 | `over_budget` 场景没有指定初始方案，模型自行挑选时可能直接选中可行组合，绕过「先超限再降级」 | 同上 | 在场景 `notes` 里写明运营倾向的初始方案（`m_short_video_pro` + `m_kol_top`），复现题目参考实例形状 |
| 3 | 终止状态判定依赖**匹配错误文本**（`"预算超限" in step.error`）。模型没调 `save_plan` 就放弃时，被误判为 `incomplete_not_saved`，实际应为 `incomplete_no_feasible_plan` | 跑 MockLLM 全场景，发现 `infeasible_all` / `tight_deadline` 状态与预期不符 | 给 `StepRecord` 增加结构化 `detail` 字段（`feasible` / `rejected_by` / `over_budget_gap` / `plan_lead_days` / `available_days`），状态判定改用结构化证据，不再做字符串匹配 |
| 4 | 超期判定用 `"超出" in violation` 会把「**缺少**截止时间」误判为超期 | 代码审查 | 改为比较结构化字段 `plan_lead_days > available_days` |
| 5 | 无可行方案时的缺口报告用 `max(gaps)`，报出的是「最不可行」方案的缺口，与「最接近可行」的措辞矛盾 | 跑 `infeasible_all`，输出「最接近的方案仍超预算 10.0」但实际最小缺口是 6.0 | 改为 `min(gaps)` |
| 6 | `_find_saved_plan` 从摘要字符串里 `split("：")[1]` 解析 plan_id —— 脆弱，摘要格式一变就断 | 代码审查 | 在 `_loop` 执行时直接记录 `save_plan` 成功返回的 `plan_id`，并校验其确实存在于方案库 |
| 7 | `ToolOutcome` 收到不存在的 `rejected_by=` 关键字参数；`self.timeout` 从未赋值却被使用；`MockLLM._last_tool` 被读取但从未设置 | 首次运行即报错 | 分别修正：`rejected_by` 移入 `payload`；`__init__` 保存 `timeout`；`_next_call` 记录 `_last_tool` |
| 8 | `save_plan` 幂等分支残留 `PlanRecord(**{...} if False else dict(...))` 死代码 | 代码审查 | 清理为直接构造 |
| 9 | 测试 `test_failed_script_does_not_satisfy_dependency` 直接调 `ctx.generate_script()` 并期望注入生效 —— 但失败注入在 **Agent 层**（`_execute`）拦截，工具层不检查，断言必然失败 | `pytest` 报错 `assert True is False` | 承认设计如此（注入统一在一处，对所有工具生效）：工具层测试改用参数非法导致的真实失败；注入型失败的断言移到 `tests/test_agent_loop.py::test_injected_script_failure_blocks_save`，并在两处测试的 docstring 里交叉注明 |
| 10 | 默认 `pytest` 会跑真实模型测试，导致挂起 120 秒以上并消耗 API | 运行 `pytest` 超时 | `pytest.ini` 增加 `-m "not network"`，默认离线；真实模型测试需显式 `-m network` |
| 11 | Windows 控制台 GBK 编码把 UTF-8 中文输出打乱 | 首次运行 CLI | `cli.py` 与生成脚本对 stdout/stderr `reconfigure(encoding="utf-8")`；`pytest.ini` 里的中文注释同样触发 GBK 解析错误，改为 ASCII |
| 12 | `agent.py` 底部用 `ContentPlanningAgent._system_prompt = fn` 猴补丁挂方法 | 代码审查 | 改为类内正常方法 |
| 13 | 测试里留了 `assert any(... or True for ...)` 占位断言（恒真，无意义）；`test_tool_call_limit_blocks_extra_calls_in_same_turn` 名字描述的行为与实际不符 | 代码审查 | 删除占位断言；重命名为 `test_tool_call_limit_stops_loop_after_budget_exhausted` 并另补 `test_parallel_calls_over_budget_rejected_mid_turn` 真正覆盖同轮并行超限分支 |
| 14 | **`plan_id` 跨运行不稳定，幂等验收实际上是坏的**：`plan_id` 的指纹用了 `script_id`，而 `script_id` 内嵌 `_script_seq`（本次运行第几个生成的脚本）。同一主题+同一素材组合，若模型在某次运行里先生成了别的脚本，序号就变，`plan_id` 随之变 → 第二次运行不会幂等命中，方案库出现重复记录 | 真实模型套件重跑时 `test_real_model_repeat_run_is_idempotent` 失败；随后写脚本单独复现，确认「相同主题+相同素材」两次得到不同 `plan_id`、方案库 2 条 | 脚本记录改用**内容指纹** `sha1(style)`（与执行顺序无关）参与 `plan_id`；`script_id` 保留但不再进入方案身份。补 `test_plan_id_is_stable_across_runs_regardless_of_script_order`（回归）与 `test_different_script_style_yields_different_plan_id`（确认身份仍区分内容） |
| 15 | 步数上限场景漏掉一种正确路径：真实模型看到 `estimate_cost` 永久失败会**主动放弃**而非撞上限，走 `incomplete_tool_failure`；而分类逻辑要求「所有步骤都失败」，把「检索成功 + 估算失败」漏到兜底的 `incomplete_not_saved` | 真实模型套件重跑时 `test_real_model_respects_step_limit` 失败（`incomplete_not_saved` != `incomplete_step_limit`） | 分类改为「存在工具自身故障型失败（`detail` 无 `rejected_by`）即 `incomplete_tool_failure`」，并区分守卫拒绝（工具按设计工作，应归 `incomplete_no_feasible_plan`）；测试改为断言不变量。补两个方向的离线断言 |

第 1、2 条尤其关键：它们不是代码 bug，而是**设计缺陷导致验收点没被真正覆盖**。如果不实际跑真实模型看 Trace，只看「测试通过」，会误以为 `over_budget` 场景已经验证了重规划 —— 实际上模型走了另一条更聪明的路径，题目要的那条路径从未发生。

第 14 条同样值得单说：它是一个**只有真实模型才会触发的 bug**。离线测试（`MockLLM`、`ScriptedLLM`）每次都用固定的动作序列，脚本序号总是从 01 开始，所以幂等断言一直绿着；真实模型在不同运行里探索的路径不同，序号才会错位。这说明"离线测试全绿"对幂等这类跨运行不变量的证明力是有限的 —— 必须让真实的、有差异的运行去撞。

## 四、验证方式与结果

### 离线测试（默认，不消耗 API）

```bash
python -m pytest
# 56 passed, 6 deselected in 0.16s
```

分层：
- `tests/test_tools.py`（25）—— 预算计算、四类守卫、幂等（含 plan_id 跨运行稳定性回归）、失败注入、部分成功、摘要
- `tests/test_agent_loop.py`（14）—— 用 `ScriptedLLM` 确定性验证循环：重规划、失败恢复与保留成功结果、步数/调用上限、同轮并行超限、终止状态判定、缺输入前置拒绝、重复执行幂等、决策依据入 Trace
- `tests/test_scenarios.py`（11）—— 用 `MockLLM` 跑全部 9 个场景，按 `expect` 断言行为；含「两条执行路径必须不同」与「验收点全覆盖」两项元断言

### 真实模型端到端（消耗 API）

```bash
python -m pytest -m network -v
# tests/test_real_model.py: 6 passed
```

6 项分别验证：超预算重规划、工具失败恢复、无可行方案报告缺口、步数上限、缺输入前置拒绝（且零 token 消耗）、重复执行幂等。

### 运行记录

```bash
python scripts/generate_examples.py --mode real
```

产出 `examples/` 下 9 组场景的 JSON + Markdown，全部由 `claude-opus-5-5` 真实决策产出，每份带 `llm_mode: "real"` 标记。

## 五、关于「个人贡献」

> **候选人请在提交前改写本节。** 下面如实说明当前状态，以及哪些内容必须由你本人确认后才能作为你的陈述提交。

### 当前状态的如实说明

本项目的代码、测试、文档均由 AI（Claude Code）产出，包括第三节那 **15 条问题的发现与修正** —— 那些问题绝大多数是 AI 在自查、跑测试、读真实模型 Trace 时自己发现并修掉的，**不是候选人逐条排查出来的**。这一点必须写明，否则本节会成为一份不实的贡献声明。

因此，如果按当前状态提交，本项目的准确定性是：

- 候选人：给出题目、设定目标、审阅 AI 产出、决定提交内容
- AI：需求拆解、架构设计与取舍、全部实现、全部测试、文档撰写、以及问题的发现与修正

### 必须由候选人本人填写/确认的部分

- [ ] **实际投入工时**：`docs/05-retrospective.md` § 一的「实际用时」列（表里的 ~3.5 小时是 AI 推算值，已明确标注不是真实计时，需要你填自己的数字）
- [ ] **技术取舍是否认同**：§ 三里那些理由（不用框架、手写 loop、Opus 5.5 + effort medium）你是否理解并认同？若有不同判断，请改写 —— 考核看重的是"能解释取舍"，照抄 AI 的理由无法证明这一点
- [ ] **对 15 条修正的复核**：至少通读第三节，确认你理解每条改了什么、为什么。评审可能就此追问
- [ ] **贡献边界的重新表述**：如果你在过程中确实做了 AI 没做的事（例如指出某个设计不对、要求补某个场景、自己读懂并重写了某段代码），请如实补充进来

### 一个诚实的建议

考核原文是「允许使用 AI 辅助工具，须如实记录使用范围、个人贡献与验证结果」，且评审目标是「能理解业务问题，借助 AI 完成可运行项目，并用测试证明效果」。**"借助 AI 完成"本身就是被允许的形态**，所以如实写"AI 完成了实现与自查、我负责 X"并不会因此失分；相反，把 AI 的发现包装成自己的排查一旦在追问中露出来，损失要大得多。

如果要提高这份材料的说服力，最有效的做法不是改写归属，而是**真的去动它**：读代码、改掉一处你不认同的设计、自己加一个场景或一条断言、亲手跑一遍真实模型并解读 Trace。这样你就有了无法被质疑的个人贡献，本节也能如实写出具体内容。

## 六、诚实性声明

- `examples/` 中所有 Trace 均为真实模型（`claude-opus-5-5`）运行产出，未经手工编造或美化；文档里引用的数字、plan_id、步数均已与实际 JSON 核对一致
- `tests/` 全部可执行且当前通过：离线 56 条 + 真实模型 6 条；未使用 `skip` / `xfail` 掩盖失败（`test_real_model.py` 的 `network` 标记是为避免默认消耗 API，用 `-m network` 可全部执行并通过）
- 四个业务工具为本地函数实现（题目允许 Mock），不联网；模型调用为真实 API
- MockLLM 的边界已在 README、`examples/README.md`、代码 docstring 三处标注：**只验证流程，不证明真实模型决策效果**
- 本文件 § 五已如实说明：代码、测试、文档及第三节 15 条问题的发现与修正，均由 AI 完成，不是候选人逐条排查所得 —— 该节的归属陈述需候选人本人复核确认后再提交
- 未提交任何真实密钥或隐私数据；`.env.example` 中为占位值；`.claude/`（本地工具状态）已列入 `.gitignore`

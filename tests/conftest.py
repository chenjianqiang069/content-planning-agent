"""pytest 共享夹具。

测试分三层，对应「每步结束都要进行测试」的要求：
1. 工具层单元测试（tests/test_tools.py）：预算计算、守卫、幂等、失败注入
2. Agent 循环测试（tests/test_agent_loop.py）：用 ScriptedLLM 精确控制模型动作，
   离线、确定性地验证循环行为（重规划、失败恢复、上限、状态判定）
3. 场景测试（tests/test_scenarios.py）：用 MockLLM 跑全部验收场景

真实模型的端到端验证放在 tests/test_real_model.py，标记为 slow/network，
默认跳过（需 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 且 -m network）。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.data_store import DataStore  # noqa: E402
from src.domain import ToolCall  # noqa: E402
from src.llm import LLMTurn  # noqa: E402
from src.scenarios import FIXED_TODAY  # noqa: E402
from src.tools import FailureInjector, ToolContext  # noqa: E402


@pytest.fixture(scope="session")
def store() -> DataStore:
    return DataStore()


@pytest.fixture
def today() -> date:
    """固定日期，让截止天数可复现。"""
    return datetime.strptime(FIXED_TODAY, "%Y-%m-%d").date()


@pytest.fixture
def injector() -> FailureInjector:
    return FailureInjector()


class ScriptedLLM:
    """按脚本返回预定义动作的假模型，用于离线、确定性地测试 Agent 循环。

    script 的每一项是 (tool_name, args, decision_basis) 或字符串 "END:最终答复"。
    这样测试可以精确复现「模型观察到 X 后选择了 Y」，不依赖真实模型的不确定性。
    """

    name = "scripted:test"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self._i = 0
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self.request_count = 0
        self.observed: list[list[dict[str, Any]]] = []  # 记录每次 observe 收到的工具结果

    def _pop(self) -> LLMTurn:
        self.request_count += 1
        if self._i >= len(self.script):
            return LLMTurn(text="（脚本已耗尽）", stop_reason="end_turn", usage=dict(self._usage))
        item = self.script[self._i]
        self._i += 1

        if isinstance(item, str) and item.startswith("END:"):
            return LLMTurn(
                text=item[4:], stop_reason="end_turn", usage=dict(self._usage)
            )
        name, args, basis = item
        call = ToolCall(id=f"scripted_{self._i:02d}", name=name, input=args)
        return LLMTurn(
            text=f"[决策依据] {basis}",
            tool_calls=[call],
            stop_reason="tool_use",
            usage=dict(self._usage),
        )

    def start(self, system: str, task_prompt: str) -> LLMTurn:
        self.system = system
        self.task_prompt = task_prompt
        return self._pop()

    def observe(self, tool_results: list[dict[str, Any]]) -> LLMTurn:
        self.observed.append(tool_results)
        return self._pop()

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    # --- 断言辅助 ---
    def last_payload(self) -> dict[str, Any]:
        """最近一次 observe 收到的最后一个工具结果 payload。"""
        import json

        if not self.observed:
            return {}
        content = self.observed[-1][-1].get("content")
        if isinstance(content, str):
            try:
                return json.loads(content)
            except ValueError:
                return {}
        return content if isinstance(content, dict) else {}


@pytest.fixture
def scripted_llm():
    """返回工厂：scripted_llm([...]) -> ScriptedLLM"""
    return lambda script: ScriptedLLM(script)


def make_context(store: DataStore, task, today: date, injector: FailureInjector | None = None,
                 saved_plans: dict | None = None) -> ToolContext:
    return ToolContext(
        task=task,
        store=store,
        injector=injector or FailureInjector(),
        today=today,
        saved_plans=saved_plans if saved_plans is not None else {},
    )

"""模型接入层。

两种实现共用同一接口，Agent 循环不关心背后是谁：
- AnthropicLLM：真实调用 Messages API（tool use），决策由模型做出
- MockLLM：离线规则策略，用于无网络/无密钥时验证流程。
  边界说明：MockLLM 只验证「循环、工具、守卫、重规划」这条链路能跑通，
  不能证明真实模型的决策效果；验收用的执行记录一律以 AnthropicLLM 产出。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from .domain import ToolCall
from .tool_schemas import TOOL_SCHEMAS


@dataclass
class LLMTurn:
    """模型一轮输出：要么请求工具调用，要么给出最终文本。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw_blocks: list[Any] = field(default_factory=list)  # 原始 content blocks，回传用

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    name: str

    def start(self, system: str, task_prompt: str) -> LLMTurn:
        """首轮：给出系统提示与任务，返回模型第一个动作。"""

    def observe(self, tool_results: list[dict[str, Any]]) -> LLMTurn:
        """把工具结果回传给模型，返回下一个动作。"""


# --------------------------------------------------------------------------
# 真实模型
# --------------------------------------------------------------------------
class AnthropicLLM:
    """通过 Anthropic Messages API 驱动决策。

    采用手写 agentic loop（而非 SDK 的 tool_runner），因为本项目必须自己控制：
    步数/工具调用上限、失败注入、逐步 Trace 记录、以及达到上限时的未完成状态。
    tool_runner 会把这些细节吞进 SDK 内部循环，不满足审核要点。
    """

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        max_tokens: int = 4096,
        timeout: float = 120.0,
        client: Any = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.name = f"anthropic:{model}"
        self.messages: list[dict[str, Any]] = []
        self.system = ""
        self._usage = {"input_tokens": 0, "output_tokens": 0}
        self.request_count = 0
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(timeout=self.timeout, max_retries=1)
        return self._client

    # --- LLMClient 接口 --------------------------------------------------
    def start(self, system: str, task_prompt: str) -> LLMTurn:
        self.system = system
        self.messages = [{"role": "user", "content": task_prompt}]
        return self._call()

    def observe(self, tool_results: list[dict[str, Any]]) -> LLMTurn:
        self.messages.append({"role": "user", "content": tool_results})
        return self._call()

    # --- internals -------------------------------------------------------
    def _call(self) -> LLMTurn:
        client = self._get_client()
        self.request_count += 1
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system,
            tools=TOOL_SCHEMAS,
            output_config={"effort": self.effort},
            messages=self.messages,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self._usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                # input 已经是 dict（SDK 解析过），不要做字符串匹配
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        input=dict(block.input or {}),
                    )
                )

        # 助手消息必须原样回传（含 thinking / tool_use blocks），否则下一轮会 400
        self.messages.append({"role": "assistant", "content": response.content})

        return LLMTurn(
            text="\n".join(t for t in text_parts if t.strip()),
            tool_calls=calls,
            stop_reason=response.stop_reason or "",
            usage=dict(self._usage),
            raw_blocks=list(response.content),
        )

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)


def build_anthropic_llm(model: str | None = None, effort: str = "medium") -> AnthropicLLM:
    """从环境变量构造真实客户端；model 可用 CONTENT_AGENT_MODEL 覆盖。"""
    model = model or os.environ.get("CONTENT_AGENT_MODEL") or "claude-opus-5-5"
    return AnthropicLLM(model=model, effort=effort)


def api_available() -> bool:
    """是否具备调用真实模型的最小条件（密钥或 OAuth 登录态）。"""
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


# --------------------------------------------------------------------------
# 离线 Mock
# --------------------------------------------------------------------------
class MockLLM:
    """规则策略：读上下文状态决定下一步，不调用任何网络。

    它会按「检索 → 选最优组合估算 → （超预算则降级到 economy 重新估算）
    → 生成脚本 → 保存」推进，并在保存被拒时尝试替代方案，
    因此可以驱动完整的重规划链路，供离线跑通测试。
    """

    def __init__(self, context: Any = None) -> None:
        self.name = "mock:policy"
        self.context = context
        self._step = 0
        self._tried: list[str] = []
        self._last_tool = ""
        self.messages: list[dict[str, Any]] = []
        self.request_count = 0
        self._usage = {"input_tokens": 0, "output_tokens": 0}

    # --- LLMClient 接口 --------------------------------------------------
    def start(self, system: str, task_prompt: str) -> LLMTurn:
        self._step = 0
        return self._decide("先检索可用素材，了解成本与交付周期。")

    def observe(self, tool_results: list[dict[str, Any]]) -> LLMTurn:
        return self._decide_from_results(tool_results)

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    # --- 策略 ------------------------------------------------------------
    def _next_call(self, name: str, args: dict[str, Any], basis: str) -> LLMTurn:
        self._step += 1
        self.request_count += 1
        self._last_tool = name  # 记录刚请求的工具，observe 时据此分支
        call = ToolCall(id=f"mock_call_{self._step:02d}", name=name, input=args)
        return LLMTurn(
            text=basis,
            tool_calls=[call],
            stop_reason="tool_use",
            usage=dict(self._usage),
        )

    def _decide(self, basis: str) -> LLMTurn:
        return self._next_call("search_materials", {}, basis)

    def _candidate_sets(self) -> list[list[str]]:
        """候选组合：先高质量组合，再经济组合。"""
        ctx = self.context
        if ctx is None:
            return []
        store = ctx.store
        premium = [m.id for m in store.materials if m.tier == "premium"]
        economy = [m.id for m in store.materials if m.tier == "economy"]
        sets: list[list[str]] = []
        if premium:
            sets.append(premium[:2])
        if economy:
            sets.append(economy[:2])
        if premium:
            sets.append(premium[:1])
        return sets

    def _decide_from_results(self, tool_results: list[dict[str, Any]]) -> LLMTurn:
        last = tool_results[-1] if tool_results else {}
        content = last.get("content")
        payload = self._as_payload(content)
        is_error = bool(last.get("is_error"))
        tool_name = self._last_tool  # 产生这批结果的工具
        sets = self._candidate_sets()

        def fallback_or_stop(reason: str) -> LLMTurn:
            remaining = [s for s in sets if self._sig(s) not in self._tried]
            if remaining:
                pick = remaining[0]
                self._tried.append(self._sig(pick))
                return self._next_call(
                    "estimate_cost",
                    {"material_ids": pick},
                    f"{reason}，改用替代组合 {pick} 重新估算。",
                )
            return LLMTurn(
                text=f"{reason}，且所有替代组合均已尝试，停止规划。",
                stop_reason="end_turn",
                usage=dict(self._usage),
            )

        if is_error:
            return fallback_or_stop("上一步工具失败")

        if tool_name == "search_materials":
            pick = next((s for s in sets if self._sig(s) not in self._tried), None)
            if pick is None:
                return LLMTurn(
                    text="没有更多候选组合，结束。", stop_reason="end_turn", usage=dict(self._usage)
                )
            self._tried.append(self._sig(pick))
            return self._next_call(
                "estimate_cost",
                {"material_ids": pick},
                f"检索完成，先估算首选组合 {pick} 的成本与周期。",
            )

        if tool_name == "estimate_cost":
            if payload.get("feasible"):
                return self._next_call(
                    "generate_script",
                    {"material_ids": payload.get("material_ids", []), "style": "mock"},
                    "估算可行，为该组合生成脚本以满足保存前的步骤依赖。",
                )
            return fallback_or_stop(
                f"上一组合不可行（{'; '.join(payload.get('violations', []))}）"
            )

        if tool_name == "generate_script":
            return self._next_call(
                "save_plan",
                {"material_ids": payload.get("material_ids", [])},
                "脚本已生成，依赖满足，保存方案。",
            )

        if tool_name == "save_plan":
            if payload.get("plan_id"):
                return LLMTurn(
                    text=f"方案已保存：{payload.get('plan_id')}。任务完成。",
                    stop_reason="end_turn",
                    usage=dict(self._usage),
                )
            return fallback_or_stop("保存被拒绝")

        return LLMTurn(text="无法继续推进，结束。", stop_reason="end_turn", usage=dict(self._usage))

    @staticmethod
    def _as_payload(content: Any) -> dict[str, Any]:
        """工具结果回传时 content 是 JSON 字符串，这里解析回 dict。"""
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            import json

            try:
                parsed = json.loads(content)
                return parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                return {}
        return {}

    @staticmethod
    def _sig(ids: list[str]) -> str:
        return "|".join(sorted(set(ids)))

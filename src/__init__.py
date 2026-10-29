"""内容任务规划 Agent（Content Task Planning Agent）。

校招 AI Agent 开发岗位考核项目。一个用 ReAct 循环驱动的内容活动方案规划 Agent：
模型根据成本超限、工具错误、部分成功结果，动态调整计划、选替代方案或停止，
并保留已成功步骤的结果。
"""

__all__ = [
    "agent",
    "data_store",
    "domain",
    "llm",
    "scenarios",
    "tool_schemas",
    "tools",
    "trace",
]

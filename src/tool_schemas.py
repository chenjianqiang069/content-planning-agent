"""工具 schema（给模型看的接口定义）。

集中一处，真实模型与 mock 模型共用，保证两条路径的工具接口完全一致。
strict=True + additionalProperties=False 让 API 校验参数结构。
"""

from __future__ import annotations

from typing import Any

MATERIAL_IDS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "素材 id 列表，来自 search_materials 的返回",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_materials",
        "description": (
            "检索可用素材。返回素材的 cost（模拟单位）、lead_days（交付天数）、quality。"
            "只读，不计费。规划的第一步通常先检索，了解有哪些素材可选。"
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按标签过滤，如 ['短视频']；留空表示不过滤",
                },
                "tier": {
                    "type": "string",
                    "enum": ["premium", "economy"],
                    "description": "按档位过滤：premium=高质量高成本，economy=经济型",
                },
                "max_cost": {
                    "type": "number",
                    "description": "只返回 cost <= max_cost 的素材，用于控制预算",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_script",
        "description": (
            "为一组素材生成活动脚本。计费 fee=1 模拟单位（计入方案总成本）。"
            "保存方案前必须先为「同一组素材」成功生成脚本，这是硬性步骤依赖。"
            "若某素材无法按期交付或 id 无效，会返回部分成功，需替换素材。"
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "material_ids": MATERIAL_IDS,
                "style": {
                    "type": "string",
                    "description": "脚本风格，如 '种草'、'促销'、'品牌故事'",
                },
            },
            "required": ["material_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "estimate_cost",
        "description": (
            "估算一组素材方案的总成本与交付周期，并与预算/截止时间比对。"
            "只读，不计费。返回 projected_spend（含已花费+素材+待付脚本费）、"
            "是否超预算及缺口、是否超期。保存前应先用它确认可行。"
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"material_ids": MATERIAL_IDS},
            "required": ["material_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_plan",
        "description": (
            "保存最终方案。仅当预算不超限、周期不超期、且同一组素材已成功生成脚本时才接受；"
            "否则返回拒绝原因与缺口。相同方案重复保存会幂等命中，不重复计费。"
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"material_ids": MATERIAL_IDS},
            "required": ["material_ids"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_SCHEMAS]

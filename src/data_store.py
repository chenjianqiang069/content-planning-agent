"""数据加载层：从 data/ 读取合成素材、工具费用与耗时表、业务规则。

数据以 JSON 文件外置，代码只读取，方便评审核对预算计算与阈值来源。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import Material

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class DataStore:
    """加载并缓存 data/ 下的合成资料与规则。"""

    def __init__(self, data_dir: Path | str = DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self._materials_raw = self._load("materials.json")
        self._tool_costs = self._load("tool_costs.json")
        self.rules = self._load("rules.json")
        self.materials: list[Material] = [
            Material(
                id=m["id"],
                name=m["name"],
                tier=m.get("tier", "economy"),
                cost=float(m["cost"]),
                lead_days=int(m.get("lead_days", 1)),
                quality=int(m.get("quality", 50)),
                tags=list(m.get("tags", [])),
            )
            for m in self._materials_raw.get("materials", [])
        ]
        self.theme_hints: dict[str, dict[str, int]] = self._materials_raw.get(
            "theme_hints", {}
        )
        self._by_id = {m.id: m for m in self.materials}

    def _load(self, name: str) -> dict[str, Any]:
        path = self.data_dir / name
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("_comment", None)
        return data

    # --- materials ---
    def get_material(self, material_id: str) -> Material | None:
        return self._by_id.get(material_id)

    def get_materials(self, ids: list[str]) -> tuple[list[Material], list[str]]:
        """返回 (找到的素材, 未找到的 id)。未知 id 不抛异常，交给工具层报告。"""
        found: list[Material] = []
        unknown: list[str] = []
        for mid in ids:
            m = self._by_id.get(mid)
            if m is None:
                unknown.append(mid)
            else:
                found.append(m)
        return found, unknown

    # --- tool cost table ---
    def tool_spec(self, tool_name: str) -> dict[str, Any]:
        spec = self._tool_costs.get("tools", {}).get(tool_name)
        if spec is None:
            return {"fee": 0.0, "duration_hours": 0.5, "script_days": 0, "billable_to_plan": False}
        return {
            "fee": float(spec.get("fee", 0.0)),
            "duration_hours": float(spec.get("duration_hours", 0.5)),
            "script_days": int(spec.get("script_days", 0)),
            "billable_to_plan": bool(spec.get("billable_to_plan", False)),
            "note": spec.get("note", ""),
        }

    @property
    def script_days(self) -> int:
        return int(self.tool_spec("generate_script").get("script_days", 3))

    # --- rules ---
    @property
    def step_limits(self) -> dict[str, int]:
        limits = self.rules.get("step_limits", {})
        return {
            "max_steps": int(limits.get("max_steps", 12)),
            "max_tool_calls": int(limits.get("max_tool_calls", 16)),
        }

    @property
    def budget_rule(self) -> dict[str, Any]:
        return self.rules.get("budget_rule", {})

    @property
    def deadline_rule(self) -> dict[str, Any]:
        return self.rules.get("deadline_rule", {})

    @property
    def status_codes(self) -> dict[str, str]:
        return self.rules.get("status_codes", {})

    def tool_fee_table_markdown(self) -> str:
        """工具费用与耗时表。

        这是「规则层」信息，放进系统提示；素材目录刻意不放进提示，
        必须由模型通过 search_materials 检索获得 —— 否则模型可以跳过检索、
        凭空算出可行性，工具就失去意义，也无法体现「观察工具结果再决策」。
        """
        lines = ["### 工具费用与耗时表"]
        for name in ("search_materials", "generate_script", "estimate_cost", "save_plan"):
            s = self.tool_spec(name)
            lines.append(
                f"- {name}: fee={s['fee']} 模拟单位, duration={s['duration_hours']}h, "
                f"计入方案成本={'是' if s['billable_to_plan'] else '否'}"
            )
        return "\n".join(lines)

    def material_catalog_markdown(self) -> str:
        """素材目录。仅用于 CLI `list` 与文档展示，不进入系统提示。"""
        lines = ["### 可用素材（cost=模拟单位, lead_days=交付天数, quality=质量分）"]
        for m in self.materials:
            lines.append(
                f"- {m.id} | {m.name} | tier={m.tier} | cost={m.cost} | "
                f"lead_days={m.lead_days} | quality={m.quality} | tags={','.join(m.tags)}"
            )
        return "\n".join(lines)

    def cost_table_markdown(self) -> str:
        """素材目录 + 工具费用表（CLI list / 文档用）。"""
        return self.material_catalog_markdown() + "\n\n" + self.tool_fee_table_markdown()

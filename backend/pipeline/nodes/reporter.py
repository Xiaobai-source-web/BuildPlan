"""节点5b：监督报告生成 — T-13

用 LLM 根据 plan_parts 润色生成 Markdown 监督报告；
LLM 不可用 → 用 plan_assembler.template_report 确定性模板兜底。
"""

import json

from ..base import BaseNode
from ..llm import LLMClient, LLMError
from ..prompts_loader import load
from .docctx import combine
from .plan_assembler import build_parts, display_caliber, template_report, with_caliber


class ReporterNode(BaseNode):
    name = "reporter"
    title = "监督报告生成"

    def __init__(self, llm=None):
        super().__init__()
        self.llm = llm or LLMClient()

    def run(self, ctx):
        parts = ctx.get("plan_parts") or build_parts(ctx)
        self.emit("node_progress", {"node": self.name, "progress": 40,
                                    "message": "调用 LLM 生成监督报告"})
        report = None
        try:
            report = self.llm.chat_text(load("report.txt"),
                                        combine(ctx, json.dumps(parts, ensure_ascii=False)),
                                        temperature=0.4)
            report = report.strip()
        except (LLMError, Exception):
            report = None
        if not report:
            self.emit("node_progress", {"node": self.name, "progress": 70,
                                        "message": "LLM 不可用，使用报告模板"})
            report = template_report(parts)

        # 口径句：无论走 LLM 还是模板，报告里都必须有（审计要看得见）。
        cal = parts.get("display_granularity") or {}
        note = cal.get("note") or display_caliber(
            ctx, len(parts.get("all_tasks_schedule") or []))["note"]
        report = with_caliber(report, note)

        ctx["report"] = report
        pj = ctx.setdefault("plan_json", {})
        if isinstance(pj, dict):
            pj["report"] = report
        self.emit("node_progress", {"node": self.name, "progress": 100,
                                    "message": "报告已生成"})
        self.done_summary = "监督报告已生成（Markdown）"
        return {"report": report}

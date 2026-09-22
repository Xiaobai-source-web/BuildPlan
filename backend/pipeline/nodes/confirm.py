"""人工确认节点 — 报告合并前的 confirm gate

发 confirm_required（含摘要）并阻塞等待 registry；
用户选 N（decision=False）→ 抛 PipelineCancelled → 引擎发 done cancelled。
"""

import uuid

from ..base import BaseNode
from ..engine import PipelineCancelled
from ..events import EV_CONFIRM_REQUIRED
from ..registry import GATE_TIMEOUT_SECONDS


class ConfirmNode(BaseNode):
    name = "confirm"
    title = "人工确认"

    def __init__(self, message="确认生成最终方案？", context=None):
        super().__init__()
        self._message = message
        self._extra_context = context or {}

    def run(self, ctx):
        confirm_id = f"confirm_{getattr(self, '_run_id', 'run')}_{uuid.uuid4().hex[:4]}"
        context = dict(self._extra_context)
        context.setdefault("summary", self._summary(ctx))
        self._registry.register(confirm_id)  # 必须先登记，否则 wait 直接返回 abort
        self.emit(EV_CONFIRM_REQUIRED, {
            "confirm_id": confirm_id,
            "message": self._message,
            "context": context,
        })
        decision = self._registry.wait(
            confirm_id, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)
        if decision.get("decision") is not True:
            raise PipelineCancelled()
        return {}

    @staticmethod
    def _summary(ctx):
        bits = []
        cpm = ctx.get("cpm_result") or {}
        if cpm.get("total_duration_days"):
            bits.append(f"总工期{cpm['total_duration_days']}天")
        cp = cpm.get("critical_path") or []
        if cp:
            bits.append(f"关键路径{len(cp)}任务")
        rd = ctx.get("resource_demand") or {}
        peak = 0
        for t in (rd.get("tasks") or []):
            for rname, q in (t.get("resources") or {}).items():
                if rname in ("普工", "钢筋工", "模板工", "混凝土工", "瓦工", "抹灰工"):
                    peak += q.get("per_day", 0)
        if peak:
            bits.append(f"人工峰值约{peak}人")
        risks = ctx.get("risks") or []
        if risks:
            bits.append(f"主要风险{len(risks)}条")
        return " / ".join(bits) or "（尚无中间结果）"

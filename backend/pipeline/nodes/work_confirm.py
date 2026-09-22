"""工作模式确认节点 —— 进入计划生成前的强制 Y/N 关口

router（LLM 初步判定）之后、extractor 之前：
- 意图为 plan（要生成计划）→ 本节点发 confirm_required **强制**用户确认
  是否启动工作模式。strict=True：终端必须输入 Y 才启动，回车/其他视为取消。
- 意图不是 plan → 不设关口，原样放行（聊天/提问已在 router 分流处理）。

背景修复：原 extractor 会用硬编码关键词 detect_intent 无条件重判并可能
_stop 掐断，把 LLM 已判定为 plan 的正常请求静默丢掉（如："建一栋12层住宅楼…
"这类不含特权词的说法）。这里让人类在进工作模式前拍板，extractor 改为
信任 router 的判定（见 extractor.py）。
"""

import uuid

from ..base import BaseNode
from ..registry import GATE_TIMEOUT_SECONDS
from ..events import EV_CONFIRM_REQUIRED


class WorkConfirmNode(BaseNode):
    name = "work_confirm"
    title = "工作模式确认"

    def run(self, ctx):
        intent = ctx.get("intent")
        if intent != "plan":
            return {}                      # 非计划请求：原样放行，不设确认门
        prompt = str(ctx.get("prompt") or "").strip()

        confirm_id = f"wc_{getattr(self, '_run_id', 'run')}_{uuid.uuid4().hex[:4]}"
        self._registry.register(confirm_id)
        self.emit(EV_CONFIRM_REQUIRED, {
            "confirm_id": confirm_id,
            # 第 34 轮：模式是用户**手选**的，不再有自动识别的说法
            "message": "你现在在【生成计划】模式，要开始编制这份施工进度计划吗？"
                       "[Y/n，回车=取消]",
            "context": {"intent": "plan", "summary": f"项目意图：{prompt[:80]}"},
            "strict": True,                # 终端 ask_confirm 读到 strict → 回车=取消
        })
        decision = self._registry.wait(
            confirm_id, cancel_evt=getattr(self, "_cancel_evt", None), timeout=GATE_TIMEOUT_SECONDS)

        if decision.get("decision") is not True:
            self.done_summary = "用户选择不启动计划生成"
            return {"_stop": "用户选择不启动计划生成（输入 /help 或直接描述项目即可开始）"}
        self.done_summary = "用户确认启动工作模式"
        return {"work_mode_confirmed": True}
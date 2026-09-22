# -*- coding: utf-8 -*-
"""节点级告警必须成为**计划数据的一部分** —— 数据层回归测试（第 42 轮）。

真实故障（本文件的由来）：
  产品跑完 `plan_sample3_after_org_v2`（30 次模型调用、¥0.40）后，
  `meta.boundary_conditions` 里 labor/equipment/materials **全是空的**
  （`peak_total: null`、`[]`、`[]`）；而同一份输入手工复现那次模型调用是成功的
  （`peak_total=120` + 8 个工种 + 4 台设备）。原因：`boundary.py` 那次 `llm.chat_json`
  抛了异常 → 走 `except` → 退回 `_boundary_by_regex`，**只** `emit("warning", {...})`
  一条事件。事件在终端/UI 一闪而过，**计划 JSON 与交付物里一个字都没留**，产物看起来
  完全正常。这是产品级的诚实性缺陷，不是 boundary 一个节点的问题 —— 所以收集点放在
  **引擎**（所有节点 `emit("warning")` 的唯一必经之路），留档落进 `meta`。

本文件钉四件事（对应任务要求的四组断言）：
  ① 引擎：`emit("warning", {...})` 之后 `ctx["node_warnings"]` 有该条；重复发仍只有
     一条且 `count == 2`；**其它事件类型一条都不收**；收集失败绝不影响事件转发；
  ② 组装：`plan_assembler.build_meta` 的 `node_warnings` / `node_warning_count` /
     `model_call_failures` **恒存在**（无告警时是 `[]` / `0`）；
  ③ 判据：一条"模型调用失败"的告警会进 `model_call_failures`，一条普通告警
     （如节拍超区间）**不会**；
  ④ 老计划：真实计划 JSON 里根本没有这些键 → 取不到就等于 `[]`，**不抛异常、不编造**。

运行：python -m pytest backend/tests/test_node_warnings.py -q
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.base import BaseNode                        # noqa: E402
from pipeline.engine import (NODE_WARNINGS_CTX_KEY,       # noqa: E402
                             Pipeline, collect_node_warning)
from pipeline.nodes import plan_assembler as PA           # noqa: E402

#: 真实事故的那份产物（老计划：meta 里没有 node_warnings 等键）
REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_org_v2.json"

#: `boundary.py:921` 的告警原文（本次事故里被静默吞掉的那一条）
BOUNDARY_MSG = "补全边界条件时模型调用失败，已退回关键词兜底（缺失参数不会被补齐）"
BOUNDARY_DETAIL = "LLM HTTP 500: {'error': 'server error'}"
#: `boundary.py:944` 的真实形态：节拍超区间 —— 它**不是**模型调用失败
CADENCE_MSG = "标准层节拍 21.0 天/层超出常见区间（3-15 天），已照原样采用"


def _warn(node, message, detail=""):
    return {"node": node, "message": message, "detail": detail}


class _WarnNode(BaseNode):
    """只发事件、不干别的假节点（写法照 test_progress_feedback.py 的 _Plain）。"""

    name = "warny"
    title = "会报警的节点"

    def __init__(self, events=()):
        BaseNode.__init__(self)
        self.events = list(events)

    def run(self, ctx):
        self.done_summary = "干完了"
        for ev, data in self.events:
            self.emit(ev, data)
        return None


def _run(events, ctx=None):
    """跑一个单节点流水线，返回 (ctx, 收到的事件表)。"""
    ctx = {} if ctx is None else ctx
    seen = []
    pipe = Pipeline(run_id="t")
    pipe.add_node(_WarnNode(events))
    pipe.run(ctx, emit=lambda ev, d: seen.append((ev, d)))
    return ctx, seen


# ══════════════════ ① 引擎：唯一收集点 ══════════════════

class TestEmitCollectsWarnings:
    def test_emit_warning落进ctx且事件照旧转发(self):
        ctx, seen = _run([("warning", _warn("boundary", BOUNDARY_MSG, BOUNDARY_DETAIL))])

        items = ctx[NODE_WARNINGS_CTX_KEY]
        assert isinstance(items, list) and len(items) == 1, items
        it = items[0]
        assert it["node"] == "boundary"
        assert it["message"] == BOUNDARY_MSG
        assert it["detail"] == BOUNDARY_DETAIL
        assert it["count"] == 1, "首次出现必须带 count"
        assert it["at"], "必须记时间（环节/时刻可复核）"

        # 留档是**旁路**：事件必须一字不改地照旧发出去（终端/UI 行为不变）
        assert [d for ev, d in seen if ev == "warning"] == [
            _warn("boundary", BOUNDARY_MSG, BOUNDARY_DETAIL)]

    def test_重复发同一条只有一条且count累加(self):
        same = ("warning", _warn("boundary", BOUNDARY_MSG, BOUNDARY_DETAIL))
        ctx, _ = _run([same, same])

        items = ctx[NODE_WARNINGS_CTX_KEY]
        assert len(items) == 1, "同一条告警不许刷成两条：%r" % (items,)
        assert items[0]["count"] == 2

    def test_同节点不同告警各留一条(self):
        ctx, _ = _run([("warning", _warn("boundary", BOUNDARY_MSG, BOUNDARY_DETAIL)),
                       ("warning", _warn("boundary", CADENCE_MSG, "节拍已照原样采用"))])

        items = ctx[NODE_WARNINGS_CTX_KEY]
        assert len(items) == 2
        assert [i["count"] for i in items] == [1, 1]
        assert [i["message"] for i in items] == [BOUNDARY_MSG, CADENCE_MSG]

    def test_其它事件类型一条都不收(self):
        """`node_progress` 每节点发好几条，收进 ctx 会把它撑爆（且落盘毫无价值）。"""
        ctx, seen = _run([
            ("node_progress", {"node": "warny", "progress": 50, "message": "在跑"}),
            ("node_progress", {"node": "warny", "progress": 100, "message": "跑完了"}),
            ("param_review", {"node": "warny", "pause_id": "p1"}),
            ("info", {"node": "warny", "message": "随便一条"}),
        ])

        assert not ctx.get(NODE_WARNINGS_CTX_KEY), \
            "只有 warning 能进 node_warnings：%r" % (ctx.get(NODE_WARNINGS_CTX_KEY),)
        # 节点自己发的 4 条必须原样转发（引擎自己的 run_plan/node_start/node_done/done
        # 不算在内，这里只看节点发的那几条）
        own = [d for ev, d in seen if ev in ("node_progress", "param_review", "info")]
        assert own == [
            {"node": "warny", "progress": 50, "message": "在跑"},
            {"node": "warny", "progress": 100, "message": "跑完了"},
            {"node": "warny", "pause_id": "p1"},
            {"node": "warny", "message": "随便一条"},
        ], own

    def test_载荷畸形或ctx异常都不许影响流水线(self):
        """留档是旁路：收集失败必须静默吞掉，绝不能让流水线/事件流受影响。"""
        # ① 载荷不是字典
        ctx, seen = _run([("warning", "这不是一个字典")])
        assert not ctx.get(NODE_WARNINGS_CTX_KEY)
        assert [d for ev, d in seen if ev == "warning"] == ["这不是一个字典"]

        # ② ctx 是 None（引擎允许），收集函数直接调用也不许抛
        collect_node_warning(None, {"node": "x", "message": "y"})
        collect_node_warning({}, None)
        collect_node_warning({}, {"node": "x", "message": "y"})

        # ③ ctx 的 get 直接抛异常 → 收集失败，但事件照样转发、流水线照样跑完
        class _BoomDict(dict):
            def get(self, key, default=None):
                raise RuntimeError("boom")

        ctx3, seen3 = _run([("warning", _warn("boundary", BOUNDARY_MSG))],
                           ctx=_BoomDict())
        assert NODE_WARNINGS_CTX_KEY not in ctx3, "收集失败时不许往 ctx 里塞半截数据"
        evs = [ev for ev, _ in seen3]
        assert "warning" in evs, "收集失败绝不能吃掉 warning 事件：%r" % (evs,)
        assert "node_start" in evs and "node_done" in evs and evs[-1] == "done", \
            "收集失败绝不能中断流水线：%r" % (evs,)

    def test_键被别的东西占用时不崩(self):
        """ctx 里已有同名的非列表值（老 ctx / 别的节点写过）→ 就地换新列表，不抛。"""
        ctx = {NODE_WARNINGS_CTX_KEY: "被写坏了"}
        ctx, _ = _run([("warning", _warn("boundary", BOUNDARY_MSG))], ctx=ctx)
        assert isinstance(ctx[NODE_WARNINGS_CTX_KEY], list)
        assert len(ctx[NODE_WARNINGS_CTX_KEY]) == 1


# ══════════════════ ② 组装：meta 里恒存在的三个键 ══════════════════

class TestBuildMetaFields:
    def test_无告警时三个键恒存在且是空列表或零(self):
        for ctx in ({}, {"extracted_params": {"floors": 3}}, {"wbs": {"phases": []}}):
            meta = PA.build_meta(ctx)
            assert meta["node_warnings"] == [], ctx
            assert meta["node_warning_count"] == 0, ctx
            assert meta["model_call_failures"] == [], ctx

    def test_告警原样透传并给出条数(self):
        ctx = {NODE_WARNINGS_CTX_KEY: [
            {"node": "boundary", "message": BOUNDARY_MSG, "detail": BOUNDARY_DETAIL,
             "at": "2026-09-01T10:00:00", "count": 3},
        ]}
        meta = PA.build_meta(ctx)

        assert meta["node_warning_count"] == 1
        got = meta["node_warnings"][0]
        # 原样透传：交付物要的是原文，不是摘要（count/at 也要留给展示层）
        assert got["message"] == BOUNDARY_MSG
        assert got["detail"] == BOUNDARY_DETAIL
        assert got["count"] == 3
        assert got["at"] == "2026-09-01T10:00:00"

    def test_透传的是拷贝不把meta暴露给调用方乱改(self):
        raw = {"node": "boundary", "message": BOUNDARY_MSG, "detail": ""}
        ctx = {NODE_WARNINGS_CTX_KEY: [raw]}
        meta = PA.build_meta(ctx)
        meta["node_warnings"][0]["message"] = "被改了"
        assert raw["message"] == BOUNDARY_MSG


# ══════════════════ ③ 判据：模型调用失败 vs 普通告警 ══════════════════

class TestModelCallFailures:
    def test_模型调用失败被归入而普通告警不会(self):
        cadence = _warn("boundary", CADENCE_MSG, "节拍已照原样采用；若原文不是这个数…")
        ctx = {NODE_WARNINGS_CTX_KEY: [
            dict(_warn("boundary", BOUNDARY_MSG, BOUNDARY_DETAIL), at="t", count=2),
            dict(cadence, at="t", count=1),
        ]}
        meta = PA.build_meta(ctx)

        fails = meta["model_call_failures"]
        assert len(fails) == 1, "节拍超区间是**普通告警**，不许混进模型失败：%r" % (fails,)
        assert fails[0] == {"node": "boundary", "message": BOUNDARY_MSG,
                            "detail": BOUNDARY_DETAIL}, "键必须固定为 node/message/detail"
        # 告警列表本身两条都在（只是汇总里挑出一条）
        assert meta["node_warning_count"] == 2

    def test_判据按内容而非节点名(self):
        """同一个节点两种告警并存、不同节点同一类失败 —— 判据只能按内容。"""
        # ① 非 boundary 节点的模型失败，照样要认（否则以后新节点必漏）
        # ② boundary 的节拍告警，不许因为"是 boundary 发的"就被误判成模型失败
        ctx = {NODE_WARNINGS_CTX_KEY: [
            _warn("reporter", "LLM 不可用，使用报告模板", "未配置 QWEN_API_KEY"),
            _warn("boundary", CADENCE_MSG, ""),
        ]}
        fails = PA.build_meta(ctx)["model_call_failures"]
        assert [f["node"] for f in fails] == ["reporter"], fails

    def test_真实失败措辞逐条都能认(self):
        """每条字面量都取自本仓真实文案（出处见 MODEL_FAILURE_MARKERS 注释）。"""
        real = [
            "补全边界条件时模型调用失败，已退回关键词兜底（缺失参数不会被补齐）",
            "模型不可用，改用关键词补条件",
            "模型没答上来，改用关键词识别",
            "LLM 不可用（未配置 QWEN_API_KEY），已用规则解析",
            "LLM 调用失败：所有尝试都失败",
            "LLM HTTP 500: {'error': 'server error'}",
            "LLM 响应结构异常：'choices'",
            "LLM 输出不是合法 JSON：Expecting value",
            "未配置 QWEN_API_KEY（见 backend/.env.example）",
        ]
        for text in real:
            fails = PA.model_call_failures_of([_warn("x", text, "")])
            assert len(fails) == 1, "这条必须被认成模型调用失败：%r" % (text,)
        # 空白/畸形载荷不许抛，也不许当成模型失败
        assert PA.model_call_failures_of([None, "x", {}, _warn("x", "", "")]) == []

    def test_失败原文一字不改地进汇总(self):
        weird = "LLM HTTP 502: 网关错误 <html>中文</html>"
        fails = PA.model_call_failures_of([_warn("kb_scope", BOUNDARY_MSG, weird)])
        assert fails[0]["detail"] == weird


# ══════════════════ ④ 老计划：缺键等于没有告警，绝不崩 ══════════════════

class TestLegacyPlan:
    def test_老计划缺键取不到就是空列表不抛异常(self):
        plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
        meta = plan["meta"]

        # 这份真实产物确实**没有**这些键（本缺陷之所以是产品级缺陷的实证）
        assert "node_warnings" not in meta
        assert "model_call_failures" not in meta

        assert PA._as_warning_list(meta) == []
        assert PA.model_call_failures_of(PA._as_warning_list(meta)) == []
        # 缺键的 meta 当 ctx 直接调 build_meta（交付/修订链的真实调用形态）也不许抛
        rebuilt = PA.build_meta(meta)
        assert rebuilt["node_warnings"] == []
        assert rebuilt["node_warning_count"] == 0
        assert rebuilt["model_call_failures"] == []

    def test_各种畸形来源一律空列表(self):
        for bad in (None, {}, [], "x", 0,
                    {"node_warnings": "不是列表"},
                    {"node_warnings": None},
                    {"node_warnings": [1, "x", [], None]}):
            assert PA._as_warning_list(bad) == [], bad

    def test_列表里的非字典项跳过而不是丢掉整份(self):
        got = PA._as_warning_list({"node_warnings": [1, {"node": "a"}, None]})
        assert got == [{"node": "a"}]

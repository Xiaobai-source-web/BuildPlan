# -*- coding: utf-8 -*-
"""第 43 轮回归：模型**答了、但资源类边界全空** → 必须留痕。

【第 2 批 · 域 2 / 2.6】材料清单已整体删除 → 判据从"三类全空"收窄为**两类**
（劳动力 / 设备）。下面引用的历史取证原文保留（当年现场确有 `materials = []`）。

被修的缺陷（真实产物实证，`plans/plan_sample3_after_org_v2.json`）：
  `meta.boundary_conditions` 里 `labor = {"peak_total": null, "by_trade": []}`、
  `equipment = []`、当年还有 `materials = []` —— **全是空的**，而同一次返回里的
  `project_duration_days = 420` 有值、`cadence_days = 7`（原文正则）也在。

为什么能断定"模型答了"而不是"抛异常退回正则兜底"：
  · `boundary._boundary_by_regex()` 只会造 `equipment`
    （且只在原文有「塔吊N」触发词时；【W3-C】它原本还会造 `labor.peak_total`，
     那条"正则抠数冒充用户申报"的路径已按用户 2026-09-21 裁定删除）—— 全仓没有第三处写
    `labor.by_trade` / `project_duration_days`（当年还有 `materials`）；
  · 真实产物里 `project_duration_days`（420）/ `labor.by_trade`（空列表）
    **都存在** ⇒ 只可能来自 `if llm_out:` 分支的模型返回。

缺陷的要害不是"值空"，而是**一句话都没有**：这条路径原来连一条 warning 都不发，
于是"本次根本没拿到劳动力/设备清单"在计划 JSON 与交付物里完全不可见 ——
用户看到设备对账表空着，会以为"我没有申报设备"。

本轮修法（只动 `backend/pipeline/nodes/boundary.py`）：
  ① 命中时 `emit("warning", {node, message, detail})`，经引擎
     `collect_node_warning`（`engine.py:49-93`，唯一收集点 `engine.py:218`）落进
     `ctx["node_warnings"]`，再由 `plan_assembler.build_meta` 透传进
     `meta["node_warnings"]` / `meta["node_warning_count"]`；
  ② 同一句人话落进 `boundary["_empty_resources_note"]`（可机读，展示层直接引用）；
  ③ 该键以 `_` 开头且在 `_BOUNDARY_META_KEYS` 里，**不许**进 `condition_keys()` 的计数。

文案硬约束（都有断言）：
  · **不许**说成"用户未申报" —— 成因是模型这次没填，甩锅给用户是错的；
  · **不许**命中 `plan_assembler.MODEL_FAILURE_MARKERS` —— 否则
    `meta.model_call_failures` 会把"模型答了但留空"误报成"模型这条路没走通"。

运行：python -m pytest backend/tests/test_boundary_empty_warning.py -q -p no:cacheprovider
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from pipeline.engine import Pipeline  # noqa: E402
from pipeline.nodes.boundary import (  # noqa: E402
    BoundaryNode, EMPTY_RESOURCES_DETAIL, EMPTY_RESOURCES_MESSAGE,
    EMPTY_RESOURCES_NOTE_KEY, SOURCE_KEYS, annotate_sources, condition_keys,
    resources_all_empty)
from pipeline.nodes.plan_assembler import (  # noqa: E402
    MODEL_FAILURE_MARKERS, build_meta)

# ── 真实产物 `plan_sample3_after_org_v2.json` 的 `meta.boundary_conditions`（原样）──────
# labor/equipment 两类全空，但结构键一个不少、工期 420 有值 ——
# 这正是"模型答了、只是资源那几项留了空"的指纹。
# 【第 2 批 · 域 2 / 2.6】当年现场还有 `materials = []` 一项，材料清单已整体删除
# → 判据收窄为**两类**（劳动力 / 设备），这里也照新契约去掉该键。
REAL_EMPTY_BC = {
    "labor": {"peak_total": None, "by_trade": []},
    "equipment": [],
    "project_duration_days": 420,
}

FULL_BC = {
    "labor": {"peak_total": 120, "by_trade": [
        {"trade": "钢筋工", "quantity": 25, "unit": "人"},
        {"trade": "木工", "quantity": 20, "unit": "人"}]},
    "equipment": [{"name": "塔吊", "quantity": 1, "unit": "台"}],
    "project_duration_days": 420,
}

# 任意**一类**有值就是"不算全空"（判据从严：宁可少报）
# 【第 2 批 · 域 2 / 2.6】原「只给了材料」一档已删除：材料清单不再是资源类边界条件的一类。
_ONE_CLASS_FILLED = {
    "只给了设备": {"labor": {"peak_total": None, "by_trade": []},
                   "equipment": [{"name": "塔吊", "quantity": 1, "unit": "台"}]},
    "只给了峰值人数": {"labor": {"peak_total": 120, "by_trade": []},
                       "equipment": []},
    "只给了工种": {"labor": {"peak_total": None,
                             "by_trade": [{"trade": "钢筋工", "quantity": 25}]},
                   "equipment": []},
}


class _StubLLM(object):
    """假 LLM（与 test_boundary_provenance.py 同形）：chat_json 直接返回预置结构。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system, user, temperature=0.3, retries=1):
        self.calls += 1
        return self.payload


class _BoomLLM(object):
    def chat_json(self, *a, **k):
        raise RuntimeError("模型不可用")


def _ctx():
    # 原文没有节拍句、没有「塔吊N」触发词（【W3-C】「总劳动力峰值N」那条正则已删）：
    # 于是 warnings 里除了本轮这条以外不该有别的东西。
    return {"extracted_params": {}, "prompt": "某住宅楼工程",
            "doc_content": "工期要求：420日历天"}


def _run_node(llm, ctx=None):
    """跑一次 BoundaryNode（手工注入 _emit，收全量事件），返回 (node, ctx, events)。"""
    ctx = ctx if ctx is not None else _ctx()
    events = []
    node = BoundaryNode(llm=llm)
    node._emit = lambda event, data: events.append((event, data))
    node.run(ctx)
    return node, ctx, events


def _warnings(events):
    return [d for e, d in events if e == "warning"]


def _empty_resource_warnings(events):
    return [w for w in _warnings(events) if w.get("message") == EMPTY_RESOURCES_MESSAGE]


# ══════════════════════════════════════════════════════════════════
# 1. 判据本身（两类全空 vs 任意一类有值）
# ══════════════════════════════════════════════════════════════════
class TestResourcesAllEmpty:
    def test_真实产物的结构判为全空(self):
        assert resources_all_empty(REAL_EMPTY_BC) is True

    def test_完全空字典也判全空(self):
        """模型连 `boundary_conditions` 键都没给（`or {}`）→ 同样"没拿到清单"。"""
        assert resources_all_empty({}) is True

    def test_任意一类有值就不算全空(self):
        for name, bc in _ONE_CLASS_FILLED.items():
            assert resources_all_empty(bc) is False, name

    def test_完整值不算全空(self):
        assert resources_all_empty(FULL_BC) is False

    def test_结构写坏按空处理(self):
        """看不懂 = 没有值（与 `annotate_sources` 的"宁可标 model"同向）。"""
        assert resources_all_empty(None) is True
        assert resources_all_empty([]) is True
        assert resources_all_empty({"labor": "???"}) is True
        assert resources_all_empty({"labor": {"peak_total": "暂无"}}) is True
        assert resources_all_empty({"equipment": [None, "", {}]}) is True
        # 有真条目就不算空
        assert resources_all_empty({"equipment": [{"name": "塔吊"}]}) is False


# ══════════════════════════════════════════════════════════════════
# 2. 节点行为：发告警 + 落可机读标记
# ══════════════════════════════════════════════════════════════════
class TestEmptyResourcesWarning:
    def test_两类全空必须发告警(self):
        node, ctx, events = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        hits = _empty_resource_warnings(events)
        assert len(hits) == 1, _warnings(events)
        assert hits[0]["node"] == "boundary"

    def test_告警原文人话且可行动(self):
        _, _, events = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        w = _empty_resource_warnings(events)[0]
        msg, det = w["message"], w["detail"]

        # message：说清"模型答了结构、两类全空" + 后果（设备对账 / 工种限额）
        assert "模型返回了边界条件结构" in msg, msg
        assert "劳动力/设备" in msg, msg
        assert "全是空的" in msg, msg
        assert "设备对账" in msg and "工种限额" in msg, msg
        # 【第 2 批 · 域 2 / 2.6】材料清单已删 → 文案里**不许**再提"材料口径"
        # （否则与交付物上「本计划不含材料计划」自相矛盾）
        assert "材料" not in msg, msg
        # detail：说清"不是用户没给，是模型这次没填" + 可操作建议
        assert "不是用户没给" in det, det
        assert "而是模型这一次没有填" in det, det
        assert "在参数门补一句" in det, det

    def test_不许甩锅给用户(self):
        """硬约束：不许说成"用户未申报"（成因是模型没填）。"""
        _, _, events = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        w = _empty_resource_warnings(events)[0]
        text = w["message"] + w["detail"]
        assert "用户未申报" not in text, text
        assert "未申报" not in text, text
        # 反过来必须是"替用户澄清"的写法
        assert "不是用户没给" in text, text

    def test_不许被误判成模型调用失败(self):
        """文案不许命中 MODEL_FAILURE_MARKERS，否则 meta.model_call_failures 会误报。"""
        _, _, events = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        w = _empty_resource_warnings(events)[0]
        text = w["message"] + " " + w["detail"]
        for marker in MODEL_FAILURE_MARKERS:
            assert marker not in text, (marker, text)

    def test_可机读标记落进boundary(self):
        node, ctx, events = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        bc = ctx["boundary_conditions"]
        assert EMPTY_RESOURCES_NOTE_KEY in bc, bc
        assert bc[EMPTY_RESOURCES_NOTE_KEY] == EMPTY_RESOURCES_MESSAGE
        # 展示层直接引用本键即可，两处文案必须同一句
        assert bc[EMPTY_RESOURCES_NOTE_KEY] == _empty_resource_warnings(events)[0]["message"]
        # 标记要活过 apply_cadence / annotate_sources（都在它之后跑）
        assert bc["_source_note"] and set(bc["_source"]) == set(SOURCE_KEYS)

    def test_模型没给boundary_conditions键同样发告警(self):
        _, ctx, events = _run_node(_StubLLM({"project_name": "某住宅楼"}))
        assert len(_empty_resource_warnings(events)) == 1
        assert ctx["boundary_conditions"][EMPTY_RESOURCES_NOTE_KEY] == EMPTY_RESOURCES_MESSAGE

    def test_同一个节点同一处只发一条(self):
        """只发一条 warning（不是每个资源键各发一条）。"""
        _, _, events = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        assert len(_warnings(events)) == 1, _warnings(events)

    @pytest.mark.parametrize("name", sorted(_ONE_CLASS_FILLED))
    def test_任意一类有值就不发这条告警(self, name):
        _, ctx, events = _run_node(
            _StubLLM({"boundary_conditions": dict(_ONE_CLASS_FILLED[name])}))
        assert _empty_resource_warnings(events) == [], (name, _warnings(events))
        assert EMPTY_RESOURCES_NOTE_KEY not in ctx["boundary_conditions"], name

    def test_完整值不发这条告警(self):
        _, ctx, events = _run_node(_StubLLM({"boundary_conditions": dict(FULL_BC)}))
        assert _empty_resource_warnings(events) == [], _warnings(events)
        assert EMPTY_RESOURCES_NOTE_KEY not in ctx["boundary_conditions"]

    def test_模型抛异常不重复发新告警(self):
        """抛异常走的是既有兜底路径（那里本来就没有 llm_out）→ 只发既有那条。

        【W3-C】原文那句「总劳动力峰值 929 人」**不再被正则抠出来**冒充用户申报
        （用户 2026-09-21 裁定；见 `test_boundary_provenance.py::
        test_正则兜底不再从文本里抠出申报峰值`）—— 所以这里 `labor.peak_total`
        是空的。本用例只关心"告警条数不许重复"，判据不受影响。
        """
        ctx = {"extracted_params": {},
               "prompt": "本工程总劳动力峰值 929 人，塔吊 2 台，工期要求 420 日历天"}
        _, ctx, events = _run_node(_BoomLLM(), ctx=ctx)
        hints = _warnings(events)
        assert len(hints) == 1, hints
        assert "补全边界条件时模型调用失败" in hints[0]["message"], hints[0]
        assert "退回关键词兜底" in hints[0]["message"], hints[0]
        # 新告警一个字都不许出现（既没有 llm_out，也就无从谈起"模型答了但全空"）
        assert _empty_resource_warnings(events) == []
        assert EMPTY_RESOURCES_NOTE_KEY not in ctx["boundary_conditions"]

    def test_模型抛异常且兜底也没捞到值仍不发新告警(self):
        """**门控回归**：兜底路径同样"两类全空"（正则一个触发词都没捞到）时，
        新告警也不许发 —— 判据只在 `if llm_out:` 分支里，否则会凭空多一条。"""
        ctx = {"extracted_params": {}, "prompt": "某住宅楼工程，工期要求 420 日历天",
               "doc_content": "剪力墙结构，地上18层"}
        _, ctx, events = _run_node(_BoomLLM(), ctx=ctx)
        hints = _warnings(events)
        assert len(hints) == 1, hints
        assert "补全边界条件时模型调用失败" in hints[0]["message"], hints[0]
        # 兜底确实什么都没捞到（两类全空），但新告警依然不许出现
        assert resources_all_empty(ctx["boundary_conditions"]) is True
        assert _empty_resource_warnings(events) == []
        assert EMPTY_RESOURCES_NOTE_KEY not in ctx["boundary_conditions"]


# ══════════════════════════════════════════════════════════════════
# 3. 新键不许破坏 `condition_keys()` 的语义（"N 项"不许虚增）
# ══════════════════════════════════════════════════════════════════
class TestConditionKeysUnaffected:
    def test_新标记不算一项边界条件(self):
        bc = {"labor": {"peak_total": None, "by_trade": []}, "equipment": [],
              "project_duration_days": 420,
              EMPTY_RESOURCES_NOTE_KEY: EMPTY_RESOURCES_MESSAGE,
              "_source": {}, "_source_note": "x"}
        assert condition_keys(bc) == ["labor", "equipment",
                                      "project_duration_days"]
        assert EMPTY_RESOURCES_NOTE_KEY not in condition_keys(bc)

    def test_标注后计数与手工排除一致(self):
        """修前修后同口径：`condition_keys` == "不含下划线开头的键"。"""
        bc = annotate_sources(dict(REAL_EMPTY_BC), {}, {})
        bc[EMPTY_RESOURCES_NOTE_KEY] = EMPTY_RESOURCES_MESSAGE
        assert condition_keys(bc) == [k for k in bc if not str(k).startswith("_")]

    def test_done_summary的N项不含新标记(self):
        node, ctx, _ = _run_node(_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)}))
        bc = ctx["boundary_conditions"]
        n = len(condition_keys(bc))
        # 【第 2 批 · 域 2 / 2.6】3 个模型返回的键（labor / equipment /
        # project_duration_days —— materials 已被节点剔除）+ 施工节拍 2 键
        # （apply_cadence 恒写）= 5；标记不计
        assert n == 5, (n, condition_keys(bc))
        assert f"（{n} 项" in node.done_summary, node.done_summary
        assert f"（{n + 1} 项" not in node.done_summary, node.done_summary


# ══════════════════════════════════════════════════════════════════
# 4. 经真实 Pipeline：这条告警最终必须出现在 meta.node_warnings
# ══════════════════════════════════════════════════════════════════
class TestReachesMetaNodeWarnings:
    def _run_pipeline(self):
        events = []
        ctx = _ctx()
        pipe = Pipeline(run_id="t43_empty_resources")
        pipe.add_node(BoundaryNode(llm=_StubLLM({"boundary_conditions": dict(REAL_EMPTY_BC)})))
        pipe.run(ctx, emit=lambda event, data: events.append((event, data)))
        return ctx, events

    def test_真实流水线把warning收进ctx(self):
        """`engine._emit_with_warning_log` → `collect_node_warning`（engine.py:218 唯一收集点）。"""
        ctx, events = self._run_pipeline()
        # ① 事件确实发出去了（终端/UI 看得到）
        assert _empty_resource_warnings(events), events
        # ② 同时落进 ctx["node_warnings"]（引擎侧留档）
        warns = ctx.get("node_warnings")
        assert isinstance(warns, list) and warns, warns
        hits = [w for w in warns if w.get("message") == EMPTY_RESOURCES_MESSAGE]
        assert len(hits) == 1, warns
        assert hits[0]["node"] == "boundary"
        assert hits[0]["detail"] == EMPTY_RESOURCES_DETAIL
        assert hits[0]["count"] == 1
        assert hits[0].get("at")

    def test_最终出现在meta_node_warnings里(self):
        """**本轮要求的证据**：`plan_assembler.build_meta(ctx)` 的 `node_warnings` 含这条。"""
        ctx, _ = self._run_pipeline()
        meta = build_meta(ctx)
        assert meta["node_warnings"], meta
        msgs = [w.get("message") for w in meta["node_warnings"]]
        assert EMPTY_RESOURCES_MESSAGE in msgs, msgs
        assert meta["node_warning_count"] == len(meta["node_warnings"]) == 1
        # 与"模型调用失败"是两个概念：不许被投影进 model_call_failures
        assert meta["model_call_failures"] == [], meta["model_call_failures"]
        # 计划数据侧一样看得见（展示层可直接引用）
        assert meta["boundary_conditions"][EMPTY_RESOURCES_NOTE_KEY] == EMPTY_RESOURCES_MESSAGE

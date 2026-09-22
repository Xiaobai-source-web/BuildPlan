# -*- coding: utf-8 -*-
"""第 41 轮回归：**施工节拍**（标准层 N 天/层）从用户原文到 `boundary_conditions`。

被修的缺陷（用户实测反馈）：
  `项目样例\\示例3_住宅楼.txt` 第 14 行明写「- 主体：剪力墙结构，标准层7天一层」，
  但整份产物里 **"7天" 一次都没出现** —— 主体被排成 34 天/层、全项目 2958 天。
  根因不是模型看不见，而是**没人要求它抽**：提示词的输出 JSON 里根本没有这一项，
  边界节点送料又只送 `doc_summary`（模型自写摘要 / 原文前 900 字），
  那句节拍既不在摘要里、也不在提示词的字段表里 —— 用户给的节拍等于白给。

契约（跨节点，键名不可改）：
  ctx["boundary_conditions"]["cadence_days"]   float | None，单位 **天/层**
  ctx["boundary_conditions"]["cadence_scope"]  str，默认 "标准层"
  两者都进 `_source`（`cadence_days` / `cadence_scope` 两键，取值只能是 user/model），
  组织层按"`cadence_days` 是正数"生效，取不到就退回旧行为。

判据从严（宁可漏也不错取 —— 漏了退回旧行为，错取会把全项目工期带偏）：
  必须**带"层"的计量语义**（N天/层、N天一层、每层N天、一层N天、标准层/主体…N天），
  子句级排除非主体分项（地下室/装修/桩基…），总工期口径（"420日历天"）不算节拍，
  住宅常见区间 3~15 天/层之外**照取不阻断**但必须留备注。

运行：python -m pytest backend/tests/test_cadence_extraction.py -q -p no:cacheprovider
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from pipeline.events import EV_PARAM_REVIEW  # noqa: E402
from pipeline.nodes.boundary import (BoundaryNode,  # noqa: E402
                                     CADENCE_ABSURD_DAYS, CADENCE_COMMON_MAX,
                                     CADENCE_COMMON_MIN, CADENCE_DAYS_KEY,
                                     CADENCE_NOTE_KEY, CADENCE_SCOPE_KEY,
                                     DEFAULT_CADENCE_SCOPE, SOURCE_KEYS,
                                     SITE_MACHINE_CONST_KEY,
                                     annotate_sources, apply_cadence,
                                     boundary_haystack, boundary_sources,
                                     cadence_gate_note, condition_keys,
                                     extract_cadence)
from pipeline.nodes.param_review import (ParamReviewNode,  # noqa: E402
                                         _gate_message)

SAMPLE3 = ROOT / "项目样例" / "示例3_住宅楼.txt"

# 示例3 原文的要害片段（"7天"之外的数字全是噪声：420 日历天、89/120㎡、120 根管桩）
SAMPLE3_KERNEL = """项目名称：某住宅楼工程
建筑规模：地下1层，地上18层
建筑面积：14200平方米
结构形式：剪力墙结构
工期要求：420日历天
户型组成：89㎡和120㎡两种户型
基础形式：预应力管桩基础，约120根管桩
- 主体：剪力墙结构，标准层7天一层
"""


class _StubReg(object):
    """最小交互登记桩（与 test_param_abort.py 同形）：wait 直接返回预置决策。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.registered = []
        self.waits = 0

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=600):
        idx = min(self.waits, len(self.decisions) - 1)
        self.waits += 1
        if not self.decisions:
            return {"action": "abort"}
        dec = self.decisions[idx]
        return dict(dec) if isinstance(dec, dict) else {"action": "abort"}


# ══════════════════════════════════════════════════════════════════
# 1. 八种写法都要认出来（回归：示例3 的写法只是其中一种）
# ══════════════════════════════════════════════════════════════════
class TestExtractCadence:
    @pytest.mark.parametrize("text,days", [
        ("- 主体：剪力墙结构，标准层7天一层", 7),      # 示例3 原文写法
        ("标准层节拍 7 天/层", 7),
        ("标准层七天一层", 7),
        ("每层7天", 7),
        ("一层7天", 7),
        ("主体节拍7天", 7),
        ("5天一层", 5),
        ("6天/层", 6),
    ])
    def test_八种写法都能认出来(self, text, days):
        det = extract_cadence(text)
        assert det, "没认出节拍：%r" % text
        assert det["cadence_days"] == days, (text, det)
        assert det["cadence_scope"], det

    def test_示例3真实原文能提取到7天(self):
        """**bug 复现测试**：修前这份原文在整条流水线里一个"7天"都没留下。"""
        text = SAMPLE3.read_text(encoding="utf-8")
        assert "标准层7天一层" in text, "样例文件变了，请核对第 14 行"
        det = extract_cadence(text)
        assert det, "示例3 原文没提取到节拍"
        assert det["cadence_days"] == 7, det
        assert det["cadence_scope"] == "标准层", det

    # ------------------------------------------------------------------
    # 非主体表述不许当成标准层节拍（误取会把主体节拍算成别的数）
    # ------------------------------------------------------------------
    @pytest.mark.parametrize("text", [
        "地下室30天",
        "地下室每层30天",
        "装修每层10天",
        "桩基施工每层5天",
        "工期要求：420日历天",
    ])
    def test_非主体表述不算节拍(self, text):
        assert extract_cadence(text) == {}, text

    def test_地下室与主体同段时取主体(self):
        """实测文本常写「地下室：每层5天；主体：每层7天」—— 排除词只在子句内生效。"""
        det = extract_cadence("地下室：每层5天；主体：每层7天")
        assert det["cadence_days"] == 7, det
        assert det["cadence_scope"] == "主体结构", det

    def test_明写标准层时排除词不掐掉它(self):
        """「标准层」是用户明说的口径 → 强证据（同句出现"地下室"也照认）。"""
        det = extract_cadence("标准层7天一层（含地下室顶板）")
        assert det and det["cadence_days"] == 7, det

    def test_只写天数没有层的语义不认(self):
        """「总工期420天」是工期不是节拍 —— 没有"层"的计量语义一律不认。"""
        assert extract_cadence("总工期 420 天") == {}
        assert extract_cadence("") == {}
        assert extract_cadence(None) == {}

    def test_超过一年一层不认(self):
        """0 < N ≤ 365 之外不认（一年一层显然不是节拍）。"""
        assert extract_cadence("标准层%d天一层" % (CADENCE_ABSURD_DAYS + 35)) == {}


# ══════════════════════════════════════════════════════════════════
# 2. 落点契约：两键恒在、类型固定、区间外照取但留备注
# ══════════════════════════════════════════════════════════════════
class TestApplyCadence:
    def test_用户原文优先且写成float(self):
        bc, info = apply_cadence({}, {"doc_content": SAMPLE3_KERNEL}, {})
        assert bc[CADENCE_DAYS_KEY] == 7.0
        assert isinstance(bc[CADENCE_DAYS_KEY], float)
        assert bc[CADENCE_SCOPE_KEY] == "标准层"
        assert info["source"] == "user" and info["matched"]
        assert CADENCE_NOTE_KEY not in bc          # 7 天在常见区间内 → 不写备注

    def test_原文没写就取模型值但标model(self):
        bc, info = apply_cadence({"cadence_days": 8, "cadence_scope": "主体结构"},
                                 {"doc_content": "工期要求：420日历天"}, {})
        assert bc[CADENCE_DAYS_KEY] == 8.0
        assert bc[CADENCE_SCOPE_KEY] == "主体结构"
        assert info["source"] == "model"
        src = boundary_sources(bc, boundary_haystack(
            {"doc_content": "工期要求：420日历天"}, {}))
        assert src[CADENCE_DAYS_KEY] == "model", "模型估的节拍不许标成用户给的"

    def test_原文优先于模型值(self):
        """模型给 10、原文写 7 → 取原文 7（用户明确给出的数值为准）。"""
        bc, info = apply_cadence({"cadence_days": 10}, {"doc_content": "标准层7天一层"}, {})
        assert bc[CADENCE_DAYS_KEY] == 7.0 and info["source"] == "user"

    @pytest.mark.parametrize("raw", [
        {}, None, [], "???", 0,
        {"cadence_days": -1}, {"cadence_days": "不是数"}, {"cadence_days": True},
        {"cadence_days": float("nan")}, {"cadence_days": 9999},
        {"cadence_days": "abc", "cadence_scope": ""},
        # 第 41 轮补：负/零/无穷/科学计数法**一律拒**（见下面两条专门用例）
        {"cadence_days": 0}, {"cadence_days": "0"}, {"cadence_days": 0.0},
        {"cadence_days": float("inf")}, {"cadence_days": float("-inf")},
        {"cadence_days": "inf"}, {"cadence_days": "1e9"}, {"cadence_days": "1e10"},
        {"cadence_days": 400}, {"cadence_days": "400"}, {"cadence_days": 366},
    ])
    def test_畸形输入下两键恒在且取值封闭(self, raw):
        bc, _info = apply_cadence(raw, {}, {})
        assert CADENCE_DAYS_KEY in bc and CADENCE_SCOPE_KEY in bc, raw
        assert bc[CADENCE_DAYS_KEY] is None, (raw, bc)
        assert bc[CADENCE_SCOPE_KEY] == DEFAULT_CADENCE_SCOPE, (raw, bc)

    @pytest.mark.parametrize("raw,why", [
        (-1, "负号：`_norm_number(-1)` 会吃掉负号返回 \"1\" → -1 变 1 天/层"),
        (0, "0 天/层同样会驱动排期（除零 / 0 层天）"),
        ("0", "字符串零：`0 < N` 判据必须挡住"),
        (float("inf"), "inf：会让工期公式算出 0 天或 inf"),
        (float("-inf"), "-inf：同上，且负号还会被吃掉"),
        ("1e9", "科学计数法：`_norm_number(\"1e9\")` 只截到 \"1\" → 1e9 变 1 天/层"),
        ("1e10", "同上，量级更离谱"),
        (400, ">365 天/层不是节拍（判据④），照收会让一层排 400 天"),
        (366, "判据④的边界外侧"),
    ])
    def test_离谱数值一律拒而不是静默生效(self, raw, why):
        """**本轮最危险的一类错**：离谱值静默变成一个"看似合理"的数并驱动全项目排期。

        `-1`→1、`"1e9"`→1 都出自 `_norm_number` 的"从文本里找第一个数"语义；
        节拍这里必须先按**纯数值**解析拿到真实量级，再由 `0 < N ≤ 365` 判据拒掉。
        拒 = 落 `None`，下游退回旧行为（不启用节拍组织施工），不会静默重排工期。
        """
        bc, info = apply_cadence({"cadence_days": raw}, {"doc_content": ""}, {})
        assert bc[CADENCE_DAYS_KEY] is None, (why, bc)
        assert info["days"] is None and info["source"] != "model", (why, info)
        src = boundary_sources(bc, "")
        assert set(src) == set(SOURCE_KEYS), (why, src)
        assert set(src.values()) <= {"user", "model"}, (why, src)

    def test_判据四的边界在365(self):
        """≤365 收下（区间外照取+留痕）；>365 拒 —— 与用户原文路径同一条线。"""
        bc, _ = apply_cadence({"cadence_days": 365}, {"doc_content": ""}, {})
        assert bc[CADENCE_DAYS_KEY] == 365.0 and bc.get(CADENCE_NOTE_KEY), bc
        bc2, _ = apply_cadence({"cadence_days": 366}, {"doc_content": ""}, {})
        assert bc2[CADENCE_DAYS_KEY] is None, bc2
        # 用户原文路径同一条线：366 天/层不认，365 天/层认（并因区间外留备注）
        assert extract_cadence("标准层366天一层") == {}
        det = extract_cadence("标准层365天一层")
        assert det and det["cadence_days"] == 365.0, det

    def test_带单位的中文写法仍然认(self):
        """收严解析不许误伤既有写法："7天" / "每层7天" 这类还要能认。"""
        for raw, want in ((7, 7.0), ("7", 7.0), ("7天", 7.0), ("每层7天", 7.0),
                          ("7.5", 7.5), ("1,200", None), ("7天/层", 7.0)):
            bc, _ = apply_cadence({"cadence_days": raw}, {"doc_content": ""}, {})
            assert bc[CADENCE_DAYS_KEY] == want, (raw, bc)

    def test_原文没提节拍时写着null而不是编一个(self):
        """全项目工期被一个编出来的节拍重排，是这轮最贵的错误 —— 宁可为 null。"""
        bc, info = apply_cadence({"project_duration_days": 420},
                                 {"doc_content": "工期要求：420日历天"}, {})
        assert bc[CADENCE_DAYS_KEY] is None
        assert info["source"] == "none" and info["days"] is None

    @pytest.mark.parametrize("days", [2, 20])
    def test_区间外照取不阻断但必须留备注(self, days):
        bc, info = apply_cadence({}, {"doc_content": "标准层%d天一层" % days}, {})
        assert bc[CADENCE_DAYS_KEY] == float(days), "不许因为可疑就丢掉用户的数"
        assert info["warning"], info
        note = bc[CADENCE_NOTE_KEY]
        assert ("%g" % CADENCE_COMMON_MIN) in note, note
        assert ("%g" % CADENCE_COMMON_MAX) in note, note

    def test_区间内不写备注(self):
        for days in (CADENCE_COMMON_MIN, 7, CADENCE_COMMON_MAX):
            bc, _ = apply_cadence({}, {"doc_content": "标准层%g天一层" % days}, {})
            assert CADENCE_NOTE_KEY not in bc, days

    def test_手动补充里写的节拍也算用户给的(self):
        """用户在参数门手输「标准层7天一层」→ 必须认（否则白填）。"""
        ctx = {"_manual_param_input": "标准层7天一层"}
        bc, info = apply_cadence({}, ctx, {})
        assert bc[CADENCE_DAYS_KEY] == 7.0 and info["source"] == "user"


# ══════════════════════════════════════════════════════════════════
# 3. `_source` 口径：数字 + 语义锚点**双条件**，纯数字"7"不许冒充用户节拍
# ══════════════════════════════════════════════════════════════════
class TestCadenceSource:
    def test_source键集由SOURCE_KEYS声明且含节拍两键(self):
        # 【第 2 批 · 域 2 / 2.6】`materials` 已删除 → 7 键变 6 键
        # （第 40 轮的 4 键 + 第 41 轮的施工节拍 2 键）。
        # 【第 2 批 · 域 7.7】`site_machine_const` 加进来 → 6 键变 **7 键**：
        # 它是**项目级常量块**，有自己的来源（user / AI 估算），按"恒有、取值封闭"的
        # 既有纪律必须登记进 `SOURCE_KEYS`（来源留痕）；但它是元数据，**不进**
        # `MODEL_DECLARED_KEYS`（登记了就会被 `strip_model_declared()` 清掉），
        # 也进 `_BOUNDARY_META_KEYS`（不算成"第 N 项边界条件"，计数不变）。
        assert len(SOURCE_KEYS) == 7, SOURCE_KEYS
        assert SITE_MACHINE_CONST_KEY in SOURCE_KEYS, SOURCE_KEYS
        assert CADENCE_DAYS_KEY in SOURCE_KEYS and CADENCE_SCOPE_KEY in SOURCE_KEYS
        assert "materials" not in SOURCE_KEYS, SOURCE_KEYS
        src = boundary_sources({}, "")
        assert set(src) == set(SOURCE_KEYS), src

    def test_原文有节拍句且数值对得上才是user(self):
        hay = boundary_haystack({"doc_content": SAMPLE3_KERNEL}, {})
        src = boundary_sources({"cadence_days": 7, "cadence_scope": "标准层"}, hay)
        assert src[CADENCE_DAYS_KEY] == "user", src
        assert src[CADENCE_SCOPE_KEY] == "user", src

    def test_数值对不上就是model(self):
        hay = boundary_haystack({"doc_content": "标准层7天一层"}, {})
        src = boundary_sources({"cadence_days": 10, "cadence_scope": "标准层"}, hay)
        assert src[CADENCE_DAYS_KEY] == "model", src

    def test_纯数字7不许冒充用户节拍(self):
        """原文里数字齐全（420/120/89）但没有节拍句 → 两键必须 model。

        这正是"纯数字匹配把模型编的值当用户依据"的老毛病，节拍这里不许重犯。
        """
        hay = boundary_haystack(
            {"doc_content": "工期要求：420日历天；约120根管桩；89㎡和120㎡两种户型"}, {})
        src = boundary_sources({"cadence_days": 7, "cadence_scope": "标准层"}, hay)
        assert src[CADENCE_DAYS_KEY] == "model", src
        assert src[CADENCE_SCOPE_KEY] == "model", src

    def test_原文没写但模型硬给了也是model(self):
        src = boundary_sources({"cadence_days": 7, "cadence_scope": "标准层"},
                               boundary_haystack({"doc_content": ""}, {}))
        assert src[CADENCE_DAYS_KEY] == "model" and src[CADENCE_SCOPE_KEY] == "model"

    def test_只写7天每层时口径算model(self):
        """原文只写「主体结构 7天/层」，没说是标准层 —— 口径是本节点按契约默认补的。"""
        hay = boundary_haystack({"doc_content": "主体结构 7天/层"}, {})
        src = boundary_sources({"cadence_days": 7, "cadence_scope": DEFAULT_CADENCE_SCOPE}, hay)
        assert src[CADENCE_DAYS_KEY] == "user", src
        assert src[CADENCE_SCOPE_KEY] == "model", src

    @pytest.mark.parametrize("raw", [
        {}, None, [], "???",
        {"cadence_days": -3}, {"cadence_days": "不是数"}, {"cadence_days": True},
        {"cadence_scope": 123}, {"cadence_days": 7, "cadence_scope": None},
    ])
    def test_畸形输入下七键恒在且取值封闭(self, raw):
        bc = annotate_sources(raw, {}, {})
        src = bc["_source"]
        assert set(src) == set(SOURCE_KEYS), (raw, src)
        assert set(src.values()) <= {"user", "model"}, (raw, src)
        assert src[CADENCE_DAYS_KEY] in ("user", "model")
        assert src[CADENCE_SCOPE_KEY] in ("user", "model")

    def test_节拍两键是真边界条件而非元数据(self):
        """`_source` / `_source_note` 是元数据（不计入"N 项"）；节拍两键是**真条件**。

        必须当真条件：下游组织层就是按 `boundary_conditions["cadence_days"]` 生效的，
        把它藏进下划线元数据里，等于让"有没有节拍"变成没人看得见的状态。
        """
        bc = annotate_sources({"cadence_days": 7, "cadence_scope": "标准层"}, {}, {})
        keys = condition_keys(bc)
        assert "_source" not in keys and "_source_note" not in keys
        assert set(keys) == {"cadence_days", "cadence_scope"}, keys


# ══════════════════════════════════════════════════════════════════
# 4. 节点级：真的写进 ctx（示例3 的 7 天必须出现在产物里）
# ══════════════════════════════════════════════════════════════════
class _StubLLM(object):
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, system, user, temperature=0.3, retries=1):
        return self.payload


class _BoomLLM(object):
    def chat_json(self, system, user, temperature=0.3, retries=1):
        raise RuntimeError("模型不可用（离线测试）")


class TestBoundaryNodeCadence:
    def test_节点把节拍写进ctx并标成用户来源(self):
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        ctx = {"doc_content": SAMPLE3_KERNEL, "extracted_params": {}, "prompt": ""}
        node.run(ctx)

        bc = ctx["boundary_conditions"]
        assert bc[CADENCE_DAYS_KEY] == 7.0, bc
        assert bc[CADENCE_SCOPE_KEY] == "标准层", bc
        assert bc["_source"][CADENCE_DAYS_KEY] == "user", bc["_source"]
        assert bc["_source"][CADENCE_SCOPE_KEY] == "user", bc["_source"]
        assert "7" in node.done_summary, node.done_summary

    def test_模型给的值会被原文顶掉(self):
        node = BoundaryNode(llm=_StubLLM(
            {"boundary_conditions": {"cadence_days": 30, "cadence_scope": "地下室"}}))
        ctx = {"doc_content": "标准层7天一层", "extracted_params": {}, "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[CADENCE_DAYS_KEY] == 7.0, bc
        assert bc["_source"][CADENCE_DAYS_KEY] == "user", bc["_source"]

    def test_模型不可用走正则兜底时照样提取节拍(self):
        """兜底路径不许"少一条判据" —— 节拍提取本来就是确定性的。"""
        node = BoundaryNode(llm=_BoomLLM())
        ctx = {"doc_content": "标准层6天一层", "extracted_params": {},
               "prompt": "标准层6天一层"}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[CADENCE_DAYS_KEY] == 6.0, bc
        assert bc["_source"][CADENCE_DAYS_KEY] == "user", bc["_source"]

    def test_没写节拍时节点不编数(self):
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        ctx = {"doc_content": "工期要求：420日历天", "extracted_params": {}, "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[CADENCE_DAYS_KEY] is None, bc
        assert bc[CADENCE_SCOPE_KEY] == DEFAULT_CADENCE_SCOPE, bc
        assert "未检测到施工节拍" in node.done_summary, node.done_summary

    def test_区间外节拍发warning事件但不中止(self):
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        events = []
        node._emit = lambda event, data: events.append((event, data))
        ctx = {"doc_content": "标准层2天一层", "extracted_params": {}, "prompt": ""}
        out = node.run(ctx)
        assert out == {}, "不许因为节拍可疑就中止整条流水线"
        warns = [d for e, d in events if e == "warning"]
        assert any("节拍" in (d.get("message") or "") for d in warns), events
        assert ctx["boundary_conditions"][CADENCE_DAYS_KEY] == 2.0

    def test_示例3真实文件端到端(self):
        node = BoundaryNode(llm=_StubLLM({"boundary_conditions": {}}))
        ctx = {"doc_content": SAMPLE3.read_text(encoding="utf-8"),
               "extracted_params": {}, "prompt": ""}
        node.run(ctx)
        bc = ctx["boundary_conditions"]
        assert bc[CADENCE_DAYS_KEY] == 7.0, bc
        assert bc["_source"][CADENCE_DAYS_KEY] == "user", bc["_source"]


# ══════════════════════════════════════════════════════════════════
# 5. 参数门回显：用户必须**看得见**节拍被收到 / 没被收到
# ══════════════════════════════════════════════════════════════════
def _cadence_note_for(doc, manual=None, params=None):
    ctx = {"doc_content": doc}
    if manual:
        ctx["_manual_param_input"] = manual
    return cadence_gate_note(ctx, params or {})


class TestCadenceGateEcho:
    def test_检测到就回显天数与口径(self):
        note = _cadence_note_for("主体：剪力墙结构，标准层7天一层")
        assert "检测到" in note and "7" in note, note
        assert "标准层" in note, note

    def test_没检测到就明说不会启用并给出补法(self):
        note = _cadence_note_for("工期要求：420日历天；约120根管桩")
        assert "未检测到" in note, note
        assert "标准层7天一层" in note, "必须告诉用户怎么补：" + note

    def test_门提示带上节拍回显(self):
        note = _cadence_note_for("标准层7天一层")
        msg = _gate_message({"ok": True, "missing_default": ["floors"]}, 1, note)
        assert "7" in msg and "检测到" in msg, msg

    def test_老签名两个位置参数继续可用(self):
        """`test_ui_naming.py:63/73` 就是这么调的 —— 新增参数必须有默认值。"""
        msg = _gate_message({"ok": False, "missing_required": ["building_count"],
                             "note": "缺关键参数"}, 1)
        assert "参数" in msg and msg

    def test_门事件载荷带上节拍回显(self):
        node = ParamReviewNode()
        events = []
        node._emit = lambda event, data: events.append((event, data))
        node._run_id = "t_cad"
        node._registry = _StubReg([{"action": "abort"}])
        ctx = {"extracted_params": {"building_count": 12, "floors": 38,
                                    "total_area": 215000, "total_concrete": 82000,
                                    "planned_start_date": "2025-04-16"},
               "doc_content": SAMPLE3_KERNEL}
        node.run(ctx)

        payloads = [d for e, d in events if e == EV_PARAM_REVIEW]
        assert payloads, events
        assert "检测到" in payloads[0]["message"], payloads[0]["message"]
        assert "7" in payloads[0]["message"], payloads[0]["message"]

    def test_门上补的节拍下一轮立刻生效(self):
        node = ParamReviewNode()
        events = []
        node._emit = lambda event, data: events.append((event, data))
        node._run_id = "t_cad2"
        node._registry = _StubReg([
            {"passed": False, "manual_input": "标准层5天一层"},   # 第 1 轮：用户现补节拍
            {"action": "abort"},
        ])
        ctx = {"extracted_params": {}, "doc_content": "工期要求：420日历天"}
        node.run(ctx)

        payloads = [d for e, d in events if e == EV_PARAM_REVIEW]
        assert len(payloads) >= 2, events
        assert "未检测到" in payloads[0]["message"], payloads[0]["message"]
        assert "5" in payloads[1]["message"], payloads[1]["message"]


# ══════════════════════════════════════════════════════════════════
# 6. 提示词真源（backend/prompts/boundary_conditions.txt）
# ══════════════════════════════════════════════════════════════════
class TestPromptDeclaresCadence:
    def test_提示词里有节拍字段与口径(self):
        text = (BACKEND / "prompts" / "boundary_conditions.txt").read_text(encoding="utf-8")
        assert "cadence_days" in text, "输出 JSON 里没有节拍字段 = 模型不会抽（本轮根因）"
        assert "cadence_scope" in text
        assert "天/层" in text
        assert "null" in text, "必须写明「提都没提就填 null」"

    def test_提示词写明不估与排除口径(self):
        text = (BACKEND / "prompts" / "boundary_conditions.txt").read_text(encoding="utf-8")
        assert "不许按常识估" in text or "不要估" in text, text[:200]
        for word in ("地下室", "装修", "总工期"):
            assert word in text, "排除口径缺 %s（否则模型会把非主体天数当节拍）" % word


class TestCadenceBadPrefix:
    """判据⑦（父代理独立复核后补）：用户原文里的**负号 / 科学计数法**不许被当节拍。

    实测三种写法会把"离谱值"静默变成合法节拍，而且都标 `_source=user`
    （最高权威、直接驱动排期）：
      · `标准层-1天一层` → 抓到 `1天一层` → **1 天/层**（负号被当分隔符）
      · `-1天一层`       → 同上
      · `1e9天一层`      → 抓到 `9天一层` → **9 天/层**（把指数里的 9 当节拍）
    这三种都在提取阶段拒（返回 `{}`），下游才不会拿它去组织施工。
    """

    @pytest.mark.parametrize("text", [
        "标准层-1天一层",
        "-1天一层",
        "主体标准层-3天一层",
        "标准层1e9天一层",
        "1e9天一层",
        "标准层1E7天一层",
        "标准层0天一层",
        "标准层366天一层",
    ])
    def test_符号与指数里的数字不算节拍(self, text):
        assert extract_cadence(text) == {}, "离谱值被当成节拍了：%r" % text

    @pytest.mark.parametrize("text,days", [
        ("1. 标准层7天一层", 7.0),            # 序号后的小点不许误伤（小点在"1"后，不在"7"前）
        ("（三）主体：标准层7天一层", 7.0),
        ("标高-1层：主体每层7天", 7.0),        # 负号在别处、节拍数字前面是"层"
        ("标准层7.5天一层", 7.5),             # 小数
        ("标准层：7天一层", 7.0),             # 冒号是合法分隔符
        ("每层7天", 7.0),
        ("标准层七天一层", 7.0),              # 中文数字
    ])
    def test_合法写法不被误伤(self, text, days):
        det = extract_cadence(text)
        assert det.get("cadence_days") == days, det


# -*- coding: utf-8 -*-
"""【域 6｜桩基 = 基础】验收护栏（6.1 / 6.2 / 6.3 / 6.4）。

用户已冻结的裁决（不得违反，逐字引用见 `pipeline/ratio_scope.py` 的
「【域 6｜桩基 = 基础】基础类型 → L4 绑定」一节）：
  · 「**不改名**，应该判断出来该项目是什么类型基础，然后所有该项目相关的基础工程量
    **都走占比表中的基础那一栏**。钢筋和模板也按这个方向处理。」
  · 「基础类型从参数中提取，应该新加一个键为基础类型……不同基础类型对应着可能有
    不同的工序」（其中「没有的话就自行推断」已被后续裁决**取代** → 提取不到就报错）
  · 口径总表第 10 条：「**基础 = 桩基**（当项目基础类型为桩基）；占比表"基础"栏是
    **通用栏目**，不因项目改表」
  · A2 用户裁定：**不新建映射表** —— 基础类型 → L4 的绑定用**代码里的常量映射**实现

本文件按四项逐条钉住（每项都有一节）：
  6.1 判断项目基础类型（从参数提取；提取不到 → 报错）—— 由 `boundary.py` 的参数门负责
  6.2 基础量走占比表「基础」栏 → 绑定项目基础类型 → 分到实际基础类型的 L4
  6.3 钢筋、模板同样处理（三个工种走同一段逻辑，不特判混凝土）
  6.4 占比项清单 = 基础、梁、板、柱、墙、楼梯…（「桩基」不是独立项，它就是「基础」）

运行：python -m pytest backend/tests/test_domain6_foundation_binding.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from pipeline import kb as KB  # noqa: E402
from pipeline import ratio_scope as RS  # noqa: E402
from pipeline.nodes import boundary as B  # noqa: E402
from pipeline.nodes import kb_scope as KS  # noqa: E402

SID = "frame_shear"

#: 真实项目参数（用户给的演示数据）。`total_pile` 只在需要的用例里加。
BASE = {"total_area": 215000, "floors": 38, "total_concrete": 8000,
        "total_rebar": 1200, "total_formwork": 25000}


# ══════════════════════════════════════════════════════════════════
# 6.1 判断项目基础类型（从参数提取；提取不到 → 报错）
# ══════════════════════════════════════════════════════════════════
class Test61FoundationTypeExtracted:
    """6.1 的「报错」部分**已由参数门实现**（第 2 批），这里只**确认**、不重复实现。"""

    def test_基础类型是硬必要且连试算也不放行(self):
        assert "foundation_type" in B.REQUIRED_KEYS, B.REQUIRED_KEYS
        assert "foundation_type" in B.ABSOLUTE_KEYS, B.ABSOLUTE_KEYS
        comp = B.params_completeness({"floors": 18, "total_area": 14200,
                                      "structure_type": "框架-剪力墙结构"})
        assert comp["ok"] is False
        assert comp["missing_absolute"] == ["foundation_type"], comp["missing_absolute"]

    def test_ratio_scope只读参数不自己兜底(self):
        """本模块**不许**把"缺参"悄悄变成某一种基础形式（报错是参数门的职责）。"""
        assert RS.foundation_type_of({}) == ""
        assert RS.foundation_type_of(None) == ""
        assert RS.foundation_type_of({"foundation_type": "  筏板基础  "}) == "筏板基础"
        # 缺参时既不认作桩基、也不认作任何具体形式
        kind, targets, mode, resolved = RS.foundation_l4_targets("")
        assert kind == RS.FOUNDATION_KIND_UNKNOWN
        assert targets == () and mode == RS.CONSERVATION_INPLACE and resolved is False

    def test_认不出基础形式时留痕且不改投(self):
        r = RS.build(dict(BASE, foundation_type="天然地基"), SID)
        fb = r["trace"]["foundation_binding"]
        assert fb["kind"] == RS.FOUNDATION_KIND_UNKNOWN
        assert fb["applied"] is False
        # 量**一点没动**（与完全没给 foundation_type 时逐键相同）
        assert r["l4_quantities"] == RS.build(dict(BASE), SID)["l4_quantities"]
        codes = {d["code"] for d in r["trace"]["degradations"]}
        assert "foundation_type_unresolved" in codes, codes
        warns = RS.foundation_binding_warnings(r)
        assert warns and "天然地基" in warns[0], warns

    def test_基础类型词表覆盖extractor的封闭表(self):
        """`extractor._FOUNDATION_TYPES`（确定性兜底词表）里每个标准名都必须能判出档。"""
        from pipeline.nodes.extractor import _FOUNDATION_TYPES
        for name in _FOUNDATION_TYPES:
            kind, targets, mode, _res = RS.foundation_l4_targets(name)
            assert kind in ("pile", "mat", "spread", "strip", "box"), (name, kind)
            assert targets, name


# ══════════════════════════════════════════════════════════════════
# 6.2 基础量走占比表「基础」栏 → 绑定基础类型 → 分到实际基础类型的 L4
# ══════════════════════════════════════════════════════════════════
class Test62FoundationColumnBinding:
    def test_占比表不改名_锚点恒为三条通用构件行(self):
        """「不改名」= 表结构与占比值不为任何基础类型特化。"""
        assert RS.FOUNDATION_ANCHORS == {"concrete": ("CONC_NEW_FOUND",),
                                         "rebar": ("REBAR_NEW_FOUND",),
                                         "formwork": ("FORM_NEW_FOUND",)}
        # 真实库：这三行在 7 种结构类型下都存在，且组内 ∑ = 100（表没被特化）
        for sid in ("frame", "shear_wall", "frame_shear", "masonry_conc", "steel", "bent", "tube"):
            got = {r["activity_id"]: r["ratio_percent"]
                   for r in RS.ratio_rows_for_structure(sid)}
            for aid in ("CONC_NEW_FOUND", "REBAR_NEW_FOUND", "FORM_NEW_FOUND"):
                assert got.get(aid) is not None, (sid, aid)

    def test_非桩基础落点不变(self):
        """筏板/独立/条形/箱形 ⇒ 「基础」栏的量落在**通用基础构件**上，一点不搬。"""
        base = RS.build(dict(BASE), SID)["l4_quantities"]
        for ft in ("筏板基础", "筏形基础", "独立基础", "独立柱基", "条形基础",
                   "箱形基础", "满堂基础", "杯口基础"):
            r = RS.build(dict(BASE, foundation_type=ft), SID)
            assert r["l4_quantities"] == base, ft
            fb = r["trace"]["foundation_binding"]
            assert fb["applied"] is True and fb["conservation"] == RS.CONSERVATION_INPLACE
            assert "CONC_NEW_FOUND" in fb["target_l4"], (ft, fb)

    @pytest.mark.parametrize("foundation, want_l4", [
        ("预应力管桩", "GD_A13_打管桩"),
        ("预制管桩", "GD_A13_打管桩"),
        ("PHC管桩", "GD_A13_打管桩"),
        ("预制方桩", "GD_A13_打方桩"),
        ("钢管桩", "GD_A13_打钢管桩"),
        ("钻孔灌注桩", "GD_A13_钻孔成孔"),
        ("冲孔灌注桩", "GD_A13_冲孔成孔"),
        ("旋挖桩", "GD_A13_旋挖成孔"),
        ("沉管灌注桩", "GD_A13_沉管灌注成孔"),
        ("CFG桩", "GD_A13_CFG桩成孔"),
        ("砂石桩", "GD_A13_砂石灌注桩"),
        ("微型桩", "GD_A13_钻孔灌注微型桩"),
        ("打圆木桩", "GD_A13_打圆木桩"),
    ])
    def test_桩型映射到对应桩基L4(self, foundation, want_l4):
        """桩型 → L4 是**代码内常量映射**（`PILE_TYPE_TARGETS`），不许建表。"""
        _kind, targets, mode, resolved = RS.foundation_l4_targets(foundation)
        assert targets == (want_l4,), (foundation, targets)
        assert mode == RS.CONSERVATION_MIGRATE
        assert resolved is True
        # 目标 L4 必须真的在 KB 的桩基组里（防手写错 ID）
        assert KB.activity_info(want_l4) is not None, want_l4

    def test_纯桩基_量改投且锚点归零_不重复计量(self):
        r = RS.build(dict(BASE, foundation_type="预应力管桩"), SID)
        q = r["l4_quantities"]
        # 「基础」栏混凝土量 8000×17.1% = 1368 → 改投到桩基主工序
        assert q["GD_A13_打管桩"] == pytest.approx(8000 * 0.171)
        assert q["CONC_NEW_FOUND"] == 0.0, "纯桩基下通用基础构件必须归零（不重复计量）"
        assert q["REBAR_NEW_FOUND"] == 0.0
        assert q["FORM_NEW_FOUND"] == 0.0
        # 改投必须留痕：量去哪了、从哪来
        moved = {e["activity_id"]: e for e in r["trace"]["excluded_zero_quantity"]
                 if e.get("kind") == "ratio_migrated_to_foundation"}
        assert set(moved) == {"CONC_NEW_FOUND", "REBAR_NEW_FOUND", "FORM_NEW_FOUND"}
        assert moved["CONC_NEW_FOUND"]["moved_to"] == "GD_A13_打管桩"
        assert moved["CONC_NEW_FOUND"]["moved_quantity"] == pytest.approx(8000 * 0.171)
        # index 里也要看得出绑定关系（下游 / 交付物可查）
        assert r["index"]["GD_A13_打管桩"]["foundation_binding"]["status"] == \
            RS.BINDING_FROM_RATIO
        fb = r["index"]["GD_A13_打管桩"]["foundation_binding"]
        assert fb["binder_work_type_id"] == "concrete", fb
        assert fb["target_work_type_id"] == "pile_foundation", fb

    def test_桩筏基础_桩与筏板并存_如实标注不守恒(self):
        r = RS.build(dict(BASE, foundation_type="桩筏基础"), SID)
        q = r["l4_quantities"]
        # 筏板（通用「基础」栏）**保留**（它本来真的是混凝土量）
        assert q["CONC_NEW_FOUND"] == pytest.approx(8000 * 0.171)
        assert q["REBAR_NEW_FOUND"] == pytest.approx(1200 * 0.164)
        assert q["FORM_NEW_FOUND"] == pytest.approx(25000 * 0.241)
        # 桩量另算（同一份量再落一次桩基 L4）
        assert q["GD_A13_钻孔成孔"] == pytest.approx(8000 * 0.171)
        codes = {d["code"] for d in r["trace"]["degradations"]}
        assert "foundation_conservation_coexist" in codes, codes

    def test_桩型认不出时兜底且留痕(self):
        r = RS.build(dict(BASE, foundation_type="桩基础"), SID)
        fb = r["trace"]["foundation_binding"]
        assert fb["pile_target"] == RS.GENERIC_PILE_TARGET
        assert fb["pile_type_resolved"] is False
        codes = {d["code"] for d in r["trace"]["degradations"]}
        assert "pile_type_unresolved" in codes
        assert RS.GENERIC_PILE_TARGET in r["l4_quantities"]

    def test_用户给的桩总量作为直接量被消费(self):
        """用户给了 `total_pile` ⇒ 该量本身就是桩量，**不再乘占比**。"""
        r = RS.build(dict(BASE, foundation_type="预应力管桩", total_pile=320), SID)
        assert r["l4_quantities"]["GD_A13_打管桩"] == pytest.approx(320.0)
        fb = r["index"]["GD_A13_打管桩"]["foundation_binding"]
        assert fb["status"] == RS.BINDING_FROM_USER_PARAM
        assert fb["binder_param"] == "total_pile"
        assert fb["binder_ratio_percent"] == pytest.approx(17.1)   # 只作留痕
        assert fb["binder_ratio_applied"] is False, "直接量不许再乘一遍占比"
        assert r["index"]["GD_A13_打管桩"]["ratio_percent"] == 0.0

    def test_多栋项目按栋数折单栋(self):
        r = RS.build(dict(BASE, foundation_type="预应力管桩", building_count=12), SID)
        assert r["l4_quantities"]["GD_A13_打管桩"] == pytest.approx(8000 * 0.171 / 12)
        assert r["index"]["GD_A13_打管桩"]["building_count"] == 12


# ══════════════════════════════════════════════════════════════════
# 6.3 钢筋、模板同样处理
# ══════════════════════════════════════════════════════════════════
class Test63RebarAndFormworkSameTreatment:
    def test_三个工种走同一段逻辑(self):
        """「钢筋和模板也按这个方向处理」⇒ 不是只给混凝土开小灶。"""
        r = RS.build(dict(BASE, foundation_type="预应力管桩"), SID)
        charged = {c["work_type_id"]: c for c in
                   r["trace"]["foundation_binding"]["charged"]}
        assert set(charged) == {"concrete", "rebar", "formwork"}, charged
        # 三个工种的绑定参数分别来自各自的总量键（不是都挂 total_concrete）
        assert charged["concrete"]["binder_param"] == "total_concrete"
        assert charged["rebar"]["binder_param"] == "total_rebar"
        assert charged["formwork"]["binder_param"] == "total_formwork"
        # 每个工种的「基础」栏占比都用了本工种的锚点行
        assert charged["concrete"]["binder_ratio_percent"] == pytest.approx(17.1)
        assert charged["rebar"]["binder_ratio_percent"] == pytest.approx(16.4)
        assert charged["formwork"]["binder_ratio_percent"] == pytest.approx(24.1)

    def test_一个L4只接一个量纲(self):
        """桩基 L4 只有一个量纲：三份不同量纲的量不许累加成假数。"""
        r = RS.build(dict(BASE, foundation_type="预应力管桩"), SID)
        # 只有混凝土那一份进了 GD_A13_打管桩（1368 m³），不是 1368+196.8+6025
        assert r["l4_quantities"]["GD_A13_打管桩"] == pytest.approx(8000 * 0.171)
        codes = [d["code"] for d in r["trace"]["degradations"]]
        assert codes.count("foundation_l4_already_claimed") == 2, codes
        # 两个被跳过的工种必须如实标 status，不许假装成功
        skipped = [c for c in r["trace"]["foundation_binding"]["charged"]
                   if c["status"] == "skipped_claimed"]
        assert {c["work_type_id"] for c in skipped} == {"rebar", "formwork"}

    def test_缺某工种总量时其余工种照常(self):
        p = {"total_area": 215000, "floors": 38, "total_concrete": 8000,
             "foundation_type": "预应力管桩"}
        r = RS.build(p, SID)
        charged = {c["work_type_id"] for c in r["trace"]["foundation_binding"]["charged"]}
        assert charged == {"concrete"}, charged
        assert r["l4_quantities"]["GD_A13_打管桩"] == pytest.approx(8000 * 0.171)


# ══════════════════════════════════════════════════════════════════
# 6.4 占比项清单 = 基础、梁、板、柱、墙、楼梯…（「桩基」不是独立项）
# ══════════════════════════════════════════════════════════════════
class Test64PileIsNotAnIndependentItem:
    def test_桩基已从占比拆分工种表移出(self):
        """`GROUP_TOTAL_PARAMS` = **参与占比表拆分**的工种；桩基不在其中。"""
        assert "pile_foundation" not in RS.GROUP_TOTAL_PARAMS, RS.GROUP_TOTAL_PARAMS
        assert set(RS.GROUP_TOTAL_PARAMS) == {"concrete", "rebar", "formwork",
                                             "masonry", "earthwork"}
        # 反证：真实库的 `Component_Ratio` 里 `pile_foundation` 0 行 ——
        # 留在表里只会让桩基 33 个 L4 全部被判「异常缺行」
        rows = RS.ratio_rows_for_structure(SID)
        l3 = RS.l4_l3_map()
        assert not [r for r in rows if l3.get(r["activity_id"]) == "pile_foundation"]

    def test_占比项清单就是构件清单(self):
        """「占比项清单 = 基础、梁、板、柱、墙、楼梯…」：真库 97 行全是构件行。"""
        names = []
        for sid in ("frame", "shear_wall", "frame_shear", "masonry_conc", "steel", "bent", "tube"):
            for r in RS.ratio_rows_for_structure(sid):
                info = KB.activity_info(r["activity_id"])
                nm = (info or {}).get("activity_name") or ""
                if nm:
                    names.append(nm)
        blob = "".join(names)
        for part in ("基础", "梁", "板", "柱", "墙", "楼梯"):
            assert part in blob, part
        # 「桩基」不许作为**占比项**出现（它是「基础」栏的一种，不是独立项）
        assert "桩基" not in blob, "占比表里不许有独立的桩基项"

    def test_桩基没有占比行但也不被当异常缺行(self):
        """6.4 的可观测后果：桩基 L4 不会因「表里没有该行」被记成 `abnormal_absent`。"""
        r = RS.build(dict(BASE, foundation_type="筏板基础"), SID)
        abnormal = {e["activity_id"] for e in r["trace"]["excluded_zero_quantity"]
                    if e.get("kind") == "abnormal_absent"}
        assert not any(a.startswith("GD_A13") for a in abnormal), abnormal
        # 而且 `pile_foundation` 不在「活跃工种」里（它没有 total_* 参与占比拆分）
        assert "pile_foundation" not in r["trace"]["active_work_types"]

    def test_用户的桩总量仍然强化桩基工种(self):
        """6.4 只改「占比表拆分」，**不改**「用户给量 → L3 强化为必须」。"""
        strengthened = KS._quantity_strengthened_l3({"total_pile": 320})
        assert strengthened.get("pile_foundation") == ("total_pile", 320), strengthened


# ══════════════════════════════════════════════════════════════════
# 接线：kb_scope 把基础类型绑定结果带进 scope / 警告通道
# ══════════════════════════════════════════════════════════════════
class TestKbScopeWiring:
    def _run(self, extra):
        node = KS.KBScopeNode()
        node._emit = lambda e, d: None
        params = {"total_area": 215000, "floors": 38, "total_concrete": 8000,
                  "total_rebar": 1200, "total_formwork": 25000,
                  "building_type": "住宅", "structure_type": "框架-剪力墙"}
        params.update(extra)
        ctx = {"extracted_params": params}
        out = node.run(ctx)
        return ctx, out

    def test_桩基项目_scope里有绑定留痕且量已改投(self):
        ctx, out = self._run({"foundation_type": "预应力管桩"})
        trace = out["kb_scope"]["component_ratio"]
        assert trace["foundation_binding"]["pile_target"] == "GD_A13_打管桩"
        assert "GD_A13_打管桩" in ctx["extracted_params"]["l4_quantities"]
        assert ctx["extracted_params"]["l4_quantities"]["CONC_NEW_FOUND"] == 0.0

    def test_认不出桩型时警告能到用户面前(self):
        ctx, out = self._run({"foundation_type": "桩基础"})
        warned = " ".join(out["kb_scope"]["warnings"])
        assert "桩基" in warned and "GD_A13_钻孔成孔" in warned, warned
        assert out["kb_warnings"], "警告必须随节点上行"

    def test_认不出基础形式时警告能到用户面前(self):
        _ctx, out = self._run({"foundation_type": "天然地基"})
        warned = " ".join(out["kb_scope"]["warnings"])
        assert "天然地基" in warned, warned

    def test_正常筏板基础不产生基础类型警告(self):
        _ctx, out = self._run({"foundation_type": "筏板基础"})
        warned = " ".join(out["kb_scope"]["warnings"])
        assert "基础形式" not in warned and "桩型" not in warned, warned


# ══════════════════════════════════════════════════════════════════
# 溯源纪律：不许建表、不许改 prompts 措辞里的常量
# ══════════════════════════════════════════════════════════════════
class TestProvenanceDiscipline:
    def test_不新建kb表(self):
        """A2 用户裁定：基础类型 → L4 的绑定用**代码里的常量映射**，不许在 kb.db 加表。

        `len(names) == 20` 是"表数一个都没多"的显式锚：域 1.6（第 6 批）删掉了
        `Workface_Capacity_Rule`（21 → 20 张用户表），本用例只跟这个数走，
        语义（**不新建表**）不变。
        """
        names = {r[0] for r in KB._query_all(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for forbidden in ("Foundation_Type_L4_Mapping", "Foundation_Binding",
                          "Foundation_Type_Map", "Pile_Type_L4_Mapping"):
            assert forbidden not in names, forbidden
        assert len(names) == 20, sorted(names)

    def test_绑定常量都可溯源(self):
        """每个桩型目标 L4 都必须真的存在，且映射写在模块常量里（可 grep）。"""
        src = (BACKEND / "pipeline" / "ratio_scope.py").read_text(encoding="utf-8")
        assert "PILE_TYPE_TARGETS" in src and "FOUNDATION_TYPE_MAP" in src
        assert "FOUNDATION_L4_BINDING" in src
        assert "不建表" in src or "不许在 kb.db 里加表" in src
        for _keywords, aid in RS.PILE_TYPE_TARGETS:
            info = KB.activity_info(aid)
            assert info is not None, aid
            assert RS.l4_l3_map().get(aid) == "pile_foundation", (aid, info)
        assert KB.activity_info(RS.GENERIC_PILE_TARGET) is not None

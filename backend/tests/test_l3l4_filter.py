"""A 组映射链筛选测试（总清单 F3 指定的 `test_l3l4_filter.py`）。

覆盖：
  · **A1** 映射三档化：`USUAL` → `OPTIONAL` 的兼容映射、代码侧不再产出 `USUAL`；
  · **A4** 「L3 被排除 → 其下 L4 一律不进」的与逻辑 + 整棵子树剔除的审计留痕
    （`kb_scope.excluded_subtrees` → `kb_conformance.scope_audit` / `metadata`）；
  · **A5** L4「该不该单独成工序」通用校验：R1 内置名称判据、R2 只能来自 KB 判据列、
    列不存在时**跳过并留痕**；
  · **A6** 用户输入参与筛选：L3 档位强化、量 = 0 的 L4 不进树、
    明确排除项（全局 / 局部 / 定位不了 三态）、层面积字典抽取与校验回退。

依赖 BuildPlan_KB/kb.db（仓库随附，只读；本文件不改库）。
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import kb  # noqa: E402
from pipeline import scope_inputs as si  # noqa: E402
from pipeline.nodes import kb_conformance as kc  # noqa: E402
from pipeline.nodes import kb_scope as ks  # noqa: E402
from pipeline.nodes.kb_scope import KBScopeNode  # noqa: E402


# ==================== 夹具 ====================
def _run(building="住宅", structure="剪力墙结构", extra=None):
    """跑一次 kb_scope；`extra` 直接并进 extracted_params。"""
    params = {"building_type": building, "structure_type": structure}
    params.update(extra or {})
    node = KBScopeNode()
    node._emit = lambda *a, **k: None
    return node.run({"extracted_params": params})["kb_scope"]


# ======================================================================
# A1 · 映射三档化
# ======================================================================
def test_normalize_level_merges_legacy_usual_into_optional():
    """历史 `USUAL` 必须映射成 `OPTIONAL`；其余值原样大写。"""
    assert ks.normalize_level("USUAL") == "OPTIONAL"
    assert ks.normalize_level("usual") == "OPTIONAL"
    assert ks.normalize_level(" REQUIRED ") == "REQUIRED"
    assert ks.normalize_level("OPTIONAL") == "OPTIONAL"
    assert ks.normalize_level("EXCLUDED") == "EXCLUDED"
    assert ks.normalize_level(None) == ""
    assert ks.normalize_level("") == ""


def test_keep_levels_only_three_tiers():
    """代码侧只认三档（收下两档 + 排除一档），不许再出现 USUAL。"""
    assert ks._KEEP_LEVELS == ("REQUIRED", "OPTIONAL")
    assert "USUAL" not in ks._KEEP_LEVELS
    assert ks.LEVEL_ALIASES == {"USUAL": "OPTIONAL"}


def test_kb_scope_never_emits_usual():
    """节点产出的档位只有 REQUIRED / OPTIONAL / UNKNOWN（不再有 USUAL）。"""
    scope = _run()
    levels = set(x["level"] for x in scope["l3_list"])
    assert levels <= {"REQUIRED", "OPTIONAL", "UNKNOWN"}
    assert "USUAL" not in levels
    # 砌筑工程在住宅下是"可选"（KB 里 OPTIONAL）→ 必须照常进树，不能因为改名丢工种
    masonry = [x for x in scope["l3_list"] if x["work_type_id"] == "masonry"]
    assert masonry and masonry[0]["level"] == "OPTIONAL"


def test_split_l3_accepts_legacy_usual_rows():
    """旧库 / 旧产物里还是 `USUAL` 的行，也必须照常收下（向后兼容）。"""
    rows = [{"work_type_id": "masonry", "work_type_name": "砌筑工程",
             "applicability_level": "USUAL", "confidence": None, "notes": None}]
    selected, excluded, cands, _n, _w, _a = ks._split_l3(rows, False, "")
    assert len(selected) == 1 and selected[0]["level"] == "OPTIONAL"
    assert excluded == []
    assert "masonry" in cands


def test_wbs_gen_injection_accepts_both_level_names(monkeypatch):
    """wbs_gen 注入：新旧枚举名（USUAL / OPTIONAL）都要能注入。"""
    from pipeline.nodes import wbs_gen

    monkeypatch.setattr(wbs_gen.kb, "l3_for", lambda bid: [
        {"work_type_id": "rebar", "work_type_name": "钢筋工程",
         "applicability_level": "USUAL", "confidence": None, "notes": None},
        {"work_type_id": "steel_structure", "work_type_name": "金属结构工程",
         "applicability_level": "EXCLUDED", "confidence": None, "notes": None},
    ])
    txt = wbs_gen.build_kb_injection("residential")
    assert txt and "钢筋工程" in txt and "[可选]" in txt
    assert "金属结构工程" in txt            # EXCLUDED 走"排除"通道
    assert "[常用]" not in txt              # 旧文案不得残留

    monkeypatch.setattr(wbs_gen.kb, "l3_for", lambda bid: [
        {"work_type_id": "rebar", "work_type_name": "钢筋工程",
         "applicability_level": "OPTIONAL", "confidence": None, "notes": None}])
    txt2 = wbs_gen.build_kb_injection("residential")
    assert txt2 and "钢筋工程" in txt2 and "[可选]" in txt2


# ======================================================================
# A4 · L3 被排除 → 其下 L4 一律不进（与逻辑）+ 审计留痕
# ======================================================================
def test_and_logic_l3_excluded_kills_whole_subtree():
    """建筑类型排除该 L3 → 其下 L4 一条都不进（与逻辑的一半）。"""
    rows = [{"work_type_id": "concrete", "work_type_name": "混凝土工程",
             "applicability_level": "EXCLUDED", "confidence": None, "notes": "住宅不适用"}]
    selected, excluded, cands, _n, _w, audit = ks._split_l3(rows, False, "shear_wall")
    assert selected == []
    assert "concrete" not in cands, "L3 被排除时其下 L4 一律不进"
    assert excluded and excluded[0]["source"] == "building_type"
    assert audit["excluded_subtrees"][0]["source_label"] == "建筑类型"


def test_and_logic_structure_filter_removes_l4_but_keeps_l3():
    """结构形式剔除该 L4 → 该条不进；L3 本身按设计保留（不做整工种降级）。"""
    rows = [{"work_type_id": "concrete", "work_type_name": "混凝土工程",
             "applicability_level": "REQUIRED", "confidence": None, "notes": None}]
    selected, _excluded, cands, _n, _w, audit = ks._split_l3(rows, False, "shear_wall")
    assert selected and selected[0]["work_type_id"] == "concrete"
    ids = [x["activity_id"] for x in cands["concrete"]]
    assert "CONC_NEW_COLUMN" not in ids, "剪力墙下柱浇筑必须被剔除"
    assert "CONC_NEW_WALL" in ids
    # 未被剔空 → 不该产生"整棵子树"留痕
    assert all(x["work_type_id"] != "concrete" for x in audit["excluded_subtrees"])


def test_excluded_subtrees_audit_building_type_source():
    """住宅下被建筑类型排除的 L3 → 审计留痕逐条带来源与原因。"""
    scope = _run()
    by_wt = {x["work_type_id"]: x for x in scope["excluded_subtrees"]}
    assert "steel_structure" in by_wt
    it = by_wt["steel_structure"]
    assert it["source"] == "building_type" and it["source_label"] == "建筑类型"
    assert it["reason"], "留痕必须带原因文案"
    assert it["l4_count"] > 0, "留痕要说明被一并剔除的 L4 条数"
    assert scope["stats"]["excluded_subtrees"] == len(scope["excluded_subtrees"])


def test_excluded_subtrees_audit_structure_type_source():
    """工业厂房 × 剪力墙：steel_structure 的 L4 被结构形式全剔空 → 整棵子树留痕。"""
    scope = _run("工业厂房", "剪力墙结构")
    assert scope["l4_candidates"]["steel_structure"] == [], "前置条件：该 L3 的 L4 被剔空"
    hits = [x for x in scope["excluded_subtrees"] if x["work_type_id"] == "steel_structure"]
    assert hits and hits[0]["source"] == "structure_type"
    assert hits[0]["source_label"] == "结构类型"
    assert "结构形式剔除" in hits[0]["reason"]
    # L3 本身仍在（不做整工种降级）
    assert "steel_structure" in [x["work_type_id"] for x in scope["l3_list"]]


def test_kb_conformance_carries_scope_audit():
    """A4 留痕要进一致性校验输出（结果 + plan_json.meta 留档块）。"""
    scope = _run()
    r = kc.check_scope_conformance({"phases": []}, scope)
    audit = r["scope_audit"]
    assert audit["excluded_subtree_count"] == len(scope["excluded_subtrees"])
    assert any(x["work_type_id"] == "steel_structure" for x in audit["excluded_subtrees"])
    assert kc.format_scope_audit(r), "留痕要能被渲染成中文行"

    meta = kc.metadata(r)
    assert json.loads(json.dumps(meta, ensure_ascii=False)) == meta
    assert meta["excluded_subtrees"] == audit["excluded_subtrees"]


def test_scope_audit_empty_for_old_scope_objects():
    """旧 plan_json 里没有这些键 → 空壳，不报错（留痕不是判据）。"""
    r = kc.check_scope_conformance({"phases": []}, {"l4_candidates": {"concrete": []}})
    assert r["scope_audit"]["excluded_subtrees"] == []
    assert r["scope_audit"]["standalone"] == {}
    assert kc.format_scope_audit(r) == ""


# ======================================================================
# A5 · L4「该不该单独成工序」通用校验
# ======================================================================
def test_r1_builtin_flags_resource_actions():
    """判据列**值为空**时，内置 R1 名称判据仍要抓出「XX运输」这类资源动作。

    A5（2026-09-21）已把 3 个判据列落库（`is_standalone_activity` /
    `standalone_rule` / `standalone_note`，**全部 NULL = 未判**）→ 这里
    `fields_present` 由 False 变 True，但值全空所以仍走内置 R1 兜底；
    留痕 `source` 必须仍是 `builtin_r1_name_pattern`（**不许冒充 KB 判据**）。
    列不存在时的跳过留痕见 `test_rule_data_absent_is_traced_not_silent`。
    """
    scope = _run()
    sa = scope["standalone_audit"]
    assert sa["judged"] > 0, "material_transport 名下 116 个「XX运输」必须被 R1 抓到"
    assert sa["fields_present"] is True, "A5 落库后判据列应已存在"
    hits = [x for x in sa["audit"] if x["verdict"] == ks.STANDALONE_VERDICT]
    assert hits and all(x["rule"] == ks.STANDALONE_RULE_R1 for x in hits)
    assert all(x["source"] == "builtin_r1_name_pattern" for x in hits), \
        "判据值为空 → 必须标内置兜底，不许标 kb_column"
    assert any("运输" in x["activity_name"] for x in hits)
    assert all(x["activity_id"] for x in hits)


def test_rule_data_absent_is_traced_not_silent(monkeypatch):
    """KB 判据列不存在 → **跳过并留痕**（绝不静默、也绝不崩）。"""
    import re as _re
    monkeypatch.setattr(ks, "_standalone_cols",
                        lambda: {"verdict": False, "rule": False, "note": False})
    monkeypatch.setattr(ks, "_standalone_db_rows", lambda cols: None)
    res = ks.check_standalone_activities(
        {"material_transport": [{"activity_id": "X1", "activity_name": "刨花板运输"}]},
        use_builtin_r1=False)
    assert res["fields_present"] is False
    assert res["audit"] and res["audit"][0]["code"] == "rule_data_absent"
    assert "未落库" in res["audit"][0]["reason"]
    assert res["judged"] == 0
    assert _re is not None


def test_r2_verdict_comes_from_kb_column_only(monkeypatch):
    """R2（消耗已包含在其他工序定额里）只能来自 KB 判据列 —— 有数据才判。"""
    monkeypatch.setattr(ks, "_standalone_cols",
                        lambda: {"verdict": True, "rule": True, "note": True})
    monkeypatch.setattr(ks, "_standalone_db_rows", lambda cols: {
        "A1": {"is_standalone_activity": 0, "standalone_rule": "R2",
               "standalone_note": "消耗已含在混凝土浇筑定额内"},
        "A2": {"is_standalone_activity": 1, "standalone_rule": None, "standalone_note": None},
        "A3": {"is_standalone_activity": None, "standalone_rule": None, "standalone_note": None},
    })
    res = ks.check_standalone_activities({
        "concrete": [{"activity_id": "A1", "activity_name": "混凝土养护"},
                     {"activity_id": "A2", "activity_name": "板浇筑"},
                     {"activity_id": "A3", "activity_name": "构件运输"}]})
    assert res["fields_present"] is True and res["r2_checked"] is True
    hit = [x for x in res["audit"] if x["activity_id"] == "A1"][0]
    assert hit["rule"] == "R2" and hit["source"] == "kb_column"
    assert hit["reason"] == "消耗已含在混凝土浇筑定额内"
    assert not any(x["activity_id"] == "A2" for x in res["audit"]), "1 = 可单独成工序，不报"
    # A3 没判定值、KB 里也没判据编号 → 只剩内置 R1（"构件运输"命中）
    a3 = [x for x in res["audit"] if x["activity_id"] == "A3"]
    assert a3 and a3[0]["source"] == "builtin_r1_name_pattern"


def test_standalone_check_is_independent_of_level():
    """与档位无关：REQUIRED 的 L4 照样会被 R1 判"不该单独成工序"。"""
    import re as _re
    assert _re.search(ks._R1_NAME_RE, "各种笆片运输")
    res = ks.check_standalone_activities(
        {"material_transport": [{"activity_id": "T1", "activity_name": "工具式脚手杆运输"}]})
    assert res["judged"] == 1
    assert "REQUIRED" not in json.dumps(res, ensure_ascii=False)  # 判据里没有档位概念


# ======================================================================
# A6 · L3 层：用户给了量 → 强化为"必须"
# ======================================================================
def test_l3_strengthened_by_user_quantities():
    """`total_concrete` 等分项量有值 → 对应 L3 档位强化为 REQUIRED 并留痕。"""
    base = _run()
    assert [x for x in base["l3_list"] if x["work_type_id"] == "concrete"][0]["level"] == "REQUIRED"

    scope = _run(extra={"total_concrete": 52000, "total_rebar": 7500,
                        "total_earthwork": 30000, "total_pile": 8000})
    levels = {x["work_type_id"]: x["level"] for x in scope["l3_list"]}
    for wt in ("concrete", "rebar", "earthwork", "pile_foundation"):
        assert levels[wt] == "REQUIRED", wt
    audit = {x["param"]: x for x in scope["l3_strengthened"]}
    # 【第 2 批 · 域 2 / 2.4】`total_wall` 已删除 → 桩基的来源键改指 `total_pile`
    # （`kb_scope._quantity_strengthened_l3` 与 `ratio_scope.GROUP_TOTAL_PARAMS` 同步）。
    assert audit["total_pile"]["work_type_id"] == "pile_foundation"
    assert audit["total_pile"]["to"] == "REQUIRED"
    assert "8000" in audit["total_pile"]["reason"]


def test_l3_not_strengthened_when_quantity_absent_or_zero():
    """没给量 / 量为 0 → 不强化，也不留痕。"""
    scope = _run(extra={"total_concrete": 0, "total_rebar": None, "total_earthwork": ""})
    assert scope["l3_strengthened"] == []
    assert scope["stats"]["l3_strengthened"] == 0


def test_l3_strengthened_when_building_type_unknown():
    """建筑类型识别不到时档位是 UNKNOWN，用户给了量就强化为 REQUIRED。"""
    scope = _run("一个不存在的类型", "剪力墙结构", extra={"total_concrete": 100})
    levels = {x["work_type_id"]: x["level"] for x in scope["l3_list"]}
    assert levels["concrete"] == "REQUIRED"
    assert levels["rebar"] == "UNKNOWN"


# ======================================================================
# A6 · L4 层：量 = 0 的 L4 直接不进树
# ======================================================================
def test_l4_zero_quantity_filtered(monkeypatch):
    """上游给了 per-L4 量 → 量 = 0 的不进树，并留痕。"""
    scope = _run(extra={"l4_quantities": {"concrete": {"CONC_NEW_FOUND": 0,
                                                      "CONC_NEW_WALL": 1200}}})
    ids = [x["activity_id"] for x in scope["l4_candidates"]["concrete"]]
    assert "CONC_NEW_FOUND" not in ids
    assert "CONC_NEW_WALL" in ids
    audit = scope["l4_excluded_by_quantity"]
    assert audit and audit[0]["activity_id"] == "CONC_NEW_FOUND"
    assert audit[0]["quantity"] == 0 and audit[0]["reason"]
    assert scope["stats"]["l4_excluded_by_quantity"] == len(audit)


def test_l4_flat_quantity_shape_also_accepted():
    """接口也接受扁平的 `{activity_id: 量}`。"""
    scope = _run(extra={"l4_quantities": {"CONC_NEW_FOUND": 0.0}})
    ids = [x["activity_id"] for x in scope["l4_candidates"]["concrete"]]
    assert "CONC_NEW_FOUND" not in ids


def test_l4_no_quantity_no_filter():
    """上游没给 per-L4 量 → 不过滤（保持现状，不猜）。"""
    scope = _run()
    ids = [x["activity_id"] for x in scope["l4_candidates"]["concrete"]]
    assert "CONC_NEW_FOUND" in ids
    assert scope["l4_excluded_by_quantity"] == []


# ======================================================================
# A6 · 明确排除项（否定表述）三态
# ======================================================================
def test_extract_exclusions_three_scopes():
    """全局 / 局部 / 定位不了，三态判定必须分得清。"""
    global_it = [x for x in si.extract_exclusions("本项目不含桩基。")][0]
    assert global_it["canonical"] == "桩基" and global_it["scope"] == "global"
    assert global_it["needs_confirm"] is False
    assert global_it["l3_candidates"] == ["pile_foundation"]

    local_it = [x for x in si.extract_exclusions("地下室顶板不含防水。")][0]
    assert local_it["canonical"] == "防水" and local_it["scope"] == "local"
    assert local_it["local_hint"] and local_it["needs_confirm"] is True
    assert si.is_hard_gate_exclusion(local_it) is False

    unknown_it = [x for x in si.extract_exclusions("东区不含保温。")][0]
    assert unknown_it["scope"] == "unknown" and unknown_it["needs_confirm"] is True
    assert si.is_hard_gate_exclusion(unknown_it) is False


def test_extract_exclusions_negation_after_term():
    """否定在词后（"桩基已另行发包"）也要抽得到，且是全局。"""
    items = si.extract_exclusions("桩基已另行发包，不在本次范围。")
    assert items and items[0]["canonical"] == "桩基"
    assert items[0]["scope"] == "global" and items[0]["needs_confirm"] is False


def test_extract_exclusions_multi_term_in_one_clause():
    """一个否定标记管一串词（"不含幕墙和精装修"）→ 两个都要命中。"""
    cans = {x["canonical"] for x in si.extract_exclusions("本项目不含幕墙和精装修。")}
    assert {"幕墙", "精装修"} <= cans


def test_extract_exclusions_unknown_term_goes_to_confirm():
    """同义词表里没有的表述 → canonical 为空 + 转待确认，绝不猜。"""
    items = si.extract_exclusions("本项目不含水晶吊灯。")
    assert items and items[0]["canonical"] is None
    assert items[0]["needs_confirm"] is True
    assert si.is_hard_gate_exclusion(items[0]) is False


def test_global_exclusion_is_hard_gate_for_l3():
    """全局排除（硬闸门）→ 对应 L3 整棵子树不进树 + 留痕。

    注意：`user_exclusions_applied` 同时收 L3 级与 L4 级留痕，且顺序取决于
    `kb.l3_for()` 的档位排序（A1 三档化修好了 `OPTIONAL` 落到 NULL 排最前的
    bug → REQUIRED 工种现排在前）→ 断言只查**成员关系**，不查下标。
    """
    ex = si.extract_exclusions("本项目不含桩基。")
    scope = _run(extra={"exclusions": ex})
    assert "pile_foundation" not in scope["l4_candidates"]
    assert "pile_foundation" in [x["work_type_id"] for x in scope["excluded_l3"]]
    l3_hits = [x for x in scope["user_exclusions_applied"] if x["level"] == "L3"]
    assert l3_hits, "必须留下 L3 级硬闸门留痕：%s" % scope["user_exclusions_applied"]
    assert "pile_foundation" in [x["target"] for x in l3_hits]
    sub = [x for x in scope["excluded_subtrees"] if x["work_type_id"] == "pile_foundation"]
    assert sub and sub[0]["source"] == "user_exclusion"


def test_local_exclusion_never_vetoes_whole_l4():
    """局部排除**绝不能**一票否决整楼的那条 L4：只转待确认，不进硬闸门。"""
    ex = si.extract_exclusions("地下室顶板不含防水。")
    scope = _run(extra={"exclusions": ex})
    assert "waterproofing" in scope["l4_candidates"], "局部排除不许砍掉整栋的防水"
    assert scope["user_exclusions_applied"] == []
    pending = scope["user_exclusions_pending"]
    assert pending and pending[0]["canonical"] == "防水"
    assert pending[0]["local_hint"]


def test_unknown_scope_exclusion_not_gated():
    """作用域定位不了 → 转待确认，不进硬闸门。"""
    ex = si.extract_exclusions("东区不含保温。")
    scope = _run(extra={"exclusions": ex})
    assert "insulation" in scope["l4_candidates"]
    assert scope["user_exclusions_applied"] == []
    assert scope["user_exclusions_pending"]


def test_normalize_exclusions_is_idempotent():
    """extractor / boundary 各调一次不许把结果搞乱（幂等）。"""
    once = si.normalize_exclusions(None, "本项目不含桩基。")
    twice = si.normalize_exclusions(once, "本项目不含桩基。")
    assert twice == once or len(twice) == len(once) == 1


# ======================================================================
# A6 · 层面积字典
# ======================================================================
_TEXT_FLOORS = "1层 1200㎡ / 2~18层 800㎡ / 地下1层 3000㎡"


def test_floor_areas_extraction_structure():
    """逐层面积抽取 → `{楼栋: {层号或层区间: 面积}}`，单位 m²。"""
    built = si.extract_floor_areas(_TEXT_FLOORS)
    assert built["source"] == "user" and built["unit"] == "m²"
    floors = built["buildings"]["default"]["floors"]
    assert floors["1"] == 1200.0
    assert floors["2~18"] == 800.0
    assert floors["地下1"] == 3000.0
    assert built["buildings"]["default"]["floor_count"] == 1 + 17 + 1
    assert built["buildings"]["default"]["sum_area"] == 1200 + 800 * 17 + 3000


def test_floor_areas_needs_review_on_big_relative_error():
    """Σ各层面积与总建筑面积相对误差 > 10% → `needs_review=True`，不静默采信。"""
    built = si.extract_floor_areas("1层 1200㎡ / 2~18层 800㎡", total_area=10000)
    b = built["buildings"]["default"]
    assert built["needs_review"] is True and b["needs_review"] is True
    assert b["rel_error"] > si.REVIEW_REL_TOLERANCE
    assert "相对误差" in b["review_reason"]


def test_floor_areas_ok_within_tolerance():
    """误差在容差内 → 不标 needs_review。"""
    built = si.extract_floor_areas(_TEXT_FLOORS, total_area=18000)
    assert built["needs_review"] is False
    assert built["buildings"]["default"]["rel_error"] < si.REVIEW_REL_TOLERANCE


def test_floor_areas_fallback_to_average_assumption():
    """用户没给逐层面积 → 退回均摊，并**必须**标注"均摊假设"。"""
    built = si.extract_floor_areas("总建筑面积 15000 ㎡，共 18 层", total_area=15000, floors=18)
    assert built["source"] == "average_assumption"
    b = built["buildings"]["default"]
    assert b["floors"] == {}
    assert b["average_area_per_floor"] == pytest.approx(15000 / 18)
    assert "均摊假设" in b["assumption"]
    assert built["notes"], "回退必须留痕"


def test_floor_areas_none_when_nothing_available():
    """既没逐层面积也没总量/层数 → `source='none'` + needs_review（绝不编数）。"""
    built = si.extract_floor_areas("这是一个项目")
    assert built["source"] == "none"
    assert built["buildings"]["default"]["average_area_per_floor"] is None
    assert built["needs_review"] is True


def test_floor_areas_do_not_swallow_total_area():
    """护栏：「38层，总建筑面积215000㎡」不许被当成逐层面积抽出来。"""
    built = si.extract_floor_areas("地上38层，总建筑面积215000㎡", total_area=215000, floors=38)
    assert built["source"] == "average_assumption"
    assert built["buildings"]["default"]["floors"] == {}


def test_expand_floor_areas_for_segment_plan():
    """展开成逐层 `{楼层号: 面积}`（供 segment_plan.segment_floors 消费），地下为负号。"""
    built = si.extract_floor_areas(_TEXT_FLOORS)
    flat, skipped = si.expand_floor_areas(built)
    assert flat["1"] == 1200.0 and flat["2"] == 800.0 and flat["18"] == 800.0
    assert flat["-1"] == 3000.0
    assert len(flat) == 19 and skipped == []


def test_expand_skips_named_floors_without_guessing():
    """名称层（"首层"/"标准层"）不猜层号 → 跳过并留痕。"""
    built = si.extract_floor_areas("首层 1200㎡ / 标准层 800㎡")
    flat, skipped = si.expand_floor_areas(built)
    assert flat == {} and set(skipped) == {"首层", "标准层"}


# ======================================================================
# A6 · extractor 接线（正则 → 字段）
# ======================================================================
def test_extractor_normalize_params_emits_new_fields():
    """`normalize_params` 必须产出 `exclusions` 与 `floor_areas` 两个字段。"""
    from pipeline.nodes.extractor import normalize_params

    p = normalize_params({}, "本项目不含幕墙。总建筑面积 15000 ㎡，共 18 层。"
                             "1层 1200㎡ / 2~18层 800㎡")
    assert isinstance(p["exclusions"], list) and p["exclusions"][0]["canonical"] == "幕墙"
    fa = p["floor_areas"]
    assert fa["source"] == "user" and fa["buildings"]["default"]["floors"]["1"] == 1200.0
    assert p["total_area"] == 15000 and p["floors"] == 18


def test_boundary_core_keys_include_new_fields():
    """boundary 的 CORE_KEYS 必须带上两个新字段（否则模型给的会被丢）。"""
    from pipeline.nodes import boundary

    assert "exclusions" in boundary.CORE_KEYS
    assert "floor_areas" in boundary.CORE_KEYS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

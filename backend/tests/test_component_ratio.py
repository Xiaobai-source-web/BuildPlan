# -*- coding: utf-8 -*-
"""`pipeline/component_ratio.py` 四组闭合校验的单元测试。

对应「修改项总清单_20260921.md」§六 验收要求第 2 条：
四个闭合校验必须全绿 —— 占比 V1–V4、MWI 五类完整性、映射表三档无空档、条件无缺维。

铁律：本测试**只用注入数据**，不碰 kb.db。
运行：`python -m pytest tests/test_component_ratio.py -q --basetemp=<临时目录>`
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from pipeline.component_ratio import (  # noqa: E402
    L4_MAPPING_LEVELS,
    MWI_CAPACITY_MODES,
    WORK_TYPE_UNKNOWN,
    check_condition_dimensions,
    check_mapping_tiers,
    check_mwi_completeness,
    check_ratio_v1_v4,
    normalize_area_unit,
    run_all_checks,
)


# ======================================================================
# 夹具
# ======================================================================


def _ratio(sid, aid, pct):
    return {"structure_type_id": sid, "activity_id": aid, "ratio_percent": pct}


def _map(sid, aid, level):
    return {"structure_type_id": sid, "activity_id": aid, "applicability_level": level}


def _mwi(name, kind, value, unit, mobility, mode):
    return {
        "resource_name": name,
        "resource_kind": kind,
        "mwi": value,
        "mwi_unit": unit,
        "resource_mobility": mobility,
        "capacity_mode": mode,
    }


def _codes(result):
    return sorted({v["code"] for v in result["violations"]})


def _errors(result):
    return [v for v in result["violations"] if v["severity"] == "error"]


def _warnings(result):
    return [v for v in result["violations"] if v["severity"] == "warning"]


#: 五类齐全、单位正确、mobility 合法的 MWI 表（一条/类）
def _green_mwi():
    return [
        _mwi("钢筋工", "labor", 20.0, "m²/人", "fixed", "area"),
        _mwi("挖掘机", "machine", 500.0, "m²/台", "fixed", "area"),
        _mwi("打桩机", "machine", None, None, "fixed", "position"),
        _mwi("交流弧焊机", "machine", None, None, "mobile", "auxiliary"),
        _mwi("自卸汽车", "machine", None, None, "mobile", "transport"),
        _mwi("塔吊", "machine", None, None, "site", "site"),
    ]


def _green_ratio():
    return [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 40.0)]


def _green_mapping():
    return [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "OPTIONAL")]


def _green_conditions():
    dict_rows = [
        {"condition_id": "C1", "condition_type": "体积", "condition_value": ">1m³",
         "applicable_work_type": "concrete"},
        {"condition_id": "C2", "condition_type": "材料类型", "condition_value": "砖",
         "applicable_work_type": "masonry"},
    ]
    norm_rows = [
        {"activity_id": "A1", "condition_key": {"体积": ">1m³", "材料类型": "砖"},
         "table": "labor"},
        {"activity_id": "A2", "condition_key": {"体积": ">1m³", "材料类型": "砖"},
         "table": "equipment"},
    ]
    return dict_rows, norm_rows


# ======================================================================
# 0. 单位口径
# ======================================================================


def test_normalize_area_unit_variants():
    """m2/人 与方块写法都归一到 m²/人；非面积单位原样返回。"""
    assert normalize_area_unit("m2/人") == "m²/人"
    assert normalize_area_unit("m²/人") == "m²/人"
    assert normalize_area_unit("㎡/台") == "m²/台"
    assert normalize_area_unit("工日/m³") == "工日/m³"
    assert normalize_area_unit(None) is None
    assert normalize_area_unit("  ") is None


def test_no_block_area_symbol_in_source():
    """铁律：源码里不得出现 U+33A1 的方块平米符号。"""
    path = os.path.join(_BACKEND, "pipeline", "component_ratio.py")
    with open(path, encoding="utf-8") as fh:
        blob = fh.read()
    assert "\u33a1" not in blob
    assert "m²" in blob


# ======================================================================
# 1. 占比 V1–V4
# ======================================================================


def test_v1_sum_equals_100_pass():
    """V1 守恒：∑ = 100 通过。

    未注入 l4_to_l3 → 两行同属哨兵工种组 `<unknown>`，组内 ∑=100 仍通过
    （并附 work_type_unknown 警告，见 test_work_type_unknown_warns_not_silent）。
    """
    got = check_ratio_v1_v4(_green_ratio(), _green_mapping(), [("S1", "A1"), ("S1", "A2")])
    assert "V1_ratio_sum" not in _codes(got)
    assert got["stats"]["structure_type_sums"] == {"S1": 100.0}
    assert got["stats"]["group_sums"] == {"S1|" + WORK_TYPE_UNKNOWN: 100.0}


def test_v1_grouped_by_structure_and_work_type():
    """V1 新口径：按「结构类型 × 工种(L3)」分组，各组各自 ∑ = 100，互不干扰。"""
    rows = [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 40.0),
            _ratio("S1", "B1", 100.0)]
    mapping = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "OPTIONAL"),
               _map("S1", "B1", "REQUIRED")]
    got = check_ratio_v1_v4(
        rows, mapping,
        l4_to_l3={"A1": "concrete", "A2": "concrete", "B1": "rebar"})
    assert "V1_ratio_sum" not in _codes(got)
    assert got["stats"]["group_sums"] == {"S1|concrete": 100.0, "S1|rebar": 100.0}
    assert got["stats"]["groups"] == 2
    assert got["stats"]["work_types"] == ["concrete", "rebar"]
    # 结构层合计 200%：不再是判据，只作参考统计
    assert got["stats"]["structure_type_sums"] == {"S1": 200.0}


def test_v1_one_bad_group_does_not_affect_other():
    """某工种组不守恒 → 只报该组；同结构下另一工种组不受影响。"""
    rows = [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 30.0),
            _ratio("S1", "B1", 100.0)]
    mapping = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "OPTIONAL"),
               _map("S1", "B1", "REQUIRED")]
    got = check_ratio_v1_v4(
        rows, mapping,
        l4_to_l3={"A1": "concrete", "A2": "concrete", "B1": "rebar"})
    hits = [v for v in got["violations"] if v["code"] == "V1_ratio_sum"]
    assert len(hits) == 1
    assert hits[0]["keys"] == ["S1", "concrete", "ratio_percent"]
    assert hits[0]["detail"]["work_type_id"] == "concrete"
    assert hits[0]["detail"]["group_key"] == "S1|concrete"
    assert hits[0]["detail"]["sum"] == 90.0
    assert hits[0]["detail"]["l4_count"] == 2


def test_v1_single_l4_group_must_be_100():
    """组内只有 1 个 L4 时该 L4 必须是 100（V1 的自然推论）。"""
    mapping = [_map("S1", "A1", "REQUIRED")]
    ok = check_ratio_v1_v4([_ratio("S1", "A1", 100.0)], mapping,
                           l4_to_l3={"A1": "concrete"})
    assert "V1_ratio_sum" not in _codes(ok)
    assert ok["stats"]["single_l4_groups"] == 1

    bad = check_ratio_v1_v4([_ratio("S1", "A1", 99.0)], mapping,
                            l4_to_l3={"A1": "concrete"})
    hits = [v for v in bad["violations"] if v["code"] == "V1_ratio_sum"]
    assert len(hits) == 1
    assert hits[0]["detail"]["l4_count"] == 1
    assert hits[0]["detail"]["sum"] == 99.0


def test_work_type_unknown_warns_not_silent():
    """缺工种映射的行不静默跳过：报 work_type_unknown（warning）并计数。"""
    rows = [_ratio("S1", "A1", 100.0)]
    got = check_ratio_v1_v4(rows, [_map("S1", "A1", "REQUIRED")])
    hits = [v for v in got["violations"] if v["code"] == "work_type_unknown"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "warning"
    assert hits[0]["detail"]["work_type_id"] == WORK_TYPE_UNKNOWN
    assert hits[0]["detail"]["group_key"] == "S1|" + WORK_TYPE_UNKNOWN
    assert got["stats"]["work_type_unknown_pairs"] == 1
    assert got["all_green"] is True   # 只是 warning，不是硬违规

    # 显式注入了字典但缺该 L4 → 同样报警
    got2 = check_ratio_v1_v4(rows, [_map("S1", "A1", "REQUIRED")],
                             l4_to_l3={"A9": "concrete"})
    assert "work_type_unknown" in _codes(got2)
    assert got2["stats"]["group_sums"] == {"S1|" + WORK_TYPE_UNKNOWN: 100.0}


def test_v3_violation_carries_its_group():
    """V3 违规必须带上所属的「结构类型 × 工种」组，便于定位。"""
    rows = [_ratio("S1", "A1", 100.0)]
    mapping = [_map("S1", "A1", "REQUIRED"), _map("S1", "B1", "REQUIRED")]
    got = check_ratio_v1_v4(rows, mapping,
                            l4_to_l3={"A1": "concrete", "B1": "rebar"})
    hits = [v for v in got["violations"] if v["code"] == "V3_required_zero"]
    assert len(hits) == 1
    assert hits[0]["detail"]["activity_id"] == "B1"
    assert hits[0]["detail"]["work_type_id"] == "rebar"
    assert hits[0]["detail"]["group_key"] == "S1|rebar"
    assert "S1|rebar" in hits[0]["message"]


def test_v2_and_v4_details_carry_group():
    """V2 / V4 的 detail 同样带组信息（口径一致）。"""
    rows = [_ratio("S1", "A1", 100.0)]
    mapping = [_map("S1", "A1", "EXCLUDED")]
    got = check_ratio_v1_v4(rows, mapping, [("S1", "A9")],
                            l4_to_l3={"A1": "concrete"})
    v2 = [v for v in got["violations"] if v["code"] == "V2_ratio_excluded"][0]
    assert v2["detail"]["group_key"] == "S1|concrete"
    assert v2["detail"]["work_type_id"] == "concrete"
    v4 = [v for v in got["violations"] if v["code"] == "V4_not_landed"][0]
    assert v4["detail"]["group_key"] == "S1|concrete"
    assert v4["detail"]["work_type_id"] == "concrete"


def test_run_all_checks_passes_l4_to_l3_to_ratio_check():
    """run_all_checks 把 l4_to_l3 透传给 check_ratio_v1_v4（否则占比组全部 unknown）。"""
    out = run_all_checks(
        ratio_rows=_green_ratio(),
        mapping_rows=_green_mapping(),
        wbs_landed=[("S1", "A1"), ("S1", "A2")],
        known_l4=["A1", "A2"],
        known_structures=["S1"],
        l4_to_l3={"A1": "concrete", "A2": "concrete"},
    )
    ratio = out["checks"][0]
    assert ratio["check"] == "ratio_v1_v4"
    assert "work_type_unknown" not in {v["code"] for v in ratio["violations"]}
    assert ratio["stats"]["group_sums"] == {"S1|concrete": 100.0}


# ======================================================================
# 1b. 路线 2：占比表只回答「部位」—— 豁免（不参与）语义
# ======================================================================


def _r2_ratio():
    """2 个切分类合计 100。"""
    return [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 40.0)]


#: 路线 2 用例统一的 L4 → 工种映射（全部落同组，避免混入哨兵工种噪声）
_R2_L3 = {"A1": "concrete", "A2": "concrete", "A3": "concrete", "A9": "masonry"}


def _r2_mapping():
    """A3 是工序类条目（REQUIRED，但不参与占比）。"""
    return [
        _map("S1", "A1", "REQUIRED"),
        _map("S1", "A2", "OPTIONAL"),
        _map("S1", "A3", "REQUIRED"),
    ]


def test_v3_required_without_ratio_is_error_by_default():
    """默认（不传豁免）= 路线 1 语义：REQUIRED 没占比 → V3 违规。"""
    got = check_ratio_v1_v4(_r2_ratio(), _r2_mapping(), l4_to_l3=_R2_L3)
    hits = [v for v in got["violations"] if v["code"] == "V3_required_zero"]
    assert [v["keys"][1] for v in hits] == ["A3"]
    assert got["all_green"] is False


def test_v3_exempt_required_is_not_missing():
    """声明豁免（不参与）的 REQUIRED 条目 → 不算缺失，V3 不报，全绿。"""
    got = check_ratio_v1_v4(
        _r2_ratio(), _r2_mapping(), l4_to_l3=_R2_L3,
        exempt_activity_ids=["A3"],
    )
    assert "V3_required_zero" not in _codes(got)
    assert _errors(got) == []
    assert got["all_green"] is True


def test_v5_ratio_on_exempt_is_error():
    """V5 守卫：已声明不参与的条目不允许再拿占比（占比只落在切分类上）。"""
    rows = _r2_ratio() + [_ratio("S1", "A3", 5.0)]
    got = check_ratio_v1_v4(
        rows, _r2_mapping(), l4_to_l3=_R2_L3,
        exempt_activity_ids=["A3"],
    )
    hits = [v for v in got["violations"] if v["code"] == "V5_ratio_on_exempt"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "error"
    assert hits[0]["keys"] == ["S1", "A3", "exempt"]
    assert hits[0]["detail"]["group_key"] == "S1|concrete"
    assert hits[0]["detail"]["exempt"] is True
    assert got["all_green"] is False
    # V1 也会同时报（组内合计 105），但 V3 不应报 A3（它已被豁免）
    assert "V3_required_zero" not in _codes(got)


def test_exempt_pairs_are_scoped_to_structure():
    """``(结构, L4)`` 形式只豁免该结构：另一个结构下同一个 L4 仍按路线 1 报 V3。"""
    rows = [_ratio("S1", "A1", 100.0), _ratio("S2", "A1", 100.0)]
    mapping = [_map("S1", "A1", "REQUIRED"), _map("S1", "A3", "REQUIRED"),
               _map("S2", "A1", "REQUIRED"), _map("S2", "A3", "REQUIRED")]
    got = check_ratio_v1_v4(
        rows, mapping, l4_to_l3={"A1": "concrete", "A3": "concrete"},
        exempt_pairs=[("S1", "A3")],
    )
    hits = [v for v in got["violations"] if v["code"] == "V3_required_zero"]
    assert [(v["keys"][0], v["keys"][1]) for v in hits] == [("S2", "A3")]


def test_exempt_accepts_pair_form_and_ignores_junk():
    """``exempt_activity_ids`` 也接受 ``(结构, L4)``；非法元素被忽略、不抛异常。"""
    got = check_ratio_v1_v4(
        _r2_ratio(), _r2_mapping(), l4_to_l3=_R2_L3,
        exempt_activity_ids=[("S1", "A3"), None, "", ("S1",), ("S1", "A1", "X"), 7],
        exempt_pairs=[None, "A3", 42],
    )
    assert "V3_required_zero" not in _codes(got)
    # ("S1","A3") 生效；exempt_pairs 里的裸字符串 "A3" 不生效、空串也不展开
    assert got["stats"]["exempt_inputs"] == 1
    assert got["stats"]["exempt_entries"] == 1


def test_stats_expose_route2_counters():
    """stats 给出：已编组数 / 无可占比行的组数 / 豁免条目数。"""
    got = check_ratio_v1_v4(
        _r2_ratio(),
        _r2_mapping() + [_map("S9", "A9", "OPTIONAL")],
        l4_to_l3=_R2_L3,
        exempt_activity_ids=["A3"],
    )
    st = got["stats"]
    assert st["groups"] == 1                 # 已编组：S1|concrete
    assert st["groups_without_ratio"] == 1   # S9|masonry 一行占比都没有（不可切分/全 EXCLUDED）
    assert st["exempt_inputs"] == 2          # A3 在 2 个出现过的结构（S1/S9）下展开
    assert st["exempt_entries"] == 1         # 真正出现在数据里的只有 (S1, A3)


def test_run_all_checks_passes_exempt_through():
    """run_all_checks 把豁免集合透传给占比校验（映射档位校验不受影响）。"""
    out = run_all_checks(
        ratio_rows=_r2_ratio(),
        mapping_rows=_r2_mapping(),
        wbs_landed=[("S1", "A1"), ("S1", "A2")],
        known_l4=["A1", "A2", "A3"],
        known_structures=["S1"],
        l4_to_l3=_R2_L3,
        exempt_activity_ids=["A3"],
    )
    ratio = out["checks"][0]
    assert "V3_required_zero" not in {v["code"] for v in ratio["violations"]}
    assert ratio["stats"]["exempt_entries"] == 1
    assert ratio["all_green"] is True


def test_v1_sum_not_100_violation():
    """V1 守恒违规：合计 95 ≠ 100，且报出实际合计。"""
    rows = [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 35.0)]
    got = check_ratio_v1_v4(rows, _green_mapping())
    hits = [v for v in got["violations"] if v["code"] == "V1_ratio_sum"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "error"
    assert hits[0]["detail"]["sum"] == 95.0
    assert hits[0]["detail"]["delta"] == -5.0
    assert "95" in hits[0]["message"]


def test_v1_tolerance_boundary_99_99_passes():
    """容差边界内侧：99.99 与 100 的差 = 0.01，不大于容差 → 通过。"""
    rows = [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 39.99)]
    got = check_ratio_v1_v4(rows, _green_mapping(), tolerance=0.01)
    assert "V1_ratio_sum" not in _codes(got)


def test_v1_tolerance_boundary_99_98_fails():
    """容差边界外侧：99.98 与 100 的差 = 0.02 > 0.01 → 违规。"""
    rows = [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 39.98)]
    got = check_ratio_v1_v4(rows, _green_mapping(), tolerance=0.01)
    assert "V1_ratio_sum" in _codes(got)


def test_v1_tolerance_is_configurable():
    """容差可参数化：99.98 在 tolerance=0.05 下通过。"""
    rows = [_ratio("S1", "A1", 60.0), _ratio("S1", "A2", 39.98)]
    got = check_ratio_v1_v4(rows, _green_mapping(), tolerance=0.05)
    assert "V1_ratio_sum" not in _codes(got)


def test_v2_ratio_positive_but_excluded():
    """V2 一致违规：占比 > 0 但映射档位是 EXCLUDED。"""
    rows = [_ratio("S1", "A1", 100.0), _ratio("S1", "A2", 0.0)]
    mapping = [_map("S1", "A1", "EXCLUDED"), _map("S1", "A2", "OPTIONAL")]
    got = check_ratio_v1_v4(rows, mapping)
    hits = [v for v in got["violations"] if v["code"] == "V2_ratio_excluded"]
    assert len(hits) == 1
    assert hits[0]["keys"] == ["S1", "A1", "applicability_level"]
    assert hits[0]["detail"]["ratio_percent"] == 100.0


def test_v2_zero_ratio_may_be_excluded():
    """占比 = 0 时档位可以是 EXCLUDED（V2 只管 > 0）。"""
    rows = [_ratio("S1", "A1", 100.0), _ratio("S1", "A2", 0.0)]
    mapping = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "EXCLUDED")]
    got = check_ratio_v1_v4(rows, mapping)
    assert "V2_ratio_excluded" not in _codes(got)


def test_v3_required_but_zero_ratio():
    """V3 完整违规：档位 REQUIRED 但占比 = 0。"""
    rows = [_ratio("S1", "A1", 100.0), _ratio("S1", "A2", 0.0)]
    mapping = [_map("S1", "A1", "OPTIONAL"), _map("S1", "A2", "REQUIRED")]
    got = check_ratio_v1_v4(rows, mapping)
    hits = [v for v in got["violations"] if v["code"] == "V3_required_zero"]
    assert len(hits) == 1
    assert hits[0]["detail"]["activity_id"] == "A2"


def test_v3_required_but_ratio_row_absent():
    """V3 完整违规：档位 REQUIRED 但占比行**缺失**（视同 0）。"""
    rows = [_ratio("S1", "A1", 100.0)]
    mapping = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "REQUIRED")]
    got = check_ratio_v1_v4(rows, mapping)
    hits = [v for v in got["violations"] if v["code"] == "V3_required_zero"]
    assert len(hits) == 1
    assert hits[0]["detail"]["ratio_percent"] is None
    assert "缺失" in hits[0]["message"]


def test_v4_not_landed():
    """V4 落地违规：占比 > 0 但最终 WBS 无落点。"""
    got = check_ratio_v1_v4(_green_ratio(), _green_mapping(), [("S1", "A1")])
    hits = [v for v in got["violations"] if v["code"] == "V4_not_landed"]
    assert len(hits) == 1
    assert hits[0]["detail"]["activity_id"] == "A2"
    assert hits[0]["severity"] == "error"


def test_v4_landed_accepts_string_and_mapping_forms():
    """V4 落点集合支持 tuple / "结构|L4" 字符串 / 映射三种元素形态。"""
    got = check_ratio_v1_v4(_green_ratio(), _green_mapping(), ["S1|A1", "S1|A2"])
    assert "V4_not_landed" not in _codes(got)

    got2 = check_ratio_v1_v4(
        _green_ratio(), _green_mapping(),
        [{"structure_type_id": "S1", "activity_id": "A1"},
         {"structure_type_id": "S1", "activity_id": "A2"}],
    )
    assert "V4_not_landed" not in _codes(got2)


def test_v4_skipped_when_wbs_landed_not_given():
    """未提供 wbs_landed → V4 标 skipped（warning），不是硬违规。"""
    got = check_ratio_v1_v4(_green_ratio(), _green_mapping())
    hits = [v for v in got["violations"] if v["code"] == "V4_skipped"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "warning"
    assert got["stats"]["v4_skipped"] is True
    assert got["all_green"] is True


def test_mapping_absent_is_warning_not_v2_violation():
    """映射**缺失 ≠ EXCLUDED**：占比 > 0 且映射缺行 → 只报 mapping_absent 警告。"""
    rows = [_ratio("S1", "A1", 100.0)]
    mapping = []  # 映射表完全没有该行
    got = check_ratio_v1_v4(rows, mapping)
    codes = _codes(got)
    assert "mapping_absent" in codes
    assert "V2_ratio_excluded" not in codes
    hits = [v for v in got["violations"] if v["code"] == "mapping_absent"]
    assert hits[0]["severity"] == "warning"
    assert got["stats"]["mapping_absent"] == 1
    assert got["all_green"] is True


def test_ratio_not_numeric():
    """占比非数值 → 显式违规，不静默当 0。"""
    rows = [_ratio("S1", "A1", "abc"), _ratio("S1", "A2", 100.0)]
    got = check_ratio_v1_v4(rows, _green_mapping())
    assert "ratio_not_numeric" in _codes(got)


def test_ratio_v1_v4_all_green():
    """占比四校验全绿。"""
    got = check_ratio_v1_v4(_green_ratio(), _green_mapping(), [("S1", "A1"), ("S1", "A2")])
    assert got["all_green"] is True
    assert _errors(got) == []


# ======================================================================
# 2. MWI 五类完整性
# ======================================================================


def test_mwi_all_green_and_mode_counts():
    """五类齐全 → 全绿，且报出每类实际条数。"""
    got = check_mwi_completeness(_green_mwi())
    assert got["all_green"] is True, got["violations"]
    assert got["stats"]["mode_counts"] == {
        "area": 2, "position": 1, "auxiliary": 1, "transport": 1, "site": 1}
    assert got["stats"]["rows"] == 6


def test_mwi_missing_one_of_five_modes():
    """五类缺一类（transport 缺失）→ 报错。"""
    rows = [r for r in _green_mwi() if r["capacity_mode"] != "transport"]
    got = check_mwi_completeness(rows)
    hits = [v for v in got["violations"] if v["code"] == "mwi_mode_empty"]
    assert len(hits) == 1
    assert hits[0]["detail"]["capacity_mode"] == "transport"
    assert got["all_green"] is False


def test_mwi_expected_mode_counts_mismatch_is_warning():
    """预期条数核对（C1 的 27/21/11/5/3）：不符只警告，不影响硬判据。"""
    got = check_mwi_completeness(
        _green_mwi(),
        expected_mode_counts={"area": 27, "position": 21, "auxiliary": 11,
                              "transport": 5, "site": 3},
    )
    hits = [v for v in got["violations"] if v["code"] == "mwi_mode_count_mismatch"]
    assert len(hits) == 5
    assert all(v["severity"] == "warning" for v in hits)


def test_mwi_expected_mode_counts_exact_pass():
    """预期条数与实际一致 → 无 mwi_mode_count_mismatch。"""
    got = check_mwi_completeness(
        _green_mwi(),
        expected_mode_counts={"area": 2, "position": 1, "auxiliary": 1,
                              "transport": 1, "site": 1},
    )
    assert "mwi_mode_count_mismatch" not in _codes(got)


def test_mwi_value_not_positive():
    """area 型 mwi <= 0 → 违规；非 area 型给了非正数也违规。"""
    for bad in (0.0, -5.0):
        rows = _green_mwi()
        rows[0] = _mwi("钢筋工", "labor", bad, "m²/人", "fixed", "area")
        got = check_mwi_completeness(rows)
        assert "mwi_value_invalid" in _codes(got), bad

    rows = _green_mwi()
    rows[2] = _mwi("打桩机", "machine", 0.0, None, "fixed", "position")
    got = check_mwi_completeness(rows)
    assert "mwi_value_invalid" in _codes(got)


def test_mwi_non_area_may_have_null_mwi():
    """非 area 型 mwi 为 None 是正常的（position/auxiliary/transport/site）。"""
    got = check_mwi_completeness(_green_mwi())
    assert "mwi_value_invalid" not in _codes(got)


def test_mwi_unit_kind_mismatch_labor():
    """人工却写 m²/台 → 违规；机械 area 型写 m²/人 → 违规。"""
    rows = _green_mwi()
    rows[0] = _mwi("钢筋工", "labor", 20.0, "m²/台", "fixed", "area")
    got = check_mwi_completeness(rows)
    hits = [v for v in got["violations"] if v["code"] == "mwi_unit_kind_mismatch"]
    assert len(hits) == 1
    assert hits[0]["detail"]["expected_unit"] == "m²/人"

    rows = _green_mwi()
    rows[1] = _mwi("挖掘机", "machine", 500.0, "m²/人", "fixed", "area")
    got = check_mwi_completeness(rows)
    hits = [v for v in got["violations"] if v["code"] == "mwi_unit_kind_mismatch"]
    assert hits[0]["detail"]["expected_unit"] == "m²/台"


def test_mwi_unit_ascii_variant_accepted():
    """迁移脚本写的是 ASCII 的 m2/人、m2/台，必须被接受。"""
    rows = _green_mwi()
    rows[0] = _mwi("钢筋工", "labor", 20.0, "m2/人", "fixed", "area")
    rows[1] = _mwi("挖掘机", "machine", 500.0, "m2/台", "fixed", "area")
    got = check_mwi_completeness(rows)
    assert "mwi_unit_kind_mismatch" not in _codes(got)


def test_mwi_non_area_with_area_unit_is_mismatch():
    """非 area 型不该带面积单位（kb_migrate_phase1_mwi.py:157 → unit = None）。"""
    rows = _green_mwi()
    rows[3] = _mwi("交流弧焊机", "machine", None, "m²/台", "mobile", "auxiliary")
    got = check_mwi_completeness(rows)
    assert "mwi_unit_kind_mismatch" in _codes(got)


def test_mwi_unit_missing_on_area_row_is_mismatch():
    """area 型 mwi_unit 为空 → 违规。"""
    rows = _green_mwi()
    rows[0] = _mwi("钢筋工", "labor", 20.0, None, "fixed", "area")
    got = check_mwi_completeness(rows)
    assert "mwi_unit_kind_mismatch" in _codes(got)


def test_mwi_labor_must_be_fixed():
    """labor 却是 mobile → 违规（§3.2 判据）。"""
    rows = _green_mwi()
    rows[0] = _mwi("钢筋工", "labor", 20.0, "m²/人", "mobile", "area")
    got = check_mwi_completeness(rows)
    hits = [v for v in got["violations"] if v["code"] == "mwi_labor_mobility_not_fixed"]
    assert len(hits) == 1
    assert hits[0]["detail"]["resource_mobility"] == "mobile"


def test_mwi_labor_site_also_flagged():
    """labor 的 mobility 是 site 同样违规（人工只能是 fixed）。"""
    rows = _green_mwi()
    rows[0] = _mwi("钢筋工", "labor", 20.0, "m²/人", "site", "area")
    got = check_mwi_completeness(rows)
    assert "mwi_labor_mobility_not_fixed" in _codes(got)


def test_mwi_unknown_mobility_violation():
    """mobility 不在 {fixed, mobile, site} → 违规。"""
    rows = _green_mwi()
    rows[4] = _mwi("自卸汽车", "machine", None, None, "roaming", "transport")
    got = check_mwi_completeness(rows)
    assert "mwi_mobility_unknown" in _codes(got)


def test_mwi_unknown_capacity_mode_violation():
    """capacity_mode 不在五类 → 违规。"""
    rows = _green_mwi()
    rows[5] = _mwi("塔吊", "machine", None, None, "site", "site_wide")
    got = check_mwi_completeness(rows)
    codes = _codes(got)
    assert "mwi_mode_unknown" in codes
    assert "mwi_mode_empty" in codes  # 'site' 类因此空了


def test_mwi_duplicate_resource_name():
    """resource_name 重复 → 违规并给出行序。"""
    rows = _green_mwi() + [_mwi("钢筋工", "labor", 25.0, "m²/人", "fixed", "area")]
    got = check_mwi_completeness(rows)
    hits = [v for v in got["violations"] if v["code"] == "mwi_duplicate_name"]
    assert len(hits) == 1
    assert hits[0]["detail"]["count"] == 2
    assert hits[0]["detail"]["row_indexes"] == [0, 6]


def test_mwi_unknown_resource_kind():
    """resource_kind 既非 labor 也非 machine → 违规。"""
    rows = _green_mwi()
    rows[0] = _mwi("钢筋工", "robot", 20.0, "m²/人", "fixed", "area")
    got = check_mwi_completeness(rows)
    assert "mwi_kind_unknown" in _codes(got)


# ======================================================================
# 3. 映射表三档无空档
# ======================================================================


def test_mapping_all_green():
    """三档合法、外键完整、无重复 → 全绿。"""
    got = check_mapping_tiers(
        _green_mapping(), known_l4=["A1", "A2"], known_structures=["S1"])
    assert got["all_green"] is True, got["violations"]
    assert got["stats"]["level_counts"] == {"OPTIONAL": 1, "REQUIRED": 1}


def test_mapping_usual_is_legacy_warning_not_hard_violation():
    """USUAL 是遗留值 → legacy 提示（warning），不算硬违规。"""
    rows = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "USUAL")]
    got = check_mapping_tiers(rows, known_l4=["A1", "A2"], known_structures=["S1"])
    hits = [v for v in got["violations"] if v["code"] == "mapping_level_legacy"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "warning"
    assert hits[0]["detail"]["suggested"] == "OPTIONAL"
    assert got["all_green"] is True


def test_mapping_level_enum_is_three_tiers():
    """三档枚举固定为 REQUIRED/OPTIONAL/EXCLUDED（USUAL 不在其中）。"""
    assert L4_MAPPING_LEVELS == ("REQUIRED", "OPTIONAL", "EXCLUDED")
    assert "USUAL" not in L4_MAPPING_LEVELS


def test_mapping_null_level_is_violation():
    """有行却档位为空/NULL → 硬违规（"无空档"的核心要求）。"""
    for empty in (None, "", "   "):
        rows = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", empty)]
        got = check_mapping_tiers(rows, known_l4=["A1", "A2"], known_structures=["S1"])
        hits = [v for v in got["violations"] if v["code"] == "mapping_level_null"]
        assert len(hits) == 1, empty
        assert hits[0]["severity"] == "error"
        assert got["all_green"] is False


def test_mapping_unknown_level_is_violation():
    """档位落在三档与遗留值之外 → 硬违规。"""
    rows = [_map("S1", "A1", "MAYBE")]
    got = check_mapping_tiers(rows, known_l4=["A1"], known_structures=["S1"])
    assert "mapping_level_unknown" in _codes(got)


def test_mapping_duplicate_rows():
    """同一 (结构类型, L4) 出现多次 → 违规并给出行序。"""
    rows = [_map("S1", "A1", "REQUIRED"), _map("S1", "A1", "OPTIONAL")]
    got = check_mapping_tiers(rows, known_l4=["A1"], known_structures=["S1"])
    hits = [v for v in got["violations"] if v["code"] == "mapping_duplicate_row"]
    assert len(hits) == 1
    assert hits[0]["detail"]["count"] == 2
    assert hits[0]["detail"]["row_indexes"] == [0, 1]


def test_mapping_foreign_key_violations():
    """structure_type_id / activity_id 不在已知集合里 → 违规。"""
    rows = [_map("S9", "A1", "REQUIRED"), _map("S1", "A9", "OPTIONAL")]
    got = check_mapping_tiers(rows, known_l4=["A1"], known_structures=["S1"])
    codes = _codes(got)
    assert "mapping_fk_unknown_structure" in codes
    assert "mapping_fk_unknown_l4" in codes


def test_mapping_fk_skipped_when_sets_not_given():
    """未给 known_* → 跳过外键校验，不误报。"""
    got = check_mapping_tiers([_map("S9", "A9", "REQUIRED")])
    assert "mapping_fk_unknown_structure" not in _codes(got)
    assert "mapping_fk_unknown_l4" not in _codes(got)


def test_mapping_absent_l4_is_counted_not_violation():
    """某 L4 完全没有结构映射行 → 只计数（A3 的诚实留白），不报违规。"""
    rows = [_map("S1", "A1", "REQUIRED")]
    got = check_mapping_tiers(rows, known_l4=["A1", "A2", "A3"], known_structures=["S1"])
    assert got["stats"]["mapping_absent_l4"] == 2
    assert got["all_green"] is True


def test_mapping_l3_zero_coverage_listed():
    """覆盖率为 0 的 L3 必须被列出（A2 要修的对象）。"""
    rows = [_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "OPTIONAL")]
    got = check_mapping_tiers(
        rows,
        known_l4=["A1", "A2", "A3", "A4"],
        known_structures=["S1"],
        l4_to_l3={"A1": "L3_X", "A2": "L3_X", "A3": "L3_Y", "A4": "L3_Y"},
    )
    assert got["stats"]["zero_coverage_l3"] == ["L3_Y"]
    hits = [v for v in got["violations"] if v["code"] == "mapping_l3_zero_coverage"]
    assert len(hits) == 1
    assert hits[0]["detail"]["l3"] == "L3_Y"
    assert got["stats"]["l3_coverage"]["L3_X"]["coverage"] == 1.0
    assert got["stats"]["l3_coverage"]["L3_Y"]["coverage"] == 0.0


def test_mapping_l3_partial_coverage_no_violation():
    """有覆盖但不满 → 只在 stats 里体现，不报违规。"""
    rows = [_map("S1", "A1", "REQUIRED")]
    got = check_mapping_tiers(
        rows, known_l4=["A1", "A2"], known_structures=["S1"],
        l4_to_l3={"A1": "L3_X", "A2": "L3_X"})
    assert got["stats"]["zero_coverage_l3"] == []
    assert got["stats"]["l3_coverage"]["L3_X"]["coverage"] == 0.5


# ======================================================================
# 4. 条件无缺维
# ======================================================================


def test_condition_all_green():
    """条件键精确匹配、维度齐全、取值都能在字典里查到 → 全绿。

    注意：未显式给 condition_keys_used 时，是从 norm_rows 反推**去重**后的键——
    本夹具两行的条件键相同（一个 labor 一个 equipment），故 keys_used == 1。
    """
    dict_rows, norm_rows = _green_conditions()
    got = check_condition_dimensions(dict_rows, norm_rows)
    assert got["all_green"] is True, got["violations"]
    assert got["stats"]["keys_used"] == 1
    assert got["stats"]["matched_keys"] == 1
    assert got["stats"]["norm_tables"] == ["equipment", "labor"]


def test_condition_key_unmatched():
    """D2 精确匹配失败 → 违规并列出该 key。"""
    dict_rows, norm_rows = _green_conditions()
    used = [{"体积": ">1m³", "材料类型": "砖"}, {"体积": ">9m³", "材料类型": "砖"}]
    got = check_condition_dimensions(dict_rows, norm_rows, used)
    hits = [v for v in got["violations"] if v["code"] == "condition_key_unmatched"]
    assert len(hits) == 1
    assert hits[0]["detail"]["condition_key"] == {"体积": ">9m³", "材料类型": "砖"}
    assert "体积=>9m³" in hits[0]["message"]
    assert got["stats"]["matched_keys"] == 1


def test_condition_no_fuzzy_or_subset_match():
    """子集不算精确匹配（禁止模糊匹配）：{体积} ⊂ Norm_行 仍判不匹配。"""
    dict_rows = [{"condition_id": "C1", "condition_type": "体积",
                  "condition_value": ">1m³"}]
    norm_rows = [{"activity_id": "A1",
                  "condition_key": {"体积": ">1m³", "材料类型": "砖"},
                  "table": "labor"}]
    got = check_condition_dimensions(dict_rows, norm_rows, [{"体积": ">1m³"}])
    assert "condition_key_unmatched" in _codes(got)


def test_condition_dim_empty_in_key():
    """条件键里有维取值为空 → 缺维违规。"""
    dict_rows, norm_rows = _green_conditions()
    used = [{"体积": "", "材料类型": "砖"}]
    got = check_condition_dimensions(dict_rows, norm_rows, used)
    hits = [v for v in got["violations"] if v["code"] == "condition_dim_empty"]
    assert len(hits) == 1
    assert "体积" in hits[0]["detail"]["empty_dims"]


def test_condition_empty_key_is_violation():
    """整键为空 → 缺维违规。"""
    dict_rows, norm_rows = _green_conditions()
    got = check_condition_dimensions(dict_rows, norm_rows, [{}])
    hits = [v for v in got["violations"] if v["code"] == "condition_dim_empty"]
    assert len(hits) == 1
    assert hits[0]["detail"]["empty_dims"] == ["<整键为空>"]


def test_condition_dim_unknown_in_dictionary():
    """维度名不在 Condition_Dictionary 的任何维度里 → 违规。"""
    dict_rows, norm_rows = _green_conditions()
    used = [{"体积": ">1m³", "材料类型": "砖", "不存在的维度": "X"}]
    got = check_condition_dimensions(dict_rows, norm_rows, used)
    hits = [v for v in got["violations"] if v["code"] == "condition_dim_unknown"]
    assert len(hits) == 1
    assert hits[0]["detail"]["dimension"] == "不存在的维度"


def test_condition_dim_value_unknown_in_dictionary():
    """维度在字典里但取值不在 → 违规。"""
    dict_rows, norm_rows = _green_conditions()
    got = check_condition_dimensions(
        dict_rows, norm_rows, [{"体积": ">1m³", "材料类型": "钢筋混凝土"}])
    assert "condition_dim_value_unknown" in _codes(got)


def test_condition_dictionary_null_dimension_row():
    """Condition_Dictionary 自己有空维度/空取值行 → 也要报。"""
    got = check_condition_dimensions(
        [{"condition_id": "C9", "condition_type": None, "condition_value": "X"}],
        [], [])
    assert "condition_dict_dim_empty" in _codes(got)


def test_condition_string_condition_key_parsed():
    """condition_key 是 "维度=值;…" 字符串时同样能解析。"""
    dict_rows, norm_rows = _green_conditions()
    norm_rows[0] = {"activity_id": "A1", "condition_key": "体积=>1m³;材料类型=砖",
                    "table": "labor"}
    got = check_condition_dimensions(dict_rows, norm_rows)
    assert got["stats"]["matched_keys"] == 2


def test_condition_json_string_condition_key_parsed():
    """condition_key 是序列化 JSON（真实库 condition_combination 的形态）也能解析。"""
    dict_rows, norm_rows = _green_conditions()
    norm_rows[0] = {
        "activity_id": "A1",
        "condition_key": '{"体积": ">1m³", "材料类型": "砖"}',
        "table": "labor",
    }
    got = check_condition_dimensions(dict_rows, norm_rows)
    assert got["stats"]["matched_keys"] == 2


def test_condition_meta_keys_skipped_for_dictionary_lookup():
    """元数据维度（_source / 构件做法）不参与字典存在性校验。"""
    dict_rows, norm_rows = _green_conditions()
    norm_rows[0] = {
        "activity_id": "A1",
        "condition_key": {"体积": ">1m³", "材料类型": "砖", "_source": "user"},
        "table": "labor",
    }
    got = check_condition_dimensions(dict_rows, norm_rows)
    assert "condition_dim_unknown" not in _codes(got)


def test_condition_no_dictionary_skips_dim_lookup():
    """未给条件字典 → 只做精确匹配校验。"""
    _, norm_rows = _green_conditions()
    got = check_condition_dimensions(None, norm_rows)
    assert got["stats"]["condition_dictionary_rows"] == 0
    assert got["all_green"] is True


# ======================================================================
# 5. 顶层汇总
# ======================================================================


def test_run_all_checks_all_green():
    """四组全绿 → all_green=True，且 checks 顺序固定（注入 l4_to_l3 后无 unknown 警告）。"""
    dict_rows, norm_rows = _green_conditions()
    out = run_all_checks(
        ratio_rows=_green_ratio(),
        mapping_rows=_green_mapping(),
        wbs_landed=[("S1", "A1"), ("S1", "A2")],
        mwi_rows=_green_mwi(),
        known_l4=["A1", "A2"],
        known_structures=["S1"],
        l4_to_l3={"A1": "L3_X", "A2": "L3_X"},
        condition_rows=dict_rows,
        norm_rows=norm_rows,
    )
    assert out["all_green"] is True, out["violations"]
    assert [c["check"] for c in out["checks"]] == [
        "ratio_v1_v4", "mwi_completeness", "mapping_tiers", "condition_dimensions"]
    assert out["violations"] == []
    assert out["warnings"] == []
    assert set(out["stats"]) == {
        "ratio_v1_v4", "mwi_completeness", "mapping_tiers", "condition_dimensions"}


def test_run_all_checks_collects_all_four_groups():
    """四组各有违规时全部被收集，且 group 字段正确。"""
    out = run_all_checks(
        ratio_rows=[_ratio("S1", "A1", 50.0)],
        mapping_rows=[_map("S1", "A1", "EXCLUDED"), _map("S1", "A2", None)],
        wbs_landed=[],
        mwi_rows=[_mwi("钢筋工", "labor", 0.0, "m²/台", "mobile", "area")],
        known_l4=["A1", "A2"],
        known_structures=["S1"],
        condition_rows=[{"condition_id": "C1", "condition_type": "体积",
                         "condition_value": ">1m³"}],
        norm_rows=[{"activity_id": "A1", "condition_key": {"体积": ">1m³"},
                    "table": "labor"}],
        condition_keys_used=[{"体积": ">1m³", "材料类型": "砖"}],
    )
    assert out["all_green"] is False
    assert {v["group"] for v in out["violations"]} == {
        "ratio", "mwi", "mapping", "condition"}
    assert out["warnings"] == [v for v in out["violations"] if v["severity"] == "warning"]


def test_violations_sorted_stably():
    """违规清单排序稳定：按 (group, code, keys, message) 字典序。"""
    got = check_ratio_v1_v4(
        [_ratio("S2", "A1", 10.0), _ratio("S1", "A1", 10.0)],
        [_map("S2", "A1", "EXCLUDED"), _map("S1", "A1", "EXCLUDED")],
    )
    keys = [(v["group"], v["code"], v["keys"]) for v in got["violations"]]
    assert keys == sorted(keys)
    sums = [v for v in got["violations"] if v["code"] == "V1_ratio_sum"]
    assert [v["keys"][0] for v in sums] == ["S1", "S2"]


def test_run_all_checks_is_deterministic():
    """确定性：同输入两次 → 输出逐位一致。"""
    dict_rows, norm_rows = _green_conditions()
    kwargs = dict(
        ratio_rows=[_ratio("S1", "A2", 40.0), _ratio("S1", "A1", 50.0)],
        mapping_rows=[_map("S1", "A2", "USUAL"), _map("S1", "A1", "EXCLUDED")],
        wbs_landed=[("S1", "A1")],
        mwi_rows=_green_mwi() + [_mwi("钢筋工", "labor", 20.0, "m²/人", "mobile", "area")],
        known_l4=["A1", "A2", "A3"],
        known_structures=["S1"],
        l4_to_l3={"A1": "L3_X", "A2": "L3_X", "A3": "L3_Y"},
        condition_rows=dict_rows,
        norm_rows=norm_rows,
        condition_keys_used=[{"体积": ">9m³"}],
    )
    first = run_all_checks(**kwargs)
    second = run_all_checks(**kwargs)
    assert first == second
    assert first["all_green"] is False


def test_run_all_checks_all_green_flag_ignores_warnings():
    """只有 warning 时 all_green 仍为 True（注入 l4_to_l3 后 warning 只剩这两条）。"""
    dict_rows, norm_rows = _green_conditions()
    out = run_all_checks(
        ratio_rows=_green_ratio(),
        mapping_rows=[_map("S1", "A1", "REQUIRED"), _map("S1", "A2", "USUAL")],
        wbs_landed=None,   # → V4_skipped warning
        mwi_rows=_green_mwi(),
        l4_to_l3={"A1": "L3_X", "A2": "L3_X"},
        condition_rows=dict_rows,
        norm_rows=norm_rows,
    )
    assert out["all_green"] is True
    assert {v["code"] for v in out["warnings"]} == {"V4_skipped", "mapping_level_legacy"}


def test_run_all_checks_with_empty_inputs():
    """全空输入：不抛异常，五类缺失报 error，V4 记 skipped。"""
    out = run_all_checks()
    assert out["all_green"] is False
    assert "mwi_mode_empty" in {v["code"] for v in out["violations"]}
    assert "V4_skipped" in {v["code"] for v in out["violations"]}
    assert len(out["checks"]) == 4


def test_mwi_capacity_modes_constant():
    """五类常量与 C1 的 area/position/auxiliary/transport/site 一致。"""
    assert MWI_CAPACITY_MODES == ("area", "position", "auxiliary", "transport", "site")


def test_no_print_no_raise_on_garbage_rows():
    """脏数据（缺键、类型错）不抛异常、不 print，只报违规。"""
    got = check_ratio_v1_v4([{"structure_type_id": None}], [{}, "not-a-row"])
    assert isinstance(got["violations"], list)
    got2 = check_mwi_completeness([{}])
    assert isinstance(got2["violations"], list)
    got3 = check_mapping_tiers([{}], known_l4=["A1"], known_structures=["S1"])
    assert isinstance(got3["violations"], list)
    got4 = check_condition_dimensions([{}], [{}], [None])
    assert isinstance(got4["violations"], list)

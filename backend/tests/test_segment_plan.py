"""施工段划分器测试 — `pipeline/segment_plan.py`（纯逻辑，无库无 LLM）。

运行：cd backend && python -m pytest tests/test_segment_plan.py -q

覆盖《资源与工期计算重构方案 v1》：
  · §4.1 分段验算表（**验收 #1：7 个用例**）
  · §2 裁定 1/2/5/9/11/12（MSSA 单一值、余量弃用、不设上限、用户规则优先、同面积连续层共用）
  · §5 阶段 2「同名兼容 beat_configs.suggest_zones()」
  · §6 验收 #5：unit 贯通 —— 断言不出现 U+33A1 的方块平米符号
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.segment_plan import (MSSA, SEGMENT_IDS, compute_segment_areas,  # noqa: E402
                                   compute_segment_areas_ex, segment_floors,
                                   suggest_zone_count, suggest_zones,
                                   suggest_zones_from_params,
                                   standard_floor_area_from_params)


# ==================== §4.1 分段验算表（7 个用例，验收 #1） ====================

@pytest.mark.parametrize("floor_area,expected", [
    (500.0,  [500.0]),                    # 1 段 500
    (833.0,  [500.0, 333.0]),             # 2 段 500 / 333（本项目标准层 = 15000÷18）
    (1000.0, [500.0, 500.0]),             # 2 段 500 / 500
    (1020.0, [510.0, 510.0]),             # 3→2：余量 20 < 167 → 弃用 MSSA，均匀切
    (1280.0, [500.0, 500.0, 280.0]),      # 3 段 500 / 500 / 280（余量 280 ≥ 167）
    (1500.0, [500.0, 500.0, 500.0]),      # 3 段 500 × 3
    (3000.0, [500.0] * 6),                # 6 段 500 × 6
])
def test_verification_table_4_1(floor_area, expected):
    got = compute_segment_areas(floor_area)
    assert len(got) == len(expected), "段数不符：%r → %r" % (floor_area, got)
    for a, b in zip(got, expected):
        assert a == pytest.approx(b, abs=1e-9), "%r → %r，期望 %r" % (floor_area, got, expected)
    assert sum(got) == pytest.approx(floor_area, abs=1e-9), "段面积之和必须等于层面积"


def test_verification_table_4_1_whole_table_at_once():
    """把 §4.1 的整张验算表一次性对账（防单条参数化被改坏）。"""
    table = [
        (500.0,  "500",              [500.0]),
        (833.0,  "500 / 333",        [500.0, 333.0]),
        (1000.0, "500 / 500",        [500.0, 500.0]),
        (1020.0, "510 / 510",        [510.0, 510.0]),
        (1280.0, "500 / 500 / 280",  [500.0, 500.0, 280.0]),
        (1500.0, "500 × 3",          [500.0, 500.0, 500.0]),
        (3000.0, "500 × 6",          [500.0] * 6),
    ]
    got = {a: compute_segment_areas(a) for a, _, _ in table}
    for area, label, expected in table:
        assert got[area] == pytest.approx(expected, abs=1e-9), \
            "%r 期望 %s，得到 %r" % (area, label, got[area])


# ==================== MSSA 单一值（裁定 5） ====================

def test_mssa_is_single_500_value():
    assert MSSA == 500.0


def test_mssa_override_is_honoured():
    """MSSA 是参数，不是散落魔法数（便于日后再调）。"""
    assert compute_segment_areas(1000.0, mssa=250.0) == [250.0] * 4
    assert compute_segment_areas(1000.0, mssa=2000.0) == [1000.0]


# ==================== 规则留痕 ====================

@pytest.mark.parametrize("floor_area,rule", [
    (300.0,  "mssa_below"),
    (500.0,  "mssa_below"),
    (833.0,  "mssa"),
    (1020.0, "mssa_uniform"),
    (1280.0, "mssa"),
    (3000.0, "mssa"),
])
def test_rule_tag_for_traceability(floor_area, rule):
    _, got_rule, note = compute_segment_areas_ex(floor_area)
    assert got_rule == rule, "%r → %s（%s）" % (floor_area, got_rule, note)
    assert note, "必须留痕说明走了哪条规则"


# ==================== 段数不设上限（裁定 9） ====================

def test_no_segment_count_cap():
    got = compute_segment_areas(5000.0)
    assert len(got) == 10, "5000 m² → 10 段，段数不得封顶（裁定 9）"
    big = compute_segment_areas(30000.0)
    assert len(big) == 60
    assert sum(big) == pytest.approx(30000.0, abs=1e-9)


def test_segment_ids_extend_beyond_roman_ledger():
    """段号台账用完也不崩（SEGMENT_IDS 只有 20 个罗马数字）。"""
    from pipeline.segment_plan import _segment_id
    assert _segment_id(0) == "Ⅰ"
    assert _segment_id(1) == "Ⅱ"
    assert _segment_id(2) == "Ⅲ"
    assert len(SEGMENT_IDS) == 20
    assert _segment_id(25) == "26"


# ==================== 用户显式规则优先（裁定 11） ====================

def test_user_rule_areas_wins_over_mssa():
    got = compute_segment_areas(1200.0, {"areas": [500, 700]})
    assert got == [500.0, 700.0]


def test_user_rule_by_segment_dict():
    got = compute_segment_areas(1200.0, {"Ⅰ": 500, "Ⅱ": 700})
    assert got == [500.0, 700.0]


def test_user_rule_bare_sequence():
    assert compute_segment_areas(900.0, [500.0, 400.0]) == [500.0, 400.0]


def test_user_rule_object_list_and_note():
    got, rule, note = compute_segment_areas_ex(
        900.0, {"segments": [{"area": 300}, {"area": 600}], "note": "甲方指定"})
    assert got == [300.0, 600.0]
    assert rule == "user"
    assert "甲方指定" in note


def test_unparsable_user_rule_falls_back_to_mssa_not_guess():
    """解析不出规则 → 退回 MSSA 规则（**不猜**）。"""
    assert compute_segment_areas(1020.0, {"note": "分三段"}) == [510.0, 510.0]
    assert compute_segment_areas(1020.0, {"areas": []}) == [510.0, 510.0]
    assert compute_segment_areas(1020.0, {"areas": ["abc"]}) == [510.0, 510.0]
    assert compute_segment_areas(1020.0, 42) == [510.0, 510.0]


# ==================== 非法层面积不猜 ====================

@pytest.mark.parametrize("bad", [None, 0, -1, "abc", float("nan"), float("inf"), True])
def test_invalid_floor_area_returns_empty_or_raises(bad):
    assert compute_segment_areas(bad) == []
    with pytest.raises(ValueError):
        compute_segment_areas(bad, strict=True)


# ==================== 裁定 12：按层划分、同面积连续层共用 ====================

def test_consecutive_same_area_floors_share_one_plan():
    groups = segment_floors([1000.0] * 5 + [1020.0] + [1020.0])
    assert len(groups) == 2, "5 个 1000 一组、2 个 1020 一组"
    g1, g2 = groups
    assert g1.floors == [1, 2, 3, 4, 5]
    assert g1.areas() == [500.0, 500.0]
    assert g1.segment_ids() == ["Ⅰ", "Ⅱ"]
    assert g1.rule == "mssa"
    assert g2.floors == [6, 7]
    assert g2.areas() == [510.0, 510.0]
    assert g2.rule == "mssa_uniform"


def test_non_consecutive_same_area_does_not_merge():
    """同面积但**不连续** → 各成一组（裁定 12 说的是"连续层"）。"""
    groups = segment_floors([1000.0, 1280.0, 1000.0])
    assert len(groups) == 3
    assert [g.floors for g in groups] == [[1], [2], [3]]


def test_segment_floors_accepts_floor_number_mapping():
    groups = segment_floors({-2: 2000.0, -1: 2000.0, 1: 833.0})
    assert len(groups) == 2
    assert groups[0].floors == [-2, -1]
    assert groups[0].segment_count == 4          # 2000 → 500 × 4
    assert groups[1].areas() == [500.0, 333.0]


def test_segment_floors_user_rule_applies_to_every_group():
    groups = segment_floors([1000.0, 1000.0, 1200.0], {"areas": [400, 600]})
    assert len(groups) == 2
    for g in groups:
        assert g.areas() == [400.0, 600.0]
        assert g.rule == "user"


def test_segment_floors_skips_invalid_area_without_guessing():
    groups = segment_floors([1000.0, None, 1000.0])
    # None 不并入任何组，所以 1 层与 3 层各自成组（都保留）
    assert len(groups) == 2
    assert [g.floors for g in groups] == [[1], [3]]


def test_floor_group_as_dict_shape_is_stage2_output():
    """§5 阶段 2 声明的输出形状：`{层组: {Ⅰ段: 面积, Ⅱ段: 面积, …}}`。"""
    groups = segment_floors([833.0, 1280.0])
    shaped = {tuple(g.floors): g.as_dict() for g in groups}
    assert shaped[(1,)] == {"Ⅰ": 500.0, "Ⅱ": 333.0}
    assert shaped[(2,)] == {"Ⅰ": 500.0, "Ⅱ": 500.0, "Ⅲ": 280.0}


# ==================== 同名兼容 beat_configs.suggest_zones() ====================

def test_suggest_zones_compat_matches_segment_count():
    for area in (500.0, 833.0, 1000.0, 1020.0, 1280.0, 1500.0, 3000.0, 7777.0):
        assert suggest_zones(area) == len(compute_segment_areas(area))
        assert suggest_zone_count(area) == len(compute_segment_areas(area))


def test_suggest_zones_new_mssa_semantics_differs_from_legacy_bands():
    """口径已换：旧实现四档（833 → 1），新实现按 MSSA（833 → 2）。"""
    assert suggest_zones(833.0) == 2
    assert suggest_zones(750.0) == 2
    assert suggest_zones(499.0) == 1


def test_suggest_zones_none_when_area_unknown():
    """沿用旧契约：取不到面积 → None（不猜，调用方回落配置 zones）。"""
    assert suggest_zones(None) is None
    assert suggest_zones(0) is None
    assert suggest_zones("abc") is None


def test_suggest_zones_from_params_single_building():
    """面积口径必须是"先摊到单栋"（total_area ÷ 栋数 ÷ 层数），否则 12 栋会误判。"""
    one = {"total_area": 15000.0, "floors": 18.0, "buildings": 1}
    twelve = {"total_area": 215000.0, "floors": 38.0, "buildings": 12}
    assert standard_floor_area_from_params(one) == pytest.approx(15000.0 / 18.0)
    assert suggest_zones_from_params(one) == 2          # 833 m² → 2 段
    assert standard_floor_area_from_params(twelve) == pytest.approx(215000.0 / 12 / 38)
    assert suggest_zones_from_params(twelve) == 1       # 471.5 m² → 1 段


def test_suggest_zones_from_params_building_key_aliases():
    base = {"total_area": 24000.0, "floors": 12.0}      # 2000 m²/层 → 4 段
    assert suggest_zones_from_params(base) == 4
    for key in ("buildings", "building_count", "building_num"):
        params = dict(base, **{key: 2})                 # 1000 m²/层 → 2 段
        assert suggest_zones_from_params(params) == 2, key


def test_suggest_zones_from_params_none_when_incomplete():
    assert suggest_zones_from_params({}) is None
    assert suggest_zones_from_params({"floors": 38}) is None
    assert suggest_zones_from_params({"total_area": 15000}) is None
    assert suggest_zones_from_params(None) is None
    assert suggest_zones_from_params("nope") is None


# ==================== §6 验收 #5：单位贯通、无 U+33A1 ====================

def test_no_u33a1_square_metre_symbol_in_output():
    groups = segment_floors([833.0, 1280.0])
    blob = repr([g.to_json() for g in groups])
    assert "\u33a1" not in blob, "输出不得含 U+33A1 方块平米符号"

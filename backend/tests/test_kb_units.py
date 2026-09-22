# -*- coding: utf-8 -*-
"""`pipeline.kb_units` 的验收测试（第 37 轮）。

盯着四件事：
  1. 写法归一（m3→m³、转义台班）；
  2. 产能 = 1/norm_value（**不是** basis/norm_value，不是 1/(basis*norm)）；
  3. 跨族换算必须给工程参数，给不出就是 None（不许 1:1）；
  4. 单位校验默认拒绝："台班"（无分母）必须判 unusable —— 这正是"120 根桩 1 天"的入口。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import kb_units as U  # noqa: E402


# ---------------- 1. 写法归一 ----------------

@pytest.mark.parametrize("raw,expected", [
    ("m3", "m³"), ("M3", "m³"), ("m2", "m²"), ("m³", "m³"),
    ("大", "大"),  # 未知写法原样返回，不猜
    ("\\u53f0\\u73ed", "台班"),
    ("", ""), (None, ""),
])
def test_normalize_unit(raw, expected):
    assert U.normalize_unit(raw) == expected


def test_unit_family_counts_do_not_share_a_family():
    """根 ≠ 块 ≠ 樘：计数类必须各自成族，否则会把"根"当"块"换算。"""
    assert U.unit_family("根") == "count:根"
    assert U.unit_family("块") == "count:块"
    assert U.unit_family("根") != U.unit_family("块")


def test_unit_family_basic_dimensions():
    assert U.unit_family("m3") == "volume"
    assert U.unit_family("m²") == "area"
    assert U.unit_family("t") == "mass"
    assert U.unit_family("工日") == "labor_day"
    assert U.unit_family("台班") == "shift"


# ---------------- 2. 产能口径 ----------------

def test_productivity_is_one_over_norm_not_basis_over_norm():
    """LN_256 实例：原始 0.175 工日/10m² → 落库 norm=0.0175 工日/m²（已归一）。

    正确产能 = 1/0.0175 = 57.14 m²/工日；
    历史上的错误写库 = basis/norm = 10/0.0175 = 571.43（放大 10 倍）。
    """
    p = U.productivity_of(0.0175, raw_quantity_basis=10.0)
    assert p == pytest.approx(57.142857142857146)
    assert p != pytest.approx(571.4285714285714)


def test_productivity_of_basis_one_unchanged():
    """basis=1 的行（2847 行）本来就用 1/norm，修完不许变。"""
    assert U.productivity_of(0.204) == pytest.approx(1.0 / 0.204)


def test_productivity_of_bad_input_returns_none():
    assert U.productivity_of(0) is None
    assert U.productivity_of(-5) is None
    assert U.productivity_of(None) is None
    assert U.productivity_of("abc") is None


# ---------------- 3. parse_norm_unit ----------------

def test_parse_norm_unit_normalized():
    p = U.parse_norm_unit("工日/m³")
    assert p["labor_unit"] == "工日" and p["denominator"] == "m³" and p["scale"] == 1.0


def test_parse_norm_unit_raw_with_scale():
    """原始单位串（带倍率）必须能被识别出来 —— 调用方要报错，而不是拿它去除。"""
    p = U.parse_norm_unit("工日/10m²")
    assert p["denominator"] == "m²" and p["scale"] == 10.0


def test_parse_norm_unit_missing_denominator():
    p = U.parse_norm_unit("台班")
    assert p["labor_unit"] == "台班" and p["denominator"] == "" and p["scale"] == 1.0


# ---------------- 4. 换算 ----------------

def test_convert_same_unit():
    assert U.convert(120, "m³", "m3")[0] == 120


def test_convert_count_to_length_needs_context():
    """120 根 × 18 m/根 = 2160 m：没有桩长就换不出来，必须返回 None。"""
    assert U.convert(120, "根", "m", None) is None
    v, note = U.convert(120, "根", "m", {"pile_length_m": 18.0})
    assert v == pytest.approx(2160.0)
    assert "跨族换算" in note


def test_convert_mass_volume_and_area_volume():
    assert U.convert(10, "t", "m³", {"density_t_per_m3": 2.5})[0] == pytest.approx(4.0)
    assert U.convert(100, "m²", "m³", {"thickness_m": 0.12})[0] == pytest.approx(12.0)


def test_convert_never_falls_back_to_one_to_one():
    """不可换算一律 None —— 这从根上堵住"根当米用"。"""
    assert U.convert(120, "根", "m³") is None
    assert U.convert(120, "块", "根") is None


# ---------------- 5. 单位校验：默认拒绝 ----------------

def test_pile_unit_missing_denominator_is_unusable():
    """KB 只写「台班」时，绝不许用叶子单位把分母补成「台班/根」。"""
    r = U.check_unit_pair("根", "台班")
    assert r["verdict"] == "unusable"
    assert "缺分母" in r["detail"]


def test_pile_with_kb_denominator_is_convertible():
    """KB 分母是 m（NE_PILE_0001：0.53 台班/100m）时：120 根 → 2160 m 才可换。"""
    r = U.check_unit_pair("根", "台班/m", {"pile_length_m": 18.0})
    assert r["verdict"] == "convertible"
    assert r["factor"] == pytest.approx(18.0)

    r2 = U.check_unit_pair("根", "台班/m")          # 没有桩长 → 不可用
    assert r2["verdict"] == "unusable"


def test_same_unit_and_mismatch():
    assert U.check_unit_pair("m³", "工日/m3")["verdict"] == "same"
    bad = U.check_unit_pair("m³", "工日/m²")
    assert bad["verdict"] == "unusable"
    assert "不可换算" in bad["detail"]


def test_unparsable_inputs_are_rejected_not_allowed():
    """旧实现"解析不出来返回 True（宁可不拦）"，这里必须反过来。"""
    assert U.check_unit_pair("", "工日/m³")["verdict"] == "unusable"
    assert U.check_unit_pair("根", "")["verdict"] == "unusable"
    assert U.check_unit_pair(None, None)["verdict"] == "unusable"


def test_raw_unit_string_scale_is_rejected():
    """传进未归一的原始单位串（工日/10m²）要报错，而不是当成 工日/m²。"""
    r = U.check_unit_pair("m²", "工日/10m²")
    assert r["verdict"] == "unusable"
    assert "未归一" in r["detail"]

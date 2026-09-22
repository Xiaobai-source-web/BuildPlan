# -*- coding: utf-8 -*-
"""兜底基线量级 + 物理量级自检 专项测试

背景（真实缺陷，已在真计划 `plan_sample3_after_org_v2` 复现）：
    该计划 `meta.extracted_params` 里 total_rebar / total_concrete / total_earthwork **全为
    null**（上游 boundary 节点模型调用失败），于是整份计划的单层量都退回「基线默认」。
    而旧的地下室基线是写死的 钢筋 2100 t / 模板 18000 ㎡ / 混凝土 8200 m³ **每层**
    （把"整个地下室总量"误当"每层量"），于是叶子 4.1.1.1「1-0.5层 钢筋绑扎」= 2100×0.5
    = 1050 t —— 比全项目钢筋总量还多；组织层据此报「需要 39 个作业面、上限 2 个面」、
    单段排程 135 天（4651 工日）。而同一条任务在标准层只有 22 t / 175 工日。

本测试守两条线：
    A. 兜底基线本身必须是**合理量级**，且能说清怎么推出来的（不再拍脑袋）。
    B. 量级自检必须**留痕**：走「基线默认/混合」且单位面积指标超物理上限时，
       叶子上出现 `_qty_suspect=True` + 非空 `_qty_suspect_reason`；
       **可疑不等于改数** —— 量原样保留。
    C. 参数齐全的「参数推算」路径必须**逐位不变**（这条最重要，它现在是对的）。

运行：python -m pytest tests/test_baseline_qty.py -q -p no:cacheprovider
"""

import copy
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import layer_engine as LE
from pipeline.nodes.beat_configs import (
    BASE_BEAT_CONFIGS,
    BASEMENT_BASELINE_CONCRETE_M3,
    BASEMENT_BASELINE_FORMWORK_M2,
    BASEMENT_BASELINE_REBAR_T,
    BASEMENT_VS_STANDARD_FLOOR,
    FORMWORK_AREA_FACTOR,
    SOURCE_BASE,
    SOURCE_MIXED,
    SOURCE_PARAM,
    SOURCE_RATIO,
    STANDARD_FLOOR_CONCRETE_M3,
    STANDARD_FLOOR_FORMWORK_M2,
    STANDARD_FLOOR_REBAR_T,
    SUSPECT_MAX_CONCRETE_PER_M2,
    SUSPECT_MAX_FORMWORK_PER_M2,
    SUSPECT_MAX_REBAR_PER_M2,
    SUSPECT_BASELINE_TOLERANCE,
    SUSPECT_STEP_METRIC,
    _calc_suspect_reason,
    derive_beat_quantities,
)

PARAMS_FULL = {"total_rebar": 852, "total_concrete": 4260, "total_area": 14200,
               "floors": 18, "building_count": 1}
PARAMS_EMPTY = {}


# ------------------------------------------------------------------ 工具
def _derive(name, params):
    return derive_beat_quantities(copy.deepcopy(BASE_BEAT_CONFIGS[name]), params)


def _step(cfg, name):
    for s in (cfg.get("cycle") or []):
        if s["name"] == name:
            return s
    raise AssertionError("没有该工序：%s" % name)


def _qty(cfg, name):
    return float(_step(cfg, name)["qty_per_floor"])


def _detail(note, name):
    d = (note.get("detail") or {}).get(name)
    assert d, "detail 缺工序 %s" % name
    return d


def _leaves(ph):
    return [l for wp in ph["work_packages"] for l in wp["sub_packages"]]


def _first_leaf(ph, step_name):
    for l in _leaves(ph):
        if l.get("_step_name") == step_name:
            return l
    raise AssertionError("没有 %s 叶子" % step_name)


# ==================================================================
# A. 兜底基线（参数缺失）自身的量级合理性
# ==================================================================
def test_basement_baseline_is_derived_from_standard_floor():
    """兜底值必须能由**地上标准层锚点 × 部位系数**推出来（不是新拍的数字）。

    推导依据（见 beat_configs.py「【1】参数缺失时的兜底基线值」）：
        钢筋   = 1900 ㎡ × 28.9 kg/㎡ ≈ 55 t
        混凝土 = 180 m³ × 2.0        = 360 m³
        模板   = 1900 ㎡ × 2.5        = 4750 ㎡（与 FORMWORK_AREA_FACTOR 同源）
    """
    assert BASEMENT_BASELINE_REBAR_T == pytest.approx(
        STANDARD_FLOOR_REBAR_T * BASEMENT_VS_STANDARD_FLOOR["rebar"])
    assert BASEMENT_BASELINE_CONCRETE_M3 == pytest.approx(
        STANDARD_FLOOR_CONCRETE_M3 * BASEMENT_VS_STANDARD_FLOOR["concrete"])
    assert BASEMENT_BASELINE_FORMWORK_M2 == pytest.approx(
        STANDARD_FLOOR_FORMWORK_M2 * BASEMENT_VS_STANDARD_FLOOR["formwork"])


def test_standard_floor_anchor_matches_above_ground_config():
    """锚点常量必须与 `地上主体结构` 配置一致 —— 否则「×2~3 倍」的推导依据就失效了。"""
    cfg = BASE_BEAT_CONFIGS["地上主体结构"]
    assert _qty(cfg, "钢筋绑扎") == STANDARD_FLOOR_REBAR_T == 22.0
    assert _qty(cfg, "铝模安装") == STANDARD_FLOOR_FORMWORK_M2 == 1900.0
    assert _qty(cfg, "混凝土浇筑") == STANDARD_FLOOR_CONCRETE_M3 == 180.0


def test_basement_and_above_ground_are_within_same_order_of_magnitude():
    """本文件自己的工程口径：地下室约为地上标准层的 2~3 倍（不是 10~100 倍）。"""
    b = (BASEMENT_BASELINE_REBAR_T / STANDARD_FLOOR_REBAR_T,
         BASEMENT_BASELINE_CONCRETE_M3 / STANDARD_FLOOR_CONCRETE_M3,
         BASEMENT_BASELINE_FORMWORK_M2 / STANDARD_FLOOR_FORMWORK_M2)
    for name, ratio in zip(("钢筋", "混凝土", "模板"), b):
        assert 2.0 <= ratio <= 3.0, "地下室%s是地上标准层的 %.2f 倍，越出 2~3 倍口径" % (name, ratio)


def test_basement_baseline_rebar_intensity_is_engineering_sane():
    """含钢量反算：55 t ÷ 1900 ㎡ ≈ 28.9 kg/㎡（地下室底板+墙柱正常 25~30）。"""
    kg_per_m2 = BASEMENT_BASELINE_REBAR_T * 1000.0 / STANDARD_FLOOR_FORMWORK_M2
    assert 25.0 <= kg_per_m2 <= 30.0, kg_per_m2

    # 与混凝土对账：55 t ÷ 360 m³ ≈ 153 kg/m³（底板/墙体正常 120~180）
    kg_per_m3 = BASEMENT_BASELINE_REBAR_T * 1000.0 / BASEMENT_BASELINE_CONCRETE_M3
    assert 120.0 <= kg_per_m3 <= 180.0, kg_per_m3

    # 模板与混凝土对账：4750 ㎡ ÷ 360 m³ ≈ 13.2 ㎡/m³（地下室厚板+墙正常量级）
    m2_per_m3 = BASEMENT_BASELINE_FORMWORK_M2 / BASEMENT_BASELINE_CONCRETE_M3
    assert 8.0 <= m2_per_m3 <= 20.0, m2_per_m3


def test_params_empty_basement_quantities_in_reasonable_band():
    """参数缺失（params={}）→ 地下室三个单层量落在合理区间（**断言数字与理由**）。

    区间口径（每条都留 2~3 倍余量，只拦"物理上不可能"）：
      钢筋   20~80 t/层   ：1900 ㎡ 的地下一层，含钢量 10~42 kg/㎡；
                            旧值 2100 t（1105 kg/㎡）必然出界。
      混凝土 200~600 m³/层：折合厚度 0.1~0.3 m，符合顶板+底板/墙柱分摊；
                            旧值 8200 m³（4.3 m³/㎡）必然出界。
      模板   3000~6000 ㎡/层：模板接触系数 1.6~3.2 倍建筑面积（本文件取 2.5）；
                            旧值 18000 ㎡（9.5 倍）必然出界。
    """
    cfg, note = _derive("地下室结构", PARAMS_EMPTY)
    assert note["source"] == SOURCE_BASE
    for step, want in zip(cfg["cycle"],
                          (BASEMENT_BASELINE_REBAR_T, BASEMENT_BASELINE_FORMWORK_M2,
                           BASEMENT_BASELINE_CONCRETE_M3)):
        assert float(step["qty_per_floor"]) == want, step["name"]

    rebar = _qty(cfg, "钢筋绑扎")
    concrete = _qty(cfg, "混凝土浇筑")
    formwork = _qty(cfg, "模板安装")

    assert 20.0 <= rebar <= 80.0, rebar
    assert 200.0 <= concrete <= 600.0, concrete
    assert 3000.0 <= formwork <= 6000.0, formwork

    # 与「地上标准层 22 t」同量级：不再是 95 倍
    assert rebar / 22.0 <= 3.0
    # 旧写死值必须彻底消失
    for old in (2100.0, 8200.0, 18000.0):
        assert old not in (rebar, concrete, formwork)


def test_params_empty_basement_formwork_equals_area_factor():
    """模板兜底值必须与面积类计算器**同源**（FORMWORK_AREA_FACTOR），不是另一套数。

    没有 total_area → 模板推不出来 → 退回基线；基线的推导依据 = 1900 ㎡ × 2.5，
    与 `_calc_formwork`（单栋标准层面积 × 2.5）同一口径，这样"缺参数"与"有参数"
    两种情形不会给出量级互斥的答案。
    """
    assert BASEMENT_BASELINE_FORMWORK_M2 == pytest.approx(
        STANDARD_FLOOR_FORMWORK_M2 * FORMWORK_AREA_FACTOR)

    # 反向对账：参数齐全时 `_calc_formwork` 算出来的就是「面积 × 2.5 ÷ 分区数」，
    # 与兜底值同源。取一整栋 1900 ㎡ 单区 → 结果应与兜底值逐位相同。
    cfg, note = _derive("地下室结构", {"total_area": 1900 * 2, "floors": 2,
                                       "building_count": 1})
    # 标准层面积 = 3800 ÷ 1 ÷ 2 = 1900 ㎡；分区由面积建议 → 2 区 → 950 ㎡ × 2.5 = 2375
    d = _detail(note, "模板安装")
    assert d["source"] == SOURCE_PARAM
    zones = LE._effective_zones_count(BASE_BEAT_CONFIGS["地下室结构"],
                                      {"total_area": 1900 * 2, "floors": 2,
                                       "building_count": 1})
    assert d["qty_per_floor"] == pytest.approx(
        STANDARD_FLOOR_FORMWORK_M2 * FORMWORK_AREA_FACTOR / zones, rel=1e-4, abs=0.05)


def test_params_missing_is_not_silent_every_step_has_source():
    """绝不静默：每一道工序都要有可机读的来源与（基线时的）说明渠道。"""
    for name in BASE_BEAT_CONFIGS:
        cfg, note = _derive(name, PARAMS_EMPTY)
        for step in cfg["cycle"]:
            d = _detail(note, step["name"])
            assert d["source"] == SOURCE_BASE, (name, step["name"], d["source"])
            # detail 里带机读的 suspect / suspect_reason（叶子上的键名是 _qty_suspect*）
            assert "suspect" in d and "suspect_reason" in d, (name, step["name"])


# ==================================================================
# B. 量级自检：可疑必须留痕，且**不改数**
# ==================================================================
def test_old_basement_values_would_be_flagged_if_they_came_back():
    """把旧写死值放回配置 → 自检必须命中，且量**原样保留**（绝不静默改数）。"""
    cfg = copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"])
    for s in cfg["cycle"]:
        if s["name"] == "钢筋绑扎":
            s["qty_per_floor"] = 2100.0
    derived, note = derive_beat_quantities(cfg, PARAMS_EMPTY)
    step = _step(derived, "钢筋绑扎")
    assert step["qty_per_floor"] == 2100.0, "可疑不等于改数：量必须原样保留"
    assert step.get("_qty_suspect") is True
    assert step.get("_qty_suspect_reason")
    # detail 里同样要机读可查
    assert _detail(note, "钢筋绑扎")["suspect"] is True
    assert _detail(note, "钢筋绑扎")["suspect_reason"]


def test_physically_impossible_leaf_is_marked_with_reason():
    """构造一条"每层量物理不可能"的叶子 → `_qty_suspect is True` 且 reason 非空。"""
    cfg = copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"])
    for s in cfg["cycle"]:
        if s["name"] == "钢筋绑扎":
            s["qty_per_floor"] = 2100.0        # 1.11 t/㎡（上限 0.15）

    # ⚠️ 必须带 total_area：自检的量纲归一要用**单栋标准层面积**。真计划里 total_area=14200
    # 是有的（缺的只是 total_rebar/total_concrete），所以这里用同样的口径更贴近现实。
    params = {"total_area": 14200, "floors": 18, "building_count": 1}
    ph, _ids = LE.expand_node(cfg, params)
    leaf = _first_leaf(ph, "钢筋绑扎")

    assert leaf["quantity"] == pytest.approx(1050.0)   # 2100 × 0.5 层，原样铺下去
    assert leaf["_qty_source"] == SOURCE_BASE
    assert leaf["_qty_suspect"] is True
    assert leaf["_qty_suspect_reason"]
    # 原因必须是人话且带数字：量、单位指标、上限、折算用的标准层面积
    reason = leaf["_qty_suspect_reason"]
    assert "钢筋" in reason and "上限" in reason
    assert "2100" in reason and "0.15" in reason
    assert "789" in reason, "必须说明用哪个标准层面积折算的（14200÷18≈789㎡）：%s" % reason
    assert "t/㎡/㎡" not in reason, "单位不能出现重复斜杠：%s" % reason


def test_concrete_and_formwork_over_limit_are_marked_too():
    """混凝土与模板超限同样要标记（旧值 8200 m³ / 18000 ㎡ 都要命中）。"""
    cfg = copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"])
    for s in cfg["cycle"]:
        if s["name"] == "混凝土浇筑":
            s["qty_per_floor"] = 8200.0
        if s["name"] == "模板安装":
            s["qty_per_floor"] = 18000.0
    derived, _note = derive_beat_quantities(cfg, PARAMS_EMPTY)
    assert _step(derived, "混凝土浇筑").get("_qty_suspect") is True
    assert _step(derived, "模板安装").get("_qty_suspect") is True


def test_normal_baseline_is_not_flagged():
    """正常量级**不会**被误标 —— 这是自检能不能上线的底线。"""
    for name in BASE_BEAT_CONFIGS:
        cfg, note = _derive(name, PARAMS_EMPTY)
        for step in cfg["cycle"]:
            assert step.get("_qty_suspect") is None, (name, step["name"])
            assert _detail(note, step["name"])["suspect"] is False, (name, step["name"])

    # 铺到叶子上：字段恒存在且为 False、reason 为 ""
    ph, _ids = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), PARAMS_EMPTY)
    for leaf in _leaves(ph):
        assert leaf["_qty_suspect"] is False, leaf["id"]
        assert leaf["_qty_suspect_reason"] == ""


def test_big_numbers_never_flagged_on_ratio_or_param_path():
    """占比表 / 参数推算出来的量**不参与**自检、不得被标记。

    自检只对「走基线」的那几步负责：按用户参数（或占比表）忠实换算出来的量，
    标它"可疑"只会误导用户。
    """
    big = {"total_rebar": 99999, "total_concrete": 99999, "total_area": 999999,
           "floors": 38, "building_count": 1}
    cfg, note = _derive("地下室结构", big)
    for step in cfg["cycle"]:
        # 无结构类型/无占比表 ⇒ 体积类退回基线；面积类走参数推算
        assert _detail(note, step["name"])["source"] in (SOURCE_BASE, SOURCE_PARAM)
    # 有占比表时：量由占比表给出，也不得被标记为"可疑"
    p = dict(big)
    p.update({"structure_type": "frame_shear",
              "l4_quantities": {"CONC_NEW_FOUND": 99999 * 0.171,
                                "REBAR_NEW_FOUND": 99999 * 0.155,
                                "FORM_NEW_FOUND": 99999 * 0.14},
              "_component_ratio": {"structure_type_id": "frame_shear", "l4_index": {
                  "CONC_NEW_FOUND": {"structure_type_id": "frame_shear",
                                     "activity_id": "CONC_NEW_FOUND",
                                     "work_type_id": "concrete", "ratio_percent": 17.1,
                                     "quantity": 99999 * 0.171, "unit": "m³",
                                     "confidence": "LOW", "review_state": "pending",
                                     "notes": ""}}}})
    cfg2, note2 = _derive("地下室结构", p)
    for step in cfg2["cycle"]:
        if _detail(note2, step["name"])["source"] == SOURCE_RATIO:
            assert step.get("_qty_suspect") is None, step["name"]
            assert _detail(note2, step["name"])["suspect"] is False


def test_mixed_source_flags_only_the_baseline_step():
    """「混合」阶段：只查走基线的那一步，推算出来的那一步不背锅。

    参数：给了 total_area（模板能推）、没给 total_rebar/total_concrete（钢筋/混凝土走基线）。
    再人为把走基线的钢筋基线改成不可能值 → 只有钢筋被标。
    """
    cfg = copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"])
    for s in cfg["cycle"]:
        if s["name"] == "钢筋绑扎":
            s["qty_per_floor"] = 2100.0
    params = {"total_area": 14200, "floors": 18, "building_count": 1}

    derived, note = derive_beat_quantities(cfg, params)
    assert note["source"] == SOURCE_MIXED
    assert _detail(note, "模板安装")["source"] == SOURCE_PARAM
    assert _detail(note, "模板安装")["suspect"] is False
    assert _detail(note, "钢筋绑扎")["source"] == SOURCE_BASE
    assert _detail(note, "钢筋绑扎")["suspect"] is True
    assert _step(derived, "钢筋绑扎").get("_qty_suspect") is True


def test_suspect_reason_helpers_directly():
    """`_calc_suspect_reason` 的边界：没面积时退到典型标准层、等于上限不判、超限给中文原因。"""
    over = {"name": "钢筋绑扎", "unit": "t", "qty_per_floor": 2100.0}
    normal = {"name": "钢筋绑扎", "unit": "t", "qty_per_floor": 55.0}
    # 缺参数（面积取不到）→ 退到 1900 ㎡ 典型标准层：2100/1900 = 1.11 > 0.15 → 命中
    reason = _calc_suspect_reason(over, {})
    assert reason and "1.11" in reason and "0.15" in reason
    assert "t/㎡/㎡" not in reason, "单位不能出现重复斜杠：%s" % reason
    # 面积齐全时按真实标准层面积判：2100/100 = 21 > 0.15 → 命中
    reason2 = _calc_suspect_reason(over, {"total_area": 3800, "floors": 38})
    assert reason2 and "21" in reason2
    # 正常量级（兜底基线本身）→ 不判
    assert _calc_suspect_reason(normal, 1900.0) is None
    assert _calc_suspect_reason(normal, None) is None
    # 未被登记的工序（不在 SUSPECT_STEP_METRIC）→ 不判
    assert _calc_suspect_reason({"name": "砌块墙", "unit": "m³",
                                 "qty_per_floor": 99999.0}, 100.0) is None
    # 恰好等于物理上限不算超：120 t ÷ 800 ㎡ = 0.15 t/㎡。
    # ⚠️ 必须选 > 宽容闸（55×1.5 = 82.5 t）的量，否则会被"已知良好值不判"直接放行、
    # 走不到物理上限这一步。
    exact = {"name": "钢筋绑扎", "unit": "t", "qty_per_floor": 0.15 * 800}
    assert exact["qty_per_floor"] == 120.0
    assert exact["qty_per_floor"] > BASEMENT_BASELINE_REBAR_T * SUSPECT_BASELINE_TOLERANCE
    # 120 t ÷ 800 ㎡ = 0.15 → 恰好等于上限，不判
    assert _calc_suspect_reason(exact, {"total_area": 800, "floors": 1}) is None
    # 抬一点就命中（0.1501 只用来判，展示值会舍入成 0.15）
    assert _calc_suspect_reason(dict(exact, qty_per_floor=120.08),
                                {"total_area": 800, "floors": 1})
    # 宽容闸：跌回兜底基线（哪怕略高）不判 —— 文档化基线不需要自证
    tol = BASEMENT_BASELINE_REBAR_T * SUSPECT_BASELINE_TOLERANCE
    assert _calc_suspect_reason({"name": "钢筋绑扎", "unit": "t",
                                 "qty_per_floor": tol}, {}) is None
    assert _calc_suspect_reason({"name": "钢筋绑扎", "unit": "t",
                                 "qty_per_floor": tol + 1.0},
                                {"total_area": 3800, "floors": 38}) is not None


def test_suspect_thresholds_are_physical_not_merely_typical():
    """阈值必须是"物理不可能"的量级：正常工程实践值要留在阈值以内（宁可漏报）。"""
    # 住宅标准层含钢量 10~20 kg/㎡，厚筏板 100~150 kg/㎡
    assert SUSPECT_MAX_REBAR_PER_M2 >= 0.15
    assert SUSPECT_MAX_CONCRETE_PER_M2 >= 1.0      # 厚筏板 ~1.0 m³/㎡
    assert SUSPECT_MAX_FORMWORK_PER_M2 >= FORMWORK_AREA_FACTOR   # 不能比本文件的系数还紧
    assert set(SUSPECT_STEP_METRIC) >= {"钢筋绑扎", "混凝土浇筑", "模板安装"}
    # 兜底基线本身必须全部在物理上限以内（否则正常配置会被自己的自检误标）
    assert BASEMENT_BASELINE_REBAR_T / STANDARD_FLOOR_FORMWORK_M2 < SUSPECT_MAX_REBAR_PER_M2
    assert BASEMENT_BASELINE_CONCRETE_M3 / STANDARD_FLOOR_FORMWORK_M2 < \
        SUSPECT_MAX_CONCRETE_PER_M2
    # 模板基线恰好"接触系数"倍：2.5 < 4.0
    assert BASEMENT_BASELINE_FORMWORK_M2 / STANDARD_FLOOR_FORMWORK_M2 < \
        SUSPECT_MAX_FORMWORK_PER_M2


# ==================================================================
# C. 「参数推算」路径逐位不变（最重要）
# ==================================================================
# 「参数推算」分支的**冻结参照值**（用 `python -c` 实跑 derive_beat_quantities(PARAMS_FULL)
# 取到，并与配置占比公式逐位核对过）。这些数字来自配置占比公式本身
# （total_rebar×10%÷2层÷zones 之类），与本次改动的 BASEMENT_BASELINE_* 常量无关，
# 因此可以作为"逐位不变"的参照：只要它们变了，就说明参数齐全路径被改坏了。
#
# ⚠️ 2026-09-21 B2（分区口径换成 MSSA=500 m²，方案 §4.1）后**整组数字减半**：
#    PARAMS_FULL 的标准层面积 = 14200 ÷ 1 ÷ 18 ≈ 788.9 m² → 旧四档口径 1 个分区，
#    新口径 `ceil(788.9 ÷ 500) = 2`、余量 288.9 ≥ 500/3 → **2 个分区**。
#    体积类除以 zones → 量减半；面积类（模板）同样除以 zones。分区数变了不是"改坏了"，
#    是 B2 的预期行为替换（详见 W3-A 报告）。zones 相关的断言仍写在 `test_full_params_
#    formula_text_unchanged` 里，用 `zones` 变量而非硬编码。
BEFORE_FULL_PARAMS = {
    "地下室结构": {"钢筋绑扎": 21.3, "混凝土浇筑": 266.25, "模板安装": 986.11},
    "地上主体结构": {"钢筋绑扎": 18.93, "混凝土浇筑": 71.0, "铝模安装": 986.11},
}


def test_full_params_without_ratio_table_falls_back_to_baseline():
    """**行为变化（2026-09-21）**：体积类（钢筋/混凝土）的量不再由「阶段占比 ÷ 阶段层数」产生。

    旧口径 `BEFORE_FULL_PARAMS` 冻的是 `total_rebar × REBAR_RATIO[阶段] ÷ 层数 ÷ 分区数`；
    那张**按施工阶段**的比例表已整体退役（用户裁定：`Component_Ratio` 做唯一真源）。
    于是"只给 total_rebar/total_concrete、不给 structure_type"这一场景下，
    体积类如实退回**基线默认**并标源（缺占比表 ⇒ 不猜），不再是 21.3 / 266.25。
    """
    cfg, note = _derive("地下室结构", PARAMS_FULL)
    assert _detail(note, "钢筋绑扎")["source"] == SOURCE_BASE
    assert _qty(cfg, "钢筋绑扎") == BASEMENT_BASELINE_REBAR_T      # 55
    assert _detail(note, "混凝土浇筑")["source"] == SOURCE_BASE
    assert _qty(cfg, "混凝土浇筑") == BASEMENT_BASELINE_CONCRETE_M3  # 360
    # 面积类仍然按参数推算（与接线前逐位相同）
    assert _detail(note, "模板安装")["source"] == SOURCE_PARAM


def test_full_params_formula_text_unchanged():
    """面积类公式文本保持中文口径（÷分区数），且与量一一对应。

    体积类的公式文本已改为占比表口径（`占比表拆分：… Component_Ratio …`），
    见 `test_qty_derive.py::test_full_params_ratio_table_drives_qty`。
    """
    zones = LE._effective_zones_count(BASE_BEAT_CONFIGS["地下室结构"], PARAMS_FULL)
    cfg, note = _derive("地下室结构", PARAMS_FULL)

    r = _detail(note, "钢筋绑扎")
    assert r["source"] == SOURCE_BASE
    assert r["qty_per_floor"] == BASEMENT_BASELINE_REBAR_T          # = 55 t/层（兜底基线）

    c = _detail(note, "混凝土浇筑")
    assert c["source"] == SOURCE_BASE
    assert c["qty_per_floor"] == BASEMENT_BASELINE_CONCRETE_M3      # = 360 m³/层

    f = _detail(note, "模板安装")
    assert f["source"] == SOURCE_PARAM
    assert f["formula"] == ("单栋标准层788.89m²×2.5（模板接触面积系数）÷%d区 = %s m²/层"
                            % (zones, round(14200 / 1 / 18 * FORMWORK_AREA_FACTOR / zones, 2)))
    assert f["qty_per_floor"] == round(14200 / 1 / 18 * FORMWORK_AREA_FACTOR / zones, 2)


def test_full_params_settles_the_real_plan_regression():
    """真实计划回归：1050 t / 4651 工日 这类数字不可能再出现。

    参数齐全时 4.1.1.1（地下室 1-0.5 层 钢筋绑扎）的量 = 单层量 × 0.5。
    """
    cfg, _note = _derive("地下室结构", PARAMS_FULL)
    per_floor = _qty(cfg, "钢筋绑扎")
    ph, _ids = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), PARAMS_FULL)
    leaf = _first_leaf(ph, "钢筋绑扎")
    assert leaf["quantity"] == pytest.approx(per_floor * 0.5, rel=1e-6, abs=0.5)
    assert leaf["quantity"] < 1050.0, "旧缺陷值 1050 t 不得复现"
    assert leaf["_qty_suspect"] is False


def test_params_missing_real_plan_scale_is_sane():
    """参数缺失 + 真计划的体量（14200 ㎡ / 18 层）→ 4.1.1.1 不再是 1050 t。

    这是本任务的核心验收：即使 total_rebar/total_concrete 全为 null，
    叶子量也必须是"一栋一层的一小半"这个量级。
    """
    params = {"total_area": 14200, "floors": 18, "building_count": 1,
              "total_rebar": None, "total_concrete": None, "total_earthwork": None}
    cfg, note = _derive("地下室结构", params)
    assert _detail(note, "钢筋绑扎")["source"] == SOURCE_BASE

    ph, _ids = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), params)
    leaf = _first_leaf(ph, "钢筋绑扎")
    assert leaf["_qty_source"] == SOURCE_BASE
    assert leaf["quantity"] == pytest.approx(28.0)   # 55 t × 0.5 层 = 27.5 → 取整 28
    assert leaf["quantity"] <= 30.0, "地下室半层钢筋不得再超过 30 t"
    assert leaf["duration_days"] <= 3, leaf["duration_days"]
    # 同一条任务在地上标准层是 22 t —— 两者必须同量级
    main_ph, _ids2 = LE.expand_node(
        copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), params)
    main_leaf = _first_leaf(main_ph, "钢筋绑扎")
    assert 0.5 <= leaf["quantity"] / main_leaf["quantity"] <= 2.0


def test_leaf_always_carries_suspect_fields():
    """叶子字段恒存在（消费方无需判 None）；无论走哪条分支。"""
    for params in (PARAMS_EMPTY, PARAMS_FULL):
        ph, _ids = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), params)
        for leaf in _leaves(ph):
            assert "_qty_suspect" in leaf and isinstance(leaf["_qty_suspect"], bool)
            assert "_qty_suspect_reason" in leaf

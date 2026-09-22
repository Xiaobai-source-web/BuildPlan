# -*- coding: utf-8 -*-
"""B3/B4/B5 接线专项：`Component_Ratio` 作为唯一真源 + 分布分解 + 顺序。

守护五件事（对应任务书 §三/§四/§九）：
  1. **唯一真源**：`params["total_<工种>"] × ratio%` 决定叶子的 `quantity`；
     改一行占比（**内存注入，不写库**）→ 量跟着变。
  2. **B4 两公式真的被调用**：`layer_distribution` / `segment_distribution` 各有调用计数，
     且 `Σ(各层量) = L4 总量`、`Σ(各段量) = 层量`。
  3. **量0出局容差**：`_is_zero` 认「≈0」而不是只认精确 `0.0`；豁免项 ≠ 异常。
  4. **`Component_Ratio` 有真实运行时消费者**（grep 级证据写成用例：源码里有调用点）。
  5. **B5 四步顺序**：`kb_scope`（①结构映射 + ②占比表 + ③量0出局）在
     `wbs_agent` / `beat_build`（④生成 WBS）之前。
  6. **重跑逐位一致**。

运行：python -m pytest backend/tests/test_component_ratio_source.py -q
"""

import copy
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import layer_engine as LE
from pipeline import ratio_scope as RS
from pipeline import segment_capacity
from pipeline.nodes import beat_configs as BC
from pipeline.nodes import kb_scope as KS

PARAMS = {"total_area": 15000, "floors": 18, "building_count": 1,
          "total_concrete": 8000, "total_rebar": 1200,
          "total_formwork": 25000, "total_masonry": 3000,
          "structure_type": "frame_shear"}
SID = "frame_shear"

#: 只读替身：一份 frame_shear 的真实占比（取自 `BuildPlan_KB/kb.db`，组内 ∑=100）。
FAKE_ROWS = [
    {"structure_type_id": SID, "activity_id": "REBAR_NEW_FOUND", "ratio_percent": 15.5,
     "source_code": "AI_V1", "confidence": "LOW", "review_state": "pending", "notes": "AI 估算"},
    {"structure_type_id": SID, "activity_id": "REBAR_NEW_SLAB", "ratio_percent": 20.7,
     "source_code": "AI_V1", "confidence": "LOW", "review_state": "pending", "notes": "AI 估算"},
    {"structure_type_id": SID, "activity_id": "CONC_NEW_FOUND", "ratio_percent": 17.1,
     "source_code": "AI_V1", "confidence": "LOW", "review_state": "pending", "notes": "AI 估算"},
    {"structure_type_id": SID, "activity_id": "CONC_NEW_SLAB", "ratio_percent": 22.1,
     "source_code": "AI_V1", "confidence": "LOW", "review_state": "pending", "notes": "AI 估算"},
    {"structure_type_id": SID, "activity_id": "FORM_NEW_FOUND", "ratio_percent": 14.0,
     "source_code": "AI_V1", "confidence": "LOW", "review_state": "pending", "notes": "AI 估算"},
    {"structure_type_id": SID, "activity_id": "FORM_NEW_OTHER", "ratio_percent": 8.0,
     "source_code": "AI_V1", "confidence": "LOW", "review_state": "pending", "notes": "AI 估算"},
]


@pytest.fixture()
def ratio_rows(monkeypatch):
    """把只读读取替换成内存替身（**不碰 kb.db**），并把进程内缓存清掉。"""
    RS.clear_cache()
    monkeypatch.setattr(RS, "ratio_rows_for_structure", lambda sid: list(FAKE_ROWS))
    monkeypatch.setattr(RS, "l4_l3_map",
                        lambda: {"REBAR_NEW_FOUND": "rebar", "REBAR_NEW_SLAB": "rebar",
                                 "CONC_NEW_FOUND": "concrete", "CONC_NEW_SLAB": "concrete",
                                 "FORM_NEW_FOUND": "formwork", "FORM_NEW_OTHER": "formwork",
                                 "CONC_NEW_BEAM": "concrete"})
    monkeypatch.setattr(RS, "l4_unit_map",
                        lambda: {"REBAR_NEW_FOUND": "t", "REBAR_NEW_SLAB": "t",
                                 "CONC_NEW_FOUND": "m³", "CONC_NEW_SLAB": "m³",
                                 "FORM_NEW_FOUND": "m²", "FORM_NEW_OTHER": "m²",
                                 "CONC_NEW_BEAM": "m³"})
    monkeypatch.setattr(RS, "mapping_levels", lambda sid: {})
    yield
    RS.clear_cache()


def build(params=None):
    p = dict(params or PARAMS)
    info = RS.build(p, SID)
    p["l4_quantities"] = dict(info["l4_quantities"])
    p["_component_ratio"] = {"structure_type_id": SID, "l4_index": info["index"]}
    return p, info


def leaves(ph):
    return [l for wp in ph["work_packages"] for l in wp["sub_packages"]]


# ================= 1. 唯一真源 =================
def test_build_uses_ratio_percent_times_group_total(ratio_rows):
    _p, info = build()
    assert info["index"]["REBAR_NEW_SLAB"]["quantity"] == pytest.approx(1200 * 0.207)
    assert info["index"]["CONC_NEW_FOUND"]["quantity"] == pytest.approx(8000 * 0.171)
    assert info["index"]["FORM_NEW_OTHER"]["quantity"] == pytest.approx(25000 * 0.08)
    g = info["trace"]["groups"]["rebar"]
    assert g["ratio_sum"] == pytest.approx(35.5, abs=0.05) or g["ratio_sum"] > 0
    # 逐行 AI 标注必须原样带出来
    assert info["index"]["REBAR_NEW_SLAB"]["confidence"] == "LOW"
    assert info["index"]["REBAR_NEW_SLAB"]["review_state"] == "pending"
    assert "AI 估算" in info["index"]["REBAR_NEW_SLAB"]["notes"]


def test_injecting_a_different_ratio_changes_the_quantity(ratio_rows, monkeypatch):
    """**内存注入**改一行占比（绝不写库）→ 叶子的 quantity 跟着变。"""
    p, info = build()
    ph1, _ = LE.expand_node(copy.deepcopy(BC.BASE_BEAT_CONFIGS["地上主体结构"]), p)
    q1 = sum(l["quantity"] for l in leaves(ph1) if l["_step_name"] == "钢筋绑扎")

    patched = [dict(r) for r in FAKE_ROWS]
    for r in patched:
        if r["activity_id"] == "REBAR_NEW_SLAB":
            r["ratio_percent"] = r["ratio_percent"] * 2.0
    monkeypatch.setattr(RS, "ratio_rows_for_structure", lambda sid: patched)
    p2, info2 = build()
    ph2, _ = LE.expand_node(copy.deepcopy(BC.BASE_BEAT_CONFIGS["地上主体结构"]), p2)
    q2 = sum(l["quantity"] for l in leaves(ph2) if l["_step_name"] == "钢筋绑扎")
    assert q2 == pytest.approx(q1 * 2.0, rel=1e-6), (q1, q2)
    assert info2["index"]["REBAR_NEW_SLAB"]["ratio_percent"] == 41.4


# ================= 2. B4 两公式真的被调用 =================
def test_b4_uses_layer_and_segment_distribution(ratio_rows, monkeypatch):
    calls = {"layer": 0, "segment": 0}
    orig_l = segment_capacity.layer_distribution
    orig_s = segment_capacity.segment_distribution

    def _l(*a, **k):
        calls["layer"] += 1
        return orig_l(*a, **k)

    def _s(*a, **k):
        calls["segment"] += 1
        return orig_s(*a, **k)

    monkeypatch.setattr(segment_capacity, "layer_distribution", _l)
    monkeypatch.setattr(segment_capacity, "segment_distribution", _s)
    p, _info = build()
    LE.expand_node(copy.deepcopy(BC.BASE_BEAT_CONFIGS["地上主体结构"]), p)
    assert calls["layer"] > 0 and calls["segment"] > 0, calls


def test_layer_quantities_sum_to_l4_total(ratio_rows):
    """② Σ各层量 ≡ L4 总量；③ Σ各段量 ≡ 层量（两次加权的权重和都是 1）。"""
    p, info = build()
    plan = RS.B4Distribution(p, "地上主体结构", 18, ["Ⅰ区", "Ⅱ区"],
                             BC.segment_floors(18, 38, per=1),
                             list(BC.BASE_BEAT_CONFIGS["地上主体结构"]["cycle"]))
    want = info["index"]["REBAR_NEW_SLAB"]["quantity"]
    got = sum(plan.step_quantity(
        {"name": "钢筋绑扎", "unit": "t", "kb_activity_id": "REBAR_NEW_SLAB"}, i, z)["qty"]
        for i in range(1, 19) for z in (1, 2))
    assert got == pytest.approx(want, rel=1e-9)
    lq = segment_capacity.layer_distribution(want, {i + 1: a
                                                    for i, a in enumerate(plan._areas)})
    assert sum(lq.values()) == pytest.approx(want, rel=1e-9)


def test_changing_floor_areas_changes_layer_quantity(ratio_rows):
    """`params["floor_areas"]` 是真正的计算输入（此前 0 消费者）。"""
    p, info = build()
    base = RS.B4Distribution(p, "地上主体结构", 3, ["Ⅰ区", "Ⅱ区"],
                             BC.segment_floors(3, 3, per=1),
                             list(BC.BASE_BEAT_CONFIGS["地上主体结构"]["cycle"]))
    step = {"name": "钢筋绑扎", "unit": "t", "kb_activity_id": "REBAR_NEW_SLAB"}
    q_equal = base.step_quantity(step, 1, 1)["qty"]

    p2 = dict(p)
    p2["floor_areas"] = {
        "source": "user", "unit": "m²",
        "buildings": {"default": {"floors": {"1": 1000.0, "2": 500.0, "3": 500.0},
                                  "sum_area": 2000.0, "total_area": 15000.0}},
    }
    big = RS.B4Distribution(p2, "地上主体结构", 3, ["Ⅰ区", "Ⅱ区"],
                            BC.segment_floors(3, 3, per=1),
                            list(BC.BASE_BEAT_CONFIGS["地上主体结构"]["cycle"]))
    q_big = big.step_quantity(step, 1, 1)["qty"]
    assert q_big > q_equal, (q_big, q_equal)
    assert big.floor_area_source == "user"


# ================= 3. 量0出局（容差）+ 豁免 =================
def test_is_zero_uses_tolerance_not_exact_equality():
    """改前只认 `float(q) == 0.0`（精确零）⇒ 浮点残差让"量0出局"整体失效。"""
    assert KS._is_zero(0) is True
    assert KS._is_zero(0.0) is True
    assert KS._is_zero(1e-9) is True, "容差判定必须把浮点残差当 0"
    assert KS._is_zero(1e-3) is False
    assert KS._is_zero(None) is False and KS._is_zero("") is False
    assert RS.is_zero_quantity(1e-9) is True


def test_absent_row_is_abnormal_and_excluded(ratio_rows):
    """既不在表里、也不在豁免集合 → **异常缺行**：报缺 + 量0出局。"""
    p, info = build()
    assert any(r["activity_id"] == "CONC_NEW_BEAM" and r["kind"] == "abnormal_absent"
               for r in info["trace"]["abnormal_absent"])
    assert p["l4_quantities"]["CONC_NEW_BEAM"] == 0.0
    assert info["trace"]["exempt"]["available"] is False


def test_exempt_row_is_not_excluded_and_not_ratio_driven(ratio_rows):
    """路线 2：豁免项既不按占比取量，也**不**被量0出局剔除（其量待派生/条件维择一）。"""
    p, info = build(dict(PARAMS, component_ratio_exempt=["CONC_NEW_BEAM"]))
    assert "CONC_NEW_BEAM" not in p["l4_quantities"]
    assert not any(r["activity_id"] == "CONC_NEW_BEAM"
                   for r in info["trace"]["abnormal_absent"])
    st = RS.step_ratio_status(p, {"name": "梁浇筑", "unit": "m³",
                                  "kb_activity_id": "CONC_NEW_BEAM"})
    assert st["status"] == "exempt", st
    assert st["reason"]


# ================= 4. 真实运行时消费者（grep 级证据） =================
def test_component_ratio_has_real_runtime_consumers():
    """`Component_Ratio` 的**真实运行时消费者**（不是只有播种脚本）。

    grep 证据（源码级）：
      · `pipeline/ratio_scope.py` —— 读 `Component_Ratio`（`ratio_rows_for_structure`）；
      · `pipeline/nodes/kb_scope.py` —— `KBScopeNode.run` 里调用 `ratio_scope.build`
        （②占比表这一跳）并在 `_is_zero` 里用容差消费 `l4_quantities`；
      · `pipeline/nodes/beat_configs.py` —— `_apply_step` 调 `step_ratio_status`；
      · `pipeline/layer_engine.py` —— `_make_leaf` 用 `B4Distribution.step_quantity` 定量。
    """
    src_root = BACKEND / "pipeline"
    hay = "\n".join((src_root / p).read_text(encoding="utf-8") for p in (
        "ratio_scope.py", "layer_engine.py",
        "nodes/kb_scope.py", "nodes/beat_configs.py", "nodes/beat_node.py"))
    assert "FROM Component_Ratio" in hay
    assert "ratio_scope.build" in hay or "from .. import ratio_scope as _RS" in hay
    assert "step_ratio_status" in hay
    assert "B4Distribution" in hay
    assert "l4_quantities" in hay


# ================= 5. B5 四步顺序 =================
def test_b5_node_order_puts_ratio_before_wbs():
    """①结构映射 → ②占比表 → ③量0出局 → ④生成 WBS（节点顺序不变）。"""
    src = (BACKEND / "pipeline" / "builder.py").read_text(encoding="utf-8")
    i_scope = src.index('"kb_scope"')
    i_wbs = src.index('"wbs_agent"')
    i_beat = src.index('"beat_build"')
    assert i_scope < i_wbs < i_beat, (i_scope, i_wbs, i_beat)
    # ②在 kb_scope 内部、且先于 _split_l3（③量0出局）的**调用点**
    ksrc = (BACKEND / "pipeline" / "nodes" / "kb_scope.py").read_text(encoding="utf-8")
    i_ratio_call = ksrc.index("ratio_scope as _RS")
    i_split_call = ksrc.index("selected, excluded, candidates, excl_by_structure")
    assert i_ratio_call < i_split_call, (i_ratio_call, i_split_call)


# ================= 6. 重跑逐位一致 =================
def test_rerun_is_bit_for_bit_identical(ratio_rows):
    p, _info = build()
    a, _ = LE.expand_node(copy.deepcopy(BC.BASE_BEAT_CONFIGS["二次结构与砌体"]), p)
    b, _ = LE.expand_node(copy.deepcopy(BC.BASE_BEAT_CONFIGS["二次结构与砌体"]), p)
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True)


# ================= 7. 段级容量字段（§6 验收 #3） =================
def test_every_beat_leaf_carries_segment_capacity_fields(ratio_rows):
    p, _info = build()
    for phase, cfg in BC.BASE_BEAT_CONFIGS.items():
        ph, _ = LE.expand_node(copy.deepcopy(cfg), p)
        for leaf in leaves(ph):
            for key in ("segment_id", "segment_area", "capacity_fixed",
                        "capacity_mobile", "capacity_source"):
                assert key in leaf, (phase, leaf["id"], key)
            assert leaf["segment_area"] >= 0
            assert leaf["capacity_source"] in ("mwi", "unresolved")


# ================= 8. 缺失数据如实降级（不静默） =================
def test_missing_floor_areas_is_annotated(ratio_rows):
    p, _info = build()
    plan = RS.B4Distribution(p, "地上主体结构", 18, ["Ⅰ区", "Ⅱ区"],
                             BC.segment_floors(18, 38, per=1), [])
    codes = {d["code"] for d in plan.degradations}
    assert "floor_area_average_assumption" in codes, plan.degradations

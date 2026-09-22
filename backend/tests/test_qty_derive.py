# -*- coding: utf-8 -*-
"""单层量推算专项测试（v2.3）—— 修「单层量写死且自相矛盾」的真实缺陷

背景（真实产品缺陷）：旧配置给 4 个按层重复的阶段写死了每层工程量，数字自相矛盾：
    地下室结构（2 层）：钢筋 2100 t/层 → 合计 4200 t，占项目钢筋 33%
    地上主体结构（38 层）：钢筋 22 t/层 → 合计 836 t，占项目钢筋 6.5%
「2 层的钢筋比 38 层还多 5 倍」，显然是把两个不同规模项目的数字混在了一起。装饰装修
写死内墙抹灰 8600 ㎡/层，而主体模板才 1900 ㎡/层（抹灰是楼面面积的 4.5 倍，偏高）。
后果：按真实定额算工期时，地下室钢筋绑扎单条 330 天、总计划工期飙到 3738 天。

修正：单层量由项目参数推算 —— **2026-09-21 起**改为唯一量链路
「① `L4 总量 = 单栋 total_<工种> × Component_Ratio[(结构类型, L4)] ÷ 100`
 → ② 层量 = L4 总量 × (该层面积 ÷ Σ各层面积)
 → ③ 段量 = 层量 × (该段面积 ÷ 该层面积)」。
旧的「按施工阶段比例表」（`CONCRETE_RATIO` / `REBAR_RATIO` / `SECONDARY_CONCRETE_RATIO`）
**已整体退役并删除**（用户裁定：`Component_Ratio` 做唯一真源）。
参数缺失仍**绝不猜**，原样保留基线写死值并标注来源。

覆盖（本轮按新语义改写）：
  1. 占比表可用：钢筋/混凝土的**叶子量**来自占比表（`SOURCE_RATIO`），
     且 Σ(各层量 × 各段权重) ≡ L4 总量（B4 两次加权的权重和都为 1）
  2. **缺陷回归**：项目总量不再由"阶段占比"决定，改由占比表 + 层面积决定
  3. 部分参数：模板类走参数推算、缺 total_* 的工序走基线默认，source == "混合"
  4. 一个参数都不给：全部"基线默认"，且结果与改动前逐字段一致（向后兼容）
  5. 总量守恒：Σ叶子量 ≡ 占比表给出的 L4 总量
  6. 层数优先取项目参数
  7. 每道工序都能说出怎么来的：source 非空、公式非空
  8. 叶子带 _qty_source / _qty_formula；expand_node 仍是二元组，段数/叶子 id 规则不变
  9. beat_node 的 beat_subtrees 带 qty_source_summary，done_summary 体现来源计数

运行：python -m pytest backend/tests/test_qty_derive.py -q
"""

import copy
import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import layer_engine as LE
from pipeline import ratio_scope as RS
from pipeline.nodes.beat_configs import (
    ALC_AREA_FACTOR,
    BLOCK_WALL_THICKNESS,
    FACADE_AREA_FACTOR,
    FORMWORK_AREA_FACTOR,
    PLASTER_AREA_FACTOR,
    SOURCE_BASE,
    SOURCE_MIXED,
    SOURCE_PARAM,
    SOURCE_RATIO,
    WINDOW_AREA_FACTOR,
    BASE_BEAT_CONFIGS,
    _calc_suspect_reason,
    _strip_area_denominator,
    derive_beat_quantities,
    resolve_l4_id,
    segment_floors,
)

# 真实项目参数（用户给的演示数据）
PARAMS_FULL = {"total_area": 215000, "total_concrete": 82000, "total_rebar": 12800,
               "floors": 38}
PARAMS_AREA_ONLY = {"total_area": 215000, "floors": 38}      # 有面积、无钢筋/混凝土
ID_RE = re.compile(r"^\d+(\.\d+)+$")

#: 占比表（`Component_Ratio`）在本文件里的**进程内替身** —— 测试**不读库**，
#: 直接把一份 frame_shear 的真实占比注入 `params`（形状与 `ratio_scope.build` 的产物一致）。
#: 值取 `BuildPlan_KB/kb.db` 实测：frame_shear 各组 ∑=100。
RATIO_ROWS = {
    "REBAR_NEW_FOUND": (15.5, "rebar", 1200.0, "t"),
    "REBAR_NEW_SLAB": (20.7, "rebar", 1200.0, "t"),
    "CONC_NEW_FOUND": (17.1, "concrete", 8000.0, "m³"),
    "CONC_NEW_SLAB": (22.1, "concrete", 8000.0, "m³"),
    "CONC_NEW_COLUMN": (18.8, "concrete", 8000.0, "m³"),
    "FORM_NEW_FOUND": (14.0, "formwork", 25000.0, "m²"),
    "FORM_NEW_OTHER": (8.0, "formwork", 25000.0, "m²"),
    "LDT724_砌块墙": (35.4, "masonry", 3000.0, "m³"),
}
#: 占比表里**没有行**、但所属工种有用户总量的 L4（用于"量0出局"与"豁免"用例）。
RATIO_ABSENT = ("CONC_NEW_BEAM", "REBAR_NEW_BEAM", "FORM_NEW_COL",
                "LDT724_砖墙_混水内", "LDT724_砌块墙勾缝")


def ratio_params(base=None, exempt=None):
    """给一份 `params` 挂上占比表替身，使其走 B3/B4 的唯一量链路。

    `ratio_params()` 的默认基座 = `PARAMS_FULL` + 模板/砌体总量（让四个工种都「活跃」）。
    """
    p = dict(PARAMS_FULL)
    if base is not None:
        p = dict(base)
    p.setdefault("total_formwork", 25000)
    p.setdefault("total_masonry", 3000)
    idx, qs = {}, {}
    for aid, (pct, wt, total, unit) in RATIO_ROWS.items():
        q = total * pct / 100.0
        qs[aid] = q
        idx[aid] = {"structure_type_id": "frame_shear", "activity_id": aid,
                    "work_type_id": wt, "ratio_percent": pct, "quantity": q,
                    "unit": unit, "confidence": "LOW", "review_state": "pending",
                    "notes": "AI 经验估算 V1"}
    for aid in RATIO_ABSENT:
        qs[aid] = 0.0
    p["structure_type"] = "frame_shear"
    p["l4_quantities"] = qs
    p["_component_ratio"] = {"structure_type_id": "frame_shear", "l4_index": idx}
    if exempt:
        p["component_ratio_exempt"] = list(exempt)
    return p


def _leaves(ph):
    return [l for wp in ph["work_packages"] for l in wp["sub_packages"]]


def approx(want):
    """量的比对口径：推算结果 round(...,2) 后存进配置，允许 0.01 的取整误差。

    相对误差阈值刻意放宽到 1e-3 并叠加绝对容差 0.01 —— 既覆盖「单层量 round 到两位
    小数」的取整误差，也覆盖「单层量 × 层数 × 分区数」放大后的末位误差，同时仍远紧于
    任务书要求的「总量守恒相对误差 < 2%」。
    """
    return pytest.approx(float(want), rel=1e-3, abs=0.01)


def approx_sum(want):
    """**跨多片叶子求和**后的比对口径。

    叶子的量在 `_make_leaf` 里逐片 `round(..., 2)`，几十上百片相加会把取整误差累积到
    千分之几 —— 这不是口径错，是展示精度。相对容差放到 1%（仍远紧于任务书要求的 2%）。
    """
    return pytest.approx(float(want), rel=1e-2, abs=0.05)


def _zones(name, params):
    """该阶段的有效分区数（与 expand_node 同口径）。"""
    return LE._effective_zones_count(BASE_BEAT_CONFIGS[name], params)


def _step_qty(cfg, name):
    """取某工序的单层量。"""
    for s in (cfg.get("cycle") or []):
        if s["name"] == name:
            return float(s["qty_per_floor"])
    raise AssertionError("没有该工序：%s" % name)


def _detail(note, name):
    d = (note.get("detail") or {}).get(name)
    assert d, "detail 缺工序 %s" % name
    return d


def _derive(name, params):
    return derive_beat_quantities(copy.deepcopy(BASE_BEAT_CONFIGS[name]), params)


# ================= 0. 旧按施工阶段比例表必须真的退役 =================
def test_old_stage_ratio_tables_are_gone():
    """用户裁定（2026-09-21）：「新表做唯一真源，旧的方法不采纳」。

    旧的按**施工阶段**的比例表必须从计算路径**移除**（而不是"并存"）——
    两套口径是正交的（阶段 vs 结构×工种），并存等于把同一个总量摊两遍。
    本用例把"删除"钉住：常量不存在、也没有任何 `_calc_concrete/_calc_rebar` 之类的旧入口。
    """
    from pipeline.nodes import beat_configs as BC
    for gone in ("CONCRETE_RATIO", "REBAR_RATIO", "SECONDARY_CONCRETE_RATIO",
                 "_calc_concrete", "_calc_rebar", "_floors_total_same_scope",
                 "_qty_formula", "_ratio_txt"):
        assert not hasattr(BC, gone), "旧阶段比例表的 %s 必须已删除" % gone
    # 取而代之的唯一来源标记
    assert BC.SOURCE_RATIO == "占比表拆分"


# ================= 1. 占比表：按 B4 唯一量链路推算 =================
def test_full_params_ratio_table_drives_qty():
    """占比表可用时：叶子量 = ③段量（`SOURCE_RATIO`），且 Σ ≡ L4 总量。"""
    p = ratio_params()
    zones = _zones("地上主体结构", p)

    main_cfg, main_note = _derive("地上主体结构", p)
    assert _detail(main_note, "钢筋绑扎")["source"] == SOURCE_RATIO
    assert _detail(main_note, "混凝土浇筑")["source"] == SOURCE_RATIO
    assert _detail(main_note, "铝模安装")["source"] == SOURCE_RATIO
    # 公式必须写清「占比表 + 各层面积分解」而不是旧的「÷层数÷分区数」
    assert "Component_Ratio" in _detail(main_note, "钢筋绑扎")["formula"]

    # 展开后：Σ(全部层 × 全部区) 必须等于占比表给出的 L4 总量
    for phase, aid, want in (("地上主体结构", "REBAR_NEW_SLAB", 1200.0 * 0.207),
                             ("地上主体结构", "CONC_NEW_SLAB", 8000.0 * 0.221),
                             ("地下室结构", "REBAR_NEW_FOUND", 1200.0 * 0.155)):
        ph, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS[phase]), p)
        got = sum(l["quantity"] for l in _leaves(ph)
                  if l.get("kb_activity_id") == aid)
        assert got == approx_sum(want), (phase, aid, got, want)
        srcs = {l["_qty_source"] for l in _leaves(ph)
                if l.get("kb_activity_id") == aid}
        assert srcs == {SOURCE_RATIO}, (phase, aid, srcs)

    # 每道工序都要能说出怎么来的
    for note in (main_note, _derive("地下室结构", p)[1]):
        for name, d in note["detail"].items():
            assert d["source"] in (SOURCE_RATIO, SOURCE_PARAM, SOURCE_BASE), (name, d)
            if d["source"] != SOURCE_BASE:
                assert d["formula"], "有来源的量必须有中文公式：%s" % name


def test_basement_rebar_per_floor_not_absurd_vs_main():
    """**缺陷回归（新语义版）**：旧基线「2 层地下室 2100 t/层 vs 38 层主体 22 t/层」。

    新口径下量不再由"阶段占比 ÷ 阶段层数"决定，而由
    `占比表 × 层面积` 决定 ⇒ 单层量之比 = (L4 总量 ÷ 层数) 之比，
    量级只差在下式给出的占比与层数上，**不可能**再出现"2 层比 38 层多 5 倍"。
    """
    p = ratio_params()
    ph_b, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), p)
    ph_m, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), p)
    q_b = [l["quantity"] for l in _leaves(ph_b) if l["_step_name"] == "钢筋绑扎"][0]
    q_m = [l["quantity"] for l in _leaves(ph_m) if l["_step_name"] == "钢筋绑扎"][0]

    # 占比表口径：地下室 L4 = 基础钢筋 15.5%（2 层分摊）；主体 L4 = 板钢筋 20.7%（38 层分摊）
    # 地下室是 **0.5 层一段**（底板/墙柱/顶板分层浇筑）⇒ 每片叶子只覆盖半层
    zb = _zones("地下室结构", p)
    zm = _zones("地上主体结构", p)
    assert q_b == approx_sum(1200.0 * 0.155 / 2 / zb * 0.5)
    assert q_m == approx_sum(1200.0 * 0.207 / 38 / zm)
    # 旧基线的荒谬比 2100×2 / 22×38 ≈ 5.0；新口径下"地下室钢筋总量 / 主体钢筋总量" < 0.9
    old_total_b, old_total_m = 2100 * 2, 22 * 38
    assert old_total_b / old_total_m > 5.0
    new_total_b = 1200.0 * 0.155
    new_total_m = 1200.0 * 0.207
    assert new_total_b / new_total_m < 0.9, "占比表口径下地下室钢筋总量必须小于主体"


def test_old_defect_absolute_numbers_gone():
    """旧写死值不得再出现在推算结果里（全参数场景）。

    ⚠️ 第 5 批（域 4.1b）后「构造柱浇筑」**不在**本清单里：它的 L4 已从混凝土族的
    `CONC_NEW_COLUMN`（柱浇筑）改成砌筑族的「方柱-混水」（`LDT724_方柱_混水`），
    而砌筑族在 `Component_Ratio` 里没有任何一行 ⇒ 它拿不到占比表口径，
    只能退回**声明的基线值** `qty_per_floor = 32`（`_qty_source == SOURCE_BASE`，
    来源如实标「基线默认」，**不是静默**）。也就是说：旧值 32 现在刚好又是正确值，
    「旧写死值必须消失」这条判据对它不成立 —— 但**其余每一条一个字都不放松**。

    ⚠️ 第 7 批（2026-09-21）**同型再加一条**：「ALC墙板安装」的 L4 已从砌筑族的
    `LDT724_砌块墙`（该活动在 `Component_Ratio` 里**有**行）改成 `MASON_ALC_PANEL`
    （在 `Component_Ratio` 里**没有任何一行**）⇒ 与构造柱完全同型：拿不到占比表口径，
    只能退回声明的基线值 `qty_per_floor = 800`（`_qty_source == SOURCE_BASE`）。
    ⇒ 这一类**不是从清单里删掉就完事**：下面逐条断言它确实走的是 `SOURCE_BASE`
    （既保住"旧写死值必须消失"对其它工序的约束，又证明这条是如实标注的兜底，
    不是复用旧值）。
    """
    p = ratio_params()
    #: 因"该族在占比表里无行"而**如实退回基线默认**、且恰好等于旧写死值的工序。
    #: 必须在下面逐条证明 `source == SOURCE_BASE`，不许静默豁免。
    _baseline_equal = {("二次结构与砌体", "ALC墙板安装"): 800.0}
    old = {"地下室结构": {"钢筋绑扎": 2100, "模板安装": 18000, "混凝土浇筑": 8200},
           "地上主体结构": {"钢筋绑扎": 22, "铝模安装": 1900, "混凝土浇筑": 180},
           "二次结构与砌体": {"ALC墙板安装": 800, "砌块墙": 85},
           "装饰装修": {"内墙抹灰": 8600, "地面找平": 7500, "门窗安装": 1020}}
    for phase, steps in old.items():
        cfg, note = _derive(phase, p)
        for name, qty in steps.items():
            if (phase, name) in _baseline_equal:
                assert _step_qty(cfg, name) == float(qty), (phase, name)
                d = note["detail"][name]
                assert d["source"] == SOURCE_BASE, (
                    "%s/%s 等于旧写死值 %s，必须证明它走的是「基线默认」"
                    "而不是复用旧值：%s" % (phase, name, qty, d))
                continue
            assert _step_qty(cfg, name) != float(qty), (phase, name)


# ================= 1b. 量0出局 / 豁免（路线 2） =================
def test_missing_ratio_row_is_excluded_from_tree():
    """工种有用户总量、但表里既没有该行也不在豁免集合 → **量0出局**（不进树）。

    绝不退回旧阶段比例表补数，也绝不静默按别的口径补。
    """
    p = ratio_params()
    ph, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["二次结构与砌体"]), p)
    names = {l["_step_name"] for l in _leaves(ph)}
    assert "勾缝" not in names, "占比表缺行的工序必须量0出局"
    assert ph["ratio_steps"]["kept"] < ph["ratio_steps"]["total"]
    assert any(e.get("activity_id") == "LDT724_砌块墙勾缝"
               for e in ph["ratio_exclusions"])


def test_exempt_row_keeps_coefficient_path_and_is_labelled():
    """路线 2：显式豁免项 ≠ 异常 —— 不按占比取量，但**也不被量0出局剔除**。"""
    p = ratio_params(exempt=("LDT724_砌块墙勾缝",))
    st = RS.step_ratio_status(p, {"name": "勾缝", "unit": "m²",
                                  "kb_activity_id": "LDT724_砌块墙勾缝"})
    assert st["status"] == "exempt", st
    ph, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["二次结构与砌体"]), p)
    names = {l["_step_name"] for l in _leaves(ph)}
    assert "勾缝" in names, "豁免项不得被量0出局剔除"
    assert not any(e.get("activity_id") == "LDT724_砌块墙勾缝"
                   for e in ph["ratio_exclusions"])
    degs = [d for d in ph["ratio_degradations"] if d.get("code") == "exempt_no_ratio"]
    assert degs, ph["ratio_degradations"]


# ================= 2. 部分参数：模板走参数推算、其余走基线 =================
def test_partial_params_mixed_keeps_baseline_for_missing():
    cfg, note = _derive("地上主体结构", PARAMS_AREA_ONLY)
    assert note["source"] == SOURCE_MIXED

    # 模板类（只要 total_area）→ 参数推算
    d_form = _detail(note, "铝模安装")
    assert d_form["source"] == SOURCE_PARAM
    zones = _zones("地上主体结构", PARAMS_AREA_ONLY)
    assert d_form["qty_per_floor"] == approx(215000 * FORMWORK_AREA_FACTOR / 38 / zones)
    assert "模板接触面积系数" in d_form["formula"]

    # 钢筋类（缺 total_rebar / 结构类型未识别）→ 基线默认，且**原样保留**（不拿面积硬凑）
    d_rebar = _detail(note, "钢筋绑扎")
    assert d_rebar["source"] == SOURCE_BASE
    assert d_rebar["qty_per_floor"] == 22.0
    assert _step_qty(cfg, "钢筋绑扎") == 22.0

    # 混凝土类（缺 total_concrete）→ 基线默认
    d_conc = _detail(note, "混凝土浇筑")
    assert d_conc["source"] == SOURCE_BASE
    assert _step_qty(cfg, "混凝土浇筑") == 180.0

    # 参数缺失时绝不猜：没有占比表就没有任何钢筋推算公式
    assert d_rebar["formula"] == ""


def test_missing_area_or_ratio_keeps_baseline():
    """**结构类型未识别 / 占比表不可用** → 体积类退回基线兜底（不瞎猜、也不退回旧阶段表）。"""
    params = {"floors": 38, "total_rebar": 12800}          # 无 structure_type
    cfg, note = _derive("地下室结构", params)
    assert _detail(note, "钢筋绑扎")["source"] == SOURCE_BASE
    assert _step_qty(cfg, "钢筋绑扎") == 55.0               # BASEMENT_BASELINE_REBAR_T
    assert _detail(note, "模板安装")["source"] == SOURCE_BASE
    # 面积缺失 → 模板退回**兜底基线**（不是旧写死值 18000，也不是拿别的参数硬凑）。
    assert _step_qty(cfg, "模板安装") == 4750.0
    assert note["source"] == SOURCE_BASE


# ================= 3. 一个参数都不给：完全等于旧基线 =================
def test_no_params_equals_legacy_baseline_field_by_field():
    for name, base in BASE_BEAT_CONFIGS.items():
        for params in (None, {}, {"building_type": "剪力墙住宅"}):
            cfg, note = derive_beat_quantities(copy.deepcopy(base), params)
            assert note["source"] == SOURCE_BASE, (name, params)
            # cycle 逐字段一致。⚠️ 第 5 批（域 3.1）：`kb_activity_id` 不再写死在配置里，
            # 而是运行时按 `(work_type_id, l4_name)` 从库里反查后补挂 —— 逐字段比对时
            # 放行这一个键（下面单独核它的值），**其余每个字段仍须逐字段相等**。
            assert len(cfg["cycle"]) == len(base["cycle"]), (name, params)
            for got, want in zip(cfg["cycle"], base["cycle"]):
                assert {k: v for k, v in got.items() if k != "kb_activity_id"} == want, \
                    (name, params, got)
                assert got.get("kb_activity_id") == resolve_l4_id(
                    want.get("work_type_id"), want.get("l4_name")), (name, params, got)
            for step in cfg["cycle"]:
                assert _detail(note, step["name"])["source"] == SOURCE_BASE
            # 措施项 / 平行专项逐字段一致（平行专项只多挂了溯源字段；同样放行 kb_activity_id）
            if base.get("attach_measures") is not None:
                assert len(cfg["attach_measures"]) == len(base["attach_measures"]), name
                for got, want in zip(cfg["attach_measures"], base["attach_measures"]):
                    assert {k: v for k, v in got.items() if k != "kb_activity_id"} == want, \
                        (name, got)
                    assert got.get("kb_activity_id") == resolve_l4_id(
                        want.get("work_type_id"), want.get("l4_name")), (name, got)
            for a, b in zip(cfg.get("parallel_work") or [], base.get("parallel_work") or []):
                assert a["qty_total"] == b["qty_total"]
                assert a["_qty_source"] == SOURCE_BASE


def test_derive_does_not_mutate_input_cfg():
    base = BASE_BEAT_CONFIGS["地下室结构"]
    snapshot = copy.deepcopy(base)
    derive_beat_quantities(base, PARAMS_FULL)
    assert base == snapshot, "推算不得原地改基线配置（否则会污染 BASE_BEAT_CONFIGS）"


# ================= 4. 总量守恒（B4：Σ层量 × Σ段权重 ≡ L4 总量） =================
def test_total_conservation_rebar():
    p = ratio_params()
    ph, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), p)
    got = sum(l["quantity"] for l in _leaves(ph) if l["_step_name"] == "钢筋绑扎")
    want = 1200.0 * 0.207
    assert abs(got - want) / want < 0.02, (got, want)


def test_total_conservation_concrete_basement_and_main():
    p = ratio_params()
    ph_b, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), p)
    ph_m, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), p)
    c_b = sum(l["quantity"] for l in _leaves(ph_b) if l["_step_name"] == "混凝土浇筑")
    c_m = sum(l["quantity"] for l in _leaves(ph_m) if l["_step_name"] == "混凝土浇筑")
    assert c_b == approx_sum(8000.0 * 0.171)
    assert c_m == approx_sum(8000.0 * 0.221)


def test_total_conservation_secondary_structure():
    """二次结构：构造柱浇筑已从混凝土族挪到砌筑族（第 5 批，域 4.1b），砌块墙仍守恒。

    语义改动（父代理裁定，本用例照改）：「构造柱浇筑」的 L4 原本是混凝土族的
    `CONC_NEW_COLUMN`（柱浇筑），因 4.1b 硬约束（该分部叶子的 L4 所属 L3 必须 ∈
    `DEFAULT_PHASES["二次结构与砌体"]["kb"] == ["masonry"]`）改挂砌筑族的「方柱-混水」
    （`LDT724_方柱_混水`）⇒ **它不再由 `Component_Ratio` 的混凝土族占比驱动**。

    实测（`ratio_params()` 替身口径，本文件唯一口径；**不是编的数字**）：
      · 无占比表口径时（`params={}`）构造柱仍在树里，每个叶子
        `_qty_source == SOURCE_BASE`、`_qty_per_floor == 32.0`、`l3_work_type_id == "masonry"`
        —— 来源是**可见的**「基线默认」，不是静默（见下第 ① 段）；
      · 代用占比表里 masonry 只有 `LDT724_砌块墙` 一行 ⇒ 构造柱**量0出局**、
        树里没有它的叶子，`ratio_exclusions` 点名 `('构造柱浇筑','LDT724_方柱_混水')`
        —— 旧断言 `构造柱 == 8000×0.188`（混凝土占比）**已不成立**（见下第 ② 段）；
      · 砌块墙（同属 masonry，本来就不走混凝土占比表）守恒断言**保留**：
        Σ = 1061.72 ≈ 3000×0.354（`approx_sum` 1% 口径）；
      · 勾缝：占比表没有该 L4 → 量0出局（既有断言，保留）。

    ⚠️ 另记（实测，供复核）：真库 `BuildPlan_KB/kb.db` 的 `Component_Ratio` 里
    **一行 masonry 都没有**（只有 concrete/rebar/formwork 三族），所以真参数下
    构造柱与砌块墙都会量0出局、只剩 ALC墙板安装。本用例只用本文件的替身口径写断言。
    """
    p = ratio_params()

    # ① 没有占比表口径 ⇒ 构造柱退回声明的基线值（来源可见），且所属 L3 是砌筑
    ph0, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["二次结构与砌体"]), {})
    col0 = [l for l in _leaves(ph0) if l["_step_name"] == "构造柱浇筑"]
    assert col0, "无占比表时构造柱仍在树里（退回基线，不量0出局）"
    assert {l["_qty_source"] for l in col0} == {SOURCE_BASE}
    assert {l["_qty_per_floor"] for l in col0} == {32.0}
    assert {l["l3_work_type_id"] for l in col0} == {"masonry"}
    assert {l.get("kb_activity_id") for l in col0} == {"LDT724_方柱_混水"}

    # ② 有了（混凝土族主导的）占比表替身：构造柱不再由混凝土占比驱动
    ph, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["二次结构与砌体"]), p)
    col = [l for l in _leaves(ph) if l["_step_name"] == "构造柱浇筑"]
    wall_leaf = [l for l in _leaves(ph) if l["_step_name"] == "砌块墙"]
    wall = sum(l["quantity"] for l in wall_leaf)
    assert col == [], ("构造柱改挂砌筑族的「方柱-混水」后，占比表里查不到该 L4 → "
                       "必须量0出局，绝不再从混凝土总量 8000×0.188 取量")
    assert any(e.get("activity_id") == "LDT724_方柱_混水"
               for e in ph["ratio_exclusions"]), ph["ratio_exclusions"]
    assert wall == approx_sum(3000.0 * 0.354)
    assert {l["l3_work_type_id"] for l in wall_leaf} == {"masonry"}
    # 勾缝：占比表没有该 L4 → 量0出局（**不**从砌体总量硬拆，也不退回旧阶段表）
    assert "勾缝" not in {l["_step_name"] for l in _leaves(ph)}


# ================= 5. 其余公式逐条核对（面积类） =================
def test_area_based_formulas():
    zones = _zones("装饰装修", PARAMS_FULL)
    area = PARAMS_FULL["total_area"]

    sec, _ = _derive("二次结构与砌体", PARAMS_FULL)
    assert _step_qty(sec, "ALC墙板安装") == approx(area * ALC_AREA_FACTOR / 38 / zones)
    # 砌块墙 = ALC 面积 × 墙厚 0.2 m
    assert _step_qty(sec, "砌块墙") == approx(
        area * ALC_AREA_FACTOR * BLOCK_WALL_THICKNESS / 38 / zones)
    # 勾缝与砌块墙同口径
    assert _step_qty(sec, "勾缝") == approx(_step_qty(sec, "砌块墙"))

    dec, dec_note = _derive("装饰装修", PARAMS_FULL)
    assert _step_qty(dec, "内墙抹灰") == approx(area * PLASTER_AREA_FACTOR / 38 / zones)
    assert _step_qty(dec, "内墙涂料") == approx(_step_qty(dec, "内墙抹灰"))
    assert _step_qty(dec, "地面找平") == approx(area / 38 / zones)
    assert _step_qty(dec, "门窗安装") == approx(area * WINDOW_AREA_FACTOR / 38 / zones)
    # 外檐平行专项：全楼总量 = 面积 × 0.6（不除以层数/分区数）
    for p in dec["parallel_work"]:
        assert p["qty_total"] == approx(area * FACADE_AREA_FACTOR)
        assert p["_qty_source"] == SOURCE_PARAM
        assert p["_qty_formula"]
    assert _detail(dec_note, "外檐保温")["source"] == SOURCE_PARAM

    # 措施项：爬架提升 = 面积 × 0.6 ÷ 层数 ÷ 分区数
    main, main_note = _derive("地上主体结构", PARAMS_FULL)
    lift = [s for s in main["attach_measures"] if s["name"] == "爬架提升"][0]
    assert lift["qty_per_floor"] == approx(area * FACADE_AREA_FACTOR / 38 / zones)
    assert _detail(main_note, "爬架提升")["source"] == SOURCE_PARAM


# ================= 6. 层数优先取项目参数 =================
def test_floors_follow_project_params():
    """同样总量摊到更少层数 → 层量更大（新口径：L4 总量 × 层面积 ÷ Σ各层面积）。"""
    p18 = ratio_params(dict(PARAMS_FULL, floors=18))
    p38 = ratio_params(dict(PARAMS_FULL, floors=38))
    ph18, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), p18)
    ph38, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), p38)

    def _layer_total(ph):
        """第 1 层（竖向段 1）的**全部平面段之和** = 该层的层量。"""
        return sum(l["quantity"] for l in _leaves(ph)
                   if l["_step_name"] == "钢筋绑扎" and l["_segment"] == 1)

    # 逐层面积都取等（无逐层数据） ⇒ 层量 = L4 总量 ÷ 层数
    assert _layer_total(ph18) == approx_sum(1200.0 * 0.207 / 18)
    assert _layer_total(ph38) == approx_sum(1200.0 * 0.207 / 38)
    assert _layer_total(ph18) > _layer_total(ph38)

    # 地下室 floors_locked：项目层数不影响它，恒 2 层
    ph_b18, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), p18)
    ph_b38, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地下室结构"]), p38)
    b18 = sum(l["quantity"] for l in _leaves(ph_b18) if l["_step_name"] == "钢筋绑扎")
    b38 = sum(l["quantity"] for l in _leaves(ph_b38) if l["_step_name"] == "钢筋绑扎")
    assert b18 == approx_sum(b38)


def test_floors_default_to_config_and_is_annotated():
    """`floors` 是占比表路径的**必需输入**（层数与标准层面积都靠它）。

    · 缺 `total_area` 或 `floors` → B4 无法按层面积分解 ⇒ 如实退回基线并标 `基线默认`
      （**不**挂"占比表拆分"的假来源）；
    · 两项都有 ⇒ 走占比表，公式写明 Component_Ratio 与 L4 总量。
    """
    p = ratio_params({"total_rebar": 12800})          # 有占比表、无 floors/total_area
    cfg, note = _derive("地上主体结构", p)
    d = _detail(note, "钢筋绑扎")
    assert d["source"] == SOURCE_BASE
    assert _step_qty(cfg, "钢筋绑扎") == 22.0

    p2 = ratio_params({"total_rebar": 12800, "total_area": 215000, "floors": 38})
    cfg2, note2 = _derive("地上主体结构", p2)
    assert _detail(note2, "钢筋绑扎")["source"] == SOURCE_RATIO
    f = _detail(note2, "钢筋绑扎")["formula"]
    assert "占比表拆分" in f and "Component_Ratio" in f and "248.4" in f
    assert "层数取配置默认" not in f, "floors 来自用户参数，不该标'取配置默认'"

    # 只有 floors、没有 total_area：占比表在、但层面积推不出来 → 仍退回基线
    p3 = ratio_params({"total_rebar": 12800, "floors": 38})
    cfg3, note3 = _derive("地上主体结构", p3)
    assert _detail(note3, "钢筋绑扎")["source"] == SOURCE_BASE


# ================= 7. 叶子带来源与公式 =================
def test_leaves_carry_qty_source_and_formula():
    p = ratio_params()
    ph, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), p)
    leaves = _leaves(ph)
    assert leaves
    for l in leaves:
        assert l.get("_qty_source") in (SOURCE_RATIO, SOURCE_PARAM, SOURCE_BASE), l
        assert isinstance(l.get("_qty_formula"), str), l
        if l["_qty_source"] != SOURCE_BASE:
            assert l["_qty_formula"], l
    # A7：预制「叠合板吊装」已从地上主体结构移除
    assert "叠合板吊装" not in {l["_step_name"] for l in leaves}, "预制叠合板工序必须已移除"
    # 有占比表 ⇒ 三道主工序全部「占比表拆分」；措施项「爬架提升」仍走参数推算
    by_step = {l["_step_name"]: l for l in leaves}
    for nm in ("钢筋绑扎", "铝模安装", "混凝土浇筑"):
        assert by_step[nm]["_qty_source"] == SOURCE_RATIO, (nm, by_step[nm]["_qty_source"])
    assert by_step["爬架提升"]["_qty_source"] == SOURCE_PARAM
    # 逐行 AI 估算标注必须透传到叶子
    assert by_step["钢筋绑扎"]["_ratio_source"]["confidence"] == "LOW"
    assert by_step["钢筋绑扎"]["_ratio_source"]["review_state"] == "pending"
    assert by_step["钢筋绑扎"]["_ratio_source"]["activity_id"] == "REBAR_NEW_SLAB"

    # 无参数 → 该工序标基线默认，量与旧基线逐字段一致（向后兼容）
    ph0, _ = LE.expand_node(copy.deepcopy(BASE_BEAT_CONFIGS["地上主体结构"]), {})
    base_leaves = [l for l in _leaves(ph0) if l["_step_name"] == "钢筋绑扎"]
    assert base_leaves and all(l["_qty_source"] == SOURCE_BASE for l in base_leaves)
    # 无参数时钢筋单层量就是配置写死的 22 t/层
    assert base_leaves[0]["_qty_per_floor"] == approx(22.0)
    # 有占比表时同一条工序走占比表，不再等于写死值
    assert by_step["钢筋绑扎"]["_qty_per_floor"] != approx(22.0)


# ================= 8. expand_node 契约不变 =================
def test_expand_node_returns_tuple_and_ids_unchanged():
    for name, cfg in BASE_BEAT_CONFIGS.items():
        out = LE.expand_node(cfg, PARAMS_FULL)
        assert isinstance(out, tuple) and len(out) == 2, name
        phase_dict, ids = out
        assert phase_dict["phase"] == name
        assert ids, name

        # 段数 / 叶子 id 规则与推算无关（推算只改量，不改结构）——
        # 分区数只跟参数面积有关，所以用同一份 params 对照
        ref = LE.expand_node(copy.deepcopy(cfg), dict(PARAMS_FULL))
        assert ids == ref[1], name
        floors = LE._eff_floors(cfg, PARAMS_FULL)
        segs = segment_floors(floors, int(cfg.get("segments") or 1),
                              per=cfg.get("floors_per_segment"))
        zones_n = _zones(name, PARAMS_FULL)
        n_steps = len(cfg.get("cycle") or []) + len(cfg.get("attach_measures") or [])
        assert len(ids) == zones_n * len(segs) * n_steps + len(cfg.get("parallel_work") or [])
        for i in ids:
            # 第 5 批（域 4）：`分部.L3工种号.L4工序号.分区.层段`（5 段）
            assert ID_RE.match(i) and len(i.split(".")) == 5, (name, i)


def test_common_validate_still_passes_after_derive():
    """校验器拿推算后的配置核对推算后的叶子：量级、层数、节拍域都必须通过。"""
    for name, cfg in BASE_BEAT_CONFIGS.items():
        derived, _ = derive_beat_quantities(copy.deepcopy(cfg), PARAMS_FULL)
        ph, _ = LE.expand_node(cfg, PARAMS_FULL)
        errs = LE.common_validate(derived, ph, PARAMS_FULL)
        assert errs == [], (name, errs)


def test_derived_durations_stay_in_beat_domain():
    """推算后节拍仍在 [2,90]（旧写死值会把地下室钢筋算成 330 天）。"""
    for name, cfg in BASE_BEAT_CONFIGS.items():
        ph, _ = LE.expand_node(cfg, PARAMS_FULL)
        for l in _leaves(ph):
            assert LE.CLAMP_MIN <= l["duration_days"] <= LE.CLAMP_MAX, (name, l)


# ================= 9. beat_node 的汇总 =================
def _stub_wbs():
    names = ["施工准备", "地基处理与桩基", "基坑支护与土方", "地下室结构", "地上主体结构",
             "二次结构与砌体", "机电安装", "装饰装修", "室外工程", "竣工验收"]
    phases = []
    for i, nm in enumerate(names, 1):
        sub = [{"id": "%d.1.1" % i, "name": nm, "duration_days": 3, "quantity": 10,
                "unit": "项", "work_type": "土建"}]
        phases.append({"phase": nm,
                       "work_packages": [{"id": "%d.1" % i, "name": nm, "sub_packages": sub}]})
    return {"phases": phases}


def test_beat_node_writes_qty_source_summary_and_done_summary():
    from pipeline.nodes.beat_node import BeatExpandNode
    ctx = {"wbs": _stub_wbs(), "extracted_params": ratio_params()}
    node = BeatExpandNode(refine=False)
    node._emit = lambda e, d: None
    node.run(ctx)

    assert set(ctx["beat_subtrees"]) == set(BASE_BEAT_CONFIGS)
    for name, info in ctx["beat_subtrees"].items():
        summary = info.get("qty_source_summary")
        assert isinstance(summary, dict) and summary, name
        assert set(summary) <= {SOURCE_RATIO, SOURCE_PARAM, SOURCE_BASE}, (name, summary)
        # 占比表留痕（哪几道被驱动 / 哪几道量0出局 / 哪几处降级）必须上行
        assert "ratio" in info, name
    # A7 + 占比表后：地上主体结构三道主工序全部「占比表拆分」，措施项走参数推算，
    # 「用默认值」为 0
    main_summary = ctx["beat_subtrees"]["地上主体结构"]["qty_source_summary"]
    assert main_summary.get(SOURCE_RATIO) == 3, main_summary
    assert not main_summary.get(SOURCE_BASE), main_summary
    # 完成摘要如实报出三条来源
    assert "按占比表（Component_Ratio）拆分" in node.done_summary
    assert "按参数推算" in node.done_summary
    assert "用默认值" in node.done_summary
    m = re.search(r"(\d+) 项按占比表（Component_Ratio）拆分、(\d+) 项按参数推算、(\d+) 项用默认值",
                  node.done_summary)
    assert m and int(m.group(3)) == 0, node.done_summary


# ================= 10. G5：产物字符串清零 U+33A1（「㎡」） =================
#: CJK 兼容方块平米符号 U+33A1。**用转义构造**：本文件源码里不留该字面量。
SQUARE_METRE_CJK = "\u33a1"


def test_no_cjk_square_metre_in_derived_outputs():
    """G5（方案 §6 验收 #5）：`beat_configs` 产出的**公式 / 说明文案**必须 0 处 U+33A1。

    `㎡`(U+33A1) 与 `m²`（m + U+00B2）不是同一个字符。`kb_units.normalize_unit` 已做
    **输入侧**归一（用户文档里写 `㎡` 照样认），所以清零只针对**输出侧**，不影响任何数字。
    """
    multi = dict(PARAMS_FULL, building_count=12)      # 触发"全项目共 N 栋（…m²÷N栋…）"文案
    cases = [PARAMS_FULL, multi, PARAMS_AREA_ONLY, {}]
    blobs = []
    for name in BASE_BEAT_CONFIGS:
        for params in cases:
            cfg, note = _derive(name, params)
            blobs.append(json.dumps(cfg, ensure_ascii=False))
            blobs.append(json.dumps(note, ensure_ascii=False))

    # 超限原因（`_qty_suspect_reason`）同样是产物字符串 —— 三条指标都要覆盖
    for step in ({"name": "钢筋绑扎", "unit": "t", "qty_per_floor": 2100.0},
                 {"name": "混凝土浇筑", "unit": "m³", "qty_per_floor": 8200.0},
                 {"name": "模板安装", "unit": "m²", "qty_per_floor": 18000.0}):
        reason = _calc_suspect_reason(step, {"total_area": 3800, "floors": 38})
        assert reason, "本用例前提：该量必须命中物理上限自检：%s" % step["name"]
        blobs.append(reason)

    for blob in blobs:
        assert SQUARE_METRE_CJK not in blob, blob


def test_cjk_square_metre_still_accepted_on_input_side():
    """G5 只清零**输出**：输入侧声明里的 `㎡` 仍必须被认出来（不许一起删掉）。

    两条输入侧容忍：
      · `_strip_area_denominator`：声明单位 `t/㎡` 与 `t/m²` 都归一成 `t`；
      · 用户文档里的 `㎡` 由 `kb_units.normalize_unit` 归一（见 test_unit_area_volume）。
    """
    for raw in ("t/\u33a1", "t/m²", "t/m2"):
        assert _strip_area_denominator(raw) == "t", raw
    assert _strip_area_denominator("m³/\u33a1") == "m³"


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.isfunction(v)]
    for fn in fns:
        fn()
        print("  PASS  " + fn.__name__)
    print("全部 qty_derive 用例通过 ✔")

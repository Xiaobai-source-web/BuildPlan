# -*- coding: utf-8 -*-
"""第 39 轮：施工段数参与容量（问题 A）+ 每施工段人数上限重定（问题 B）回归门。

覆盖的契约语义（全部来自 `docs/修改契约_v1.md` §4 / §5-WS4 ⑤ 与实测）：

A. **`segments_factor` 是什么** —— 查证结论：它是 **0/1 闸门**（"要不要按施工段并行
   放大"），**不是折减系数**：
     · 契约 §4 列注释 `segments_factor,  # 0/1`、DDL `INTEGER DEFAULT 1`；
     · §5-WS4 ⑤ 只定义 `segments_factor == 1` 时的 `cap_total = cap_labor × 并行段数`，
       全篇没有 `× segments_factor` 的写法 —— 若它是折减系数，取 0 会把容量乘成 0；
     · `kb.workface_capacity()` 的 docstring 说"施工段折减"，与 0/1 定义域冲突 →
       **以实际取值分布为准**：全表 478 行**全部是 1**（下有用例钉住这个事实）。
   实现：`segments_parallel_enabled()`（闸门）+ `parallel_segment_count()`（段数入口）。
   计划侧事实：322 条节拍叶子**没有一条**带显式段数键 → `parallel_segment_count` 全为 1
   （节拍叶子 id 是 `{node}.{z}.{s}.{k}`，每条 = 一个 (分区,施工段,工序)，段间并行由串行
   排程决定，排程期再乘一遍段数就是重复计数）→ 本改动在当前计划上影响 **0 条**任务。

B. **上限策略（C8-7 后已改口径）**：原先的「每施工段人数上限重定」
   `effective_crew_max = max(crew_max, min(40, ceil(crew_base × 2.5)))` **已整条删除**
   （C 组 C8 删除清单第 7 项：无规范依据）。现在人工侧上限就是标定行的裸 `crew_max`；
   每工能上多少改由 MWI 表（`Resource_Workface_Index`）经 `org_plan.plan_capacity_chain`
   给出。`norm_coverage_report` 的 `workface_saturated` 保留、
   `workface_ceiling_raised` **恒为 0**（不再有抬升口径）。

C. **边界条件的来源闸门**（第 40 轮）：`boundary_conditions["_source"]` 标 `model` 的
   限额（`labor.peak_total` / `labor.by_trade` / `equipment` / `project_duration_days`）
   **不当限额用**（记进 `ignored_model_values` 并出中文 warning）；标 `user` 照旧；
   **没有 `_source`**（旧计划 / 直接传 dict 的调用方）保持旧行为。

测试只跑纯函数 + 只读 sqlite/JSON，不跑流水线、不写库。
"""

from __future__ import annotations

import json
import math
import pathlib
import sqlite3
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import org_defaults, org_plan                     # noqa: E402
from pipeline.nodes import scheduler as S                          # noqa: E402

DB = BACKEND.parent / "BuildPlan_KB" / "kb.db"
PLAN = BACKEND.parent / "terminal" / "plans" / "plan_run_1789827002.json"


# ---------------------------------------------------------------- 夹具
def _rule(**kw):
    """v2 标定行（默认取 FORM_NEW_OTHER 那一档：base=12 / step 500·1 / min 4 / max 15）。"""
    base = {
        "unit_basis": "每施工段",
        "quantity_unit": "m²",
        "source_type": "ai_estimate",
        "confidence": "LOW",
        "crew_base": 12.0, "crew_step_q": 500.0, "crew_step_n": 1.0,
        "crew_min": 4.0, "crew_max": 15.0, "q_ref": 1000.0,
        "segments_factor": 1,
        "machine_base": 1.0, "machine_step_q": 50.0, "machine_step_n": 1.0,
        "machine_min": 1.0, "machine_max": 2.0, "machine_q_ref": 200.0,
    }
    base.update(kw)
    return base


def _leaf(**kw):
    row = {"id": "T1", "name": "T1", "quantity": 500.0, "unit": "m²",
           "duration_days": 10, "kb_activity_id": "__none__"}
    row.update(kw)
    return row


def _plain_item(**kw):
    """合成台账条目（只给 norm_coverage_report 读的键）。"""
    item = {"usable": True, "cap_labor": None, "cap_machine": None,
            "kb_activity_id": "ACT1", "not_usable_reason": ""}
    item.update(kw)
    return item


# ================================================================ A：segments_factor 语义
def test_segments_factor_contract_says_zero_one_gate():
    """契约原文钉住结论：§4 的列注释是 `# 0/1`，§5-WS4 ⑤ 只在 `== 1` 时放大。

    这条用例防的是"下一个人又把 `segments_factor` 当折减系数乘进公式"。
    """
    doc = (BACKEND.parent / "docs" / "修改契约_v1.md").read_text(encoding="utf-8")
    assert "segments_factor,                          # 0/1" in doc
    assert "segments_factor=1 时：cap_total = cap_labor × 并行段数" in doc
    assert "segments_factor INTEGER DEFAULT 1" in doc


def test_parallel_segment_gate_is_boolean_not_multiplier():
    """闸门语义：1 / True / 缺列 / None / 非法值 → 放行；0 / "0" → 拦下。"""
    assert S.segments_parallel_enabled({"segments_factor": 1}) is True
    assert S.segments_parallel_enabled({"segments_factor": 0}) is False
    assert S.segments_parallel_enabled({"segments_factor": "0"}) is False
    assert S.segments_parallel_enabled({"segments_factor": 0.0}) is False
    # 缺列 / None / 空串 / 老库 / 非法值 → 按"真"处理（缺省就是 1，向后兼容）
    assert S.segments_parallel_enabled({}) is True
    assert S.segments_parallel_enabled(None) is True
    assert S.segments_parallel_enabled({"segments_factor": None}) is True
    assert S.segments_parallel_enabled({"segments_factor": ""}) is True
    assert S.segments_parallel_enabled({"segments_factor": "坏值"}) is True
    # 它**不是**乘数：0.5 也不能把容量折半（合法域只有 0/1，非 0 一律按放行）
    assert S.segments_parallel_enabled({"segments_factor": 0.5}) is True


def test_segment_count_reads_explicit_keys_only():
    """显式段数键才认；节拍叶子自带的 `_zone`/`_segment` 不是段总数。"""
    assert S._segment_count({}) == 1
    assert S._segment_count(None) == 1
    assert S._segment_count({"segments": 3}) == 3
    assert S._segment_count({"_segments": 2}) == 2
    assert S._segment_count({"segment_count": 4}) == 4
    assert S._segment_count({"workface_segments": 5}) == 5
    assert S._segment_count({"segment_total": 6}) == 6
    assert S._segment_count({"parallel_segments": 2}) == 2
    # 非法/0 → 1（不放大、不除零）
    assert S._segment_count({"segments": 0}) == 1
    assert S._segment_count({"segments": "x"}) == 1
    # 节拍叶子：`_zone=2`/`_segment=3` 是"本条是第 2 区第 3 段"，不是段总数 → 必须为 1
    assert S._segment_count({"_beat": True, "_zone": 2, "_segment": 3, "_step": 1}) == 1


def test_gate_zero_forces_single_segment():
    """闸门拦下时，段数按 1 算（即使叶子显式写了段数）。"""
    leaf = _leaf(segment_count=2)
    assert S.parallel_segment_count(leaf, _rule(segments_factor=1)) == 2
    assert S.parallel_segment_count(leaf, _rule(segments_factor=0)) == 1
    # 没有标定行（workface=None）时只看叶子显式键
    assert S.parallel_segment_count(leaf, None) == 2
    assert S.parallel_segment_count(_leaf(), None) == 1


def test_segments_factor_zero_keeps_quantity_undivided():
    """非 1（= 0）时的行为：`Q_seg` 不按段折减 → 容量按**总量**算（更大、更保守）。

    标定：base=8 / q_ref=200 / step 50·1 / min 4 / max 15，叶子量 500、显式 2 段。
      · 闸门开（1）：Q_seg = 250 → 8 + ⌊50/50⌋ = 9 人（**每段** 9 人）
      · 闸门关（0）：Q_seg = 500 → 8 + ⌊300/50⌋ = 14 人
    两条都低于新上限 20（= max(15, ceil(8×2.5))），所以夹取不干扰本用例。
    """
    rule = dict(_rule(quantity_unit="m²", crew_base=8.0, crew_step_q=50.0,
                      crew_step_n=1.0, crew_min=4.0, crew_max=15.0, q_ref=200.0))
    on = _leaf(quantity=500.0, unit="m²", segment_count=2,
               workface_capacity=dict(rule, segments_factor=1))
    off = _leaf(quantity=500.0, unit="m²", segment_count=2,
                workface_capacity=dict(rule, segments_factor=0))
    cap_on, _ = S.workface_limits_from_rule(on, 500.0, "m²")
    cap_off, _ = S.workface_limits_from_rule(off, 500.0, "m²")
    assert cap_on == 8 + math.floor((250 - 200) / 50) == 9
    assert cap_off == 8 + math.floor((500 - 200) / 50) == 14
    # 单段（无显式段数）时两者一致：闸门对"本来就只有一段"的任务无影响
    plain_on = _leaf(quantity=500.0, unit="m²",
                     workface_capacity=dict(rule, segments_factor=1))
    plain_off = _leaf(quantity=500.0, unit="m²",
                      workface_capacity=dict(rule, segments_factor=0))
    assert S.workface_limits_from_rule(plain_on, 500.0, "m²")[0] == 14
    assert S.workface_limits_from_rule(plain_off, 500.0, "m²")[0] == 14


@pytest.mark.skipif(not DB.exists(), reason="随仓库附带的 kb.db 不存在")
def test_kb_segments_factor_is_all_one_today():
    """A3 只读事实：`Workface_Capacity_Rule.segments_factor` 全表都是 1。

    这是"只记录事实、不改公式行为"的依据。若这条用例失败，说明库被人改过
    —— 那是**数据事件**（要逐行看语义：0 = 不按施工段并行），不是代码回归。
    """
    con = sqlite3.connect(str(DB))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(Workface_Capacity_Rule)")]
        if not cols:
            pytest.skip("KB 无 Workface_Capacity_Rule")
        rows = con.execute("SELECT segments_factor, COUNT(*) FROM "
                           "Workface_Capacity_Rule GROUP BY segments_factor").fetchall()
        assert rows, "容量表为空"
        # 只钉"非 1 行数 == 0"这个事实（不钉总行数：库会被别的迁移加行，
        # 实测 2026-09 为 478 行全 1）
        non_unit = [r for r in rows if r[0] not in (1, 1.0)]
        assert non_unit == [], \
            "segments_factor 出现非 1 行（实测全表都是 1）：%s" % (rows,)
    finally:
        con.close()


@pytest.mark.skipif(not PLAN.exists(), reason="随仓库附带的 plan_run_1789827002.json 不存在")
def test_plan_leaves_are_single_segment_so_factor_affects_nothing():
    """A4 口径：当前计划 322 条任务里，受 `segments_factor` 影响的条数 **= 0**。

    实测：322/322 条叶子都不带段数键（`parallel_segment_count == 1`），且解析出的
    标定行 `segments_factor` 全为 1 → 闸门开、段数 1 → 改前 = 改后（主控机械与人工
    容量都不变）。这正是"全表皆 1 就不改公式行为"的可复核证据。
    """
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    leaves = [lf for ph in plan["wbs"]["phases"]
              for wp in ph.get("work_packages", [])
              for lf in wp.get("sub_packages", [])]
    assert len(leaves) == 322
    seg_counts = set()
    affected = 0
    seen_factor = set()
    for lf in leaves:
        rule = S.resolve_workface(lf)
        rule = rule if isinstance(rule, dict) else {}
        n = S.parallel_segment_count(lf, rule)
        seg_counts.add(n)
        seen_factor.add(rule.get("segments_factor"))
        # 会改变结果的只有"闸门关 且 段数 > 1"这一种组合
        if not S.segments_parallel_enabled(rule) and S._segment_count(lf) > 1:
            affected += 1
    assert seg_counts == {1}, "有叶子带显式段数：%s" % (seg_counts,)
    assert seen_factor <= {1, 1.0, None}, "标定行里出现非 1 的 segments_factor：%s" % (
        seen_factor,)
    assert affected == 0, "受 segments_factor 影响的任务数应为 0，实际 %d" % affected


# ================================================================ B1：上限策略
# ⚠️ C8-7（2026-09-21）：`CREW_CEILING_BAND=2.5` / `CREW_CEILING_CAP=40` /
# `effective_crew_max()`（`max(crew_max, min(40, ceil(crew_base×2.5)))` 那条
# **无规范依据的 ×2.5 带**）已按 C 组 C8 删除清单第 7 项**整条删除**。
# 本节原断言（"上限被抬高到 20/25/30/40"）因此全部作废，改为钉死"该口径不存在"。
def test_crew_ceiling_band_is_deleted():
    """×2.5 带已删：常量、函数、以及它在共用入口的抬升效果都不许再存在。"""
    assert not hasattr(S, "CREW_CEILING_BAND"), "×2.5 带宽常量必须已删"
    assert not hasattr(S, "CREW_CEILING_CAP"), "单段 40 人天花板必须已删"
    assert not hasattr(S, "effective_crew_max"), "曲线抬人函数必须已删"
    assert not hasattr(org_defaults, "crew_ceiling_from_curve")
    assert not hasattr(org_defaults, "CREW_CEILING_BAND")
    assert not hasattr(org_defaults, "CREW_CEILING_CAP")
    assert not hasattr(org_defaults, "CREW_CURVE_REF")
    assert not hasattr(org_plan, "CREW_CEILING_BAND")
    assert not hasattr(org_plan, "effective_crew_max")


def test_capacity_formula_applies_ceiling_only_when_asked():
    """纯公式（默认）仍夹在标定行的 `crew_max`；上限可由调用方显式传入。

    口径变更必须显式发生：默认路径永远是契约 §5-WS4 ⑤ 的纯公式
    （`crew_max=15` 时 99999 夹到 15）。
    """
    rule = _rule(quantity_unit="m²")
    assert S.workface_capacity_for_qty(rule, 99999.0, "m²", kind="labor") == 15
    assert S.workface_capacity_for_qty(rule, 99999.0, "m²", kind="labor",
                                       crew_ceiling=30) == 30
    # crew_ceiling 为 None → 等价于不传（向后兼容的默认路径）
    assert S.workface_capacity_for_qty(rule, 99999.0, "m²", kind="labor",
                                       crew_ceiling=None) == 15


def test_shared_entry_point_no_longer_raises_labor_ceiling():
    """共用入口：人工上限**不再是 ×2.5 带**，就是标定行的裸 `crew_max`。"""
    leaf = _leaf(quantity=14200.0, unit="m²", workface_capacity=_rule(quantity_unit="m²"))
    cap_labor, cap_machine = S.workface_limits_from_rule(leaf, 14200.0, "m²")
    # 14200 → 12 + ⌊13200/500⌋ = 38 → 夹到标定行的 crew_max = 15（改前被抬到 30）
    assert cap_labor == 15, "裸 crew_max（改前是 max(15, ceil(12×2.5))=30）"
    # 机械：base=1 / step 50·1 / q_ref=200 / max=2 → 1 + ⌊14000/50⌋ → 夹到 machine_max=2
    assert cap_machine == 2

    machine = _leaf(quantity=320.0, unit="m", workface_capacity=_rule(
        quantity_unit="m", machine_base=1.0, machine_min=1.0, machine_max=2.0,
        machine_q_ref=200.0, machine_step_q=50.0, machine_step_n=1.0))
    # 320 m → 1 + ⌊120/50⌋ = 3 台 → 夹到 machine_max = 2
    _cl, cap_m = S.workface_limits_from_rule(machine, 320.0, "m")
    assert cap_m == 2

    # 机械分支本身也不吃 crew_ceiling（显式传了也只作用于人工）
    rule = _rule()
    assert S.workface_capacity_for_qty(rule, 99999.0, "m²", kind="machine",
                                       crew_ceiling=40) == 2


def test_ledger_item_has_no_ceiling_recalibration():
    """台账留痕：`crew_max_effective` 就是裸 `crew_max`，`crew_ceiling_raised` 恒假。"""
    leaf = _leaf(quantity=14200.0, unit="m²", workface_capacity=_rule(quantity_unit="m²"))
    item = S._build_ledger_item(leaf, "T1", "T1")
    assert item["crew_max_raw"] == 15.0
    assert item["crew_max_effective"] == 15.0, "改前是 30（×2.5 带）"
    assert item["crew_ceiling_raised"] is False, "不再有「上限被抬高」这回事"
    assert item["segments_factor"] == 1
    assert item["parallel_segments"] == 1
    assert item["cap_labor"] == 15, "台账里的容量值就是标定行的 crew_max"

    # 没有标定行的叶子：两个键都要给"缺"（None），不许凑数
    bare = S._build_ledger_item(_leaf(workface_capacity=None), "T2", "T2")
    assert bare["crew_max_raw"] is None and bare["crew_max_effective"] is None
    assert bare["crew_ceiling_raised"] is False


# ================================================================ B3：覆盖率报告计数
def test_coverage_report_counts_workface_saturation():
    """`workface_saturated`：容量公式值顶到上限的条数；
    `workface_ceiling_raised`：C8-7 之后**恒为 0**（不再有抬升口径）。"""
    ledger = {
        "1.1.1": _plain_item(cap_labor=30, crew_max_raw=15, crew_max_effective=15),
        "1.1.2": _plain_item(cap_labor=7, crew_max_raw=15, crew_max_effective=15),
        "1.1.3": _plain_item(cap_labor=25, crew_max_raw=25, crew_max_effective=25),
        "1.1.4": _plain_item(cap_labor=7, crew_max_raw=25, crew_max_effective=25),
        "1.1.5": _plain_item(usable=False, not_usable_reason="AI估算定额",
                             cap_labor=30, crew_max_raw=15, crew_max_effective=15),
    }
    cov = S.norm_coverage_report(ledger, total=5)
    assert cov["workface_saturated"] == 2, "顶到上限的是 1.1.1 与 1.1.3"
    assert cov["workface_ceiling_raised"] == 0, "×2.5 带已删 → 不可能有上限被抬高"
    assert cov["bound"] == 4


def test_coverage_report_workface_keys_default_to_zero_without_data():
    """缺数据（老台账 / 没有容量信息）→ 记 0，不抛异常。"""
    ledger = {
        "9.9.1": _plain_item(),                       # 只有 usable，没有容量键
        "9.9.2": _plain_item(cap_labor=5),            # 有容量值但没有上限信息
        "9.9.3": _plain_item(usable=False, not_usable_reason="KB无定额行"),
    }
    cov = S.norm_coverage_report(ledger, total=3)
    assert cov["workface_saturated"] == 0
    assert cov["workface_ceiling_raised"] == 0
    assert cov["total"] == 3 and cov["bound"] == 2


def test_coverage_report_never_reports_a_raised_ceiling():
    """老台账（没有重定键）→ 仍从 `workface_capacity` 现算，但**不再有抬高**。"""
    ledger = {
        "8.8.1": _plain_item(cap_labor=30, workface_capacity={
            "max_labor": 30, "crew_base": 12, "crew_min": 4, "crew_max": 15}),
    }
    cov = S.norm_coverage_report(ledger, total=1)
    assert cov["workface_ceiling_raised"] == 0, "15 就是上限，没有「15 → 30」这回事"
    assert cov["workface_saturated"] == 1, "cap_labor=30 >= 上限 15"



# ================================================================ 第 40 轮：来源闸门
# 用户实测：`示例3_住宅楼.txt` 原文一条资源数据都没有，boundary 节点让 LLM 按
# "18 层住宅常见做法"补了 labor.peak_total=120，这个**模型补的值**却被下游当"用户限额"
# 用来卡排程。判据（`boundary_conditions["_source"]`）：model → 不采纳（留痕）；
# user → 照旧采纳；没有 `_source` → 保持旧行为（旧计划/直接传 dict 的调用方）。
def _bc_with_source(sources, **kw):
    bc = {"labor": {"peak_total": 120}}
    bc.update(kw)
    bc["_source"] = sources
    return bc


def test_model_sourced_labor_peak_is_not_adopted():
    """`_source` 标 model 的 120 不当限额用；标 user / 无标注 → 照旧。"""
    model = S.parse_boundary_limits(_bc_with_source({"labor.peak_total": "model"}))
    assert model["labor_total"] is None, "模型补的 120 不许当用户限额"
    assert model["ignored_model_values"], "拦下的值必须留痕（绝不静默丢弃）"
    assert any("120" in t and "model" in t for t in model["ignored_model_values"])

    user = S.parse_boundary_limits(_bc_with_source({"labor.peak_total": "user"}))
    assert user["labor_total"] == 120
    assert user["ignored_model_values"] == []

    # 没有 `_source`（旧计划 / 既有用例直接传 boundary dict）→ 保持旧行为
    legacy = S.parse_boundary_limits({"labor": {"peak_total": 120}})
    assert legacy["labor_total"] == 120
    assert legacy["ignored_model_values"] == []
    # `_source` 在但该键没标注 → 同样按旧行为采纳
    part = S.parse_boundary_limits(_bc_with_source({"materials": "model"}))
    assert part["labor_total"] == 120


def test_model_sourced_trade_equipment_and_target_are_gated():
    """分工种 / 设备 / 目标工期同样按 `_source` 判；别名与正键共用同一标注。"""
    bc = _bc_with_source(
        {"labor.by_trade": "model", "equipment": "model",
         "project_duration_days": "model"},
        labor={"peak_total": 120, "by_trade": [{"trade": "钢筋工", "quantity": 3}]},
        equipment=[{"name": "塔吊", "quantity": 2}],
        project_duration_days=365)
    lim = S.parse_boundary_limits(bc)
    assert lim["by_trade"] == {} and lim["equipment"] == {}
    assert lim["user_target"] is None
    assert len(lim["ignored_model_values"]) == 3

    # 别名必须一起拦：只拦正键会留下"模型值换个键就进来了"的后门
    alias = S.parse_boundary_limits({
        "labor_peak": 120, "trade_peak": [{"trade": "钢筋工", "quantity": 3}],
        "equipment_peak": [{"name": "塔吊", "quantity": 2}], "user_target_days": 365,
        "_source": {"labor.peak_total": "model", "labor.by_trade": "model",
                    "equipment": "model", "project_duration_days": "model"}})
    assert alias["labor_total"] is None and alias["by_trade"] == {}
    assert alias["equipment"] == {} and alias["user_target"] is None

    # 标 user → 全部照旧采纳（含别名）
    user = S.parse_boundary_limits({
        "labor_peak": 120, "trade_peak": [{"trade": "钢筋工", "quantity": 3}],
        "equipment_peak": [{"name": "塔吊", "quantity": 2}], "user_target_days": 365,
        "_source": {"labor.peak_total": "user", "labor.by_trade": "user",
                    "equipment": "user", "project_duration_days": "user"}})
    assert user["labor_total"] == 120 and user["user_target"] == 365
    assert user["by_trade"] == {"钢筋工": 3} and user["equipment"] == {"塔吊": 2}
    assert user["ignored_model_values"] == []


def test_model_sourced_labor_total_does_not_cap_the_schedule_end_to_end():
    """端到端：模型补的"总人工上限 1 人"不生效（峰值仍是工作面上限 6、无超限记录）；
    标 user / 无标注时照旧生效。"""
    from test_scheduler import leaf as mk_leaf, make_wbs, run_scheduler

    def _run(boundary):
        wbs = make_wbs(mk_leaf("A", "A", "钢筋工", 1000, 10, 1.0, cap_labor=6))
        return run_scheduler(wbs, boundary=boundary)[1]["schedule_versions"]

    model = _run({"labor": {"peak_total": 1}, "_source": {"labor.peak_total": "model"}})
    user = _run({"labor": {"peak_total": 1}, "_source": {"labor.peak_total": "user"}})
    legacy = _run({"labor": {"peak_total": 1}})

    def _total_records(ver):
        return [r for r in (ver["resource_ok"].get("over_limit") or [])
                if r.get("resource") == "总人工"]

    assert model["resource_ok"]["peak_labor"] == 6, "区间峰值仍是工作面上限，没被 1 人压住"
    assert _total_records(model) == [], "模型补的上限不该产生「已达上限」记录"
    assert _total_records(user), "用户给的 1 人照旧生效（必须留痕）"
    assert _total_records(legacy), "没有 _source → 旧行为：照旧采纳"
    # 拦下模型值必须说出来（用户看得见，不然会以为"我给的 120 被吞了"）
    assert any("model" in w for w in model["warnings"]), model["warnings"]

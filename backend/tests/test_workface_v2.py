"""WS4 工作面容量 v2 标定（契约 §5-WS4）回归门 —— 第 37 轮。

被验证的口径（全部来自 `docs/修改契约_v1.md` §5-WS4 与 §4 容量标定）：

  ① 删掉 `workface_is_evidence()` 的**否决语义**：
     `source_type=ai_estimate` / `confidence=LOW` 只是置信度标注，
     容量照常参与计算（LOW ≠ 禁用）。
  ② 班组：theory_min 无条件 `班组 := cap`；resource_ok `班组 := min(cap, 用户限额)`，
     没有用户限额时两者相等。
  ③ 删掉"按目标工期反推班组"的分支。
  ④ 机械总台班 = 换算到定额分母单位后的量 ÷ basis × 台班定额；
     `verdict == unusable` 的定额直接降级（沿用 WBS 工期）。
  ⑤ `cap = clamp(base + step_n × ⌊(Q_seg − q_ref)/step_q⌋, min, max)`，
     `step_n == 0` / `step_q` 缺失 → 退化为常量 `clamp(base, min, max)`；
     `Q_seg = 总量 ÷ 段数`。

这些用例直接跑模块级纯函数（不经过整条流水线），并且**只在 KB 存在对应表/行时**
才做数值断言（KB 缺列/缺行时退化为结构断言），避免本仓库的 KB 版本差异造成 flaky。
"""
from __future__ import annotations

import math
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes import scheduler as S                          # noqa: E402

DB = BACKEND.parent / "BuildPlan_KB" / "kb.db"


# ---------------------------------------------------------------- 夹具
def _rule(**kw):
    base = {
        "unit_basis": "每施工段",
        "quantity_unit": "m³",
        "source_type": "ai_estimate",
        "confidence": "LOW",
        "crew_base": 12.0, "crew_step_q": 500.0, "crew_step_n": 1.0,
        "crew_min": 4.0, "crew_max": 15.0, "q_ref": 1000.0,
        "segments_factor": 1.0,
        "machine_base": 1.0, "machine_step_q": 50.0, "machine_step_n": 1.0,
        "machine_min": 1.0, "machine_max": 2.0, "machine_q_ref": 200.0,
    }
    base.update(kw)
    return base


def _leaf(**kw):
    row = {
        "id": "T1", "name": "T1", "quantity": 1972.0, "unit": "m²",
        "duration_days": 14,
        "norm_binding": {"mode": "labor", "norm_value": 0.025,
                         "productivity_value": 40.0, "unit": "工日/m²",
                         "quantity_basis": 10.0, "source_code": "LD_T72_6_2008",
                         "match_type": "exact", "labor_types": ["模板工"]},
    }
    row.update(kw)
    return row


# ================================================================ ⑤ 容量公式
def test_capacity_formula_matches_contract():
    """契约 §5-WS4 ⑤：clamp(base + step_n × ⌊(Q−q_ref)/step_q⌋, min, max)。

    FORM_NEW_OTHER 的真实标定：base=12、q_ref=1000、step 500/1、min=4、max=15。
    Q=1972 → 12 + ⌊972/500⌋ = 13 人（**13**，不是旧表同族的 10）。
    """
    rule = _rule(quantity_unit="m²")
    assert S.workface_capacity_for_qty(rule, 1972.0, "m²", kind="labor") == 13
    # 低于 q_ref 时同样按公式走（floor 会给负步数），min 兜底
    assert S.workface_capacity_for_qty(rule, 0.0, "m²", kind="labor") == 10
    assert S.workface_capacity_for_qty(rule, 500.0, "m²", kind="labor") == 11
    # 超过 max 时夹到 max
    assert S.workface_capacity_for_qty(rule, 99999.0, "m²", kind="labor") == 15


def test_capacity_formula_constant_when_step_n_zero():
    """`crew_step_n == 0`（现库 95 行：项/樘/块/台/座…）必须退化为常量。

    不除零、不抛异常：cap = clamp(base, min, max)。
    """
    rule = _rule(crew_step_n=0.0, crew_base=6.0, crew_min=6.0, crew_max=6.0)
    for q in (0.0, 1.0, 12345.0):
        assert S.workface_capacity_for_qty(rule, q, "m³", kind="labor") == 6
    # step_q 缺失 → 同样退化
    rule2 = _rule(crew_step_q=None, crew_step_n=2.0, crew_base=5.0,
                  crew_min=1.0, crew_max=9.0)
    assert S.workface_capacity_for_qty(rule2, 1000.0, "m³", kind="labor") == 5


def test_capacity_formula_clamps_and_unit_guards():
    """夹取 + 量纲护栏：量纲不符不许套公式（m² 的量套 m 的标定毫无意义）。"""
    rule = _rule(crew_base=100.0, crew_min=4.0, crew_max=15.0)
    assert S.workface_capacity_for_qty(rule, 10.0, "m³", kind="labor") == 15
    assert S.workface_capacity_for_qty(rule, 10.0, "m²", kind="labor") is None
    assert S.workface_capacity_for_qty({}, 10.0, "m³", kind="labor") is None


def test_segment_quantity_divides_by_parallel_segments():
    """Q_seg = 总量 ÷ 并行段数；段数为 1 时 `cap_total = cap_labor`（不放大）。"""
    rule = _rule(quantity_unit="m³", crew_base=8.0, crew_step_q=50.0,
                 crew_step_n=1.0, crew_min=4.0, crew_max=15.0, q_ref=200.0)
    one = _leaf(quantity=500.0, unit="m³", kb_activity_id="__none__",
                workface_capacity=rule)
    two = _leaf(quantity=500.0, unit="m³", kb_activity_id="__none__",
                workface_capacity=rule, segment_count=2)
    cap_one, _ = S.workface_limits_from_rule(one, 500.0, "m³")
    cap_two, _ = S.workface_limits_from_rule(two, 500.0, "m³")
    assert cap_one == 8 + math.floor((500 - 200) / 50)          # 14
    assert cap_two == 8 + math.floor((250 - 200) / 50)          # 9（按段量算）


# ================================================================ ① 否决语义
def test_low_confidence_capacity_still_participates():
    """①：LOW / ai_estimate 照常参与封顶（旧语义"只作参考"已废除）。"""
    assert S.workface_is_evidence({"source_type": "ai_estimate", "confidence": "LOW"})
    leaf = _leaf(kb_activity_id="__none__",
                 workface_capacity=_rule(quantity_unit="m²", max_labor=10))
    cap_labor, _ = S.workface_limits_from_rule(leaf, 1972.0, "m²")
    assert cap_labor == 13, "v2 公式值优先于旧表同族的 max_labor=10"
    item = S._build_ledger_item(leaf, "T1", "T1")
    assert item["cap_labor"] == 13
    assert item["workface_capacity"]["confidence"] == "LOW", "置信度仍要进报告"


# ================================================================ ②③ 班组来源
def test_theory_min_fills_workface_capacity_unconditionally():
    """②：没有用户限额时，两版的班组都 := cap（顶满工作面容量）。"""
    leaf = _leaf(quantity=1972.0, kb_activity_id="__none__",
                 workface_capacity=_rule(quantity_unit="m²"))
    item = S._build_ledger_item(leaf, "T1", "T1")
    # 1972 → 12 + ⌊(1972−1000)/500⌋ = 12 + 1 = 13
    assert item["cap_labor"] == 13
    plan_t = S._plan_task(item, {"equipment": {}, "by_trade": {}}, False, {})
    plan_r = S._plan_task(item, {"equipment": {}, "by_trade": {}}, True, {})
    assert plan_t["crew"] == {"模板工": 13.0}
    assert plan_r["crew"] == {"模板工": 13.0}, "没有用户限额 → 两版一致"
    # 工期由定额决定：1972 m² ÷ 40 m²/工日 = 50 人日 ÷ 13 人 → 4 天
    assert plan_t["duration"] == math.ceil(1972 / (40.0 * 13))
    assert not any("目标工期" in c["reason"] for c in plan_t["capped"])


def test_resource_ok_takes_min_of_cap_and_user_limit():
    """②：resource_ok 的班组 = min(工作面上限, 用户限额)。"""
    leaf = _leaf(kb_activity_id="__none__",
                 workface_capacity=_rule(quantity_unit="m²"))
    item = S._build_ledger_item(leaf, "T1", "T1")
    plan = S._plan_task(item, {"equipment": {}, "by_trade": {"模板工": 5}}, True, {})
    assert plan["crew"] == {"模板工": 5.0}
    assert any("用户" in c["reason"] for c in plan["capped"]), "压限额必须留痕"


def test_no_target_duration_crew_inference_in_source():
    """③：源码里不得再有「工程量 ÷(产能 × 目标工期)」反推班组。"""
    src = (BACKEND / "pipeline" / "nodes" / "scheduler.py").read_text(encoding="utf-8")
    squeezed = src.replace(" ", "").replace("\n", "")
    assert "(productivity*target)" not in squeezed
    assert "(productivity*int(target))" not in squeezed


# ================================================================ ④ 机械换算
def test_machine_total_shifts_uses_conversion_factor():
    """④：120 根 × 18 m/根 = 2160 m；2160 ÷ 100 × 0.49 = 10.58 台班。"""
    item = {
        "quantity": 120.0, "unit": "根", "basis": 100.0, "norm_value": 0.49,
        "machine_norm_unit_pair": {"verdict": "convertible", "factor": 18.0,
                                   "denominator": "m"},
    }
    assert S.machine_total_shifts(item) == pytest.approx(10.584)
    # 分母缺失 / 不可换算 → 退回基准口径（旧行为），绝不按 1:1 硬套
    item["machine_norm_unit_pair"] = {"verdict": "unusable", "factor": None,
                                      "denominator": ""}
    assert S.machine_total_shifts(item) == pytest.approx(120 * 0.49 / 100)


def test_machine_plan_takes_capacity_machine_count():
    """机械台数 := 工作面 machine_max（不是按目标工期反推），工期 = 台班 ÷ 台数。"""
    leaf = _leaf(id="P1", name="P1", quantity=120.0, unit="根", duration_days=30,
                 kb_activity_id="__none__", workface_capacity=None)
    leaf["norm_binding"] = {"mode": "machine", "norm_value": 0.49,
                            "unit": "台班/m", "quantity_basis": 100.0,
                            "ctx_value": {"pile_length_m": 18.0},
                            "convert_factor": 18.0, "convert_denominator": "m",
                            "unit_check": {"verdict": "convertible", "factor": 18.0,
                                           "denominator": "m"},
                            "machine_name": "静力压桩机", "match_type": "exact"}
    leaf["workface_capacity"] = _rule(quantity_unit="m", machine_base=1.0,
                                      machine_max=2.0, machine_q_ref=200.0)
    item = S._build_ledger_item(leaf, "P1", "P1")
    assert item["usable"] is True and item["norm_value"] == 0.49
    # 工作面标定按 m 算，但叶子的量是「根」→ 标定行的单位是 m，公式里量纲不同则回退旧键
    plan = S._plan_task(item, {"equipment": {}, "by_trade": {}}, False, {})
    assert plan["duration"] == math.ceil(10.584 / float(item["cap_machine"] or 1))
    assert "静力压桩机" in plan["resources"]


def test_unusable_unit_binding_is_rejected():
    """④：`unit_check.verdict == unusable` 的定额直接降级（沿用 WBS 工期）。"""
    leaf = _leaf(id="X1", name="X1", quantity=120.0, unit="根", duration_days=25,
                 kb_activity_id="__none__", workface_capacity=None)
    leaf["norm_binding"] = {"mode": "labor", "norm_value": 2.966, "unit": "工日/m³",
                            "quantity_basis": 1.0, "match_type": "exact",
                            "usable": False,
                            "not_usable_reason": "单位不一致且不可换算：任务「根」 vs 定额分母「m³」",
                            "unit_check": {"verdict": "unusable", "factor": None,
                                           "denominator": "m³"},
                            "labor_types": ["普工"]}
    item = S._build_ledger_item(leaf, "X1", "X1")
    assert item["usable"] is False
    assert "不可换算" in item["not_usable_reason"], "要沿用绑定层给的中文原因"
    plan = S._plan_task(item, {"equipment": {}, "by_trade": {}}, False, {})
    assert plan["duration"] == 25, "降级后沿用 WBS 工期"


# ================================================================ 回退链
@pytest.mark.skipif(not DB.exists(), reason="随仓库附带的 kb.db 不存在")
def test_capacity_table_is_present_and_calibrated():
    """KB 层：合表后的唯一容量表存在，且公式列都在（不许回退到兼容键取值）。"""
    import sqlite3
    con = sqlite3.connect(str(DB))
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(Workface_Capacity_Rule)")]
        if not cols:
            pytest.skip("KB 无 Workface_Capacity_Rule（合表迁移尚未执行）")
        for need in ("crew_base", "crew_step_q", "crew_step_n", "crew_min",
                     "crew_max", "q_ref", "legacy_max_labor", "legacy_max_machine"):
            assert need in cols, "容量表缺列 %s" % need
        n = con.execute("SELECT COUNT(*) FROM Workface_Capacity_Rule").fetchone()[0]
        assert n > 0
        # 旧两表必须只以归档名存在（运行时表只有一个）
        live = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='Workface_Capacity_Rule_v2'").fetchone()
        assert live is None, "Workface_Capacity_Rule_v2 不该还存在（应已改名为 _legacy_v2）"
    finally:
        con.close()


def test_capacity_fallback_chain_never_returns_default_ceiling_for_machine():
    """回退链：v2 → 旧键 max_labor / max_machine → 人工 DEFAULT_CEILING / 机械 1 台。

    特别是机械侧：**绝不**用 DEFAULT_CEILING 让台数无限（台数直接决定工期）。
    """
    no_cap = _leaf(kb_activity_id="__none__")
    cap_labor, cap_machine = S.workface_limits_from_rule(no_cap, 100.0, "m²")
    assert (cap_labor, cap_machine) == (None, None)
    # 旧键兜底
    legacy = _leaf(kb_activity_id="__none__",
                   workface_capacity={"max_labor": 7, "max_machine": 1})
    assert S.workface_limits_from_rule(legacy, 100.0, "m²") == (7.0, 1.0)
    # 缺容量时的兜底常量：机械 1 台
    assert S.DEFAULT_MACHINE_FALLBACK == 1
    assert S.workface_cap_of({"mode": "machine", "cap_machine": None,
                              "cap_labor": None, "labor_name": "普工"},
                             "静力压桩机") == S.DEFAULT_CEILING


def test_machine_plan_warns_when_capacity_missing():
    """缺容量数据时机械任务**按叶子排期反推台数**，并记中文留痕（绝不静默）。

    ⚠️ 期望值变更原因（第 44 轮补，用户 2026-09-21 实测 `9.2.5 管沟回填夯实` 161 天）：
       旧行为是"缺容量 → 台数固定兜底 1 台 → 工期 = 总台班 ÷ 1"，
       实测把一条**叶子排期 12 天**的工序算成 **161 天**（2900 m³ × 5.53 台班/100m³）。
       新行为：**无任何容量依据时**，改按叶子既有排期反推台数
       （`台数 = ⌈总台班 ÷ 叶子工期⌉`，总台班守恒、不编造定额），
       有 MWI 容量或用户设备清单时**一律不介入**（台数仍以容量为唯一真源）。
       本例总台班 = 500 × 0.05 = 25，叶子排期 10 天 → 台数 ⌈25/10⌉ = 3 台，
       工期 ⌈25 ÷ 3⌉ = 9 天（≤ 叶子排期）。两条留痕都会记：先记"缺容量数据"，
       再记"按叶子排期定台数"。这是**预期**变化，不是被掩盖的失败。
    """
    leaf = _leaf(id="M9", name="M9", quantity=500.0, unit="m³", duration_days=10,
                 kb_activity_id="__none__", workface_capacity=None)
    leaf["norm_binding"] = {"mode": "machine", "norm_value": 0.05, "unit": "台班/m³",
                            "quantity_basis": 1.0, "match_type": "exact",
                            "machine_name": "塔吊", "unit_check": {"verdict": "same",
                                                                   "factor": 1.0,
                                                                   "denominator": "m³"}}
    item = S._build_ledger_item(leaf, "M9", "M9")
    plan = S._plan_task(item, {"equipment": {}, "by_trade": {}}, False, {})
    # ⚠️ 期望值变更原因（第 39 轮）：知识库配员表 `Equipment_Crew_Mapping` 新增了
    #    「塔吊」（司机1名+信号工1名）与「施工电梯」（司机1名）两行（用户直接指令，
    #    source_type='user_directive' / confidence='LOW'）。`_plan_task` 的机械分支按
    #    `machine_crew_of()` 把机组配员计入 `resources`/`crew` —— 3 台塔吊 →
    #    司机 3 人 + 信号工 3 人。这是**预期**变化，不是被掩盖的失败。
    assert plan["resources"] == {"塔吊": 3.0, "司机": 3.0, "信号工": 3.0}
    assert plan["crew"] == {"司机": 3.0, "信号工": 3.0}, "crew 与 resources 同一口径（总人数）"
    assert plan["duration"] == math.ceil(500 * 0.05 / 3.0) == 9
    assert any("缺工作面容量数据" in c["reason"] for c in plan["capped"])
    assert any("无容量依据按叶子排期定台数" in c["reason"] for c in plan["capped"]), \
        "台数被反推时必须留痕（绝不静默）"


# ================================================================ 用户设备限额绑定
def test_machine_alias_matching_binds_same_machine():
    """同机异名必须绑上：用户「静压桩机」 ↔ KB「静力压桩机」；「挖掘机」↔「履带式单斗液压挖掘机」。

    旧实现是纯子串匹配（`key in name or name in key`）：
    「静压桩机」不是「静力压桩机」的子串 → 用户申报的 1 台桩机被**静默丢弃**，
    公式给 2 台就真的上 2 台（实测 2.1.1 从 11 天掉到 6 天）。
    """
    limits = {"equipment": {"静压桩机": 1, "挖掘机": 1, "塔吊": 1}}
    assert S._match_limit("静力压桩机", limits["equipment"]) == (1, "静压桩机")
    assert S._match_limit("履带式单斗液压挖掘机", limits["equipment"]) == (1, "挖掘机")
    assert S._match_limit("塔吊QTZ80", limits["equipment"]) == (1, "塔吊")
    # 类型核心词：桩机族归一，"静力压桩机" 与 "静压桩机" 必须同族
    assert S.machine_type("静力压桩机") == S.machine_type("静压桩机") == "桩机"
    # 不同设备不许错绑：用户只申报了塔吊/泵车时，桩机不该匹配到它们的限额
    assert S._match_limit("静力压桩机", {"塔吊": 1, "混凝土泵车": 1}) == (None, None)


def test_unmatched_user_equipment_is_reported_not_silently_dropped():
    """用户申报了、计划里没有对应资源 → 必须出结构化对账 + 中文告警（绝不静默）。"""
    limits = {"equipment": {"静压桩机": 1, "塔吊": 1}}
    report = S.equipment_binding_report(limits, ["静力压桩机"])
    assert report["静压桩机"]["effective"] is True
    assert report["静压桩机"]["bound_to"] == "静力压桩机"
    assert report["塔吊"]["effective"] is False
    assert report["塔吊"]["bound_to"] is None
    ws = S.unmatched_equipment_warnings(report)
    assert len(ws) == 1 and "塔吊" in ws[0] and "未生效" in ws[0]


def test_user_equipment_limit_caps_machine_count_end_to_end():
    """端到端：用户「静压桩机」1 台（异名）必须把公式给的 2 台压到 1 台，工期 11 天。"""
    from test_scheduler import leaf as mk_leaf, make_wbs, run_scheduler
    row = mk_leaf("P1", "PHC 静压桩", "静力压桩机", 120.0, 30, 0.0)
    row["unit"] = "根"
    row["norm_binding"] = {"mode": "machine", "norm_value": 0.49, "unit": "台班/m",
                           "quantity_basis": 100.0, "ctx_value": {"pile_length_m": 18.0},
                           "convert_factor": 18.0, "convert_denominator": "m",
                           "unit_check": {"verdict": "convertible", "factor": 18.0,
                                          "denominator": "m"},
                           "machine_name": "静力压桩机", "match_type": "exact"}
    row["workface_capacity"] = _rule(quantity_unit="m", machine_base=1.0,
                                     machine_min=1.0, machine_max=2.0,
                                     machine_q_ref=200.0, machine_step_q=50.0,
                                     machine_step_n=1.0)
    _, out = run_scheduler(make_wbs(row), boundary={"equipment": [
        {"name": "静压桩机", "quantity": 1}]})
    v = out["schedule_versions"]
    rec = [c for c in v["resource_ok"]["capped"] if c["task_id"] == "P1"]
    assert rec and rec[0]["got"] == 1, rec
    assert "用户资源限额" in rec[0]["reason"]
    # theory_min 不吃用户限额（公式 2 台 → 6 天），resource_ok 吃（1 台 → 11 天）
    assert v["resource_ok"]["total_duration_days"] == math.ceil(10.584 / 1) == 11
    assert v["theory_min"]["total_duration_days"] == math.ceil(10.584 / 2)


# ================================================================ 稳态：不变量
def test_two_versions_invariant_theory_le_resource_ok():
    """核心不变量：theory_min ≤ resource_ok（两版差异只能来自用户限额）。"""
    from test_scheduler import (leaf as mk_leaf, make_wbs, run_scheduler,
                                rows_of)
    wbs = make_wbs(
        mk_leaf("A", "A", "钢筋工", 1000, 10, 1.0, cap_labor=6),
        mk_leaf("B", "B", "钢筋工", 1000, 10, 1.0, cap_labor=6),
    )
    _, out = run_scheduler(wbs, boundary={"labor": {"by_trade": [
        {"trade": "钢筋工", "quantity": 3}]}})
    v = out["schedule_versions"]
    assert v["theory_min"]["total_duration_days"] <= v["resource_ok"]["total_duration_days"]
    assert rows_of(v["theory_min"])["A"]["crew"]["钢筋工"] == 6
    assert rows_of(v["resource_ok"])["A"]["crew"]["钢筋工"] == 3

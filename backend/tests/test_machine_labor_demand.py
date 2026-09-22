# -*- coding: utf-8 -*-
"""机械主导任务的「人工需求」口径测试

背景（真实缺口）：知识库把现浇混凝土这类活动标成 `equipment_driven`，于是它们的工期
由**台班定额**决定，而 `design_crew_demand()` 只统计 labor 主导的任务 —— 这些活动的
**人工工日**就完全没进"本工程要多少工日"。可混凝土是要人的（KB 里 CONC_NEW_SLAB
挂着 360 行人工定额）。不计进来，用户拿来自查用工总量的那个数就是**偏小的**，
而且**看不出来偏**。

处置：**单独报出来，绝不用它改工期**（改"机械主导任务工期由台班决定"这个口径属于
口径变更，必须单独评估）。本文件守住三件事：
  1. 有 KB 人工定额的机械主导任务 → 人工需求被算出来并报出
  2. **工期一个数都不许变**（只是多报一行账）
  3. 取不到可信定额时**不猜**（样本不足 3 行不报；AI 经验估算行自政策变更
     2026-09-20 起与真人定额同一口径计入样本，但样本门槛不变）

运行：python -m pytest backend/tests/test_machine_labor_demand.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest

from pipeline import kb
from pipeline.nodes.scheduler import (
    _ai_source, _median, compute_schedules, machine_labor_demand,
    typical_labor_productivity,
)

# 知识库里确有大量人工定额、且被标为 equipment_driven 的真实活动
CONC_ACT = "CONC_NEW_SLAB"
REBAR_ACT = "REBAR_NEW_SLAB"


def _machine_leaf(tid, activity_id, quantity=100.0, unit="m³", shift_norm=0.5,
                  labor_types=None, duration=10, machine_cap=None):
    """一条机械主导的叶子（台班定额决定工期）。

    `machine_cap`：显式给出**工作面机械容量**（写入叶子的 `workface_capacity`）。
    第 44 轮补起，机械台数有两条来源：① 工作面容量 / 用户设备清单（权威）；
    ② 都没有时**按叶子既有排期反推**（`台数 = ⌈总台班 ÷ 叶子工期⌉`，见
    `test_workface_v2.test_machine_plan_warns_when_capacity_missing`）。
    要测"工期只由台班定额决定、不随目标工期变"，就必须走 ① —— 所以要显式给容量。
    """
    leaf = {
        "id": tid, "name": tid, "quantity": quantity, "unit": unit,
        "duration_days": duration, "work_type": "混凝土工程",
        "kb_activity_id": activity_id,
        "norm_binding": {
            "task_id": tid, "mode": "machine",
            "norm_value": shift_norm, "quantity_basis": 1.0,
            "unit": "台班/%s" % unit,
            "source_code": "LD_T72_8_2008", "match_type": "exact",
            "labor_types": labor_types or ["混凝土工"],
            "machine_name": "混凝土输送泵",
        },
    }
    if machine_cap is not None:
        leaf["workface_capacity"] = {"max_machine": machine_cap}
    return leaf


def _labor_leaf(tid, activity_id, quantity=100.0, unit="t", productivity=0.2):
    return {
        "id": tid, "name": tid, "quantity": quantity, "unit": unit,
        "duration_days": 5, "work_type": "钢筋工程",
        "kb_activity_id": activity_id,
        "norm_binding": {
            "task_id": tid, "mode": "labor",
            "productivity_value": productivity, "quantity_basis": 1.0,
            "unit": "工日/%s" % unit,
            "source_code": "LD_T72_7_2008", "match_type": "exact",
            "labor_types": ["钢筋工"],
        },
    }


def _wbs(*rows):
    return {"phases": [{"phase": "测试", "work_packages": [
        {"id": "1.1", "name": "wp", "sub_packages": list(rows)}]}]}


def _run(wbs):
    """compute_schedules 返回一个 dict（schedule_versions / norm_coverage /
    machine_labor_demand 等）—— 直接返回，别解包。"""
    return compute_schedules(wbs, {"dependencies": []}, {}, {})


# ==================== 1. 前置事实：KB 里确实有这些定额 ====================
def test_precondition_kb_has_labor_norms_for_the_machine_activity():
    assert kb.labor_norms(CONC_ACT), "测试前提：该活动应有人工定额行"
    rows = [r for r in kb.labor_norms(CONC_ACT) if not _ai_source(r.get("source_code"))]
    assert len(rows) >= 3, "测试前提：应有不少于 3 行非 AI 人工定额"


def test_typical_labor_productivity_uses_median_of_real_rows():
    prod, n = typical_labor_productivity(CONC_ACT)
    assert prod and prod > 0
    assert n >= 3
    # 中位数应当落在该活动产能的实际区间内（而不是第一行那种极端值）
    vals = [r["productivity_value"] for r in kb.labor_norms(CONC_ACT)
            if not _ai_source(r.get("source_code")) and r.get("productivity_value")]
    assert min(vals) <= prod <= max(vals)


# ==================== 2. 工具函数 ====================
def test_median_helper():
    assert _median([3, 1, 2]) == 2
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([]) is None
    assert _median([5]) == 5


def test_ai_source_predicate_identifies_ai_estimate_sources():
    """`_ai_source()` 现在只用来**标注来源**（政策变更 2026-09-20）。

    断言本身没变（它就是个前缀/子串判据），但语义变了：旧口径下它同时是"不许算
    工期"的否决判据；新口径下 AI 定额与真人定额同等参与，它只决定要不要打
    `released_ai` 标注。函数名保留是因为既有调用方（本文件、覆盖率统计）在 import 它。
    """
    assert _ai_source("AI_ESTIMATE_V1")
    assert _ai_source("ai_estimate_v1")
    assert _ai_source("AI_FOO")
    assert not _ai_source("LD_T72_8_2008")
    assert not _ai_source("GD_2018_A1_5")
    assert not _ai_source("")
    assert not _ai_source(None)


# ==================== 3. 机械主导任务的人工需求被算出来 ====================
def test_machine_task_labor_demand_is_reported():
    out = _run(_wbs(_machine_leaf("M1", CONC_ACT, quantity=100.0)))
    md = out["machine_labor_demand"]
    assert md.get("demand"), "机械主导任务的人工需求必须被报出来"
    assert "混凝土工" in md["demand"]
    prod, _n = typical_labor_productivity(CONC_ACT)
    assert md["demand"]["混凝土工"] == pytest.approx(100.0 / prod, rel=1e-6)
    d = md["detail"]["混凝土工"]
    assert d["tasks"] == 1 and d["activity"] == CONC_ACT and d["norm_rows"] >= 3
    assert d["median_productivity"] == pytest.approx(prod)


def test_warning_states_both_numbers_and_that_duration_is_unchanged():
    out = _run(_wbs(_machine_leaf("M1", CONC_ACT, quantity=100.0)))
    ws = out["schedule_versions"]["warnings"]
    hit = [w for w in ws if "机械主导任务" in w]
    assert hit, ws
    text = hit[0]
    assert "实际总用工" in text, "要给出两个口径加起来的汇总，用户才能核对"
    assert "本版不用该需求延长工期" in text, "必须说清它没有参与工期计算"
    # 政策变更（2026-09-20）：AI 经验估算行不再被排除，文案必须如实说"同样计入样本"
    assert "中位数" in text and "AI 经验估算行" in text and "计入样本" in text, \
        "产能口径与 AI 行的处置要写出来（旧文案写的是「已排除 AI 估算行」）"
    assert any(CONC_ACT in w and "人工定额" in w for w in ws), "明细要能溯源到活动编号"


# ==================== 4. 关键回归：只是报账，一个工期数字都不许变 ====================
def test_machine_task_duration_is_unchanged_by_this_reporting():
    """第 37 轮口径（契约 §5-WS4②③）：**有工作面容量依据时**，台数只来自工作面容量 /
    用户设备清单，**绝不许按目标工期反推**（旧实现 50 台班 ÷ 10 天 = 5 台 → 工期被
    "凑"成 10 天）。

    ⚠️ 第 44 轮补（用户 2026-09-21 实测 `9.2.5 管沟回填夯实` 161 天）：本叶子原先
    **并没有真的给容量** —— `_machine_leaf` 不写 `workface_capacity`，台数=1 其实
    来自"缺容量 → 兜底 1 台"这条路径。新规则会让**这条兜底路径**改按叶子既有排期
    反推台数（总台班 50 ÷ 叶子 10 天 = 5 台 → 工期 10 天），于是本用例不再是它自称的
    "台数由工作面容量决定"。现改为**显式给出** `max_machine=1`（与该文档口径一致），
    本用例重新只测它该测的那件事：新增的"机械主导任务人工需求"只是报账，
    工期 / 台数 / 峰值一个都不许变。无容量依据那条新路径由
    `test_workface_v2.test_machine_plan_warns_when_capacity_missing` 覆盖。
    """
    wbs = _wbs(_machine_leaf("M1", CONC_ACT, quantity=100.0, shift_norm=0.5, duration=10,
                             machine_cap=1))
    out = _run(wbs)
    version = out["schedule_versions"]["theory_min"]
    row = version["schedule"][0]
    machines = int(version["peak_equipment"])
    assert machines == 1, "无用户设备清单时台数由工作面容量决定；目标工期 10 天不参与反推"
    assert row["ef"] - row["es"] == 50, "总台班 50 ÷ 1 台 = 50 天（不是目标工期 10 天）"
    assert any(c.get("task_id") == "M1" and int(c.get("got") or 0) == 1
               for c in (version.get("capped") or [])), "台数必须留痕（不静默）"
    assert out["machine_labor_demand"]["demand"], "需求确实报了"
    # 但工时曲线里不该凭空多出混凝土工（那会让人以为它已经参与排程）
    assert all("混凝土工" not in (rec.get("trades") or {})
               for rec in version["daily_labor"])


def test_labor_dominated_tasks_are_not_double_counted():
    """labor 主导任务已进 design_crew_demand，不许再进 machine_labor_demand。"""
    out = _run(_wbs(_labor_leaf("L1", REBAR_ACT)))
    assert out["machine_labor_demand"]["demand"] == {}, "劳动主导任务不能被重复计入"


def test_two_tasks_of_one_trade_are_summed():
    rows = [
        _machine_leaf("M1", CONC_ACT, quantity=50.0, labor_types=["混凝土工"]),
        _machine_leaf("M2", CONC_ACT, quantity=50.0, labor_types=["混凝土工"]),
    ]
    out = _run(_wbs(*rows))
    md = out["machine_labor_demand"]
    assert md["detail"]["混凝土工"]["tasks"] == 2
    prod, _n = typical_labor_productivity(CONC_ACT)
    assert md["demand"]["混凝土工"] == pytest.approx(100.0 / prod, rel=1e-6)


# ==================== 5. 取不到可信定额就不猜 ====================
def test_no_activity_id_means_no_demand():
    leaf = _machine_leaf("M1", CONC_ACT)
    leaf.pop("kb_activity_id")
    out = _run(_wbs(leaf))
    assert out["machine_labor_demand"]["demand"] == {}


def test_unknown_activity_means_no_demand():
    out = _run(_wbs(_machine_leaf("M1", "NOT_A_REAL_ACTIVITY")))
    assert out["machine_labor_demand"]["demand"] == {}


def test_ai_only_activity_rows_now_count_but_sample_floor_holds():
    """只有 AI 估算定额的活动 → **行现在计入样本**（政策变更 2026-09-20）。

    实测（本仓库 KB）：`WALL_AI_001`（内墙抹灰）只有 **1 行**、来源 `AI_ESTIMATE_V1`。
      · 旧口径（第 37~39 轮）：AI 行整行排除 → `(None, 0)`；
      · 新口径：AI 行与真人定额同一口径计入 → `(None, 1)`；
        "至少 3 行才报中位数"这条门槛**对 AI 与真人一视同仁**（1 行不许当代表值），
        所以仍然不报产能 —— 放开的是"AI 能不能用"，不是"样本够不够"。
    """
    prod, n = typical_labor_productivity("WALL_AI_001")
    assert n == 1, "AI 估算行现在要计入样本（政策变更 2026-09-20）"
    assert prod is None, "样本不足 3 行的门槛不变 —— 1 行不许当代表值"


def test_fewer_than_three_rows_is_not_enough():
    """样本不足 3 行宁可不算（只有 1~2 行时中位数等于单条定额，不可信）。"""
    prod, n = typical_labor_productivity("CONC_NEW_WALL")
    if prod is not None:
        assert n >= 3, "报出产能时必须样本充足"


def test_demand_function_handles_garbage_ledger():
    """畸形台账不崩（排程器整体容错，统计函数不能成为例外）。"""
    for ledger in ({}, {"x": None}, {"x": "不是字典"}):
        demand, detail = machine_labor_demand(ledger, ["x"])
        assert demand == {} and detail == {}


def test_plan_meta_carries_the_machine_labor_demand():
    """口径账本要进计划元数据（交付物里能核对），且契约不许把它丢掉。"""
    from pipeline.nodes.plan_assembler import build_meta
    from pipeline import schemas

    meta = build_meta({"machine_labor_demand": {"demand": {"混凝土工": 123.0}, "detail": {}}})
    assert meta["machine_labor_demand"]["demand"]["混凝土工"] == 123.0
    dumped = schemas.PlanJson.model_validate({
        "plan_id": "x",
        "overview": {"project_name": "n", "total_duration_days": 1,
                     "planned_start_date": "2026-01-01", "planned_end_date": "2026-01-02",
                     "critical_path_length": 0},
        "wbs": {"phases": []}, "dependencies": [],
        "cpm_result": {"total_duration_days": 1}, "resource_demand": {},
        "meta": meta,
    }).model_dump()
    assert dumped["meta"]["machine_labor_demand"]["demand"]["混凝土工"] == 123.0

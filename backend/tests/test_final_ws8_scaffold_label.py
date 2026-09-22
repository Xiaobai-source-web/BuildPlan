# -*- coding: utf-8 -*-
"""终版修改 · WS8 验收门：`SCAFFOLD_V1` 占位定额在交付层必须算**非规范来源**。

背景（用户 2026-09-20 裁定「保留占位，但必须全面如实标注」）
------------------------------------------------------------
WS6 给 PC 吊装等活动写了 6 行**估的** `SCAFFOLD_V1` 台班占位
（`source_type='scaffold_placeholder'` / `status='needs_review'`）。
交付层原来的 AI 判据只认 `AI_*` / `AI_ESTIMATE`，不认 `SCAFFOLD_V1` →
一条由占位定额驱动的任务会被判成「规范台班 / 规范人工」，
**并计入 `critical_norm_coverage` 的分子**（虚报覆盖率，WS8 缺陷 ②）。

本文件锁定修复后的判据：
  ① `_norm_tier_of` 对 `SCAFFOLD` 前缀来源（不论 mode=machine/labor/mixed）
     一律返回「AI 定额（已审）」（常量 `NORM_TIER_AI`），**不是**规范两档；
  ② `SCAFFOLD_V1` 不在 `NORM_TIER_IS_SPEC` 里 → 不进覆盖率分子；
  ③ 真规范来源（GD_2018_* / LD_T72*）与 AI 来源的原有判定**未被误伤**。

运行：cd backend && python -m pytest tests/test_final_ws8_scaffold_label.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import delivery as D             # noqa: E402

SCAFFOLD = "SCAFFOLD_V1"


def _rd(tid, norm):
    """最小 resource_demand 任务记录（判据只看 `_norm_applied` 与资源）。"""
    return {"task_id": tid, "task_name": tid, "quantity": 100.0,
            "planned_duration_days": 5, "_norm_applied": norm,
            "resources": {"普工": {"per_day": 1}}}


def _scaffold_norm(mode):
    return {"mode": mode, "norm_value": 1.0, "source_code": SCAFFOLD,
            "match_type": "scaffold", "origin": "kb"}


SCAFFOLD_MACHINE = _scaffold_norm("machine")
SCAFFOLD_LABOR = _scaffold_norm("labor")
GD_MACHINE = {"mode": "machine", "norm_value": 1.5, "source_code": "GD_2018_A1_1",
              "match_type": "exact", "origin": "kb"}
LD_LABOR = {"mode": "labor", "norm_value": 0.1, "source_code": "LD_T72_6_2008",
            "match_type": "exact", "origin": "kb"}
AI_LABOR = {"mode": "labor", "norm_value": 0.1, "source_code": "AI_ESTIMATE_V1",
            "match_type": "ai", "origin": "ai"}


# ═══════════════ ① 交付层档次：SCAFFOLD 一律非规范 ═══════════════

def test_scaffold_v1_machine_是AI档次不是规范台班():
    tier = D._norm_tier_of(_rd("ws8m", SCAFFOLD_MACHINE))[0]
    assert tier == D.NORM_TIER_AI, (
        "SCAFFOLD_V1 是类别占位（无真规范依据），mode=machine 也必须算「%s」，"
        "绝不能冒充「%s」；实得 %r" % (D.NORM_TIER_AI, D.NORM_TIER_MACHINE, tier))
    assert tier != D.NORM_TIER_MACHINE, "占位定额被算成了规范台班：%r" % tier


def test_scaffold_v1_labor_是AI档次不是规范人工():
    tier = D._norm_tier_of(_rd("ws8l", SCAFFOLD_LABOR))[0]
    assert tier == D.NORM_TIER_AI, "实得 %r" % tier
    assert tier != D.NORM_TIER_LABOR, "占位定额被算成了规范人工：%r" % tier


def test_任何SCAFFOLD前缀来源都算非规范():
    """`SCAFFOLD*` 一律非规范：不靠单点写死 `SCAFFOLD_V1`。"""
    for code in ("SCAFFOLD_V1", "SCAFFOLD_V2", "scaffold_v9"):
        norm = dict(SCAFFOLD_MACHINE, source_code=code)
        assert D._norm_tier_of(_rd("x", norm))[0] == D.NORM_TIER_AI, code
    # 判据函数本身（历史名沿用）：非规范来源 = AI_* / AI_ESTIMATE / SCAFFOLD*
    assert D._ai_norm_source_code_ai("SCAFFOLD_V1") is True
    assert D._ai_norm_source_code_ai("AI_ESTIMATE_V1") is True
    assert D._ai_norm_source_code_ai("GD_2018_A1_1") is False
    assert D._ai_norm_source_code_ai("LD_T72_6_2008") is False
    assert D._ai_norm_source_code_ai("") is False


def test_真规范与AI来源判定未被误伤():
    """回归：修复 SCAFFOLD 不能把真规范来源一起打成 AI。"""
    assert D._norm_tier_of(_rd("g", GD_MACHINE))[0] == D.NORM_TIER_MACHINE
    assert D._norm_tier_of(_rd("d", LD_LABOR))[0] == D.NORM_TIER_LABOR
    assert D._norm_tier_of(_rd("a", AI_LABOR))[0] == D.NORM_TIER_AI


def test_ai_norm_state_把SCAFFOLD记为strong():
    st = D._ai_norm_state(_rd("s", SCAFFOLD_MACHINE))
    assert st is not None and st["norm_ai"] is True and st["strong"] is True, st


# ═══════════════ ② 覆盖率分子排除占位 ═══════════════

def _sched(tid, start, days):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    d1 = d0 + timedelta(days=days - 1)
    return {"task_id": tid, "task_name": tid, "start_date": d0.isoformat(),
            "finish_date": d1.isoformat(), "duration_days": days,
            "assigned_resources": {}}


def _plan(rd_tasks, sched, critical, total_days):
    return {
        "plan_id": "plan_ws8_test",
        "overview": {"project_name": "WS8 验收样例", "total_duration_days": total_days},
        "cpm_result": {"total_duration_days": total_days,
                       "critical_path": list(critical)},
        "all_tasks_schedule": sched,
        "resource_demand": {"tasks": rd_tasks},
    }


def test_覆盖率分子排除SCAFFOLD占位():
    """关键路径两条全是 SCAFFOLD 占位 → 分子必须是 0（不是 10/20）。"""
    rds = [_rd("k1", SCAFFOLD_MACHINE), _rd("k2", SCAFFOLD_LABOR),
           _rd("n1", GD_MACHINE)]
    sched = [_sched("k1", "2026-01-01", 10), _sched("k2", "2026-01-11", 20),
             _sched("n1", "2026-02-01", 30)]
    cov = D._norm_critical_coverage(_plan(rds, sched, ("k1", "k2"), 100))
    assert cov is not None, "覆盖率必须算得出来（否则这条测试没验证到东西）"
    assert cov["norm_days"] == 0.0, (
        "占位定额不得进分子；实得 norm_days=%r pct=%r tiers=%s"
        % (cov["norm_days"], cov["pct"], cov["tiers"]))
    assert cov["pct"] == 0.0, cov
    assert cov["tiers"][D.NORM_TIER_AI] == 30.0, cov
    assert D.NORM_TIER_AI not in D.NORM_TIER_IS_SPEC, (
        "NORM_TIER_IS_SPEC 必须只含规范两档，实得 %r" % (D.NORM_TIER_IS_SPEC,))

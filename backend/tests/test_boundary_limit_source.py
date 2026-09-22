# -*- coding: utf-8 -*-
"""第 40 轮 · `boundary_conditions._source` 到"限额"的闸门（resource 侧）。

背景（用户实测）：原文里一条资源数据都没有，boundary 节点却让 LLM 按"18 层住宅常见
做法"补齐了总人工峰值 120、分工种人数、4 台设备 —— 这些**模型编的数**被
`parse_boundary_conditions()` 无差别收成限额，再被 `apply_peak_shaving()` 拿去削峰、
顺带把工期改长。等于把"模型猜的"当成"甲方要求的"。

修法：`_source` 标 `"model"` 的**不纳入限额**（但在 `ignored_model_limits` 里留痕）；
标 `"user"` 的照旧；**没有 `_source`**（旧计划 / 既有用例直接传 dict）保持旧行为。

运行：python -m pytest backend/tests/test_boundary_limit_source.py -q
"""

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from pipeline.nodes.resource import (  # noqa: E402
    apply_peak_shaving, parse_boundary_conditions)

# 用户实测的那份"模型补齐"内容（原文一条资源数据都没有）
MODEL_BOUNDARY = {
    "labor": {
        "peak_total": 120,
        "by_trade": {"钢筋工": 25, "木工": 20, "混凝土工": 18, "架子工": 12},
    },
    "equipment": [{"name": "塔吊", "quantity": 1}, {"name": "施工电梯", "quantity": 1},
                  {"name": "混凝土泵车", "quantity": 1}, {"name": "静压桩机", "quantity": 1}],
    "_source": {
        "labor.peak_total": "model",
        "labor.by_trade": "model",
        "equipment": "model",
        "materials": "model",
    },
    "_source_note": "「user」= 用户在自己提供的文件/参数里明确给出；「model」= 模型按常见做法补齐（非用户输入）",
}


def test_model_limits_are_not_taken_as_caps():
    """标 `model` → 一条都不进限额，且逐项留痕（绝不静默）。"""
    b = parse_boundary_conditions(MODEL_BOUNDARY)
    assert b["equipment_peak"] == {}, "模型补的设备台数不是用户限额"
    assert b["trade_peak"] == {}, "模型补的分工种人数不是用户限额"
    ignored = b.get("ignored_model_limits") or []
    assert "equipment.塔吊=1" in ignored
    assert "equipment.静压桩机=1" in ignored
    assert "labor.by_trade.钢筋工=25" in ignored
    assert len(ignored) == 8, ignored       # 4 台设备 + 4 个工种


def test_model_limits_no_longer_shave_peak_or_extend_duration():
    """闸门的**目的**：模型编的限额不许再削峰、也不许再改工期。"""
    demand = {"钢筋工_per_day": 40, "钢筋工_total_days": 400,
              "planned_duration_days": 10}
    b = parse_boundary_conditions(MODEL_BOUNDARY)
    out = apply_peak_shaving(dict(demand), b)
    assert out == demand, "模型限额不该产生任何削峰"
    assert "_adjusted" not in out


def test_user_limits_are_still_taken_as_caps():
    """标 `user` → 照旧纳入（削峰行为不变）。"""
    raw = {
        "labor": {"by_trade": {"钢筋工": 25}},
        "equipment": {"塔吊": 1},
        "_source": {"labor.by_trade": "user", "equipment": "user"},
    }
    b = parse_boundary_conditions(raw)
    assert b["equipment_peak"] == {"塔吊": 1}
    assert b["trade_peak"] == {"钢筋工": 25}
    assert "ignored_model_limits" not in b
    out = apply_peak_shaving({"钢筋工_per_day": 40, "钢筋工_total_days": 400,
                              "planned_duration_days": 10}, b)
    assert out["钢筋工_per_day"] == 25
    assert out["planned_duration_days"] == 16          # 400 / 25


def test_mixed_source_only_drops_the_model_half():
    """`_source` 是逐项的：只丢标了 model 的那一项，user 的那一项照用。"""
    raw = {
        "equipment": {"塔吊": 1},
        "labor": {"by_trade": {"钢筋工": 25}},
        "_source": {"equipment": "model", "labor.by_trade": "user"},
    }
    b = parse_boundary_conditions(raw)
    assert b["equipment_peak"] == {}
    assert b["trade_peak"] == {"钢筋工": 25}
    assert b["ignored_model_limits"] == ["equipment.塔吊=1"]


def test_without_source_keeps_legacy_behaviour():
    """**没有 `_source`** → 保持旧行为（既有大量用例直接传 dict 并期望生效）。"""
    raw = {"equipment": {"塔吊": 1, "施工电梯": 2},
           "labor": {"by_trade": {"钢筋工": 25, "木工": 20}}}
    b = parse_boundary_conditions(raw)
    assert b["equipment_peak"] == {"塔吊": 1, "施工电梯": 2}
    assert b["trade_peak"] == {"钢筋工": 25, "木工": 20}
    assert "ignored_model_limits" not in b

    # 旧形态（列表 + 顶层 labor_peak / equipment_peak / trade_peak）同样不受影响
    legacy = parse_boundary_conditions({
        "equipment_peak": {"塔吊": 3},
        "labor_peak": 88,
        "trade_peak": [{"trade": "钢筋工", "quantity": 7}],
    })
    assert legacy["equipment_peak"] == {"塔吊": 3}
    assert legacy["labor_peak"] == 88
    assert legacy["trade_peak"] == {"钢筋工": 7}


def test_top_level_labor_peak_is_untouched_by_design():
    """`labor_peak` 只读顶层 `labor_peak`/`peak_manpower` —— 本轮不动它（保持现状）。"""
    b = parse_boundary_conditions({
        "labor": {"peak_total": 120},
        "_source": {"labor.peak_total": "model"},
        "peak_manpower": 60,
    })
    assert b["labor_peak"] == 60, "顶层显式值照旧；嵌套 labor.peak_total 仍然不读"


def test_degenerate_inputs_do_not_crash():
    assert parse_boundary_conditions(None) == {
        "equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
    assert parse_boundary_conditions("{bad json") == {
        "equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
    assert parse_boundary_conditions("[]") == {
        "equipment_peak": {}, "labor_peak": None, "trade_peak": {}}
    # `_source` 不是 dict / 值是 null / labor 不是 dict：一律退回旧行为，不抛
    assert parse_boundary_conditions(
        {"equipment": {"塔吊": 1}, "_source": "model"})["equipment_peak"] == {"塔吊": 1}
    assert parse_boundary_conditions(
        {"equipment": {"塔吊": 1}, "_source": {"equipment": None}})["equipment_peak"] == {"塔吊": 1}
    assert parse_boundary_conditions({"labor": "n/a"})["trade_peak"] == {}

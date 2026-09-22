"""工作面容量"随工程量联动"必须真正落到计划上（域 1.6 之后的口径）。

两个不变式（都是实测踩出来的）：

  1. **单一真源**：资源节点（决定看板/Word 上的曲线）与排程节点（决定班组与工期）
     必须用**同一个容量公式**。旧实现里资源节点直接读叶子上的旧表常数，实测同一
     任务两套数（1.5.1 混凝土运输：旧表 4 人 vs v2 公式 15 人），于是 v2 公式那句
     "随工程量变化"从来没体现在交付物上。
  2. **工日守恒**：`人数 × 工期 = 工日需求`。旧实现用 WBS 的目标天数当分母，
     实测 1.5.1 被按 **1 天** 算 → 每天 2216 人 → 压到 4 人 → 曲线上只剩 4 工日，
     自述"仍缺 2212 工日"。用排程实际工期当分母，缺口就消失了。

**第 6 批 / 域 1.6 迁移说明（必读）**：本文件原先靠
`kb.workface_capacity("REBAR_NEW_FOUND")` 去 KB 的 `Workface_Capacity_Rule` 取那条
v2 标定行（`q_ref=22 / crew_base=8 / crew_step_q=10 / crew_step_n=1 / min=4 / max=16`），
取不到时用 `if not rule.get("crew_base"): return` 提前返回。该表与函数**已被删除**
（域 1.6 彻底退役），`try/except` 把 `AttributeError` 吞掉后恒返回 `{}`
→ 3 条用例全部在开头短路、**0 条断言真正执行**（"假绿"，比失败更危险）。

本文件现在把同一条 v2 标定规则**搬到叶子自带的 `workface_capacity` 里** ——
域 1.6 之后这是**唯一仍然有效**的容量来源（`scheduler.resolve_workface()` 与
`resource._resolve_workface()` 都只吃叶子键，见
`backend/pipeline/nodes/scheduler.py::workface_limits_from_rule` 的 docstring）。
断言值仍是实测标定的真值、仍然钉住上面两个不变式；`assert r_l != 14` 也因此重新
有了意义（叶子同时带着旧表常数 `max_labor=14`，公式必须给 7 而不是 14）。

实测数字（`backend/_probe_tmp/probe_workface_link.py`，16.0 t）：
  · 叶子带 v2 规则 → `S.workface_limits_from_rule(...) == (7, None)`
    （`clamp(8 + floor((16 − 22)/10), 4, 16) == clamp(7, 4, 16) == 7`）
  · 叶子只带旧表常数 `max_labor=14` → `== (14.0, None)`（兼容分支仍然说话）
  · 资源节点的 `_workface_caps` 与排程节点的 `workface_limits_from_rule` 逐值相等。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes import resource as R                    # noqa: E402
from pipeline.nodes import scheduler as S                   # noqa: E402

ACT = "REBAR_NEW_FOUND"

# 叶子自带的 v2 标定规则（域 1.6 之前，这份内容在 KB 的 Workface_Capacity_Rule 里）。
V2_RULE = {
    # 旧表常数：只有在 v2 公式项一个都没有时才允许兜底（见 _leaf_legacy_only）
    "max_labor": 14, "max_machine": None,
    "unit_basis": "每施工段",
    "source_type": "ai_estimate", "confidence": "LOW",
    "crew_base": 8.0, "crew_step_q": 10.0, "crew_step_n": 1.0,
    "crew_min": 4.0, "crew_max": 16.0, "q_ref": 22.0,
    "quantity_unit": "t",
}


def _binding(old_cap=14):
    return {"mode": "labor", "norm_value": 4.43,
            "productivity_value": 0.225734,
            "unit": "工日/t", "source_code": "LD_T72_7_2008",
            "match_type": "default", "usable": True,
            "crew": {"钢筋工": old_cap},
            "provenance": {"origin": "kb"}}


def _leaf(quantity=16.0, days=2, old_cap=14):
    """叶子自带 v2 标定规则 + 旧表常数快照 —— 域 1.6 之后的标准样子。"""
    rule = dict(V2_RULE)
    rule["max_labor"] = old_cap
    return {"id": "1.1.1", "name": "1-0.5层 钢筋绑扎", "quantity": quantity,
            "unit": "t", "duration_days": days, "kb_activity_id": ACT,
            "workface_capacity": rule, "norm_binding": _binding(old_cap)}


def _leaf_legacy_only(old_cap=14):
    """叶子只带旧表那张"每施工段最多几人"的定值表（**没有**任何 v2 公式项）。"""
    leaf = _leaf(old_cap=old_cap)
    leaf["workface_capacity"] = {
        "max_labor": old_cap, "max_machine": None,
        "unit_basis": "每施工段",
        "source_type": "ai_estimate", "confidence": "LOW",
    }
    return leaf


def _caps_of(leaf, quantity=16.0, unit="t"):
    """两个节点各自的容量：排程 `workface_limits_from_rule` vs 资源 `_workface_caps`。"""
    cap = S.workface_limits_from_rule(leaf, quantity, unit)
    r_l, r_m, note = R._workface_caps(R._resolve_workface(leaf), leaf=leaf,
                                      quantity=quantity, unit=unit)
    return cap, (r_l, r_m), note


def test_leaf_borne_v2_rule_is_the_single_source():
    """叶子自带 v2 规则 → 两节点按**同一个公式**给 7 人，不退回旧表常数 14。

    `7 = clamp(8 + floor((16 − 22)/10), 4, 16)`。叶子上一并带着旧表常数
    `max_labor=14`，正是用来抓"又退回旧表常数"的回归。
    """
    leaf = _leaf()
    (cap_l, cap_m), (r_l, r_m), note = _caps_of(leaf)
    assert (cap_l, cap_m) == (7, None), (cap_l, cap_m)
    assert (r_l, r_m) == (cap_l, cap_m), \
        "两节点容量不一致：资源 (%s,%s) vs 排程 (%s,%s)" % (r_l, r_m, cap_l, cap_m)
    assert r_l == 7, r_l
    assert r_l != 14, "不该再退回旧表常数 14"
    assert "工作面容量" in note


def test_legacy_constant_on_leaf_still_caps():
    """兼容分支：叶子只有旧表常数（无 v2 公式项）时，`max_labor` 仍然说话。"""
    (cap_l, cap_m), (r_l, r_m), note = _caps_of(_leaf_legacy_only(14))
    assert (cap_l, cap_m) == (14.0, None), (cap_l, cap_m)
    assert (r_l, r_m) == (cap_l, cap_m), \
        "两节点容量不一致：资源 (%s,%s) vs 排程 (%s,%s)" % (r_l, r_m, cap_l, cap_m)
    assert "工作面容量" in note


def test_scheduled_duration_conserves_person_days():
    """用排程工期当分母 → 人数 == 公式容量，且 人数×工期 ≈ 工日需求。"""
    leaf = _leaf()
    binding = leaf["norm_binding"]
    cap_l, _cap_m = S.workface_limits_from_rule(leaf, 16.0, "t")
    assert cap_l == 7

    # ① 旧口径：按 WBS 目标 2 天算 → 每天要 36 人 → 被容量压到 7 人 → 只剩 14 工日
    short = R.compute_norm_resources(leaf, binding, 16.0, 2)
    assert short["钢筋工_per_day"] == cap_l
    assert short["钢筋工_per_day"] * 2 < 20, "2 天口径必然留下巨大工日缺口"
    assert short["_workface_capped"][0]["original_per_day"] == 36

    # ② 新口径：按排程实际 11 天算 → 每天 7 人 → 7 × 11 = 77 ≈ 定额需求 70.9 工日
    long_ = R.compute_norm_resources(leaf, binding, 16.0, 11)
    assert long_["钢筋工_per_day"] == cap_l == long_["_crew"]["钢筋工"]
    assert abs(long_["钢筋工_per_day"] * 11 - 16.0 / 0.225734) <= 7, \
        "工日必须守恒（实际 %s）" % (long_["钢筋工_per_day"] * 11,)
    assert not long_.get("_workface_capped"), "人数据公式算出来，就不该再触发封顶"


def test_compute_flat_prefers_scheduled_days():
    """`compute_flat(schedule_days=…)` 真的用排程工期，而不是 WBS 目标天数。"""
    leaf = _leaf()
    wbs = {"phases": [{"name": "主体", "work_packages": [
        {"name": "钢筋", "sub_packages": [leaf]}]}]}
    a = R.compute_flat(wbs)
    b = R.compute_flat(wbs, schedule_days={"1.1.1": 11})
    ta = (a.get("resource_demand") or {}).get("tasks") or []
    tb = (b.get("resource_demand") or {}).get("tasks") or []
    assert len(ta) == 1 and len(tb) == 1, "前置条件：两旁应各恰好 1 行资源"

    assert ta[0]["planned_duration_days"] == 2
    assert tb[0]["planned_duration_days"] == 11

    # 决定性证据：2 天口径触发封顶（36 → 7），11 天口径不该触发。
    # 若 compute_flat 忽略 schedule_days、仍按 WBS 的 2 天算，b 也会带封顶痕迹。
    assert ta[0]["_workface_capped"][0]["original_per_day"] == 36
    assert ta[0]["钢筋工_per_day"] == 7
    assert not tb[0].get("_workface_capped"), tb[0].get("_workface_capped")
    assert tb[0]["钢筋工_per_day"] == 7

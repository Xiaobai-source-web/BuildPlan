# -*- coding: utf-8 -*-
"""第 2 批 · 域 7.2（三轮回压）+ 两处跨辖区交接 + 7.11 接线（`scheduler.py`）。

口径与判据（`docs/域7_资源层_实现设计.md`，父代理裁决见 §14）：
  · **7.2 三轮回压**：`_run_version_with_backpressure()` 把一版最多跑
    `scheduler.BACKPRESSURE_MAX_ROUNDS`（**常量 = 3**）轮；轮次之间**只收窄份额**；
    3 轮后仍超限 → **采用超限额值 + 如实标出**（不抛异常、不无限循环）。
  · **裁决 #7**：`_plan_task(daily_share=…)` 用「该任务窗口内份额的最小值」。
  · **裁决 #8**：3 轮触顶的记录**复用既有 `over_limit` 的 `{resource, limit, peak, note}`**
    （键只增不改：新增 `round` / `breached`），且 note 必须是人能读的轮次说明。
  · **裁决 D（硬约束）**：老场景 `daily_share=None`（用户没给限额 / 理论版）必须
    **逐字段退回旧行为** —— 本文件用**改前实测的基线字面量**钉住它。
  · **交接 ①**：`_over_limit_records` 白名单剔除场地级机械（判据 = `site_machine_const`
    的 `machines` 键集，**不写死资源名**）。
  · **交接 ②**：机组配员（司机 / 信号工）**跟台数走、不设限额**（`user_cap_for_task`
    与 `_run_one_version` 的 pool 循环各一处）。`_max_by_crew` **必须保留**。
  · **7.11**：不展开的活动按**施工面积**开段（段数 = 1）；`measure_scope` 是唯一真源。

运行：cd backend && python -m pytest tests\\test_batch4_backpressure.py -q ^
      -p no:cacheprovider --basetemp=_test_tmp\\d72
"""

import copy
import json
import math
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import org_defaults as OD                     # noqa: E402
from pipeline.nodes import scheduler as S                   # noqa: E402

# ══════════════════════════════════════════════════════════════════
# 夹具（与 tests/test_scheduler.py 的 leaf/make_wbs/deps 同形）
# ══════════════════════════════════════════════════════════════════


def leaf(tid, name, trade, quantity, duration, productivity, cap_labor=None,
         cap_machine=None, mode="labor", machine_crew=None, machine_name=None,
         kb_activity_id=None):
    binding = {"task_id": tid, "mode": mode, "productivity_value": productivity,
               "source_code": "TEST_KB", "match_type": "exact", "labor_types": [trade]}
    if mode == "machine":
        binding["norm_value"] = productivity
        binding["productivity_value"] = None
        binding["quantity_basis"] = 1.0
        binding["machine_name"] = machine_name or trade
    row = {"id": tid, "name": name, "quantity": quantity, "unit": "m3",
           "duration_days": duration, "work_type": trade, "norm_binding": binding}
    if kb_activity_id:
        row["kb_activity_id"] = kb_activity_id
    if machine_crew:
        row["machine_crew"] = dict(machine_crew)
    if cap_labor is not None or cap_machine is not None:
        row["workface_capacity"] = {"max_labor": cap_labor, "max_machine": cap_machine,
                                    "unit_basis": "每施工段", "origin": "kb",
                                    "confidence": "LOW", "note": "测试用工作面容量"}
    return row


def make_wbs(*rows):
    return {"phases": [{"phase": "测试阶段", "work_packages": [
        {"id": "1.1", "name": "测试工作包", "sub_packages": list(rows)}]}]}


def deps(*pairs):
    return {"dependencies": [{"predecessor": p, "successor": s, "type": "FS",
                              "lag_days": 0} for p, s in pairs]}


def six_leaf_wbs():
    """`test_scheduler.six_leaf_wbs` 的副本（5 条钢筋工 + 1 条塔吊）。"""
    return make_wbs(
        leaf("1.1.1", "甲区钢筋绑扎", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.2", "甲区模板支设", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.3", "甲区浇筑", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.4", "甲区吊装", "塔吊", 2000, 10, 0.05, cap_machine=5,
             mode="machine", machine_name="塔吊"),
        leaf("1.1.5", "乙区钢筋绑扎", "钢筋工", 20, 2, 1.0, cap_labor=10),
        leaf("1.1.6", "乙区浇筑", "钢筋工", 20, 2, 1.0, cap_labor=10))


def six_deps():
    return deps(("1.1.1", "1.1.5"), ("1.1.3", "1.1.6"))


PARAMS_TINY = {"total_area": 48.0, "building_count": 1, "floors": 1}
PARAMS_TINY12 = {"total_area": 12.0, "building_count": 1, "floors": 1}


def project(version):
    """紧凑投影：只留会随 7.2 变化的量（逐字段可比、人可读）。"""
    rows = sorted(version["schedule"], key=lambda r: str(r["task_id"]))
    return {
        "total_duration_days": version["total_duration_days"],
        "rows": [[str(r["task_id"]), r["es"], r["ef"]] for r in rows],
        "peak_labor": version["peak_labor"],
        "peak_equipment": version["peak_equipment"],
        "over_limit": version["over_limit"],
        "capped": [[c["task_id"], c["resource"], c["want"], c["got"]]
                   for c in version["capped"]],
    }


def run(wbs, dependencies, boundary, params=None):
    out = S.compute_schedules(wbs, dependencies, boundary, params)
    return out["schedule_versions"]


# ══════════════════════════════════════════════════════════════════
# 1 · 裁决 D：老场景（daily_share=None）**逐字段退回旧行为**
#     期望值 = 改造**前**实测基线（`backend/_probe_tmp/q_d72_baseline.py` 的同一夹具）
# ══════════════════════════════════════════════════════════════════
#: 改前实测（2026-09-21，`scheduler.py` 未改任何一行时）：
#:   S1 = six_leaf + 无任何用户限额；S2 = six_leaf + by_trade 钢筋工=8；S5 = 3 条钢筋工 + 无限额
_OLD_S1 = {
    "total_duration_days": 20,
    "rows": [["1.1.1", 0, 2], ["1.1.2", 2, 4], ["1.1.3", 4, 6],
             ["1.1.4", 0, 20], ["1.1.5", 6, 8], ["1.1.6", 8, 10]],
    "peak_labor": 20.0, "peak_equipment": 5.0, "over_limit": [],
    "capped": [["1.1.1", "钢筋工", 10, 10], ["1.1.2", "钢筋工", 10, 10],
               ["1.1.3", "钢筋工", 10, 10], ["1.1.4", "塔吊", 5, 5],
               ["1.1.5", "钢筋工", 10, 10], ["1.1.6", "钢筋工", 10, 10]],
}
_OLD_S2_RESOURCE_OK = {
    "total_duration_days": 20,
    "rows": [["1.1.1", 0, 3], ["1.1.2", 3, 6], ["1.1.3", 6, 9],
             ["1.1.4", 0, 20], ["1.1.5", 9, 12], ["1.1.6", 12, 15]],
    "peak_labor": 18.0, "peak_equipment": 5.0,
    "over_limit": [{"resource": "钢筋工", "limit": 8, "peak": 8.0,
                    "note": "已达上限（钢筋工 限额 8 人）"}],
    "capped": [["1.1.1", "钢筋工", 10, 8], ["1.1.2", "钢筋工", 10, 8],
               ["1.1.3", "钢筋工", 10, 8], ["1.1.4", "塔吊", 5, 5],
               ["1.1.5", "钢筋工", 10, 8], ["1.1.6", "钢筋工", 10, 8]],
}
#: S5：3 条钢筋工 40 工日、层面积 48 m²（段容量 = ⌈48÷12⌉ = 4 人/条）、用户限额 10 人
_OLD_S5 = {
    "total_duration_days": 10,
    "rows": [["A1", 0, 10], ["A2", 0, 10], ["A3", 0, 10]],
    "peak_labor": 12.0, "peak_equipment": 0.0, "over_limit": [],
    "capped": [["A1", "钢筋工", 4, 4], ["A2", "钢筋工", 4, 4], ["A3", "钢筋工", 4, 4]],
}


def _three_rebar():
    return make_wbs(*[leaf("A%d" % i, "钢筋%d" % i, "钢筋工", 40, 4, 1.0, cap_labor=10)
                      for i in (1, 2, 3)])


class TestLegacyPathUnchanged:
    """★ 裁决 D：没有份额时（用户没给限额 / 理论版）逐字段等于改造前。"""

    def test_无用户限额时两版逐字段等于改前基线(self):
        v = run(six_leaf_wbs(), six_deps(), {}, None)
        assert project(v["theory_min"]) == _OLD_S1
        assert project(v["resource_ok"]) == _OLD_S1

    def test_用户限额未被突破时逐字段等于改前基线(self):
        """by_trade 钢筋工=8：峰值恰好 8 = 限额（无严格超限）→ 不回压、输出不变。"""
        v = run(six_leaf_wbs(), six_deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 8}]}}, None)
        assert project(v["theory_min"]) == _OLD_S1
        assert project(v["resource_ok"]) == _OLD_S2_RESOURCE_OK

    def test_无限额时回压轮次恒为1且收敛(self):
        v = run(six_leaf_wbs(), six_deps(), {}, None)
        for ver in ("theory_min", "resource_ok"):
            bp = v[ver]["_backpressure"]
            assert bp["cap"] == S.BACKPRESSURE_MAX_ROUNDS == 3
            assert bp["rounds_used"] == 1, "用户没给限额 → 没有可收窄的维度"
            assert bp["converged"] is True
            assert bp["share_source"] == "demand_ratio"
            assert v[ver]["_daily_share"] == {}, "没有份额时必须空"
            assert len(bp["trace"]) == 1 and bp["trace"][0]["over"] == []

    def test_理论版不做回压(self):
        v = run(six_leaf_wbs(), six_deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 4}]}}, None)
        bp = v["theory_min"]["_backpressure"]
        assert bp["rounds_used"] == 1 and bp["converged"] is True
        assert v["theory_min"]["_daily_share"] == {}
        assert v["theory_min"]["over_limit"] == []

    def test_每日曲线与改前基线一致(self):
        v = run(six_leaf_wbs(), six_deps(), {}, None)
        doc = json.dumps(v["resource_ok"]["daily_labor"], ensure_ascii=False)
        assert '"day": 20' in doc and '"钢筋工": 10.0' in doc
        for rec in v["resource_ok"]["daily_labor"]:
            assert rec["trades"].get("钢筋工", 0) <= 10

    def test_daily_share形状逐位可复现(self):
        """域 7.12：`_daily_share` 已定型（day 是 str、份额是 int、遍历 sorted）。"""
        v = run(_three_rebar(), deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 10}]}},
                PARAMS_TINY)
        share = v["resource_ok"]["_daily_share"]
        assert list(share) == ["钢筋工"], "L1 = 资源名（只有一个资源）"
        assert sorted(share["钢筋工"]) == ["A1", "A2", "A3"], "L2 = task_id"
        for tid in sorted(share["钢筋工"]):
            days = share["钢筋工"][tid]
            assert days, tid
            for day, val in days.items():
                assert isinstance(day, str) and day.isdigit(), day
                assert isinstance(val, int) and val >= 1, (day, val)
        # 份额之和 = 限额（7.4 最大余数法：Σallocated == limit，逐日守恒）
        days = sorted(share["钢筋工"]["A1"])
        assert days, share
        for day in days:
            per_day = 0
            for tid in sorted(share["钢筋工"]):
                per_day += share["钢筋工"][tid].get(day, 0)
            assert per_day == 10, (day, per_day)

    def test_同输入重跑逐位一致(self):
        args = (six_leaf_wbs(), six_deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 8}]}}, None)
        a = json.dumps(run(*args), ensure_ascii=False, sort_keys=True)
        b = json.dumps(run(*args), ensure_ascii=False, sort_keys=True)
        assert a == b

    def test_回压场景重跑也逐位一致(self):
        args = (_three_rebar(), deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 10}]}},
                PARAMS_TINY)
        a = json.dumps(run(*args)["resource_ok"], ensure_ascii=False, sort_keys=True)
        b = json.dumps(run(*args)["resource_ok"], ensure_ascii=False, sort_keys=True)
        assert a == b


# ══════════════════════════════════════════════════════════════════
# 2 · 7.2 主任务：轮次、收敛、3 轮上限、产物键
# ══════════════════════════════════════════════════════════════════
class TestBackpressureRounds:
    def test_严格超限时第二轮收窄并收敛(self):
        """3 条任务各 4 人、同一天并行 = 12 人 > 限额 10 → 第 2 轮收窄后收敛。

        段容量 = ⌈48÷12⌉ = 4 人/条（层面积 48 m²、钢筋工 MWI=12）；
        回压按需求量（各 40 工日）分摊限额 10 → [4, 3, 3] → 工期 10 / 14 / 14 天。
        """
        v = run(_three_rebar(), deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 10}]}},
                PARAMS_TINY)
        ok = v["resource_ok"]
        bp = ok["_backpressure"]
        assert bp["rounds_used"] == 2, bp["trace"]
        assert bp["converged"] is True
        assert len(bp["trace"]) == 2, "每轮一条 trace"
        assert bp["trace"][0]["over"] and bp["trace"][0]["over"][0]["resource"] == "钢筋工"
        assert bp["trace"][0]["over"][0]["peak"] == 12.0
        assert bp["trace"][1]["over"] == [], "第 2 轮收敛"
        assert [r["ef"] - r["es"] for r in ok["schedule"]] == [10, 14, 14]
        assert ok["total_duration_days"] == 14
        assert max(rec["trades"].get("钢筋工", 0) for rec in ok["daily_labor"]) == 10
        # 收敛时超限清单退回"已达上限"（与老口径同形）
        assert ok["over_limit"] == [{"resource": "钢筋工", "limit": 10, "peak": 10.0,
                                     "note": "已达上限（钢筋工 限额 10 人）"}]

    def test_三轮上限常量与触顶标出(self):
        """5 条各 1 人、同一天并行 = 5 人 > 限额 3，且限额 < 条数（7.6 突破）→ 3 轮触顶。

        `_daily_share` 压到 1 人/条后仍 5 > 3（份额只收窄、不再变），**必然**跑满 3 轮。
        """
        wbs = make_wbs(*[leaf("B%d" % i, "钢筋%d" % i, "钢筋工", 20, 2, 1.0, cap_labor=10)
                         for i in (1, 2, 3, 4, 5)])
        v = run(wbs, deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 3}]}},
                PARAMS_TINY12)
        ok = v["resource_ok"]
        bp = ok["_backpressure"]
        assert S.BACKPRESSURE_MAX_ROUNDS == 3
        assert bp["cap"] == 3 and bp["rounds_used"] == 3
        assert bp["converged"] is False, "3 轮后仍超限 → 不收敛，但**绝不抛异常**"
        assert len(bp["trace"]) == 3
        # 裁决 #8：复用既有 over_limit 形状，键只增不改
        rec = ok["over_limit"][0]
        for key in ("resource", "limit", "peak", "note"):
            assert key in rec, key
        assert rec["resource"] == "钢筋工" and rec["limit"] == 3
        assert rec["peak"] > rec["limit"], "必须如实报出超限额值"
        assert rec["round"] == 3 and rec["breached"] is True
        assert "3 轮" in rec["note"], rec["note"]
        assert "采用超限额值" in rec["note"], rec["note"]
        # 人能读的轮次说明（裁定 #8）
        assert "3 轮" in bp["note"] and "采用超限额值" in bp["note"], bp["note"]

    def test_触顶时进warning而不是抛异常(self):
        wbs = make_wbs(*[leaf("B%d" % i, "钢筋%d" % i, "钢筋工", 20, 2, 1.0, cap_labor=10)
                         for i in (1, 2, 3, 4, 5)])
        v = run(wbs, deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 3}]}},
                PARAMS_TINY12)
        assert any("资源不超额工期" in w and "3 轮" in w and "采用超限额值" in w
                   for w in v["warnings"]), v["warnings"][-3:]

    def test_超限记录仍进daily_曲线的真实峰值(self):
        """触顶场景里逐日曲线**如实**超过限额（采用超限额值 = 不静默）。"""
        wbs = make_wbs(*[leaf("B%d" % i, "钢筋%d" % i, "钢筋工", 20, 2, 1.0, cap_labor=10)
                         for i in (1, 2, 3, 4, 5)])
        v = run(wbs, deps(),
                {"labor": {"by_trade": [{"trade": "钢筋工", "quantity": 3}]}},
                PARAMS_TINY12)
        ok = v["resource_ok"]
        assert max(rec["trades"].get("钢筋工", 0) for rec in ok["daily_labor"]) == 5
        assert ok["over_limit"][0]["peak"] == 5.0


# ══════════════════════════════════════════════════════════════════
# 3 · 2.2 的三个纯函数
# ══════════════════════════════════════════════════════════════════
def _curve(day, trades=None, items=None):
    trades = dict(trades or {})
    items = dict(items or {})
    return {"day": day, "labor": float(sum(trades.values())),
            "equipment": float(sum(items.values())), "trades": trades, "items": items}


class TestPureHelpers:
    def test_share_of_取窗口内最小值(self):
        share = {"钢筋工": {"t1": {0: 4, 1: 3, 2: 9}}}
        assert S._share_of(share, "t1", (0, 2)) == 3
        assert S._share_of(share, "t1", (0, 1)) == 4
        assert S._share_of(share, "t1") == 3
        assert S._share_of(share, "t2") is None
        assert S._share_of({}, "t1") is None

    def test_share_of_跨资源取最紧的一条(self):
        share = {"钢筋工": {"t1": {0: 5}}, "塔吊": {"t1": {0: 2}}}
        assert S._share_of(share, "t1", (0, 1)) == 2

    def test_share_of_零份额不采用(self):
        assert S._share_of({"钢筋工": {"t1": {0: 0}}}, "t1") is None

    def test_merge_share_只收窄不放大(self):
        old = {"钢筋工": {"t1": {0: 3, 5: 2}}}
        new = {"钢筋工": {"t1": {0: 5, 1: 4}}, "塔吊": {"t2": {0: 1}}}
        merged = S._merge_share(old, new)
        assert merged["钢筋工"]["t1"] == {0: 3, 1: 4, 5: 2}, merged
        assert merged["塔吊"]["t2"] == {0: 1}
        # 幂等：再并一次同样结果（只收窄）
        assert S._merge_share(merged, new) == merged

    def test_merge_share_确定性逐位一致(self):
        old = {"B": {"t2": {1: 1}}, "A": {"t1": {0: 2}}}
        new = {"A": {"t1": {0: 1}}, "B": {"t2": {1: 3}}}
        a = json.dumps(S._merge_share(old, new), sort_keys=True)
        b = json.dumps(S._merge_share(copy.deepcopy(old), copy.deepcopy(new)), sort_keys=True)
        assert a == b

    def test_daily_share_snapshot_定型且day转str(self):
        snap = S._daily_share_snapshot({"钢筋工": {"t1": {2: 3, 1: 4}}})
        assert snap == {"钢筋工": {"t1": {"1": 4, "2": 3}}}
        assert list(snap["钢筋工"]["t1"]) == ["1", "2"], "键序固定（sorted）"

    def test_demand_weight_走需求量而不是施工量(self):
        item = {"quantity": 99999.0}          # 施工量很大
        plan = {"resource_kind": "labor", "organization": {"demand": 40.0}}
        assert S._demand_weight(item, plan, "钢筋工") == 40.0
        assert S._demand_weight(item, {"resource_kind": "labor"}, "钢筋工") == 0.0

    def test_resource_backpressure_一轮按需求量分摊(self):
        """2 条任务各需求 60 工日、当天各用 6 人 = 12 > 限额 10 → 各分 5。"""
        planned = {}
        ledger = {}
        rows = {}
        for tid in ("t1", "t2"):
            planned[tid] = {"resources": {"钢筋工": 6.0}, "resource_kind": "labor",
                            "organization": {"demand": 60.0}}
            ledger[tid] = {}                   # 台账里取不到定额 → 回退 organization.demand
            rows[tid] = {"es": 0, "ef": 10}
        curves = [_curve(d, {"钢筋工": 12.0}) for d in range(10)]
        limits = {"by_trade": {"钢筋工": 10}, "equipment": {}, "labor_total": None}
        nxt, over, notes = S.resource_backpressure(ledger, planned, rows, curves, limits, 1)
        assert len(over) == 1
        assert over[0]["resource"] == "钢筋工" and over[0]["limit"] == 10
        assert over[0]["peak"] == 12.0 and over[0]["round"] == 1
        assert over[0]["breached"] is False
        assert nxt["钢筋工"]["t1"] == dict((d, 5) for d in range(10)), nxt
        assert nxt["钢筋工"]["t2"] == dict((d, 5) for d in range(10))
        assert notes and "按**需求量**" in notes[0]

    def test_resource_backpressure_用户没给限额就不回压(self):
        planned = {"t1": {"resources": {"钢筋工": 6.0}, "resource_kind": "labor",
                          "organization": {"demand": 60.0}}}
        rows = {"t1": {"es": 0, "ef": 3}}
        curves = [_curve(0, {"钢筋工": 6.0})]
        nxt, over, notes = S.resource_backpressure(
            {"t1": {}}, planned, rows, curves, {"by_trade": {}, "equipment": {}}, 1)
        assert nxt == {} and over == [] and notes == []

    def test_resource_backpressure_权重取不到时不参与分摊(self):
        """需求量一条都取不到 → 不参与分摊（不猜）；该资源**有**限额时如实留痕。"""
        planned = {"t1": {"resources": {"钢筋工": 6.0}}}      # 没有 resource_kind / 定额
        rows = {"t1": {"es": 0, "ef": 2}}
        curves = [_curve(0, {"钢筋工": 6.0})]
        nxt, over, notes = S.resource_backpressure(
            {"t1": {}}, planned, rows, curves, {"by_trade": {"钢筋工": 2}, "equipment": {}}, 1)
        assert nxt == {} and over == [], "权重取不到 → 不分摊，也绝不猜"
        assert notes and "需求量权重取不到" in notes[0], notes

    def test_resource_backpressure_限额小于条数时标出突破(self):
        planned = {}
        rows = {}
        for tid in ("t1", "t2", "t3"):
            planned[tid] = {"resources": {"钢筋工": 2.0}, "resource_kind": "labor",
                            "organization": {"demand": 20.0}}
            rows[tid] = {"es": 0, "ef": 2}
        curves = [_curve(0, {"钢筋工": 6.0})]
        nxt, over, _n = S.resource_backpressure(
            {t: {} for t in planned}, planned, rows, curves,
            {"by_trade": {"钢筋工": 2}, "equipment": {}}, 1)
        assert over[0]["breached"] is True
        assert nxt["钢筋工"] == {"t1": {0: 1, 1: 1}, "t2": {0: 1, 1: 1},
                                 "t3": {0: 1, 1: 1}}, nxt

    def test_resource_backpressure_场地级机械与配员不参与(self):
        planned = {"t1": {"resources": {"塔吊": 12.0}, "resource_kind": "machine",
                          "organization": {"demand": 30.0}}}
        rows = {"t1": {"es": 0, "ef": 3}}
        curves = [_curve(0, items={"塔吊": 12.0})]
        nxt, over, notes = S.resource_backpressure(
            {"t1": {}}, planned, rows, curves,
            {"by_trade": {}, "equipment": {"塔吊": 2}}, 1)
        assert nxt == {} and over == [] and notes == []


# ══════════════════════════════════════════════════════════════════
# 4 · 交接 ①：超限清单永不含场地级机械（判据 = site_machine_const 键集）
# ══════════════════════════════════════════════════════════════════
def _peaks(items):
    return {"labor": 0, "equipment": float(sum(items.values())), "trades": {}, "items": items}


def _const(machines=None):
    return {"schema": 1, "machines": dict(machines or {})}


class TestSiteMachineHandover:
    def test_用户申报塔吊后超限清单里没有塔吊(self):
        limits = S.parse_boundary_limits(
            {"equipment": {"塔吊": 12}, "_source": {"equipment": "user"}})
        assert S._over_limit_records([], _peaks({"塔吊": 12}), limits) == []
        assert S._over_limit_records([], _peaks({"施工电梯": 12}), limits) == []

    def test_常量块存在时同样剔除(self):
        limits = S.parse_boundary_limits({"equipment": {"塔吊": 12}})
        const = _const({"塔吊": {"count": 12, "crew_per_unit": {"司机": 1}}})
        assert S._over_limit_records([], _peaks({"塔吊": 12}), limits,
                                     site_const=const) == []

    def test_普通机械仍然照报(self):
        """白名单只剔场地级机械：别的机械超限必须照旧报出来（不静默）。"""
        limits = S.parse_boundary_limits({"equipment": {"静压桩机": 2}})
        recs = S._over_limit_records([], _peaks({"静压桩机": 3}), limits)
        assert recs and recs[0]["resource"] == "静压桩机"

    def test_判据来源不是写死的资源名(self):
        """判据取自 `site_machine_const.machines` 的**键集**：登记表加一台就跟着剔。"""
        limits = S.parse_boundary_limits({"equipment": {"测试塔": 1}})
        assert S._over_limit_records([], _peaks({"测试塔": 1}), limits)
        const = _const({"测试塔": {"count": 1, "crew_per_unit": {}}})
        assert S._over_limit_records([], _peaks({"测试塔": 1}), limits,
                                     site_const=const) == []

    def test_登记表键集来自org_defaults(self):
        machines, roles = S._site_machine_registry(limits={})
        assert list(machines) == sorted(OD.SITE_MACHINE_MACHINES)
        assert "司机" in roles, "配员角色从 KB 配员原文解析（不写死角色名）"


# ══════════════════════════════════════════════════════════════════
# 5 · 交接 ②：机组配员跟台数走、不设限额；`_max_by_crew` 不许误删
# ══════════════════════════════════════════════════════════════════
def _site_const():
    return OD.build_site_machine_const({"total_area": 215000.0, "building_count": 12,
                                        "floors": 38},
                                       crew_of=lambda m: OD.resolve_site_machine_crew(
                                           m, {"composition": "司机1名+信号工1名"
                                               if m == "塔吊" else "司机1名"}))


class TestCrewRoleHandover:
    def test_配员角色不受用户同类限额夹(self):
        """用户给「司机 1 人」，但塔吊 5 台的配员是 5 司机 —— 跟台数走，不被夹到 1。"""
        wbs = make_wbs(
            leaf("1.1.1", "甲区钢筋绑扎", "钢筋工", 20, 2, 1.0, cap_labor=10),
            leaf("1.1.4", "甲区吊装", "塔吊", 2000, 10, 0.05, cap_machine=5,
                 mode="machine", machine_name="塔吊",
                 machine_crew={"司机": 1, "信号工": 1}))
        boundary = {OD.SITE_MACHINE_CONST_KEY: _site_const(),
                    "labor": {"by_trade": [{"trade": "司机", "quantity": 1}]}}
        v = run(wbs, deps(), boundary, None)
        ok = v["resource_ok"]
        peak = max((rec["trades"].get("司机", 0) for rec in ok["daily_labor"]), default=0)
        assert peak == 5.0, "司机 = 台数 × 每台 1 名 = 5，**不许**被限额夹到 1"
        assert not [r for r in ok["over_limit"] if r["resource"] == "司机"]

    def test_user_cap_for_task_对配员角色返回None(self):
        limits = {OD.SITE_MACHINE_CONST_KEY: _site_const(),
                  "by_trade": {"司机": 1}, "labor_total": 2}
        assert S.user_cap_for_task("司机", limits) is None
        assert S.user_cap_for_task("钢筋工", limits) == 2, "普通工种照旧取最小"

    def test_塔吊不进超限清单即使用户申报过(self):
        """7.8：用户申报过台数时也不进（修好前这里会报"已达上限"）。"""
        wbs = make_wbs(leaf("1.1.4", "甲区吊装", "塔吊", 2000, 10, 0.05, cap_machine=5,
                            mode="machine", machine_name="塔吊",
                            machine_crew={"司机": 1, "信号工": 1}))
        boundary = {OD.SITE_MACHINE_CONST_KEY: _site_const(),
                    "equipment": {"塔吊": 5}, "_source": {"equipment": "user"}}
        v = run(wbs, deps(), boundary, None)
        ok = v["resource_ok"]
        assert max(rec["items"].get("塔吊", 0) for rec in ok["daily_equipment"]) == 5.0
        assert not [r for r in ok["over_limit"] if r["resource"] == "塔吊"], ok["over_limit"]

    def test_max_by_crew必须保留_总人工限额靠少上机器(self):
        """总人工限额 4 人、机组配员 2 人/台 → 最多 2 台（工期同比例延长）。"""
        assert hasattr(S, "_plan_task")
        wbs = make_wbs(leaf("1.1.4", "甲区吊装", "塔吊", 2000, 10, 0.05, cap_machine=5,
                            mode="machine", machine_name="塔吊",
                            machine_crew={"司机": 1, "信号工": 1}))
        boundary = {"labor": {"peak_total": 4}}
        v = run(wbs, deps(), boundary, None)
        rows = dict((r["task_id"], r) for r in v["resource_ok"]["schedule"])
        assert rows["1.1.4"]["ef"] - rows["1.1.4"]["es"] == 50, "5 台→2 台，工期 20→50 天"
        reasons = " ".join(c["reason"] for c in v["resource_ok"]["capped"])
        assert "总人工限额" in reasons and "最多同时上 2 台" in reasons, reasons
        assert "人不够" not in reasons          # 配员没被当成"发限额"的对象


# ══════════════════════════════════════════════════════════════════
# 6 · 7.11 接线：不展开的活动按施工面积开段（段数 = 1）
# ══════════════════════════════════════════════════════════════════
_MWI_REBAR = {"resource_name": "钢筋工", "mwi": 12.0, "mwi_unit": "m2/人",
              "resource_kind": "labor", "resource_mobility": "fixed",
              "capacity_mode": "area"}


class TestSegmentAreaWiring:
    def _item(self, **kw):
        base = {"task_id": "X1", "name": "平整场地", "mode": "labor", "usable": True,
                "quantity": 100.0, "unit": "m²", "norm_value": None,
                "productivity": 1.0, "cap_labor": None, "cap_machine": None,
                "kb_activity_id": "GD_A11_平整场地",
                "is_l5_expandable": 0, "measure_scope": "建筑面积", "segment_id": "",
                "labor_name": "钢筋工", "quantity_basis": 1.0}
        base.update(kw)
        return base

    def test_不展开时按施工面积且段数为1(self):
        table = S._segment_table_for_item(
            self._item(), _MWI_REBAR, 48.0, None, {"total_area": 14200.0}, "钢筋工")
        assert table["ok"] is True
        assert table["segment_ids"] == ["Ⅰ"] and table["segment_areas"] == [14200.0]
        assert table["caliber"] == "construction_area"
        assert "measure_scope" in table["note"] or "施工面积" in table["note"]

    def test_可展开时沿用标准层面积口径(self):
        table = S._segment_table_for_item(
            self._item(is_l5_expandable=1), _MWI_REBAR, 400.0, None,
            {"total_area": 14200.0}, "钢筋工")
        assert table["ok"] is True
        assert table.get("caliber") != "construction_area", "不是施工面积口径"
        assert table["segment_areas"] == [400.0]

    def test_判据未知时不切施工面积口径(self):
        table = S._segment_table_for_item(
            self._item(is_l5_expandable=None), _MWI_REBAR, 400.0, None,
            {"total_area": 14200.0}, "钢筋工")
        assert table["segment_areas"] == [400.0]

    def test_叶子已分段时走可分层口径(self):
        table = S._segment_table_for_item(
            self._item(segment_id="Ⅱ"), _MWI_REBAR, 400.0, None,
            {"total_area": 14200.0}, "钢筋工")
        assert table["segment_areas"] == [400.0]

    def test_用户显式分段规则优先(self):
        table = S._segment_table_for_item(
            self._item(), _MWI_REBAR, 400.0, 2, {"total_area": 14200.0}, "钢筋工")
        assert table.get("caliber") != "construction_area"
        assert len(table["segment_areas"]) == 2, table

    def test_面积取不到时报缺不编(self):
        table = S._segment_table_for_item(
            self._item(measure_scope="建筑面积"), _MWI_REBAR, None, None, {}, "钢筋工")
        assert table["ok"] is False and table["segment_areas"] == []

    def test_plan_organization端到端接上施工面积(self):
        org = S.plan_organization(self._item(), "钢筋工", {}, True, face_area=48.0,
                                  area_params={"total_area": 14200.0})
        assert org is not None
        assert org["segment_count"] == 1
        assert org["capacity_rollup"] == 1184, "⌈14200 ÷ 12⌉ = 1184"
        assert org["segment_area_caliber"] == "construction_area"

    def test_plan_organization把daily_share透传下去(self):
        org = S.plan_organization(self._item(quantity=700.0), "钢筋工", {}, True,
                                  face_area=48.0, daily_share=7,
                                  area_params={"total_area": 14200.0})
        assert org is not None
        assert org["effective_source"] == "daily_share"
        assert org["crew_total"] == 7
        assert org["capacity_rollup"] == 1184, "段容量不受份额影响（只收窄当天能上多少）"
        assert org["duration_days"] == math.ceil(org["demand"] / 7.0)
        assert "逐日份额" in org["basis"] or "份额" in org["basis"], org["basis"]

    def test_非法份额抛ValueError而不是静默(self):
        with pytest.raises(ValueError):
            S.plan_organization(self._item(), "钢筋工", {}, True, face_area=48.0,
                                daily_share=0, area_params={"total_area": 14200.0})

    def test_plan_task新形参是keyword_only(self):
        import inspect
        for fn in (S._plan_task, S.plan_organization):
            sig = inspect.signature(fn)
            for name, param in sig.parameters.items():
                if name in ("daily_share", "site_const", "area_params"):
                    assert param.kind == inspect.Parameter.KEYWORD_ONLY, (fn, name)
                    assert param.default is None, (fn, name)

    def test_三轮上限是常量且不散落(self):
        assert S.BACKPRESSURE_MAX_ROUNDS == 3
        src = Path(S.__file__).read_text(encoding="utf-8")
        assert "range(1, int(BACKPRESSURE_MAX_ROUNDS) + 1)" in src
        assert "while " not in src.split("def _run_version_with_backpressure")[1] \
            .split("def _is_crew_bound_machine")[0]

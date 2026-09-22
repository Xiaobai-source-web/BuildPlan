"""域 7.1 / 7.3 / 7.4 / 7.5 / 7.6 — 有效容量取小、按需求分摊、最大余数法、强制最少一人、限额突破。

运行（**必须带 `--basetemp`**；沙箱 `tmp_path` 会 WinError 5）：
    cd backend
    python -m pytest tests/test_batch4_daily_share.py tests/test_segment_capacity.py \
        tests/test_segment_plan.py -q -p no:cacheprovider --basetemp=_test_tmp\\d7share

覆盖（派工单逐条）
  · 7.1 `effective_capacity_daily(segment_cap, day_share)` = min(段容量, 当天分到的份额)；
        `day_share is None` → 不限（返回段容量）—— 老路径逐位不变的保证点。
  · 7.3 分摊公式 = 限额 × (各自需求量 ÷ 当天合计需求)；**明确不按施工量**（见
        `test_73_not_weighted_by_construction_quantity`）。
  · 7.4 最大余数法，`Σallocated == limit` 恒成立（含浮点容差与 2000 组穷举）。
  · 7.5 强制最少一人；被取整成 0 者从「**小数余数最小**」的那条减 1。
  · 7.6 限额 < 条数 ⇒ 突破限额 + 明确标出（structured `breached=True` + 人可读中文）。
  · 老 `largest_remainder` **一字未改**（6 条钉死断言 + 与老函数并列的差异对照）。

本文件**只读**产品代码、不写任何文件、不读 KB。
"""

import itertools
import math
import sys
from fractions import Fraction
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.segment_capacity import (  # noqa: E402
    CapacityAllocation,
    allocation_by_entity,
    effective_capacity,
    effective_capacity_daily,
    largest_remainder,
    largest_remainder_by_demand,
)


# ======================================================================
# 7.1 · 有效容量 = min(段容量, 当天分到的份额)
# ======================================================================


def test_71_effective_capacity_daily_takes_min():
    """7.1 正面：份额低于段容量 → 取份额。"""
    assert effective_capacity_daily(20, 7) == 7


def test_71_no_share_means_unlimited():
    """7.1 兼容：`day_share is None` → 不限，逐位等于段容量（老路径不变）。"""
    assert effective_capacity_daily(20, None) == 20
    # 与老 `effective_capacity` 在"无限额"下的语义一致
    assert effective_capacity_daily(20, None) == effective_capacity(20, None)


def test_71_segment_cap_wins_when_smaller():
    """7.1 反面：份额大于段容量 → 段容量赢（份额只会收窄，绝不放大）。"""
    assert effective_capacity_daily(3, 9) == 3


def test_71_segment_cap_none_stays_none():
    """容量不可用 → None（**不猜**，由调用方报缺）。"""
    assert effective_capacity_daily(None, 7) is None
    assert effective_capacity_daily(None, None) is None


def test_71_zero_share_is_rejected_not_silently_zero():
    """份额 <= 0 是上游回压算错了 → 抛错，绝不静默返回 0（会把工期算成无穷）。"""
    with pytest.raises(ValueError):
        effective_capacity_daily(10, 0)
    with pytest.raises(ValueError):
        effective_capacity_daily(10, -3)


def test_71_share_only_ever_narrows_across_a_day_ladder():
    """7.1 全链路语义：一条逐日份额阶梯只会把有效容量压小或保持。"""
    segment_cap = 12
    ladder = [12, 12, 7, 7, 3, 3, 3, 1]
    got = [effective_capacity_daily(segment_cap, s) for s in ladder]
    assert got == [12, 12, 7, 7, 3, 3, 3, 1]
    assert all(v <= segment_cap for v in got)
    # 7.2 的取法：窗口内份额的最小值（设计 §3.2.4）
    assert min(got) == 1


# ======================================================================
# 7.3 / 7.4 · 按需求量分摊 + 最大余数法（合计恰等于限额）
# ======================================================================


def test_73_design_acceptance_7_70_30():
    """设计 §3.3 验收：`largest_remainder_by_demand(7, [70, 30])` → [5, 2]。"""
    alloc, trace = largest_remainder_by_demand(7, [70, 30])
    assert alloc.allocated == [5, 2]
    assert sum(alloc.allocated) == 7
    assert alloc.weight_kind == "demand"
    assert alloc.breached is False
    assert trace["total"] == 7


def test_73_formula_is_limit_times_demand_over_total():
    """7.3 公式逐条核：`alloc_i ≈ limit × demand_i ÷ Σdemand`。"""
    limit = 10
    demands = [5.0, 20.0, 75.0]
    alloc, _ = largest_remainder_by_demand(limit, demands, ["a", "b", "c"])
    total = sum(demands)
    # e_i = [0.5, 2.0, 7.5] → p = [0, 2, 7]，R = 1 → 小数最大者 idx2 得 +1
    for got, demand in zip(alloc.allocated, demands):
        exact = limit * demand / total
        assert abs(got - exact) < 1.0                      # 最大余数法的误差恒 < 1
    assert alloc.allocated == [1, 2, 7]
    assert sum(alloc.allocated) == limit
    # 但 idx0 属"参与分摊却被取整成 0" → 必须被 7.5 强制到 1
    assert all(v >= 1 for v in alloc.allocated)


def test_73_not_weighted_by_construction_quantity():
    """7.3 铁律：**不按施工量**分摊 —— 权重只能来自需求量。

    反证：若误把"施工量"当权重，`[1000 m², 1 m³]` 这种不可比量纲会把限额几乎
    全给第一条。本测试钉住"用需求量 [1, 1]"必须**均分**，与施工量无关。
    """
    construction_qty = [1000.0, 1.0]        # m² 与 m³ —— 不可比，**不得**进公式
    demands = [1.0, 1.0]                    # 需求量（工日）—— 同量纲
    alloc, trace = largest_remainder_by_demand(6, demands, ["A", "B"])
    assert alloc.allocated == [3, 3]        # 均分 = 需求量的结果
    # 若按施工量会是 [6, 0]（且触发 7.5 强制最少一人）—— 明确不是这个结果
    assert alloc.allocated != [6, 0]
    # trace 里只记需求量，施工量根本没有入口
    assert trace["demands"] == demands
    assert all("m²" not in str(v) and "m³" not in str(v) for v in trace["demands"])
    assert construction_qty != demands      # 夹具自证：两者确实不同


def test_73_tie_break_is_by_entity_order_not_by_name():
    """平局按**实体序号升序**（不是按名字）—— 传 `sorted()` 过的 task_id。

    限额 2 / 需求 [1,1,1]：e 全 = 2/3 → R = 2 → 序号最小者先 +1 → [1,1,0]；
    再被 7.6 抬到最后一条 → [1,1,1]。次序完全由**传入次序**决定（可复现）。
    """
    alloc, trace = largest_remainder_by_demand(
        2, [1.0, 1.0, 1.0], ["4.1.1.1", "4.1.1.2", "4.1.1.3"])
    assert alloc.allocated == [1, 1, 1]
    assert alloc.segment_ids == ["4.1.1.1", "4.1.1.2", "4.1.1.3"]
    assert trace["min_one_lifted"] == [0, 1, 2]
    assert trace["remainder"] == -1                     # 7.6 突破：R = 2 − 3
    # 传乱序 id 时结果跟着传参走（不按名字排序）
    alloc2, _ = largest_remainder_by_demand(2, [1.0, 1.0, 1.0], ["c", "a", "b"])
    assert alloc2.allocated == [1, 1, 1]
    assert alloc2.segment_ids == ["c", "a", "b"]


@pytest.mark.parametrize("limit,demands", [
    (1, [1.0]),
    (1, [5.0, 5.0]),
    (2, [1.0, 1.0, 1.0]),
    (3, [600.0, 400.0]),
    (5, [1.0, 1.0, 1.0, 1.0, 996.0]),
    (5, [10.0, 20.0, 30.0, 0.5, 100.0]),
    (7, [70.0, 30.0]),
    (100, [1.0, 2.0, 3.0, 4.0]),
    (9, [0.001, 0.002, 0.003]),
    (13, [1e6, 1e6, 1e6, 1e6, 1e6, 1e6, 1e6]),
])
def test_74_sum_equals_limit_exactly(limit, demands):
    """7.4：`Σallocated` **恰等于**限额（除非触发 7.6 突破）。"""
    alloc, trace = largest_remainder_by_demand(limit, demands)
    if alloc.breached:
        assert sum(alloc.allocated) == len(demands) > limit
    else:
        assert sum(alloc.allocated) == limit
    assert alloc.total == limit
    assert trace["allocated"] == alloc.allocated


def test_74_no_off_by_one_on_float_weights():
    """7.4 浮点：0.1/0.2/0.3 这类权重不得造成 Σ=limit±1。"""
    for limit in range(1, 60):
        alloc, _ = largest_remainder_by_demand(limit, [0.1, 0.2, 0.3])
        if alloc.breached:                             # limit < 3 → 归 7.6
            assert sum(alloc.allocated) == 3 > limit
        else:
            assert sum(alloc.allocated) == limit, (limit, alloc.allocated)


def test_74_exhaustive_small_grid():
    """7.4 穷举：限额 1..12 × 权重组合穷举 → Σ 恒等式 + 各值单调不超上界。"""
    weight_sets = [
        [1.0], [1.0, 1.0], [1.0, 2.0], [1.0, 1.0, 1.0], [3.0, 3.0, 3.0],
        [1.0, 0.0, 0.0], [0.5, 1.5, 2.0], [7.0, 11.0, 13.0, 17.0],
        [0.0, 1.0, 0.0, 1.0], [1e-9, 1.0], [100.0, 1.0],
    ]
    for limit in range(1, 13):
        for demands in weight_sets:
            alloc, _ = largest_remainder_by_demand(limit, demands)
            if alloc.breached:
                continue
            assert sum(alloc.allocated) == limit, (limit, demands, alloc.allocated)
            assert all(v >= 0 for v in alloc.allocated)


def test_74_remainder_matches_definition():
    """7.4：`remainder == limit − Σfloored`（非突破路径）。"""
    alloc, _ = largest_remainder_by_demand(17, [3.0, 5.0, 7.0, 11.0])
    assert not alloc.breached
    assert alloc.remainder == 17 - sum(alloc.floored)


def test_74_exact_fractions_are_reproducible_strings():
    """7.12：精确份额以 `Fraction` 字符串留痕（不受浮点尾差影响）。"""
    _, trace = largest_remainder_by_demand(7, [70.0, 30.0])
    assert trace["exact"] == ["49/10", "21/10"]
    assert all(isinstance(s, str) for s in trace["exact"])
    # Fraction 可无损回读
    assert [Fraction(s) for s in trace["exact"]] == [Fraction(49, 10), Fraction(21, 10)]


def test_74_weight_kind_marks_the_caliber():
    """7.4：新函数 `weight_kind == "demand"`，老函数 `== "area"`（口径必须可区分）。"""
    new_alloc, _ = largest_remainder_by_demand(3, [600.0, 400.0])
    old_alloc = largest_remainder(3, [600.0, 400.0])
    assert new_alloc.weight_kind == "demand"
    assert old_alloc.weight_kind == "area"


# ======================================================================
# 7.5 · 强制最少一人 + 从「小数余数最小」的那条减 1
# ======================================================================


def test_75_zero_is_forced_to_one():
    """7.5：限额 5 / 需求 [1,1,1,1,996] —— 被取整成 0 的必须抬到 1，Σ 不变。"""
    alloc, trace = largest_remainder_by_demand(5, [1.0, 1.0, 1.0, 1.0, 996.0])
    assert all(v >= 1 for v in alloc.allocated), alloc.allocated
    assert sum(alloc.allocated) == 5
    assert alloc.breached is False
    # 归一化到基准线：需求差距 996:1 也**不影响**"每条都够 1 人"的下限
    assert alloc.allocated == [1, 1, 1, 1, 1]
    assert trace["min_one_lifted"] == [0, 1, 2, 3]
    assert trace["min_one_donors"] == [4, 4, 4, 4]


def test_75_donor_is_a_single_oversupplied_entry():
    """7.5 核心：需求 [1,1,1,1,996]、限额 5 —— 4 条被取整成 0，全靠**同一条**补齐。

    实测推导：
      e = [0.005, 0.005, 0.005, 0.005, 4.98]  p = [0,0,0,0,4]  R = 1
      小数降序 → idx4 得 +1 → [0,0,0,0,5]
      → idx0..idx3 各被抬到 1；唯一"值 > 1"的条目是 idx4，故 4 次都从它扣
      → [1,1,1,1,1]，Σ 恒 = 5（7.5 **绝不改 Σ**）
    注意：idx0..idx3 的小数余数（0.005）虽最小，但它们当前值为 0，**不可扣**；
    donor 必须"当前值 > 1"，这是 Σ 守恒的唯一来源。
    """
    alloc, trace = largest_remainder_by_demand(5, [1.0, 1.0, 1.0, 1.0, 996.0])
    assert trace["min_one_lifted"] == [0, 1, 2, 3]
    assert trace["min_one_donors"] == [4, 4, 4, 4]
    assert alloc.allocated == [1, 1, 1, 1, 1]
    assert all(v >= 1 for v in alloc.allocated)
    assert sum(alloc.allocated) == 5
    assert alloc.breached is False
    assert "小数余数最小" in " ".join(alloc.steps)
    assert "小数余数最小" in " ".join(trace["steps"])


def test_75_donor_order_differs_from_the_old_richness_order():
    """7.5 与老函数的 donor 口径**故意不同** —— 同一输入做出不同分配。

    需求 [1,23,19,19,3]、限额 6：
      新（可行性 + 小数余数）→ allocated [1,2,1,1,1]，donors [2,3]
      老（富余量从大到小）    → allocated [1,1,1,2,1]，donors [1,2]
    两者 Σ 都 = 6、都满足 ≥1，但**具体份额与 donor 留痕都不同**。
    """
    demands = [1.0, 23.0, 19.0, 19.0, 3.0]
    alloc, trace = largest_remainder_by_demand(6, demands, ["a", "b", "c", "d", "e"])
    assert alloc.allocated == [1, 2, 1, 1, 1]
    assert sum(alloc.allocated) == 6
    assert all(v >= 1 for v in alloc.allocated)
    assert trace["min_one_lifted"] == [0, 4]
    assert trace["min_one_donors"] == [2, 3]
    # 明确断言"不是老口径的结果"
    assert alloc.allocated != [1, 1, 1, 2, 1]


def test_75_donor_order_another_divergent_case():
    """7.5 第二个差异样例（donor 次序正好相反）。

    需求 [1,23,19,19,3]、限额 5：
      新 donor = [2, 1]，allocated [1,1,1,1,1]
      老 donor = [1, 2]，allocated [1,1,1,1,1]（同结果、**次序不同**）
    次序不同 → 留痕不同 → 若将来次序被改，本条会红（回归锁）。
    """
    alloc, trace = largest_remainder_by_demand(5, [1.0, 23.0, 19.0, 19.0, 3.0])
    assert trace["min_one_lifted"] == [0, 4]
    assert trace["min_one_donors"] == [2, 1]
    assert alloc.allocated == [1, 1, 1, 1, 1]
    assert sum(alloc.allocated) == 5


def test_75_slack_demand_does_not_break_sum_conservation():
    """回归锁：Σ需求量 < 限额 时 floor 把多条压成 0。

    限额 145 / 需求 [1,1,1,100]：Σ需求 = 103 < 145
      e ≈ [1.408, 1.408, 1.408, 140.78]，p = [1,1,1,140]，R = 2 → 两条 +1
      → 剩下一条为 0 → 7.5 抬到 1，必须从"值 > 1"的条目扣，**Σ 必须保持 145**。
    （早期实现按"余数最小"直接扣，会扣到只剩 1 的条目上、把 Σ 弄成 146。）
    """
    alloc, trace = largest_remainder_by_demand(145, [1.0, 1.0, 1.0, 100.0])
    assert sum(alloc.allocated) == 145
    assert all(v >= 1 for v in alloc.allocated)
    assert not alloc.breached
    assert len(trace["min_one_donors"]) == len(trace["min_one_lifted"])
    # donor 必须全是"扣之前值 > 1"的条目
    assert all(alloc.allocated[j] >= 1 for j in trace["min_one_donors"])


def test_75_slack_demand_fuzz_sum_always_preserved():
    """回归锁（穷举）：`Σ需求量 < 限额` 且 `限额 >= 条数` 时，Σ 恒 = 限额且每条 ≥1。"""
    grids = [
        [1.0, 1.0, 1.0, 100.0],
        [1.0, 2.0, 3.0, 1000.0],
        [0.5, 0.5, 50.0],
        [7.0, 3.0, 3.0, 1.0],
        [1e-6, 1.0, 1.0],
    ]
    for demands in grids:
        for limit in range(len(demands), 160):
            alloc, _ = largest_remainder_by_demand(limit, demands)
            assert not alloc.breached, (limit, demands)
            assert sum(alloc.allocated) == limit, (limit, demands, alloc.allocated)
            assert all(v >= 1 for v in alloc.allocated), (limit, demands, alloc.allocated)


def test_75_slack_large_limit_gets_wide_coverage():
    """限额 5 / 需求 [0,0,0,0,100]（派工单用例）：0 需求不参与，唯一参与者拿满 5。"""
    alloc, trace = largest_remainder_by_demand(5, [0.0, 0.0, 0.0, 0.0, 100.0])
    assert alloc.allocated == [0, 0, 0, 0, 5]
    assert sum(alloc.allocated) == 5
    assert trace["min_one_lifted"] == []
    assert alloc.breached is False


def test_75_zero_demand_is_not_forced_to_one():
    """7.5 边界：需求量为 0 的条目**不参与分摊**，份额恒 0、不触发强制最少一人。"""
    alloc, trace = largest_remainder_by_demand(4, [0.0, 0.0, 0.0, 0.0, 100.0])
    assert alloc.allocated == [0, 0, 0, 0, 4]
    assert trace["min_one_lifted"] == []
    assert any("不参与分摊" in w for w in alloc.warnings)
    assert sum(alloc.allocated) == 4


def test_75_all_zero_demands_refuses_to_guess():
    """需求全 0 → **不猜**：不发限额，进 warnings，allocated 全 0。"""
    alloc, trace = largest_remainder_by_demand(5, [0.0, 0.0, 0.0])
    assert alloc.allocated == [0, 0, 0]
    assert alloc.breached is False
    assert any("全为 0" in w for w in alloc.warnings)
    assert trace["allocated"] == [0, 0, 0]


def test_75_sum_is_preserved_after_min_one_fixups():
    """7.5 不变式：强制最少一人**绝不改 Σ**（除非 7.6 突破）。"""
    for limit in range(1, 25):
        for demands in ([1.0, 1.0, 1.0, 1.0, 996.0], [0.5] * 9, [1e-6, 1.0, 1.0],
                        [10.0, 19.0, 1.0, 19.0, 1.0], [1.0, 23.0, 19.0, 19.0, 3.0]):
            if limit < len(demands):
                continue                               # 归 7.6 管
            alloc, _ = largest_remainder_by_demand(limit, demands)
            assert sum(alloc.allocated) == limit, (limit, demands, alloc.allocated)


def test_75_every_positive_demand_gets_at_least_one_when_limit_allows():
    """7.5 结论：`limit >= 条数` 时，**所有需求 > 0 的条目都拿到 >= 1**。"""
    cases = [
        (5, [1.0, 1.0, 1.0, 1.0, 996.0]),
        (6, [1000.0, 1.0, 1.0, 1.0, 1.0, 1.0]),
        (9, [1e-9, 1e-9, 1e-9, 1.0, 1.0, 1.0, 1.0, 1.0, 1e6]),
        (20, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
    ]
    for limit, demands in cases:
        alloc, _ = largest_remainder_by_demand(limit, demands)
        assert not alloc.breached
        assert all(v >= 1 for v in alloc.allocated), (limit, demands, alloc.allocated)


# ======================================================================
# 7.6 · 限额 < 条数 → 突破限额 + 明确标出
# ======================================================================


def test_76_design_acceptance_limit_2_needs_4():
    """设计 §3.6 验收：`largest_remainder_by_demand(2, [5,5,5,5])` → [1,1,1,1] + breached。"""
    alloc, trace = largest_remainder_by_demand(2, [5.0, 5.0, 5.0, 5.0])
    assert alloc.allocated == [1, 1, 1, 1]
    assert alloc.breached is True
    assert "突破限额" in " ".join(alloc.warnings)


def test_76_breach_is_structured_and_human_readable():
    """7.6：突破必须**结构化留痕** + 人可读理由，**不静默**、不抛异常。"""
    alloc, trace = largest_remainder_by_demand(2, [1.0, 1.0, 1.0])
    assert alloc.breached is True
    assert trace["breached"] is True
    assert trace["total"] == 2
    assert sum(alloc.allocated) == 3 > alloc.total      # 合计 > 限额（如实证）
    assert alloc.allocated == [1, 1, 1]
    msg = " ".join(alloc.warnings)
    assert "突破限额" in msg
    assert "2" in msg and "3" in msg                    # 数字写清楚
    assert "突破限额" in " ".join(alloc.steps)
    # 键只增不改：老消费方要读的键仍在
    payload = alloc.as_dict()
    for key in ("segment_ids", "segment_areas", "exact", "floored", "remainder",
                "allocated", "total", "steps", "warnings"):
        assert key in payload


def test_76_no_silent_breach_and_no_exception():
    """7.6：限额远小于条数也不得抛异常、不得静默（warning 必非空）。"""
    for n in range(2, 12):
        alloc, trace = largest_remainder_by_demand(1, [1.0] * n)
        assert alloc.breached is True
        assert alloc.allocated == [1] * n
        assert alloc.warnings, "突破限额必须留 warning（不许静默）"
        assert sum(alloc.allocated) == n


def test_76_differs_from_old_function_on_the_same_input():
    """7.6 与老函数口径**正好相反**（这是不能改老函数的确切理由）。

    老：限额 < 段数 → 保留 Σ=N + warning（`test_edge_2_segment_count_gt_n_conflict...` 钉着）。
    新：限额 < 条数 → 突破限额（每人 >= 1）+ `breached=True`。
    """
    old = largest_remainder(2, [500.0, 500.0, 500.0, 500.0])
    new, _ = largest_remainder_by_demand(2, [500.0, 500.0, 500.0, 500.0])
    assert sum(old.allocated) == 2                       # 老：Σ = N，只发 warning
    assert old.breached is False
    assert any("N=2 < 段数=4" in w for w in old.warnings)
    assert sum(new.allocated) == 4                       # 新：突破，每人 >= 1
    assert new.allocated == [1, 1, 1, 1]
    assert new.breached is True
    assert "突破限额" in " ".join(new.warnings)


def test_76_breach_with_zero_demand_entries():
    """7.6 边界：「条数」以**需求 > 0** 的为准；0 需求条目既不参与也不被强制到 1。"""
    alloc, _ = largest_remainder_by_demand(2, [5.0, 5.0, 5.0, 0.0, 0.0])
    assert alloc.breached is True
    assert alloc.allocated == [1, 1, 1, 0, 0]
    assert sum(alloc.allocated) == 3 > 2
    # 口径钉死：限额 2 < 参与分摊条数 3 ⇒ 突破；0 需求条目仍为 0
    assert alloc.allocated[3] == 0 and alloc.allocated[4] == 0
    assert "突破限额" in " ".join(alloc.warnings)


def test_76_zero_demand_entries_do_not_trigger_a_false_breach():
    """7.6 反例：`limit >= 参与分摊条数` 时**不得**因为 0 需求条目多而误判突破。"""
    alloc, _ = largest_remainder_by_demand(4, [0.0, 0.0, 0.0, 0.0, 100.0])
    assert alloc.breached is False
    assert alloc.allocated == [0, 0, 0, 0, 4]
    assert sum(alloc.allocated) == 4


# ======================================================================
# 7.12 前置 · 确定性（逐位一致）与契约（键只增不改）
# ======================================================================


def test_determinism_bitwise_over_200_repeats():
    """硬约束 1：同一输入重复 200 次 → 逐位同样的输出（无随机、无 set/dict 迭代序）。"""
    first = None
    for _ in range(200):
        alloc, trace = largest_remainder_by_demand(
            7, [70.0, 30.0], ["4.1.1.1", "4.1.2.1"])
        snapshot = (list(alloc.allocated), list(alloc.exact), alloc.remainder,
                    list(alloc.steps), list(alloc.warnings), trace["exact"])
        if first is None:
            first = snapshot
        assert snapshot == first
    assert first[0] == [5, 2]


def test_determinism_exhaustive_over_grid():
    """确定性在更宽的输入网格上同样成立（含 7.5 / 7.6 分支）。"""
    for limit in range(1, 10):
        for demands in ([1.0, 2.0, 3.0], [1.0, 1.0, 1.0, 1.0], [0.0, 1.0],
                        [10.0, 19.0, 1.0, 19.0, 1.0]):
            a, ta = largest_remainder_by_demand(limit, demands)
            b, tb = largest_remainder_by_demand(limit, demands)
            assert a.allocated == b.allocated
            assert a.steps == b.steps
            assert a.warnings == b.warnings
            assert ta == tb


def test_determinism_is_independent_of_input_container_types():
    """list / tuple 输入逐位一致（不许依赖容器身份）。"""
    a, _ = largest_remainder_by_demand(7, [70.0, 30.0], ["x", "y"])
    b, _ = largest_remainder_by_demand(7, (70.0, 30.0), ("x", "y"))
    assert a.allocated == b.allocated
    assert a.steps == b.steps


def test_no_random_or_time_source_in_new_symbols():
    """硬约束 1：新函数本体不得出现 random / time / uuid / datetime。"""
    source = (BACKEND / "pipeline" / "segment_capacity.py").read_text(encoding="utf-8")
    for token in ("import random", "random.", "import time", "time.time",
                  "uuid", "datetime.now"):
        assert token not in source, "segment_capacity.py 出现了非确定性来源：%s" % token


def test_contract_as_dict_keys_add_only():
    """契约：`as_dict()` 键**只增不改** —— 老 9 键仍在 + 新 3 键。"""
    old = largest_remainder(3, [600.0, 400.0]).as_dict()
    new = largest_remainder_by_demand(3, [600.0, 400.0])[0].as_dict()
    legacy_keys = {"segment_ids", "segment_areas", "exact", "floored", "remainder",
                   "allocated", "total", "steps", "warnings"}
    assert legacy_keys <= set(old)
    assert legacy_keys <= set(new)
    assert {"breached", "weight_kind", "trace"} <= set(new)
    assert old["breached"] is False and old["weight_kind"] == "area"
    assert new["weight_kind"] == "demand"


def test_contract_allocate_segment_demand_payload_unchanged():
    """契约：老 `largest_remainder` 的 as_dict 在**除新增键外**逐键未变。"""
    got = largest_remainder(7, [500.0, 333.0], ["Ⅰ", "Ⅱ"]).as_dict()
    expected = {
        "segment_ids": ["Ⅰ", "Ⅱ"],
        "segment_areas": [500.0, 333.0],
        "exact": [float(Fraction(7) * Fraction(500) / Fraction(833)),
                  float(Fraction(7) * Fraction(333) / Fraction(833))],
        "floored": [4, 2],
        "remainder": 1,
        "allocated": [4, 3],
        "total": 7,
    }
    for key, want in expected.items():
        assert got[key] == want, key


def test_contract_allocation_by_entity_helper():
    """域 7.2 便利函数：`{entity_id: 份额}`，与 zip 等价。"""
    alloc, _ = largest_remainder_by_demand(
        7, [70.0, 30.0], ["4.1.1.1", "4.1.2.1"])
    assert allocation_by_entity(alloc) == {"4.1.1.1": 5, "4.1.2.1": 2}
    assert allocation_by_entity(alloc) == dict(
        zip(alloc.segment_ids, alloc.allocated))


def test_contract_returns_capacity_allocation_and_plain_dict_trace():
    """契约：返回 `(CapacityAllocation, dict)` —— 7.2 用 `.allocated`，留痕用 trace。"""
    result = largest_remainder_by_demand(7, [70.0, 30.0])
    assert isinstance(result, tuple) and len(result) == 2
    alloc, trace = result
    assert isinstance(alloc, CapacityAllocation)
    assert isinstance(trace, dict)
    assert trace["allocated"] == alloc.allocated
    assert trace["steps"] == alloc.steps
    assert trace["warnings"] == alloc.warnings
    assert alloc.trace == trace


# ======================================================================
# 入参防御（不猜）
# ======================================================================


def test_rejects_bad_limit():
    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            largest_remainder_by_demand(bad, [1.0])


def test_rejects_empty_demands():
    with pytest.raises(ValueError):
        largest_remainder_by_demand(3, [])


def test_rejects_length_mismatch():
    with pytest.raises(ValueError):
        largest_remainder_by_demand(3, [1.0, 2.0], ["only-one"])


def test_rejects_negative_or_nonfinite_demands():
    for bad in (-1.0, float("nan"), float("inf"), None):
        with pytest.raises(ValueError):
            largest_remainder_by_demand(3, [1.0, bad])


def test_no_u33a1_square_metre_symbol_in_this_module():
    """本仓铁律：源码里不得出现 U+33A1 的方块平米符号。"""
    source = (BACKEND / "pipeline" / "segment_capacity.py").read_text(encoding="utf-8")
    assert "\u33a1" not in source

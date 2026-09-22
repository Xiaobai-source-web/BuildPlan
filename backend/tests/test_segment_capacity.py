"""容量字典生成器测试 — `pipeline/segment_capacity.py`（纯逻辑，无库无 LLM）。

运行：cd backend && python -m pytest tests/test_segment_capacity.py -q

覆盖《资源与工期计算重构方案 v1》：
  · §4.3 回分验算（**验收 #2：3 个用例**）
  · §6 验收 #3：`segment_id` / `segment_area` / `capacity_fixed` / `capacity_mobile` 齐全
  · §3.2 固定型 / 移动型判据（名单来自注入数据，**不写死工种**）
  · §3【2】场地级 site 不进段容量
  · §3【5】/ §4.5 / C10：汇总 → min(N, 用户同类限额) → 唯一工期公式
  · §4.4 边界清单 9 条
  · 裁定 4/6/7/10、C9、B4 分布分解
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.segment_capacity import (FIXED, MOBILE, SITE, ActivityDemand,  # noqa: E402
                                        MWIRow, RoleAssignment, UserCap,
                                        allocate_segment_demand,
                                        build_mwi_index, ceil_div, duration_days,
                                        effective_capacity, floor_div,
                                        largest_remainder, layer_distribution,
                                        normalize_trade, parallel_batches,
                                        primary_companion, resolve_user_cap,
                                        segment_capacity, segment_distribution,
                                        task_capacity_fields)


# ==================== 注入数据夹具（对应 `Resource_Workface_Index` 投影） ====================

MWI_ROWS = [
    # 固定型人工：驻段干活，逐段取整相加（§3.2）
    MWIRow("钢筋工", 12.0, "m²/人", "labor", FIXED, "area"),
    MWIRow("模板工", 15.0, "m²/人", "labor", FIXED, "area"),
    # 移动型机械：沿面移动/外部覆盖，汇总取整一次（§3.2）
    MWIRow("混凝土输送泵车", 500.0, "m²/台", "machine", MOBILE, "transport"),
    MWIRow("混凝土罐车", 400.0, "m²/台", "machine", MOBILE, "transport"),
    MWIRow("混凝土振捣器", 250.0, "m²/台", "machine", FIXED, "position"),
    # 场地级：不进段容量（§3【2】/ §4.4 #8）
    MWIRow("塔式起重机", 3000.0, "m²/台", "machine", SITE, "site"),
]
MWI = build_mwi_index(MWI_ROWS)


def _ids(n):
    from pipeline.segment_plan import _segment_id
    return [_segment_id(i) for i in range(n)]


# ==================== 取整（裁定 6：一律向上；§4.3 ② 是唯一例外） ====================

@pytest.mark.parametrize("num,den,up,down", [
    (1000, 500, 2, 2),
    (1001, 500, 3, 2),
    (833, 500, 2, 1),
    (333, 400, 1, 0),
    (0, 500, 0, 0),
    (499.999, 500, 1, 0),
])
def test_ceil_and_floor_div(num, den, up, down):
    assert ceil_div(num, den) == up
    assert floor_div(num, den) == down


def test_ceil_div_rejects_bad_arguments():
    with pytest.raises(ValueError):
        ceil_div(10, 0)
    with pytest.raises(ValueError):
        ceil_div(-1, 10)
    with pytest.raises(ValueError):
        floor_div(10, -5)


# ==================== §4.3 回分验算（3 个用例，验收 #2） ====================

def test_verification_4_3_case_one():
    """N=3、600/400 → 2 / 1。"""
    got = largest_remainder(3, [600.0, 400.0], ["Ⅰ", "Ⅱ"])
    assert got.allocated == [2, 1]
    assert got.total == 3
    assert sum(got.allocated) == 3
    assert got.floored == [1, 1] and got.remainder == 1


def test_verification_4_3_case_two():
    """N=5、500/500/520 → 2 / 1 / 2。"""
    got = largest_remainder(5, [500.0, 500.0, 520.0], ["Ⅰ", "Ⅱ", "Ⅲ"])
    assert got.allocated == [2, 1, 2]
    assert sum(got.allocated) == 5
    assert got.floored == [1, 1,1] and got.remainder == 2


def test_verification_4_3_case_three():
    """N=7、500/333 → 4 / 3。"""
    got = largest_remainder(7, [500.0, 333.0], ["Ⅰ", "Ⅱ"])
    assert got.allocated == [4, 3]
    assert sum(got.allocated) == 7


def test_verification_4_3_whole_table_at_once():
    assert largest_remainder(3, [600.0, 400.0]).allocated == [2, 1]
    assert largest_remainder(5, [500.0, 500.0, 520.0]).allocated == [2, 1, 2]
    assert largest_remainder(7, [500.0, 333.0]).allocated == [4, 3]


def test_remainder_tie_breaks_by_segment_order():
    """§4.3 ④：小数相同 → 按段号 Ⅰ→Ⅱ→Ⅲ（保证可复现）。"""
    got = largest_remainder(3, [500.0, 500.0, 500.0], ["Ⅰ", "Ⅱ", "Ⅲ"])
    assert got.allocated == [1, 1, 1]
    got2 = largest_remainder(4, [500.0, 500.0, 500.0], ["Ⅰ", "Ⅱ", "Ⅲ"])
    assert got2.allocated == [2, 1, 1], "余数相同必须给靠前的段号"


def test_remainder_is_reproducible_over_many_random_splits():
    import random
    rng = random.Random(20260921)
    for _ in range(300):
        n_seg = rng.randint(2, 9)
        n = rng.randint(n_seg, 40)
        areas = [float(rng.randint(100, 900)) for _ in range(n_seg)]
        a = largest_remainder(n, areas, _ids(n_seg))
        b = largest_remainder(n, areas, _ids(n_seg))
        assert a.allocated == b.allocated, "同输入必须逐位一致"
        assert sum(a.allocated) == n
        assert all(v >= 1 for v in a.allocated)


# ==================== §4.4 边界清单 ====================

def test_edge_5_single_segment_gets_all_n():
    """§4.4 #5：只有 1 段同时施工 → n = N，不分配。"""
    got = largest_remainder(5, [900.0], ["Ⅰ"])
    assert got.allocated == [5]
    assert "不分配" in " ".join(got.steps)


def test_edge_6_n_equals_one_with_many_segments():
    """§4.4 #6：N=1 而多段 → 各段时间上依次共用这 1 台。"""
    got = largest_remainder(1, [500.0, 500.0, 300.0], ["Ⅰ", "Ⅱ", "Ⅲ"])
    assert got.allocated == [1, 1, 1]
    assert "依次共用" in " ".join(got.steps)


def test_edge_4_zero_segment_is_lifted_to_one_and_clawed_back():
    """§4.4 #4 / §4.3 ⑤：某段算下来 0 → 抬到 1，从余数最大段扣回，总和仍 = N。"""
    got = largest_remainder(3, [10.0, 10.0, 980.0], ["Ⅰ", "Ⅱ", "Ⅲ"])
    assert sum(got.allocated) == 3
    assert all(v >= 1 for v in got.allocated), got.allocated
    assert got.allocated[-1] == 1, "大户段被扣回 1"


def test_edge_2_segment_count_gt_n_conflict_is_surfaced_not_guessed():
    """§4.4 #2：段数 > N 是**唯一会矛盾的地方** —— 本模块不猜，显式抛矛盾。

    §4.3 ⑤ 要求"各段 ≥ 1"，§4.3 ③ 要求"Σ = N"；N < 段数时二者不可兼得。
    模块保留 Σ = N 并给出告警，由调用方走"并行段数上限 = N、多余段错开批次"。
    """
    got = largest_remainder(2, [500.0, 500.0, 500.0, 500.0])
    assert sum(got.allocated) == 2
    assert got.warnings, "必须显式告警，不得静默"


def test_edge_4_one_donor_pays_several_zeros():
    """§4.3 ⑤ 的守恒式扣回：一个富余段要同时抬好几个 0 段，总和仍 = N。"""
    got = largest_remainder(4, [10.0, 10.0, 10.0, 970.0])
    assert sum(got.allocated) == 4
    assert all(v >= 1 for v in got.allocated), got.allocated
    assert got.allocated[-1] == 1, "富余段被扣回 3 台"
    assert not got.warnings, got.warnings


def test_edge_2_parallel_batches_cap_at_n():
    """§4.4 #2/#3：段数 > N → 并行段数上限 = N，多余段错开批次。"""
    batches, count, notes = parallel_batches(["Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ"], 2)
    assert count == 2
    assert batches == [0, 1, 0, 1]
    assert notes
    batches2, count2, _ = parallel_batches(["Ⅰ", "Ⅱ"], 4)
    assert batches2 == [0, 0] and count2 == 1


def test_edge_3_user_cap_below_n_is_used_as_n():
    """§4.4 #3：用户限额 < N → 取用户限额为 N，再走第 2 条。"""
    plan = segment_capacity([500.0] * 4, _ids(4), MWI["混凝土输送泵车"],
                            user_cap=2)
    assert plan.rollup == 4, "移动型汇总 = ceil(2000 ÷ 500) = 4"
    assert plan.effective == 2
    batches, count, _ = parallel_batches(_ids(4), plan.effective)
    assert count == 2 and batches == [0, 1, 0, 1]


def test_edge_7_fixed_never_allocates():
    """§4.4 #7：固定型不走分配、逐段相加，天然一致。"""
    plan = segment_capacity([500.0, 500.0, 500.0, 500.0], _ids(4), MWI["钢筋工"])
    assert plan.mobility == FIXED
    assert plan.segment_demand == [42, 42, 42, 42]      # ceil(500 ÷ 12)
    assert plan.rollup == 168
    assert plan.rollup_kind == "sum"
    assert plan.effective == 168


def test_edge_8_site_equipment_excluded_from_segment_capacity():
    """§4.4 #8 / §3【2】：塔吊/施工电梯走 _site_equipment，不进段容量。"""
    plan = segment_capacity([500.0, 500.0], _ids(2), MWI["塔式起重机"])
    assert plan.is_site is True
    assert plan.rollup == 0
    assert plan.rollup_kind == "site_excluded"
    assert plan.segment_demand == [0, 0]
    assert plan.segments[0].capacity_fixed == 0
    assert plan.segments[0].capacity_mobile == 0
    assert any("_site_equipment" in w for w in plan.warnings)


def test_edge_9_segment_level_numbers_always_present():
    """§4.4 #9 / §3.1：段级数字永远保留、永远写进每道工序。"""
    plan = segment_capacity([500.0, 333.0], _ids(2), MWI["模板工"])
    assert plan.segment_demand == [34, 23]              # ceil(500÷15), ceil(333÷15)
    for seg in plan.segments:
        assert seg.capacity_fixed >= 1
        assert seg.capacity_mobile >= 1
    # 即使汇总被用户限额削到 1，段级数字仍在
    plan2 = segment_capacity([500.0, 333.0], _ids(2), MWI["模板工"], user_cap=1)
    assert plan2.segment_demand == [34, 23]
    assert plan2.effective == 1


# ==================== §3.1 / §4.2 三类口径 ====================

def test_segment_capacity_fixed_vs_mobile_rollup():
    areas = [500.0, 400.0, 520.0]
    ids = _ids(3)
    fixed = segment_capacity(areas, ids, MWI["模板工"])
    assert fixed.segment_demand == [34, 27, 35]
    assert fixed.rollup == 96                            # 逐段相加
    mobile = segment_capacity(areas, ids, MWI["混凝土输送泵车"])
    assert mobile.segment_demand == [1, 1, 2]            # ceil(500÷500), ceil(400÷500), ceil(520÷500)
    assert mobile.rollup == 3                            # ceil(1420 ÷ 500)
    assert mobile.rollup_kind == "area_ceil"


def test_rollup_can_be_recomputed_from_real_overlap():
    """§3【5】：同时段由排程后**真实重叠**决定 → rollup_capacity 覆盖全段汇总。"""
    plan = segment_capacity([500.0, 400.0, 520.0], _ids(3), MWI["混凝土输送泵车"],
                            rollup_capacity=2)
    assert plan.rollup == 2
    assert plan.rollup_kind == "caller_overlap"
    assert plan.segment_demand == [1, 1, 2], "段级需求不受汇总口径影响"


def test_mobility_comes_from_injected_data_not_hardcoded_lists():
    """§3.2：mobility 来自 `Resource_Workface_Index.resource_mobility`（可逐行修改）。

    同一个资源名，注入表里改成 mobile 后汇总口径必须**跟着变** —— 证明没有写死名单。
    """
    as_fixed = build_mwi_index([MWIRow("挖掘机", 100.0, "m²/台", "machine", FIXED)])
    as_mobile = build_mwi_index([MWIRow("挖掘机", 100.0, "m²/台", "machine", MOBILE)])
    areas, ids = [300.0, 300.0], _ids(2)
    assert segment_capacity(areas, ids, as_fixed["挖掘机"]).rollup == 6      # 3+3
    assert segment_capacity(areas, ids, as_mobile["挖掘机"]).rollup == 6     # ceil(600÷100)
    # 用一个能区分两者口径的例子：300/300、MWI=250
    f = build_mwi_index([MWIRow("X", 250.0, "m²/台", "machine", FIXED)])["X"]
    m = build_mwi_index([MWIRow("X", 250.0, "m²/台", "machine", MOBILE)])["X"]
    assert segment_capacity([300.0, 300.0], ids, f).rollup == 4      # 2 + 2 逐段相加
    assert segment_capacity([300.0, 300.0], ids, m).rollup == 3      # ceil(600 ÷ 250)
    assert segment_capacity([300.0, 300.0], ids, f).segment_demand == [2, 2]


def test_unknown_mobility_falls_back_to_fixed_with_warning():
    row = MWIRow("怪东西", 100.0, "m²/台", "machine", "飞行")
    plan = segment_capacity([300.0], _ids(1), row)
    assert plan.mobility == FIXED
    assert any("不在" in w for w in plan.warnings)


def test_mwi_must_be_positive():
    with pytest.raises(ValueError):
        segment_capacity([300.0], _ids(1), MWIRow("零", 0.0))


# ==================== §4.5 / C9 用户限额口径 ====================

def test_only_user_sourced_caps_are_adopted():
    """§4.5：**只有用户明确给出的**才进 min()；model/AI 一律丢弃并留痕。"""
    caps = [
        UserCap("模板工", 5, "user"),
        UserCap("模板工", 999, "model"),        # AI 补的 → 丢弃
        {"resource_name": "模板工", "value": 888, "_source": "ai"},
    ]
    adopted, discarded = resolve_user_cap(caps, "模板工")
    assert adopted == 5
    assert len(discarded) == 2
    assert all("未采纳" in d for d in discarded)


def test_user_cap_takes_min_of_same_kind():
    """§4.5 / C9：与字典容量**同类取最小**（同一资源的多个用户限额取最小）。"""
    caps = [UserCap("模板工", 7, "user"), UserCap("模板工", 4, "user")]
    adopted, _ = resolve_user_cap(caps, "模板工")
    assert adopted == 4


def test_user_cap_trade_normalization():
    """§4.5：工种名走 `_normalize_trade`（木工→模板工、砼工→混凝土工、杂工→普工）。"""
    assert normalize_trade("木工") == "模板工"
    assert normalize_trade("砼工") == "混凝土工"
    assert normalize_trade("杂工") == "普工"
    assert normalize_trade("没听过的工种") == "没听过的工种"
    adopted, _ = resolve_user_cap([UserCap("木工", 6, "user")], "模板工")
    assert adopted == 6


def test_user_cap_alias_table_injection_point():
    """别名表可注入（父代理接 `scheduler._TRADE_ALIAS` 的入口）。"""
    adopted, _ = resolve_user_cap([UserCap("铁匠", 3, "user")], "钢筋工",
                                  aliases={"铁匠": "钢筋工"})
    assert adopted == 3


def test_effective_capacity_and_unique_duration_formula():
    """C10：有效容量 = min(N, 用户同类限额)；工期 = ceil(Demand ÷ 有效容量)。"""
    assert effective_capacity(10, None) == 10
    assert effective_capacity(10, 4) == 4
    assert effective_capacity(4, 10) == 4
    assert duration_days(100, 4) == 25
    assert duration_days(101, 4) == 26
    assert duration_days(1, 4) == 1
    assert duration_days(100, None) is None
    assert duration_days(100, 0) is None


# ==================== §4.2 / 裁定 10：主控 + 伴生 ====================

ROLE_MAP = [
    RoleAssignment("05.02.01", "混凝土输送泵车", "primary", 1.0, "machine_combination_json"),
    RoleAssignment("05.02.01", "混凝土罐车", "companion", 2.0, "Equipment_Crew_Mapping"),
    RoleAssignment("05.02.01", "混凝土振捣器", "companion", 1.0, "Equipment_Crew_Mapping"),
]


def test_primary_is_max_demand_over_capacity():
    demands = [
        ActivityDemand("05.02.01", "混凝土输送泵车", 60.0),
        ActivityDemand("05.02.01", "混凝土罐车", 200.0),
        ActivityDemand("05.02.01", "混凝土振捣器", 30.0),
    ]
    rollups = {"混凝土输送泵车": 3, "混凝土罐车": 5, "混凝土振捣器": 2}
    got = primary_companion("05.02.01", demands, ROLE_MAP, MWI,
                            segment_rollups=rollups)
    # Demand÷容量：60/3=20、200/5=40、30/2=15 → Role Map 标的主控是泵车；
    # 本实现以 Role Map 的 primary 标注为准（§4.2 初稿由 machine_combination_json 生成）
    assert got["primary"]["resource_name"] == "混凝土输送泵车"
    # 若 Role Map 没有 primary 标注，则严格按 Demand÷容量 最大者（罐车 40）
    no_primary = [RoleAssignment("05.02.01", "混凝土罐车", "companion", 2.0)]
    got2 = primary_companion("05.02.01", demands, no_primary, MWI,
                             segment_rollups=rollups)
    assert got2["primary"]["resource_name"] == "混凝土罐车"
    assert got2["primary"]["load"] == pytest.approx(40.0)


def test_companions_derived_from_primary_qty_by_ratio():
    demands = [
        ActivityDemand("05.02.01", "混凝土输送泵车", 60.0),
        ActivityDemand("05.02.01", "混凝土罐车", 200.0),
        ActivityDemand("05.02.01", "混凝土振捣器", 30.0),
    ]
    rollups = {"混凝土输送泵车": 2, "混凝土罐车": 5, "混凝土振捣器": 2}
    got = primary_companion("05.02.01", demands, ROLE_MAP, MWI,
                            segment_rollups=rollups)
    by = {c["resource_name"]: c for c in got["companions"]}
    assert by["混凝土罐车"]["quantity"] == 4      # ceil(2 × 2.0)
    assert by["混凝土振捣器"]["quantity"] == 2    # ceil(2 × 1.0)
    assert by["混凝土罐车"]["derived_from"] == "混凝土输送泵车"
    assert by["混凝土罐车"]["primary_qty"] == 2


def test_companion_without_role_map_is_not_guessed():
    """Role Map 没给配比 → **不猜**，出告警。"""
    demands = [
        ActivityDemand("05.02.01", "混凝土输送泵车", 60.0),
        ActivityDemand("05.02.01", "混凝土罐车", 200.0),
    ]
    role_map = [RoleAssignment("05.02.01", "混凝土输送泵车", "primary", 1.0)]
    got = primary_companion("05.02.01", demands, role_map, MWI,
                            segment_rollups={"混凝土输送泵车": 2, "混凝土罐车": 5})
    assert got["companions"] == []
    assert any("不猜配比" in w for w in got["warnings"])


def test_role_map_missing_activity_warns():
    got = primary_companion("99.99.99", [
        ActivityDemand("99.99.99", "混凝土输送泵车", 60.0),
    ], ROLE_MAP, MWI, segment_rollups={"混凝土输送泵车": 2})
    assert any("Role Map 无 activity_id" in w for w in got["warnings"])


def test_primary_missing_capacity_warns_not_guess():
    got = primary_companion("05.02.01", [
        ActivityDemand("05.02.01", "混凝土输送泵车", 60.0),
    ], ROLE_MAP, MWI, segment_rollups={})
    assert got["primary"] is None
    assert any("缺容量" in w for w in got["warnings"])


# ==================== §6 验收 #3：四个容量字段齐全 ====================

def test_acceptance_3_four_fields_on_every_task():
    fields = task_capacity_fields("Ⅱ", 416.5, 35, 1)
    for key in ("segment_id", "segment_area", "capacity_fixed", "capacity_mobile"):
        assert key in fields, key
    assert fields["segment_id"] == "Ⅱ"
    assert fields["segment_area"] == 416.5
    assert fields["capacity_fixed"] == 35 and fields["capacity_mobile"] == 1


def test_acceptance_3_fields_present_via_allocate_segment_demand():
    out = allocate_segment_demand(
        demands=[
            ActivityDemand("05.02.01", "模板工", 500.0),
            ActivityDemand("05.02.01", "混凝土输送泵车", 200.0),
            ActivityDemand("05.02.01", "塔式起重机", 900.0),
        ],
        segment_ids=_ids(3),
        segment_areas=[500.0, 416.5, 416.5],
        mwi_index=MWI,
        role_map=ROLE_MAP,
        user_caps=[UserCap("模板工", 30, "user"), UserCap("模板工", 500, "model")],
        activity_id="05.02.01",
    )
    assert out["segments"], "段级容量字典不得为空"
    for row in out["segments"]:
        for key in ("segment_id", "segment_area", "capacity_fixed", "capacity_mobile"):
            assert key in row, key
    # 场地级被排除但留了标记
    assert out["plans"]["塔式起重机"]["is_site"] is True
    # 用户限额取小
    assert out["user_caps"]["模板工"] == 30
    assert out["effective"]["模板工"] == 30
    # model 来源的限额被丢弃留痕
    assert any("未采纳" in d for d in out["discarded_caps"])
    # 移动型有回分，固定型没有
    assert "混凝土输送泵车" in out["allocations"]
    assert "模板工" not in out["allocations"]
    # 回分总和 = 有效容量
    alloc = out["allocations"]["混凝土输送泵车"]
    assert sum(alloc["allocated"]) == alloc["total"] == out["effective"]["混凝土输送泵车"]


# ==================== B4 分布分解公式 ====================

def test_b4_layer_distribution():
    """B4 公式一：某 L4 在某层的量 = L4 总量 × (该层面积 ÷ 总面积)。"""
    got = layer_distribution(1800.0, {1: 1000.0, 2: 1000.0})
    assert got == {1: 900.0, 2: 900.0}
    got2 = layer_distribution(1000.0, {1: 333.0, 2: 667.0})
    assert got2[1] == pytest.approx(333.0)
    assert got2[2] == pytest.approx(667.0)
    assert sum(got2.values()) == pytest.approx(1000.0)


def test_b4_segment_distribution():
    """B4 公式二：某 L4 在某段的量 = 上一层结果 × (该段面积 ÷ 该层面积)。"""
    got = segment_distribution(510.0, {"Ⅰ": 255.0, "Ⅱ": 255.0})
    assert got == {"Ⅰ": 255.0, "Ⅱ": 255.0}
    got2 = segment_distribution(400.0, [500.0, 300.0])
    assert got2[1] == pytest.approx(250.0)
    assert got2[2] == pytest.approx(150.0)
    assert sum(got2.values()) == pytest.approx(400.0)


def test_b4_zero_area_rejected():
    with pytest.raises(ValueError):
        layer_distribution(100.0, {1: 0.0, 2: 0.0})
    with pytest.raises(ValueError):
        segment_distribution(100.0, [])


# ==================== §6 验收 #5：无 U+33A1 ====================

def test_no_u33a1_square_metre_symbol():
    blob = repr([r.__dict__ for r in MWI_ROWS]) + repr(
        segment_capacity([500.0], _ids(1), MWI["钢筋工"]).as_dict())
    assert "\u33a1" not in blob, "不得出现 U+33A1 方块平米符号"
    assert "m²" in MWI["钢筋工"].mwi_unit

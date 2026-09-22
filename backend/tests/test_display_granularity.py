# -*- coding: utf-8 -*-
"""展示粒度「两个独立维度」的测试 —— 修一个**设计错了 + 没接线**的功能

背景（真实问题）：
`plan_level` 原来是**一个**开关（L3 / L4），它把两件正交的事混在一起：
"同一层内按工种合并" 与 "跨楼层合并"。而每道节拍工序的 `work_type` 本来就不一样，
所以它在层内几乎合并不了任何东西 —— **实际只是在合并楼层**。

真实计划（潭村 12 栋）实测行数矩阵：

    深度 \\ 楼层分组     按层    每5层   整栋
    工序级（现状）        415     116      25
    工种级               338      99      22

结论：**缺的那个轴（楼层分组）才是行数主杠杆（415→116→25，16 倍）**，
现在问的那个轴（L3/L4，≈工种级合并）只省 18%。
所以粒度必须拆成两个维度问，且两个维度都要给出真实行数。

本文件守住：
  1. 楼层分组的三种口径与行数（按层 415 / 每5层 116 / 整栋 25 的**结构性关系**）
  2. 楼层从 location / name 里解析；取不到楼层的不参与楼层分组（"分层外"）
  3. 分组只影响展示，**不改树、不改量、不改工期口径**
  4. 单位不一致时不硬加（别把 m³ 和 t 加成"总量"）
  5. 畸形输入不崩

运行：python -m pytest backend/tests/test_display_granularity.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import quantity as Q  # noqa: E402


# ==================== 夹具：三层 × 每层 2 道工序 + 1 条全楼任务 ====================
def _leaf(tid, floor_lo, floor_hi, step, work_type, unit, qty, duration=3):
    loc = "%s层" % (floor_lo if floor_lo == floor_hi else "%s-%s" % (floor_lo, floor_hi))
    return {
        "id": tid, "name": "Ⅰ区 %s %s" % (loc, step), "location": "Ⅰ区 " + loc,
        "work_type": work_type, "unit": unit, "quantity": qty,
        "duration_days": duration, "_step_name": step,
    }


def _plan(floors=12):
    leaves = []
    for f in range(1, floors + 1):
        leaves.append(_leaf("5.1.%d.1" % f, f, f, "钢筋绑扎", "钢筋工程", "t", 22.0))
        leaves.append(_leaf("5.1.%d.2" % f, f, f, "铝模安装", "模板工程", "m²", 1178.0))
    leaves.append({"id": "9.1.1", "name": "外檐保温（全楼平行）", "location": "全楼",
                   "work_type": "保温工程", "unit": "m²", "quantity": 500.0,
                   "duration_days": 10})
    return {"phases": [{"phase": "主体", "work_packages": [
        {"id": "5.1", "name": "Ⅰ区主体", "sub_packages": leaves}]}]}


# ==================== 1. 楼层解析 ====================
def test_floor_bucket_parses_the_leaf_location():
    leaf = _leaf("a", 16, 20, "钢筋绑扎", "钢筋工程", "t", 1)
    assert Q.floor_bucket(leaf, Q.FLOOR_PER_FLOOR) == "第 16 层"
    assert Q.floor_bucket(leaf, Q.FLOOR_PER_5) == "第 16-20 层"
    assert Q.floor_bucket(leaf, Q.FLOOR_WHOLE) == "整栋"


def test_floor_bucket_handles_half_floors_from_the_basement():
    """地下室是 0.5 层一段（layer_engine 会产出 "1-0.5层" 这种写法）。"""
    leaf = {"id": "x", "name": "Ⅰ区 1-0.5层 混凝土浇筑", "location": "Ⅰ区 1-0.5层"}
    assert Q.floor_bucket(leaf, Q.FLOOR_PER_5) == "第 1-5 层"
    assert Q.floor_bucket(leaf, Q.FLOOR_PER_FLOOR).startswith("第 1")


def test_leaf_without_floor_is_not_floor_grouped():
    """全楼平行 / 场地准备类任务没有楼层，不能硬塞进某一层。"""
    leaf = {"id": "z", "name": "外檐保温（全楼平行）", "location": "全楼"}
    for g in Q.FLOOR_GROUPINGS:
        assert Q.floor_bucket(leaf, g) == "分层外"


def test_grouping_by_5_uses_five_floor_buckets():
    assert Q.floor_bucket(_leaf("a", 1, 1, "x", "w", "t", 1), Q.FLOOR_PER_5) == "第 1-5 层"
    assert Q.floor_bucket(_leaf("a", 5, 5, "x", "w", "t", 1), Q.FLOOR_PER_5) == "第 1-5 层"
    assert Q.floor_bucket(_leaf("a", 6, 6, "x", "w", "t", 1), Q.FLOOR_PER_5) == "第 6-10 层"


# ==================== 2. 行数矩阵（产品的核心判断） ====================
def test_row_matrix_has_all_six_combinations():
    m = Q.estimate_row_matrix(_plan())
    assert set(m) == set(Q.DEPTHS)
    for depth in Q.DEPTHS:
        assert set(m[depth]) == set(Q.FLOOR_GROUPINGS)
        assert all(isinstance(v, int) and v > 0 for v in m[depth].values())


def test_floor_grouping_is_the_real_lever():
    """楼层分组必须**单调地**大幅减少行数；这才是用户想要的粒度开关。

    12 层 × 2 工序 + 1 条全楼 = 25 条叶子：
      按层   → 12×2 + 1 = 25
      每5层  → 3×2 + 1  = 7
      整栋   → 2 + 1    = 3
    """
    m = Q.estimate_row_matrix(_plan())
    row = m[Q.DEPTH_COMPONENT]
    assert row[Q.FLOOR_PER_FLOOR] == 25
    assert row[Q.FLOOR_PER_5] == 7
    assert row[Q.FLOOR_WHOLE] == 3
    assert row[Q.FLOOR_PER_FLOOR] > row[Q.FLOOR_PER_5] > row[Q.FLOOR_WHOLE]


def test_depth_axis_is_the_minor_axis_and_that_is_expected():
    """工种级合并在真实数据里只能省一小部分 —— 把它当主开关是个设计错误。

    这里用两道**同工种不同工序**的叶子构造：工种级能合、工序级不能。
    """
    leaves = [
        _leaf("1", 1, 1, "钢筋绑扎", "钢筋工程", "t", 10),
        _leaf("2", 1, 1, "钢筋焊接", "钢筋工程", "t", 5),
    ]
    wbs = {"phases": [{"phase": "p", "work_packages": [
        {"id": "1.1", "name": "w", "sub_packages": leaves}]}]}
    m = Q.estimate_row_matrix(wbs)
    assert m[Q.DEPTH_COMPONENT][Q.FLOOR_PER_FLOOR] == 2, "工序级不合并"
    assert m[Q.DEPTH_COARSE][Q.FLOOR_PER_FLOOR] == 1, "工种级合并成一行"


def test_estimate_rows_matches_group_rows_length():
    """矩阵里的数字必须与真正分组出来的行数一致（不能两套口径）。"""
    wbs = _plan()
    for depth in Q.DEPTHS:
        for g in Q.FLOOR_GROUPINGS:
            assert Q.estimate_rows_for(wbs, depth, g) == len(Q.group_rows(wbs, depth, g))


def test_row_counts_backward_compatible_helper_still_works():
    """旧的 estimate_row_counts（L3/L4 单维口径）保留，但只是历史兼容。"""
    l3, l4 = Q.estimate_row_counts(_plan())
    assert l4 == 25 and l3 == 3, (l3, l4)   # L3 口径≈整栋+工种合并


# ==================== 3. 分组只影响展示 ====================
def test_group_rows_sums_quantity_within_one_unit():
    rows = Q.group_rows(_plan(), Q.DEPTH_COMPONENT, Q.FLOOR_PER_5)
    rebar = [r for r in rows if r["工序/工种"] == "钢筋绑扎" and r["楼层组"] == "第 1-5 层"]
    assert len(rebar) == 1
    assert rebar[0]["工序数"] == 5
    assert rebar[0]["工程量"] == 110.0 and rebar[0]["单位"] == "t"
    assert rebar[0]["work_package"] == "Ⅰ区主体"


def test_group_rows_does_not_mix_units():
    """**同一工种**下单位不一致时不许硬加成"总量"（m³ 和 t 相加没有意义）。"""
    leaves = [
        {"id": "1", "name": "Ⅰ区 1-1层 混凝土浇筑", "location": "Ⅰ区 1-1层",
         "work_type": "混凝土工程", "unit": "m³", "quantity": 100, "_step_name": "浇筑A"},
        {"id": "2", "name": "Ⅰ区 1-1层 混凝土养护", "location": "Ⅰ区 1-1层",
         "work_type": "混凝土工程", "unit": "m²", "quantity": 50, "_step_name": "养护B"},
    ]
    wbs = {"phases": [{"phase": "p", "work_packages": [
        {"id": "1.1", "name": "w", "sub_packages": leaves}]}]}
    rows = Q.group_rows(wbs, Q.DEPTH_COARSE, Q.FLOOR_PER_FLOOR)
    assert len(rows) == 1, "同工种同楼层 → 合成一行"
    assert rows[0]["工程量"] is None, "单位不一致时不许给总量"
    assert rows[0]["单位"] == "（单位不一）"


def test_group_rows_reports_duration_caliber_honestly():
    """没排程就不许假装有工期口径；给了排程才用时间跨度。"""
    wbs = _plan()
    rows = Q.group_rows(wbs, Q.DEPTH_COMPONENT, Q.FLOOR_WHOLE)
    for r in rows:
        assert r["工期"] is None
        assert r["工期口径"] == "未排程（无跨度可算）"

    schedule = {"5.1.1.1": {"es": 0, "ef": 3}, "5.1.2.1": {"es": 3, "ef": 6}}
    rows2 = Q.group_rows(wbs, Q.DEPTH_COMPONENT, Q.FLOOR_WHOLE, schedule=schedule)
    rebar = [r for r in rows2 if r["工序/工种"] == "钢筋绑扎"][0]
    assert rebar["工期"] == 6 and rebar["工期口径"] == "排程时间跨度"


def test_group_rows_never_mutates_the_tree():
    """分组是纯展示：不许把叶子删掉、合并掉或改字段。"""
    wbs = _plan()
    before = [dict(l) for ph in wbs["phases"] for wp in ph["work_packages"]
              for l in wp["sub_packages"]]
    Q.group_rows(wbs, Q.DEPTH_COARSE, Q.FLOOR_WHOLE)
    after = [l for ph in wbs["phases"] for wp in ph["work_packages"]
             for l in wp["sub_packages"]]
    assert len(after) == len(before) == 25
    assert after[0] == before[0]


# ==================== 4. 容错 ====================
def test_empty_and_broken_input_do_not_crash():
    assert Q.estimate_row_matrix({})[Q.DEPTH_COMPONENT][Q.FLOOR_PER_FLOOR] == 0
    assert Q.group_rows({}) == []
    assert Q.group_rows(None) == []
    assert Q.floor_bucket(None, Q.FLOOR_PER_FLOOR) == "分层外"
    assert Q.step_of(None) == ""
    broken = {"phases": [{"work_packages": [{"sub_packages": [None, "字符串"]}]}]}
    assert Q.group_rows(broken) == []
    assert Q.estimate_rows_for(broken) == 0

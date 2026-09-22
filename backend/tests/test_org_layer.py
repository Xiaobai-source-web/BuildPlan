# -*- coding: utf-8 -*-
"""施工组织层 · **新链路**（2026-09-21 C 组「资源与工期计算收敛」）测试。

唯一口径（依据 `docs/资源与工期计算重构方案_v1.md`）：

    资源只来自**工作面容量**；工期**只有一个公式**：工期 = ceil(需求量 ÷ 有效容量)

    【0】层面积 →【1】按 MSSA=500 ㎡ 切施工段 →【2】段容量 = ceil(段面积 ÷ MWI)
    →【3】需求量 = 工程量 × 定额 →【4】有效容量 = min(汇总容量, 用户同类限额)
    →【5】工期 = ceil(需求量 ÷ 有效容量) →【6】投入资源 = 有效容量

**不读真实 kb.db**：MWI 行全部由 fixture **注入**（`S._MWI_CACHE`），
符合 `segment_capacity.build_mwi_index(rows)` 的契约。

本文件替代了原先钉死**已删除口径**的断言：
  · `org_defaults.eta` / `ETA_FLOOR`（规模效率折减，C8-1）
  · `is_continuous_pour` / `CONTINUOUS_POUR_*`（结构缝，C8-2）
  · `org_plan._retract_crew`（节拍反算人数，C8-3）
  · `org_plan.plan_workfaces`（节拍驱动作业面规划，C8-4）
  · `effective_crew_max` / `crew_ceiling_from_curve` / `CREW_CEILING_*`（×2.5 带，C8-7）
"""

import json
import math
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import org_defaults, org_plan, segment_capacity          # noqa: E402
from pipeline.nodes import scheduler as S                              # noqa: E402
from pipeline.nodes.scheduler import SchedulerNode                     # noqa: E402

# ==================== 注入的 MWI fixture（不读 kb.db）====================
MWI_ROWS = [
    {"resource_name": "钢筋工", "resource_kind": "labor", "mwi": 12.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "普工", "resource_kind": "labor", "mwi": 25.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "混凝土工", "resource_kind": "labor", "mwi": 25.0,
     "mwi_unit": "m2/人", "resource_mobility": "mobile", "capacity_mode": "area"},
    {"resource_name": "架子工", "resource_kind": "labor", "mwi": 20.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "塔吊", "resource_kind": "machine", "mwi": None,
     "mwi_unit": None, "resource_mobility": "site", "capacity_mode": "site"},
    # 裁定 C：area 型机械（与人工同一套公式）
    {"resource_name": "混凝土输送泵车", "resource_kind": "machine", "mwi": 500.0,
     "mwi_unit": "m2/台", "resource_mobility": "mobile", "capacity_mode": "area"},
    # 裁定 C：非 area 型机械（position，mwi 为 NULL）→ 不发明口径
    {"resource_name": "静力压桩机", "resource_kind": "machine", "mwi": None,
     "mwi_unit": None, "resource_mobility": "fixed", "capacity_mode": "position"},
]


@pytest.fixture(autouse=True)
def _inject_mwi(monkeypatch):
    """把 fixture 行注入 MWI 缓存 —— 全文件都不碰真实 kb.db。"""
    monkeypatch.setitem(S._MWI_CACHE, "rows",
                        segment_capacity.build_mwi_index(MWI_ROWS))


# ==================== 构造工具 ====================
def leaf(tid, name, trade, quantity, duration, productivity, cap_labor=None,
         workface=None, mode="labor"):
    """造一条带定额锚定的叶子（`workface` 直接写进叶子的工作面容量标定）。"""
    binding = {
        "task_id": tid,
        "mode": mode,
        "productivity_value": productivity,
        "source_code": "TEST_KB",
        "match_type": "exact",
        "labor_types": [trade],
    }
    row = {
        "id": tid,
        "name": name,
        "quantity": quantity,
        "unit": "m3",
        "duration_days": duration,
        "work_type": trade,
        "norm_binding": binding,
    }
    if cap_labor is not None:
        row["workface_capacity"] = {"max_labor": cap_labor, "unit_basis": "每施工段",
                                    "origin": "kb", "confidence": "LOW",
                                    "note": "测试用工作面容量"}
    if workface is not None:
        row["workface_capacity"] = dict(workface)
    return row


def make_wbs(*rows):
    return {"phases": [{"phase": "测试阶段", "work_packages": [
        {"id": "1.1", "name": "测试工作包", "sub_packages": list(rows)}]}]}


#: 层面积 15000 ÷ 18 层 = 833.33 ㎡ →【1】切 [500, 333.33]（§4.1 验算表第 2 行）
PARAMS = {"total_area": 15000.0, "floors": 18.0, "buildings": 1.0}
FACE = 15000.0 / 18.0


def run_node(wbs, boundary=None, params=None):
    node = SchedulerNode()
    return node, node.run({"wbs": wbs, "boundary_conditions": boundary or {},
                           "extracted_params": params if params is not None else PARAMS})


# 一条"钢筋"叶子：306 工日（quantity=306、P=1.0 单位/工日）
def rebar_leaf(tid="R1", name="1-1层 钢筋绑扎"):
    return leaf(tid, name, "钢筋工", 306, 2, 1.0,
                workface={"max_labor": 16, "crew_base": 8, "crew_min": 4,
                          "crew_max": 16, "crew_preferred": 8, "unit_basis": "每施工段",
                          "confidence": "LOW", "origin": "kb"})


# ==================== ① 已删口径不存在 ====================
def test_已删口径全部不可达():
    """C8 删除清单 1/2/3/4/7：η、结构缝、_retract_crew、plan_workfaces、×2.5 带。"""
    for name in ("eta", "ETA_FLOOR", "is_continuous_pour", "CONTINUOUS_POUR_KEYWORDS",
                 "CONTINUOUS_POUR_ACTIVITIES", "crew_ceiling_from_curve",
                 "CREW_CEILING_BAND", "CREW_CEILING_CAP", "CREW_CURVE_REF",
                 "CREW_SOURCE_ORG_CURVE"):
        assert not hasattr(org_defaults, name), "org_defaults.%s 必须已删" % name
    for name in ("plan_workfaces", "_retract_crew", "effective_crew_max",
                 "CREW_CEILING_BAND", "CREW_CEILING_CAP"):
        assert not hasattr(org_plan, name), "org_plan.%s 必须已删" % name
    for name in ("effective_crew_max", "CREW_CEILING_BAND", "CREW_CEILING_CAP",
                 "resolve_design_crews"):
        assert not hasattr(S, name), "scheduler.%s 必须已删" % name


# ==================== ② §4.1 分段验算表（走 segment_plan，不另写算法）====================
def test_分段验算表七用例():
    cases = [(500.0, [500.0]),
             (833.0, [500.0, 333.0]),
             (1000.0, [500.0, 500.0]),
             (1020.0, [510.0, 510.0]),
             (1280.0, [500.0, 500.0, 280.0]),
             (1500.0, [500.0, 500.0, 500.0]),
             (3000.0, [500.0] * 6)]
    for area, want in cases:
        table = org_plan.build_segment_table(area)
        assert table["ok"] and len(table["segment_areas"]) == len(want), area
        for got, exp in zip(table["segment_areas"], want):
            assert abs(got - exp) < 1e-6, (area, got, exp)


def test_分段表给出段号与规则():
    table = org_plan.build_segment_table(833.0)
    assert table["segment_ids"] == ["Ⅰ", "Ⅱ"]
    assert table["rule"] == "mssa"
    assert table["floor_area"] == 833.0
    bad = org_plan.build_segment_table(None)
    assert bad["ok"] is False and bad["segment_areas"] == [], "缺层面积不许编段"


# ==================== ③ 三种 mobility 分开算（C4）====================
def _row(mobility, mwi=30.0, kind="labor", name="钢筋工"):
    return {"resource_name": name, "resource_kind": kind, "mwi": mwi,
            "mwi_unit": "m2/人", "resource_mobility": mobility,
            "capacity_mode": "area"}


def test_fixed逐段取整后相加_mobile汇总取整一次_数字必须不同():
    """反例用 [100,100,100] + MWI=30：fixed=4+4+4=12，mobile=ceil(300÷30)=10。"""
    areas, ids = [100.0, 100.0, 100.0], ["Ⅰ", "Ⅱ", "Ⅲ"]
    fixed = org_plan.plan_capacity_chain(100.0, areas, ids, _row("fixed"),
                                         resource_name="钢筋工")
    mobile = org_plan.plan_capacity_chain(100.0, areas, ids, _row("mobile"),
                                          resource_name="钢筋工")
    assert fixed["capacity_rollup"] == 12, "fixed = Σ ceil(段面积÷MWI)"
    assert mobile["capacity_rollup"] == 10, "mobile = ceil(Σ段面积÷MWI)"
    assert fixed["crew_total"] != mobile["crew_total"], "两种模式必须真的分开算"
    assert fixed["duration_days"] == 9 and mobile["duration_days"] == 10
    # mobile 走最大余数法回分，总和恒等于有效容量（§4.3）
    assert mobile["allocation"]["allocated"] == [4, 3, 3]
    assert sum(mobile["allocation"]["allocated"]) == mobile["crew_total"] == 10
    assert mobile["allocation"] is not None and fixed["allocation"] is None, \
        "回分只对移动型做"


def test_site独立_不进段容量():
    site = org_plan.plan_capacity_chain(10.0, [100.0, 100.0], ["Ⅰ", "Ⅱ"],
                                        _row("site", None, "machine", "塔吊"),
                                        resource_name="塔吊")
    assert site["resource_mobility"] == "site"
    assert site["capacity_rollup"] == 0
    assert site["crew_total"] is None and site["duration_days"] is None
    assert site["segment_plan"]["is_site"] is True
    assert any("场地级" in x for x in site["basis_lines"])


# ==================== ④ 用户限额三种情形（C9）====================
def test_用户没给限额_不限():
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("fixed", 12.0), resource_name="钢筋工")
    assert org["capacity_rollup"] == 70 and org["crew_total"] == 70
    assert org["user_cap"] is None and org["user_cap_source"] == ""
    assert any("未给同类限额" in x for x in org["basis_lines"])
    assert org["duration_days"] == 5, "ceil(306 ÷ 70)"


def test_用户给了限额_取小():
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("fixed", 12.0), user_cap=20,
                                       user_cap_source="用户申报同类限额",
                                       resource_name="钢筋工")
    assert org["capacity_rollup"] == 70 and org["user_cap"] == 20
    assert org["crew_total"] == 20, "有效容量 = min(70, 20)"
    assert org["duration_days"] == 16, "ceil(306 ÷ 20)"
    assert any("取小过程" in x for x in org["basis_lines"])


def test_模型补的限额被丢弃():
    """`resolve_user_cap` 只认 `_source == 'user'`；model 补的一律丢弃并留痕。"""
    caps = [{"resource_name": "钢筋工", "value": 5, "_source": "model"}]
    cap, discarded = segment_capacity.resolve_user_cap(caps, "钢筋工")
    assert cap is None and discarded, "模型补的限额不许生效，但必须留痕"
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("fixed", 12.0), user_cap=cap,
                                       resource_name="钢筋工")
    assert org["crew_total"] == 70, "丢弃后 → 不限"
    user_caps = [{"resource_name": "钢筋工", "value": 5, "_source": "user"}]
    cap2, _d = segment_capacity.resolve_user_cap(user_caps, "钢筋工")
    assert cap2 == 5


# ==================== ⑤ 唯一工期公式（C10）====================
def test_工期只有一个入口():
    """`duration_days(需求量, 有效容量)` 是唯一公式；各情形逐一对齐 ceil 除法。"""
    for demand, cap in ((306.0, 70), (306.0, 20), (100.0, 12), (1.0, 7), (0.5, 1)):
        org = org_plan.plan_capacity_chain(
            demand, [100.0, 100.0, 100.0], ["Ⅰ", "Ⅱ", "Ⅲ"],
            _row("fixed", 30.0), user_cap=cap, resource_name="钢筋工")
        eff = min(org["capacity_rollup"], cap)
        assert org["crew_total"] == eff, (demand, cap)
        assert org["duration_days"] == math.ceil(demand / eff), (demand, cap)
    # capacity <= 0 / 非法 → None（不猜）
    assert segment_capacity.duration_days(10.0, 0) is None
    assert segment_capacity.duration_days(10.0, None) is None


def test_节拍只作对比展示_不参与计算():
    """C11：给了 / 不给节拍，容量与工期必须逐位相同。"""
    a = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                     _row("fixed", 12.0), resource_name="钢筋工")
    b = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                     _row("fixed", 12.0), resource_name="钢筋工",
                                     cadence_days=3.0, cadence_scope="标准层",
                                     cadence_source="user")
    assert a["crew_total"] == b["crew_total"] == 70
    assert a["duration_days"] == b["duration_days"] == 5, "节拍 3 天也不改工期"
    assert a["cadence_days"] is None and b["cadence_days"] == 3.0


# ==================== ⑥ 端到端：排程行走新链路 ====================
def test_端到端_排程行带段容量字段与依据():
    _, out = run_node(make_wbs(rebar_leaf()))
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]
    org = row["_organization"]
    assert org["source"] == "workface_capacity"
    assert org["person_days"] == 306.0, "需求量 = 本工种工日"
    assert org["capacity_rollup"] == 70 and org["crew_total"] == 70
    assert org["duration_days"] == 5
    assert row["ef"] - row["es"] == 5
    assert row["crew"] == {"钢筋工": 70.0}, "投入资源 = 有效容量"
    assert org["segment_count"] == 2 and org["segment_ids"] == ["Ⅰ", "Ⅱ"]
    assert [s["segment_area"] for s in org["segments"]] == pytest.approx(
        [500.0, FACE - 500.0])
    assert {s["capacity_fixed"] for s in org["segments"]} == {42, 28}
    assert all(s["resource_name"] == "钢筋工" for s in org["segments"])
    assert org["basis"] and "工期 = ceil" in org["basis"]
    # 契约：`_organization` 一定自洽（n_faces×每段容量 ≠ 汇总时不写 crew_per_face）
    assert org["effective_crew_total"] == org["crew_total"]
    assert org["eta"] == 1.0 and org["shifts"] == 1
    assert out["organization_gaps"] == [], "新链路不产生组织缺口"


def test_端到端_留痕说明为什么是N人():
    _, out = run_node(make_wbs(rebar_leaf()))
    capped = [c["reason"] for c in out["schedule_versions"]["resource_ok"]["capped"]
              if c["task_id"] == "R1"]
    assert any("工作面容量（MWI 段容量）" in x for x in capped)
    assert any("段 Ⅰ" in x and "MWI" in x for x in capped), \
        "留痕必须写清 段面积 ÷ MWI → 段容量"


def test_端到端_用户同类限额取小():
    _, out = run_node(make_wbs(rebar_leaf()),
                      boundary={"labor": {"by_trade": [{"trade": "钢筋工",
                                                        "quantity": 20}]}})
    org = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]["_organization"]
    assert org["capacity_rollup"] == 70 and org["user_cap"] == 20
    assert org["crew_total"] == 20 and org["duration_days"] == 16
    capped = [c["reason"] for c in out["schedule_versions"]["resource_ok"]["capped"]
              if c["task_id"] == "R1"]
    assert any("用户同类限额" in x and "取小" in x for x in capped)


def test_端到端_理论版不受用户限额影响():
    _, out = run_node(make_wbs(rebar_leaf()),
                      boundary={"labor": {"peak_total": 10}})
    v = out["schedule_versions"]
    t = [r for r in v["theory_min"]["schedule"] if r["task_id"] == "R1"][0]
    k = [r for r in v["resource_ok"]["schedule"] if r["task_id"] == "R1"][0]
    assert t["_organization"]["crew_total"] == 70, "理论版只看工作面容量"
    assert k["_organization"]["crew_total"] == 10, "资源版取 min(70, 用户 10)"
    assert t["ef"] - t["es"] == 5 and k["ef"] - k["es"] == 31


def test_端到端_MWI缺该资源时如实报缺且不编人数():
    """MWI 表里没有「架子工」以外的工种 → 该任务不许编人数。"""
    lf = leaf("X1", "1-1层 特殊工种", "电焊工", 100, 2, 1.0,
              workface={"max_labor": 6, "crew_base": 4, "crew_min": 2,
                        "crew_max": 6, "unit_basis": "每施工段"})
    item = S._build_ledger_item(lf, "X1", lf["name"])
    item["_leaf"] = lf
    org = S.plan_organization(item, "电焊工", {}, False, face_area=FACE)
    assert org is None, "MWI 表没有该资源 → 不猜容量"
    plan = S._plan_task(item, {"equipment": {}, "by_trade": {}}, False, {},
                        face_area=FACE)
    assert plan["organization"] is None
    assert plan["resources"] == {"电焊工": 4.0}, "退回 KB 工作面容量 cap_labor（裸公式）"
    assert any("工作面容量兜底（非 MWI）" in c["reason"] for c in plan["capped"])


def test_端到端_缺层面积时退回工作面容量兜底并留痕():
    item = S._build_ledger_item(rebar_leaf(), "R1", "1-1层 钢筋绑扎")
    item["_leaf"] = rebar_leaf()
    plan = S._plan_task(item, {"equipment": {}, "by_trade": {}}, False, {}, face_area=None)
    assert plan["organization"] is None
    assert plan["resources"] == {"钢筋工": 8.0}, "退回 KB 工作面容量 cap_labor（= crew_base）"
    assert plan["duration"] == math.ceil(306.0 / 8) == 39
    assert any("工作面容量兜底（非 MWI）" in c["reason"] for c in plan["capped"])


def test_端到端_节拍来源仍被留痕_但只作对比():
    _, out = run_node(make_wbs(rebar_leaf()),
                      boundary={"cadence_days": 7.0,
                                "_source": {"cadence_days": "model"}})
    org = out["schedule_versions"]["resource_ok"]["schedule"][0]["_organization"]
    assert org["cadence_source"] == "model"
    assert org["cadence_days"] == 7.0
    assert org["duration_days"] == 5, "节拍不参与计算"


def test_端到端_机械任务工期也走同一个公式():
    """C10：机械侧 工期 = ceil(总台班 ÷ 台数)，与人工侧同一个 `duration_days`。"""
    assert segment_capacity.duration_days(10.0, 3) == 4
    assert segment_capacity.duration_days(10.0, 4) == 3


# ==================== ⑦ 桩/措施项不再有独立工期口径 ====================
def test_措施项不再有固定操作时长的独立工期():
    """C10 唯一公式：措施项也走 ceil(需求量 ÷ 有效容量)，旧的 measure_item 口径已删。"""
    lf = leaf("C1", "1-1层 爬架提升", "架子工", 99, 5, 1.0,
              workface={"max_labor": 15, "crew_base": 8, "crew_min": 4,
                        "crew_max": 15, "crew_preferred": 10, "unit_basis": "每施工段"})
    _, out = run_node(make_wbs(lf))
    org = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "C1"][0]["_organization"]
    # 架子工 MWI=20 → 833.33 ㎡ → 500/20=25 + 333.33/20=17 → 42 人
    assert org["capacity_rollup"] == 42
    assert org["duration_days"] == math.ceil(99.0 / 42) == 3
    assert org["source"] == "workface_capacity", "不再有 measure_item 口径"
    # 函数本体保留（兼容 import），但新链路不消费它
    assert org_defaults.measure_item_duration("1-1层 爬架提升", "架子工") == \
        (1.0, "爬架提升")


# ==================== ⑧ 守恒 ====================
def test_守恒_新链路不变量():
    cases = ((306.0, 12.0, None), (306.0, 12.0, 20), (99.0, 25.0, 3),
             (5000.0, 30.0, None), (0.5, 15.0, None))
    for pd, mwi, cap in cases:
        org = org_plan.plan_capacity_chain(pd, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                           _row("fixed", mwi), user_cap=cap,
                                           resource_name="钢筋工")
        tag = (pd, mwi, cap)
        assert org["crew_total"] >= 1, tag
        assert org["duration_days"] >= 1, tag
        assert org["duration_days"] == math.ceil(pd / org["crew_total"]), tag
        if cap is None:
            assert org["crew_total"] == org["capacity_rollup"], tag
        else:
            assert org["crew_total"] == min(org["capacity_rollup"], cap), tag
        assert org["planned_person_days"] == org["person_days"] == pd, tag
        assert org["attendance_person_days"] == \
            round(org["crew_total"] * org["duration_days"], 2), tag
        assert len(org["segments"]) == 2, tag
        assert all(s["segment_id"] in ("Ⅰ", "Ⅱ") for s in org["segments"]), tag


def test_守恒_JSON可序列化():
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("mobile", 12.0), resource_name="混凝土工")
    json.dumps(org, ensure_ascii=False, sort_keys=True)


# ==================== ⑨ 裁定 B：三种 capacity_source 可追溯 ====================
def test_裁定B_capacity_source两态():
    """域 1.6 收敛后：容量来源必须逐行可追溯，两态之一，绝不静默。

    ① `"mwi"`                   —— 新链路（段面积 ÷ MWI）
    ② `"reported_missing"`      —— 取不到 MWI → 报缺（域 1.6 已删 Workface_Capacity_Rule）
    """
    wf = {"max_labor": 16, "crew_base": 8, "crew_min": 4, "crew_max": 16}
    # ① MWI
    _, out = run_node(make_wbs(leaf("R1", "R1", "钢筋工", 306, 2, 1.0, workface=dict(wf))))
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]
    assert row["capacity_source"] == "mwi"
    assert row["crew"] == {"钢筋工": 70.0}
    assert "段 Ⅰ" in row["capacity_basis"] and "MWI" in row["capacity_basis"]

    # ② 缺层面积（params 里没有 total_area/floors）→ 切不出段 → 域 1.6 报缺
    _, out = run_node(make_wbs(leaf("R1", "R1", "钢筋工", 306, 2, 1.0, workface=dict(wf))),
                      params={})
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]
    assert row["capacity_source"] == "reported_missing"
    assert "仍走唯一公式" in row["capacity_basis"]

    # ③ 连工作面容量都没有 → 报缺（不编人数，工期沿用叶子原值）
    _, out = run_node(make_wbs(leaf("R1", "R1", "钢筋工", 306, 2, 1.0)), params={})
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]
    assert row["capacity_source"] == "reported_missing"
    assert not row.get("crew"), "不编人数"
    assert row["ef"] - row["es"] == 2, "工期沿用叶子原值"
    assert "未参与容量计算" in row["capacity_basis"]
    assert any("缺工作面容量数据" in w for w in out["schedule_versions"]["warnings"])


# ==================== ⑩ 裁定 C：area 型机械也走 MWI ====================
def test_裁定C_area型机械走MWI_非area型保底并标注():
    """`capacity_mode == "area"` 的机械与人工同一套公式；其余沿用机台规则并标注。"""
    # 混凝土输送泵车：MWI floor 500、mobile → 833.33 ㎡ → ⌈833.33/500⌉ = 2 台
    m = leaf("M1", "1-1层 混凝土浇筑", "混凝土工", 100, 2, 1.0, mode="machine",
             workface={"max_machine": 1, "machine_base": 1, "machine_min": 1,
                       "machine_max": 1, "unit_basis": "每施工段"})
    m["norm_binding"]["machine_name"] = "混凝土输送泵车"
    m["norm_binding"]["norm_value"] = 1.0
    m["norm_binding"]["quantity_basis"] = 1.0
    m["norm_binding"]["productivity_value"] = None
    _, out = run_node(make_wbs(m))
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "M1"][0]
    org = row["_organization"]
    assert row["capacity_source"] == "mwi", "area 型机械必须走 MWI"
    assert org["resource_mobility"] == "mobile"
    assert org["capacity_rollup"] == 2, "⌈833.33 ÷ 500⌉ = 2 台"
    assert org["duration_days"] == 50, "总台班 100 ÷ 2 台"
    # 无层面积 → 域 1.6 报缺（不再退回旧表）
    _, out = run_node(make_wbs(m), params={})
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "M1"][0]
    assert row["capacity_source"] == "reported_missing"
    assert row["ef"] - row["es"] == 100, "1 台 → 100 天"
    assert "MWI" in row["capacity_basis"]

    # 非 area 型（position）：静力压桩机 → 一律 reported_missing + 写明 capacity_mode
    p = leaf("P1", "桩基 静压桩", "桩机工", 120, 2, 1.0, mode="machine",
             workface={"max_machine": 2, "machine_base": 1, "machine_min": 1,
                       "machine_max": 2, "unit_basis": "每施工段"})
    p["norm_binding"]["machine_name"] = "静力压桩机"
    p["norm_binding"]["norm_value"] = 1.0
    p["norm_binding"]["quantity_basis"] = 1.0
    p["norm_binding"]["productivity_value"] = None
    _, out = run_node(make_wbs(p))
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "P1"][0]
    assert row["capacity_source"] == "reported_missing"
    assert "MWI 口径未定义" in row["capacity_basis"]
    assert "capacity_mode=position" in row["capacity_basis"]


# ==================== ⑪ 裁定 E：用户分段规则优先于 MSSA ====================
@pytest.mark.parametrize("rule,want_count,want_areas", [
    (3, 3, None),                                        # int = 段数
    ([600.0, 400.0], 2, [600.0, 400.0]),                 # 面积序列
    ({"segment_count": 4}, 4, None),                     # dict: 段数
    ({"segment_areas": [700.0, 300.0]}, 2, [700.0, 300.0]),   # dict: 面积序列
])
def test_裁定E_segment_rule用户给优先于MSSA(rule, want_count, want_areas):
    """`boundary_conditions.segment_rule`（int / 面积序列 / dict）优先于 MSSA。"""
    wf = {"max_labor": 16, "crew_base": 8, "crew_min": 4, "crew_max": 16}
    _, out = run_node(make_wbs(leaf("R1", "R1", "钢筋工", 306, 2, 1.0, workface=dict(wf))),
                      boundary={"segment_rule": rule})
    row = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]
    org = row["_organization"]
    assert org["segment_rule"] == "user", "用户规则优先于一切（裁定 11）"
    assert org["segment_count"] == want_count
    if want_areas is None:
        assert all(abs(a - FACE / want_count) < 1e-6 for a in org["segment_areas"])
    else:
        assert org["segment_areas"] == want_areas
    # 按用户段面积重算：⌈600/12⌉ + ⌈400/12⌉ = 50 + 34 = 84
    if want_areas == [600.0, 400.0]:
        assert org["capacity_rollup"] == 84 and org["duration_days"] == 4


def test_裁定E_没给规则就走MSSA():
    wf = {"max_labor": 16, "crew_base": 8, "crew_min": 4, "crew_max": 16}
    _, out = run_node(make_wbs(leaf("R1", "R1", "钢筋工", 306, 2, 1.0, workface=dict(wf))))
    org = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]["_organization"]
    assert org["segment_rule"] == "mssa"
    assert org["segment_count"] == 2
    assert org["segment_areas"] == pytest.approx([500.0, FACE - 500.0])
    assert org["capacity_rollup"] == 70


def test_裁定E_无效或无法识别的规则退回MSSA不猜():
    """`floor_overrides` 需要楼层号，排程调用点拿不到 → **不认**、退回 MSSA（见 BLOCKERS）。
    （`floor_overrides` 已由 W4-U 在抽取侧展平，此处仍钉住"消费侧不猜"的兜底行为。）

    ⚠️ `_normalize_user_rule` 的契约是**单返回值**（`scope_inputs.py` 的闸门与
    `tests/test_w4u_input_channels.py` 都按 `is not None` / 直接下标在用）—— 不得改。
    """
    assert org_plan._normalize_user_rule({"floor_overrides": {"1": [500]}}, 833.0) is None
    assert org_plan._normalize_user_rule({}, 833.0) is None
    assert org_plan._normalize_user_rule(0, 833.0) is None
    assert org_plan._normalize_user_rule(None, 833.0) is None
    assert org_plan._normalize_user_rule(3, None) is None
    tbl = org_plan.build_segment_table(833.0, {"floor_overrides": {"1": [500]}})
    assert tbl["rule"] == "mssa" and tbl["user_rule_given"] is False


def test_归一卷入口契约是单返回值():
    """钉住 `_normalize_user_rule` 的返回形状 —— 外部（`scope_inputs.py`、W4-U 的测试）
    按单返回值消费它。留痕版本是另一个函数 `_normalize_user_rule_ex`。"""
    got = org_plan._normalize_user_rule({"segment_count": 2}, 1000.0)
    assert isinstance(got, dict) and got["areas"] == [500.0, 500.0]
    assert org_plan._normalize_user_rule(None, 1000.0) is None
    rule, note = org_plan._normalize_user_rule_ex({"mssa": 0}, 1000.0)
    assert rule is None and "退回 MSSA" in note


# ============ ⑫ MSSA 覆盖通道：`{"mssa": v}` = 用户「每段不超过 v m²」 ============
def test_mssa覆盖_每段不超过400():
    """父代理 2026-09-21 裁定：`{"mssa": v}` 由**本模块**翻译成均匀切 n 段（不动 segment_plan）。"""
    rule, note = org_plan._normalize_user_rule_ex({"mssa": 400}, 1000.0)
    assert rule is not None and len(rule["areas"]) == 3, "ceil(1000 ÷ 400) = 3 段"
    assert all(a <= 400 for a in rule["areas"]), "每段实际面积必须 ≤ 用户上限"
    assert rule["areas"] == [1000 / 3] * 3
    tbl = org_plan.build_segment_table(1000.0, {"mssa": 400})
    assert tbl["rule"] == "user", "用户规则优先于 MSSA（裁定 11）"
    assert len(tbl["segment_areas"]) == 3
    assert all(a <= 400 + 1e-9 for a in tbl["segment_areas"])
    assert "每段不超过 400" in tbl["note"], "翻译过程必须留痕：%s" % tbl["note"]


def test_mssa覆盖_上限大于层面积时一段():
    tbl = org_plan.build_segment_table(1000.0, {"mssa": 5000})
    assert tbl["rule"] == "user" and len(tbl["segment_areas"]) == 1
    assert tbl["segment_areas"] == [1000.0], "ceil(1000 ÷ 5000) = 1 段（整层一段）"
    assert "1 段" in tbl["note"]
    # 上限恰好等于层面积 → 同样 1 段
    t2 = org_plan.build_segment_table(1000.0, {"mssa": 1000})
    assert len(t2["segment_areas"]) == 1 and t2["segment_areas"] == [1000.0]


@pytest.mark.parametrize("bad", [
    {"mssa": 0}, {"mssa": -1}, {"mssa": "abc"}, {"mssa": None},
])
def test_mssa覆盖_非法值退回MSSA并留痕(bad):
    """`v <= 0` / 不是数 → **不猜**，退回 MSSA=500，且 `note` 里写明原因。"""
    rule, note = org_plan._normalize_user_rule_ex(bad, 833.33)
    assert rule is None, "非法上限不许当规则用"
    assert "退回 MSSA" in note, "必须留痕：%r" % note
    tbl = org_plan.build_segment_table(833.33, bad)
    assert tbl["rule"] == "mssa" and tbl["user_rule_given"] is False
    assert "退回 MSSA" in tbl["note"], tbl["note"]
    assert tbl["segment_areas"] == pytest.approx([500.0, 333.33])


def test_mssa覆盖_缺层面积时退回MSSA并留痕():
    """取不到层面积 → 翻译不出段数 → **不猜**（此时 `segment_plan` 自身也会报缺）。"""
    rule, note = org_plan._normalize_user_rule_ex({"mssa": 400}, None)
    assert rule is None and "退回 MSSA" in note
    tbl = org_plan.build_segment_table(None, {"mssa": 400})
    assert tbl["ok"] is False and tbl["segment_areas"] == []
    assert "退回 MSSA" in tbl["note"], "即使层面积缺失也要把'用户规则为何没用上'写清楚"


def test_mssa覆盖_数字字符串也认():
    tbl = org_plan.build_segment_table(1000.0, {"mssa": "400"})
    assert tbl["rule"] == "user" and len(tbl["segment_areas"]) == 3


def test_mssa覆盖_端到端走排程():
    """`boundary_conditions.segment_rule = {"mssa": 400}` 要真的改到段数与人数。"""
    wf = {"max_labor": 16, "crew_base": 8, "crew_min": 4, "crew_max": 16}
    _, out = run_node(make_wbs(leaf("R1", "R1", "钢筋工", 306, 2, 1.0, workface=dict(wf))),
                      boundary={"segment_rule": {"mssa": 400}})
    org = [r for r in out["schedule_versions"]["resource_ok"]["schedule"]
           if r["task_id"] == "R1"][0]["_organization"]
    assert org["segment_rule"] == "user"
    # 层面积 833.33 ÷ 400 → 3 段，每段 277.78 m²；⌈277.78/12⌉ × 3 = 24 × 3 = 72
    assert org["segment_count"] == 3
    assert all(a <= 400 + 1e-9 for a in org["segment_areas"])
    assert org["capacity_rollup"] == 72
    assert "每段不超过 400" in org["segment_rule_note"]


def test_既有四形状不回归_缺省与显式面积均不受MSSA通道影响():
    """MSSA 覆盖通道上线后，既有四种形状逐一复核（行为不许变）。"""
    t = org_plan.build_segment_table(833.33, 3)                     # ① int
    assert t["rule"] == "user" and len(t["segment_areas"]) == 3
    t = org_plan.build_segment_table(833.33, {"segment_count": 4})  # ② segment_count
    assert t["rule"] == "user" and len(t["segment_areas"]) == 4
    t = org_plan.build_segment_table(833.33, [600.0, 400.0])        # ③ 裸序列
    assert t["segment_areas"] == [600.0, 400.0]
    t = org_plan.build_segment_table(833.33, {"segment_areas": [700.0, 300.0]})
    assert t["segment_areas"] == [700.0, 300.0]
    t = org_plan.build_segment_table(833.33, None)                  # ④ 没给 → MSSA
    assert t["rule"] == "mssa" and len(t["segment_areas"]) == 2
    # ⑤ 显式段面积**优先于** mssa（同一 dict 里同时给了两者）
    t = org_plan.build_segment_table(833.33, {"segment_areas": [700.0, 300.0], "mssa": 100})
    assert t["segment_areas"] == [700.0, 300.0], "用户给了逐段面积就不按上限反推"


# ============ ⑬ 用户分段规则的软上限 MAX_USER_SEGMENTS = 200 ============
def test_软上限_是具名模块常量而非内联魔数():
    assert org_plan.MAX_USER_SEGMENTS == 200
    assert "MAX_USER_SEGMENTS" in org_plan.__all__, "常量要能被外部引用（报告/交付物）"


def test_软上限_mssa极小值不生成千万段():
    """① `{"mssa": 0.0001}` + 层面积 1000 → ceil = 10⁷ 段 → **不生成**，退回 MSSA + 留痕。"""
    rule, note = org_plan._normalize_user_rule_ex({"mssa": 0.0001}, 1000.0)
    assert rule is None, "不允许生成 10⁷ 段"
    assert "超过软上限 200" in note and "退回 MSSA" in note, note
    assert "疑似输入有误" in note
    tbl = org_plan.build_segment_table(1000.0, {"mssa": 0.0001})
    assert tbl["rule"] == "mssa" and tbl["segment_areas"] == [500.0, 500.0]
    assert "超过软上限 200" in tbl["note"]
    # 再极端一点也不许炸（1e-9 → 10¹² 段）
    rule2, note2 = org_plan._normalize_user_rule_ex({"mssa": 1e-9}, 1000.0)
    assert rule2 is None and "超过软上限 200" in note2


def test_软上限_segment_count过大被拦():
    """② `{"segment_count": 100000}` → 不猜、退回 MSSA。"""
    rule, note = org_plan._normalize_user_rule_ex({"segment_count": 100000}, 1000.0)
    assert rule is None and "超过软上限 200" in note, note
    tbl = org_plan.build_segment_table(1000.0, {"segment_count": 100000})
    assert tbl["rule"] == "mssa" and tbl["segment_areas"] == [500.0, 500.0]
    assert "超过软上限 200" in tbl["note"]


def test_软上限_裸int过大被拦():
    rule, note = org_plan._normalize_user_rule_ex(100000, 1000.0)
    assert rule is None and "超过软上限 200" in note, note
    assert org_plan.build_segment_table(1000.0, 201)["rule"] == "mssa"


@pytest.mark.parametrize("rule", [
    {"segment_areas": [10.0] * 250},          # dict 里的显式段面积
    {"areas": [10.0] * 250},
    {"segments": [10.0] * 250},
    [10.0] * 250,                             # 裸序列
])
def test_软上限_显式段面积列表过长被拦(rule):
    """③ 显式段面积 —— **列表长度**同样受同一个上限约束。"""
    r, note = org_plan._normalize_user_rule_ex(rule, 1000.0)
    assert r is None and "超过软上限 200" in note, note
    tbl = org_plan.build_segment_table(1000.0, rule)
    assert tbl["rule"] == "mssa" and tbl["segment_areas"] == [500.0, 500.0]
    assert "超过软上限 200" in tbl["note"]


def test_软上限_边界200正常通过201被拦():
    """④ 边界：`n == 200` **正常通过**（不许误伤合法值）；`n == 201` 才拦。"""
    # segment_count = 200 → 200 段，每段 5 m²
    rule, note = org_plan._normalize_user_rule_ex({"segment_count": 200}, 1000.0)
    assert rule is not None and len(rule["areas"]) == 200 and note == ""
    assert set(rule["areas"]) == {5.0}
    # mssa = 5.0 → ceil(1000 ÷ 5) = 200 → 恰好在线内
    r2, n2 = org_plan._normalize_user_rule_ex({"mssa": 5.0}, 1000.0)
    assert r2 is not None and len(r2["areas"]) == 200 and n2 == ""
    # 显式 200 段也放行
    r3, n3 = org_plan._normalize_user_rule_ex({"segment_areas": [5.0] * 200}, 1000.0)
    assert r3 is not None and n3 == ""
    # 201 → 拦
    assert org_plan.build_segment_table(1000.0, {"segment_count": 201})["rule"] == "mssa"
    assert org_plan.build_segment_table(1000.0, {"mssa": 4.9})["rule"] == "mssa"
    assert org_plan.build_segment_table(
        1000.0, {"segment_areas": [5.0] * 201})["rule"] == "mssa"


def test_软上限_只约束用户规则通道不碰segment_plan内部():
    """上限只在"用户规则"通道生效；`segment_plan` 自身的分段不受影响。"""
    from pipeline import segment_plan as sp
    # segment_plan 自己的 suggest_zones / compute_segment_areas 不受 200 限制
    assert sp.suggest_zone_count(4000.0) == 8
    assert sp.suggest_zone_count(5656.0) == 11
    assert len(sp.compute_segment_areas(1000.0)) == 2, "无用户规则时仍按 MSSA 切"
    # 且 MSSA 自身切不出超过 200 段（层面积 ≤ 100 000 才可能，这里不设限、只验证不受我影响）
    assert org_plan.build_segment_table(1000.0, None)["rule"] == "mssa"


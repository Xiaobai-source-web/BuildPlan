# -*- coding: utf-8 -*-
"""域 7 第 4 批：**7.11「不展开的活动按施工面积开段」** + **逐日份额接进组织层容量链**。

依据 `docs/域7_资源层_实现设计.md` §3.5（7.11）/ §4.4（`_daily_share` 形状）/
§7（判据表 A1–A6）/ §14.2 父代理裁决 2（`measure_scope` 是面积的唯一真源）
+ 父代理 2026-09-21 冻结接口（`plan_capacity_chain(daily_share=…)`）。

## 两条判据（父代理冻结，**不许自创**）

    第一判据 = `L4_Activity_Dictionary.is_l5_expandable == 0`（KB 决定）
    第二判据 = 树内叶子没有 `segment_id`

⚠️ `is_standalone_activity` 实测 493 行**全为 NULL**，本文件**不使用它**。

## 面积口径（§14.2 裁决 2）

`measure_scope` 是唯一真源；**为空时**（实测 98 行）必须**回退 `Σ floor_areas`** 并
在 `basis` 里写明"回退"，**不许静默**。

## 边界样例（真实 KB 值）

`GD_A11_平整场地`：`unit='m²'` / `measure_scope='建筑面积'` / `is_l5_expandable=0` /
`equipment_driven`；`total_area=14200 m²`；履带式推土机 `mwi=667.0 m2/台`
→ `⌈14200 ÷ 667⌉ = 22 台`。

⚠️ 14200 **恰与 `total_area` 相等、无法区分两种口径** —— 本文件钉住"必须诚实留痕"。

**不读真实 kb.db**（除一个显式的只读探查用例）：MWI 行全部由 fixture 注入。
"""

import math
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import org_plan, segment_capacity                     # noqa: E402
from pipeline.nodes import resource as R                            # noqa: E402

# ==================== 真实 KB 样例的常量（只读实测，绝不写库）====================
PINGZHENG_TOTAL_AREA = 14200.0          # GD_A11_平整场地 的 total_area（m²）
TUIDUJI_MWI = 667.0                     # 履带式推土机 mwi（m2/台）
PINGZHENG_MWI_ROW = {
    "resource_name": "履带式推土机", "resource_kind": "machine", "mwi": TUIDUJI_MWI,
    "mwi_unit": "m2/台", "resource_mobility": "mobile", "capacity_mode": "area",
}


# ==================== ① 7.11 判据：第一 + 第二判据同时成立才"不展开" ====================
def test_711_判据_不展开时按施工面积开段且段数为1():
    """第一判据（`is_l5_expandable==0`）∧ 第二判据（叶子无 `segment_id`）→ 段数 = 1。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": PINGZHENG_TOTAL_AREA},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW,
        activity_id="GD_A11_平整场地", activity_name="平整场地")
    assert out["ok"] is True
    assert out["expandable"] == 0
    assert out["segment_count"] == 1, "不展开的一次性活动段数恒为 1"
    assert out["segment_ids"] == ["Ⅰ"] and out["segment_areas"] == [PINGZHENG_TOTAL_AREA]
    assert out["caliber"] == "construction_area"
    assert "不展开" in out["basis"] and "不需要楼层范围" in out["basis"]


def test_711_判据_第二判据不成立时走按层展开():
    """有 `segment_id` → 第二判据不成立 → 仍走可分层实体工程（按层展开）。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": 15000.0, "floors": 18.0},
        is_l5_expandable=0, leaf_segment_id="Ⅰ", mwi_row=PINGZHENG_MWI_ROW,
        floor_area=15000.0 / 18.0)
    assert out["caliber"] == "floor_area"
    assert out["segment_count"] == 2, "833.33 m² 按 MSSA=500 切 2 段"
    assert out["segment_areas"] == pytest.approx([500.0, 15000.0 / 18.0 - 500.0])


def test_711_判据_可展开活动不走本口径():
    """`is_l5_expandable == 1` → **可展开**，即使没有 `segment_id` 也不按施工面积开段。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": 1000.0},
        is_l5_expandable=1, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW,
        floor_area=500.0)
    assert out["caliber"] == "floor_area"
    assert out["segment_count"] == 1, "层面积 500 → MSSA 500 → 1 段"


def test_711_判据_未知时不判不展开():
    """`is_l5_expandable` 未知（KB 取不到）→ **不判"不展开"**，沿用可分层口径。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": 1000.0},
        is_l5_expandable=None, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW,
        floor_area=1000.0)
    assert out["caliber"] == "floor_area"
    assert "未知" in out["basis"]


def test_711_不使用is_standalone_activity():
    """`is_standalone_activity` 全库 NULL、**不可用** —— 本批代码一个字都不该碰它。

    （只允许出现在注释/文档字符串里"解释为什么不能用"；代码里出现即失败。）
    """
    import io
    import tokenize
    src = (BACKEND / "pipeline" / "org_plan.py").read_text(encoding="utf-8")
    code = "".join(
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING))
    assert "is_standalone_activity" not in code, \
        "7.11 判据不许用 is_standalone_activity（493 行全 NULL）"
    # 判据字段本身必须真的被用到（第一判据 = is_l5_expandable）
    assert "is_l5_expandable" in code


# ==================== ② 面积口径：`measure_scope` 是唯一真源 ====================
def test_711_边界样例_平整场地14200推土机667算22台():
    """★ 交付汇报要求的边界用例：`⌈14200 ÷ 667⌉ = 22 台`，且两种口径无法区分要留痕。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": PINGZHENG_TOTAL_AREA},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW,
        activity_id="GD_A11_平整场地", activity_name="平整场地")
    assert out["face_area"] == PINGZHENG_TOTAL_AREA
    assert out["area_source"] == "total_area"
    assert out["capacity_units"] == 22 == int(math.ceil(14200.0 / 667.0))
    # ① 依据写明了 `measure_scope`
    assert "measure_scope='建筑面积'" in out["basis"]
    # ② 诚实留痕：14200 == total_area，**无法区分**"按建筑面积"与"按 total_area"两种口径
    assert "无法区分两种口径" in out["basis"], out["basis"]


def test_711_measure_scope为空时回退层面积合计并留痕():
    """★ 硬要求 ①：`measure_scope` 为空（实测 98 行）→ 回退 `Σ floor_areas` + 写明回退。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "", "total_area": 9999.0,
         "floor_areas": [500.0, 500.0, 333.33]},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW,
        activity_name="平整场地")
    assert out["face_area"] == pytest.approx(1333.33)
    assert out["area_source"] == "sum_floor_areas"
    assert "measure_scope 为空" in out["basis"] and "回退层面积合计" in out["basis"], out["basis"]
    assert out["segment_count"] == 1
    assert out["capacity_units"] == int(math.ceil(1333.33 / 667.0)) == 2


def test_711_层面积也取不到时退回total_area并留痕():
    """`measure_scope` 为空 **且** 层面积取不到 → 再回退 `total_area`，仍要写明。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "", "total_area": 2000.0},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW)
    assert out["face_area"] == 2000.0 and out["area_source"] == "total_area"
    assert "回退 total_area" in out["basis"], out["basis"]


def test_711_建筑面积口径下逐层面积优先于总面积():
    """`measure_scope=建筑面积` 且给了逐层面积 → 按层面积合计（并留痕说明为什么）。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": 9999.0,
         "floor_areas": [100.0, 200.0]},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW)
    assert out["face_area"] == 300.0 and out["area_source"] == "sum_floor_areas"
    assert "层面积合计" in out["basis"]


@pytest.mark.parametrize("scope,expect_word", [
    ("楼地面面积", "按**层面积合计**取数"),
    ("天棚面积", "按**层面积合计**取数"),
    ("模板接触面积", "按**层面积合计**取数"),
    ("防水面积", "按**层面积合计**取数"),
    ("保温面积", "按**层面积合计**取数"),
    ("外墙面积", "按**层面积合计**取数"),
    ("内墙抹灰面积", "按**层面积合计**取数"),
    ("风管展开面积", "按**层面积合计**取数"),
])
def test_711_其它面积口径词走层面积合计(scope, expect_word):
    out = org_plan.face_area_for_activity(
        {"measure_scope": scope, "total_area": 8000.0, "floor_areas": [700.0, 700.0]},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW)
    assert out["face_area"] == 1400.0 and out["area_source"] == "sum_floor_areas"
    assert expect_word in out["basis"], out["basis"]


@pytest.mark.parametrize("scope", ["体积", "质量", "自然单位", "项", "台数",
                                  "管道长度", "电缆长度", "桩根数"])
def test_711_非面积口径词注记不静默(scope):
    """A5：非面积口径 → 仍按 MWI 公式，但**必须注记**"非面积口径"。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": scope, "total_area": 12000.0, "floor_areas": [600.0]},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW)
    assert out["face_area"] == 600.0
    assert "非面积口径" in out["basis"] and "MWI 公式" in out["basis"], out["basis"]


def test_711_词表外的measure_scope不猜口径():
    """`m²` 是 `unit` 不是计量对象 → 不在词表内 → 按层面积合计 + 如实记原值（不猜）。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "m²", "total_area": 500.0, "floor_areas": [250.0, 250.0]},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW)
    assert out["face_area"] == 500.0
    assert "不在受控词表内" in out["basis"] and "不猜口径" in out["basis"]


def test_711_面积全缺时报缺不编面积():
    """A6：面积一条都取不到 → `ok=False` + 写明报缺，**绝不编面积**。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积"},
        is_l5_expandable=0, leaf_segment_id=None, mwi_row=PINGZHENG_MWI_ROW)
    assert out["ok"] is False
    assert out["face_area"] is None and out["capacity_units"] is None
    assert out["segment_ids"] == [] and out["segment_areas"] == []
    assert "报缺" in out["basis"]


def test_711_MWI取不到时不编台数():
    """MWI 缺该资源 → 面积证据照给，但 `capacity_units=None`（**不猜台数**）。"""
    out = org_plan.face_area_for_activity(
        {"measure_scope": "建筑面积", "total_area": 1000.0},
        is_l5_expandable=0, leaf_segment_id=None,
        mwi_row={"resource_name": "人力", "mwi": None})
    assert out["face_area"] == 1000.0 and out["capacity_units"] is None
    assert "不编台数" in out["basis"]


# ==================== ③ 判据实现位置可被引用（交付汇报第 3 项）====================
def test_711_判据实现位置与常量可导出():
    """判据实现 = `org_plan.face_area_for_activity`；词表常量必须可被报告引用。"""
    assert callable(org_plan.face_area_for_activity)
    assert "face_area_for_activity" in org_plan.__all__
    assert org_plan.AREA_SCOPE_BUILDING == "建筑面积"
    assert "天棚面积" in org_plan.AREA_SCOPE_NON_BUILDING
    assert "体积" in org_plan.NON_AREA_SCOPES
    # KB 只读取数器在 resource.py（scheduler 的调用点由父代理收口）
    assert callable(R.activity_l5_expandable)
    assert callable(R.activity_measure_scope_of_l4)


def test_711_纯函数确定性_同输入逐位同输出():
    """7.12 冻结的前提：同输入**逐位**同输出（无随机 / 无时间 / 无 set 迭代序）。"""
    args = ({"measure_scope": "", "total_area": 9999.0,
             "floor_areas": [500.0, 500.0, 333.33]},)
    kw = {"is_l5_expandable": 0, "leaf_segment_id": None,
          "mwi_row": PINGZHENG_MWI_ROW, "activity_name": "平整场地"}
    a = org_plan.face_area_for_activity(*args, **kw)
    b = org_plan.face_area_for_activity(*args, **kw)
    assert a == b
    import json
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True)


# ==================== ④ B：`plan_capacity_chain` 的 `daily_share` 接口 ====================
def _row(mobility, mwi=30.0, kind="labor", name="钢筋工"):
    return {"resource_name": name, "resource_kind": kind, "mwi": mwi,
            "mwi_unit": "m2/人", "resource_mobility": mobility,
            "capacity_mode": "area"}


def test_B_签名只有新增的daily_share():
    """★ 接口冻结：只新增**一个**关键字形参 `daily_share`，其余一个都不许动。"""
    import inspect
    sig = inspect.signature(org_plan.plan_capacity_chain)
    names = list(sig.parameters)
    assert names == ["demand", "segment_areas", "segment_ids", "mwi_row",
                     "user_cap", "user_cap_source", "aliases", "resource_name",
                     "capacity_source", "cadence_days", "cadence_scope",
                     "cadence_source", "extra_warnings", "daily_share"], names
    p = sig.parameters
    assert p["daily_share"].kind is inspect.Parameter.KEYWORD_ONLY, \
        "daily_share 必须是**关键字**形参"
    assert p["daily_share"].default is None, "缺省必须是 None（= 逐字段退回旧行为）"
    for name in ("demand", "segment_areas", "segment_ids", "mwi_row"):
        assert p[name].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name


def test_B_daily_share为None时键集只多effective_source():
    """★ 硬要求：`daily_share=None` ⇒ 逐字段退回旧行为（老键名/类型一个都没动）。"""
    base = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                        _row("fixed", 12.0), resource_name="钢筋工")
    assert base["effective_source"] == "legacy"
    # 与"不传 daily_share"逐字段相同（含老键的类型）
    assert base == org_plan.plan_capacity_chain(
        306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"], _row("fixed", 12.0),
        resource_name="钢筋工", daily_share=None)
    assert base["crew_total"] == base["effective_crew_total"] == \
        base["capacity_effective"] == 70
    assert base["duration_days"] == 5 and base["capacity_rollup"] == 70
    assert isinstance(base["crew_total"], int)
    assert "effective_source" in base


def test_B_daily_share非None时取小且留痕():
    """`daily_share=20` → 有效容量 = min(70, 20) = 20、工期 = ⌈306÷20⌉ = 16。"""
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("fixed", 12.0), user_cap=70,
                                       resource_name="钢筋工", daily_share=20)
    assert org["effective_source"] == "daily_share"
    assert org["capacity_rollup"] == 70
    assert org["crew_total"] == 20 and org["duration_days"] == 16
    assert any("逐日份额" in x for x in org["basis_lines"]), org["basis_lines"]
    assert any("逐日份额取小过程" in x for x in org["basis_lines"]), org["basis_lines"]


def test_B_daily_share与用户限额一起取小():
    """`min(汇总容量, 用户限额, 逐日份额)` —— 三档同取小，逐位可复算。"""
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("fixed", 12.0), user_cap=40,
                                       resource_name="钢筋工", daily_share=25)
    assert org["crew_total"] == 25 and org["duration_days"] == math.ceil(306 / 25)
    assert org["effective_source"] == "daily_share"
    # 份额比其它两档都大 → 取小后仍等于用户限额，但来源必须记实
    org2 = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                        _row("fixed", 12.0), user_cap=40,
                                        resource_name="钢筋工", daily_share=400)
    assert org2["crew_total"] == 40 and org2["effective_source"] == "daily_share"
    assert org2["duration_days"] == math.ceil(306 / 40)


def test_B_daily_share非法值抛ValueError不静默():
    """份额 `<= 0` 是上游回压算错 → **必须抛**（由 `effective_capacity_daily` 抛）。"""
    for bad in (0, -1):
        with pytest.raises(ValueError):
            org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                         _row("fixed", 12.0), daily_share=bad)


def test_B_走的是segment_capacity的唯一实现():
    """取小**只调** `effective_capacity_daily`，本模块不许重写一遍取小逻辑。"""
    src = (BACKEND / "pipeline" / "org_plan.py").read_text(encoding="utf-8")
    assert "effective_capacity_daily(" in src
    # 不许出现第二个 min(…, daily_share) 形式的自造取小
    assert "min(int(eff" not in src


def test_B_移动型回分随逐日份额重算():
    """移动型：份额压小有效容量后，最大余数法回分**必须跟着重算**（总和 = 新容量）。"""
    org = org_plan.plan_capacity_chain(500.0, [100.0, 100.0, 100.0], ["Ⅰ", "Ⅱ", "Ⅲ"],
                                       _row("mobile", 30.0), resource_name="混凝土工",
                                       daily_share=4)
    assert org["allocation"] is not None
    assert sum(org["allocation"]["allocated"]) == org["crew_total"] == 4
    assert org["duration_days"] == math.ceil(500 / 4)


def test_B_attendance随份额重算():
    """出勤工日 = 有效容量 × 工期，份额改了两者都要跟着改。"""
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("fixed", 12.0), resource_name="钢筋工",
                                       daily_share=20)
    assert org["attendance_person_days"] == round(20 * 16, 2)
    assert org["planned_person_days"] == org["person_days"] == 306.0


def test_B_site型不受份额影响():
    """场地级资源不进段容量（有效容量 None）→ 份额也无处可施，不许造出容量。"""
    org = org_plan.plan_capacity_chain(10.0, [100.0, 100.0], ["Ⅰ", "Ⅱ"],
                                       _row("site", None, "machine", "塔吊"),
                                       resource_name="塔吊", daily_share=5)
    assert org["crew_total"] is None and org["duration_days"] is None
    assert org["effective_source"] == "daily_share", "给了份额就如实记来源"


def test_B_JSON可序列化且确定性():
    import json
    kw = dict(resource_name="钢筋工", daily_share=22)
    a = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                     _row("fixed", 12.0), **kw)
    b = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                     _row("fixed", 12.0), **kw)
    ja = json.dumps(a, ensure_ascii=False, sort_keys=True)
    assert ja == json.dumps(b, ensure_ascii=False, sort_keys=True)


def test_B_与segment_capacity逐位同源():
    """同输入下 `effective_source` 的两态与 `effective_capacity_daily` 逐位一致。"""
    for share in (None, 1, 4, 22, 70, 71):
        got = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                           _row("fixed", 12.0), daily_share=share)
        want = segment_capacity.effective_capacity_daily(70, share)
        assert got["crew_total"] == want, share
        assert got["effective_source"] == ("legacy" if share is None else "daily_share")


# ==================== ⑤ 老行为回归：`daily_share=None` 逐字段等于改造前 ====================
OLD_CASES = [
    # (demand, areas, ids, mobility, mwi, user_cap)
    (306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"], "fixed", 12.0, None),
    (306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"], "fixed", 12.0, 20),
    (100.0, [100.0, 100.0, 100.0], ["Ⅰ", "Ⅱ", "Ⅲ"], "mobile", 30.0, None),
    (99.0, [500.0, 333.33], ["Ⅰ", "Ⅱ"], "fixed", 20.0, None),
    (5000.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"], "fixed", 30.0, None),
    (0.5, [500.0, 333.0], ["Ⅰ", "Ⅱ"], "fixed", 15.0, None),
    (10.0, [100.0, 100.0], ["Ⅰ", "Ⅱ"], "site", None, None),
]


@pytest.mark.parametrize("demand,areas,ids,mobility,mwi,cap", OLD_CASES)
def test_回归_daily_share为None时逐字段等于老公式(demand, areas, ids, mobility, mwi, cap):
    """老口径（无逐日份额）逐字段复算：`crew_total = min(rollup, cap)`、
    `duration = ceil(demand ÷ crew_total)`、`capacity_effective` 三键同值。"""
    org = org_plan.plan_capacity_chain(demand, areas, ids, _row(mobility, mwi),
                                       user_cap=cap, resource_name="测试工种")
    tag = (demand, mobility, mwi, cap)
    if mobility == "site":
        assert org["crew_total"] is None and org["duration_days"] is None, tag
        assert org["capacity_rollup"] == 0 and org["effective_source"] == "legacy", tag
        return
    want = org["capacity_rollup"] if cap is None else min(org["capacity_rollup"], cap)
    assert org["crew_total"] == want, tag
    assert org["effective_crew_total"] == want and org["capacity_effective"] == want, tag
    assert org["duration_days"] == math.ceil(demand / want), tag
    assert org["effective_source"] == "legacy", tag
    assert org["attendance_person_days"] == round(want * math.ceil(demand / want), 2), tag
    assert len(org["segments"]) == len(ids), tag
    assert org["segment_ids"] == ids and org["segment_areas"] == areas, tag

# -*- coding: utf-8 -*-
"""终版修改 · WS4（组织层）：**每工/每台能上多少只有一个来源**。

⚠️ **2026-09-21 C 组改写**：原文件钉的是「组织层工种曲线 = 组织层每面人数单一来源」，
即 `org_defaults.crew_ceiling_from_curve = max(crew_max, min(40, ceil(crew_base×2.5)))`，
与 `scheduler.effective_crew_max` 逐值一致。**那条 ×2.5 带已按 C 组 C8 删除清单第 7 项
整条删除**（无规范依据），本文件随之改写为新链路的"单一来源"口径：

    容量的**唯一来源** = MWI 表 `Resource_Workface_Index`
        （`mwi` + `resource_mobility`，由 P1 维护）
    段容量 `n_i = ceil(段面积 ÷ MWI)` → 汇总（fixed 逐段相加 / mobile 汇总取整一次）
    → 有效容量 = min(汇总容量, 用户同类限额) → 工期 = ceil(需求量 ÷ 有效容量)

本文件钉四件事：
  ① 旧来源（`org_curve` / `crew_ceiling_from_curve` / `effective_crew_max` / `CREW_CEILING_*`）
     **全部不可达**，且组织层输出里不再有 `crew_source`、`eta` 恒为中性值 1.0；
  ② 新来源只有一个实现：`segment_capacity.segment_capacity` + `org_plan.plan_capacity_chain`
     （组织层不另写一份容量公式）；
  ③ 不变式 `duration_days == max(1, ceil(person_days ÷ 有效容量))` 在
     fixed / mobile / site / 有用户限额 / 无用户限额 各路径上都成立；
  ④ `MWIRow` 契约：`resource_mobility` 缺列/缺值 → **不猜**（`plan_organization` 返回 None）。

不联网、不调 LLM、**不读 kb.db**：MWI 行走 fixture 注入（符合 `build_mwi_index` 契约）。
"""

import math
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import org_defaults, org_plan, segment_capacity        # noqa: E402
from pipeline.nodes import scheduler as S                            # noqa: E402

# ==================== 注入的 MWI fixture ====================
FIXTURE_ROWS = [
    {"resource_name": "钢筋工", "resource_kind": "labor", "mwi": 12.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "模板工", "resource_kind": "labor", "mwi": 15.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "抹灰工", "resource_kind": "labor", "mwi": 12.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "普工", "resource_kind": "labor", "mwi": 25.0,
     "mwi_unit": "m2/人", "resource_mobility": "fixed", "capacity_mode": "area"},
    {"resource_name": "混凝土工", "resource_kind": "labor", "mwi": 25.0,
     "mwi_unit": "m2/人", "resource_mobility": "mobile", "capacity_mode": "area"},
    {"resource_name": "塔吊", "resource_kind": "machine", "mwi": None,
     "mwi_unit": None, "resource_mobility": "site", "capacity_mode": "site"},
    # `resource_mobility` 缺值 → 必须报缺，不许按名字猜型别
    {"resource_name": "电焊工", "resource_kind": "labor", "mwi": 12.0,
     "mwi_unit": "m2/人", "resource_mobility": None, "capacity_mode": "area"},
]


@pytest.fixture(autouse=True)
def _inject_mwi(monkeypatch):
    """注入**原始行**（`_MWI_CACHE["raw"]`），让 `_mwi_rows_by_name` 走同一条校验路径。"""
    monkeypatch.setitem(S._MWI_CACHE, "raw", [dict(r) for r in FIXTURE_ROWS])
    monkeypatch.delitem(S._MWI_CACHE, "rows", raising=False)


def _row(name, mobility="fixed", mwi=12.0, kind="labor"):
    return {"resource_name": name, "resource_kind": kind, "mwi": mwi,
            "mwi_unit": "m2/人", "resource_mobility": mobility,
            "capacity_mode": "area"}


# ==================== ① 旧来源全部不可达 ====================
def test_旧曲线来源全部不可达():
    for mod, names in (
            (org_defaults, ("eta", "ETA_FLOOR", "crew_ceiling_from_curve",
                            "CREW_CEILING_BAND", "CREW_CEILING_CAP", "CREW_SOURCE_ORG_CURVE",
                            "CREW_CURVE_REF")),
            (org_plan, ("plan_workfaces", "effective_crew_max", "CREW_CEILING_BAND",
                        "CREW_CEILING_CAP", "CREW_SOURCE_ORG_CURVE")),
            (S, ("effective_crew_max", "CREW_CEILING_BAND", "CREW_CEILING_CAP",
                 "resolve_design_crews"))):
        for name in names:
            assert not hasattr(mod, name), "%s.%s 必须已删（C8）" % (mod.__name__, name)


def test_组织层不再另写容量公式():
    """容量只有一个实现：`segment_capacity`；`org_plan` 只做串接。"""
    src = Path(org_plan.__file__).read_text(encoding="utf-8")
    body = src.split("def plan_capacity_chain", 1)[1].split("\ndef ", 1)[0]
    assert "segment_capacity.segment_capacity(" in body
    assert "segment_capacity.duration_days(" in body
    assert not re.search(r"math\.ceil\([^)]*(area|A_seg)", body), \
        "不许在组织层复制 ceil(面积 ÷ MWI) 这类容量算术"
    assert "效率折减" not in src.split('"""', 2)[-1]


def test_组织层输出的eta是中性值():
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row("钢筋工"), resource_name="钢筋工")
    assert org["eta"] == 1.0, "η 已删 → 不再有任何效率折减"
    assert org["effective_crew_total"] == org["crew_total"]
    assert "crew_source" not in org, "旧 `crew_source=org_curve` 标记已删"
    assert org["source"] == "workface_capacity"


# ==================== ② 单一来源 = MWI 表 ====================
def test_容量唯一来源是MWI表():
    """同一份段面积，MWI 不同 → 容量不同（证明容量真的来自 MWI，不是别的常量）。"""
    got = {}
    for mwi in (12.0, 15.0, 25.0):
        org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                           _row("钢筋工", mwi=mwi), resource_name="钢筋工")
        got[mwi] = org["capacity_rollup"]
    assert got == {12.0: 42 + 28, 15.0: 34 + 23, 25.0: 20 + 14}, got
    assert got[12.0] == math.ceil(500 / 12) + math.ceil(333 / 12)


def test_mobility缺值时不猜型别():
    """`resource_mobility` 为 None → 该行被剔除、调用方报缺，绝不按资源名猜。"""
    assert org_plan.mobility_of({"resource_mobility": None}) is None
    assert org_plan.mobility_of({"resource_mobility": "奇怪"}) is None
    assert org_plan.mobility_of({"resource_mobility": "fixed"}) == "fixed"
    assert S._mwi_row_of("电焊工") is None, "型别缺值的行不许默认成 fixed"
    assert "电焊工" in S._MWI_CACHE.get("skipped", []), "被剔除的行必须留痕"
    assert S._mwi_row_of("钢筋工") is not None, "型别合法的行照常可用"


def test_代码里没有资源名到型别的映射表():
    """C4 的型别只许来自 MWI 表的列，不许在代码里写死映射。"""
    src = Path(S.__file__).read_text(encoding="utf-8")
    head = src.split("def _mwi_rows_by_name", 1)[1].split("\ndef ", 1)[0]
    assert "resource_mobility" in head
    for banned in ("MOBILITY_BY_NAME", "FIXED_TRADES", "MOBILE_PATTERNS"):
        assert banned not in src, "不许出现写死的型别映射表：%s" % banned


# ==================== ③ 唯一工期不变式在每条路径上成立 ====================
def test_工期不变式在每种型别与限额上都成立():
    for mobility, cap in (("fixed", None), ("fixed", 20), ("mobile", None),
                          ("mobile", 7)):
        for pd in (0.5, 1.0, 33.333333, 221.484013, 306.0, 5000.0):
            org = org_plan.plan_capacity_chain(
                pd, [500.0, 333.0], ["Ⅰ", "Ⅱ"], _row("钢筋工", mobility=mobility),
                user_cap=cap, resource_name="钢筋工")
            eff = org["crew_total"]
            tag = (mobility, cap, pd)
            assert eff >= 1, tag
            assert org["person_days"] == pd, tag
            assert org["duration_days"] == max(1, math.ceil(pd / eff)), tag
            if cap is None:
                assert eff == org["capacity_rollup"], tag
            else:
                assert eff == min(org["capacity_rollup"], cap), tag


def test_场地级不进段容量():
    org = org_plan.plan_capacity_chain(
        10.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
        _row("塔吊", mobility="site", mwi=None, kind="machine"), resource_name="塔吊")
    assert org["capacity_rollup"] == 0 and org["crew_total"] is None
    assert org["duration_days"] is None, "场地级由 _site_equipment 独立给，不由本链算"
    assert org["segment_plan"]["is_site"] is True


# ==================== ④ 主要工种从 MWI 可复核 ====================
@pytest.mark.parametrize("trade,mwi,want", [
    ("钢筋工", 12.0, 42 + 28),
    ("模板工", 15.0, 34 + 23),
    ("抹灰工", 12.0, 42 + 28),
    ("普工", 25.0, 20 + 14),
])
def test_主要工种段容量逐段可复核(trade, mwi, want):
    org = org_plan.plan_capacity_chain(306.0, [500.0, 333.0], ["Ⅰ", "Ⅱ"],
                                       _row(trade, mwi=mwi), resource_name=trade)
    assert org["capacity_rollup"] == want
    assert org["segments"][0]["capacity_fixed"] == math.ceil(500 / mwi)
    assert org["segments"][1]["capacity_fixed"] == math.ceil(333 / mwi)
    assert all(s["mwi"] == mwi and s["mwi_unit"] == "m2/人" for s in org["segments"])

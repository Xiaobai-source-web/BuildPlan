# -*- coding: utf-8 -*-
"""WS2 §5 单位贯通：`resource_demand.tasks[*]` 必须带 `unit` 与 `measure_scope`。

依据 `devtools/_dev-notes/终版修改_接口冻结.md` §5 / §11：
  · WBS 叶子 304/304 有 `unit`，而落盘的 demand **0/304 有** —— 交付物里只剩一个
    裸数字（2000 是 m² 还是 t？），下游只能去猜；
  · `unit` 必须经 `kb_units.normalize_unit` 归一（`㎡`(U+33A1) → `m²`）；
  · `measure_scope`（§1 受控词表）说明"这个 m² 是哪张面积"，可为 `''`（未知）；
  · **不许**把单位塞进 `quantity_basis` / `raw_quantity_basis`（历史坑：会让定额值
    被放大 10~1000 倍，见 `devtools/fix_norm_basis.py` 开头注释）。

运行：python -m pytest backend/tests/test_final_ws2_units.py -q
"""

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import kb_units                       # noqa: E402
from pipeline.nodes import resource as R            # noqa: E402


def _binding(**over):
    b = {"mode": "labor", "norm_value": 0.5, "productivity_value": 2.0,
         "unit": "工日/m²", "source_code": "LD_TEST", "match_type": "kb",
         "usable": True, "norm_is_evidence": True, "quantity_basis": 1.0,
         "crew": {"普工": 4}, "crew_kind": {"普工": "labor"},
         "labor_types": ["普工"]}
    b.update(over)
    return b


def _leaf(**over):
    t = {"id": "1.1.1", "name": "场地平整", "quantity": 100.0, "unit": "㎡",
         "duration_days": 5, "kb_activity_id": None,
         "workface_capacity": {"max_labor": 50, "max_machine": None,
                               "unit_basis": "每施工段",
                               "source_type": "ai_estimate", "confidence": "LOW"}}
    t.update(over)
    return t


def _wbs(leaf):
    return {"phases": [{"phase": "施工准备", "work_packages": [
        {"name": "场地准备", "sub_packages": [leaf]}]}]}


def test_demand的unit归一为m2():
    """`㎡`(U+33A1) → `m²`：与 WBS 里的其它 129 条写法对齐（§5）。"""
    d = R.compute_norm_resources(_leaf(), _binding(), 100.0, 5)
    assert d["unit"] == "m²", "㎡ 必须归一成 m²，实际 %r" % d["unit"]
    assert "measure_scope" in d, "即使未知也要有键（未知写 ''）"
    assert d["measure_scope"] == ""


def test_measure_scope的取值优先级与受控词表():
    """§1/§5：binding（WS1 口径关）→ 叶子显式声明；词表外的写法当"未填"，绝不编。"""
    d = R.compute_norm_resources(
        _leaf(), _binding(task_measure_scope="建筑面积"), 100.0, 5)
    assert d["measure_scope"] == "建筑面积", "WS1 口径关算出来的值优先（单一真源）"

    d = R.compute_norm_resources(
        _leaf(measure_scope="楼地面面积"), _binding(), 100.0, 5)
    assert d["measure_scope"] == "楼地面面积"

    d = R.compute_norm_resources(
        _leaf(measure_scope="不知道啥面积"), _binding(), 100.0, 5)
    assert d["measure_scope"] == "", "词表外的写法一律当未填，不许原样下发"

    d = R.compute_norm_resources(
        _leaf(measure_scope="建筑面积"), _binding(task_measure_scope="风管展开面积"),
        100.0, 5)
    assert d["measure_scope"] == "风管展开面积", "binding 里的口径胜出"


def test_单位不许写进quantity_basis():
    """§5 明文禁止：单位只能放在独立键上（`quantity_basis` 是"定额分母的量"）。"""
    b = _binding(quantity_basis=1.0)
    d = R.compute_norm_resources(_leaf(), b, 100.0, 5)
    assert d["_norm_applied"]["quantity_basis"] == 1.0
    assert "raw_quantity_basis" not in d["_norm_applied"]
    assert "m²" not in str(d["_norm_applied"]["quantity_basis"])
    assert "㎡" not in json.dumps(d, ensure_ascii=False), "整行都不许出现 ㎡"


def test_未计算班组的行也带单位与计量对象():
    """早退行（`_norm_flagged` = 未计算班组）同样要有单位 —— 它也是交付物里的一行。"""
    leaf = _leaf(id="9.2.3", name="沥青混凝土路面施工", quantity=2000.0)
    leaf["norm_binding"] = _binding(method_conflict="机械挖基坑土方 vs 人工挖小坑")
    flat = R.compute_flat(_wbs(leaf))
    row = flat["resource_demand"]["tasks"][0]
    assert row.get("_norm_flagged"), row
    assert row["unit"] == "m²"
    assert row["measure_scope"] == ""


def test_量级不可信的行也带单位与计量对象():
    """`_scale_flagged` 行（量级不可信、未给班组）同样带单位（契约 §5 是"每一行"）。"""
    leaf = _leaf(id="1.1.1", name="定位放线", quantity=128000.0, unit="㎡",
                 norm_binding=_binding())
    flat = R.compute_flat(_wbs(leaf), {"total_area": 14200})
    row = flat["resource_demand"]["tasks"][0]
    assert row.get("_scale_flagged"), "这条量级必须被判不可信：%s" % row
    assert row["unit"] == "m²"
    assert row["measure_scope"] == ""


def test_嵌套resources不吞掉unit与measure_scope():
    """`to_nested_resources` 只收 `_per_day`/`_total_days` 键，新键必须原样留在任务上。"""
    flat = R.compute_flat(_wbs(_leaf(norm_binding=_binding())))
    nested = R.to_nested_resources(flat["resource_demand"])
    t = nested["tasks"][0]
    assert t["unit"] == "m²" and t["measure_scope"] == ""
    res = t.get("resources") or {}
    assert "普工" in res, res
    assert "unit" not in res and "measure_scope" not in res
    assert set(res["普工"]) == {"per_day", "total_days"}


def test_活动自身的计量对象在缺列时如实写空():
    """§1/§6 并行：`L4_Activity_Dictionary.measure_scope` 列可能还不存在 → 写 ''，不抛异常。"""
    kb_scope = ""
    try:
        kb_scope = kb_units.normalize_measure_scope(
            R.kb.activity_measure_scope("REBAR_NEW_SLAB"))
    except Exception:                                    # noqa: BLE001
        kb_scope = ""
    leaf = _leaf(kb_activity_id="REBAR_NEW_SLAB", workface_capacity=None)
    d = R.compute_norm_resources(leaf, _binding(), 100.0, 5)
    if kb_scope in kb_units.MEASURE_SCOPES:
        assert d["measure_scope"] == kb_scope
    else:
        assert d["measure_scope"] == "", "KB 列还没迁 → 如实写空，绝不编"

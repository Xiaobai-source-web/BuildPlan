# -*- coding: utf-8 -*-
"""第 2 批 · 域 2 / 2.6 —— `material_summary`（"主要材料"表）**已删除** 的回归护栏。

历史（本文件原来守的是相反行为）：W3-B 时 `plan_assembler` 有一张 `mat_map`，
把 `total_concrete / total_rebar / total_area / total_earthwork / total_formwork /
total_masonry` 六项汇成 `resource_plan.material_summary`，交付物印成「主要材料：…」。

本批裁定把材料计划整体移出本系统（交付物改为声明
「本计划不含材料计划。材料按"管够"处理，不参与工期与资源计算。」），所以：
  ① `build_parts(...)["resource_plan"]` **不再有** `material_summary` 键；
  ② 那些 `total_*` **量本身没有丢** —— 它们仍在 `extracted_params` /
     `meta.extracted_params` 里（工程量口径的唯一真源），删掉的是"展示成材料清单"；
  ③ G5（验收 §6#5）的产物侧要求**照旧**：产物里 0 处 U+33A1、源码里不许有该字面量。

⚠️ 本文件**不**覆盖 `material_transport`（材料运输**工序**）与
`_materialize_unit_assumption`（"落实假设值"，与材料无关）—— 那两样与本批无关，
删了会毁掉一大批工序 / 换算路径。

运行：python -m pytest backend/tests/test_plan_assembler_material_summary.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes import plan_assembler as PA          # noqa: E402

U33A1 = chr(0x33A1)


def _leaf(tid, name, qty=10.0, unit="m³"):
    return {"id": tid, "name": name, "quantity": qty, "unit": unit,
            "duration_days": 2, "work_type": "混凝土工程"}


def _ctx(params):
    leaf = _leaf("1.1.1", "混凝土浇筑")
    return {
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "1.1", "name": "结构", "sub_packages": [leaf]}]}]},
        "cpm_result": {"total_duration_days": 2, "critical_path": ["1.1.1"],
                       "schedule": [{"task_id": "1.1.1", "es": 0, "ef": 1}]},
        "resource_demand": {"tasks": [{"task_id": "1.1.1", "task_name": "混凝土浇筑",
                                       "resources": {}}]},
        "extracted_params": dict(params),
    }


def _rp(params):
    return PA.build_parts(_ctx(params))["resource_plan"]


def test_不再产出material_summary():
    """① `resource_plan` 里**不再有** `material_summary` —— 材料清单不再展示。"""
    rp = _rp({"total_concrete": 52000, "total_rebar": 7500, "total_area": 128000,
              "total_earthwork": 3000, "total_formwork": 25000, "total_masonry": 3000})
    assert "material_summary" not in rp, rp.get("material_summary")
    # 其他关键字段照旧（删的只是材料表，不是整张 resource_plan）
    for k in ("total_manpower_days", "peak_manpower", "equipment_peak",
              "machine_crew_peak", "labor_demand"):
        assert k in rp, (k, sorted(rp))


def test_主要工程量参数本身没丢():
    """② 量仍在 `extracted_params`（工程量口径的唯一真源）—— 删的是展示，不是数据。"""
    params = {"total_concrete": 52000, "total_rebar": 7500, "total_area": 128000,
              "total_earthwork": 3000, "total_formwork": 25000, "total_masonry": 3000,
              "total_infill_wall": 1800, "total_pile": 320}
    ctx = _ctx(params)
    out = PA.assemble_plan_json(ctx, PA.build_parts(ctx), report="")
    got = (out.get("meta") or {}).get("extracted_params") or {}
    for k, v in params.items():
        assert got.get(k) == v, (k, got.get(k), v)


def test_源码里没有mat_map构建():
    """源码级：`mat_map` 那段构建已删除（防止有人在别处把它加回来）。"""
    src = (BACKEND / "pipeline" / "nodes" / "plan_assembler.py").read_text(encoding="utf-8")
    assert "mat_map" not in src, "`mat_map` 已被删除，不该再出现"
    assert 'material_summary.append(' not in src, "material_summary 的构建已被删除"


def test_产物里0处U33A1():
    """③ G5 / 验收 §6#5：产物（plan_json）里 0 处 U+33A1 方块平米符号。"""
    params = {"total_concrete": 52000, "total_area": 128000,
              "total_formwork": 25000, "total_masonry": 3000}
    ctx = _ctx(params)
    out = PA.assemble_plan_json(ctx, PA.build_parts(ctx), report="")
    assert PA.find_cjk_compat_square_metre(out, "plan_json") == [], \
        PA.find_cjk_compat_square_metre(out, "plan_json")


def test_源码里无U33A1字面量():
    """G5 的**源码级**要求：判据一律用 `chr(0x33A1)` 构造，免得字面量被复制传播。"""
    src = (BACKEND / "pipeline" / "nodes" / "plan_assembler.py").read_text(encoding="utf-8")
    assert U33A1 not in src, "plan_assembler.py 源码里不许有 U+33A1 字面量"

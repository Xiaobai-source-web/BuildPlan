# -*- coding: utf-8 -*-
"""交付物「施工段表 + 容量字典 + 取小/回分 + 为什么是 N 人」可见性护栏（E2 / W3-B）。

数据契约由 W2-C 交付（`org_plan.plan_capacity_chain` → 排程行的 `_organization`）：
`segments[]` / `segment_ids` / `segment_areas` / `segment_count` / `capacity_rollup` /
`capacity_effective` / `user_cap` / `user_cap_source` / `allocation.steps[]` /
`basis_lines[]` / `floor_area` / `segment_rule_note`。

交付侧的铁律：**照抄，一个数都不重算、一个字段都不新造**；取不到就不印（不编行）。

运行：python -m pytest tests/test_delivery_segment_capacity.py -q
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import config                                # noqa: E402
from pipeline.nodes import delivery as D                    # noqa: E402
from pipeline.nodes import plan_assembler as PA              # noqa: E402

U33A1 = chr(0x33A1)


@pytest.fixture()
def tmp_deliverables(tmp_path, monkeypatch):
    d = tmp_path / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DELIVERABLES_DIR", d)
    monkeypatch.setattr(D.config, "DELIVERABLES_DIR", d)
    return d


def _org(with_segments=True):
    org = {
        "source": "workface_capacity",
        "resource_name": "瓦工", "resource_kind": "labor",
        "resource_mobility": "fixed",
        "capacity_source": "mwi",
        "demand": 306.0, "person_days": 306.0,
        "duration_days": 6, "feasible": True, "t_min_days": 6,
        "cadence_days": 7.0,
        "floor_area": 833.33,
        "segment_rule_note": "层面积 833.33 m² ÷ MSSA 500 → 500 / 333.33（余量 ≥ 167，先满后余）",
        "basis_lines": ["段容量 = ceil(500 ÷ 40) + ceil(333.33 ÷ 40) = 13 + 9 = 22 人",
                        "有效容量 = min(汇总容量 22 人, 用户同类限额 12 人) = 12 人（取小）",
                        "工期 = ceil(306 ÷ 12) = 26 天"],
        "allocation": {"steps": ["① e_i = 12 × 500/833.33 = 7.2 → p_i = 7",
                                 "② 余额 R = 12 − 12 = 0"],
                       "total": 12},
        "user_cap": 12, "user_cap_source": "用户申报同类限额",
        "capacity_rollup": 22, "capacity_effective": 12, "rollup_kind": "sum",
        "segment_count": 2, "segment_ids": ["Ⅰ段", "Ⅱ段"], "segment_areas": [500.0, 333.33],
    }
    if with_segments:
        org["segments"] = [
            {"segment_id": "Ⅰ段", "segment_area": 500.0, "capacity_fixed": 13,
             "capacity_mobile": 0, "capacity_allocated": 7, "mwi": 40.0, "mwi_unit": "m²/人",
             "segment_demand": 7, "batch": 1},
            {"segment_id": "Ⅱ段", "segment_area": 333.33, "capacity_fixed": 9,
             "capacity_mobile": 0, "capacity_allocated": 5, "mwi": 40.0, "mwi_unit": "m²/人",
             "segment_demand": 5, "batch": 1},
        ]
    return org


def _plan(with_segments=True, with_org=True):
    rd = {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装", "quantity": 1420.0,
          "resources": {"瓦工": {"per_day": 12, "total_days": 312.0}}}
    if with_org:
        rd["_organization"] = _org(with_segments)
    sched = [{"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装",
              "start_date": "2028-10-11", "finish_date": "2028-11-05", "duration_days": 26,
              "assigned_resources": {"瓦工": 12}}]
    return {
        "plan_id": "plan_segment_capacity",
        "overview": {"project_name": "施工段容量测试", "total_duration_days": 26,
                     "planned_start_date": "2028-10-11",
                     "planned_end_date": "2028-11-05", "critical_path_length": 1},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "6.1", "name": "砌体", "sub_packages": [
                {"id": "6.1.1.1", "name": "1-1层 ALC墙板安装", "duration_days": 26,
                 "quantity": 1420.0, "unit": "m²"}]}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 26, "critical_path": ["6.1.1.1"],
                       "schedule": [{"task_id": "6.1.1.1", "es": 0, "ef": 25}]},
        "all_tasks_schedule": sched,
        "critical_path_tasks": [dict(sched[0])],
        "key_milestones": [{"name": "开工", "date": "2028-10-11",
                            "task_id": "6.1.1.1", "description": "开工"}],
        "resource_demand": {"tasks": [rd]},
        "resource_plan": {"total_manpower_days": 312.0, "peak_manpower": 12,
                          "peak_manpower_source": "resource_curve",
                          "curve_peak_manpower": 12, "equipment_peak": {},
                          "machine_crew_peak": {}, "material_summary": []},
        "risks": [],
        "report": "# 报告",
    }


def _html(plan):
    return Path(D.build_plan_html(plan)).read_text(encoding="utf-8")


def _word_parts(plan):
    from docx import Document
    doc = Document(D.build_plan_docx(plan))
    paras = "\n".join(p.text for p in doc.paragraphs)
    grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
    cells = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    return paras, grids, cells


# ══════════════════════ ① 施工段表 + 容量字典 ══════════════════════

def test_看板有施工段表与容量字典(tmp_deliverables):
    h = _html(_plan())
    for kw in ("施工段表", "段号", "段面积(m²)", "固定型(人/台)", "移动型(人/台)",
               "MWI 单位", "段级需求", "批次", "Ⅰ段", "Ⅱ段", "500", "333.33"):
        assert kw in h, "看板缺施工段表要素：%s" % kw
    for kw in ("汇总容量", "用户同类限额", "有效容量", "工期(天)", "瓦工", "12", "22"):
        assert kw in h, "看板缺容量字典要素：%s" % kw


def test_Word有施工段表与容量字典(tmp_deliverables):
    paras, grids, cells = _word_parts(_plan())
    m = D.organization_section_model(_plan(), D._compute_view(_plan()))
    cap = m["capacity"]
    assert cap["present"] is True
    assert list(cap["cap_header"]) in grids, grids
    assert list(cap["seg_header"]) in grids, grids
    assert "施工段表 · 6.1.1.1" in paras
    assert "1-1层 ALC墙板安装" in paras
    for kw in ("汇总容量", "用户同类限额", "有效容量"):
        assert kw in paras or kw in cells or kw in "\n".join(grids[0]), kw


def test_逐段容量字段照抄不重算():
    m = D.organization_section_model(_plan(), D._compute_view(_plan()))
    cap = m["capacity"]
    assert cap["count"] == 1
    row = cap["seg_tables"][0]["rows"][0]
    assert row[0] == "Ⅰ段" and row[1] == "500" and row[2] == "13"
    assert row[4] == "7" and row[5] == "40" and row[6] == "m²/人" and row[8] == "1"


# ══════════════════════ ② 取小 / 回分过程 + 为什么是 N 人 ══════════════════════

def test_取小与回分过程与依据齐备(tmp_deliverables):
    plan = _plan()
    h = _html(plan)
    assert "为什么是 N 人 / N 台" in h
    assert "min(汇总容量 22 人, 用户同类限额 12 人) = 12 人（取小）" in h
    assert "工期 = ceil(306 ÷ 12) = 26 天" in h
    assert "e_i = 12 × 500/833.33 = 7.2" in h, "回分过程必须留痕"
    paras, _grids, _cells = _word_parts(plan)
    assert "为什么是 N 人 / N 台（容量依据）" in paras
    assert "min(汇总容量 22 人, 用户同类限额 12 人) = 12 人（取小）" in paras


def test_分段规则说明照抄():
    m = D.organization_section_model(_plan(), D._compute_view(_plan()))
    notes = "\n".join(m["capacity"]["notes"])
    assert "MSSA 500" in notes and "先满后余" in notes


# ══════════════════════ ③ 取不到就不印（不编行） ══════════════════════

def test_没有segments时不编施工段表(tmp_deliverables):
    plan = _plan(with_segments=False)          # 旧计划 / 非 MWI 链路
    m = D.organization_section_model(plan, D._compute_view(plan))
    cap = m["capacity"]
    assert cap["present"] is False and cap["cap_rows"] == []
    for text in (_html(plan), _word_parts(plan)[0]):
        assert "施工段表" not in text, "没有数据就不许编一张表出来"


def test_没有organization时整节不出(tmp_deliverables):
    plan = _plan(with_org=False)
    m = D.organization_section_model(plan, D._compute_view(plan))
    assert m["capacity"]["present"] is False
    for text in (_html(plan), _word_parts(plan)[0]):
        assert "施工段表" not in text and "有效容量" not in text


# ══════════════════════ ④ facts 与 G5 ══════════════════════

def test_facts带容量表_且不喂超量():
    plan = _plan()
    facts = D._facts_bundle(plan, D._compute_view(plan))
    ct = facts["capacity_table"]
    assert ct["present"] and ct["cap_rows"]
    assert ct["seg_header"] and ct["seg_tables"]
    assert "how_to_write" in ct
    assert len(ct["seg_tables"]) <= 3 and len(ct["basis"]) <= 3


def test_施工段容量段落无U33A1(tmp_deliverables):
    plan = _plan()
    plan["resource_demand"]["tasks"][0]["_organization"]["segments"][0]["mwi_unit"] = "m²/人"
    plan["resource_demand"]["tasks"][0]["_organization"]["basis_lines"] = [
        "段面积 500 " + U33A1 + " ÷ 40 " + U33A1 + "/人 = 13 人"]
    for text in (_html(plan), _word_parts(plan)[0]):
        assert U33A1 not in text, "交付物里不许有 U+33A1（G5）"
    m = D.organization_section_model(plan, D._compute_view(plan))
    assert PA.find_cjk_compat_square_metre(m["capacity"], "capacity") == []

# -*- coding: utf-8 -*-
"""交付物「容量口径两态」可见性护栏（域 1.6 收敛为两态）。

背景（裁定 B / W2-C 第三轮 + 域 1.6）：`scheduler` 的 `_plan_task` 会把容量来源逐行写进
`all_tasks_schedule[*].capacity_source`（两态）+ `capacity_basis`（整段依据）：

  · `"mwi"`                    → 走 MWI 段容量（正常）→ 交付物**一个字都不提**；
  · `"reported_missing"`       → 缺容量数据 → **必须**让用户看到
    「工期不随工程量变化」+ 是哪几条 + 原因（缺容量数据）+ 怎么办。

域 1.6 已删除 Workface_Capacity_Rule 表，旧兜底态已无数据源；新计划只产出上面两态。
旧计划若残留已删表的旧取值，落进「认不出的取值」分支**原样照抄**，不硬套口径。
（实测：现存冻结档案 plan_run_1789827002 / 1789895021 / 1789911477.json 里带
`capacity_source` 的条目 **0 条** —— 该兼容路径没有任何真实产物命中。）

运行：python -m pytest tests/test_delivery_capacity_caliber.py -q
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
from _text_guard import assert_absent, strip_invisible        # noqa: E402

U33A1 = chr(0x33A1)
MISSING_SENTENCE = "工期不随工程量变化"


@pytest.fixture()
def tmp_deliverables(tmp_path, monkeypatch):
    d = tmp_path / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DELIVERABLES_DIR", d)
    monkeypatch.setattr(D.config, "DELIVERABLES_DIR", d)
    return d


def _sch(tid, name, src, basis=""):
    row = {"task_id": tid, "task_name": name, "start_date": "2026-09-01",
           "finish_date": "2026-09-06", "duration_days": 5,
           "assigned_resources": {"钢筋工": 4}}
    if src:
        row["capacity_source"] = src
        row["capacity_basis"] = basis
    return row


def _plan(rows):
    return {
        "plan_id": "plan_capacity_caliber",
        "overview": {"project_name": "容量口径测试", "total_duration_days": 10,
                     "planned_start_date": "2026-09-01",
                     "planned_end_date": "2026-09-10", "critical_path_length": len(rows)},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "1.1", "name": "结构", "sub_packages": [
                {"id": r["task_id"], "name": r["task_name"], "duration_days": 5,
                 "quantity": 10, "unit": "m³", "work_type": "混凝土工程"} for r in rows]}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 10, "critical_path": [rows[0]["task_id"]],
                       "schedule": [{"task_id": r["task_id"], "es": 0, "ef": 5} for r in rows]},
        "resource_demand": {"tasks": [
            {"task_id": r["task_id"], "task_name": r["task_name"],
             "resources": {"钢筋工": {"per_day": 4, "total_days": 20.0}}} for r in rows]},
        "key_milestones": [{"name": "开工", "date": "2026-09-01",
                            "task_id": rows[0]["task_id"], "description": "开工"}],
        "critical_path_tasks": [dict(rows[0])],
        "all_tasks_schedule": [dict(r) for r in rows],
        "resource_plan": {"total_manpower_days": 40.0, "peak_manpower": 8,
                          "peak_manpower_source": "resource_curve",
                          "curve_peak_manpower": 8, "equipment_peak": {},
                          "machine_crew_peak": {}, "material_summary": []},
        "risks": [],
        "report": "# 报告",
    }


def _html_raw(plan):
    """看板 HTML 的**原始全文**（1 MB 级，内嵌 ECharts）—— 慎用。

    对它做 `assert x not in ...`，失败时 pytest 会跑 O(n·m) 的 difflib 失败
    diff（域 9.1）。要断言"用户看到什么"，请用 `_html()`。
    """
    return Path(D.build_plan_html(plan)).read_text(encoding="utf-8")


def _html(plan):
    """看板 HTML 的**用户可见区**（已剥 `<script>` / `<style>`）。

    域 9.1：内嵌的 ECharts 自带 `enableNone` 等标识符 —— 原始 HTML 里恒有 7 处
    字面量 `None`，用户一处也看不见（探针 `_probe_tmp/q_none_in_html.py` 实测：
    script 段 7 处 / 非 script 段 0 处；换一条正常计划仍是 7 处）。
    交付物用例要断言的是"有没有把 Python 的 None 印给用户"，故默认剥脚本。
    """
    return strip_invisible(_html_raw(plan))


def _word_text(plan):
    from docx import Document
    doc = Document(D.build_plan_docx(plan))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for r in t.rows:
            parts.extend(c.text for c in r.cells)
    return "\n".join(parts)


# ══════════════════════ ① reported_missing：必须打那句提示 ══════════════════════

def test_缺容量数据的任务在看板与Word里如实标注(tmp_deliverables):
    rows = [_sch("1.1.1", "ALC墙板安装", D.CAPACITY_SOURCE_MISSING,
                 "⚠ **本次工程量变化未反映到工期（缺容量数据）** —— 改工程量不会…")]
    plan = _plan(rows)
    for text in (_html(plan), _word_text(plan)):
        assert MISSING_SENTENCE in text, text[:400]
        assert "缺容量数据" in text
        assert "1.1.1" in text, "必须让用户看到是哪几条"
        assert "补 MWI 行" in text, "还要给出怎么办"


def test_同一条只列一次且带任务名(tmp_deliverables):
    rows = [_sch("1.1.1", "ALC墙板安装", D.CAPACITY_SOURCE_MISSING, "x"),
            _sch("1.1.2", "砌块墙", D.CAPACITY_SOURCE_MISSING, "y")]
    plan = _plan(rows)
    h = _html(plan)
    assert h.count(MISSING_SENTENCE) == 1, "整段只写一次，不逐条刷屏"
    assert "2 条" in h and "1.1.1" in h and "1.1.2" in h
    # 长文案不照抄：`capacity_basis` 的原文一个字都不进交付物
    assert_absent(h, "⚠ **本次工程量变化未反映到工期")


# ══════════════════════ ② mwi：正常态一个字都不提 ══════════════════════

def test_全mwi时一个字都不提(tmp_deliverables):
    rows = [_sch("1.1.1", "ALC墙板安装", D.CAPACITY_SOURCE_MWI, "正常")]
    plan = _plan(rows)
    for text in (_html(plan), _word_text(plan)):
        assert_absent(text, MISSING_SENTENCE)
        assert_absent(text, "缺容量数据")
        assert_absent(text, "退回 KB 工作面容量口径")
    assert D.capacity_caliber_model(plan)["lines"] == []


def test_混合时只报缺失_不报mwi(tmp_deliverables):
    rows = [_sch("1.1.1", "ALC墙板安装", D.CAPACITY_SOURCE_MWI, ""),
            _sch("1.1.2", "砌块墙", D.CAPACITY_SOURCE_MISSING, ""),
            _sch("1.1.3", "钢筋绑扎", D.CAPACITY_SOURCE_MISSING, "")]
    plan = _plan(rows)
    m = D.capacity_caliber_model(plan)
    # 域 1.6：只有两态，所以这里是两条 missing
    assert (m["missing_count"], m["fallback_count"], m["mwi_count"]) == (2, 0, 1), m
    h = _html(plan)
    assert MISSING_SENTENCE in h
    assert_absent(h.split("容量口径")[1], "1.1.1", what="mwi 那条不许被点名")


# ══════════════════════ ③ 兜底态：如实说明，不许打"不随工程量变化" ══════════════════════

def test_缺容量态如实说明并打缺容量提示(tmp_deliverables):
    """域 1.6 后不再有「兜底态」；缺容量即 `reported_missing`，必须打那句提示。"""
    rows = [_sch("1.1.2", "砌块墙", D.CAPACITY_SOURCE_MISSING, "本次未取到 MWI 段容量…")]
    plan = _plan(rows)
    for text in (_html(plan), _word_text(plan)):
        assert MISSING_SENTENCE in text


# ══════════════════════ ④ 无字段 / 未知取值：不猜 ══════════════════════

def test_没有capacity_source时不编(tmp_deliverables):
    rows = [_sch("1.1.1", "ALC墙板安装", "", "")]
    plan = _plan(rows)
    m = D.capacity_caliber_model(plan)
    assert m["present"] is False and m["lines"] == []
    for text in (_html(plan), _word_text(plan)):
        assert_absent(text, MISSING_SENTENCE)


def test_认不出的取值原样照抄不硬套口径():
    plan = _plan([_sch("1.1.1", "ALC墙板安装", "future_source", "")])
    m = D.capacity_caliber_model(plan)
    assert m["other_count"] == 1
    assert "future_source" in "\n".join(m["lines"])
    assert MISSING_SENTENCE not in "\n".join(m["lines"])


def test_旧产物键缺失或为None时不崩也不显示None字面量(tmp_deliverables):
    """硬要求（实测）：C8 之前的冻结档案里这两个键**不存在或是 None** —— 必须容忍。

    实测交回：`plan_run_1789827002.json` 322 条 / `plan_run_1789895021.json` 304 条 /
    `plan_run_1789911477.json` 503 条，带 `capacity_source` 的条目 **0 条**。
    """
    absent = {"task_id": "1.1.1", "task_name": "旧任务A",          # 键完全不存在
              "start_date": "2026-09-01", "finish_date": "2026-09-06",
              "duration_days": 5, "assigned_resources": {}}
    none_val = dict(absent, task_id="1.1.2", task_name="旧任务B",
                    capacity_source=None, capacity_basis=None)      # 键存在但为 None
    empty_str = dict(absent, task_id="1.1.3", task_name="旧任务C",
                     capacity_source="", capacity_basis="")
    plan = _plan([absent, none_val, empty_str])
    m = D.capacity_caliber_model(plan)
    assert m["present"] is False and m["lines"] == [], m
    for text in (_html(plan), _word_text(plan)):
        assert_absent(text, "None", what="不许把 Python 的 None 字面量印给用户")
        assert_absent(text, MISSING_SENTENCE)
        assert_absent(text, "退回 KB 工作面容量口径")
    # 部分缺失（一条有、两条没有）时也只报有的那条
    mixed = _plan([absent,
                   _sch("1.1.4", "有数据的任务", D.CAPACITY_SOURCE_MISSING, "x")])
    mm = D.capacity_caliber_model(mixed)
    assert mm["missing_count"] == 1 and mm["present"] is True, mm
    h = _html(mixed)
    assert "1.1.4" in h and (assert_absent(h.split("容量口径")[1], "1.1.1") is None)


@pytest.mark.skipif(not (BACKEND / "plans" / "plan_run_1789827002.json").exists(),
                    reason="plans/ 是运行产物，旧冻结档案不在仓库里")
def test_真实旧产物不崩且不显示None(tmp_deliverables):
    """拿真冻结档案跑交付物：3 个状态下都不许崩、不许出现 None 字面量。"""
    import json as _json
    plan = _json.loads((BACKEND / "plans" / "plan_run_1789827002.json").read_text(
        encoding="utf-8"))
    plan["plan_id"] = "zz_capacity_legacy_probe"
    rows = plan.get("all_tasks_schedule") or []
    assert all("capacity_source" not in r for r in rows), "本断言要求它是旧的冻结档案"
    assert D.capacity_caliber_model(plan)["lines"] == []
    for text in (_html(plan), _word_text(plan)):
        assert_absent(text, "None", what="不许把 Python 的 None 字面量印给用户")
        assert_absent(text, MISSING_SENTENCE)


# ══════════════════════ ⑤ G5：这一节也不许带 U+33A1 ══════════════════════

def test_容量口径段落无U33A1(tmp_deliverables):
    rows = [_sch("1.1.1", "ALC墙板安装", D.CAPACITY_SOURCE_MISSING,
                 "面积 1420 " + U33A1 + " 的描述"),
            _sch("1.1.2", "砌块墙", D.CAPACITY_SOURCE_MISSING, "")]
    plan = _plan(rows)
    for text in (_html(plan), _word_text(plan)):
        assert_absent(text, U33A1, what="交付物里不许有 U+33A1")
    m = D.capacity_caliber_model(plan)
    assert U33A1 not in "\n".join(m["lines"])
    assert PA.find_cjk_compat_square_metre(m, "capacity_caliber") == []


def test_facts只喂分流口径不喂长文():
    plan = _plan([_sch("1.1.1", "ALC墙板安装", D.CAPACITY_SOURCE_MISSING, "很长的上游依据")])
    facts = D._facts_bundle(plan, D._compute_view(plan))
    cc = facts["capacity_caliber"]
    assert cc["missing_count"] == 1 and cc["mwi_count"] == 0
    assert "how_to_write" in cc
    assert "很长的上游依据" not in str(cc), "facts 只给分流口径，不照抄上游长文"

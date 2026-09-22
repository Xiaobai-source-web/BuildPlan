"""交付节点测试 —— plan_json → Word / HTML。

运行：python -m pytest backend/tests/test_delivery.py -v
"""

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import schemas
from pipeline.nodes.delivery import (
    HtmlPageNode, WordExportNode, build_plan_docx, build_plan_html,
)

# 交付物目录的隔离由 tests/conftest.py 全局负责（把 DELIVERABLES_DIR 与 PLANS_DIR
# 都指向临时目录），此处不再重复实现。

MINI = schemas.PlanJson(
    plan_id="plan_delivery_test",
    overview={"project_name": "交付测试项目", "total_duration_days": 30,
              "planned_start_date": "2026-09-01", "planned_end_date": "2026-10-01",
              "critical_path_length": 1},
    wbs={"phases": [{"phase": "主体", "work_packages": [{"id": "1.1", "name": "结构",
        "sub_packages": [{"id": "1.1.1", "name": "柱浇筑", "duration_days": 5,
        "quantity": 10, "unit": "m³", "work_type": "混凝土工程"}]}]}]},
    dependencies=[],
    cpm_result={"total_duration_days": 30, "critical_path": ["1.1.1"],
                "schedule": [{"task_id": "1.1.1", "es": 0, "ef": 5, "ls": 0, "lf": 5}]},
    resource_demand={"tasks": []},
    key_milestones=[{"name": "开工", "date": "2026-09-01", "task_id": "1.1.1", "description": "开工"}],
    critical_path_tasks=[{"task_id": "1.1.1", "task_name": "柱浇筑", "start_date": "2026-09-01",
                          "finish_date": "2026-09-06", "duration_days": 5,
                          "assigned_resources": {"钢筋工": 4}}],
    all_tasks_schedule=[{"task_id": "1.1.1", "task_name": "柱浇筑", "start_date": "2026-09-01",
                         "finish_date": "2026-09-06", "duration_days": 5,
                         "assigned_resources": {"钢筋工": 4}}],
    resource_plan={"total_manpower_days": 20, "peak_manpower": 4,
                   "equipment_peak": {"泵车": 1},
                   "material_summary": [{"name": "concrete", "total_quantity": 100, "unit": "m³"}]},
    risks=[{"risk_name": "雨季", "mitigation": "排水"}],
    report="# 监督报告",
).model_dump()


def test_docx_generated_and_reopenable():
    p = build_plan_docx(MINI)
    assert p.endswith(".docx") and os.path.exists(p)
    from docx import Document
    doc = Document(p)                      # 能重新打开 = 合法 docx
    assert any("交付测试项目" in para.text for para in doc.paragraphs)


def test_html_generated_self_contained():
    p = build_plan_html(MINI)
    html = open(p, encoding="utf-8").read()
    assert "svg" in html                       # SVG 甘特/曲线
    assert "甘特" in html and "人员配置" in html
    assert "交付测试项目" in html
    assert html.startswith("<!DOCTYPE html>")


def test_word_node_sets_artifacts():
    node = WordExportNode()
    node._emit = lambda e, d: None
    out = node.run({"plan_json": MINI})
    assert out["artifacts"]["docx"].endswith(".docx")


def test_html_node_sets_artifacts():
    node = HtmlPageNode()
    node._emit = lambda e, d: None
    out = node.run({"plan_json": MINI})
    assert out["artifacts"]["html"].endswith(".html")


def test_nodes_degrade_when_no_plan():
    a = WordExportNode(); a._emit = lambda e, d: None
    assert "_stop" in a.run({"plan_json": None})
    b = HtmlPageNode(); b._emit = lambda e, d: None
    assert "_stop" in b.run({"plan_json": None})


def test_html_shows_machine_crew_and_labor_demand():
    """口径修正的交付侧护栏：机械配员单列、「台班定额人工需求」必须上看板。

    背景：过去 `resource_plan.equipment_peak` 混进 `泵工/辅助/操作工/司机`（人），
    而 `meta.machine_labor_demand` 里已算好的「混凝土工 2633.6 工日」没进
    resource_plan → 看板上「混凝土工」完全消失。这里从**看板产物**上把它钉住。
    """
    plan = dict(MINI)
    plan["resource_plan"] = dict(
        MINI["resource_plan"],
        equipment_peak={"混凝土输送泵车": 17},
        machine_crew_peak={"泵工": 17, "辅助": 17, "操作工": 15, "司机": 1},
        labor_demand={"混凝土工": 2633.601925925928},
    )
    html = open(build_plan_html(plan), encoding="utf-8").read()
    assert "机械配员峰值" in html
    assert "泵工 17人" in html and "操作工 15人" in html
    assert "台班定额人工需求" in html
    assert "混凝土工 2633.6 工日" in html


# ============================================================
# WS5 数据来源与置信度 —— 交付物护栏
# ============================================================
# 上面 MINI 刻意**不带** meta.* 置信度字段：不是每份计划都有，缺了就不许出现空章节。
# 这里另起一份带全字段的计划，专门钉新章节与两个峰值口径。

_TMP_WBS_LEAVES = [
    {"id": "1.1.1", "name": "支护桩施工", "quantity": 12, "unit": "根",
     "work_type": "桩基工程",
     "norm_binding": {"task_id": "1.1.1", "mode": "labor", "unit": "工日/m³",
                      "source_code": "GD_2018_A1_1", "match_type": "exact"}},
    {"id": "1.1.2", "name": "ALC墙板安装", "quantity": 200, "unit": "m²",
     "work_type": "砌筑工程",
     "norm_binding": {"task_id": "1.1.2", "mode": "labor", "unit": "工日/m³",
                      "source_code": "LD_T72_2_2008", "match_type": "ai"}},
]

_CONF_META = {
    "norm_coverage": {"total": 307, "bound": 155, "bound_pct": 50.5,
                      "unbound": 152, "unbound_pct": 49.5,
                      "by_reason": {"AI估算定额": 125, "单位不一致": 24, "定额口径不符": 3},
                      "by_reason_pct": {"AI估算定额": 40.7, "单位不一致": 7.8,
                                        "定额口径不符": 1.0}},
    "credibility": {"user": 0.0, "kb": 0.28, "ai": 0.72},
    "data_sources": ["AI_ESTIMATE_V1", "GD_2018_A1_3", "LD_T72_2_2008"],
    "kb_warnings": ["钢筋工（R1）没有该工种值的映射数据，已标注为 无结构约束"],
    "schedule_versions": {"theory_min_days": 677, "resource_ok_days": 847,
                          "delta_days": 170},
    "boundary_conditions": {"labor": {"peak_total": 3},
                            "project_duration_days": 420,
                            # E1（2026-09-21 裁定）：交付物只在目标工期**来源 = 用户**时
                            # 才展示它；这个 fixture 的目标工期是用户给的，所以要带来源键，
                            # 否则它会被当成"分不出谁给的"而不展示。
                            "_source": {"project_duration_days": "user",
                                        "labor.peak_total": "user"}},
}

# 资源曲线峰值由 all_tasks_schedule 逐日累加得到：两条 5 天任务、各 4 人 → 8 人，
# 与 resource_plan.peak_manpower（= 用户限额 5）**不是同一个数**。
_CONF_PLAN = dict(
    MINI,
    plan_id="plan_confidence_test",
    wbs={"phases": [{"phase": "主体", "work_packages": [
        {"id": "1.1", "name": "结构与围护", "sub_packages": _TMP_WBS_LEAVES}]}]},
    all_tasks_schedule=[
        {"task_id": "1.1.1", "task_name": "支护桩施工", "start_date": "2026-09-01",
         "finish_date": "2026-09-06", "duration_days": 5,
         "assigned_resources": {"钢筋工": 4}},
        {"task_id": "1.1.2", "task_name": "ALC墙板安装", "start_date": "2026-09-01",
         "finish_date": "2026-09-06", "duration_days": 5,
         "assigned_resources": {"木工": 4}},
    ],
    critical_path_tasks=[],
    resource_plan={"total_manpower_days": 40, "peak_manpower": 3,
                   "equipment_peak": {"履带式起重机": 2}},
    meta=dict(_CONF_META, audit_status="未审计",
              kb_scope_conformance=True),
)
# 注：**别**往这份 fixture 的 meta 里塞 `display_granularity`（字符串）：
# `_display_granularity()` 期待 `{depth: ...}` 形，喂字符串会 AttributeError。
# 那是别人的读取路径，本文件不去动它，测试绕开即可。


def _conf_doc():
    from docx import Document
    return Document(build_plan_docx(_CONF_PLAN))


def _all_text(doc):
    """段落 + 所有表格单元格文本，拼成一个大字符串。"""
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for r in t.rows:
            parts.extend(c.text for c in r.cells)
    return "\n".join(parts)


def test_docx_has_data_source_confidence_section():
    """交付物必须带「数据来源与置信度」，且五项要点都在。"""
    doc = _conf_doc()
    text = _all_text(doc)
    assert "数据来源与置信度" in text
    # 章节标题是 Heading 2：目录（audit_gate.draft_outline_payload）只镜像 Heading 1，
    # 用二级标题才不会让别人的目录守卫 test_d3 与真产物错位。这条断言就是那道锁。
    titles = [(p.style.name, p.text) for p in doc.paragraphs
              if (p.style.name or "").startswith("Heading")]
    assert ("Heading 2", "数据来源与置信度") in titles
    for sub in ("1. 定额覆盖率", "2. 来源构成", "3. 两版工期与用户目标",
                "4. 人工 / 机械峰值口径", "5. 单位与定额降级清单"):
        assert sub in text, sub
    # 覆盖率与来源构成（比例 0.28 → 28.0%，不是 0.3% 也不是 28）
    assert "50.5%" in text and "155" in text and "152" in text
    assert "28.0%" in text and "72.0%" in text and "0.0%" in text
    # 来源代码翻成人话，不是把 GD_2018_A1_3 这种内部代号直接甩给用户
    assert "国标定额" in text and "地方定额" in text and "AI 经验估算" in text
    # 两版工期 + 与用户目标的差额（847 - 420 = 427）
    assert "677" in text and "847" in text and "420" in text and "427" in text
    assert "用户目标工期（来源：用户）" in text, "用户给的才展示、并标清来源 = 用户"
    # 未绑定原因逐条
    assert "AI估算定额" in text and "40.7%" in text
    # 降级清单只列不一致的那条，一致的那条不许误伤
    assert "支护桩施工" in text and "ALC墙板安装" in text
    assert "共 2 条" not in text          # 两条都降级 = 判据写错
    # kb_warnings 只出**数量**，不把日志原文抄进交付物
    assert "知识库未映射工种：1 个" in text
    assert "没有该工种值的映射数据" not in text


def test_模型补的目标工期不展示_用户给的才展示():
    """E1（用户 2026-09-21 裁定）：**模型补的**目标工期（病根 3 里的 450 天）一律不展示。

    判据只看来源键：`boundary_conditions._source.project_duration_days`。
    · `"user"` → 展示，并标清「（来源：用户）」；
    · `"model"` / 缺失 → **不展示**（取不到来源 = 分不出是不是用户给的 → 按"不是用户给的"处理）。
    """
    import copy as _copy
    from docx import Document
    from pipeline.nodes import delivery as _D

    def _doc_text(plan):
        doc = Document(_D.build_plan_docx(plan))
        return "\n".join(p.text for p in doc.paragraphs)

    model_plan = _copy.deepcopy(_CONF_PLAN)
    model_plan["meta"]["boundary_conditions"]["_source"]["project_duration_days"] = "model"
    text = _doc_text(model_plan)
    assert "677" in text and "847" in text, "两版工期与目标工期无关，必须照旧展示"
    assert "用户目标工期" not in text, "模型补的目标工期一个字都不许出现"
    assert "420" not in text and "427" not in text, "连它的差额也不许算出来给用户看"

    none_plan = _copy.deepcopy(_CONF_PLAN)
    none_plan["meta"]["boundary_conditions"]["_source"].pop("project_duration_days")
    assert "用户目标工期" not in _doc_text(none_plan), "来源缺失 → 不展示（不猜）"

def test_docx_unifies_the_two_peak_numbers():
    """同一个词「峰值」不许指两个数：两处都要写出口径名。

    历史缺陷：总览「人工峰值」= resource_plan.peak_manpower（用户限额 5），
    四、人员配置「峰值总人数」= 逐日曲线峰值 8 —— 同一份文档自相矛盾。

    第 43 轮分类口径修正后，曲线峰值才是本 fixture 注释里写的那个 **8**（两条 5 天
    任务、各 4 人、日期重叠 → 逐日累加 8）：旧分类表（`delivery.LABOR` /
    `plan_assembler.LABOR_NAMES`）漏了「木工/砌筑工」，于是挂「木工 4 人」的那条被
    当成机械、不进人工曲线，峰值只剩 4。补全分类后回到人工侧 = 4 + 4 = 8。
    （旧断言写死 4，钉住的就是这个分类缺陷。）
    """
    doc = _conf_doc()
    text = _all_text(doc)
    assert "峰值总人数 · 资源曲线口径（逐日累加，含机械配员）" in text
    assert "\n8 人\n" in text
    assert "用户限额口径 3 人" in text
    assert "人工峰值（口径见「四、人员配置」）" in text
    assert "峰值总人数 · 资源曲线口径" in text
    # 口径说明段必须点明"两者不是同一个数"
    assert "不是同一个数" in text


def test_docx_has_no_markdown_leak():
    """markdown 泄漏护栏：管道符表格必须变成真 Word 表格。

    历史缺陷：`add_md_lines` 只认行首 `#`，`| a | b |` 与 `| :--- |` 原样落进
    Word 正文，交付物里 12 个段落带 `|`。
    """
    doc = _conf_doc()
    for p in doc.paragraphs:
        assert "|" not in p.text, p.text
    text = _all_text(doc)
    assert "|:--|" not in text and "|---|" not in text
    assert "<br>" not in text
    doc2 = _conf_doc()
    assert not [p.text for p in doc2.paragraphs if "|" in p.text]


def test_docx_skips_confidence_section_when_meta_absent():
    """没有置信度元数据 → 连标题都不许出现（整段优雅降级，不留空壳）。"""
    plain = dict(MINI, meta={"audit_status": "未审计"})
    from docx import Document
    doc = Document(build_plan_docx(plain))
    text = _all_text(doc)
    assert "数据来源与置信度" not in text


def test_kb_warning_phrasing_is_matched_loosely():
    """kb_warnings 措辞会漂移，按最小稳定子串数，别按整句匹配。

    实测原文：`天棚工程（ceiling）：结构映射表中没有该工种的映射数据，已保留其全部 L4…`
    曾经按「没有该工种值的映射数据」整句匹配 → 26 条警告数出 0 个（细节见
    `test_docx_has_data_source_confidence_section` 的对照）。
    """
    from pipeline.nodes.delivery import _kb_unmapped_work_types
    live = "天棚工程（ceiling）：结构映射表中没有该工种的映射数据，已保留其全部 L4（无结构约束）。"
    other = "钢筋工（R1）没有该工种值的映射数据，已标注为 无结构约束"
    assert _kb_unmapped_work_types({"kb_warnings": [live, other]}) == 2
    assert _kb_unmapped_work_types({"kb_warnings": ["某工种未映射到定额"]}) == 1
    # 不相关警告不许被数进来
    assert _kb_unmapped_work_types({"kb_warnings": ["单位不一致，已跳过换算"]}) == 0
    assert _kb_unmapped_work_types({}) == 0
    assert _kb_unmapped_work_types({"kb_warnings": None}) == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n全部 {len(tests)} 个 delivery 用例通过 ✔")
# -*- coding: utf-8 -*-
"""逐条工序表的「工期一致性」与「依据 / 资源」列 —— 交付物护栏。

对应用户实测提的两处缺陷：
1. **同一行里"工期"和起止日期自相矛盾**：ALC 行日期跨 31 天、"工期"列却印 3
   （WBS 里模型写的目标天数）。行里既有日期又有另一个数，用户只会读成"编的"。
   → 工期列 = **排程跨度**（起止日期含首尾，`ef-es+1`）；WBS 目标另起一列。
2. **逐条工序表看不到"依据"**：一条任务有没有定额依据、班组从哪来、是不是
   "没算出来只沿用了估算工期"、单位是不是按假定换算的 —— 全都看不见
   （用户原话："为什么不给班组，还能算出工日，这不是编的吗"）。
   → 新增「依据 / 资源」列，数据只取自 `resource_demand.tasks[*]`（拿不到写"来源未记录"，不许猜）。

运行：python -m pytest backend/tests/test_delivery_evidence_columns.py -v
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline.nodes import delivery as D          # noqa: E402
from pipeline.nodes import plan_assembler as PA   # noqa: E402


def _plan():
    """一份最小计划：ALC 任务**日期跨 31 天、WBS 目标 3 天**（就是用户实测那一行）。

    · 6.1.1.1：`_unit_assumed`（算了，但换算用了写明的 AI 假定）+ 班组瓦工 9 人；
    · 8.1.1.1：`_norm_flagged`（旧政策的 AI 拦截原文，政策变更 2026-09-20 后 AI 经验
      估算定额照用 → 该行必须标注「AI 经验估算定额（无规范依据，待审）」）；
    · 9.9.7：单位不可换算（**真降级**）→ 仍写「无可用定额…工期沿用 WBS 估算，未计算班组」；
    · 5.1.1.3：普通定额路径（有 resources + `_resource_source`）。
    """
    wbs_leaves = [
        {"id": "6.1.1.1", "name": "1-1层 ALC墙板安装", "duration_days": 3,
         "quantity": 1420.0, "unit": "m²", "work_type": "砌筑工程"},
        {"id": "8.1.1.1", "name": "1-3层 内墙抹灰", "duration_days": 11,
         "quantity": 5207.0, "unit": "m²", "work_type": "装饰装修"},
        {"id": "9.9.7", "name": "截（凿）桩头（单位不可换算）", "duration_days": 5,
         "quantity": 120.0, "unit": "根", "work_type": "桩基工程",
         "norm_binding": {"unit": "工日/m³", "source_code": "LN_001"}},
        {"id": "5.1.1.3", "name": "1-1层 叠合板吊装", "duration_days": 4,
         "quantity": 800.0, "unit": "m²", "work_type": "装配式"},
    ]
    sched = [
        {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装",
         "start_date": "2028-10-11", "finish_date": "2028-11-10", "duration_days": 3,
         "assigned_resources": {"瓦工": 9}},
        {"task_id": "8.1.1.1", "task_name": "1-3层 内墙抹灰",
         "start_date": "2028-10-11", "finish_date": "2028-10-21", "duration_days": 11,
         "assigned_resources": {"塔吊": 1, "司机": 1, "信号工": 1}},
        {"task_id": "9.9.7", "task_name": "截（凿）桩头（单位不可换算）",
         "start_date": "2028-10-11", "finish_date": "2028-10-15", "duration_days": 5,
         "assigned_resources": {}},
        {"task_id": "5.1.1.3", "task_name": "1-1层 叠合板吊装",
         "start_date": "2028-11-01", "finish_date": "2028-11-04", "duration_days": 4,
         "assigned_resources": {"塔吊": 1, "司机": 1, "信号工": 1}},
    ]
    rd_tasks = [
        {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装", "quantity": 1420.0,
         "planned_duration_days": 31,
         "resources": {"瓦工": {"per_day": 9, "total_days": 279.0}},
         "_unit_assumed": "按 AI 假定墙厚 200mm 换算：1420 m² × 0.2 m = 284 m³；"
                          "定额档位 LN_781（0.943 工日/单位）",
         # P3/D5 换算参数来源留痕键：AI 猜的 → 文案必须写「AI估算换算参数」
         "ctx_source": "ai_estimate",
         "_resource_source": {"瓦工": {"origin": "kb", "ref": "LN_781"}},
         "_norm_applied": {"mode": "labor", "source_code": "LN_781"}},
        # ⚠ 政策变更（2026-09-20）：`_norm_flagged`/`_warning` 这两行是**旧计划落盘
        # 数据的原样复刻**（旧口径的绑定层原文）。保留它正是为了让"交付物不许复述旧口径"
        # 这条回归有真实输入可测：渲染必须换成 AI_NORM_LABEL。
        {"task_id": "8.1.1.1", "task_name": "1-3层 内墙抹灰", "quantity": 5207.0,
         "planned_duration_days": 11,
         "resources": {"塔吊": {"per_day": 1, "total_days": 11.0},
                       "司机": {"per_day": 1, "total_days": 11.0},
                       "信号工": {"per_day": 1, "total_days": 11.0}},
         "_norm_flagged": "AI 估算定额（只作参考，不用来算班组）",
         "_warning": "定额不可作证据，未计算班组：AI 估算定额（只作参考，不用来算班组）",
         "_site_equipment": [{"name": "塔吊", "quantity": 1, "unit": "台",
                              "quantity_source": "ai_default", "crew": {"司机": 1, "信号工": 1},
                              "crew_composition": "司机1名+信号工1名", "crew_source": "kb",
                              "note": "垂直运输设备：塔吊 1 台（AI 默认口径：用户未申报台数）"}],
         "_resource_source": {"塔吊": {"origin": "ai_estimate", "ref": "AI_ESTIMATE_V1"}}},
        # 真降级：单位不可换算（**不是** AI 来源）→ 「⚠ 无可用定额…未计算班组」一个字不改。
        {"task_id": "9.9.7", "task_name": "截（凿）桩头（单位不可换算）", "quantity": 120.0,
         "planned_duration_days": 5, "resources": {},
         "_norm_flagged": "单位不可用：单位不一致且不可换算：任务「根」（count:根） vs "
                          "定额分母「m³」（volume）；缺换算参数 volume_per_pile_m3"},
        {"task_id": "5.1.1.3", "task_name": "1-1层 叠合板吊装", "quantity": 800.0,
         "planned_duration_days": 4,
         "resources": {"装配式安装工": {"per_day": 6, "total_days": 24.0},
                       "塔吊": {"per_day": 1, "total_days": 4.0}},
         "_crew": {"装配式安装工": 6},
         "_resource_source": {"装配式安装工": {"origin": "kb", "ref": "LD_T72_7_2008"},
                              "塔吊": {"origin": "user", "ref": "boundary.equipment"}}},
    ]
    return {
        "plan_id": "plan_evidence_columns_test",
        "overview": {"project_name": "依据列测试", "total_duration_days": 60,
                     "planned_start_date": "2028-10-11", "planned_end_date": "2028-12-10",
                     "critical_path_length": 2},
        "wbs": {"phases": [{"phase": "装饰", "work_packages": [
            {"id": "6.1", "name": "砌体", "sub_packages": wbs_leaves}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 60, "critical_path": ["6.1.1.1"],
                       "schedule": [{"task_id": "6.1.1.1", "es": 0, "ef": 30},
                                    {"task_id": "8.1.1.1", "es": 0, "ef": 10},
                                    {"task_id": "5.1.1.3", "es": 21, "ef": 24}]},
        "all_tasks_schedule": sched,
        "critical_path_tasks": [dict(sched[0])],
        "key_milestones": [{"name": "开工", "date": "2028-10-11", "task_id": "6.1.1.1",
                            "description": "开工"}],
        "resource_demand": {"tasks": rd_tasks, "_site_level_equipment": {
            "machines": {"塔吊": {"quantity": 1, "unit": "台",
                                  "quantity_source": "ai_default",
                                  "crew": {"司机": 1, "信号工": 1},
                                  "crew_composition": "司机1名+信号工1名",
                                  "crew_source": "kb", "crew_ref": "user_directive",
                                  "norm_source": "AI_ESTIMATE_V1",
                                  "caliber": "site_level_max", "hit_tasks": 2,
                                  "note": "垂直运输设备：塔吊 1 台（AI 默认口径：用户未申报台数）"}},
            "tasks": [{"task_id": "8.1.1.1", "task_name": "1-3层 内墙抹灰", "equipment": ["塔吊"]},
                      {"task_id": "5.1.1.3", "task_name": "1-1层 叠合板吊装", "equipment": ["塔吊"]}],
            "count": 2, "norm_source": "AI_ESTIMATE_V1",
            "caliber": "场地级常驻设备：逐日曲线取 max", "note": "不引用任何规范台班"}},
        "resource_plan": {"total_manpower_days": 303.0, "peak_manpower": 9,
                          "peak_manpower_source": "resource_curve",
                          "curve_peak_manpower": 9,
                          "equipment_peak": {"塔吊": 1},
                          "machine_crew_peak": {"司机": 1, "信号工": 1},
                          "material_summary": []},
        "meta": {"audit_status": "未审计"},
        "report": "# 报告",
    }


def _html():
    return Path(D.build_plan_html(_plan())).read_text(encoding="utf-8")


def _rows(html, keyword):
    import re
    out = []
    for r in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        if keyword in r:
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            out.append([c for c in cells])
    return out


def _evidence_row_text(html, keyword):
    """取含 keyword 的**进度计划表行**（依据 / 资源 列所在行）。

    政策变更（2026-09-20）让「AI 经验估算定额」行也进了置信度章节：置信度卡里的
    §5 降级清单 / 5b 假定清单是 3 列表格，同一任务名会**先于**进度计划表出现，
    所以不能再拿 `_rows(...)[0]` 当依据行 —— 按列数（≥5 列）挑出真正的进度表行。
    """
    rows = [r for r in _rows(html, keyword) if len(r) >= 5]
    assert rows, "进度计划表里必须有 %s 行，实际命中：%s" % (keyword, _rows(html, keyword))
    return " ".join(rows[0])


# ---------------- 修复 1：工期列 = 排程跨度 ----------------

def test_wbs表工期列是排程跨度_wbs目标另起一列():
    """ALC 日期跨 31 天 → 「工期(天·排程)」必须是 31；WBS 目标 3 在**单独一列**里。

    两张表都要查：关键路径明细（带日期）与 WBS 汇总（不带日期，9 列）。
    """
    rows = _rows(_html(), "ALC墙板安装")
    assert rows, "ALC 行必须出现在看板表格里"
    dated = [r for r in rows if any(c.startswith("2028-") for c in r)]
    assert dated, "ALC 行必须出现在关键路径明细（带起止日期）里：%s" % rows[:1]
    row = dated[0]
    assert "2028-10-11" in row and "2028-11-10" in row
    assert "31" in row, "工期列必须等于起止日期跨度（含首尾）= 31，实际：%s" % row
    assert "3" in row, "WBS 目标天数必须也在（单独一列）"
    i31, i3 = row.index("31"), row.index("3")
    assert i31 < i3, "排程跨度列必须在 WBS 目标列之前：%s" % row
    # WBS 汇总表（9 列：阶段/工作包/ID/任务/工期(排程)/WBS 目标/工程量/单位/依据）
    wbs_rows = [r for r in rows if len(r) == 9]
    assert wbs_rows, "WBS 汇总表里也要有 ALC 行：%s" % [len(r) for r in rows]
    wr = wbs_rows[0]
    assert wr[4] == "31", "WBS 汇总表的工期列同样必须是排程跨度 31，实际 %s" % wr
    assert wr[5] == "3", "WBS 汇总表的 WBS 目标列必须是 3，实际 %s" % wr


def test_看板表头写明两列口径():
    h = _html()
    assert "工期(天·排程)" in h
    assert "WBS 目标(天)" in h


def test_word进度计划表工期列是排程跨度():
    from docx import Document
    doc = Document(D.build_plan_docx(_plan()))
    tables = []
    for t in doc.tables:
        head = [c.text for c in t.rows[0].cells]
        if "工期(天·排程)" in head:
            tables.append(t)
    assert tables, "Word 进度计划表必须带「工期(天·排程)」列"
    t = tables[0]
    head = [c.text for c in t.rows[0].cells]
    i_dur, i_wbs = head.index("工期(天·排程)"), head.index("WBS 目标(天)")
    hit = None
    for r in t.rows[1:]:
        if "ALC" in r.cells[1].text:
            hit = [c.text for c in r.cells]
            break
    assert hit, "Word 表里要有 ALC 行"
    # es=0, ef=30 → 相对天数 0..30 → 含首尾 31 天；WBS 目标 3
    assert hit[i_dur] == "31", "Word 工期列必须是排程跨度 31，实际 %s（整行 %s）" % (hit[i_dur], hit)
    assert hit[i_wbs] == "3", "Word 的 WBS 目标列必须是 3，实际 %s" % hit[i_wbs]


# ---------------- 修复 2：依据 / 资源 列 ----------------

def test_依据列_单位假定与班组同时可见():
    row = _evidence_row_text(_html(), "ALC墙板安装")
    assert "单位换算按AI估算换算参数" in row, row[:300]
    assert "墙厚 200mm" in row
    assert "班组 瓦工 9 人" in row, "假定换算的同时必须显示班组/工日：%s" % row[-300:]
    assert "定额 LN_781" in row


def test_依据列_AI经验估算定额逐条标注():
    """政策变更（2026-09-20）：AI 经验估算定额照用，但必须逐条标注。

    8.1.1.1 的 `_norm_flagged` 是旧政策的拦截原文（AI 估算定额只作参考…）。
    新政策下这一行的依据来源就是 AI 经验估算定额 —— 列里出现的必须是政策文案，
    且**不许**把它当成"算了班组"（这一回它确实没按该定额算班组，如实写出来）。
    """
    row = _evidence_row_text(_html(), "内墙抹灰")
    assert "AI 经验估算定额（无规范依据，待审）" in row, row[:300]
    assert "本计划未按该定额计算班组" in row and "工期沿用 WBS 估算" in row
    # 旧政策那句从今天起是假话，交付物里一个字都不许有
    assert "只作参考，不用来算班组" not in row, row[:300]
    # 场地级设备仍如实列出（标了 _norm_flagged ≠ 没有垂直运输需求）
    assert "塔吊 1 台/日" in row and "场地级" in row


def test_依据列_真降级仍写未计算班组():
    """真降级（单位不可换算 / 人工否决 / 无定额绑定）的措辞**一个字没删**。"""
    row = _evidence_row_text(_html(), "单位不可换算")
    assert "⚠ 无可用定额" in row, row[:300]
    assert "未计算班组" in row
    assert "工期沿用 WBS 估算" in row
    assert "AI 经验估算定额" not in row, "真降级不是 AI 来源，不许给它贴 AI 标注"


def test_依据列_定额来源与班组来自_resource_source():
    row = _evidence_row_text(_html(), "叠合板吊装")
    assert "定额 LD_T72_7_2008" in row or "LD_T72_7_2008" in row, row[:300]
    assert "装配式安装工 6 人" in row


def test_依据列_拿不到来源就写来源未记录():
    plan = _plan()
    plan["resource_demand"]["tasks"].append(
        {"task_id": "9.9.9", "task_name": "无记录任务", "quantity": 1,
         "planned_duration_days": 1, "resources": {"普工": {"per_day": 2, "total_days": 2}}})
    plan["all_tasks_schedule"].append(
        {"task_id": "9.9.9", "task_name": "无记录任务", "start_date": "2028-10-11",
         "finish_date": "2028-10-11", "duration_days": 1,
         "assigned_resources": {"普工": 2}})
    plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"].append(
        {"id": "9.9.9", "name": "无记录任务", "duration_days": 1, "quantity": 1,
         "unit": "项", "work_type": "其他"})
    h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
    row = _evidence_row_text(h, "无记录任务")
    assert "来源未记录" in row


def test_word进度计划表也有依据列():
    from docx import Document
    doc = Document(D.build_plan_docx(_plan()))
    t = [t for t in doc.tables if "依据 / 资源" in [c.text for c in t.rows[0].cells]]
    assert t, "Word 进度计划表必须带「依据 / 资源」列"
    cells = "\n".join(c.text for r in t[0].rows for c in r.cells)
    assert "单位换算按AI估算换算参数" in cells
    assert "未计算班组" in cells
    assert "无可用定额" in cells


# ---------------- 侧栏：信号工是人不是机械 + 场地级 max 口径 ----------------

def test_信号工是机械配员不是设备():
    from pipeline.nodes import delivery as _d
    assert _d._is_labor("信号工") and _d._is_labor("木工") and _d._is_labor("砌筑工")
    assert not _d._is_labor("塔吊")
    h = _html()
    assert "机械配员峰值" in h and "信号工 1人" in h
    # 「主要机械峰值」行里只能有真设备
    import re
    m = re.search(r"主要机械峰值：</b>([^<]*)", h)
    assert m, h[:200]
    assert "信号工" not in m.group(1) and "司机" not in m.group(1)
    assert "塔吊 1台" in m.group(1)


def test_场地级设备逐日取max_任务级仍求和():
    """同一天 3 条任务都需要塔吊 → 曲线 1 台；3 条任务各有 1 台泵车 → 曲线 3 台。"""
    rd = {"tasks": []}
    for i, tid in enumerate(("T1", "T2", "T3")):
        rd["tasks"].append({
            "task_id": tid,
            "resources": {"塔吊": {"per_day": 1, "total_days": 5},
                          "司机": {"per_day": 1, "total_days": 5},
                          "信号工": {"per_day": 1, "total_days": 5},
                          "混凝土输送泵车": {"per_day": 1, "total_days": 5},
                          "泵工": {"per_day": 1, "total_days": 5}},
            "_site_equipment": [{"name": "塔吊", "quantity": 1, "crew": {"司机": 1, "信号工": 1}}],
        })
    sched = {t["task_id"]: {"es": 0, "ef": 4} for t in rd["tasks"]}
    eq, _ = PA._daily_peak(rd, sched,
                           lambda n: n not in PA.LABOR_NAMES and n not in PA.MACHINE_CREW)
    crew, _ = PA._daily_peak(rd, sched, lambda n: n in PA.MACHINE_CREW)
    assert eq["塔吊"] == 1, "场地级设备逐日取 max：同一天 3 条任务也只算 1 台，实际 %s" % eq
    assert eq["混凝土输送泵车"] == 3, "任务级机械仍按日叠加"
    assert crew["信号工"] == 1 and crew["司机"] == 1, "场地级设备的配员同为逐日 max：%s" % crew
    assert crew["泵工"] == 3, "任务级机械的配员仍按日叠加"


def test_场地级设备台数按用户申报翻倍时配员一起翻():
    rd = {"tasks": [{
        "task_id": "T1",
        "resources": {"塔吊": {"per_day": 2, "total_days": 5},
                      "司机": {"per_day": 2, "total_days": 10},
                      "信号工": {"per_day": 2, "total_days": 10}},
        "_site_equipment": [{"name": "塔吊", "quantity": 2,
                             "crew": {"司机": 1, "信号工": 1}}]}]}
    sched = {"T1": {"es": 0, "ef": 4}}
    eq, _ = PA._daily_peak(rd, sched,
                           lambda n: n not in PA.LABOR_NAMES and n not in PA.MACHINE_CREW)
    crew, _ = PA._daily_peak(rd, sched, lambda n: n in PA.MACHINE_CREW)
    assert eq["塔吊"] == 2
    assert crew["司机"] == 2 and crew["信号工"] == 2

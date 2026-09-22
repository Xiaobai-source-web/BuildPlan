"""看板图表回归测试 —— 锁住 ECharts 迁移 + 用户提出的 6 条缺陷修复。

覆盖：
  · 图表现走 ECharts（bundle 内联、三张图容器、bundle 早于 glue）
  · 甘特 y 轴带**工序编号**（缺陷②）
  · 横轴用**日历工期**而不是相对天数（缺陷⑤）
  · 人工/机械分离：泵工/辅助/操作工/司机 不再出现在设备图（缺陷⑥）
  · 兜底 SVG 路径同样不重叠、不截断、用日历轴（缺陷①③⑤）
  · 计划概要卡片不再 nowrap 溢出（缺陷①）

运行：python -m pytest backend/tests/test_dashboard_echarts.py -v
"""

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import schemas
from pipeline.nodes import delivery, echarts_page
from pipeline.nodes.delivery import build_plan_html

# 一份最小但"该有的都有"的计划：两个任务，一个挂机械配员、一个挂纯工种，
# 且日期跨月，便于验证日历轴。
MINI = schemas.PlanJson(
    plan_id="plan_echarts_test",
    overview={"project_name": "图表回归项目", "total_duration_days": 60,
              "planned_start_date": "2026-06-01", "planned_end_date": "2026-07-31",
              "critical_path_length": 1},
    wbs={"phases": [{"phase": "主体结构", "work_packages": [{"id": "1.1", "name": "结构", "sub_packages": [
        {"id": "1.1.1", "name": "混凝土浇筑", "duration_days": 3, "quantity": 10, "unit": "m³",
         "work_type": "混凝土工程"},
        {"id": "1.1.2", "name": "钢筋绑扎", "duration_days": 4, "quantity": 5, "unit": "t",
         "work_type": "钢筋工程"},
    ]}]}]},
    dependencies=[],
    cpm_result={"total_duration_days": 60, "critical_path": ["1.1.1"],
                "schedule": [{"task_id": "1.1.1", "es": 0, "ef": 3, "ls": 0, "lf": 3},
                             {"task_id": "1.1.2", "es": 3, "ef": 7, "ls": 3, "lf": 7}]},
    resource_demand={"tasks": []},
    key_milestones=[{"name": "开工", "date": "2026-06-01", "task_id": "1.1.1", "description": "开工"}],
    critical_path_tasks=[{"task_id": "1.1.1", "task_name": "混凝土浇筑", "start_date": "2026-06-01",
                          "finish_date": "2026-06-03", "duration_days": 3,
                          "assigned_resources": {"混凝土输送泵车": 1, "泵工": 2, "辅助": 2}}],
    all_tasks_schedule=[
        {"task_id": "1.1.1", "task_name": "混凝土浇筑", "start_date": "2026-06-01",
         "finish_date": "2026-06-03", "duration_days": 3,
         "assigned_resources": {"混凝土输送泵车": 1, "泵工": 2, "辅助": 2}},
        {"task_id": "1.1.2", "task_name": "钢筋绑扎", "start_date": "2026-06-03",
         "finish_date": "2026-06-07", "duration_days": 4,
         "assigned_resources": {"钢筋工": 5}},
    ],
    resource_plan={"total_manpower_days": 26, "peak_manpower": 5,
                   "equipment_peak": {"混凝土输送泵车": 1},
                   "machine_crew_peak": {"泵工": 2, "辅助": 2},
                   "labor_demand": {"混凝土工": 2633.6},
                   "material_summary": [{"name": "concrete", "total_quantity": 100, "unit": "m³"}]},
    risks=[{"risk_name": "雨季", "mitigation": "排水"}],
    report="# 监督报告",
).model_dump()

MECH_CREW = {"泵工", "辅助", "操作工", "司机"}


def _view():
    return delivery._compute_view(MINI)


# ---------------- 口径：人工 / 机械分离（缺陷⑥） ----------------

def test_machine_crew_counted_as_labor_not_equipment():
    v = _view()
    labor_names = {k for x in v["labor_daily"] for k in x["trades"]}
    equip_names = {k for x in v["equip_daily"] for k in x["items"]}
    present = {k for t in MINI["all_tasks_schedule"]
               for k in (t.get("assigned_resources") or {})} & MECH_CREW
    assert present, "fixture 本身应含机械配员，否则用例没有意义"
    assert present <= labor_names, "机械配员是人，必须计入人工"
    assert not (MECH_CREW & equip_names), "机械配员不能出现在设备里"


def test_trade_totals_labels_kind():
    rows = {r[0]: r[3] for r in delivery._trade_totals(_view())}
    assert rows.get("泵工") == "机械配员"
    assert rows.get("钢筋工") == "工种"


# ---------------- ECharts option（缺陷②⑤⑥） ----------------

def test_chart_options_are_pure_json():
    opt = echarts_page.build_chart_options(MINI, _view())
    assert sorted(opt.keys()) == ["equipment", "gantt", "personnel"]
    json.dumps(opt, ensure_ascii=False)          # 不能含函数/set


def test_gantt_rows_carry_task_number():
    opt = echarts_page.build_chart_options(MINI, _view())
    labels = opt["gantt"]["yAxis"]["data"]
    assert len(labels) == len(MINI["all_tasks_schedule"])
    assert labels[0].startswith("1.1.1"), labels[0]
    assert "1.1.2" in labels[1]


def test_gantt_axis_is_calendar():
    g = echarts_page.build_chart_options(MINI, _view())["gantt"]
    assert g["__startDateISO"] == "2026-06-01"
    ticks = g["__dateTicks"]
    assert ticks and ticks[0][1] == "6-1", ticks[:3]
    assert ticks[-1][1] == "7-31", ticks[-1:]


def test_equipment_excludes_people():
    e = echarts_page.build_chart_options(MINI, _view())["equipment"]
    assert e["xAxis"]["data"] == ["混凝土输送泵车"], e["xAxis"]["data"]


def test_personnel_has_per_trade_series():
    p = echarts_page.build_chart_options(MINI, _view())["personnel"]
    names = [s["name"] for s in p["series"]]
    assert "钢筋工" in names and "泵工" in names, names


# ---------------- 生成的 HTML ----------------

def test_html_has_inline_echarts_before_glue():
    html = open(build_plan_html(MINI), encoding="utf-8").read()
    assert html.startswith("<!DOCTYPE html>")
    if not echarts_page.has_echarts():
        return                                    # vendor 缺失时走兜底，另有用例覆盖
    assert "Apache Software Foundation" in html, "ECharts bundle 未内联"
    for cid in ("dsh-gantt", "dsh-personnel", "dsh-equipment"):
        assert cid in html, cid
    # bundle 必须早于图表 glue，否则 glue 里 window.echarts 还不存在
    assert html.index("Apache Software Foundation") < html.index("dsh-gantt")
    assert "var DSH_OPT" in html


def test_kv_card_no_longer_nowrap():
    html = open(build_plan_html(MINI), encoding="utf-8").read()
    m = re.search(r"\.kv b\{([^}]*)\}", html)
    assert m, "找不到 .kv b 样式"
    assert "nowrap" not in m.group(1), "计划概要长值仍会撑破卡片（缺陷①）"


def test_milestone_rendered_as_table():
    """还原成果提交的里程碑表（名称/日期/说明），不再是一串 <li>。"""
    html = open(build_plan_html(MINI), encoding="utf-8").read()
    assert "<th>里程碑名称</th><th>日期</th><th>说明</th>" in html
    assert "<td><b>开工</b></td>" in html


def test_schedule_versions_shown_as_number_comparison():
    """成果提交的「甘特对比模式」在 plan_json 里没有备选排程可对比，
    退化为工期方案的数字对比（理论最短 → 资源可行）。"""
    plan = json.loads(json.dumps(MINI))
    plan["meta"] = dict(plan.get("meta") or {},
                        schedule_versions={"theory_min_days": 677,
                                           "resource_ok_days": 847, "delta_days": 170})
    html = open(build_plan_html(plan), encoding="utf-8").read()
    assert "工期方案(理论→可行)" in html
    assert "677 → 847 天（+170）" in html


def test_single_zone_prefix_stripped_from_display():
    """缺陷④ 的展示层兜底：旧计划 JSON 的名字在生成时就写死了「Ⅰ区」，
    复看时不该再冒出来（新计划由 layer_engine 保证本来就没有）。"""
    plan = json.loads(json.dumps(MINI))
    for t in plan["all_tasks_schedule"]:
        t["task_name"] = "Ⅰ区 " + t["task_name"]
    for wp in plan["wbs"]["phases"][0]["work_packages"]:
        wp["name"] = "Ⅰ区 " + wp["name"]
        for sub in wp["sub_packages"]:
            sub["location"] = "Ⅰ区 " + str(sub.get("location") or "1-0.5层")
    # 监督报告是自由文本，区名嵌在句子中间 —— 整篇替换才剥得干净
    plan["report"] = "1. **Ⅰ区 混凝土浇筑** (1.1.1, 3天)"
    html = open(build_plan_html(plan), encoding="utf-8").read()
    assert "Ⅰ区" not in html, "单区工程的看板不该出现「Ⅰ区」"
    assert "混凝土浇筑" in html and "钢筋绑扎" in html, "名字本体不能被误删"


def test_multi_zone_prefix_is_kept():
    """真·多区（≥2 个不同前缀）必须保留区号，否则会丢掉分栋/分区的关键信息。"""
    plan = json.loads(json.dumps(MINI))
    for i, t in enumerate(plan["all_tasks_schedule"]):
        t["task_name"] = ("Ⅰ区 " if i % 2 == 0 else "Ⅱ区 ") + t["task_name"]
    html = open(build_plan_html(plan), encoding="utf-8").read()
    assert "Ⅰ区" in html and "Ⅱ区" in html


def test_zone_strip_is_non_destructive():
    """剥前缀只作用于展示副本，不得改动调用方手里的 plan（也不回写磁盘）。"""
    plan = json.loads(json.dumps(MINI))
    plan["all_tasks_schedule"][0]["task_name"] = "Ⅰ区 混凝土浇筑"
    delivery._strip_single_zone_prefix(plan)
    assert plan["all_tasks_schedule"][0]["task_name"] == "Ⅰ区 混凝土浇筑"


def test_zone_strip_noop_when_already_clean():
    """本来就干净的计划原样返回（零开销路径），不产生无意义的复制。"""
    plan = json.loads(json.dumps(MINI))
    assert delivery._strip_single_zone_prefix(plan) is plan


def test_gantt_applyfilters_keeps_renderitem():
    """回归：applyFilters 里的 setOption 曾写成 ``series:[{data:newData}]``，
    ``replaceMerge:["series"]`` 会把 custom series 整个换成**没有 renderItem** 的残缺对象
    → init 时画好的 307 条甘特条在 ``applyFilters("all")`` 之后**全部消失**。
    这就是用户报的「甘特图渲染失败」（只有左侧工序名、右侧空白）。

    已用真实 ECharts SSR 复现并验证修复：修复前 blue=0/red=0，修复后 blue=228/red=79。
    断言放在源码级 —— 数据层测试完全看不出这个问题。
    """
    glue = echarts_page._GLUE_JS
    m = re.search(r"ganttChart\.setOption\(\{(.*?)\}\s*,\s*\{\s*replaceMerge", glue, re.S)
    assert m, "找不到 applyFilters 里带 replaceMerge 的 setOption"
    body = m.group(1)
    for key in ("renderItem", "type", "encode"):
        assert key in body, f"甘特筛选后的 setOption 缺 {key} → custom series 会一条都不画"


def test_gantt_label_width_is_adaptive():
    """回归：左栏曾是固定 340px，短名字右对齐后左半边全是空白（用户反馈
    「左边工序名太多空白」）。现在必须由 fitLabelWidth 按最长工序名算出来。"""
    glue = echarts_page._GLUE_JS
    assert "function fitLabelWidth" in glue
    assert "function estTextPx" in glue
    assert "g.grid.left = fitLabelWidth(" in glue
    assert "g.yAxis.axisLabel.width = g.grid.left - 14" in glue


def test_report_rendered_as_markdown_not_pre():
    """施工监督报告要像 VS Code 的 md 预览那样渲染，而不是裸 markdown 塞 <pre>。"""
    html = open(build_plan_html(MINI), encoding="utf-8").read()
    assert "class='md-body'" in html, "报告卡片应套 .md-body"
    assert "施工监督报告</h2><pre>" not in html, "报告不该再是 <pre> 裸文本"


def test_md_to_html_renders_structure():
    """markdown 渲染器：标题 / 无序表 / 表格 / 有序表 / 粗体 都要出来。"""
    md = ("# 监督报告\n\n## 一、总体情况\n\n*   **项目名称**：某小区\n"
          "*   **总工期**：847日历天\n\n| 里程碑名称 | 计划日期 |\n| :--- | :--- |\n"
          "| 开工 | 2026-06-01 |\n\n## 三、分析\n\n1.  **施工许可等手续办理**（1.4.1，10天）\n")
    out = delivery._md_to_html(md)
    assert "<h4>一、总体情况</h4>" in out
    assert "<li><strong>项目名称</strong>：某小区</li>" in out
    assert "<th>里程碑名称</th>" in out and "<td>2026-06-01</td>" in out
    assert "<ol><li><strong>施工许可等手续办理</strong>" in out
    # 首行「# 监督报告」与卡片自带的 <h2>施工监督报告</h2> 重复 → 丢掉
    assert "监督报告</h3>" not in out


def test_md_to_html_escapes_before_markup():
    """先 _html.escape 再套标签：任务名里的 < & 不能破坏结构，也不能变成可执行标记。"""
    out = delivery._md_to_html("*   **<script>alert(1)</script>** 与 A&B\n")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>" in out
    assert "A&amp;B" in out


# ---------------- 兜底 SVG 路径（ECharts 不可用时不能退化） ----------------

def test_svg_fallback_cards(monkeypatch):
    monkeypatch.setattr(delivery, "_echarts_ok", lambda: False)
    html = open(build_plan_html(MINI), encoding="utf-8").read()
    assert "dsh-gantt" not in html
    assert "<svg" in html
    assert "主要工种人员配置曲线" in html and "设备峰值需求统计" in html


def test_svg_gantt_no_overlap_and_numbered():
    import datetime
    svg = delivery._svg_gantt(_view(), start_date=datetime.date(2026, 6, 1))
    # 缺陷②：y 轴标签带工序编号，且未被 [:20] 硬截断吃掉右括号
    assert "1.1.1 混凝土浇筑" in svg
    # 缺陷①：工期文字在左栏内右对齐（x=label_w-6=294），条形从 label_w=300 起 —— 物理隔离
    assert "text-anchor='end'" in svg
    assert "x='294'" in svg and "x='300'" in svg
    # 缺陷⑤：横轴是日历日期而非相对天数
    assert "06-01" in svg


def test_svg_gantt_ellipsis_for_overlong_names():
    import datetime
    plan = json.loads(json.dumps(MINI))
    plan["all_tasks_schedule"][0]["task_name"] = "超长工序名称" * 12
    view = delivery._compute_view(plan)
    svg = delivery._svg_gantt(view, start_date=datetime.date(2026, 6, 1))
    assert "…" in svg, "超长名称必须补省略号，而不是硬切"

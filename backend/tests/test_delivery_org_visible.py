# -*- coding: utf-8 -*-
"""交付物「施工组织层口径」可见性的回归护栏（本轮新增，**唯一**允许加新断言的文件）。

背景 —— 用户最大的疑问是「**306 工日 ÷ 9 人 = 34 天/层，凭什么**」。改动前交付物只印
「定额、工日、人数、设备」，看不到工期是怎么来的。上游组织层给每条排程行挂
`_organization`（契约字段名不许改）：

    {"cadence_days","n_faces","crew_per_face","crew_total","shifts","eta",
     "effective_crew_total","duration_days","feasible","t_min_days","source","person_days"}

外加 `meta["organization_gaps"]`（组织缺口）与 `meta["scope_audit"]`（审计提示）。
节拍落点 = `meta["boundary_conditions"]["cadence_days"]`（+ `cadence_scope`、
`_source.cadence_days`）；工日列真源 = `_organization.person_days`（工种工日，不含机械配员），
只有缺该字段时才退到交付侧累计并**必须标注**「可能含机械配员，与组织层口径可能不同」。
本文件钉住交付侧的四件硬要求：

① **看得到**：看板资源卡 / Word 相应章节出现「施工组织口径」，含主体节拍、
   口径公式（`工期 = 工日 ÷ (作业面数 × 每面人数 × 班次 × 效率折减)`）、效率折减 η
   是什么 + 当前取值，以及逐条工序的「作业面数 / 每面人数 / 班次 / 工日 → 工期」列；
② **不猜**：`_organization` 缺字段一律写「来源未记录」；没有 `_organization` 的工序
   不编行（宁可少一行，也不把"没有数据"画成"有数据"）；
③ **说实话**：组织缺口逐条写"需要 N 个面 / 上限只允许 M 个面 → 可达最短 Y 天"+ 杠杆；
   审计提示（重复建项 / 上限待审 / 选行离散）一律「疑似…请在人工门确认」「建议人工审定」，
   **不是**"系统已经知道错了"的结论；同一份文档里三个「人数」口径必须各自写明；
④ **丢段兜底**：新段落进 `_facts_bundle`，并由 `_ensure_org_section` 追加确定性段落
   （标记 `ORG_MARKERS` 与既有 `DELIVERY_MARKERS` **互相独立**，既有回归门语义不变）。

终版修改（WS3 维护本文件）：`终版修改_接口冻结.md` **§6「每面人数上限单源」**——
组织层工种曲线是唯一来源，`cap_per_face == org.crew_per_face`、`cap_total == crew_total`，
`resource_cap_below_org` 保留字段但**正常为 False**。本文件的班组真源样例已按此改为
单源（新增 `test_单源后正常路径不再有第二个每面上限`），旧计划那种"两套上限不同源"
的数据改由 `_crew_truth_plan_legacy()` 复刻，逐条说明的旧断言一并保留。

运行：python -m pytest backend/tests/test_delivery_org_visible.py -v
"""

import copy
import json
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import config                          # noqa: E402
from pipeline.nodes import delivery as D             # noqa: E402

REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_fix.json"


# ══════════════════════ 公共样例 ══════════════════════

def _plan():
    """一份最小计划：2 条带 `_organization` 的工序 + 1 条**不带**的工序。

    数值取自真计划实测证据（`plan_sample3_after_fix`）：
    · `6.1.1.1` 1-1层 ALC墙板安装：1420 m²、人工 306 工日（瓦工 9 人 × 34 天 → 270 +
      司机/信号工/塔吊/施工电梯的配员另计），组织层给「3 个作业面 × 20 人 × 1 班，
      η=0.8625 → 有效班组 51.75 人 → 工期 6 天」；
    · `6.1.1.3` 1-1层 砌块墙：284 m³（= 1420 m² × 0.2 m，**同一批墙**），组织层只给了
      节拍/作业面数/每面人数，**没有 η 与工期** → 交付侧必须写「来源未记录」；
    · `2.1.4` 截（凿）桩头：**没有** `_organization` → 组织层表里不许编行。
    """
    org_alc = {"cadence_days": 7.0, "n_faces": 3, "crew_per_face": 20, "crew_total": 60,
               "shifts": 1, "eta": 0.8625, "effective_crew_total": 51.75,
               "duration_days": 6, "feasible": True, "t_min_days": 4.4, "source": "cadence"}
    org_block = {"cadence_days": 7.0, "n_faces": 2, "crew_per_face": 9, "crew_total": 18,
                 "shifts": 1, "source": "preferred"}     # 故意缺 eta / effective / duration
    sched = [
        {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装",
         "start_date": "2028-10-11", "finish_date": "2028-11-10", "duration_days": 31,
         "assigned_resources": {"瓦工": 9}},
        {"task_id": "6.1.1.3", "task_name": "1-1层 砌块墙",
         "start_date": "2028-10-11", "finish_date": "2028-11-06", "duration_days": 27,
         "assigned_resources": {"瓦工": 9}},
        {"task_id": "2.1.4", "task_name": "截（凿）桩头",
         "start_date": "2028-10-11", "finish_date": "2028-10-15", "duration_days": 5,
         "assigned_resources": {}},
    ]
    rd_tasks = [
        {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装", "quantity": 1420.0,
         "planned_duration_days": 31, "resources": {"瓦工": {"per_day": 9, "total_days": 270.0}},
         "_organization": org_alc},
        {"task_id": "6.1.1.3", "task_name": "1-1层 砌块墙", "quantity": 284.0,
         "planned_duration_days": 27, "resources": {"瓦工": {"per_day": 9, "total_days": 243.0}},
         "_organization": org_block},
        {"task_id": "2.1.4", "task_name": "截（凿）桩头", "quantity": 120.0,
         "planned_duration_days": 5, "resources": {}},
    ]
    return {
        "plan_id": "plan_org_visible_test",
        "overview": {"project_name": "施工组织层口径测试", "total_duration_days": 60,
                     "planned_start_date": "2028-10-11", "planned_end_date": "2028-12-10",
                     "critical_path_length": 1},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "6.1", "name": "砌体", "sub_packages": [
                {"id": "6.1.1.1", "name": "1-1层 ALC墙板安装", "duration_days": 3,
                 "quantity": 1420.0, "unit": "m²"},
                {"id": "6.1.1.3", "name": "1-1层 砌块墙", "duration_days": 3,
                 "quantity": 284.0, "unit": "m³"}]}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 60, "critical_path": ["6.1.1.1"],
                       "schedule": [{"task_id": "6.1.1.1", "es": 0, "ef": 30}]},
        "all_tasks_schedule": sched,
        "critical_path_tasks": [dict(sched[0])],
        "key_milestones": [{"name": "开工", "date": "2028-10-11", "task_id": "6.1.1.1",
                            "description": "开工"}],
        "resource_demand": {"tasks": rd_tasks},
        "resource_plan": {"total_manpower_days": 513.0, "peak_manpower": 9,
                          "peak_manpower_source": "resource_curve", "curve_peak_manpower": 9,
                          "declared_peak_manpower": 180,
                          "declared_peak_manpower_source": "model",
                          "equipment_peak": {}, "machine_crew_peak": {},
                          "material_summary": []},
        "meta": {
            "audit_status": "未审计",
            "organization": {"cadence_days": 7.0},          # plan 级主体节拍
            "organization_gaps": [{
                "task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装", "trade": "瓦工",
                "person_days": 306.0, "cadence_days": 7.0, "n_needed": 5, "n_max": 3,
                "c_max": 20, "t_min_days": 4.4,
                "levers": ["放宽节拍到 9 天", "增加作业面到 5", "改工艺减少工日"]}],
            "scope_audit": {
                "duplicate_scopes": [{
                    "kb_activity_id": "KB-ALC-001", "location": "1-1层 墙体",
                    "task_ids": ["6.1.1.1", "6.1.1.3"],
                    "evidence": "1420 m² × 0.2 m = 284 m³（6.1.1.3 以 m³ 计量同一批墙）"}],
                "norm_row_spread": [
                    {"kb_activity_id": "KB-MASON-7", "unit": "m³", "ratio": 1.8,
                     "samples": [{"source_code": "LN_781", "value": 0.943},
                                 {"source_code": "LN_902", "value": 1.7}]},
                    {"kb_activity_id": "KB-OK-9", "unit": "m²", "ratio": 1.2, "samples": []}],
                "cmax_review": {"count": 381, "sample": [
                    {"kb_activity_id": "KB-1", "v1_max_labor": 20, "v2_crew_max": 42}]}},
        },
        "report": "# 报告",
    }


@pytest.fixture()
def tmp_deliverables(tmp_path, monkeypatch):
    """把交付物目录指向 tmp，避免测试往 `输出结果/` 里写东西。"""
    d = tmp_path / "deliverables"
    d.mkdir(parents=True, exist_ok=True)   # PID 复用时 `_test_tmp/p<pid>/tmp/testN` 会残留
    monkeypatch.setattr(config, "DELIVERABLES_DIR", d)
    monkeypatch.setattr(D.config, "DELIVERABLES_DIR", d)
    return d


def _html(plan=None):
    return D._org_section_html(plan or _plan(), D._compute_view(plan or _plan()))


def _word_text(path):
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


class _FakeLLM:
    """假 LLM（默认线路 `mimo-v2.5` 配额已耗尽 —— 本轮**不跑真实 LLM**）。"""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    def chat_text(self, *a, **kw):
        self.calls += 1
        return self.text


BARE_LLM_HTML = ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
                 "<title>模型编排</title></head><body><h1>施工进度计划看板</h1>"
                 "<p>模型只写了个壳</p></body></html>")


# ══════════════════════ ① 主体节拍 / 口径公式 / 效率折减 ══════════════════════

class TestCadenceAndFormula:
    def test_plan级节拍渲染成用户输入(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        assert m["cadence_text"] == "主体节拍：标准层 7 天/层（来源：用户输入）", m["cadence_text"]

    def test_显式来源字段优先(self):
        plan = _plan()
        plan["meta"]["organization"]["cadence_source"] = "用户在设计门填写"
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"].endswith("（来源：用户在设计门填写）"), m["cadence_text"]

    def test_没有节拍时不猜_写未提供(self):
        plan = _plan()
        plan["meta"].pop("organization")
        for t in plan["resource_demand"]["tasks"]:
            org = t.get("_organization")
            if isinstance(org, dict):
                org.pop("cadence_days", None)
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：" + D.ORG_NO_CADENCE, m["cadence_text"]

    def test_逐条节拍兜底并标出来源(self):
        """plan 级没有节拍时，从 `_organization.source == "cadence"` 的行兜底。"""
        plan = _plan()
        plan["meta"].pop("organization")
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：标准层 7 天/层（来源：用户输入）", m["cadence_text"]

    def test_契约落点boundary_conditions是第一位(self):
        """节拍落点 = `meta["boundary_conditions"]["cadence_days"]`（契约确定）。"""
        plan = _plan()
        plan["meta"]["boundary_conditions"] = {
            "cadence_days": 7.0, "cadence_scope": "标准层",
            "_source": {"cadence_days": "user"}}
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：标准层 7 天/层（来源：用户输入）", m["cadence_text"]

    def test_契约落点压过历史探测键(self):
        plan = _plan()
        plan["meta"]["organization"]["cadence_days"] = 5.0     # 历史键：不该赢
        plan["meta"]["organization_cadence_days"] = 4.0
        plan["meta"]["boundary_conditions"] = {
            "cadence_days": 7.0, "_source": {"cadence_days": "user"}}
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：标准层 7 天/层（来源：用户输入）", m["cadence_text"]

    def test_模型估算的节拍如实标成非用户输入(self):
        plan = _plan()
        plan["meta"]["boundary_conditions"] = {
            "cadence_days": 9.0, "_source": {"cadence_days": "model"}}
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：标准层 9 天/层（来源：模型估算，非用户输入）", \
            m["cadence_text"]

    def test_cadence_scope原样照抄(self):
        plan = _plan()
        plan["meta"]["boundary_conditions"] = {
            "cadence_days": 7.0, "cadence_scope": "地下室",
            "_source": {"cadence_days": "user"}}
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：地下室 7 天/层（来源：用户输入）", m["cadence_text"]

    def test_没有来源字段也没有证据时不编来源(self):
        plan = _plan()
        plan["meta"].pop("organization")
        plan["meta"]["boundary_conditions"] = {"cadence_days": 7.0}   # 无 _source
        for t in plan["resource_demand"]["tasks"]:                    # 无逐条证据
            org = t.get("_organization")
            if isinstance(org, dict):
                org.pop("cadence_days", None)
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["cadence_text"] == "主体节拍：标准层 7 天/层（来源：%s）" % D.ORG_UNRECORDED, \
            m["cadence_text"]

    def test_旧口径公式与效率折减说明已删除(self):
        """C8①（用户明确裁定）：效率折减 η / 旧口径公式**整条删除**，交付物一个字都不印。

        旧断言（`D.ORG_FORMULA in h`、`"效率折减" in h and "η" in h`、`"0.8625" in h`、
        「材料转运」/「工日 ÷ 有效班组 = 工期」）钉的正是**被删除的旧口径**，
        已随 η 一起废掉，改钉「不再出现」。
        """
        plan = _plan()
        h = _html(plan)
        for banned in ("效率折减", "η", "0.8625", "有效班组", "材料转运",
                       "工期 = 工日 ÷ (作业面数 × 每面人数 × 班次 × 效率折减)",
                       "工日 ÷ 有效班组 = 工期"):
            assert banned not in h, "旧口径残留：%s" % banned
        assert not hasattr(D, "ORG_FORMULA"), "ORG_FORMULA 常量必须整体删除"
        assert not hasattr(D, "ORG_ETA_EXPLAIN"), "ORG_ETA_EXPLAIN 常量必须整体删除"
        # 组织层该有的东西一个都不许连带丢
        assert "作业面数" in h and "每面人数" in h and "组织来源" in h

    def test_不再有eta取值展示(self):
        """旧 `eta_current`（「当前取值：η = 0.8625…」）已删；model 里也没有这三个键。"""
        plan = _plan()
        m = D.organization_section_model(plan, D._compute_view(plan))
        for gone in ("formula", "eta_explain", "eta_current"):
            assert gone not in m, "旧口径键必须删除：%s" % gone
        assert "0.8625" not in json.dumps(m, ensure_ascii=False, default=str)
        assert "η" not in _html(plan)


# ══════════════════════ ② 逐条工序表（不许猜数） ══════════════════════

class TestPerRowTable:
    def test_表头就是契约要求的列(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        assert m["header"] == list(D.ORG_TABLE_HEADER)
        for col in ("作业面数", "每面人数", "班次", "工日", "工期(天)"):
            assert col in m["header"], col

    def test_逐条单元格照抄契约字段(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        cells = {r["task_id"]: r["cells"] for r in m["rows"]}
        alc = cells["6.1.1.1"]
        # C8① 之后表头 8 列（去掉旧口径的「效率折减 η」「有效班组(人)」两列）
        assert alc == ["6.1.1.1", "1-1层 ALC墙板安装", "270", "3", "20", "1",
                       "6", "用户输入节拍"], alc
        blk = cells["6.1.1.3"]
        assert blk[3] == "2" and blk[4] == "9" and blk[5] == "1"
        assert blk[6] == D.ORG_UNRECORDED, "组织层没给工期就是「来源未记录」，不许代填"
        assert blk[7] == "推荐班组配置", "source=preferred 要翻成人话"
        assert len(D.ORG_TABLE_HEADER) == 8, "旧口径两列必须删掉"
        for banned in ("效率折减", "η", "有效班组"):
            assert all(banned not in str(c) for c in alc + blk), banned

    def test_没有organization的工序不编行(self):
        plan = _plan()
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert [r["task_id"] for r in m["rows"]] == ["6.1.1.1", "6.1.1.3"], "2.1.4 无组织层数据"
        assert "2.1.4" not in _html(plan), "宁可少一行，也不把『没有数据』画成『有数据』"
        assert m["row_count"] == 2 and m["task_count"] == 3
        assert "本段覆盖 2 / 3 条排程行" in m["rows_note"]

    def test_排程行上挂organization也能取到(self):
        """契约说「每条排程行 / resource_demand.tasks[*]」—— 排程行同样要认。"""
        plan = _plan()
        sched_org = plan["resource_demand"]["tasks"][0].pop("_organization")
        plan["all_tasks_schedule"][0]["_organization"] = sched_org
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert [r["task_id"] for r in m["rows"]] == ["6.1.1.1", "6.1.1.3"]
        # 旧口径的 `eta` 键已随 C8① 删除：行字典里不许再有它
        assert "eta" not in m["rows"][0] and "effective_crew_total" not in m["rows"][0]

    def test_工日缺person_days时用累计并强制标注口径不同(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        assert m["rows"][0]["person_days"] == 270.0
        assert m["rows"][0]["person_days_source"] == D.ORG_PERSON_DAYS_FALLBACK
        assert m["rows"][0]["person_days_from_org"] is False
        assert m["person_days_org_count"] == 0
        assert "可能含机械配员，与组织层口径可能不同" in m["person_days_note"]
        assert "total_days" in m["person_days_note"]
        assert m["rows"][0]["contract_notes"] == [], "3×20×1=60 与 crew_total 自洽"

    def test_工日真源优先用组织层person_days(self):
        """契约给了 `_organization.person_days`（工种工日，不含机械配员）就必须用它。"""
        plan = _plan()
        plan["resource_demand"]["tasks"][0]["_organization"]["person_days"] = 306.0
        m = D.organization_section_model(plan, D._compute_view(plan))
        row = m["rows"][0]
        assert row["person_days"] == 306.0, "270 是含机械配员的累计，不能顶替真源"
        assert row["person_days_from_org"] is True
        assert "本工序工种工日" in row["person_days_source"]
        assert "不含机械配员" in row["person_days_source"]
        assert m["person_days_org_count"] == 1
        assert "其余 1 条组织层没给该字段" in m["person_days_note"], m["person_days_note"]
        h = _html(plan)
        assert "本工序工种工日" in h and "可能含机械配员" in h

    def test_契约不自洽时照抄原值并留痕(self):
        plan = _plan()
        plan["resource_demand"]["tasks"][0]["_organization"]["crew_total"] = 99
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["rows"][0]["cells"][6] == "6", "原值照抄，不擅自统一"
        assert m["rows"][0]["cells"][5] == "1", "班次原值照抄"
        assert m["rows"][0]["contract_notes"], "不自洽必须留痕"
        assert any("契约字段自检" in x for x in m["consistency"])


# ══════════════════════ ③ 组织缺口报告 ══════════════════════

class TestOrganizationGaps:
    def test_逐条缺口写清需要几个面_上限几个面_可达最短几天(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        line = m["gaps"]["lines"][0]
        assert "1-1层 ALC墙板安装" in line and "6.1.1.1" in line
        assert "在 7 天节拍下需要 5 个作业面" in line, line
        assert "上限只允许 3 个面" in line, line
        assert "每面人数上限 20 人" in line and "可达最短 4.4 天" in line, line
        assert "可动杠杆：放宽节拍到 9 天；增加作业面到 5；改工艺减少工日。" in line, line
        assert D.ORG_GAP_TITLE in _html()

    def test_空列表且有节拍_是校核过没有做不到的工序(self):
        """空列表 + 有节拍 = **真的**按节拍校核过、没有做不到的工序。"""
        plan = _plan()
        plan["meta"]["organization_gaps"] = []
        m = D.organization_section_model(plan, D._compute_view(plan))
        line = m["gaps"]["lines"][0]
        assert line.startswith(D.ORG_NO_GAP), line
        assert "已按主体节拍（标准层 7 天/层）逐条校核" in line, line
        assert "没有做不到的工序" in line, line
        assert m["gaps"]["state"] == "checked_none", m["gaps"]["state"]
        assert D.ORG_NO_GAP in _html(plan)

    def test_空列表但没有节拍_必须写未做校核(self):
        """没节拍 = 组织层**没跑过**校核：空列表绝不能冒充「校核通过」。"""
        plan = _plan()
        plan["meta"]["organization_gaps"] = []
        plan["meta"].pop("organization")                     # 没有 plan 级节拍
        for t in plan["resource_demand"]["tasks"]:
            org = t.get("_organization")
            if isinstance(org, dict):
                org.pop("cadence_days", None)                # 也没有逐条节拍
        m = D.organization_section_model(plan, D._compute_view(plan))
        line = m["gaps"]["lines"][0]
        assert line.startswith(D.ORG_NO_GAP), line
        assert D.ORG_NO_CADENCE in line, line
        assert "未做组织层校核" in line and "不等于「校核通过」" in line, line
        assert m["gaps"]["state"] == "unchecked", m["gaps"]["state"]
        assert "未做组织层校核" in _html(plan)

    def test_非空列表逐条列出且状态是gaps(self):
        plan = _plan()
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["gaps"]["state"] == "gaps" and m["gaps"]["present"] is True
        assert len(m["gaps"]["lines"]) == 1
        assert "19 个作业面" not in m["gaps"]["lines"][0]     # 样例里是 5 个面

    def test_格式不是列表时不猜含义(self):
        plan = _plan()
        plan["meta"]["organization_gaps"] = {"task_id": "6.1.1.1"}
        m = D.organization_section_model(plan, D._compute_view(plan))
        line = m["gaps"]["lines"][0]
        assert "格式不是列表" in line, line
        assert m["gaps"]["state"] == "malformed", m["gaps"]["state"]

    def test_字段缺失时不冒充已核对(self):
        plan = _plan()
        plan["meta"].pop("organization_gaps")
        m = D.organization_section_model(plan, D._compute_view(plan))
        line = m["gaps"]["lines"][0]
        assert line.startswith(D.ORG_NO_GAP), line
        assert "不代表已核对" in line and m["gaps"]["present"] is False, line


# ══════════════════════ ④ 审计提示（待审，不是结论） ══════════════════════

class TestScopeAuditHints:
    def test_重复建项逐条列出并给证据数字(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        line = m["scope"]["duplicates"][0]
        assert line.startswith("疑似重复计量，请在人工门确认"), line
        assert "6.1.1.1" in line and "6.1.1.3" in line, "两条工序号都要列出来"
        assert "1420 m² × 0.2 m = 284 m³" in line, "证据数字必须原样带出来"
        assert "2 条工序指向同一批工程量" in line, line

    def test_上限待审带381行与前若干行样本(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        line = m["scope"]["cmax_line"]
        assert "有 381 行的单面人数上限存在两个互相矛盾的值" in line, line
        assert "v1 常数 vs v2 同族最大" in line
        assert "本次沿用现行口径，建议人工审定" in line
        assert m["scope"]["cmax_sample"], "要附前若干行"
        assert m["scope"]["cmax_header"] == ["kb_activity_id", "v1_max_labor", "v2_crew_max"]
        assert "381" in _html(), "样本区要写清共多少行"

    def test_选行离散只列超过1_5倍的组(self):
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        joined = "\n".join(m["scope"]["spread_lines"])
        assert "KB-MASON-7" in joined and "1.8" in joined, joined
        assert "请在人工门确认" in joined
        assert "KB-OK-9" not in joined, "1.2 倍低于阈值，不许当成问题列出来"

    def test_没有离散数据时说明未核对(self):
        plan = _plan()
        plan["meta"]["scope_audit"]["norm_row_spread"] = []
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert any("未提供选行离散数据" in x and "未核对" in x
                   for x in m["scope"]["spread_lines"])

    def test_没有scope_audit时三项都写未核对(self):
        plan = _plan()
        plan["meta"].pop("scope_audit")
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert "重复计量 / 单面人数上限 / 选行离散三项「均未核对」" in m["scope"]["note"]
        assert "不做「没有问题」的结论" in m["scope"]["note"]

    def test_组织段文本不许泄漏markdown记号(self):
        """组织段同时进看板 HTML 与 Word 纯文本 —— 一个 `**` / 反引号都不能漏。"""
        m = D.organization_section_model(_plan(), D._compute_view(_plan()))
        texts = ([m["cadence_text"], m["rows_note"], m.get("person_days_note", ""),
                  m["scope"]["cmax_line"], m["scope"]["note"]]
                 + m["gaps"]["lines"] + m["consistency"]
                 + m.get("planned_person_days_lines", [])
                 + m["scope"]["duplicates"] + m["scope"]["spread_lines"]
                 + [r["person_days_source"] or "" for r in m["rows"]])
        for t in texts:
            assert "**" not in t, t
            assert "`" not in t, t

    def test_措辞是待审不是结论(self):
        h = _html()
        assert D.ORG_AUDIT_TITLE in h
        assert "提示与待审，不是系统结论" in h
        assert "疑似重复计量，请在人工门确认" in h
        # 审计段里不许出现"系统已判定/错误"这类结论式措辞
        seg = h.split(D.ORG_AUDIT_TITLE)[1].split("口径对齐自检")[0]
        assert "错误" not in seg, "审计提示不许写成结论式的『错误』"
        assert "系统已" not in seg, seg[:200]


# ══════════════════════ ⑤ 守恒/一致性自检 ══════════════════════

class TestConsistency:
    def test_两个口径的人数各自写明(self):
        plan = _plan()
        m = D.organization_section_model(plan, D._compute_view(plan))
        joined = "\n".join(m["consistency"])
        assert "逐日人员曲线峰值" in joined, "曲线峰值口径要写明"
        assert "名义班组" in joined
        assert "互不替代" in joined
        # C8①：旧口径的「有效班组 = 名义班组 × η」已删，自检里不许再出现
        assert "有效班组" not in joined and "η" not in joined, joined

    def test_无组织层数据时数值曲线口径也解释清楚(self):
        plan = _plan()
        for t in plan["resource_demand"]["tasks"]:
            t.pop("_organization", None)
        for t in plan["all_tasks_schedule"]:
            t.pop("_organization", None)
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["row_count"] == 0
        joined = "\n".join(m["consistency"])
        assert "逐日人员曲线峰值" in joined and "不是同一个数" in joined
        # 没有任何一行带 _organization 时也不许编行
        assert "（无：没有排程行携带 _organization）" in _html(plan)

    def test_组织层工期与排程跨度不一致要显式说明(self):
        plan = _plan()          # 6.1.1.1 组织层 6 天 / 排程跨度 31 天
        m = D.organization_section_model(plan, D._compute_view(plan))
        joined = "\n".join(m["consistency"])
        assert "不一致的行 1 条" in joined, joined
        assert "6.1.1.1（组织层 6 天 / 排程跨度 31 天）" in joined
        assert "两个数都列出、不合并" in joined

    def test_可行行与缺口条目并存时标疑似上游口径不一致(self):
        """契约要求 `feasible=true ⟺ 无缺口条目`：同时出现就是上游口径打架。"""
        plan = _plan()          # 6.1.1.1：feasible=true / 工期 6 天，同时又在缺口报告里
        m = D.organization_section_model(plan, D._compute_view(plan))
        joined = "\n".join(m["consistency"])
        assert "疑似上游口径不一致" in joined, joined
        assert "6.1.1.1" in joined
        assert "可达最短 4.4 天" in joined, "缺口证据要带出来"
        assert "未擅自取舍" in joined
        h = _html(plan)
        assert "疑似上游口径不一致" in h, "这条自检必须落到看板上"

    def test_行内已标不可达则不算口径打架(self):
        plan = _plan()
        org = plan["resource_demand"]["tasks"][0]["_organization"]
        org["feasible"] = False
        org.pop("duration_days")
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert "疑似上游口径不一致" not in "\n".join(m["consistency"])

    def test_缺口条目落在没有组织行的任务上不算打架(self):
        plan = _plan()
        plan["meta"]["organization_gaps"][0]["task_id"] = "2.1.4"   # 这一行没有 _organization
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert "疑似上游口径不一致" not in "\n".join(m["consistency"])


# ══════════════════════ ⑤·2 需求工日 vs 实际投入工日（防御式） ══════════════════════

class TestPlannedPersonDays:
    """O 侧新增 `_organization.planned_person_days = crew_total × duration_days`。

    措施项（`source == "measure_item"`，如"爬架提升"按固定操作时长 1 天）会出现
    `person_days=99` 而 `planned_person_days=10`：只印 99，读者会自己除出
    `99 ÷ (1 面 × 10 人 × η) ≈ 11 天`，而工期写 1 天 —— 又一个「同名两个数」。
    铁律：**键缺失或与 person_days 相等 → 一个字都不加**，且这类行**不许**报口径打架假告警。
    """

    @staticmethod
    def _measure_task(plan, planned=10.0, person_days=99.0):
        org = plan["resource_demand"]["tasks"][0]["_organization"]
        org.update({"source": "measure_item", "person_days": person_days,
                    "planned_person_days": planned, "crew_total": 10, "crew_per_face": 10,
                    "n_faces": 1, "shifts": 1, "duration_days": 1, "feasible": True,
                    "eta": 0.9, "effective_crew_total": 9.0})
        return org

    def test_两个口径不同才出脚注并写清关系(self):
        plan = _plan()
        self._measure_task(plan)
        m = D.organization_section_model(plan, D._compute_view(plan))
        lines = m["planned_person_days_lines"]
        assert len(lines) == 1, lines
        line = lines[0]
        assert "工日：需求 99" in line, line
        assert "投入 10" in line, line
        assert "差额 89" in line, line
        assert "措施项按固定操作时长 1 天 × 10 人" in line, line
        assert "6.1.1.1" in line, "要能指回哪条工序"
        assert "`" not in line and "**" not in line, "markdown 记号会原样印进 Word"
        h = _html(plan)
        assert "工日：需求 99" in h and "投入 10" in h, "脚注必须落到看板上"
        assert "工日两口径" in h

    def test_差额指向缺口报告留痕(self):
        plan = _plan()          # 6.1.1.1 在 organization_gaps 里
        self._measure_task(plan)
        line = D.organization_section_model(plan, D._compute_view(plan))[
            "planned_person_days_lines"][0]
        assert "差额 89 工日见「%s」留痕" % D.ORG_GAP_TITLE in line, line

    def test_没有缺口留痕就不冒充已解释(self):
        plan = _plan()
        plan["meta"].pop("organization_gaps")
        self._measure_task(plan)
        line = D.organization_section_model(plan, D._compute_view(plan))[
            "planned_person_days_lines"][0]
        assert "成因未记录，请人工确认" in line, line
        assert "留痕" not in line, "没有缺口条目就不许说『已由封顶/缺口留痕记录』"

    def test_不显式给措施项理由时只写字段名不编工期解释(self):
        """`crew_total` / `duration_days` 缺一个 —— 括号里不许编数字。"""
        plan = _plan()
        org = self._measure_task(plan)
        org.pop("duration_days")
        line = D.organization_section_model(plan, D._compute_view(plan))[
            "planned_person_days_lines"][0]
        assert "措施项按固定操作时长" not in line, line
        assert "planned_person_days = crew_total × duration_days" in line, line

    def test_相等时一个字都不加(self):
        plan = _plan()
        org = plan["resource_demand"]["tasks"][0]["_organization"]
        org["person_days"] = 306.0
        org["planned_person_days"] = 306.0        # 相等 → 噪声，不许出现
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["planned_person_days_lines"] == []
        h = _html(plan)
        assert "工日：需求" not in h, "相等时不许出现任何两口径文案"
        assert "工日两口径" not in h

    def test_键缺失时一个字都不加(self):
        plan = _plan()          # fixture 里没有任何 planned_person_days
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["planned_person_days_lines"] == []
        assert all(r["planned_person_days"] is None for r in m["rows"])
        h = _html(plan)
        assert "工日：需求" not in h and "工日两口径" not in h

    def test_只有差异行出现在脚注里(self):
        plan = _plan()
        self._measure_task(plan)
        plan["resource_demand"]["tasks"][1]["_organization"]["planned_person_days"] = 243.0
        plan["resource_demand"]["tasks"][1]["_organization"]["person_days"] = 243.0
        lines = D.organization_section_model(plan, D._compute_view(plan))[
            "planned_person_days_lines"]
        assert len(lines) == 1 and "6.1.1.1" in lines[0], lines

    def test_非法值不炸也不编(self):
        plan = _plan()
        self._measure_task(plan, planned="未知")
        m = D.organization_section_model(plan, D._compute_view(plan))
        assert m["planned_person_days_lines"] == []

    def test_措施项工期不等于公式推算不算口径打架(self, tmp_deliverables):
        """契约例外：措施项按固定操作时长排期，`duration_days != ceil(工日/有效班组)` 属预期。"""
        plan = _plan()
        self._measure_task(plan)      # feasible=True + 在缺口报告里 + 工期 1 天 ≠ 公式值
        m = D.organization_section_model(plan, D._compute_view(plan))
        joined = "\n".join(m["consistency"])
        assert "疑似上游口径不一致" not in joined, joined
        assert "措施项" in joined and "预期口径差异" in joined, joined
        h = _html(plan)
        assert "疑似上游口径不一致" not in h, "每次重跑都出假告警就失去自检价值"
        assert "预期口径差异" in h

    def test_措施项例外只豁免自己(self):
        plan = _plan()
        self._measure_task(plan)                       # 6.1.1.1 豁免
        block = plan["resource_demand"]["tasks"][1]["_organization"]
        block.update({"feasible": True, "duration_days": 6, "person_days": 324.0})
        plan["meta"]["organization_gaps"].append(
            {"task_id": "6.1.1.3", "task_name": "1-1层 砌块墙", "trade": "瓦工",
             "person_days": 324.0, "cadence_days": 7.0, "n_needed": 6, "n_max": 2,
             "c_max": 9, "t_min_days": 18.0, "levers": ["增加作业面"]})
        joined = "\n".join(D.organization_section_model(
            plan, D._compute_view(plan))["consistency"])
        assert "疑似上游口径不一致" in joined, joined
        seg = joined.split("疑似上游口径不一致")[1]
        assert "6.1.1.3" in seg and "6.1.1.1" not in seg.split("\n")[0], seg

    def test_Word也印两口径脚注(self, tmp_deliverables):
        from docx import Document
        plan = _plan()
        self._measure_task(plan)
        doc = Document(D.build_plan_docx(plan))
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "工日两口径" in text, text[-800:]
        assert "工日：需求 99" in text and "投入 10" in text
        assert "差额 89" in text
        assert "|" not in text, "Word 段落不许出现竖线"

    def test_Word相等或缺失时不提两口径(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "工日两口径" not in text, "相等/缺失时一个字都不加"
        assert "工日：需求" not in text


# ══════════════════════ ⑥ 看板 HTML 落地 ══════════════════════

class TestBoardHtml:
    def test_看板含全部关键词(self, tmp_deliverables):
        plan = _plan()
        path = D.build_plan_html(plan)
        h = Path(path).read_text(encoding="utf-8")
        assert "施工组织口径" in h and "作业面数" in h and "组织来源" in h
        assert "组织缺口" in h and "疑似重复计量" in h
        # C8①：旧口径（效率折减 η / 有效班组）一个字都不许出现在看板上
        for banned in ("效率折减", "η", "0.8625", "有效班组"):
            assert banned not in h, banned
        assert "1420 m² × 0.2 m = 284 m³" in h
        # 新表不许抢既有表的位置：组织层卡片必须在 WBS 卡之后
        assert h.index("工序 ID") > h.index("WBS 目标(天)")

    def test_没有organization时看板仍渲染且说实话(self, tmp_deliverables):
        plan = _plan()
        for t in plan["resource_demand"]["tasks"]:
            t.pop("_organization", None)
        for t in plan["all_tasks_schedule"]:
            t.pop("_organization", None)
        plan["meta"].pop("organization", None)
        h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        assert "施工组织口径" in h and D.ORG_NO_CADENCE in h
        assert D.ORG_UNRECORDED in h
        assert "没有任何一行」携带「_organization」" in h


# ══════════════════════ ⑦ Word 落地 ══════════════════════

class TestWordSection:
    def test_Word含组织段且不新增Heading进目录(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        body = "\n".join(p.text for p in doc.paragraphs)
        assert "施工组织口径" in body, "Word 也要能看到工期是怎么来的"
        # C8①：旧口径（口径公式 / 效率折减是什么 / 当前取值 η）整段删除
        for banned in ("效率折减", "η", "0.8625", "有效班组", "口径公式："):
            assert banned not in body, banned
        assert "组织缺口" in body and "需要 5 个作业面" in body
        assert "疑似重复计量，请在人工门确认" in body
        assert "有 381 行的单面人数上限存在两个互相矛盾的值" in body
        headings = [p.text for p in doc.paragraphs
                    if (p.style.name or "").startswith("Heading")]
        for t in (D.ORG_TITLE, D.ORG_GAP_TITLE, D.ORG_AUDIT_TITLE):
            assert t not in headings, (
                "组织段标题不能进 Heading 目录（audit_gate 的目录清单逐字固定）")

    def test_Word组织表可以按表头唯一定位(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
        assert list(D.ORG_TABLE_HEADER) in grids, grids
        idx = grids.index(list(D.ORG_TABLE_HEADER))
        rows = [[c.text for c in r.cells] for r in doc.tables[idx].rows]
        got = {r[0]: r for r in rows[1:]}
        assert set(got) == {"6.1.1.1", "6.1.1.3"}, "不许给 2.1.4 编行"
        assert got["6.1.1.1"][6] == "6"
        assert got["6.1.1.3"][6] == D.ORG_UNRECORDED
        assert len(D.ORG_TABLE_HEADER) == 8, "8 列（C8① 删掉 η / 有效班组两列）"

    def test_Word组织段排在横道表之后(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
        gantt = next(i for i, g in enumerate(grids) if g and g[0] == "任务 ID")
        org = grids.index(list(D.ORG_TABLE_HEADER))
        assert org > gantt, "组织段必须在横道图之后，不许抢既有『表格第一行』的位置"

    def test_Word组织段不许有竖线或br(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        body = "\n".join(p.text for p in doc.paragraphs)
        assert "|" not in body and "<br>" not in body

    def test_Word口径对齐自检可读到两个数(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        body = "\n".join(p.text for p in doc.paragraphs)
        assert "逐日人员曲线峰值" in body and "名义班组" in body
        # C8①：旧口径的「有效班组 = 名义班组 × η」已删
        assert "有效班组" not in body and "η" not in body

    def test_Word也写出口径打架自检与工日来源(self, tmp_deliverables):
        from docx import Document
        doc = Document(D.build_plan_docx(_plan()))
        body = "\n".join(p.text for p in doc.paragraphs)
        assert "疑似上游口径不一致" in body
        assert "可能含机械配员，与组织层口径可能不同" in body


# ══════════════════════ ⑧ LLM 编排路径与丢段兜底 ══════════════════════

class TestFactsAndMarkerGuard:
    def test_facts_bundle带组织层口径(self):
        plan = _plan()
        facts = D._facts_bundle(plan, D._compute_view(plan))
        org = facts["organization"]
        # C8①：旧口径的 formula / eta_explain / eta_current 三个键已删
        for gone in ("formula", "eta_explain", "eta_current"):
            assert gone not in org, gone
        assert "节拍仅作对比参考" in org["cadence_caliber"]
        assert org["cadence_text"] == "主体节拍：标准层 7 天/层（来源：用户输入）"
        assert org["header"] == list(D.ORG_TABLE_HEADER)
        assert [r["task_id"] for r in org["rows"]] == ["6.1.1.1", "6.1.1.3"]
        assert org["rows"][0]["cells"][6] == "6"
        assert org["gaps"]["lines"] and "需要 5 个作业面" in org["gaps"]["lines"][0]
        assert org["scope_audit_lines"]["duplicates"]
        assert org["consistency_lines"]
        assert "本工序工种工日" in org["person_days_note"] or \
            "可能含机械配员" in org["person_days_note"]
        assert org["rows"][0]["person_days_from_org"] is False
        assert any("不许补数" in r for r in org["rules"])
        assert any("person_days" in r for r in org["rules"]), "工日真源规则必须进 rules"
        # 原始契约字段也照抄一份
        assert facts["organization_gaps"] == plan["meta"]["organization_gaps"]
        assert facts["scope_audit"] == plan["meta"]["scope_audit"]

    def test_标记缺失时追加确定性段落(self):
        plan = _plan()
        view = D._compute_view(plan)
        merged, added = D._ensure_org_section(BARE_LLM_HTML, plan, view)
        assert added is True
        assert D.ORG_FALLBACK_COMMENT in merged
        for m in D.ORG_MARKERS:
            assert m in merged, "追加后仍缺标记：%s" % m
        assert merged.rfind(D.ORG_FALLBACK_COMMENT) < merged.rfind("</body>")
        assert "模型只写了个壳" in merged, "模型原有内容不许丢"

    def test_标记齐全时一字不动(self):
        plan = _plan()
        view = D._compute_view(plan)
        rich = ("<html><body>施工组织口径 … 组织缺口 …</body></html>")
        merged, added = D._ensure_org_section(rich, plan, view)
        assert added is False and merged == rich

    def test_与既有DELIVERY_MARKERS互相独立(self):
        """既有回归门钉的是「三个旧标记齐 → 页面一字不动」，语义不能被新标记破坏。"""
        assert D.ORG_MARKERS and not (set(D.ORG_MARKERS) & set(D.DELIVERY_MARKERS))
        plan = _plan()
        view = D._compute_view(plan)
        old_only = "<html><body>依据 / 资源 工作面容量 主要机械峰值</body></html>"
        merged, added = D._ensure_org_section(old_only, plan, view)
        assert added is True, "旧标记齐不代表组织层段在"
        assert D._ensure_delivery_markers(old_only, plan, view)[1] == "agent"

    def test_追加失败时保持原页面不动(self, monkeypatch):
        plan = _plan()
        view = D._compute_view(plan)

        def _boom(*a, **kw):
            raise RuntimeError("org section exploded")

        monkeypatch.setattr(D, "_org_section_html", _boom)
        merged, added = D._ensure_org_section(BARE_LLM_HTML, plan, view)
        assert merged == BARE_LLM_HTML and added is False, "宁缺勿造"

    def test_编排路径最终产物带组织段与痕迹(self, tmp_deliverables):
        """① 页面只有旧标记（模型漏了组织层段）→ 追加组织段 + 留可诊断痕迹。

        这是真实场景：模型把 `DELIVERY_MARKERS` 那几个词写上了，却整段漏掉
        「工日 → 工期 是怎么来的」，用户那边看到的就是"计划自己说不清"。
        """
        plan = _plan()
        ctx = {}
        old_markers_only = ("<!DOCTYPE html><html lang='zh'><body>"
                            "<p>依据 / 资源　工作面容量　主要机械峰值</p></body></html>")
        llm = _FakeLLM(old_markers_only)
        path, used_agent = D.build_plan_html_agent(plan, llm, ctx=ctx)
        assert used_agent is True and llm.calls == 1
        h = Path(path).read_text(encoding="utf-8")
        for m in list(D.DELIVERY_MARKERS) + list(D.ORG_MARKERS):
            assert m in h, "LLM 丢段后仍缺标记：%s" % m
        assert h.count(D.ORG_FALLBACK_COMMENT) == 1
        assert "已追加确定性组织层段落" in " ".join(ctx.get("wbs_warnings") or [])
        # 旧标记齐备时**不该**再追加整段确定性口径（既有回归门的语义）
        assert D.DELIVERY_FALLBACK_COMMENT not in h

    def test_编排路径裸HTML时全部标记齐备(self, tmp_deliverables):
        plan = _plan()
        ctx = {}
        llm = _FakeLLM(BARE_LLM_HTML)
        path, used_agent = D.build_plan_html_agent(plan, llm, ctx=ctx)
        assert used_agent is True and llm.calls == 1
        h = Path(path).read_text(encoding="utf-8")
        for m in list(D.DELIVERY_MARKERS) + list(D.ORG_MARKERS):
            assert m in h, "LLM 丢段后仍缺标记：%s" % m
        assert D.ORG_FALLBACK_COMMENT not in h, "整段确定性追加里已含组织段，不必重复追加"

    def test_编排页面自带组织段时不重复追加(self, tmp_deliverables):
        plan = _plan()
        view = D._compute_view(plan)
        llm = _FakeLLM(BARE_LLM_HTML.replace(
            "<p>模型只写了个壳</p>",
            "<p>施工组织口径：工日 · 作业面数 · 每面人数 · 班次 → 工期</p>"
            "<p>组织缺口报告</p>"))
        ctx = {}
        path, used_agent = D.build_plan_html_agent(plan, llm, ctx=ctx)
        h = Path(path).read_text(encoding="utf-8")
        assert h.count(D.ORG_FALLBACK_COMMENT) == 0, "模型写全了就不该再追加"
        assert "施工组织口径" in h and "组织缺口" in h


# ══════════════════════ ⑨ 真计划数据渲染（不覆盖既有产物） ══════════════════════

def _real_plan():
    return json.loads(REAL_PLAN.read_text(encoding="utf-8"))


def _inject_contract_fields(plan):
    """把契约字段按**真计划实测证据**注入（上游组织层尚未合入时的渲染演练）。

    · 6.1.1.1 / 6.1.1.3 是同一批墙（1420 m² × 0.2 m = 284 m³）→ 重复建项证据；
    · 6.1.1.1 的人工工日取资源行 total_days 之和（306 工日：瓦工 270 + 司机 60? …实取）；
    · 单面人数上限的 381 行来自 KB 事实（478 行里 381 行 v1.max_labor != v2.crew_max）。
    """
    rd = plan.setdefault("resource_demand", {})
    tasks = {str(t.get("task_id")): t for t in (rd.get("tasks") or [])}
    if "6.1.1.1" in tasks:
        tasks["6.1.1.1"]["_organization"] = {
            "cadence_days": 7.0, "n_faces": 3, "crew_per_face": 20, "crew_total": 60,
            "shifts": 1, "eta": 0.8625, "effective_crew_total": 51.75,
            # 契约真源：工种工日（≠ 交付侧累计 360 = 瓦工 270 + 司机 60 + 信号工 30）
            "person_days": 306.0,
            # 契约自洽：进缺口报告 = 不可达 → feasible 必须为 false、不给 duration_days
            "feasible": False, "t_min_days": 4.4, "source": "cadence"}
    if "6.1.1.3" in tasks:
        tasks["6.1.1.3"]["_organization"] = {
            "cadence_days": 7.0, "n_faces": 2, "crew_per_face": 9, "crew_total": 18,
            "shifts": 1, "source": "preferred"}
    meta = plan.setdefault("meta", {})
    # 节拍落点 = `meta.boundary_conditions.cadence_days`（契约确定，不再是 meta.organization）
    bc = meta.setdefault("boundary_conditions", {})
    bc["cadence_days"] = 7.0
    bc["cadence_scope"] = "标准层"
    bc.setdefault("_source", {})["cadence_days"] = "user"
    meta["organization_gaps"] = [{
        "task_id": "6.1.1.1", "task_name": get_task_name(tasks.get("6.1.1.1")) or "ALC墙板安装",
        "trade": "瓦工", "person_days": 306.0, "cadence_days": 7.0, "n_needed": 5, "n_max": 3,
        "c_max": 20, "t_min_days": 4.4,
        "levers": ["放宽节拍到 9 天", "增加作业面到 5", "改工艺减少工日"]}]
    meta["scope_audit"] = {
        "duplicate_scopes": [{
            "kb_activity_id": "KB-ALC-001", "location": "1-1层 墙体",
            "task_ids": ["6.1.1.1", "6.1.1.3"],
            "evidence": "1420 m² × 0.2 m = 284 m³"}],
        "norm_row_spread": [{"kb_activity_id": "KB-MASON-7", "unit": "m³", "ratio": 1.8,
                             "samples": [{"source_code": "LN_781", "value": 0.943}]}],
        "cmax_review": {"count": 381, "sample": [
            {"kb_activity_id": "KB-1", "v1_max_labor": 20, "v2_crew_max": 42}]}}
    return plan


def get_task_name(t):
    return (t or {}).get("task_name")


@pytest.mark.skipif(not REAL_PLAN.exists(), reason="plans/ 是运行产物，真实计划不在仓库里")
class TestRealPlanRender:
    def test_真计划_无组织层字段时如实说来源未记录(self, tmp_deliverables):
        plan = copy.deepcopy(_real_plan())
        plan["plan_id"] = "zz_org_visible_plain"
        h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        assert "施工组织口径" in h and "组织缺口" in h
        assert D.ORG_NO_CADENCE in h, "真计划没有节拍字段 → 不许编一个节拍出来"
        assert D.ORG_UNRECORDED in h
        assert D.ORG_NO_GAP in h
        assert "均未核对" in h or "未提供审计核对数据" in h

    def test_真计划_注入契约字段后关键词齐全(self, tmp_deliverables):
        plan = _inject_contract_fields(copy.deepcopy(_real_plan()))
        plan["plan_id"] = "zz_org_visible_injected"
        h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        for kw in ("施工组织口径", "作业面数", "组织来源", "组织缺口", "疑似重复计量",
                   "上限待审", "选行离散", "1420 m² × 0.2 m = 284 m³", "381"):
            assert kw in h, "看板缺关键词：%s" % kw
        # C8①：旧口径（效率折减 η / 0.8625 / 有效班组）一个字都不许出现
        for banned in ("效率折减", "η", "0.8625", "有效班组"):
            assert banned not in h, banned
        # ① 节拍原文（契约落点 boundary_conditions）
        assert "主体节拍：标准层 7 天/层（来源：用户输入）" in h
        # ② 工日列真源 + 口径标注
        assert "306" in h, "person_days 真源必须进表（不许用含配员的 360）"
        assert "本工序工种工日" in h
        assert "可能含机械配员，与组织层口径可能不同" in h, "缺 person_days 的行要带这条标注"
        # ③ 契约自洽时不该报口径打架
        assert "疑似上游口径不一致" not in h
        from docx import Document
        doc = Document(D.build_plan_docx(plan))
        body = "\n".join(p.text for p in doc.paragraphs)
        grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
        assert "施工组织口径" in body and "疑似重复计量，请在人工门确认" in body
        assert "主体节拍：标准层 7 天/层（来源：用户输入）" in body
        assert "本工序工种工日" in body
        assert list(D.ORG_TABLE_HEADER) in grids
        # 既有产物目录没有被这次渲染碰到（测试全程走 tmp_deliverables）
        assert (tmp_deliverables / "计划_zz_org_visible_injected").exists()

    def test_真计划_注入不自洽口径时自检报警(self, tmp_deliverables):
        """同一任务既可行又进缺口报告 → 必须在真计划渲染里报「疑似上游口径不一致」。"""
        plan = _inject_contract_fields(copy.deepcopy(_real_plan()))
        plan["plan_id"] = "zz_org_visible_conflict"
        org = None
        for t in plan["resource_demand"]["tasks"]:
            if str(t.get("task_id")) == "6.1.1.1":
                org = t["_organization"]
        org["feasible"] = True
        org["duration_days"] = 6
        h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        assert "疑似上游口径不一致" in h
        assert "可达最短 4.4 天" in h
        assert "未擅自取舍" in h


# ══════════════════════ ⑤ 天数口径：下标 vs 天数 ══════════════════════

class TestDayCountCaliber:
    """`view["total_days"]` 是**末日下标**（0 基，687）；**天数** = 下标 + 1（688）。

    半开区间 → 闭区间那次修复（`finish = 开工 + (ef-1)`）之后，凡是"除天数"的地方
    都必须用 `total_day_count`。日均是最典型的一处：它是"物理量 ÷ 天数"，
    除以末日下标会系统性偏大 1/天数（688 天的计划偏大 0.15%）。
    """

    @staticmethod
    def _canon_plan():
        """把公共样例的 `overview` 调成**契约口径**：竣工 = 开工 + 天数 - 1。

        公共样例的 `overview` 是手写的旧口径（2028-10-11 → 2028-12-10 共 61 天，
        却写 `total_duration_days = 60`），这条测试钉的是**契约关系**本身，所以显式
        给一份自洽的日期：开工 2028-10-11、最晚竣工 2028-11-10（= 排程行里的最晚
        `finish_date`）→ 天数 31。
        """
        plan = _plan()
        ov = plan["overview"]
        ov["planned_start_date"] = "2028-10-11"
        ov["planned_end_date"] = "2028-11-10"
        ov["total_duration_days"] = 31
        return plan

    def test_天数等于日期跨度且等于总工期(self):
        import datetime
        plan = self._canon_plan()
        view = D._compute_view(plan)
        ov = plan["overview"]
        d0 = datetime.date.fromisoformat(ov["planned_start_date"])
        d1 = datetime.date.fromisoformat(ov["planned_end_date"])
        span = (d1 - d0).days + 1
        assert span == ov["total_duration_days"] == 31
        assert view["total_day_count"] == span, (view["total_day_count"], span)
        assert view["total_days"] + 1 == view["total_day_count"], "天数 = 末日下标 + 1"
        # 最晚竣工那天就是 planned_end_date ⇒ 下标 = span - 1
        assert view["total_days"] == span - 1

    def test_日均分母是天数不是末日下标(self, tmp_deliverables):
        from docx import Document
        plan = self._canon_plan()
        days = D._compute_view(plan)["total_day_count"]
        assert days == 31
        doc = Document(D.build_plan_docx(plan))
        grid = None
        for t in doc.tables:
            if [c.text for c in t.rows[0].cells][:3] == ["工种", "累计人·日", "日均"]:
                grid = t
                break
        assert grid is not None, "「工种 / 累计人·日 / 日均」表还在"
        rows = [[c.text for c in r.cells] for r in grid.rows[1:]]
        assert rows, "至少有瓦工一行"
        for trade, cum, avg in rows:
            cum_f, avg_f = float(cum), float(avg)
            assert avg_f == round(cum_f / days, 1), (trade, cum, avg, days)
            # 用末日下标（30）会得到另一个数 —— 说明这条断言真的钉住了分母
            assert avg_f != round(cum_f / (days - 1), 1), (trade, cum, avg)

    def test_总工期回退值是天数不是下标(self):
        """`overview` 缺 `total_duration_days` 时，回退值必须是天数（31），不是下标（30）。"""
        plan = self._canon_plan()
        plan["overview"].pop("total_duration_days")
        view = D._compute_view(plan)
        facts = D._facts_bundle(plan, view)
        assert facts["key_numbers"]["total_duration_days"] == 31
        assert view["total_day_count"] == 31


# ══════════════════════ ⑥ 班组真源 / 峰值两条路径 ══════════════════════

def _crew_truth_plan():
    """在公共样例上模拟资源层 WS6 的落点：组织层给班组的行 + 未削峰。

    依据 `终版修改_接口冻结.md` **§6（每面人数上限单源，WS4 定源 / WS2 消费）**：
    组织层的工种曲线（`crew_per_face`，来源标记 `crew_source="org_curve"`）是**唯一来源**，
    资源层不得对同一工序再产出第二个更小的上限 —— 正常路径下
    `cap_per_face == org.crew_per_face`（这里 20）、`cap_total == crew_total`（60），
    字段 `resource_cap_below_org` 保留但**正常为 False**。
    旧计划落盘的两套上限不同源那种数据另由 `_crew_truth_plan_legacy()` 复刻。
    """
    plan = _plan()
    rd = plan["resource_demand"]
    # 6.1.1.1：班组来自组织层（3 面 × 20 人 = 60 人）；§6 之后资源层认组织层那一版，
    # 单面上限就是 20（不再有第二个"按本段工程量算出的 8 人"上限）。
    rd["tasks"][0]["_organization_crew"] = {
        "trade": "瓦工", "crew_total": 60, "n_faces": 3, "crew_per_face": 20,
        "cap_per_face": 20, "cap_total": 60, "resource_cap_below_org": False,
        "capped": False, "crew_source": "org_curve",
        "basis": "总（跨 3 个作业面）"}
    rd["tasks"][0]["_resource_source"] = {
        "瓦工": {"origin": "org_layer", "ref": "施工组织层：3 面 × 20 人/面 = 60 人（单面上限 20 已由组织层校验）"}}
    rd["tasks"][0]["_peak_shaving_skipped"] = {
        "reason": "班组真源是施工组织层", "declared_trade_limit": 45, "crew_total": 60}
    # 6.1.1.3：仍走工作面容量标定公式（1 条顶到上限）
    rd["tasks"][1]["_workface_note"] = "按本施工段工程量标定"
    rd["tasks"][1]["_workface_capped"] = [{
        "resource": "瓦工", "original_per_day": 36, "capped_per_day": 7,
        "reason": "工作面容量封顶"}]
    return plan


def _crew_truth_plan_legacy():
    """**旧计划**落盘的数据：两套「每面上限」不同源（`resource_cap_below_org=True`）。

    合同 §6 之前的口径（资源层按本段工程量另算一个更小的单面上限，如 8 人）。改造后
    正常路径不再产出这种数据，但**旧计划仍会被交付**，交付侧照旧逐条如实说明。
    """
    plan = _crew_truth_plan()
    oc = plan["resource_demand"]["tasks"][0]["_organization_crew"]
    oc.update({"cap_per_face": 8, "cap_total": 24, "resource_cap_below_org": True})
    plan["resource_demand"]["tasks"][0]["_resource_source"] = {
        "瓦工": {"origin": "org_layer",
                 "ref": "施工组织层：3 面 × 20 人/面 = 60 人（单面上限 8 已由组织层校验）"}}
    return plan


class TestCrewTruthSources:
    def test_两个真源分开报_组织层那批不说成标定公式(self):
        plan = _crew_truth_plan()
        ws = D._workface_summary(plan)
        assert ws["org_count"] == 1 and ws["applied"] == 1 and ws["capped"] == 1, ws
        txt = D._workface_sentence(plan)
        assert txt.startswith("共 1 条任务的班组来自施工组织层"), txt
        assert "3 面 × 20 人/面 = 60 人" in txt, txt
        # C11：旧文案「节拍 × 作业面数 / 节拍决定天数」已改 —— 节拍只作对比参考
        assert "作业面数 × 每面人数 = 班组总人数" in txt, txt
        assert "节拍仅作对比参考、不参与任何计算" in txt, txt
        assert "节拍决定天数" not in txt, txt
        assert "单面上限已由组织层校验" in txt, txt
        assert "共 1 条任务的班组人数按本施工段工程量用标定公式算出" in txt, txt
        assert "其中 1 条顶到上限" in txt, txt
        assert "工作面容量标定来源：ai_estimate / LOW" in txt, txt

    def test_两套每面上限不同源时逐条说明(self):
        """合同 §6 之前的旧计划数据：交付侧仍要逐条如实说明（新的单源口径见下一个测试）。"""
        plan = _crew_truth_plan_legacy()
        ws = D._workface_summary(plan)
        assert ws["below_org_count"] == 1, ws
        lines = D._workface_below_org_lines(ws)
        assert len(lines) == 1, lines
        line = lines[0]
        assert "本行以施工组织层为准" in line, line
        assert "资源层按本段工程量算出的单面上限为 8 人" in line, line
        assert "低于组织层 3 面后的 60 人" in line, line
        assert "两套「每面上限」目前不同源，已在资源行逐条留痕" in line, line

    def test_单源后正常路径不再有第二个每面上限(self):
        """`终版修改_接口冻结.md` §6：`cap_per_face == org.crew_per_face`、`cap_total == crew_total`，
        `resource_cap_below_org` 正常为 False → 交付侧不再出现"两套每面上限不同源"的说法。"""
        plan = _crew_truth_plan()
        oc = plan["resource_demand"]["tasks"][0]["_organization_crew"]
        assert oc["cap_per_face"] == oc["crew_per_face"] == 20, oc
        assert oc["cap_total"] == oc["crew_total"] == 60, oc
        assert oc["resource_cap_below_org"] is False, oc
        ws = D._workface_summary(plan)
        assert ws["org_count"] == 1 and ws["below_org_count"] == 0, ws
        assert D._workface_below_org_lines(ws) == [], ws
        assert D._workface_below_org_lead(ws) == "", ws
        assert "两套「每面上限」" not in D._workface_sentence(plan)
        # 组织层那批仍然照旧报告（不能因为不再有第二个上限，就把这一批也吞掉）
        assert "共 1 条任务的班组来自施工组织层" in D._workface_sentence(plan)

    def test_未削峰不许静默(self):
        plan = _crew_truth_plan()
        ws = D._workface_summary(plan)
        assert ws["peak_shaving_count"] == 1, ws
        txt = D._workface_peak_shaving_sentence(ws)
        assert "本计划有 1 条任务未按工种总人数二次削峰" in txt, txt
        assert "班组真源是施工组织层" in txt, txt
        assert "瓦工" in txt and "60" in txt and "45" in txt, txt

    def test_没有这些键时一个字都不加(self):
        plan = _plan()                       # 公共样例没有任何 _organization_crew / _peak_shaving
        ws = D._workface_summary(plan)
        assert ws["org_count"] == 0 and ws["below_org_count"] == 0
        assert ws["peak_shaving_count"] == 0
        assert D._workface_below_org_lines(ws) == []
        assert D._workface_peak_shaving_sentence(ws) == ""
        assert "_organization_crew" not in D._workface_sentence(plan)

    def test_看板与Word都印出这三段(self, tmp_deliverables):
        plan = _crew_truth_plan()
        plan["plan_id"] = "zz_crew_truth"
        h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        from docx import Document
        doc = Document(D.build_plan_docx(plan))
        body = "\n".join(p.text for p in doc.paragraphs)
        for txt in ("班组来自施工组织层", "未按工种总人数二次削峰"):
            assert txt in h, "看板缺：%s" % txt
            assert txt in body, "Word 缺：%s" % txt
        # §6（每面人数上限单源）之后，「两套每面上限不同源」只存在于旧计划数据里 ——
        # 交付侧对那种数据仍要逐条印出来（用 legacy 样例验证，别把这条门一起删掉）。
        legacy = _crew_truth_plan_legacy()
        legacy["plan_id"] = "zz_crew_truth_legacy"
        hl = Path(D.build_plan_html(legacy)).read_text(encoding="utf-8")
        dl = Document(D.build_plan_docx(legacy))
        bodyl = "\n".join(p.text for p in dl.paragraphs)
        assert "本行以施工组织层为准" in hl, "看板缺：旧计划的两套每面上限逐条说明"
        assert "本行以施工组织层为准" in bodyl, "Word 缺：旧计划的两套每面上限逐条说明"


class TestPeakCurveDifference:
    """本页曲线峰值 vs 调度器 `curve_peak_manpower`：差额必须是**算出来的**场地级配员。"""

    @staticmethod
    def _plan_with_site_crew():
        """任务级峰值 18 人（6.1.1.1 与 6.1.1.3 并行：瓦工各 9 人），场地级再叠
        司机 2 + 信号工 1 → 本页 21；调度器口径只报任务级的 18。"""
        plan = _plan()
        plan["resource_plan"]["curve_peak_manpower"] = 18
        for t in plan["resource_demand"]["tasks"]:
            if str(t.get("task_id")) == "6.1.1.1":
                t["_site_equipment"] = [{"name": "塔吊", "quantity": 1, "crew": {"司机": 2}}]
            if str(t.get("task_id")) == "6.1.1.3":
                t["_site_equipment"] = [{"name": "施工电梯", "quantity": 1, "crew": {"信号工": 1}}]
        return plan

    def test_差额归因到场地级配员且两条独立计算一致(self):
        plan = self._plan_with_site_crew()
        view = D._compute_view(plan)
        pcd = D._peak_curve_diff(plan, view)
        assert pcd is not None and pcd["attributed"] is True, pcd
        assert pcd["curve_peak"] == 18, pcd
        assert pcd["view_peak"] == view["peak_total"] == 21, pcd
        # 两条独立计算：① 摘掉场地级逐日贡献后重算；② 峰值日的场地级贡献
        assert pcd["task_only_peak"] == 18 == pcd["curve_peak"], pcd
        assert pcd["site_peak"] == 3, pcd
        assert pcd["view_peak"] - pcd["task_only_peak"] == pcd["site_peak"], pcd
        assert pcd["site_trades"] == {"司机": 2, "信号工": 1}, pcd["site_trades"]
        s = pcd["sentence"]
        assert "本页「峰值人数 21 人」" in s and "任务级配员峰值 18 人" in s, s
        assert "场地级设备配员（司机 2 人、信号工 1 人" in s, s
        assert "两者不是同一个数" in s, s

    def test_两数一致时不出一句话(self):
        plan = _plan()
        plan["resource_plan"]["curve_peak_manpower"] = 18     # 与 view 峰值一致
        view = D._compute_view(plan)
        assert view["peak_total"] == 18
        assert D._peak_curve_diff(plan, view) is None

    def test_对不上调度器口径时不硬归因(self):
        plan = self._plan_with_site_crew()
        plan["resource_plan"]["curve_peak_manpower"] = 7      # 摘掉场地级也仍是 18 ≠ 7
        view = D._compute_view(plan)
        pcd = D._peak_curve_diff(plan, view)
        assert pcd is not None and pcd["attributed"] is False, pcd
        assert "未完全" in pcd["sentence"], pcd["sentence"]
        assert "仍与调度器差" in pcd["sentence"], pcd["sentence"]

    def test_上游缺键时一个字都不加(self):
        plan = self._plan_with_site_crew()
        plan["resource_plan"].pop("curve_peak_manpower")
        view = D._compute_view(plan)
        assert D._peak_curve_diff(plan, view) is None

    def test_看板与Word都印口径句(self, tmp_deliverables):
        plan = self._plan_with_site_crew()
        plan["plan_id"] = "zz_peak_curve"
        h = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        assert "余下 3 人来自场地级设备配员" in h, "看板缺峰值差额归因句"
        from docx import Document
        doc = Document(D.build_plan_docx(plan))
        body = "\n".join(p.text for p in doc.paragraphs)
        assert "余下 3 人来自场地级设备配员" in body, "Word 缺峰值差额归因句"
        facts = D._facts_bundle(plan, D._compute_view(plan))
        assert facts["peak_curve_diff"]["site_peak"] == 3


# ══════════════════════ ⑦ 图表侧天数口径（与看板同源） ══════════════════════

class TestChartDayCaliber:
    """`echarts_page` 也必须用**含首尾的天数**：图表说 687、看板说 688 就是两套口径。"""

    @staticmethod
    def _chart_plan():
        """一条 `duration_days` 与日期跨度**对不上**的行（复刻真计划 `1.5.1`：字段 1 天、
        日期跨度 67 天）—— 甘特气泡写的是这根条的宽度，不是那个字段。"""
        plan = _plan()
        ov = plan["overview"]
        ov["planned_start_date"] = "2028-10-11"
        ov["planned_end_date"] = "2028-12-16"       # 67 天
        ov["total_duration_days"] = 67
        plan["all_tasks_schedule"] = [
            {"task_id": "1.5.1", "task_name": "混凝土运输",
             "start_date": "2028-10-11", "finish_date": "2028-12-16",
             "duration_days": 1, "assigned_resources": {"普工": 30}},
            {"task_id": "1.5.2", "task_name": "砂运输",
             "start_date": "2028-10-11", "finish_date": "2028-10-17",
             "duration_days": 7, "assigned_resources": {"普工": 4}},
        ]
        return plan

    def test_图表天数与看板天数同源(self):
        from pipeline.nodes import echarts_page as E
        plan = self._chart_plan()
        ov = plan["overview"]
        end = E._as_date(ov["planned_end_date"])
        start = E._as_date(ov["planned_start_date"])
        chart_days = (end - start).days + 1
        assert chart_days == ov["total_duration_days"] == 67
        view = D._compute_view(plan)
        assert view["total_day_count"] == chart_days, "看板天数与图表天数必须相等"
        opt = E._build_gantt(plan, None)
        ticks = opt["__dateTicks"]
        # 末刻度 = 末日 → 百分比 = (end-start).days / 天数
        assert ticks[-1][0] == round((end - start).days / chart_days * 100.0, 4), ticks[-1]
        assert ticks[0][1] == "10-11" and ticks[-1][1] == "12-16", (ticks[0], ticks[-1])

    def test_每根条跨度等于日期跨度而不是排程行的duration_days(self):
        from pipeline.nodes import echarts_page as E
        plan = self._chart_plan()
        start = E._as_date(plan["overview"]["planned_start_date"])
        total = 67
        opt = E._build_gantt(plan, None)
        data = opt["series"][0]["data"]
        spans = {}
        for row in data:
            spans[row[4]] = row
        # 1.5.1：duration_days=1，但日期跨度 67 → 条形与气泡都必须按 67 天
        r = spans["1.5.1"]
        assert r[7] == 67, "甘特气泡的天数必须等于条形宽度（日期跨度）"
        assert r[2] == round(max(67 / total * 100.0, 0.4), 4), r[2]
        # 1.5.2：7 天，两处一致
        r2 = spans["1.5.2"]
        assert r2[7] == 7 and r2[2] == round(max(7 / total * 100.0, 0.4), 4), r2
        # 偏移量按 es（含首尾的天数口径下 offset 不变）
        assert r[1] == 0.0 and r2[1] == 0.0
        assert [(x[2] - start).days for x in E._gantt_source(plan, start)] == [0, 0]

    def test_最短条的最小可见宽度只影响显示百分比(self):
        from pipeline.nodes import echarts_page as E
        plan = self._chart_plan()
        opt = E._build_gantt(plan, None)
        for row in opt["series"][0]["data"]:
            assert row[2] >= 0.4, "条形至少 0.4% 宽（保证看得见）"

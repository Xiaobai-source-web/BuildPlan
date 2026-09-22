# -*- coding: utf-8 -*-
"""交付物「说实话」三件套的回归护栏（第 40 轮）。

背景 —— 用户真跑了一份计划 `plan_sample3_after_fix`，产物在磁盘上，实测出三条缺陷：

① **Word 正文自相矛盾**：`施工进度计划.docx` 里 `单位换算按 AI 假定` 出现 **18** 次
   （这 18 条确实算出了班组与工日），可第 5 节 `5. 单位与定额降级清单（已降级为「仅参考」）`
   下面又列着同一批 18 条 `6.1.1.1 … 6.1.18.1`（`定额单位 m³ 与工程量单位 m² 不一致`）。
   同一份文档里两个互相矛盾的结论 → 用户读到的就是"这计划自己打架"。
   修法：判据用任务级 `resource_demand.tasks[*]._unit_assumed`（不按任务名硬编码），
   这类任务从降级清单**排除**，改列到 `5b. 已按 AI 假定换算（非降级）`。

② **`振捣工`（人）被当成机械**：`resource_plan.equipment_peak` 里含 `"振捣工": 1`
   （KB `Equipment_Crew_Mapping`：`混凝土振捣器 → 振捣工1人`）。交付侧 `LABOR` /
   `MACHINE_CREW` 里都不含它 → `_is_labor()` 判 false → 当机械。
   修法：补进 `MACHINE_CREW`（两处同源集合），并加一条**全表解析** `crew_composition`
   的回归 —— 凡是会被误判成机械的角色名一律不许漏。

③ **看板是 LLM 编排的，整段丢"说实话"内容**：实测
   `输出结果\\计划_plan_sample3_after_fix\\计划看板.html`（1128916 字节）里
   `工期(天·排程)` 0 次、`WBS 目标` 0 次、`依据 / 资源` 0 次、`工作面容量` 0 次、
   `主要机械峰值` 0 次；而确定性 `build_plan_html` 同一份计划全都有。
   修法：(a) `_facts_bundle` 补齐口径字段（峰值来源 / 申报峰值 / 工作面容量 /
   `meta.equipment_binding` / 任务级依据）；(b) `build_plan_html_agent` 里加**结构性保证**
   —— 缺关键标记就把确定性口径段追加进模型页面（做不到则整体回退确定性渲染）。

运行：python -m pytest backend/tests/test_delivery_truth_guard.py -v
"""

import copy
import re
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from pipeline import config                          # noqa: E402
from pipeline.nodes import crew_bind as CB           # noqa: E402
from pipeline.nodes import delivery as D             # noqa: E402
from pipeline.nodes import plan_assembler as PA      # noqa: E402

REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_fix.json"

# KB 里配员表的兜底快照（真库不在时用它；值与 `BuildPlan_KB/kb.db` 实测一致）。
# **不把测试绑死在本机 kb.db 上** —— 库不在就优雅跳过或用这份固定小样例。
KB_CREW_FALLBACK = {"司机1名", "司机1名+信号工1名", "振捣工1人",
                    "操作工1名", "泵工1人+辅助1人"}


# ══════════════════════ 公共样例：一份最小计划 ══════════════════════

def _plan():
    """一份最小计划：1 条「已按 AI 假定换算」+ 1 条 AI 经验估算定额 + 1 条真降级。

    · `6.1.1.1`：`_unit_assumed`（算出来了，换算用了写明的 AI 假定）+ 班组瓦工 9 人；
    · `2.1.4`：绑定层按**旧口径**标记「AI 估算定额（只作参考，不用来算班组）」
      —— 政策变更（2026-09-20）后 AI 经验估算定额照用，交付物必须换成
      「AI 经验估算定额（无规范依据，待审）」，**绝不许复述旧口径**；
    · `9.2.2`：单位族不一致且不可换算（**真降级**，仍写「工期沿用 WBS 估算，未计算班组」）。
    """
    leaves = [
        {"id": "6.1.1.1", "name": "1-1层 ALC墙板安装", "duration_days": 3,
         "quantity": 1420.0, "unit": "m²", "work_type": "砌筑工程",
         # 单位不一致，但资源层已按写明假定换算 → 该进 5b，不该进降级清单
         # 换算参数的**来源**键（P3 留痕）：这里是 AI 猜的 → 交付物必须写「AI估算换算参数」
         "norm_binding": {"unit": "工日/m³", "source_code": "LN_781",
                          "ctx_source": "ai_estimate",
                          "condition_text": "加气混凝土砌块，≤200mm"}},
        {"id": "2.1.4", "name": "截（凿）桩头", "duration_days": 5,
         "quantity": 120.0, "unit": "根", "work_type": "桩基工程",
         "norm_binding": {"unit": "工日/m³", "source_code": "LN_001"}},
        {"id": "9.2.2", "name": "路基碾压（压路机碾压）", "duration_days": 4,
         "quantity": 3000.0, "unit": "m²", "work_type": "道路工程",
         "norm_binding": {"unit": "工日/m³", "source_code": "LN_002"}},
    ]
    sched = [
        {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装",
         "start_date": "2028-10-11", "finish_date": "2028-11-10", "duration_days": 3,
         "assigned_resources": {"瓦工": 9}},
        {"task_id": "2.1.4", "task_name": "截（凿）桩头",
         "start_date": "2028-10-11", "finish_date": "2028-10-15", "duration_days": 5,
         "assigned_resources": {}},
        {"task_id": "9.2.2", "task_name": "路基碾压（压路机碾压）",
         "start_date": "2028-10-16", "finish_date": "2028-10-19", "duration_days": 4,
         "assigned_resources": {}},
    ]
    rd_tasks = [
        {"task_id": "6.1.1.1", "task_name": "1-1层 ALC墙板安装", "quantity": 1420.0,
         "planned_duration_days": 31,
         "resources": {"瓦工": {"per_day": 9, "total_days": 279.0}},
         "_unit_assumed": "按 AI 假定墙厚 200mm 换算：1420 m² × 0.2 m = 284 m³；"
                          "定额档位 LN_781（0.943 工日/单位）",
         "_resource_source": {"瓦工": {"origin": "kb", "ref": "LN_781"}},
         "_workface_note": "按本施工段工程量 1420 m² 用标定公式算出班组 9 人",
         "_norm_applied": {"mode": "labor", "source_code": "LN_781"}},
        # ⚠ 政策变更（2026-09-20）：这行是**旧计划落盘数据的原样复刻**（旧口径的绑定层
        # 标记原文）。保留它正是为了让"交付物不许复述旧口径"这条回归有真实输入可测。
        {"task_id": "2.1.4", "task_name": "截（凿）桩头", "quantity": 120.0,
         "planned_duration_days": 5, "resources": {},
         "_norm_flagged": "AI 估算定额（只作参考，不用来算班组）"},
        # 真降级：单位不一致且不可换算（**不是** AI 来源）→ 仍写「无可用定额…未计算班组」。
        {"task_id": "9.2.2", "task_name": "路基碾压（压路机碾压）", "quantity": 3000.0,
         "planned_duration_days": 4, "resources": {},
         "_norm_flagged": "单位不可用：单位不一致且不可换算：任务「m²」（area） vs "
                          "定额分母「m³」（volume）；「m²」与「m³」之间没有量纲换算依据"},
    ]
    return {
        "plan_id": "plan_truth_guard_test",
        "overview": {"project_name": "交付口径测试", "total_duration_days": 60,
                     "planned_start_date": "2028-10-11", "planned_end_date": "2028-12-10",
                     "critical_path_length": 1},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "6.1", "name": "砌体", "sub_packages": leaves}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 60, "critical_path": ["6.1.1.1"],
                       "schedule": [{"task_id": "6.1.1.1", "es": 0, "ef": 30},
                                    {"task_id": "2.1.4", "es": 0, "ef": 4},
                                    {"task_id": "9.2.2", "es": 5, "ef": 8}]},
        "all_tasks_schedule": sched,
        "critical_path_tasks": [dict(sched[0])],
        "key_milestones": [{"name": "开工", "date": "2028-10-11", "task_id": "6.1.1.1",
                            "description": "开工"}],
        "resource_demand": {"tasks": rd_tasks},
        "resource_plan": {"total_manpower_days": 279.0, "peak_manpower": 9,
                          "peak_manpower_source": "resource_curve",
                          "curve_peak_manpower": 9,
                          "declared_peak_manpower": 180,
                          "declared_peak_manpower_source": "model",
                          "equipment_peak": {"混凝土振捣器": 1},
                          "machine_crew_peak": {"振捣工": 1},
                          "material_summary": []},
        "meta": {"audit_status": "未审计", "norm_coverage": {"total": 3, "bound": 1}},
        "report": "# 报告",
    }


@pytest.fixture()
def tmp_deliverables(tmp_path, monkeypatch):
    """把交付物目录指向 tmp，避免测试往 `输出结果/` 里写东西。"""
    d = tmp_path / "deliverables"
    d.mkdir()
    monkeypatch.setattr(config, "DELIVERABLES_DIR", d)
    monkeypatch.setattr(D.config, "DELIVERABLES_DIR", d)
    return d


def _docx_text(path):
    """把 docx 当 zip 读 `word/document.xml`，去掉标签 → 纯文本（复现命令同款）。"""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", "", xml)


# ══════════════════════ ① 降级清单 vs 已假定换算 ══════════════════════

class TestDegradedListExcludesAssumed:
    def test_已按假定换算的任务不进降级清单(self):
        rows, total = D._norm_degradations(_plan(), cap=0)
        ids = [r["task_id"] for r in rows]
        assert "6.1.1.1" not in ids, "算出了资源的任务不许再列进「仅参考」清单"
        assert ids == ["2.1.4", "9.2.2"], ids
        assert total == 2

    def test_已假定换算任务单列在5b节(self):
        rows, total = D._norm_assumed_rows(_plan(), cap=0)
        assert [r["task_id"] for r in rows] == ["6.1.1.1"]
        assert total == 1
        assert "墙厚 200mm" in rows[0]["reason"], "假定原文要原样带出来"

    def test_两份清单互斥(self):
        deg, _ = D._norm_degradations(_plan(), cap=0)
        asm, _ = D._norm_assumed_rows(_plan(), cap=0)
        assert not ({r["task_id"] for r in deg} & {r["task_id"] for r in asm})

    def test_判据是任务级字段不是任务名(self):
        """把任务名换成完全不同的字，判据仍然成立（不按名字硬编码）。"""
        plan = _plan()
        plan["resource_demand"]["tasks"][0]["task_name"] = "随便改个名字"
        deg, _ = D._norm_degradations(plan, cap=0)
        assert "6.1.1.1" not in [r["task_id"] for r in deg]

    def test_Word第5节不含已假定任务_第5b节含(self):
        from docx import Document
        plan = _plan()
        doc = Document(D.build_plan_docx(plan))
        # 降级清单以 Word 表格落地：找到「任务 ID / 任务 / 原因」那张表
        grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
        assert ["任务 ID", "任务", "原因"] in grids, grids
        idx = grids.index(["任务 ID", "任务", "原因"])
        deg_cells = "\n".join(c.text for r in doc.tables[idx].rows for c in r.cells)
        assert "2.1.4" in deg_cells and "9.2.2" in deg_cells
        assert "6.1.1.1" not in deg_cells, "已假定换算的任务不该出现在降级表里"
        # 5b 节单独一张表（P3/换算参数来源分流后加了「换算参数来源」列）
        _asm_header = ["任务 ID", "任务", "换算参数来源", "换算过程与结果（依据列原文）"]
        assert _asm_header in grids, grids
        i2 = grids.index(_asm_header)
        asm_cells = "\n".join(c.text for r in doc.tables[i2].rows for c in r.cells)
        assert "6.1.1.1" in asm_cells and "按 AI 假定墙厚 200mm 换算" in asm_cells
        # 换算参数的**来源**必须逐条写出来（`ctx_source == ai_estimate` → AI 估算换算参数）
        assert "AI估算换算参数" in asm_cells, asm_cells
        # 正文里也必须有「非降级」的口径说明；**本段**不许泄漏 markdown 星号
        # （计划自带的 report 里有 markdown，那是 `_add_md_lines` 的职责范围，不在本判据内）
        paras = [p.text for p in doc.paragraphs]
        body = "\n".join(paras)
        assert "5b. 已按 AI 假定换算（非降级）" in body
        k = paras.index("5b. 已按 AI 假定换算（非降级）")
        mine = "\n".join(paras[k:k + 4])
        assert "**" not in mine, mine

    def test_看板确定性卡片与Word同口径(self):
        plan = _plan()
        rows = D._norm_assumed_rows(plan)[0]
        title = D.assumed_section_title(rows)
        h = D._norm_lists_card_html(plan)
        assert D.NORM_DEGRADED_TITLE in h
        assert title in h
        # 降级卡片里不许出现已假定换算的任务号
        deg = h.split(title)[0]
        assert "6.1.1.1" not in deg

    def test_换算参数来源按ctx_source分流(self):
        """P3/D5：来源是 `norm_condition`（定额行适用条件档位）时**不许说成 AI**。"""
        plan = _plan()
        leaf = plan["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]
        leaf["norm_binding"]["ctx_source"] = "norm_condition"
        leaf["norm_binding"]["unit_assumption"] = {
            "thickness_m": 0.2, "source": "norm_condition",
            "note": "厚度取 0.2 m（200mm）：来自定额行适用条件的厚度档位（加气混凝土砌块，≤200mm）"}
        rows = D._norm_assumed_rows(plan)[0]
        label = D.UNIT_ASSUMPTION_SOURCE_LABELS["norm_condition"]
        assert rows[0]["source"] == "norm_condition"
        assert rows[0]["source_label"] == label
        assert "非 AI 估算" in label and "AI" not in label.replace("非 AI 估算", "")
        assert "≤200mm" in rows[0]["evidence"], rows[0]
        h = D._norm_lists_card_html(plan)
        assert label in h and "≤200mm" in h
        assert D.assumed_section_title(rows) == "5b. 已按写明换算参数换算（非降级）"
        assert "AI 假定换算" not in D.assumed_section_foot(rows)
        # 来源键缺失 → 如实写「换算参数来源未记录」，**绝不默认归到 AI**
        plan2 = _plan()
        plan2["wbs"]["phases"][0]["work_packages"][0]["sub_packages"][0]["norm_binding"].pop(
            "ctx_source")
        r2 = D._norm_assumed_rows(plan2)[0][0]
        assert r2["source"] == "" and r2["source_label"] == D.UNIT_ASSUMPTION_SOURCE_UNRECORDED
        assert "AI估算换算参数" not in r2["source_label"]

    def test_交付物里不许再出现旧政策的AI口径(self, tmp_deliverables):
        """政策变更（用户 2026-09-20）：AI 经验估算定额照用、与真人定额同等参与计算。

        旧政策那句「只作参考，不用来算班组」从今天起是**假话** —— fixture 里
        `2.1.4` 的 `_norm_flagged` 就是旧计划落盘的原文，交付物必须换文案，
        一个字都不许把它复述出来（看板与 Word 同一真源，两处都要查）。
        """
        forbidden = "只作参考，不用来算班组"
        from docx import Document
        plan = _plan()
        html = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        assert forbidden not in html, "看板复述了旧政策的 AI 口径"
        assert D.AI_NORM_LABEL in html, "看板必须逐条标出 AI 经验估算定额"
        doc = Document(D.build_plan_docx(plan))
        cells = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
        body = "\n".join(p.text for p in doc.paragraphs)
        assert forbidden not in (cells + body), "Word 复述了旧政策的 AI 口径"
        assert D.AI_NORM_LABEL in (cells + body)
        # 真降级那条的措辞仍然在（政策变更的"不能省"的另一半）
        assert "工期沿用 WBS 估算，未计算班组" in cells


@pytest.mark.skipif(not REAL_PLAN.exists(), reason="plans/ 是运行产物，真实计划不在仓库里")
def _real_plan():
    import json
    return json.loads(REAL_PLAN.read_text(encoding="utf-8"))


class TestRealPlanDegradationSplit:
    """真计划实测：25 条候选 = 18 条已按 AI 假定换算（ALC 墙板）+ 7 条真降级。

    数字是实测出来的（`_norm_degradations(cap=0)` → 25 行，其中 18 行带
    `_unit_assumed`）。换一份计划这些数都会变，所以判据写成"两边都从计划里取"。
    """

    def test_真计划_两份清单互斥且加起来等于原候选集(self):
        plan = _real_plan()
        deg, dtotal = D._norm_degradations(plan, cap=0)
        asm, atotal = D._norm_assumed_rows(plan, cap=0)
        assert atotal == 18, atotal
        assert dtotal == 7, dtotal
        assert not ({r["task_id"] for r in deg} & {r["task_id"] for r in asm})
        # 已假定换算的任务，判据全部来自任务级字段
        assumed_ids = D._unit_assumed_ids(plan)
        assert {r["task_id"] for r in asm} == assumed_ids

    def test_真计划_Word里18条不再同时出现在降级清单(self, tmp_deliverables):
        from docx import Document
        p = copy.deepcopy(_real_plan())
        p["plan_id"] = "zz_truth_guard_verify"
        try:
            doc = Document(D.build_plan_docx(p))
            grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
            body = "\n".join(pp.text for pp in doc.paragraphs)
            i = grids.index(["任务 ID", "任务", "原因"])
            deg_cells = "\n".join(c.text for r in doc.tables[i].rows for c in r.cells)
            assert "共 25 条" not in body, "25 是改动前的候选总数，不该再出现"
            assert "6.1.1.1" not in deg_cells, "18 条 ALC 不该在降级清单里"
            # 5b 段内有总数页脚：18 条不被静默吞掉
            assert "共 18 条，此处列出前 10 条" in body, body[-2000:]
            j = grids.index(["任务 ID", "任务", "换算参数来源", "换算过程与结果（依据列原文）"])
            asm_cells = "\n".join(c.text for r in doc.tables[j].rows for c in r.cells)
            assert "6.1.1.1" in asm_cells and "1-1层 ALC墙板安装" in asm_cells
        finally:
            shutil.rmtree(D._plan_dir(p), ignore_errors=True)


# ══════════════════════ ② 机械配员是「人」不是设备 ══════════════════════

def _kb_crew_roles():
    """从 `KB.Equipment_Crew_Mapping.crew_composition` 解析出**全部角色名**（只读）。

    优先真库；库不在 → 用 `KB_CREW_FALLBACK`（与实测值一致的固定小样例）。
    返回 (roles:set, source:str)。
    """
    db = Path(config.KB_DB_PATH)
    if not db.exists():
        roles = set()
        for txt in KB_CREW_FALLBACK:
            roles |= set(CB.parse_crew_composition(txt))
        return roles, "fallback"
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    try:
        cur = con.execute("SELECT crew_composition FROM Equipment_Crew_Mapping")
        comps = [r[0] for r in cur.fetchall()]
    finally:
        con.close()
    roles = set()
    for txt in comps:
        roles |= set(CB.parse_crew_composition(txt))
    return roles, "kb"


class TestMachineCrewRolesArePeople:
    def test_振捣工是人不是机械(self):
        assert D._is_labor("振捣工"), "振捣工是机械配员（KB: 混凝土振捣器 → 振捣工1人）"
        assert PA.MACHINE_CREW >= {"振捣工"}, "resource_plan 侧的集合也要同步"
        assert not D._is_labor("混凝土振捣器"), "振捣器才是机械"

    def test_KB配员表里所有角色都被判为人(self):
        roles, source = _kb_crew_roles()
        assert roles, "配员表解析不出任何角色（source=%s）" % source
        wrong = sorted(r for r in roles if not D._is_labor(r))
        assert not wrong, "这些配员角色被误判为机械（source=%s）：%s" % (source, wrong)

    def test_KB配员角色不进equipment_peak(self):
        roles, source = _kb_crew_roles()
        fake = {r: 3 for r in sorted(roles)}
        rd = {"tasks": [{
            "task_id": "T1",
            "resources": dict(fake, **{"混凝土振捣器": {"per_day": 3, "total_days": 6}}),
        }]}
        sched = {"T1": {"es": 0, "ef": 5}}
        eq, _ = PA._daily_peak(
            rd, sched, lambda n: n not in PA.LABOR_NAMES and n not in PA.MACHINE_CREW)
        crew, _ = PA._daily_peak(rd, sched, lambda n: n in PA.MACHINE_CREW)
        leaked = sorted(n for n in eq if n in roles)
        assert not leaked, "配员角色漏进设备峰值（source=%s）：%s" % (source, leaked)
        assert eq.get("混凝土振捣器") == 3, eq
        for r in roles:
            assert crew.get(r) == 3, "配员 %s 必须进机械配员峰值，实际 %s" % (r, crew)

    def test_看板把振捣工画在配员行而不是机械行(self):
        view = D._compute_view(_plan())
        h = D._resource_card_html(_plan(), view, D._peak_caliber(_plan(), view))
        m = re.search(r"主要机械峰值：</b>([^<]*)", h)
        assert m, h[:300]
        assert "振捣工" not in m.group(1), "人不能出现在「主要机械峰值」里：%s" % m.group(1)
        assert "混凝土振捣器 1台" in m.group(1)
        m2 = re.search(r"机械配员峰值（人）：</b>([^<]*)", h)
        assert m2 and "振捣工 1人" in m2.group(1), m2 and m2.group(1)

    def test_旧落盘计划的设备表也要过滤配员(self, tmp_deliverables):
        """展示层兜底：落盘的计划不会因为分类表改了而自己变干净。

        实测 `plan_sample3_after_fix` 的 `resource_plan.equipment_peak` 里就有
        `"振捣工": 1`（落盘于分类修复之前），Word 的「设备峰值台数」表直接读该字段，
        于是同一份文档里「设备峰值」印出「振捣工1」—— 必须按当前分类表过滤。
        """
        plan = _plan()
        # 模拟旧计划：设备表里混进两名人，machine_crew_peak 里一个都没有
        plan["resource_plan"]["equipment_peak"] = {"混凝土振捣器": 1, "振捣工": 1, "信号工": 2}
        plan["resource_plan"]["machine_crew_peak"] = {}
        equip, crew = D._split_equipment_peak(plan["resource_plan"])
        assert [k for k, _ in equip] == ["混凝土振捣器"], equip
        assert dict(crew) == {"振捣工": 1, "信号工": 2}, crew

        view = D._compute_view(plan)
        h = D._resource_card_html(plan, view, D._peak_caliber(plan, view))
        m = re.search(r"主要机械峰值：</b>([^<]*)", h)
        assert m and "振捣工" not in m.group(1) and "信号工" not in m.group(1), m and m.group(1)
        assert "混凝土振捣器 1台" in m.group(1)
        m2 = re.search(r"机械配员峰值（人）：</b>([^<]*)", h)
        assert m2 and "振捣工 1人" in m2.group(1)

        from docx import Document
        p = copy.deepcopy(plan)
        p["plan_id"] = "zz_equip_filter_verify"
        try:
            doc = Document(D.build_plan_docx(p))
            body = "\n".join(pp.text for pp in doc.paragraphs)
            assert "机械配员（人，随机械台数配置，非设备）：" in body
            k = body.index("机械配员（人，随机械台数配置，非设备）：")
            assert "振捣工 1 人" in body[k:k + 120]
            # 设备表里不许出现人
            grids = [[c.text for c in t.rows[0].cells] for t in doc.tables]
            assert ["设备", "峰值台数"] in grids
            i = grids.index(["设备", "峰值台数"])
            cells = [c.text for r in doc.tables[i].rows for c in r.cells]
            assert "振捣工" not in cells and "信号工" not in cells, cells
            assert "混凝土振捣器" in cells and "1" in cells
        finally:
            shutil.rmtree(D._plan_dir(p), ignore_errors=True)


# ══════════════════════ ③ 看板缺段的结构性保证 ══════════════════════

# 故意不含任何关键标记的假 HTML（模拟模型"只写了个壳"的产出）
BARE_LLM_HTML = (
    "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><title>模型编排</title>"
    "</head><body><h1>施工进度计划看板</h1>"
    "<table><tr><th>序号</th><th>任务编号</th><th>任务名称</th>"
    "<th>开始日期</th><th>完成日期</th><th>工期(天)</th></tr>"
    "<tr><td>1</td><td>6.1.1.1</td><td>ALC墙板</td><td>2028-10-11</td>"
    "<td>2028-11-10</td><td>31</td></tr></table></body></html>"
)


class _FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def chat_text(self, *a, **kw):
        self.calls += 1
        return self.text


class TestFactsBundleCarriesCaliber:
    """③-a：缺什么补什么 —— 模型必须拿得到这些字段才能写出这几段。"""

    def test_facts含峰值口径与工作面容量(self):
        plan = _plan()
        facts = D._facts_bundle(plan, D._compute_view(plan))
        rp = facts["resource_plan"]
        assert rp["peak_manpower_source"] == "resource_curve"
        # E1（2026-09-21 裁定）：申报峰值的两个键**整体删除**（模型补的 120 不再透传）
        assert "declared_peak_manpower" not in rp, rp
        assert "declared_peak_manpower_source" not in rp, rp
        assert rp["curve_peak_manpower"] == 9
        assert "peak_caliber" in facts and facts["peak_caliber"]["peak"] == 9
        assert "declared" not in facts["peak_caliber"], facts["peak_caliber"]
        assert rp["machine_crew_peak"] == {"振捣工": 1}
        assert "workface_capacity" in facts and "applied" in facts["workface_capacity"]
        assert "sentence" in facts["workface_capacity"]
        assert "peak_caliber" in facts and facts["peak_caliber"]["peak"] == 9

    def test_facts含设备对账与任务级依据(self):
        plan = _plan()
        plan["meta"]["equipment_binding"] = [
            {"name": "塔吊", "quantity": 2, "effective": False, "note": "未匹配工序"}]
        facts = D._facts_bundle(plan, D._compute_view(plan))
        eb = {e["name"]: e for e in facts["equipment_binding"]}
        assert eb["塔吊"]["effective"] is False and eb["塔吊"]["quantity"] == 2
        ev = {e["task_id"]: e["evidence"] for e in facts["task_evidence"]}
        # P3/D5：换算参数来源按 `ctx_source` 分流 —— fixture 里是 ai_estimate，
        # 所以文案是「AI估算换算参数」（不再是笼统的「AI 假定」）。
        assert "单位换算按AI估算换算参数" in ev["6.1.1.1"], ev["6.1.1.1"]
        # 政策变更（2026-09-20）：AI 经验估算定额照用，逐条标注换成政策文案；
        # 真降级（单位不可换算）那条仍然写「无可用定额…未计算班组」，一个字没删。
        assert D.AI_NORM_LABEL in ev["2.1.4"], ev["2.1.4"]
        assert "无可用定额" in ev["9.2.2"], ev["9.2.2"]
        assert "工期沿用 WBS 估算，未计算班组" in ev["9.2.2"], ev["9.2.2"]
        assert [t["task_id"] for t in facts["unit_assumed_tasks"]] == ["6.1.1.1"]
        assert [t["task_id"] for t in facts["norm_degradations"]] == ["2.1.4", "9.2.2"]
        assert "互斥" in facts["norm_caliber_note"]


class TestStructuralMarkerGuard:
    """③-b：模型漏段 → 追加确定性段；所有分支都有可诊断痕迹。"""

    def test_假HTML缺标记时被追加且关键标记齐备(self):
        plan = _plan()
        view = D._compute_view(plan)
        merged, mode = D._ensure_delivery_markers(BARE_LLM_HTML, plan, view)
        assert mode == "agent+appendix", mode
        assert merged is not None
        for m in D.DELIVERY_MARKERS:
            assert m in merged, "追加后仍缺标记：%s" % m
        assert D.DELIVERY_FALLBACK_COMMENT in merged
        # 追加段必须在 </body> 之前（页面仍合法）
        assert merged.rfind(D.DELIVERY_FALLBACK_COMMENT) < merged.rfind("</body>")
        # 模型原有内容不许被丢掉
        assert "模型编排" in merged

    def test_标记齐全时不追加(self):
        plan = _plan()
        view = D._compute_view(plan)
        rich = "<html><body>依据 / 资源 工作面容量 主要机械峰值</body></html>"
        merged, mode = D._ensure_delivery_markers(rich, plan, view)
        assert mode == "agent"
        assert merged == rich, "标记齐备就不该动模型页面"

    def test_追加失败时回退确定性渲染(self, monkeypatch, tmp_deliverables):
        """追加段构建炸了 → 保底整体回退确定性渲染（它一定带全部标记）。"""
        def _boom(plan, view, cal_h=None):
            raise RuntimeError("appendix builder exploded")
        monkeypatch.setattr(D, "_delivery_appendix_html", _boom)
        ctx = {}
        path, used_agent = D.build_plan_html_agent(_plan(), _FakeLLM(BARE_LLM_HTML), ctx=ctx)
        assert used_agent is False, "回退路径必须报『非 LLM 编排』"
        html = Path(path).read_text(encoding="utf-8")
        for m in D.DELIVERY_MARKERS:
            assert m in html, "回退后的确定性页面仍缺标记：%s" % m
        assert "回退确定性模板" in " ".join(ctx.get("wbs_warnings") or [])

    def test_agent产出最终文件带关键标记(self, tmp_deliverables):
        plan = _plan()
        llm = _FakeLLM(BARE_LLM_HTML)
        path, used_agent = D.build_plan_html_agent(plan, llm, ctx={})
        assert llm.calls == 1
        assert used_agent is True, "追加段走的是 LLM 编排页面这一路"
        html = Path(path).read_text(encoding="utf-8")
        for m in D.DELIVERY_MARKERS:
            assert m in html, "最终产物仍缺标记：%s" % m
        assert D.DELIVERY_FALLBACK_COMMENT in html

    def test_标记缺失时留下可诊断痕迹(self, tmp_deliverables):
        ctx = {}
        D.build_plan_html_agent(_plan(), _FakeLLM(BARE_LLM_HTML), ctx=ctx)
        joined = " ".join(ctx.get("wbs_warnings") or [])
        assert "追加确定性口径段" in joined, ctx.get("wbs_warnings")

    def test_确定性页面与追加段同一函数(self, tmp_deliverables):
        """追加段的资源卡必须与 `build_plan_html` 的卡片一字不差（复用同一函数）。"""
        plan = _plan()
        view = D._compute_view(plan)
        card = D._resource_card_html(plan, view, D._peak_caliber(plan, view))
        assert card in D._delivery_appendix_html(plan, view)
        path = D.build_plan_html(copy.deepcopy(plan))
        assert card in Path(path).read_text(encoding="utf-8")

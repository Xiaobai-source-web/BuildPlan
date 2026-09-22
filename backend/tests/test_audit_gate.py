# -*- coding: utf-8 -*-
"""三轮回审门测试 —— 「审得了」必须真的是用户说了算

产品定位是"不替用户拍板"，所以三个关键点必须停下来给用户看：
  R1 WBS 结构 → R2 两版工期 → R3 Word 草案（不含图表）→ 打 Y → 才出定稿与看板

本文件守住四件事：
  1. 三轮**都**审过**且每轮 answered_by=human** → `meta.audit_status = 已审计`
     （第 43 轮加「谁答的门」：脚本代答不算人工复审，见 `test_audit_honesty_source.py`）
  2. 任何一轮退回（输入意见）→ 计划保持"未审计"、**停在当前阶段**（不产出定稿）
  3. 草案 Word 带「未审计」戳、定稿不带；草案还显式说明"不含图表"
  4. 摘要算不出来 / 门被取消 / 畸形 ctx，一律不崩

运行：python -m pytest backend/tests/test_audit_gate.py -q
"""

import json
import os
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest

from pipeline.nodes.audit_gate import (
    DraftAuditNode, ScheduleAuditNode, WBSAuditNode,
    schedule_highlights, wbs_highlights,
)


# ==================== 夹具 ====================
class _FakeRegistry(object):
    """假的交互登记处：记录门发出的 payload，回放预设决策。"""

    def __init__(self, decision=None):
        self.decision = decision if decision is not None else {"passed": True}
        self.registered = []
        self.payloads = []

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=None):
        return dict(self.decision)


class _FakeNode(object):
    """最小节点替身：只提供 emit 与注册表注入点。"""


def _run_gate(node, ctx, decision):
    reg = _FakeRegistry(decision)
    events = []
    node._registry = reg
    node._run_id = "t"
    node._cancel_evt = None
    node._emit = lambda e, d: events.append((e, d))
    result = node.run(ctx)
    return result, events, reg


def _ctx(**kw):
    ctx = {
        "wbs": {"phases": [
            {"phase": "地下室结构", "work_packages": [{"id": "4.1", "sub_packages": [
                {"id": "4.1.1.1", "quantity": 100, "unit": "t"}]}]},
            {"phase": "地上主体结构", "work_packages": [{"id": "5.1", "sub_packages": [
                {"id": "5.1.1.1", "quantity": 900, "unit": "t"}]}]},
            {"phase": "装饰装修", "work_packages": []},          # 空阶段：必须被点出来
        ]},
        "plan_level": "L4",
        "schedule_versions": {
            "theory_min": {"total_duration_days": 541, "peak_labor": 92,
                           "peak_equipment": 6},
            "resource_ok": {"total_duration_days": 743, "peak_labor": 44,
                            "peak_equipment": 3},
            "compare": {"delta_days": 202, "user_target": 600,
                        "target_verdict": "需放宽资源"},
            "warnings": ["定额口径覆盖率：只有 67% 有据可查"],
        },
        "norm_coverage": {"total": 100, "bound": 67, "bound_pct": 67.0,
                          "by_reason": {"AI估算定额": 25, "单位不一致": 8},
                          "by_reason_pct": {"AI估算定额": 25.0, "单位不一致": 8.0}},
        "plan_json": {"plan_id": "p1", "meta": {}, "overview": {
            "project_name": "审计测试", "total_duration_days": 743,
            "planned_start_date": "2026-01-01", "planned_end_date": "2028-01-13"}},
        "artifacts": {"docx_draft": "D:/tmp/草案.docx"},
    }
    ctx.update(kw)
    return ctx


# ==================== 1. 摘要内容 ====================
def test_wbs_highlights_reports_structure_and_empty_phases():
    text, hl = wbs_highlights(_ctx())
    assert "第 1 轮" in text and "WBS 结构审计" in text
    assert "阶段 3 个" in text and "叶子任务 2 条" in text
    assert hl["leaves"] == 2 and hl["phases"] == 3
    assert "装饰装修" in text, "没有叶子的阶段必须被点出来让用户核对"
    assert hl["empty_phases"] == ["装饰装修"]
    assert "t" in text, "工程量要按单位汇总"


def test_schedule_highlights_reports_two_versions_and_coverage():
    text, hl = schedule_highlights(_ctx())
    assert "理论最短 541 天" in text and "资源不超额 743 天" in text and "相差 202 天" in text
    assert "人工峰值 44 人" in text, "报的是 resource_ok 版的峰值"
    assert "67.0%" in text and "未纳入" in text
    assert "需放宽资源" in text
    assert hl["delta_days"] == 202 and hl["resource_ok_days"] == 743


def test_highlights_never_crash_on_empty_ctx():
    """空 ctx 也要出可读文本，不能抛异常（门不能因为上游缺字段就挂掉）。"""
    for fn in (wbs_highlights, schedule_highlights):
        text, hl = fn({})
        assert isinstance(text, str) and text and isinstance(hl, dict)


# ==================== 2. 通过 / 退回 / 取消 ====================
def test_gate_passes_and_records_round():
    node = WBSAuditNode()
    ctx = _ctx()
    result, events, reg = _run_gate(node, ctx, {"passed": True})

    assert result.get("_stop") is None
    assert reg.registered, "门必须先注册交互 id"
    ev = [d for e, d in events if e == "param_review"]
    assert ev and ev[0]["purpose"] == "audit" and ev[0]["round"] == 1
    assert ev[0]["summary"] and ev[0]["next_hint"]
    assert ctx["audit_rounds"][0]["passed"] is True
    assert ctx["audited"] is False, "只过一轮还不能算已审计"
    assert ctx["plan_json"]["meta"]["audit_status"] == "未审计"


def test_all_three_rounds_make_it_audited():
    """三轮**人工**通过 → `audited=True` 且 meta 写「已审计」。

    第 43 轮（用户审计 P0-A）：判据加了「谁答的门」—— 不声明 `answered_by="human"`，
    即使三轮都放行，`meta.audit_status` 也只能是「未审计」（脚本代答不算人工复审）。
    """
    ctx = _ctx()
    for node in (WBSAuditNode(), ScheduleAuditNode(), DraftAuditNode()):
        _run_gate(node, ctx, {"passed": True, "answered_by": "human"})
    assert ctx["audited"] is True
    assert ctx["plan_json"]["meta"]["audit_status"] == "已审计"
    rounds = ctx["plan_json"]["meta"]["audit_rounds"]
    assert [r["round"] for r in rounds] == [1, 2, 3]
    assert all(r.get("answered_by") == "human" for r in rounds)


def test_all_three_rounds_script_answered_is_not_audited():
    """三轮都放行但是**脚本代答** → `audited` 仍为真（流程放行了），meta 只能是未审计。"""
    ctx = _ctx()
    for node in (WBSAuditNode(), ScheduleAuditNode(), DraftAuditNode()):
        _run_gate(node, ctx, {"passed": True, "answered_by": "script"})
    assert ctx["audited"] is True
    assert ctx["plan_json"]["meta"]["audit_status"] == "未审计"
    assert all(r.get("answered_by") == "script"
               for r in ctx["plan_json"]["meta"]["audit_rounds"])


def test_third_round_hint_matches_the_product_wording():
    """第 3 轮的提示语：必须说清"认可草案才出定稿与看板"。

    ⚠️ 第 32 轮改过一次措辞：原句是「如计划已成熟，请输入 Y，我将整理出**最终计划**…」，
    「最终」是**被明令禁止**的词（此刻计划仍是「未审计」，见 renderer 落盘文案处的同一条禁令）。
    """
    _text, _hl = DraftAuditNode()._summary(_ctx())
    node = DraftAuditNode()
    _result, events, _reg = _run_gate(node, _ctx(), {"passed": True})
    ev = [d for e, d in events if e == "param_review"][0]
    assert "认可这份草案请输入" in ev["next_hint"]
    assert "Y" in ev["next_hint"]
    assert "绘制看板" in ev["next_hint"]
    assert "最终" not in ev["next_hint"], "落盘时计划还没审计，不许说「最终」"
    assert "不含图表" in ev["summary"], "要说明这一轮看的是不含图表的草案"


def test_comment_at_round_one_also_blocks_final_deliverables(docx_dir):
    """第 1 轮退回同样有效：计划交付、定稿与看板不出。"""
    ctx, _events, finished, seen = _run_pipeline(
        [{"passed": False, "manual_input": "地下室阶段缺了支护"}, {}, {}])
    assert finished
    # 第 1 轮的**修改意见**现在会先给一张 [1]/[2]/[3] 菜单（同一轮可以问不止一次，
    # 见 test_audit_gate_dialogue.py）；但第 2/3 轮绝不该再出现。
    assert seen and set(seen) == {1}, "第 1 轮退回就不该再问第 2/3 轮，实际 %s" % seen
    plan = ctx.get("plan_json") or {}
    assert plan["meta"]["audit_status"] == "未审计"
    art = ctx.get("artifacts") or {}
    assert not art.get("html") and not art.get("docx")


def test_comment_stops_the_pipeline_and_keeps_unaudited():
    node = ScheduleAuditNode()
    ctx = _ctx()
    result, _events, _reg = _run_gate(node, ctx, {"passed": False,
                                                   "manual_input": "主体班组太小，钢筋工要 30 人"})

    assert ctx["audited"] is False
    assert ctx["audit_rejected"] is True, "退回要留下标记，第 3 轮门口据此拦下最终交付物"
    meta = ctx["plan_json"]["meta"]
    assert meta["audit_status"] == "未审计"
    assert meta["audit_comments"][0]["comment"] == "主体班组太小，钢筋工要 30 人"
    assert meta["audit_rounds"][0]["passed"] is False
    # R2 退回不掐流水线（那时 plan_json 还没组装），所以不带 _stop
    assert not result or "_stop" not in result


def test_round_three_reject_stops_immediately():
    """第 3 轮退回要立刻停（它后面就是定稿 Word 与看板）。"""
    result, _events, _reg = _run_gate(
        DraftAuditNode(), _ctx(), {"passed": False, "manual_input": "总工期还是太长"})
    assert result.get("_stop")
    assert "停止产出定稿与看板" in result["_stop"]
    assert "总工期还是太长" in result["_stop"]


def test_round_three_skips_when_earlier_round_rejected():
    """前两轮退回过 → 第 3 轮不再提问，直接拦下最终交付物。"""
    ctx = _ctx(audit_rejected=True,
               audit_comments=[{"round": 2, "name": "两版工期", "comment": "班组要加大"}])
    node = DraftAuditNode()
    node._registry = _FakeRegistry({"passed": True})
    node._run_id = "t"
    node._cancel_evt = None
    events = []
    node._emit = lambda e, d: events.append((e, d))
    result = node.run(ctx)
    assert result.get("_stop") and "前序审计未通过" in result["_stop"]
    assert "班组要加大" in result["_stop"]
    assert not [e for e, _ in events if e == "param_review"], "不该再打扰用户"


def test_abort_cancels_the_run():
    result, _events, _reg = _run_gate(WBSAuditNode(), _ctx(), {"action": "abort"})
    assert result.get("_stop") and "取消" in result["_stop"]


def test_summary_failure_does_not_break_the_gate():
    """摘要函数抛异常时，门仍要能正常提问（否则用户被卡死在流水线里）。"""
    class _Boom(WBSAuditNode):
        def _summary(self, ctx):
            raise RuntimeError("上游数据烂了")

    ctx = _ctx()
    _result, events, _reg = _run_gate(_Boom(), ctx, {"passed": True})
    ev = [d for e, d in events if e == "param_review"][0]
    assert "摘要生成失败" in ev["summary"]
    assert ctx["audited"] is False


# ==================== 3. pipeline 装配顺序 ====================
def test_pipeline_contains_three_audit_gates_in_order():
    from pipeline.builder import build_pipeline
    names = [n.name for n in build_pipeline(run_id="t").nodes]

    assert names.index("audit_wbs") < names.index("norm_bind"), "R1 应在定额锚定之前（先审结构）"
    assert names.index("scheduler") < names.index("audit_schedule"), "R2 应在两版工期之后"
    assert names.index("word_draft") < names.index("audit_draft") < names.index("word_export"), \
        "R3 必须夹在 Word 草案与定稿之间"
    assert names.index("audit_draft") < names.index("html_page"), "看板在审过之后才画"
    # 引擎按 name 存检查点，两个 Word 节点必须不同名
    assert len(names) == len(set(names)), "节点 name 必须唯一：%s" % names


# ==================== 4. Word 草案 / 定稿 ====================
@pytest.fixture()
def docx_dir(monkeypatch):
    import os as _os
    import shutil as _sh
    from pipeline import config

    root = BACKEND / "_test_tmp" / ("docx_p%s" % _os.getpid())
    _sh.rmtree(str(root), ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DELIVERABLES_DIR", root, raising=False)
    yield root
    _sh.rmtree(str(root), ignore_errors=True)


def _three_human_rounds():
    """三轮**人工**通过。

    第 43 轮（用户审计 P0-A）起，「已审计」的判据是"三轮都有记录、都通过、且每轮
    `answered_by == "human"`"（见 `audit_gate.audit_honesty`）—— 只写 `audit_status`
    或只堆 `passed=True` 都**不再**算数。
    """
    return [{"round": 1, "name": "WBS 结构", "passed": True, "answered_by": "human"},
            {"round": 2, "name": "两版工期", "passed": True, "answered_by": "human"},
            {"round": 3, "name": "Word 草案（不含图表）", "passed": True,
             "answered_by": "human"}]


def _plan_for_docx(audit_status="未审计", rounds=None):
    return {
        "plan_id": "docx_test",
        "overview": {"project_name": "文档测试", "total_duration_days": 100,
                     "planned_start_date": "2026-01-01", "planned_end_date": "2026-04-11",
                     "critical_path_length": 2},
        "meta": {"audit_status": audit_status,
                 "audit_rounds": (rounds if rounds is not None
                                  else [{"round": 1, "name": "WBS 结构", "passed": True}]),
                 "caliber_note": "单栋项目，按整栋口径编制"},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "5.1", "name": "wp", "sub_packages": [
                {"id": "5.1.1.1", "name": "钢筋", "quantity": 10, "unit": "t",
                 "duration_days": 20}]}]}]},
        "dependencies": [],
        "all_tasks_schedule": [{"task_id": "5.1.1.1", "task_name": "钢筋",
                                "start_date": "2026-01-01", "finish_date": "2026-01-21",
                                "duration_days": 20, "assigned_resources": {"钢筋工": 5}}],
        "key_milestones": [], "critical_path_tasks": [],
        "resource_plan": {"peak_manpower": 5, "total_manpower_days": 100.0,
                          "equipment_peak": {}, "material_summary": []},
        "risks": [], "report": "报告正文",
    }


def _docx_text(path):
    from docx import Document
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


def test_draft_docx_is_stamped_unaudited_and_has_no_charts(docx_dir):
    from pipeline.nodes.delivery import build_plan_docx

    path = build_plan_docx(_plan_for_docx("未审计"), draft=True)
    assert "草案" in os.path.basename(path) and "未审计" in os.path.basename(path)
    text = _docx_text(path)
    assert "草案 · 未审计" in text
    assert "不含图表" in text, "必须告诉用户这份草案里没有图表"
    assert "可视化看板" in text
    assert "审计状态" in text and "未审计" in text


def test_final_docx_is_marked_audited(docx_dir):
    """三轮**人工**通过 + `audit_status="已审计"` → 才允许印「已审计定稿」。

    第 43 轮修正（用户审计 P0-A）：本用例原来只塞了一条 R1、`audit_status="已审计"`，
    就断言交付物出现「已审计定稿」—— 那正是被审计抓到的谎（数据里只有 R1/R2，
    定稿却印「R3 通过」）。断言因此改到**新判据**上：必须有三轮且每轮 answered_by=human。
    旧口径的降级行为由 `test_final_docx_refuses_self_declared_audit` 覆盖。
    """
    from pipeline.nodes.delivery import build_plan_docx

    path = build_plan_docx(_plan_for_docx("已审计", rounds=_three_human_rounds()), draft=False)
    assert os.path.basename(path) == "施工进度计划.docx"
    text = _docx_text(path)
    assert "已审计定稿" in text
    assert "草案 · 未审计" not in text
    assert "R1 通过" in text and "R3 通过" in text, "审计链要写进文档（谁在第几轮审的）"
    assert "真人通过" in text or "人工复核通过" in text, "要写明是人工审的，不是系统代答"


def test_final_docx_refuses_self_declared_audit(docx_dir):
    """**数据自称已审计但拿不出三轮人工记录 → 定稿也必须印未审计**（P0-A 核心）。

    三种造假/残缺各断言一次：只有 R1/R2（真计划的实际状态）、`answered_by` 缺失
    （第 37 轮之前的老计划）、三轮全是脚本代答（用户审计里 `devtools/rerun_sample3.py`
    的真实情形）。
    """
    from pipeline.nodes.delivery import build_plan_docx

    cases = {
        "only_r1_r2": [{"round": 1, "name": "WBS 结构", "passed": True},
                       {"round": 2, "name": "两版工期", "passed": True}],
        "no_source": [{"round": 1, "name": "WBS 结构", "passed": True},
                      {"round": 2, "name": "两版工期", "passed": True},
                      {"round": 3, "name": "Word 草案（不含图表）", "passed": True}],
        "script_answered": [{"round": 1, "name": "WBS 结构", "passed": True,
                             "answered_by": "script"},
                            {"round": 2, "name": "两版工期", "passed": True,
                             "answered_by": "script"},
                            {"round": 3, "name": "Word 草案（不含图表）", "passed": True,
                             "answered_by": "script"}],
    }
    for name, rounds in cases.items():
        text = _docx_text(build_plan_docx(_plan_for_docx("已审计", rounds=rounds), draft=False))
        assert "已审计定稿" not in text, "%s：自称已审计却印了已审计定稿" % name
        assert "未审计 · 待人工复审" in text, name
        assert "本文件按未审计口径出具" in text, "%s：必须说明为什么不能印" % name
        # 用户的复检脚本按禁语做子串匹配（`_probe_tmp/q_audit_check.py`）
        for banned in ("已审计定稿", "R3 通过", "审计状态 已审计"):
            assert banned not in text, "%s：命中禁语 %s" % (name, banned)
    # 脚本代答的那一例，原因里必须点名"脚本代答"而不是含糊其辞
    text = _docx_text(build_plan_docx(
        _plan_for_docx("已审计", rounds=cases["script_answered"]), draft=False))
    assert "脚本代答" in text, text[:600]


def test_docx_carries_caliber_note_and_audit_comments(docx_dir):
    from pipeline.nodes.delivery import build_plan_docx

    plan = _plan_for_docx("未审计")
    plan["meta"]["audit_comments"] = [{"round": 2, "name": "两版工期",
                                       "comment": "钢筋工班组要加到 30 人"}]
    text = _docx_text(build_plan_docx(plan, draft=True))
    assert "单栋项目，按整栋口径编制" in text, "编制口径必须出现在交付物里"
    assert "钢筋工班组要加到 30 人" in text, "审计意见要留痕"


def test_word_export_node_writes_draft_and_final_to_distinct_keys(docx_dir):
    from pipeline.nodes.delivery import WordExportNode

    ctx = {"plan_json": _plan_for_docx("未审计")}
    draft = WordExportNode(draft=True)
    draft._emit = lambda e, d: None
    out1 = draft.run(ctx)
    assert "docx_draft" in out1["artifacts"] and "docx" not in out1["artifacts"]

    ctx["plan_json"]["meta"]["audit_status"] = "已审计"
    final = WordExportNode()
    final._emit = lambda e, d: None
    out2 = final.run(ctx)
    assert out2["artifacts"]["docx"].endswith("施工进度计划.docx")
    # 两个节点必须不同名（引擎按 name 存检查点）
    assert draft.name != final.name


# ==================== 5. 端到端：真跑一遍流水线 ====================
def _run_pipeline(audit_decisions):
    """跑完整离线流水线；audit_decisions 是第 1/2/3 轮的决策字典列表。

    返回 (ctx, events, finished)。审计门与参数门都走 param_review 事件，
    这里按 `purpose` 区分：audit 轮按传入的决策应答，其它一律打 Y。
    """
    import threading
    import time

    from pipeline.builder import build_pipeline
    from pipeline.llm import LLMError

    class _NoLLM(object):
        def chat_json(self, *a, **k):
            raise LLMError("测试强制无 LLM")

        def chat_text(self, *a, **k):
            raise LLMError("测试强制无 LLM")

    pipeline = build_pipeline(run_id="audit_e2e", llm=_NoLLM())
    events = []
    resolved = set()
    audit_seen = []

    def emit(e, d):
        events.append((e, d))

    # 【第 2 批 · 域 2 / 2.1 + 收口】提示词里必须写明**基础类型**与**结构形式**：
    # 两者都是硬必要键（缺了参数门直接中断、试算也绕不过），
    # 无 LLM 的确定性全链路跑不出计划。
    ctx = {"prompt": "某住宅项目，共 12 栋，地上 38 层，基础类型：筏板基础，"
                     "结构形式：框架-剪力墙结构，"
                     "总建筑面积12.8万㎡，混凝土5.2万m³，"
                     "钢筋7.5万吨，总劳动力峰值929人，开工2025-04-16",
           "_run_id": "audit_e2e",
           # 第 34 轮：意图识别已取消 —— 要跑完整流水线必须**显式进入 plan 模式**
           "mode": "plan"}
    t = threading.Thread(target=lambda: pipeline.run(ctx, emit=emit), daemon=True)
    t.start()
    deadline = time.time() + 120
    while time.time() < deadline:
        if not t.is_alive():
            break
        for e, d in list(events):
            key = d.get("pause_id") or d.get("confirm_id") or d.get("review_id")
            if not key or key in resolved:
                continue
            resolved.add(key)
            try:
                if e == "param_review" and d.get("purpose") == "audit":
                    rnd = int(d.get("round") or 0)
                    audit_seen.append(rnd)
                    decision = audit_decisions[rnd - 1] if 0 < rnd <= len(audit_decisions) else {}
                    # 这个测试**扮演人**（它就是"人点 Y"的替身），所以必须显式声明
                    # answered_by="human"。第 43 轮起不声明就按 unknown 记账，
                    # 交付物会如实印「未审计 · 待人工复审」（用户审计 P0-A）。
                    decision = dict(decision or {})
                    decision.setdefault("answered_by", "human")
                    pipeline.registry.resolve(key, decision)
                elif e == "node_paused":
                    pipeline.registry.resolve(key, {"action": "continue"})
                elif e == "confirm_required":
                    pipeline.registry.resolve(key, {"decision": True})
                elif e == "param_review":
                    pipeline.registry.resolve(key, {"passed": True})
            except Exception:
                pass
        time.sleep(0.05)
    t.join(timeout=5)
    return ctx, events, (not t.is_alive()), audit_seen


def test_end_to_end_three_rounds_then_final_deliverables(docx_dir):
    """三轮全打 Y → 已审计 + 定稿 Word + 看板都产出。"""
    ctx, events, finished, seen = _run_pipeline([{"passed": True}] * 3)
    assert finished, "流水线未结束"
    assert seen == [1, 2, 3], "三轮回审必须按顺序各来一次，实际 %s" % seen

    plan = ctx.get("plan_json") or {}
    assert plan["meta"]["audit_status"] == "已审计", plan["meta"].get("audit_status")
    assert ctx.get("audited") is True

    art = ctx.get("artifacts") or {}
    assert art.get("docx") and art.get("html"), art
    assert art.get("docx_draft"), "草案也必须留在产物里（审计痕迹）"
    assert "草案" in os.path.basename(art["docx_draft"])
    assert os.path.basename(art["docx"]) == "施工进度计划.docx"
    assert os.path.exists(art["html"])

    # 真产物要能读出「已审计定稿」与三轮记录；草案要带「未审计」戳
    draft_text = _docx_text(art["docx_draft"])
    assert "草案 · 未审计" in draft_text
    assert "不含图表" in draft_text
    final_text = _docx_text(art["docx"])
    assert "已审计定稿" in final_text and "草案 · 未审计" not in final_text
    assert "R1 通过" in final_text and "R3 通过" in final_text, final_text[:400]
    # 看板也要能看出这是一份审过的计划
    html = Path(art["html"]).read_text(encoding="utf-8")
    assert "已审计" in html


def test_end_to_end_reject_in_round_two_stops_before_final(docx_dir):
    """第 2 轮退回 → **计划照常交付**（带"未审计"），但定稿 Word 与看板被拦下。

    为什么计划要交付：R2 跑在 assembler 之前，那时 plan_json 还没组装。
    在这儿掐掉流水线等于让用户白跑一遍、连计划都拿不到。
    """
    ctx, events, finished, seen = _run_pipeline(
        [{"passed": True}, {"passed": False, "manual_input": "主体班组要加大"}, {}])
    assert finished
    # 同 test_comment_at_round_one...：修改意见会先给菜单，所以同一轮可能出现多次；
    # 关键是**第 3 轮绝不再问**（前两轮退回后 R3 直接拦下最终交付物）。
    assert seen and set(seen) == {1, 2}, "第 2 轮退回就不该再问第 3 轮，实际 %s" % seen

    plan = ctx.get("plan_json") or {}
    assert plan, "计划本身必须交付，用户要能拿到它接着改"
    assert plan["meta"]["audit_status"] == "未审计"
    assert ctx.get("audited") is not True
    assert plan["meta"]["audit_comments"][0]["comment"] == "主体班组要加大"
    assert plan["meta"]["audit_rounds"][1]["passed"] is False

    art = ctx.get("artifacts") or {}
    assert not art.get("html"), "没审过就不该画看板"
    assert not art.get("docx"), "没审过就不该出定稿"
    assert any(e == "plan_final" for e, _ in events), "计划本身要落盘，便于用户接着 /revise"

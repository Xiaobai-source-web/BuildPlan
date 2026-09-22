# -*- coding: utf-8 -*-
"""第 44 轮「交付物不许说谎」——三处诚实性缺陷的护栏（看板 + Word）。

用户原话（逐条）：
  ① 「设备对账文案：现在会甩锅给用户」——真实看板原文：
     「用户申报设备限额对账 本计划无用户申报设备限额（equipment_binding 为空），
       所有设备台数均为 AI 默认口径。」
     而真相是这次边界条件里 `equipment` 是**空列表**（模型答了但把设备留空）：
     根本**没有取得设备清单**，不是"用户没申报"。
  ② 「节点级告警」根本没展示：`meta` 里 `node_warnings` / `node_warning_count` /
     `model_call_failures` 恒存在，交付物里一个字都不印。
  ③ 模型用量被少报：`plan_sample3_after_org_v2.json` 的 `meta.usage` 是
     `calls=30 / ¥0.4038`（**计划定稿时**的快照），而这次运行真实是
     `calls=32 / ¥0.8018`（`reporter` / `html_page` 在 `build_meta` 之后才跑）。

本文件从**真产物**（`build_plan_html` / `build_plan_docx` 写出的文件）上钉：
  · 设备清单三态各自的渲染（用户申报 / 模型估算 / 空清单），空清单那句必须含
    「未取得设备清单」且**不得**含「无用户申报设备限额」；
  · 节点级告警：`count == 0` 时看板与 Word 都不出现该节，有告警时两处都出现且条数取自数据，
    键缺失 / 坏数据不抛；
  · 用量：`usage_final` 存在时看板显示最终值且含「定稿时」，Word 含「未计入」，
    `usage_final` 缺失时两处都不崩。

运行：python -m pytest backend/tests/test_delivery_honesty_display.py -q -p no:cacheprovider
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import config                          # noqa: E402
from pipeline import schemas                         # noqa: E402
from pipeline.nodes import delivery as D             # noqa: E402

REAL_PLAN = BACKEND / "plans" / "plan_sample3_after_org_v2.json"

# 交付物目录 / 计划目录的隔离由 tests/conftest.py 全局负责；这里的 fixture 只是把
# 交付物目录再指到 tmp（与 test_delivery_confidence_board.py 同一写法）。
USAGE_BASE = {"calls": 30, "prompt_tokens": 355273, "completion_tokens": 24079,
              "total_tokens": 379352, "cost_cny": 0.4038, "model": "mimo-v2.5"}
USAGE_FINAL = {"calls": 32, "prompt_tokens": 620000, "completion_tokens": 31000,
               "total_tokens": 651000, "cost_cny": 0.8018, "model": "mimo-v2.5"}


# ══════════════════════ 公共样例 ══════════════════════

def _plan(meta=None, **over):
    """最小计划（两条 5 天任务），meta 由调用方给。"""
    tasks = [
        {"task_id": "1.1.1", "task_name": "柱浇筑", "start_date": "2026-09-01",
         "finish_date": "2026-09-06", "duration_days": 5,
         "assigned_resources": {"钢筋工": 4}},
        {"task_id": "1.1.2", "task_name": "梁浇筑", "start_date": "2026-09-01",
         "finish_date": "2026-09-06", "duration_days": 5,
         "assigned_resources": {"木工": 4}},
    ]
    plan = {
        "plan_id": "plan_honesty_display",
        "overview": {"project_name": "诚实性展示测试", "total_duration_days": 10,
                     "planned_start_date": "2026-09-01",
                     "planned_end_date": "2026-09-11", "critical_path_length": 2},
        "wbs": {"phases": [{"phase": "主体", "work_packages": [
            {"id": "1.1", "name": "结构", "sub_packages": [
                {"id": "1.1.1", "name": "柱浇筑", "duration_days": 5, "quantity": 10,
                 "unit": "m³", "work_type": "混凝土工程"},
                {"id": "1.1.2", "name": "梁浇筑", "duration_days": 5, "quantity": 12,
                 "unit": "m³", "work_type": "混凝土工程"}]}]}]},
        "dependencies": [],
        "cpm_result": {"total_duration_days": 10, "critical_path": ["1.1.1"],
                       "schedule": [{"task_id": "1.1.1", "es": 0, "ef": 5,
                                     "ls": 0, "lf": 5}]},
        "resource_demand": {"tasks": tasks},
        "key_milestones": [{"name": "开工", "date": "2026-09-01",
                            "task_id": "1.1.1", "description": "开工"}],
        "critical_path_tasks": tasks,
        "all_tasks_schedule": tasks,
        "resource_plan": {"total_manpower_days": 40, "peak_manpower": 8,
                          "equipment_peak": {"混凝土输送泵车": 1},
                          "material_summary": []},
        "risks": [],
        "report": "# 监督报告",
    }
    if meta is not None:
        plan["meta"] = meta
    plan.update(over)
    return plan


def _bc(equipment, source="model"):
    """边界条件（`_source` 恒含 `equipment` 键，与 `boundary.annotate_sources` 同形）。"""
    return {"equipment": equipment,
            "_source": {"equipment": source, "project_duration_days": "user"},
            "_source_note": "「user」= 用户明确给出；「model」= 模型按常见做法补齐"}


def _binding_rows():
    """用户申报 → 排程端逐条对账结果（一条未生效、一条生效）。"""
    return {
        "塔吊": {"declared": 2, "bound_to": None, "effective": False,
                 "note": "用户申报的「塔吊」未匹配到计划中的任何机械资源，该限额未生效"},
        "混凝土输送泵车": {"declared": 1, "bound_to": "混凝土输送泵车", "effective": True,
                           "note": "已绑定到计划资源「混凝土输送泵车」，限额 1 生效"},
    }


def _board(plan):
    return Path(D.build_plan_html(plan)).read_text(encoding="utf-8")


def _word_text(plan):
    """Word 的段落 + 所有表格单元格文本（与 test_delivery_workface_visible 同口径）。"""
    from docx import Document
    doc = Document(D.build_plan_docx(plan))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for r in t.rows:
            parts.extend(c.text for c in r.cells)
    return "\n".join(parts)


class _FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def chat_text(self, *a, **kw):
        self.calls += 1
        return self.text


BARE_LLM_HTML = ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
                 "<title>模型编排</title></head><body><h1>施工进度计划看板</h1>"
                 "<p>模型只写了个壳</p></body></html>")


@pytest.fixture()
def tmp_deliverables(tmp_path, monkeypatch):
    """把交付物目录指向 tmp，避免测试往 `输出结果/` 里写东西。"""
    d = tmp_path / "deliverables"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DELIVERABLES_DIR", d)
    monkeypatch.setattr(D.config, "DELIVERABLES_DIR", d)
    return d


# ══════════════════════════════════════════════════════════════
# ① 设备清单三态：用户申报 / 模型估算 / 空清单
# ══════════════════════════════════════════════════════════════

class TestEquipmentDeclaredState:
    def test_三态判据直接可查(self):
        user = _plan(meta={"boundary_conditions": _bc(
            [{"name": "塔吊", "quantity": 2, "unit": "台"}], "user")})
        model = _plan(meta={"boundary_conditions": _bc(
            [{"name": "塔吊", "quantity": 1, "unit": "台"}], "model")})
        empty = _plan(meta={"boundary_conditions": _bc([], "model")})
        empty_user = _plan(meta={"boundary_conditions": _bc([], "user")})
        legacy = _plan(meta={"boundary_conditions": {"equipment": []}})
        assert D.equipment_declared_state(user)[0] == "user"
        assert D.equipment_declared_state(model)[0] == "model"
        assert D.equipment_declared_state(empty)[0] == "empty"
        # 清单为空时**无论 `_source` 是什么**都是"本次没取得清单"
        assert D.equipment_declared_state(empty_user)[0] == "empty"
        # 老计划（没有 `_source`）→ None：按旧行为，一个字都不多写
        assert D.equipment_declared_state(legacy)[0] is None
        assert D.equipment_declared_state(_plan(meta={}))[0] is None

    def test_空清单文案逐字(self):
        plan = _plan(meta={"boundary_conditions": _bc([], "model")})
        _state, text = D.equipment_declared_sentence(plan)
        assert "本次未取得设备清单（边界条件的设备项为空）" in text
        assert "这不等于「你没有申报设备」" in text
        assert "无用户申报设备限额" not in text

    def test_模型估算文案含那半句(self):
        plan = _plan(meta={"boundary_conditions": _bc(
            [{"name": "塔吊", "quantity": 1, "unit": "台"}], "model")})
        _state, text = D.equipment_declared_sentence(plan)
        assert D.MODEL_EQUIPMENT_LABEL in text
        assert "塔吊 1 台" in text


class TestEquipmentBoard:
    def test_用户申报保留逐项对账表(self, tmp_deliverables):
        plan = _plan(meta={"boundary_conditions": _bc(
            [{"name": "塔吊", "quantity": 2, "unit": "台"}], "user"),
            "equipment_binding": _binding_rows()})
        board = _board(plan)
        word = _word_text(plan)
        assert "用户申报设备限额对账" in board
        assert "⚠ 未生效" in board and "未生效" in word
        # 用户真申报了 → 不许出现"未取得设备清单"那句
        assert "未取得设备清单" not in board
        assert "未取得设备清单" not in word

    def test_模型估算清单标明非用户输入且未作限额(self, tmp_deliverables):
        plan = _plan(meta={"boundary_conditions": _bc(
            [{"name": "塔吊", "quantity": 1, "unit": "台"},
             {"name": "施工电梯", "quantity": 1, "unit": "台"}], "model")})
        board = _board(plan)
        word = _word_text(plan)
        for got in (board, word):
            assert D.MODEL_EQUIPMENT_LABEL in got, got[:200]
            assert "塔吊 1 台" in got and "施工电梯 1 台" in got
            assert "无用户申报设备限额" not in got
            assert "未取得设备清单" not in got

    def test_空清单写明未取得设备清单而不甩锅用户(self, tmp_deliverables):
        plan = _plan(meta={"boundary_conditions": _bc([], "model"),
                           "equipment_binding": []})
        board = _board(plan)
        word = _word_text(plan)
        for got in (board, word):
            assert "未取得设备清单" in got, got[:400]
            # 这两句都是甩锅 / 误导用户的原文，一个字都不许再出现
            assert "无用户申报设备限额" not in got
            assert "用户未申报设备" not in got

    def test_空清单带相关告警条数_条数从数据算(self, tmp_deliverables):
        warns = [
            {"node": "boundary",
             "message": "模型返回了边界条件结构，但劳动力/设备/材料三类的值全是空的",
             "detail": "不是用户没给，而是模型这一次没有填", "count": 2},
            {"node": "reporter", "message": "报告缺失一句话", "detail": "", "count": 1},
        ]
        plan = _plan(meta={"boundary_conditions": _bc([], "model"),
                           "node_warnings": warns, "node_warning_count": 2})
        board = _board(plan)
        assert "本次运行有 2 条与设备/边界条件有关的节点级告警" in board
        assert "3 条与设备" not in board          # 不许把无关告警也算进来
        # 没有 `node_warnings` 键（老计划）→ 不写条数，也不崩
        plain = _plan(meta={"boundary_conditions": _bc([], "model")})
        assert D._equipment_related_warning_count(plain) is None
        assert "条与设备" not in _board(plain)

    def test_老计划缺来源标注按旧行为(self, tmp_deliverables):
        # 有 `equipment_binding`、但 `boundary_conditions` 没有 `_source` → 既有对账表
        legacy = _plan(meta={"boundary_conditions": {"equipment": [], "labor": {}},
                             "equipment_binding": _binding_rows()})
        word = _word_text(legacy)
        assert "用户申报设备限额对账" in word and "⚠ 未生效" in word
        assert "未取得设备清单" not in word
        # 连 boundary_conditions 都没有 → 整段不出（修复前就是如此）
        bare = _plan(meta={})
        assert "用户申报设备限额对账" not in _board(bare)
        assert "用户申报设备限额对账" not in _word_text(bare)

    def test_agent页面缺那句实话时被追加(self, tmp_deliverables):
        plan = _plan(meta={"boundary_conditions": _bc([], "model")})
        # ① 走完整 agent 路：模型页面照抄了错误说法（真实产物原文），最终产物里
        #    必须有"未取得设备清单"那句实话（本用例里由确定性追加段带出）。
        page = BARE_LLM_HTML.replace(
            "<p>模型只写了个壳</p>",
            "<h3>用户申报设备限额对账</h3><p>本计划无用户申报设备限额"
            "（equipment_binding 为空），所有设备台数均为 AI 默认口径。</p>")
        ctx = {}
        path, used_agent = D.build_plan_html_agent(plan, _FakeLLM(page), ctx=ctx)
        assert used_agent is True
        html = Path(path).read_text(encoding="utf-8")
        assert "未取得设备清单" in html

    def test_页面齐了别的段但缺设备实话时单独追加(self):
        """设备段的判据独立于 `DELIVERY_MARKERS`：别的段齐了，它照样补。"""
        plan = _plan(meta={"boundary_conditions": _bc([], "model")})
        page = ("<!DOCTYPE html><html lang='zh'><body>"
                "依据 工作面容量 主要机械峰值 施工组织口径 组织缺口 数据来源与置信度"
                "<h3>用户申报设备限额对账</h3><p>本计划无用户申报设备限额"
                "（equipment_binding 为空），所有设备台数均为 AI 默认口径。</p></body></html>")
        merged, added = D._ensure_equipment_section(page, plan)
        assert added is True
        assert "未取得设备清单" in merged
        assert D.EQUIPMENT_FALLBACK_COMMENT in merged
        # 页面里已经有那句实话 → 不重复追加
        again, added2 = D._ensure_equipment_section(merged, plan)
        assert added2 is False and again == merged
        # 老计划（无来源标注）→ 一个字都不动
        legacy = _plan(meta={"boundary_conditions": {"equipment": []}})
        same, added3 = D._ensure_equipment_section(page, legacy)
        assert added3 is False and same == page

    def test_facts里给出该写的那句话(self):
        plan = _plan(meta={"boundary_conditions": _bc([], "model")})
        facts = D._facts_bundle(plan, D._compute_view(plan))
        eq = facts["equipment_declared"]
        assert eq["state"] == "empty"
        assert "未取得设备清单" in eq["sentence"]
        assert "无用户申报设备限额" in eq["how_to_write"]      # 明确禁止那种写法
        # 既有 facts 键不许被动过（别的用例钉着它）
        assert isinstance(facts["equipment_binding"], list)


# ══════════════════════════════════════════════════════════════
# ② 节点级告警
# ══════════════════════════════════════════════════════════════

_WARNS = [
    {"node": "boundary",
     "message": "补全边界条件时模型调用失败，已退回关键词兜底（缺失参数不会被补齐）",
     "detail": "LLM HTTP 500", "at": "2026-09-20T14:20:00", "count": 1},
    {"node": "beat_configs", "message": "9 天/层 超出常见区间", "detail": "",
     "at": "2026-09-20T14:21:00", "count": 1},
]


class TestNodeWarnings:
    def test_无告警时看板与Word都不出现该节(self, tmp_deliverables):
        zero = _plan(meta={"node_warnings": [], "node_warning_count": 0,
                           "model_call_failures": []})
        board = _board(zero)
        word = _word_text(zero)
        assert "节点级告警" not in board
        assert "节点级告警" not in word
        assert "本次运行有 0 条" not in board and "无告警" not in board
        assert "本次运行有 0 条" not in word and "无告警" not in word

    def test_有告警时看板与Word都出现且条数取自数据(self, tmp_deliverables):
        plan = _plan(meta={"node_warnings": _WARNS, "node_warning_count": 2,
                           "model_call_failures": [{"node": "boundary",
                                                    "message": _WARNS[0]["message"],
                                                    "detail": "LLM HTTP 500"}]})
        board = _board(plan)
        word = _word_text(plan)
        line = "本次运行有 2 条节点级告警，其中 1 条是模型调用失败。"
        assert "节点级告警" in board and line in board
        assert "节点级告警" in word and line in word
        for got in (board, word):
            assert "补全边界条件时模型调用失败" in got
            assert "9 天/层 超出常见区间" in got
            assert "beat_configs" in got

    def test_条数键缺失时用同一判据现算(self, tmp_deliverables):
        # `node_warning_count` / `model_call_failures` 都缺 → N 现算、M 现算（同一判据函数）
        plan = _plan(meta={"node_warnings": _WARNS})
        board = _board(plan)
        assert "本次运行有 2 条节点级告警，其中 1 条是模型调用失败。" in board

    def test_告警键缺失或坏数据不抛(self, tmp_deliverables):
        for meta in ({}, {"node_warnings": None}, {"node_warnings": "不是列表"},
                     {"node_warnings": [1, "x", {}, None]}, {"node_warnings": []}):
            plan = _plan(meta=meta)
            assert D.node_warnings_model(plan) is None or \
                D.node_warnings_model(plan)["count"] == 0
            _board(plan)                    # 不许抛
            _word_text(plan)                # 不许抛
        # 空字典条目：count 现算 = 0 → 整节不出
        bad = _plan(meta={"node_warnings": [1, "x", None]})
        assert "节点级告警" not in _board(bad)

    def test_告警标题是二级标题不进目录(self, tmp_deliverables):
        from docx import Document
        plan = _plan(meta={"node_warnings": _WARNS, "node_warning_count": 2})
        doc = Document(D.build_plan_docx(plan))
        titles = [(p.style.name, p.text) for p in doc.paragraphs
                  if (p.style.name or "").startswith("Heading")]
        assert ("Heading 2", "节点级告警") in titles, titles
        # `audit_gate.draft_outline_payload` 的目录只镜像 Heading 1：不许混进一级标题
        assert not [t for s, t in titles if s == "Heading 1" and t == "节点级告警"]

    def test_agent页面缺告警段时被追加(self, tmp_deliverables):
        plan = _plan(meta={"node_warnings": _WARNS, "node_warning_count": 2,
                           "model_call_failures": []})
        ctx = {}
        path, _used = D.build_plan_html_agent(plan, _FakeLLM(BARE_LLM_HTML), ctx=ctx)
        html = Path(path).read_text(encoding="utf-8")
        assert "本次运行有 2 条节点级告警，其中 0 条是模型调用失败。" in html
        assert D.NODE_WARNINGS_FALLBACK_COMMENT in html

    def test_无告警时agent页面一个字都不加(self, tmp_deliverables):
        plan = _plan(meta={"node_warnings": [], "node_warning_count": 0,
                           "model_call_failures": []})
        path, _used = D.build_plan_html_agent(plan, _FakeLLM(BARE_LLM_HTML), ctx={})
        html = Path(path).read_text(encoding="utf-8")
        assert "节点级告警" not in html
        assert D.NODE_WARNINGS_FALLBACK_COMMENT not in html


# ══════════════════════════════════════════════════════════════
# ③ 模型用量：定稿时快照 vs 运行末尾
# ══════════════════════════════════════════════════════════════

class TestUsageDisplay:
    def test_看板显示最终用量并注明定稿时次数(self, tmp_deliverables):
        plan = _plan(meta={"usage": dict(USAGE_BASE),
                           "usage_final": dict(USAGE_FINAL)})
        board = _board(plan)
        assert "0.8018" in board, board[-800:]
        assert "32 次调用" in board
        assert "计划数据定稿时为 30 次调用" in board
        assert "定稿时" in board

    def test_Word显示定稿时快照并注明其后未计入(self, tmp_deliverables):
        plan = _plan(meta={"usage": dict(USAGE_BASE),
                           "usage_final": dict(USAGE_FINAL)})
        word = _word_text(plan)
        assert "0.4038" in word
        assert "未计入" in word
        assert "其后还有节点调用未计入" in word
        # Word 生成于最后一个 LLM 节点之前：**不许**把末尾那个数当自己的数写出来
        assert "0.8018" not in word

    def test_usage_final缺失时两处都不崩(self, tmp_deliverables):
        plan = _plan(meta={"usage": dict(USAGE_BASE)})
        board = _board(plan)
        word = _word_text(plan)
        assert "模型用量" in board and "定稿时" in board
        assert "未计入" in word
        # 拿不到末尾快照 → 明说这不是总量
        assert "不是本次运行总量" in board

    def test_没有任何用量数据时两处都不出现该节(self, tmp_deliverables, monkeypatch):
        monkeypatch.setattr(D, "_usage_snapshots", lambda plan: (None, None, None))
        plan = _plan(meta={})
        assert D.usage_final_line(plan) == ""
        assert D.usage_draft_line(plan) == ""
        # 注意：看板里另有「本次运行未记录模型用量…」（模型参与度提示，另一个功能），
        # 所以这里钉的是**本节标题**而不是"模型用量"四个字。
        assert D.USAGE_TITLE not in _board(plan)
        assert D.USAGE_TITLE not in _word_text(plan)
        assert D.USAGE_FALLBACK_COMMENT not in _board(plan)

    def test_HtmlPageNode把运行末尾用量写回计划JSON(self, tmp_path, tmp_deliverables,
                                                   monkeypatch):
        plans = tmp_path / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "PLANS_DIR", plans)
        monkeypatch.setattr(D.config, "PLANS_DIR", plans)
        plan = _plan(meta={"usage": dict(USAGE_BASE)})
        path = plans / ("%s.json" % plan["plan_id"])
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        # 模拟"运行末尾"：本进程计量器此刻已有 32 次调用（定稿时是 30 次）
        monkeypatch.setattr(D, "_usage_snapshots",
                            lambda p: (p.get("meta", {}).get("usage"), None,
                                       dict(USAGE_FINAL)))
        out = D.HtmlPageNode().run({"plan_json": plan})
        assert out["artifacts"]["html"].endswith(".html")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["meta"]["usage_final"]["calls"] == 32
        assert saved["meta"]["usage_final"]["cost_cny"] == 0.8018
        assert saved["meta"]["usage_note"]
        # `usage`（定稿时快照）保持不动
        assert saved["meta"]["usage"]["calls"] == 30
        # 既有读回校验：meta 过一遍契约模型后两个键都还在（`extra="allow"` 不丢字段）
        back = schemas.PlanMeta.model_validate(saved["meta"]).model_dump()
        assert back["usage_final"]["calls"] == 32
        assert back["usage_note"] == D.USAGE_NOTE
        # 看板（本节点写出的那份）也显示运行末尾用量
        html = Path(out["artifacts"]["html"]).read_text(encoding="utf-8")
        assert "0.8018" in html and "计划数据定稿时为 30 次调用" in html

    def test_离线复看不许新建计划文件(self, tmp_path, tmp_deliverables, monkeypatch):
        plans = tmp_path / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "PLANS_DIR", plans)
        monkeypatch.setattr(D.config, "PLANS_DIR", plans)
        plan = _plan(meta={"usage": dict(USAGE_BASE)})
        # 计划 JSON 不在 plans/ 里（离线复看）→ 不许造文件
        assert D._persist_usage_final(plan, dict(USAGE_FINAL)) == ""
        assert list(plans.glob("*.json")) == []

    def test_运行末尾比定稿时还少时不当成总量(self, tmp_deliverables):
        """同一个进程里更早那次运行留下的数（更小）不许被说成"本次运行总量"。"""
        base = dict(USAGE_BASE)
        stale = dict(USAGE_BASE, calls=7, cost_cny=0.1)
        assert D._live_as_final(stale, base) is None
        assert D._live_as_final(dict(USAGE_FINAL), base) is not None


# ══════════════════════════════════════════════════════════════
# ④ 真实计划（老计划：没有 node_warnings / usage_final）
# ══════════════════════════════════════════════════════════════

@pytest.mark.skipif(not REAL_PLAN.exists(), reason="plans/ 是运行产物，真实计划不在仓库里")
class TestRealPlan:
    def test_空清单写成未取得设备清单(self, tmp_deliverables):
        plan = json.loads(REAL_PLAN.read_text(encoding="utf-8"))
        board = _board(plan)
        word = _word_text(plan)
        assert "未取得设备清单" in board
        assert "未取得设备清单" in word
        assert "无用户申报设备限额" not in board
        assert "无用户申报设备限额" not in word
        # 老计划没有 `node_warnings` → 告警节一个字都不加，也不崩
        assert "节点级告警" not in board
        assert "节点级告警" not in word
        # 有 `meta.usage`（定稿时）而没有 `usage_final` → 看板只说定稿时、Word 写明未计入
        assert "定稿时" in board
        assert "未计入" in word

# -*- coding: utf-8 -*-
"""「本次运行模型没有参与」必须无法被忽略 —— 数据层判定 + 交付物层渲染。

真实故障（本文件的由来）：
  计划 `plan_sample3_after_org` 的 `meta["usage"]` 是
  `{"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    "cost_cny": 0.0, "by_node": {}, "model": ""}` —— **一次模型都没调用**。
  26 个流水线节点全部静默走确定性兜底，流水线照样"成功"产出交付物：209 条任务、
  门都答了、`audit_status=未审计`，**没有任何地方说明"模型没有参与"**。
  后果是硬的：`meta.boundary_conditions` 里 labor/equipment/materials/工期全无值
  （只剩从用户原文正则抽到的节拍）、`meta.equipment_binding` 为空、
  `extracted_params.total_concrete / total_rebar` 为 `null`，而用户会拿它当方案用。

本文件钉两件事：
  ① 数据层：`plan_assembler.build_meta` 恒写入 `model_participation`，判据真源 =
     `meta["usage"]["calls"]`，三态 none / ok / unknown（**未知绝不当成 ok**）；
  ② 交付物层：`level != "ok"` 时看板 HTML 与 Word 正文都出现「模型未参与」，
     `level == "ok"` 时一个字都不加（既有逐字回归门）。

运行：python -m pytest backend/tests/test_model_participation.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pipeline import schemas                              # noqa: E402
from pipeline.nodes import delivery as D                  # noqa: E402
from pipeline.nodes import plan_assembler as PA           # noqa: E402

# fixture 写法照抄 test_delivery.py 的 MINI（同一套 schema 构造 + 交付物隔离由
# tests/conftest.py 全局负责，这里不再自己造临时目录）。
MINI = schemas.PlanJson(
    plan_id="plan_participation_test",
    overview={"project_name": "模型参与度测试项目", "total_duration_days": 30,
              "planned_start_date": "2026-09-01", "planned_end_date": "2026-10-01",
              "critical_path_length": 1},
    wbs={"phases": [{"phase": "主体", "work_packages": [{"id": "1.1", "name": "结构",
        "sub_packages": [{"id": "1.1.1", "name": "柱浇筑", "duration_days": 5,
        "quantity": 10, "unit": "m³", "work_type": "混凝土工程"}]}]}]},
    dependencies=[],
    cpm_result={"total_duration_days": 30, "critical_path": ["1.1.1"],
                "schedule": [{"task_id": "1.1.1", "es": 0, "ef": 5, "ls": 0, "lf": 5}]},
    resource_demand={"tasks": []},
    key_milestones=[{"name": "开工", "date": "2026-09-01", "task_id": "1.1.1",
                     "description": "开工"}],
    critical_path_tasks=[{"task_id": "1.1.1", "task_name": "柱浇筑",
                          "start_date": "2026-09-01", "finish_date": "2026-09-06",
                          "duration_days": 5, "assigned_resources": {"钢筋工": 4}}],
    all_tasks_schedule=[{"task_id": "1.1.1", "task_name": "柱浇筑",
                         "start_date": "2026-09-01", "finish_date": "2026-09-06",
                         "duration_days": 5, "assigned_resources": {"钢筋工": 4}}],
    resource_plan={"total_manpower_days": 20, "peak_manpower": 4,
                   "equipment_peak": {"泵车": 1},
                   "material_summary": [{"name": "concrete", "total_quantity": 100,
                                         "unit": "m³"}]},
    risks=[{"risk_name": "雨季", "mitigation": "排水"}],
    report="# 监督报告",
).model_dump()


def _plan(plan_id, usage=None, participation=None, with_field=True):
    """MINI 计划 + 指定的 meta（usage 与 model_participation 都由调用方给）。"""
    p = dict(MINI, plan_id=plan_id)
    meta = {"audit_status": "未审计"}
    if usage is not None:
        meta["usage"] = usage
    if with_field:
        meta["model_participation"] = (participation if participation is not None
                                       else PA.model_participation(usage))
    p["meta"] = meta
    return p


def _word_text(plan):
    """段落 + 全部表格单元格文本，拼成一个大字符串（照抄 test_delivery.py）。"""
    from docx import Document
    doc = Document(D.build_plan_docx(plan))
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for r in t.rows:
            parts.extend(c.text for c in r.cells)
    return "\n".join(parts)


def _board_html(plan):
    return Path(D.build_plan_html(plan)).read_text(encoding="utf-8")


# ══════════════════════ ① 数据层：三态判定 ══════════════════════

class TestModelParticipationField:
    def test_calls为0判none且note非空(self):
        """一次调用都没有 = 确定性兜底生成的计划，必须自带"请勿据此施工"。"""
        mp = PA.model_participation({"calls": 0, "model": "", "by_node": {}})
        assert mp["participated"] is False
        assert mp["calls"] == 0
        assert mp["level"] == "none"
        assert mp["note"].strip(), "note 不许为空 —— 交付物直接渲染它"
        assert "请勿据此施工" in mp["note"]

    def test_calls大于0判ok并带模型名(self):
        mp = PA.model_participation({"calls": 12, "model": "qwen-plus"})
        assert mp["participated"] is True
        assert mp["calls"] == 12
        assert mp["level"] == "ok"
        assert mp["model"] == "qwen-plus"
        assert mp["note"] == ""              # ok 时交付侧一个字都不加

    def test_usage缺失或取不到calls时判unknown绝不当ok(self):
        for bad in (None, {}, "x", [], {"calls": None}, {"calls": "abc"},
                    {"calls": -1}, {"by_node": {}}):
            mp = PA.model_participation(bad)
            assert mp["level"] == "unknown", bad
            assert mp["level"] != "ok", "未知绝不能被读成合格：%r" % (bad,)
            assert mp["participated"] is None, bad
            assert mp["calls"] is None, bad
            assert mp["note"].strip(), bad

    def test_build_meta恒写入该字段且与usage自洽(self):
        """字段恒存在，且值必须等于"用同一份 usage 现算"的结果（真源就是 usage）。"""
        for ctx in ({}, {"extracted_params": {"floors": 3}},
                    {"wbs": {"phases": []}}):
            meta = PA.build_meta(ctx)
            assert "model_participation" in meta, "字段必须恒存在"
            assert meta["model_participation"] == PA.model_participation(meta["usage"])
            # 判据真源是 meta["usage"]["calls"]，不是别的什么
            assert meta["model_participation"]["level"] in ("none", "ok", "unknown")


# ══════════════════════ ② 交付物层：看板 + Word ══════════════════════

class TestDeliveryRendersNotice:
    def test_没有模型参与时看板与Word都出现模型未参与(self):
        plan = _plan("plan_mp_none", usage={"calls": 0, "model": "", "by_node": {}})
        html = _board_html(plan)
        word = _word_text(plan)
        assert "模型未参与" in html
        assert "模型未参与" in word
        # 调用次数取自数据（0 次），不是另编一句话
        assert "模型调用次数：0" in html
        assert "模型调用次数：0" in word
        # note 原文必须完整落到交付物上（用户要看到"请勿据此施工"）
        assert "请勿据此施工" in html and "请勿据此施工" in word

    def test_usage未知时也要显眼提示不许静默(self):
        """老计划没有 model_participation，但 usage 在 → 从 usage 现算，仍要提示。"""
        plan = _plan("plan_mp_legacy", usage={"calls": 0, "model": ""},
                     with_field=False)
        assert "模型未参与" in _board_html(plan)
        assert "模型未参与" in _word_text(plan)

    def test_模型参与时一个字都不加(self):
        plan = _plan("plan_mp_ok", usage={"calls": 12, "model": "qwen-plus"})
        assert D.model_participation_notice(plan) == ""
        html = _board_html(plan)
        word = _word_text(plan)
        assert "模型未参与" not in html
        assert "模型未参与" not in word
        assert "模型调用次数" not in html and "模型调用次数" not in word

    def test_文案里的调用次数来自数据而非写死(self):
        """level=none 但 calls 由数据给出（这里是 3）→ 交付物必须印 3。"""
        plan = _plan("plan_mp_count", with_field=False)
        plan["meta"]["model_participation"] = {
            "participated": False, "calls": 3, "level": "none",
            "note": "（测试用 note）"}
        notice = D.model_participation_notice(plan)
        assert "模型调用次数：3" in notice
        assert "模型调用次数：3" in _board_html(plan)
        assert "模型调用次数：3" in _word_text(plan)

    def test_未知态同样渲染且写明无法判断(self):
        plan = _plan("plan_mp_unknown", usage=None, with_field=False)
        plan["meta"]["model_participation"] = PA.model_participation(None)
        assert plan["meta"]["model_participation"]["level"] == "unknown"
        notice = D.model_participation_notice(plan)
        assert "模型未参与" in notice and "无法判断" in notice
        assert "模型未参与" in _board_html(plan)
        assert "模型未参与" in _word_text(plan)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for cls in (TestModelParticipationField, TestDeliveryRendersNotice):
        for name in [n for n in dir(cls) if n.startswith("test_")]:
            getattr(cls(), name)()
            print("  PASS  %s.%s" % (cls.__name__, name))
    print("\n全部用例通过 ✔")

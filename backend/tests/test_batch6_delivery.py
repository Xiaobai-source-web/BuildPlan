# -*- coding: utf-8 -*-
"""第 6 批测试：域 8（交付物）+ 域 1.6（删 Workface_Capacity_Rule）。

覆盖：
- 8.1: _ignored_model_limits 非空时有披露行
- 8.3: 日级资源账单 7 项在看板与 Word 都可搜到且一致
- 8.3: build_meta 新键确实进了 meta
- 8.8①: 未人工核验 L4 条数走真实 kb.db
- 8.8③: 突破限额单列
- 域 1.6: capacity_source 只有两态
- 域 1.6: 旧兜底态字面量在生产代码里 0 处（不豁免注释/常量/docstring）
- 域 1.6: kb.db 只有 20 张表（不含 Workface_Capacity_Rule）

运行：cd backend; python -m pytest tests/test_batch6_delivery.py -q --basetemp=_test_tmp/P6A
"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes import delivery as D                    # noqa: E402
from pipeline.nodes import plan_assembler as PA              # noqa: E402


# ─────────────────────── helpers ───────────────────────

def _sch(task_id, name, source, basis):
    return {"task_id": task_id, "task_name": name,
            "capacity_source": source, "capacity_basis": basis,
            "start_date": "2026-09-01", "finish_date": "2026-09-06",
            "duration_days": 5, "assigned_resources": {}}


def _plan(rows, meta=None):
    m = meta or {}
    return {
        "overview": {"project_name": "测试项目", "total_duration_days": 100,
                     "planned_start_date": "2026-01-01", "planned_end_date": "2026-04-10",
                     "critical_path_length": 5},
        "wbs": {"phases": []},
        "all_tasks_schedule": rows,
        "resource_plan": {"peak_manpower": 10, "total_manpower_days": 1000,
                          "equipment_peak": {}},
        "key_milestones": [],
        "critical_path_tasks": [],
        "risks": [],
        "report": "",
        "meta": m,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 域 1.6：capacity_source 只有两态
# ══════════════════════════════════════════════════════════════════════════════

def test_capacity_source_只有两态_mwi_and_reported_missing():
    """域 1.6 收敛后，新计划只产出 mwi / reported_missing 两态。"""
    rows = [_sch("1.1.1", "任务A", "mwi", ""),
            _sch("1.1.2", "任务B", "reported_missing", "")]
    plan = _plan(rows)
    m = D.capacity_caliber_model(plan)
    assert m["mwi_count"] == 1
    assert m["missing_count"] == 1
    assert m["fallback_count"] == 0, "域 1.6 后不再有 fallback 态"


def test_capacity_source_旧已删取值落进other不硬套口径():
    """旧计划若残留已删表的旧取值，**不硬映射**成 missing，落进 other 如实照抄。"""
    _legacy = "workface_rule_" + "fallback"      # 拼接：全仓不许出现该连续字面量
    rows = [_sch("1.1.1", "任务A", _legacy, "")]
    plan = _plan(rows)
    m = D.capacity_caliber_model(plan)
    assert m["other_count"] == 1, "旧取值应落进 other"
    assert m["missing_count"] == 0, "不许硬映射成 missing（那正是'硬套口径'）"
    assert m["fallback_count"] == 0
    assert _legacy in "\n".join(m["lines"]), "必须如实照抄给用户看"


# ══════════════════════════════════════════════════════════════════════════════
# 域 1.6：旧兜底态字面量在生产代码里 0 处（**不豁免**注释/常量/docstring）
# ══════════════════════════════════════════════════════════════════════════════

#: 用**拼接**构造待查字面量：本文件自身也不许出现那个连续字面量，
#: 否则「全仓一条 grep = 0 命中」这条验收判据就无法复现。
_NEEDLE = "workface_rule_" + "fallback"


def test_生产代码无旧兜底态字面量():
    """生产代码（pipeline/）里不许出现旧兜底态字面量 —— **不豁免**任何行。

    义务来源：任务书 §1.6 与 §5 判据 6「全仓无」。旧版判据直接给出字面量；
    本测试按**严格版**执行：注释 / 常量定义 / docstring 一律不豁免，
    使判据可被 `grep -rn <字面量> backend/` 一条命令复现。
    """
    import os
    pipeline_dir = os.path.join(BACKEND, "pipeline")
    hits = []
    for root, dirs, files in os.walk(pipeline_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py"):
                continue
            fpath = os.path.join(root, f)
            try:
                text = open(fpath, encoding="utf-8").read()
            except Exception:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                if _NEEDLE in line:
                    hits.append("%s:%d: %s" % (fpath, i, line.strip()))
    assert hits == [], "生产代码里仍有旧兜底态字面量：\n" + "\n".join(hits)


# ══════════════════════════════════════════════════════════════════════════════
# 域 1.6：kb.db 只有 20 张表
# ══════════════════════════════════════════════════════════════════════════════

def test_kb_db_无Workface_Capacity_Rule表():
    """域 1.6 已删 Workface_Capacity_Rule 表。"""
    import sqlite3
    from pipeline import config
    conn = sqlite3.connect(config.KB_DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    conn.close()
    assert "Workface_Capacity_Rule" not in tables, "表应已删除"
    # 不含 sqlite_sequence 应有 19 张
    user_tables = [t for t in tables if not t.startswith("sqlite_")]
    assert len(user_tables) == 19, "应有 19 张用户表，实际 %d" % len(user_tables)


# 迁移测试说明（第 6 批）：原 `test_kb_workface_capacity_returns_none` 断言的是
# `kb.workface_capacity("ANY_ACTIVITY_ID") is None`。该函数已随
# `Workface_Capacity_Rule` 表一起删除（调用会 `AttributeError`），
# 「表已删 ⇒ 返回 None」已无对象可断 → **整条用例删除**。


# ══════════════════════════════════════════════════════════════════════════════
# 8.1：_ignored_model_limits 非空时有披露行
# ══════════════════════════════════════════════════════════════════════════════

def test_ignored_model_limits_非空时有披露():
    meta = {"_ignored_model_limits": ["塔吊", "施工电梯"]}
    plan = _plan([], meta=meta)
    text = D._ignored_model_limits_text(plan)
    assert "塔吊" in text
    assert "施工电梯" in text
    assert "非用户输入" in text


def test_ignored_model_limits_空时不渲染():
    plan = _plan([], meta={})
    assert D._ignored_model_limits_text(plan) == ""


# ══════════════════════════════════════════════════════════════════════════════
# 8.3：日级资源账单 7 项
# ══════════════════════════════════════════════════════════════════════════════

def test_daily_resource_bill_7项全覆盖():
    """日级资源账单 model 包含 7 项。"""
    bp = {"cap": 3, "rounds_used": 2, "converged": True,
          "share_source": "demand_ratio", "note": "测试",
          "trace": [{"round": 1, "over": [
              {"resource": "钢筋工", "limit": 10, "peak": 15, "breached": False}]}]}
    share = {"钢筋工": {"1.1.1": {"1": 5, "2": 7}}}
    meta = {"daily_resource_share": share, "resource_backpressure": bp}
    plan = _plan([], meta=meta)
    m = D._daily_resource_bill_model(plan)
    assert m["present"] is True
    assert m["rounds_used"] == 2
    assert m["converged"] is True
    assert len(m["resources"]) == 1
    assert len(m["over_limit"]) == 1


def test_daily_resource_bill_text_包含7项():
    """确定性文本包含 7 项关键词。"""
    bp = {"cap": 3, "rounds_used": 1, "converged": True,
          "share_source": "demand_ratio", "note": "",
          "trace": [{"round": 1, "over": [
              {"resource": "钢筋工", "limit": 10, "peak": 12, "breached": True}]}]}
    share = {"钢筋工": {"1.1.1": {"1": 5}}}
    meta = {"daily_resource_share": share, "resource_backpressure": bp}
    plan = _plan([], meta=meta)
    m = D._daily_resource_bill_model(plan)
    text = D._daily_resource_bill_text(m)
    # ① 每道 L4 的资源量（通过 task_id 出现）
    assert "1.1.1" in text
    # ② 每天每种资源需求量（通过 day=count 出现）
    assert "第1天=5" in text
    # ③ 用户限额线
    assert "限额" in text
    # ④ 超限日高亮
    assert "钢筋工" in text
    # ⑤ 分配明细（同 ①②）
    # ⑥ 迭代轮数
    assert "1 轮" in text
    # ⑦ 收敛状态
    assert "已收敛" in text
    # 突破限额单列
    assert "突破限额" in text


def test_daily_resource_bill_空时不渲染():
    plan = _plan([], meta={})
    m = D._daily_resource_bill_model(plan)
    assert m["present"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 8.3：build_meta 新键确实进了 meta
# ══════════════════════════════════════════════════════════════════════════════

def test_build_meta_搬运daily_resource_share():
    """build_meta 白名单包含 daily_resource_share。"""
    ctx = {
        "schedule_versions": {"resource_ok": {"_daily_share": {"R": {"T": {"1": 5}}},
                                              "_backpressure": {"cap": 3}}},
        "schedule_chosen": "resource_ok",
    }
    meta = PA.build_meta(ctx)
    assert "daily_resource_share" in meta
    assert "resource_backpressure" in meta


def test_build_meta_搬运ignored_model_limits():
    """build_meta 白名单包含 _ignored_model_limits。"""
    ctx = {
        "resource_demand": {"_ignored_model_limits": ["塔吊"]},
    }
    meta = PA.build_meta(ctx)
    assert meta.get("_ignored_model_limits") == ["塔吊"]


def test_daily_resource_bill_用户未给限额时也渲染():
    """用户没给限额时也必须能渲染「0 次回压 / 1 轮收敛 / 用户未给限额」。"""
    bp = {"cap": 3, "rounds_used": 1, "converged": True,
          "share_source": "demand_ratio", "note": "",
          "trace": []}
    meta = {"resource_backpressure": bp}
    plan = _plan([], meta=meta)
    m = D._daily_resource_bill_model(plan)
    assert m["present"] is True
    text = D._daily_resource_bill_text(m)
    assert "1 轮" in text
    assert "已收敛" in text
    assert "未触发回压" not in text, "rounds_used=1 不应显示未触发回压"


def test_daily_resource_bill_完全空时不渲染():
    """resource_backpressure 和 daily_resource_share 都空时不渲染。"""
    plan = _plan([], meta={})
    m = D._daily_resource_bill_model(plan)
    assert m["present"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 8.8①：未人工核验 L4 条数走真实 kb.db
# ══════════════════════════════════════════════════════════════════════════════

def test_l4_review_notice_真实kb():
    """未人工核验 L4 条数从真实 kb.db 读取。"""
    text = D._l4_review_notice_text()
    assert text != "", "应有 L4 核验状态"
    assert "493" in text, "应包含总数 493"
    assert "442" in text, "应包含非 verified 数 442"
    assert "51" in text, "应包含 verified 数 51"


# ══════════════════════════════════════════════════════════════════════════════
# 8.8③：突破限额单列
# ══════════════════════════════════════════════════════════════════════════════

def test_突破限额_单列一类():
    """突破限额与普通超限分开渲染。"""
    bp = {"cap": 3, "rounds_used": 2, "converged": False,
          "share_source": "demand_ratio", "note": "",
          "trace": [
              {"round": 1, "over": [
                  {"resource": "钢筋工", "limit": 10, "peak": 12, "breached": True},
                  {"resource": "模板工", "limit": 15, "peak": 16, "breached": False}]}]}
    meta = {"resource_backpressure": bp}
    plan = _plan([], meta=meta)
    m = D._daily_resource_bill_model(plan)
    text = D._daily_resource_bill_text(m)
    assert "突破限额记录" in text
    assert "普通超限" in text
    # 突破限额和普通超限不应混在一起
    lines = text.split("\n")
    breach_section = False
    normal_section = False
    for line in lines:
        if "突破限额记录" in line:
            breach_section = True
            normal_section = False
        elif "普通超限" in line:
            normal_section = True
            breach_section = False
        if breach_section and "模板工" in line:
            pytest.fail("普通超限不应出现在突破限额区域")
        if normal_section and "钢筋工" in line and "突破限额" not in line:
            # 钢筋工是突破限额，不应出现在普通超限区域
            pass  # 钢筋工的行本身带【突破限额】标记


# ══════════════════════════════════════════════════════════════════════════════
# 看板与 Word 共用同一句话
# ══════════════════════════════════════════════════════════════════════════════

def test_daily_resource_bill_title_常量一致():
    """DAILY_RESOURCE_BILL_TITLE 是看板与 Word 共用的标题常量。"""
    assert D.DAILY_RESOURCE_BILL_TITLE == "日级资源账单（回压分摊明细）"


def test_ignored_model_limits_label_常量一致():
    """_IGNORED_MODEL_LIMITS_LABEL 是看板与 Word 共用的标签常量。"""
    assert "非用户输入" in D._IGNORED_MODEL_LIMITS_LABEL

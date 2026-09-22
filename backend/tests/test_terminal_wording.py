# -*- coding: utf-8 -*-
"""两处措辞/提示的收口（第 31 轮）—— 用户实测反馈引出。

1. `plan_final` 事件在**方案组装落盘**时就发，那时三轮回审门还没走完、计划是「未审计」、
   定稿 Word 与看板都还没产出。原来打的是「📄 最终方案已生成」，用户实测反问：
   「明明我刚刚还输入了 WBS 的意见，用户都还没有审批，为什么叫最终方案？」
   → 现在必须写明"未审计"、且**不许**出现"最终/已交付"这类字样。
2. 审计门的"意见菜单"态，输入提示原来是 `[Y=审过 / 直接输入审计意见=退回]`，
   与菜单里的 `[1]/[2]/[3]` 对不上，用户不知道该回编号。
   → 后端在菜单态多带一个 `options_hint`，终端据此换提示；没有该字段时提示**一字不变**。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
for p in (str(BACKEND), str(ROOT / "terminal")):
    if p not in sys.path:
        sys.path.insert(0, p)

import confirmer  # noqa: E402
import renderer   # noqa: E402

_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def _plain(t):
    return _ANSI.sub("", str(t))


# ---------------- 1. plan_final 的措辞 ----------------
def _plan_final_text(**extra):
    data = {"plan_id": "plan_x",
            "plan_overview": {"project_name": "某项目", "total_duration_days": 10,
                              "planned_start_date": "2026-01-01",
                              "planned_end_date": "2026-01-11",
                              "leaves": 3, "critical": 1, "peak_labor": 5},
            "saved_path": "backend/plans/plan_x.json"}
    data.update(extra)
    return _plain(renderer.render_event("plan_final", data))


def test_计划落盘不许说成最终方案或已交付():
    text = _plan_final_text()
    for bad in ("最终方案", "已交付", "定稿 Word 已"):
        assert bad not in text, "「%s」会让人以为已验收通过：\n%s" % (bad, text)


def test_计划落盘必须写明尚未审计():
    text = _plan_final_text()
    assert "未审计" in text, text
    assert "计划数据" in text, text
    # 并且要说清定稿/看板还没产出
    assert "定稿" in text and "看板" in text, text


def test_计划落盘仍保留编号与路径():
    text = _plan_final_text()
    assert "plan_x" in text and "plan_x.json" in text, text


def test_审计未通过时的收尾措辞不许说已交付():
    src = (BACKEND / "pipeline" / "nodes" / "audit_gate.py").read_text(encoding="utf-8")
    assert "计划本身已交付" not in src, "收尾消息不许再说「计划本身已交付」"
    assert "不能当作已交付的成果" in src, "应明确写清：落盘的只是计划数据，不能当作成果"


# ---------------- 2. 菜单态的输入提示 ----------------
def test_菜单态要用编号提示(monkeypatch):
    """后端给了 options_hint → 终端提示必须换成"输入 1/2/3"。"""
    seen = {}

    def _fake_ask(prompt):
        seen["prompt"] = prompt
        return "y"                                     # 直接通过，避免进循环

    out = []
    monkeypatch.setattr(confirmer, "_ask", _fake_ask)
    monkeypatch.setattr(confirmer.tui, "out", lambda *a, **k: out.append(a))
    monkeypatch.setattr(confirmer, "_client_post", lambda *a, **k: None, raising=False)

    class _C:
        def post_params(self, *a, **k):
            pass

    confirmer._ask_audit(_C(), {"review_id": "r1",
                                "options_hint": "  [输入 1 / 2 / 3 选择] "}, "r1", "run")
    assert "1 / 2 / 3" in seen["prompt"] or "1/2/3" in seen["prompt"], seen["prompt"]


def test_非菜单态提示一字不变(monkeypatch):
    seen = {}

    def _fake_ask(prompt):
        seen["prompt"] = prompt
        return "y"

    monkeypatch.setattr(confirmer, "_ask", _fake_ask)
    monkeypatch.setattr(confirmer.tui, "out", lambda *a, **k: None)

    class _C:
        def post_params(self, *a, **k):
            pass

    confirmer._ask_audit(_C(), {"review_id": "r1"}, "r1", "run")
    assert seen["prompt"].strip() == "[Y=审过 / 直接输入审计意见=退回]", seen["prompt"]


def test_后端只在菜单态带options_hint():
    src = (BACKEND / "pipeline" / "nodes" / "audit_gate.py").read_text(encoding="utf-8")
    assert 'if menu:' in src and 'payload["options_hint"]' in src, \
        "options_hint 必须只在菜单态（menu=True）出现，否则非菜单态的提示也会被换掉"
    assert "menu=bool(pending)" in src, "菜单态要由 pending 决定"

# -*- coding: utf-8 -*-
"""端到端：用户在粒度门敲下的那一个号，必须一路走到交付文件里。

为什么单独一个文件：此前的测试各自守一半 ——
  · `test_plan_level.py::test_printed_picker_is_actually_honored` 守"门上印的号 → ctx"；
  · `test_delivery_granularity.py` 守"plan.meta.display_granularity → 文件"。
中间那一跳（ctx → `build_meta` → `plan_json` → 交付节点）**没有任何测试**，
而它恰恰是用户真正在问的那句：**"我选的这个，到底被用上了没有？"**

本文件用真实节点（`PlanLevelNode` → `build_parts` → `assemble_plan_json` →
`build_plan_html` / `build_plan_docx`）把六个号逐个跑通，断言：

  1. 门上印的行数 = 落进 `meta.display_granularity.rows` = 看板 WBS 表行数
     = 看板甘特条数 = Word 横道表行数；
  2. 粗粒度的六个号各自对应正确的 L3/L4；
  3. WBS 树**一行都没动**（叶子数、id 集合都不变）；
  4. 监督报告里带着与选择一致的口径句。

交付物写进 `tmp_path`，不碰 `输出结果/`。
"""

import re
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline import quantity                                        # noqa: E402
from pipeline.nodes import delivery as D                             # noqa: E402
from pipeline.nodes.plan_assembler import (assemble_plan_json, build_parts,  # noqa: E402
                                           template_report)
from pipeline.nodes.plan_level import LEVEL_L3, LEVEL_L4, PlanLevelNode       # noqa: E402

START = "2026-03-01"


def _leaf(tid, location, step, wtype, qty, unit, dur):
    return {"id": tid, "name": "Ⅰ区 %s %s" % (location, step),
            "location": location, "duration_days": dur, "quantity": qty,
            "unit": unit, "work_type": wtype, "_step_name": step,
            "kb_activity_id": None}


# 三层 × 两道工序 + 一条分层外：足够让"按层 / 每 5 层 / 整栋"分出不同行数
_LEAVES = [
    _leaf("5.1.1.1", "1-1层", "钢筋绑扎", "钢筋工程", 10.0, "t", 2),
    _leaf("5.1.1.2", "1-1层", "模板安装", "模板工程", 100.0, "m²", 2),
    _leaf("5.1.2.1", "2-2层", "钢筋绑扎", "钢筋工程", 12.0, "t", 3),
    _leaf("5.1.2.2", "2-2层", "模板安装", "模板工程", 120.0, "m²", 3),
    _leaf("5.1.3.1", "6-6层", "钢筋绑扎", "钢筋工程", 12.0, "t", 3),
    _leaf("5.1.3.2", "6-6层", "模板安装", "模板工程", 120.0, "m²", 3),
    _leaf("8.1.1.1", "全楼", "内墙抹灰", "抹灰工程", 900.0, "m²", 5),
]

_SCHED = [("5.1.1.1", 0, 2), ("5.1.1.2", 2, 4), ("5.1.2.1", 4, 7),
          ("5.1.2.2", 7, 10), ("5.1.3.1", 10, 13), ("5.1.3.2", 13, 16),
          ("8.1.1.1", 16, 21)]


def _day(n):
    import datetime
    return (datetime.date.fromisoformat(START) + datetime.timedelta(days=n)).isoformat()


def _wbs():
    return {"phases": [
        {"phase": "地上主体结构", "work_packages": [
            {"id": "5.1", "name": "主体结构", "sub_packages": _LEAVES[:6]}]},
        {"phase": "装饰装修", "work_packages": [
            {"id": "8.1", "name": "装饰装修", "sub_packages": _LEAVES[6:]}]},
    ]}


def _ctx():
    return {
        "wbs": _wbs(),
        "extracted_params": {"project_name": "端到端测试", "planned_start_date": START},
        "cpm_result": {
            "total_duration_days": 21, "critical_path": ["5.1.1.1"],
            "schedule": [{"task_id": t, "es": a, "ef": b, "ls": a, "lf": b}
                         for t, a, b in _SCHED],
        },
        "resource_demand": {"tasks": []},
    }


class _StubReg(object):
    """最小交互桩：wait 直接返回预置决策，不阻塞。"""

    def __init__(self, decision):
        self.decision = decision
        self.registered = []

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=600):
        return dict(self.decision)


def _run_gate(ctx, decision):
    node = PlanLevelNode()
    events = []
    node._emit = lambda event, data: events.append((event, data))
    node._run_id = "t_e2e"
    node._registry = _StubReg(decision)
    node.run(ctx)
    gate = [d for e, d in events if e == "param_review"][-1]
    return gate


@pytest.fixture
def deliver_dir(tmp_path, monkeypatch):
    """交付物写临时目录，避免污染 输出结果/。"""
    monkeypatch.setattr(D.config, "DELIVERABLES_DIR", tmp_path / "deliverables")
    return tmp_path / "deliverables"


def _plan_of(ctx):
    parts = build_parts(ctx)
    plan = assemble_plan_json(ctx, parts)
    plan["plan_id"] = "plan_e2e"
    return plan, parts


def _all_ids(wbs):
    return [s["id"] for ph in wbs["phases"] for wp in ph["work_packages"]
            for s in wp["sub_packages"]]


def test_六个号逐个走到交付文件(deliver_dir):
    ref_ctx = _ctx()
    gate0 = _run_gate(_ctx(), {"passed": True})
    options = gate0["picker"]["options"]
    assert [o["no"] for o in options] == [1, 2, 3, 4, 5, 6]
    assert len({(o["depth"], o["floor_grouping"]) for o in options}) == 6, \
        "六个号必须是六个不同组合"
    leaf_ids = _all_ids(ref_ctx["wbs"])

    seen_rows = set()
    for opt in options:
        no = str(opt["no"])
        ctx = _ctx()
        gate = _run_gate(ctx, {"passed": False, "manual_input": no})

        # ① 门上印的行数 = 节点记下的行数
        printed = [o for o in gate["picker"]["options"] if o["no"] == opt["no"]][0]
        g = ctx["display_granularity"]
        assert (g["depth"], g["floor_grouping"]) == (opt["depth"],
                                                     opt["floor_grouping"]), no
        assert g["rows"] == printed["rows"], no

        # ② 粗 ↔ L3、细 ↔ L4
        want_level = LEVEL_L3 if opt["depth"] == quantity.DEPTH_COARSE else LEVEL_L4
        assert ctx["plan_level"] == want_level, no

        # ③ 落进计划 JSON
        plan, parts = _plan_of(ctx)
        assert plan["meta"]["display_granularity"] == g, no

        # ④ 三个交付面行数一致
        html = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
        wbs_seg = html.split("工作分解结构")[1].split("</table>")[0]
        html_rows = len(re.findall(r"<tr>", wbs_seg)) - 1
        gantt_rows = int(re.search(r'"__totalTasks": (\d+)', html).group(1))
        opt_json = D.echarts_page.build_chart_options(plan, D._compute_view(plan))
        assert gantt_rows == opt_json["gantt"]["__totalTasks"]
        assert html_rows == g["rows"], (no, html_rows, g["rows"])
        assert gantt_rows == g["rows"], (no, gantt_rows, g["rows"])

        import docx
        doc = docx.Document(D.build_plan_docx(plan))
        # Word 侧必须按表头**钉住横道表**：`任务 ID` 已不再唯一（D6 新增的
        # 「来源档次 / 无定额依据说明」表也用同一表头，且排在横道表之前）。
        # 取"第一张含 任务 ID 的表"会拿到那张表 —— 它的行数由来源档次决定，
        # 与展示粒度无关（实测 no=3：误取到 7 行，而横道表 5 行 = g["rows"]）。
        word_rows = None
        for t in doc.tables:
            hdr = [c.text.strip() for c in t.rows[0].cells]
            if "任务 ID" in hdr and "开始(D)" in hdr and len(t.rows) > 1:
                word_rows = len(t.rows) - 1
                break
        assert word_rows == g["rows"], (no, word_rows, g["rows"])

        # ⑤ WBS 树一行都没动
        assert _all_ids(plan["wbs"]) == leaf_ids, "展示粒度不许改树"

        # ⑥ 报告口径句与选择一致
        rep = template_report(parts)
        assert "展示口径" in rep and quantity.DEPTH_LABELS[opt["depth"]] in rep
        assert quantity.FLOOR_LABELS[opt["floor_grouping"]] in rep
        assert "%s 行" % g["rows"] in rep or g["rows"] == 0

        # ⑦ 六个号若真给出六种行数，就说明粒度确实在起作用（不是摆设）
        seen_rows.add((g["depth"], g["floor_grouping"], g["rows"]))

        shutil.rmtree(Path(D._plan_dir(plan)), ignore_errors=True)

    assert len({(d, f) for d, f, _ in seen_rows}) == 6
    assert len({r for _, _, r in seen_rows}) >= 3, \
        "六个组合至少该给出三种不同行数（楼层分段是主杠杆）：%s" % sorted(seen_rows)


def test_未选时逐叶子且无口径污染(deliver_dir):
    """打 Y（用推荐值）也要真的落盘生效，不是"只有手动选才算"。"""
    ctx = _ctx()
    gate = _run_gate(ctx, {"passed": True})
    rec = gate["options"]["recommend"]
    g = ctx["display_granularity"]
    assert (g["depth"], g["floor_grouping"]) == (rec["depth"],
                                                 rec["floor_grouping"])
    plan, _ = _plan_of(ctx)
    assert plan["meta"]["display_granularity"]["rows"] == g["rows"]
    html = Path(D.build_plan_html(plan)).read_text(encoding="utf-8")
    assert int(re.search(r'"__totalTasks": (\d+)', html).group(1)) == g["rows"]
    shutil.rmtree(Path(D._plan_dir(plan)), ignore_errors=True)

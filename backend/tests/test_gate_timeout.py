# -*- coding: utf-8 -*-
"""第 35 轮真实缺陷：「我在人工门输入 1，流程反而取消了」

## 用户原话
「为什么我输入1，流程反而取消了？」（截图：WBS 复评门上输入 `1`，界面回「⏹ 流程已取消」）

## 根因（已用探针逐条排除，见 backend/_probe_gate1b.py）
把"用户按 1"的所有可能决策形态喂给真实的 `wbs_agent._human_gate`：

| 决策形态 | 判定 |
|---|---|
| `{action: repair, repair_key: reexpand_wbs}`（终端按契约发） | OK repair |
| `{action: revise, instruction: "1"}`（没认出编号，当自由意见） | OK revise |
| `{action: revise, manual_input: "1"}`（老终端） | OK approve |
| `{instruction: "1"}`（没有 action） | OK revise |
| `{action: continue}` / 空决策 | OK approve |
| **`{action: abort}`（引擎超时兜底）** | **中止** |

**只有最后一条会取消**。`registry.wait` 在超过 `timeout` 之后无条件返回
`{"action": "abort"}`，而旧上限是 **600 秒（10 分钟）** —— 用户在第 6 步"参数补充"上
看参数、翻资料很容易超过 10 分钟，门早就悄悄超时关掉了；他之后输入 `1`，那一行其实
是在回答一道**已经过期**的题，界面上就表现为"输入 1 反而取消"。

## 本文件钉住
1. 人工门的等待上限是 **30 分钟**，且只有**一处**常量（不许再散落硬编码）；
2. 超时返回的 abort **带 reason="timeout"**，与"用户取消"区分开（文案不一样）；
3. 引擎给出**可读的收尾说明**，不再是光秃秃一句"流程已取消"；
4. 用户那一行**没送达**时，终端必须说出来（原来是完全静默 —— 观感的直接来源）。

运行：python -m pytest backend/tests/test_gate_timeout.py -q
"""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

from pipeline.registry import GATE_TIMEOUT_SECONDS, InteractionRegistry  # noqa: E402


# ==================== 1. 等待上限 ====================

def test_人工门等待上限是60分钟():
    """用户实测超过 10 分钟就会踩坑；门必须给足时间。

    第 35 轮发现这个缺陷时先提到 30 分钟，第 36 轮同一症状又被实测打回来一次
    （「为什么我输入 Y，却直接退出了计划」），于是再提到 60 分钟。
    """
    assert GATE_TIMEOUT_SECONDS == 3600, GATE_TIMEOUT_SECONDS


def test_等待上限可以用环境变量覆盖(monkeypatch):
    """现场可调：BUILDPLAN_GATE_TIMEOUT（秒）。"""
    import importlib

    import pipeline.registry as reg

    monkeypatch.setenv("BUILDPLAN_GATE_TIMEOUT", "120")
    importlib.reload(reg)
    try:
        assert reg.GATE_TIMEOUT_SECONDS == 120
    finally:
        monkeypatch.delenv("BUILDPLAN_GATE_TIMEOUT", raising=False)
        importlib.reload(reg)
    assert reg.GATE_TIMEOUT_SECONDS == 3600


def test_环境变量给垃圾值时回到默认(monkeypatch):
    import importlib

    import pipeline.registry as reg

    for bad in ("", "abc", "-5", "0"):
        monkeypatch.setenv("BUILDPLAN_GATE_TIMEOUT", bad)
        importlib.reload(reg)
        assert reg.GATE_TIMEOUT_SECONDS == 3600, bad
    monkeypatch.delenv("BUILDPLAN_GATE_TIMEOUT", raising=False)
    importlib.reload(reg)


def test_超时上限只有一处常量():
    """不许再散落硬编码 —— 散落就是这次缺陷的成因（10 处各自写 600）。"""
    offenders = []
    for p in (BACKEND / "pipeline").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "timeout=600" in text and p.name != "registry.py":
            offenders.append(p.relative_to(ROOT).as_posix())
    assert not offenders, "还在硬编码 600 秒：%s" % offenders


def test_所有人工门都用了同一个上限():
    """每道门都要显式用它 —— 漏一个就还是 10 分钟（默认值来自常量，但显式更清楚）。"""
    gates = ["doc_load.py", "param_review.py", "confirm.py", "audit_gate.py",
             "plan_level.py", "work_confirm.py", "router.py", "wbs_agent.py"]
    missing = []
    for name in gates:
        text = (BACKEND / "pipeline" / "nodes" / name).read_text(encoding="utf-8")
        assert "GATE_TIMEOUT_SECONDS" in text, "%s 没有用统一上限" % name
        if re.search(r"timeout=\d+", text):
            missing.append(name)
    assert not missing, "这些门还在写死超时数字：%s" % missing


# ==================== 2. 超时与取消必须区分 ====================

def test_超时返回的abort带timeout原因():
    reg = InteractionRegistry()
    reg.register("k")
    d = reg.wait("k", timeout=0.05)          # 没人回答 → 立刻超时
    assert d.get("action") == "abort"
    assert d.get("reason") == "timeout", d


def test_取消返回的abort带cancelled原因():
    import threading

    reg = InteractionRegistry()
    reg.register("k")
    ev = threading.Event()
    ev.set()                                  # 模拟 /cancel
    d = reg.wait("k", cancel_evt=ev, timeout=5)
    assert d.get("action") == "abort"
    assert d.get("reason") == "cancelled", d


def test_默认超时就是常量_不是写死的小值():
    sig = inspect.signature(InteractionRegistry.wait)
    assert sig.parameters["timeout"].default is None, \
        "默认值要走 GATE_TIMEOUT_SECONDS，不能是写死的小数字"


def test_没人回答不会立刻返回abort():
    """回归：wait 必须在 deadline 之前一直等，否则用户根本没机会作答。"""
    import time

    reg = InteractionRegistry()
    reg.register("k")
    t = time.time()
    reg.wait("k", timeout=0.4)
    assert time.time() - t >= 0.3, "wait 提前返回了 abort"


# ==================== 3. 用户那一行没送达时，终端必须说出来 ====================

def test_门已作废时终端不再静默(monkeypatch):
    """后端回 {"ok": false}（门超时作废）时，终端原来**一句话都不说** ——
    用户看到的就是"我输入 1，什么都没发生，然后流程取消"。

    这里拦 `tui.out` 而不是 capsys：VT 模式下它走的是自己的输出通道（带 ANSI），
    不一定落到 stdout 上；而"有没有告诉用户"这件事发生在**调用点**，拦调用点更准。
    """
    import confirmer
    import tui

    seen = []
    monkeypatch.setattr(tui, "out",
                        lambda text, gap=True, rule_before=False: seen.append(str(text)))
    confirmer._report_resume({"ok": False})
    joined = "\n".join(seen)
    assert "没有送达" in joined or "超时" in joined, seen


def test_审计门那条路也要说话(monkeypatch):
    """第 36 轮：`_ask_audit` 走 `/params`，当初只给 `/resume` 补了提示 ——
    于是"我输入 Y 却被丢掉"这条路线上**至今没有任何反馈**。"""
    import confirmer
    import tui

    seen = []
    monkeypatch.setattr(tui, "out",
                        lambda text, gap=True, rule_before=False: seen.append(str(text)))
    confirmer._report_params({"ok": False})
    assert "没有送达" in "\n".join(seen), seen


def test_审计门每一个上报点都接了送达检查():
    """`/params` 有 6 个上报点（参数门 3 个 + 审计门 3 个），一个都不能漏。"""
    import re

    src = (ROOT / "terminal" / "confirmer.py").read_text(encoding="utf-8")
    total = len(re.findall(r"client\.post_params\(", src))
    checked = len(re.findall(r"_report_params\(client\.post_params\(", src))
    assert total == checked == 6, "post_params %d 处，接了检查的 %d 处" % (total, checked)


def test_送达成功时不吓唬用户(monkeypatch):
    import confirmer
    import tui

    seen = []
    monkeypatch.setattr(tui, "out",
                        lambda text, gap=True, rule_before=False: seen.append(str(text)))
    confirmer._report_resume({"ok": True})
    assert "没有送达" not in "\n".join(seen), seen


# ==================== 4. 收尾说明要能读懂 ====================

def test_引擎在取消时会说明原因():
    """done(cancelled) 必须带 note，不能只有一句"流程已取消"。"""
    text = (BACKEND / "pipeline" / "engine.py").read_text(encoding="utf-8")
    assert '"status": "cancelled"' in text
    assert text.count('"status": "cancelled"') >= 2, "两条取消路径都要带说明"
    assert "note" in text.split('"status": "cancelled"')[1][:400], \
        "取消事件必须带 note 说明原因"

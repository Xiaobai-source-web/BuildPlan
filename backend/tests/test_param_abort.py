# -*- coding: utf-8 -*-
"""参数复核门的「/abort 中止」回归 —— 提示说会中止，行为就必须真的中止。

实测缺陷（本轮要修的）：
  终端门上写着「③ 输入 /abort → 中止本次运行」/「要中止本次运行，输入 /abort。」，
  但 `param_review` 收到这条输入时把它当成**用户手输的文本**（manual_input）上行 ——
  参数门把它当"补充参数"。也就是**提示说会中止，实际没中止**。

修法与判据（`pipeline.nodes.param_review`）：
  · 注册表给的 `action == "abort"`（门超时 / 运行被取消）→ 中止；
  · 用户整条输入就是中止命令（`ABORT_HINTS`：/abort、/cancel、退出、中止；
    大小写不敏感、两边空白随意）→ 中止；
  · **只有整条输入就是斜杠命令**才算中止：`/abort 顺带一句` 是"顺便补一句话"，
    必须走原来的文本路径（老实现按子串命中直接掐掉整条运行，用户的补充被静默丢弃）。

离线可跑：注入假 registry（register 记账、wait 直接返回预置决策），
不起服务、不联网、不碰真实 LLM / 注册表 / 文件系统。

运行：python -m pytest backend/tests/test_param_abort.py -q
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes.boundary import params_completeness  # noqa: E402
from pipeline.nodes.param_review import (  # noqa: E402
    ABORT_HINTS,
    ABORT_STOP,
    ABORT_SUMMARY,
    ParamReviewNode,
    is_abort_decision,
)

# 齐备参数（走文本路径时能一次放行，方便区分"中止"与"参数没齐"两种 _stop）
# ⚠️ 域 2.1 起 `foundation_type`（基础类型）是 REQUIRED_KEYS + ABSOLUTE_KEYS 成员；
#    第 2 批收口（用户裁决「结构各类型和基础类型都是，如果没有输入，那就报错」）后
#    `structure_type`（结构形式）同期进入同两档。缺任一个都会命中参数门的必中断出口，
#    所以"齐备参数"必须包含它们俩。
#    这里补的是 fixture 的完备性，不是放宽断言 —— 本文件测的是中止/斜杠命令/试算
#    三种出口，与"基础类型/结构形式"无关，不该被它们误伤。
_FULL = {"building_count": 12, "floors": 38, "total_area": 215000,
         "total_concrete": 82000, "planned_start_date": "2025-04-16",
         "foundation_type": "筏板基础", "structure_type": "框架-剪力墙结构"}


class _StubReg(object):
    """最小交互登记桩：register 记账，wait 直接返回预置决策（不阻塞）。

    决策用尽后一律返回 `{"action": "abort"}`（等价真实注册表的兜底），
    免得门在测试里空转（MAX_ROUNDS=20 轮）。
    """

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.registered = []
        self.waits = 0

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=600):
        idx = min(self.waits, len(self.decisions) - 1)
        self.waits += 1
        if not self.decisions:
            return {"action": "abort"}
        dec = self.decisions[idx]
        return dict(dec) if isinstance(dec, dict) else {"action": "abort"}


def _run(decisions, params=None):
    """跑一次参数门，返回 (节点, 事件载荷列表, ctx, 返回值)。"""
    node = ParamReviewNode()
    events = []
    node._emit = lambda event, data: events.append((event, data))
    node._run_id = "t_abort"
    node._registry = _StubReg(decisions if isinstance(decisions, list) else [decisions])
    ctx = {"extracted_params": dict(_FULL if params is None else params)}
    out = node.run(ctx)
    return node, [d for e, d in events if e == "param_review"], ctx, out


# ==================== 1. 手输 /abort → 真的中止 ====================
def test_用户输入_abort_节点返回_stop():
    """参数齐备时也要中止：提示说了 /abort 会中止，就不能被当成"补充参数"放行。"""
    node, events, ctx, out = _run({"passed": False, "manual_input": "/abort"})

    assert isinstance(out, dict)
    assert "_stop" in out, out
    assert "中止" in out["_stop"], out["_stop"]
    assert "中止" in node.done_summary, node.done_summary
    # 中止发生在**第一轮**，不是问满 20 轮才停
    assert len(events) == 1, events
    assert len(node._registry.registered) == 1
    # 中止路径不许留下放行痕迹
    assert "trial_mode" not in ctx or ctx.get("trial_mode") is None, ctx
    assert "params_completeness" not in ctx, ctx


def test_门上的提示确实提到了_abort():
    """提示与行为对齐的证据：门载荷里写了 /abort，且这条输入真能中止。

    注意 /abort 这句只出现在**缺必要参数**那一版提示里（参数齐备版不提中止文案），
    所以这里故意用空参数跑 —— 用户看到 /abort 的场景正是他卡在参数门上。
    """
    node, events, _ctx, out = _run({"passed": False, "manual_input": "/abort"}, {})

    assert "/abort" in events[0]["message"], events[0]["message"]
    assert "_stop" in out


# ==================== 2. /cancel、带空白、大小写 ====================
@pytest.mark.parametrize("text", [
    "/cancel", "/CANCEL", "/Cancel",
    "  /abort  ", "\t/abort\n", "/ABORT",
    "中止", "退出", "  中止  ",
])
def test_其它中止写法同样生效(text):
    node, _events, _ctx, out = _run({"passed": False, "manual_input": text})

    assert "_stop" in out, (text, out)
    assert "中止" in out["_stop"], (text, out["_stop"])
    assert "中止" in node.done_summary, (text, node.done_summary)


# ==================== 3. 斜杠命令带尾巴 → 不当中止 ====================
def test_abort_带尾巴不当中止_仍走文本路径():
    """`/abort 顺便说一句`：用户是在补充内容，不是在喊停 —— 不许误伤掐掉整条运行。"""
    node, _events, ctx, out = _run({"passed": False, "manual_input": "/abort 顺带一句"})

    assert "_stop" not in out, out
    assert "中止" not in (node.done_summary or ""), node.done_summary
    # 走的是原文本路径：这行字作为用户补充参数收下并上行
    assert out.get("_manual_param_input") and "顺带一句" in out["_manual_param_input"], out
    assert ctx["_manual_param_input"] and "顺带一句" in ctx["_manual_param_input"], ctx


@pytest.mark.parametrize("text", ["/cancel 我还有话说", "/abort顺便说一句", "别急 /abort"])
def test_带尾巴的斜杠命令一律不中止(text):
    """只有**整条输入就是**斜杠命令才算中止（"别急 /abort" 也不是）。"""
    node, _events, _ctx, out = _run({"passed": False, "manual_input": text})

    assert "_stop" not in out, (text, out)
    assert "中止" not in (node.done_summary or ""), (text, node.done_summary)


def test_带尾巴且参数不齐_继续追问而不是中止():
    """参数不齐时也**不能**因为输入以 /abort 开头就直接取消 —— 要继续问参数。

    （追问满 MAX_ROUNDS 后的兜底 _stop 文案里本来就带"中止"二字，那是"缺参数不放行"
    的安全网，不是本门的 /abort 出口 —— 所以判据要看 ABORT_STOP / ABORT_SUMMARY。）
    """
    node, events, _ctx, out = _run({"passed": False, "manual_input": "/abort 顺带一句"}, {})
    stop = (out or {}).get("_stop") or ""

    assert ABORT_STOP not in stop, stop
    assert node.done_summary != ABORT_SUMMARY, node.done_summary
    assert len(events) > 1, "参数没齐就该继续追问（实际门次数 %d）" % len(events)
    assert "必要参数" in stop, stop


# ==================== 4. 注册表给的 abort（超时 / 取消）====================
def test_action_abort_也算中止():
    node, events, _ctx, out = _run({"action": "abort"})

    assert "_stop" in out, out
    assert "中止" in out["_stop"], out["_stop"]
    assert "中止" in node.done_summary, node.done_summary
    assert len(events) == 1, events


def test_两条中止入口文案一致():
    """注册表超时/取消 与 用户手输 /abort 必须给同一个出口（否则提示与行为又会对不上）。"""
    _n1, _e1, _c1, out_registry = _run({"action": "abort"})
    _n2, _e2, _c2, out_typed = _run({"passed": False, "manual_input": "/abort"})

    assert out_registry["_stop"] == out_typed["_stop"] == ABORT_STOP


# ==================== 5. 判据本身的单元表 ====================
@pytest.mark.parametrize("decision, want", [
    ({"action": "abort"}, True),
    ({"manual_input": "/abort"}, True),
    ({"manual_input": "  /Abort  "}, True),
    ({"manual_input": "/cancel"}, True),
    ({"manual_input": "中止"}, True),
    ({"manual_input": "/abort 顺带一句"}, False),
    ({"manual_input": "/cancelled"}, False),
    ({"manual_input": ""}, False),
    ({"manual_input": None}, False),
    ({}, False),
    (None, False),
])
def test_is_abort_decision_判据(decision, want):
    assert is_abort_decision(decision) is want, decision


def test_中止提示词表非空且含斜杠命令():
    assert ABORT_HINTS, "至少要有一个可识别的中止说法"
    assert any(h.startswith("/") for h in ABORT_HINTS)


# ==================== 6. 非中止路径没有被改坏 ====================
def test_正常文本补充仍然放行():
    node, _events, ctx, out = _run({"passed": False, "manual_input": "栋数 12，地上 38 层，总建筑面积 215000 ㎡"})

    assert "_stop" not in out, out
    assert ctx["params_completeness"]["ok"] is True, ctx["params_completeness"]
    assert out["review_passed"] is False and out["_manual_param_input"], out
    assert "中止" not in (node.done_summary or ""), node.done_summary


def test_试算仍然生效():
    _node, _events, ctx, out = _run({"passed": False, "manual_input": "试算"})

    assert "_stop" not in out, out
    assert ctx["trial_mode"] is True
    assert params_completeness({})["ok"] is False, "空参数本就该判为不齐"


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-q"]))

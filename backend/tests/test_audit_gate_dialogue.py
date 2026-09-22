# -*- coding: utf-8 -*-
"""审计门对话测试 —— 「提问」不能被当成审计意见，退回必须把代价说出来

用户实测（原话）：
  ① 他在【R1】门里输入了一句**疑问**「我选择的不是五层为一组吗」，系统把它当成审计意见：
     计划被标「未审计」，而定额锚定/配员/排程照常算完 —— "明明返回了意见，却还在做定额"。
  ② 紧接着【R2】被静默跳过、一路跑到终稿确认门 —— "不管我输入什么都直接跳到最后一个门"。

所以本文件守住四件事：
  1. 疑问句 → 解释 + 重新问一次：**不记账、不计轮次、不设 audit_rejected**，
     也不再因此跳过 R2；
  2. 真·修改意见 → 先给 [1]/[2]/[3] 菜单（"会继续算 + R2 会被跳过"必须写在**选之前**）：
     [1] 停（消息带原话与重跑指引）/ [2] 与老版本**逐项一致** / [3] 重新问；
  3. R2 因前序退回而跳过时，默认折叠模式下也要**看得见**（走 node_done 的警告通道）；
  4. Y 通过 / abort 取消 / 第 3 轮退回立刻停 / 认不出的输入 → 老行为一字不变。

运行：python -m pytest backend/tests/test_audit_gate_dialogue.py -q
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import pytest                                                          # noqa: E402

import renderer                                                        # noqa: E402
from pipeline import quantity                                          # noqa: E402
from pipeline.engine import Pipeline                                   # noqa: E402
from pipeline.nodes.audit_gate import (                                # noqa: E402
    MENU_MAX_ROUNDS, QUESTION_MAX_ASKS,
    DraftAuditNode, ScheduleAuditNode, WBSAuditNode,
    _choice_of, looks_like_question,
)

_ANSI = re.compile(r"\033\[[0-9;]*m")


def _plain(text):
    return _ANSI.sub("", str(text or ""))


# ==================== 夹具 ====================
class _QueueRegistry(object):
    """按队列回放决策的登记处：每次 wait 弹一个（弹完复用最后一个）。

    审计门现在会**多次提问**（解释 / 菜单），所以必须能回放一串决策 ——
    这正是 `_AuditGate.run` 里的对话状态机要测的东西。
    """

    def __init__(self, decisions):
        self.q = list(decisions)
        self.tail = self.q[-1] if self.q else {"passed": True}
        self.registered = []

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=None):
        return dict(self.q.pop(0) if self.q else self.tail)


def _ctx(**kw):
    ctx = {
        "wbs": {"phases": [
            {"phase": "地下室结构", "work_packages": [{"id": "4.1", "sub_packages": [
                {"id": "4.1.1.1", "name": "底板钢筋", "quantity": 100, "unit": "t"}]}]},
            {"phase": "地上主体结构", "work_packages": [{"id": "5.1", "sub_packages": [
                {"id": "5.1.1.1", "name": "主体钢筋", "quantity": 900, "unit": "t"}]}]},
        ]},
        "plan_level": "L4",
        # 用户实测里那句「我选择的不是五层为一组吗」的真实来源
        "display_granularity": {"depth": quantity.DEPTH_COMPONENT,
                                "floor_grouping": quantity.FLOOR_PER_5, "rows": 12},
        "plan_json": {"plan_id": "dialogue_t", "meta": {}},
    }
    ctx.update(kw)
    return ctx


def _run(node, decisions, ctx=None):
    ctx = _ctx() if ctx is None else ctx
    reg = _QueueRegistry(decisions)
    events = []
    node._registry = reg
    node._run_id = "t"
    node._cancel_evt = None
    node._emit = lambda e, d: events.append((e, d))
    result = node.run(ctx)
    return result, events, ctx


def _gates(events):
    return [d for e, d in events if e == "param_review"]


def _gate_text(gate):
    """门给用户看的文字：`next_hint` 是审计门**一定会打印**的那一行。

    为什么断言这里而不是只断言 summary：R1/R2 的正文是结构化实物内容
    （WBS 树 / 两版工期对比），终端渲染时 summary 会被正文顶掉；解释与菜单
    因此落在 next_hint（终端的 render_audit_gate 一定会打它）。
    """
    return str(gate.get("next_hint") or "")


# ======================================================================
# 1. 疑问句：解释 + 再问一次，绝不记账
# ======================================================================
@pytest.mark.parametrize("ask", [
    "我选择的不是五层为一组吗",       # 用户原话
    "为什么地上主体只有 3 条？",
    "什么意思",
    "怎么会这样呢",
    "哪些阶段没有工序？",
])
def test_疑问句不记账且重新问一次(ask):
    """疑问句 → 两帧（原问题 + 解释后重问），中间不落任何审计结论。"""
    # 第二帧给的仍是"没通过"，但随后 abort，方便检查"中间态"有没有被记账
    result, events, ctx = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": ask},
        {"action": "abort"},
    ])
    gates = _gates(events)
    assert len(gates) == 2, "疑问句必须解释完再问一次，实际 %d 帧" % len(gates)

    assert "提问" in _gate_text(gates[1]), _gate_text(gates[1])
    assert "原始 WBS" in _gate_text(gates[1])
    assert "三轮回审门" in _gate_text(gates[1])

    # 不记账：没有审计轮次、没有 audit_rejected、计划也没被标「未审计」
    assert "audit_rounds" not in ctx, ctx.get("audit_rounds")
    assert "audit_comments" not in ctx
    assert not ctx.get("audit_rejected"), "疑问句不许设 audit_rejected（否则 R2 会被跳过）"
    assert "audit_status" not in ctx["plan_json"]["meta"]
    # 第二帧之后是 abort（老行为），不是"退回"
    assert result.get("_stop") and "取消" in result["_stop"]


def test_疑问句之后打Y仍然正常通过():
    """解释完再问一次，用户打 Y 就走正常通过路径（只记一条 passed 轮次）。"""
    result, events, ctx = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": "我选择的不是五层为一组吗"},
        {"passed": True},
    ])
    assert result.get("_stop") is None
    assert len(_gates(events)) == 2
    rounds = ctx["audit_rounds"]
    assert len(rounds) == 1 and rounds[0]["passed"] is True, rounds
    assert not ctx.get("audit_comments")
    assert ctx["plan_json"]["meta"]["audit_status"] == "未审计"   # 只过了一轮，还不算审计完成


def test_疑问句不会让R2被跳过():
    """用户实测的第二个问题：一句疑问不该把 R2 变成"直接跳过"。"""
    events = []
    ctx = _ctx()
    reg = _QueueRegistry([
        {"passed": False, "manual_input": "我选择的不是五层为一组吗"},
        {"passed": True},                       # R1 重问后打 Y
        {"passed": True},                       # R2 正常提问并打 Y
    ])
    pipe = Pipeline(run_id="t", registry=reg)
    pipe.add_nodes(WBSAuditNode(), ScheduleAuditNode())
    pipe.run(ctx, emit=lambda e, d: events.append((e, d)))

    rounds = [d.get("round") for e, d in events if e == "param_review"]
    assert 2 in rounds, "疑问句之后 R2 必须照常提问，实际轮次 %s" % rounds
    assert not ctx.get("audit_rejected")
    assert not [d for e, d in events
                if e == "node_done" and "跳过" in str(d.get("summary") or "")]


def test_疑问句最多解释几次就转成菜单_不无限循环():
    """护栏：连着问也不许死循环 —— 超过 QUESTION_MAX_ASKS 就转成"意见"菜单。"""
    decisions = [{"passed": False, "manual_input": "为什么？"}] * 12
    result, events, ctx = _run(WBSAuditNode(), decisions)
    gates = _gates(events)
    assert len(gates) == QUESTION_MAX_ASKS + 2, \
        "解释 %d 次后会转成菜单（+1 帧），菜单里认不出输入就按 [2] 收尾，实际 %d" \
        % (QUESTION_MAX_ASKS, len(gates))
    assert "请选择" in _gate_text(gates[-1])
    assert ctx.get("audit_rejected") is True, "转成意见后按 [2] 保守处理"


# ======================================================================
# 2. 真·修改意见：[1]/[2]/[3] 菜单
# ======================================================================
OPINION = "主体结构要按 18 层展开"


def test_修改意见先给菜单且把代价写在选之前():
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": OPINION},
        {"action": "abort"},                                # 只看菜单那一帧的中间态
    ], ctx)
    gates = _gates(events)
    assert len(gates) == 2
    menu = _gate_text(gates[1])
    for must in ("[1]", "[2]", "[3]", OPINION,
                 "会继续算", "R2", "跳过",          # 副作用必须写在选之前
                 "WBS 复评门", "推荐"):
        assert must in menu, "菜单缺少关键信息 %r：\n%s" % (must, menu)
    # 与旧行为的差别：菜单出现的那一刻**还没有**记账（选之前不落任何审计结论）
    assert "audit_rounds" not in ctx and not ctx.get("audit_rejected")
    assert "audit_status" not in ctx["plan_json"]["meta"]
    assert result.get("_stop") and "取消" in result["_stop"]


def test_选1停止本次运行且消息带原话():
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "1"},
    ], ctx)
    assert result.get("_stop"), "选 [1] 必须停止本次运行"
    msg = result["_stop"]
    assert OPINION in msg, msg
    assert "WBS 复评门" in msg and "重新展开" in msg and "补齐" in msg
    assert "未审计" in msg and "定稿" in msg
    # 停止 ≠ 退回：不进审计链、不设 audit_rejected（后面的门不该"继续跑"）
    assert "audit_rounds" not in ctx and not ctx.get("audit_rejected")
    assert "audit_status" not in ctx["plan_json"]["meta"]
    assert not hasattr(WBSAuditNode, "audited")


def test_选2与老版本逐项一致():
    """选 [2] 的落账结果必须**逐项**等于旧版 `_mark(passed=False, comment)`。"""
    opinion = "主体班组太小，钢筋工要 30 人"
    ctx = _ctx()
    result, _events, _ = _run(ScheduleAuditNode(), [
        {"passed": False, "manual_input": opinion},
        {"passed": False, "manual_input": "2"},
    ], ctx)
    assert result == {"audit_rejected": True}, "老返回值不许变：%r" % (result,)

    legacy = _ctx()
    ScheduleAuditNode()._mark(legacy, passed=False, comment=opinion)
    for key in ("audit_rounds", "audited", "audit_rejected", "audit_comments"):
        assert ctx.get(key) == legacy.get(key), key
    assert ctx["plan_json"]["meta"]["audit_status"] == "未审计"
    assert ctx["plan_json"]["meta"]["audit_rounds"] == legacy["plan_json"]["meta"]["audit_rounds"]


def test_选2的消息里明说会继续算且后续门会被跳过():
    node = WBSAuditNode()
    ctx = _ctx()
    result, _events, _ = _run(node, [
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "2"},
    ], ctx)
    summary = node.done_summary
    assert "还会继续算" in summary and "不出定稿与看板" in summary, summary
    assert "【R2】" in summary and "跳过" in summary, summary
    # R2 上选 [2] 时说的是 R3
    node2 = ScheduleAuditNode()
    _run(node2, [{"passed": False, "manual_input": "班组要加大"},
                 {"passed": False, "manual_input": "2"}])
    assert "【R3】" in node2.done_summary and "跳过" in node2.done_summary, node2.done_summary


def test_选3重新问且不记账():
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "3"},
        {"action": "abort"},                                # 只看中间态
    ], ctx)
    gates = _gates(events)
    assert len(gates) == 3, "菜单 → [3] 返回重填 → 重新问，实际 %d 帧" % len(gates)
    assert "请选择" in _gate_text(gates[1])
    assert "请选择" not in _gate_text(gates[2]), "返回重填后应回到普通审计提问"
    assert "audit_rounds" not in ctx and not ctx.get("audit_rejected")
    assert ctx["plan_json"]["meta"].get("audit_status") is None
    assert result.get("_stop") and "取消" in result["_stop"]


def test_选2之后接着选3也能回退():
    """[3] 的语义不能做反：它是"返回重填"，不是"继续跑"。"""
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "3"},
        {"passed": True},
    ], ctx)
    assert result.get("_stop") is None and not ctx.get("audit_rejected")
    assert len([1 for d in _gates(events) if "请选择" in _gate_text(d)]) == 1


def test_菜单次数上限后按2保守收尾():
    """连着选 [3] 不许无限循环：到 MENU_MAX_ROUNDS 后按 [2]（与老行为一致）。"""
    decisions = [{"passed": False, "manual_input": OPINION}]
    for _ in range(MENU_MAX_ROUNDS + 2):
        decisions += [{"passed": False, "manual_input": "3"},
                      {"passed": False, "manual_input": OPINION}]
    result, events, ctx = _run(WBSAuditNode(), decisions)
    assert ctx.get("audit_rejected") is True
    assert result == {"audit_rejected": True}
    menus = [d for d in _gates(events) if "请选择" in _gate_text(d)]
    assert len(menus) == MENU_MAX_ROUNDS, "菜单最多 %d 次，实际 %d" % (MENU_MAX_ROUNDS, len(menus))


def test_认不出的输入按2处理():
    """编号越界 / 又写一句新意见 → 保守按 [2]（老行为），绝不静默丢弃。"""
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "9"},         # 越界编号
    ], ctx)
    assert result == {"audit_rejected": True}
    assert ctx["audit_rejected"] is True
    # 记账记的是那条**真正的意见**，不是敲错的编号
    assert ctx["audit_comments"][0]["comment"] == OPINION
    assert len(_gates(events)) == 2

    # 菜单里又写了一句新意见 → 记新的那句（用户没选编号，但话说得很清楚）
    ctx2 = _ctx()
    _run(WBSAuditNode(), [
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "把装饰装修阶段拆细一点"},
    ], ctx2)
    assert ctx2["audit_rejected"] is True
    assert ctx2["audit_comments"][0]["comment"] == "把装饰装修阶段拆细一点"


def test_空输入保持老行为_不进菜单():
    """EOF / Ctrl+C 兜底路径会 post 一个 passed=False 且没有文字 —— 老行为：记账继续。"""
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [{"passed": False}], ctx)
    assert result == {"audit_rejected": True}
    assert ctx["audit_rejected"] is True and ctx["audit_rounds"][0]["passed"] is False
    assert len(_gates(events)) == 1, "空输入不该再弹菜单"


# ======================================================================
# 3. 提问判定：阈值与保守性
# ======================================================================
def test_提问判定阈值():
    # 结尾信号：？/?/吗/呢
    for t in ("我选择的不是五层为一组吗", "为什么这样？", "为什么这样?", "那 WBS 结构呢"):
        assert looks_like_question(t) is True, t
    # 明确疑问词
    for t in ("什么意思", "为什么不能改成 18 层", "是不是漏了工序", "怎么回事"):
        assert looks_like_question(t) is True, t
    # 真·修改意见：一律不当提问
    for t in ("主体结构要按 18 层展开", "地下室阶段缺了支护", "主体班组太小，钢筋工要 30 人",
              "工期有点长", "混凝土量不对"):
        assert looks_like_question(t) is False, t
    # 含糊（弱疑问词 + "要我改"的指令）→ 按意见处理（保守）
    assert looks_like_question("怎么把工期缩短") is False
    assert looks_like_question("怎么把混凝土量补上") is False
    # 空输入/畸形不许抛
    assert looks_like_question("") is False
    assert looks_like_question(None) is False
    assert looks_like_question(123) is False


def test_含糊输入在门上按意见走菜单():
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": "怎么把工期缩短"},      # 含糊 → 意见
        {"passed": False, "manual_input": "2"},
    ], ctx)
    gates = _gates(events)
    assert "请选择" in _gate_text(gates[1]), _gate_text(gates[1])
    assert "提问" not in _gate_text(gates[1])
    assert ctx["audit_rejected"] is True


def test_菜单编号解析():
    for t in ("1", "[1]", "（1）", "1.", " 2 ", "3、"):
        assert _choice_of(t) == int(re.sub(r"\D", "", t)), t
    for t in ("", "0", "4", "9", "abc", "主体结构要按 18 层展开", None):
        assert _choice_of(t) is None, t


# ======================================================================
# 4. 展示粒度：有就念出正确标签，没有就整节不提
# ======================================================================
def test_有粒度时解释里念出正确标签():
    ctx = _ctx(display_granularity={"depth": quantity.DEPTH_COMPONENT,
                                    "floor_grouping": quantity.FLOOR_PER_5,
                                    "rows": 12})
    _result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": "我选择的不是五层为一组吗"},
        {"action": "abort"},
    ], ctx)
    out = _gate_text(_gates(events)[1])
    assert quantity.FLOOR_LABELS[quantity.FLOOR_PER_5] in out, out   # 「每 5 层一组」
    assert quantity.DEPTH_LABELS[quantity.DEPTH_COMPONENT] in out, out
    assert "只影响交付物" in out and "不改 WBS 结构" in out


@pytest.mark.parametrize("granularity", [
    None, {}, {"depth": "???", "floor_grouping": "???"}, {"rows": 12}, "烂数据",
])
def test_没有粒度时不提这一节(granularity):
    ctx = _ctx(display_granularity=granularity)
    _result, events, _ = _run(WBSAuditNode(), [
        {"passed": False, "manual_input": "什么意思"},
        {"action": "abort"},
    ], ctx)
    out = _gate_text(_gates(events)[1])
    assert "展示粒度" not in out, "拿不到粒度就不许提这一节：\n%s" % out
    assert "每 5 层一组" not in out and "工序级" not in out


# ======================================================================
# 5. R2 跳过必须看得见（默认折叠模式）
# ======================================================================
def test_R2跳过的提示在默认折叠模式下也打印():
    events = []
    ctx = _ctx()
    reg = _QueueRegistry([
        {"passed": False, "manual_input": OPINION},
        {"passed": False, "manual_input": "2"},          # R1 选 [2] 继续跑
    ])
    pipe = Pipeline(run_id="t", registry=reg)
    pipe.add_nodes(WBSAuditNode(), ScheduleAuditNode())
    pipe.run(ctx, emit=lambda e, d: events.append((e, d)))

    # 真的没有向用户提问 R2（跳过就是跳过，不是"问了但他没看见"）
    assert not [d for e, d in events if e == "param_review" and d.get("round") == 2]

    payload = [d for e, d in events
               if e == "node_done" and d.get("node") == "audit_schedule"][0]
    assert "跳过" in payload["summary"] and "R2" in payload["summary"], payload
    assert payload.get("warnings"), "必须走 node_done 的警告通道：%r" % payload
    notice = payload["warnings"][0]
    assert "R2" in notice and "跳过" in notice and "未审计" in notice, notice

    # 默认（非 /verbose）折叠策略下它是"完整打印"，不是被折进状态区
    assert renderer.event_action("node_done", payload, verbose=False) == "print"
    out = _plain(renderer.render_event("node_done", payload))
    assert "跳过" in out and "R2" in out, out


def test_前序退回后R3也不提问而是拦下():
    """老行为回归：前两轮退回 → R3 不该再打扰用户，直接拦下最终交付物。"""
    ctx = _ctx(audit_rejected=True,
               audit_comments=[{"round": 1, "name": "WBS 结构", "comment": "结构不对"}])
    node = DraftAuditNode()
    events = []
    node._registry = _QueueRegistry([{"passed": True}])
    node._run_id = "t"
    node._cancel_evt = None
    node._emit = lambda e, d: events.append((e, d))
    result = node.run(ctx)
    assert result.get("_stop") and "前序审计未通过" in result["_stop"]
    assert "结构不对" in result["_stop"]
    assert not _gates(events)


# ======================================================================
# 6. 老行为回归：Y / abort / 第 3 轮立刻停
# ======================================================================
def test_Y通过的老行为不变():
    ctx = _ctx()
    result, events, _ = _run(WBSAuditNode(), [{"passed": True}], ctx)
    assert result.get("_stop") is None
    assert len(_gates(events)) == 1, "打 Y 只应有一帧，不该弹菜单"
    assert "请选择" not in _gate_text(_gates(events)[0])
    assert ctx["audit_rounds"][0]["passed"] is True
    assert ctx["audited"] is False


def test_abort取消的老行为不变():
    result, events, ctx = _run(WBSAuditNode(), [{"action": "abort"}])
    assert result.get("_stop") == "用户在第 1 轮审计（WBS 结构）取消了本次运行"
    assert "audit_rounds" not in ctx
    assert len(_gates(events)) == 1


def test_第三轮退回仍然立刻停():
    """R3 后面就是定稿与看板：老行为是立刻 _stop（不给"继续跑完"的选项）。"""
    ctx = _ctx()
    result, events, _ = _run(DraftAuditNode(), [
        {"passed": False, "manual_input": "总工期还是太长"}], ctx)
    assert result.get("_stop")
    assert "停止产出定稿与看板" in result["_stop"]
    assert "总工期还是太长" in result["_stop"]
    assert len(_gates(events)) == 1, "第 3 轮不给菜单"
    assert ctx["audit_rejected"] is True


def test_第三轮的疑问句也只是解释而不是停():
    """提问分流在三轮都生效：R3 里问一句不该把整次运行掐掉。"""
    ctx = _ctx()
    result, events, _ = _run(DraftAuditNode(), [
        {"passed": False, "manual_input": "为什么还没有看板？"},
        {"passed": True},
    ], ctx)
    assert result.get("_stop") is None
    assert "提问" in _gate_text(_gates(events)[1])
    assert not ctx.get("audit_rejected")


def test_门的载荷字段没丢():
    """§D 冻结契约：解释/菜单只加在 summary 与 next_hint 上，老字段一个不少。"""
    _result, events, _ = _run(WBSAuditNode(), [{"passed": True}])
    payload = _gates(events)[0]
    for key in ("review_id", "purpose", "round", "round_name", "title",
                "summary", "highlights", "next_hint"):
        assert payload.get(key) is not None, key
    assert payload["purpose"] == "audit" and payload["round"] == 1
    assert payload["wbs_tree"], "R1 的实物内容（WBS 树）不许因为对话分流而丢"

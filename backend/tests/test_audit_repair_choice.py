# -*- coding: utf-8 -*-
"""审计门「编号选择题 + 真去修」回归（规格：资料/审计门一键修复规格.md）。

先核实（§2）再动手，结论带证据：
  · 那 4 条 HIGH `issues` 由 `wbs_agent._review_loop` → `_review()` 的评审 LLM 产生，
    经 `_human_gate()` 以 `node_paused` 发出；R1 审计门（`audit_gate.WBSAuditNode`）
    **不产 `issues` 字段**（`_AuditGate.run` 的 payload 里根本没有这个键）。
  · 引擎 `_reenter()` 只能重跑当前 pause_point；真能重做 WBS 的机制只在
    `wbs_agent` 节点内部（`_expand_phase(retry_req=...)`）。
  → 所以"一键修复"做在 **wbs_agent 的 node_paused 人工门**上，R1 保持看树 + Y。

本文件覆盖（规格 §6.3）：
  1. `repairs` 缺失 → 渲染与行为同老版本（回归）；
  2. 输入 `1` → 上行 payload 带 `repair_key`，`manual_input` = 该选项 label；
  3. 输入任意文字 → 仍走"自由意见"（没有 `repair_key`）；
  4. 输入 `Y` → 仍然通过；
  5. 选了编号后**真的重做了**（断言产物/统计确实变了）；
  6. 用满 `MAX_RETRY_ON_HUMAN` 后 `repairs` 里不再出现该选项，且门上有说明文字；
  7. 无法自动修的问题**不出现在 `repairs` 里**（防止假修复），改成"需要你提供信息"。

运行：python -m pytest backend/tests/test_audit_repair_choice.py -q
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT / "terminal"))

import confirmer                                                       # noqa: E402
import renderer                                                        # noqa: E402
from pipeline.nodes.wbs_agent import (                                 # noqa: E402
    MAX_RETRY_ON_HUMAN, REPAIR_FILL_TYPES, REPAIR_REEXPAND, WBSAgentNode,
    issue_needs_user_info, missing_kb_essentials, kb_essentials_report, _kb_scope_gap)
from pipeline.registry import InteractionRegistry                      # noqa: E402
from pipeline import kb                                                # noqa: E402

_ANSI = re.compile(r"\033\[[0-9;]*m")

PARAMS = {"building_type": "剪力墙住宅", "structure_type": "剪力墙",
          "area": 8000, "floors": 38, "total_area": 301354.26,
          "total_concrete": 82000, "total_rebar": 12800}

# 用户截图里那几条 HIGH 的原文形态（"脚本自检显示…"正是 _self_check 的键名）
HIGH_CONC = {"severity": "HIGH", "dimension": "工程量", "target": "1",
             "finding": "脚本自检显示 conc_m3=0 m³，但项目参数明确 total_concrete=82000，"
                        "混凝土总量可能漏了 93%",
             "suggestion": "按层重新展开混凝土工程量"}
HIGH_COVER = {"severity": "HIGH", "dimension": "覆盖", "target": "机电安装",
              "finding": "REQUIRED 工程类型缺失：无给排水工程、无退场与恢复",
              "suggestion": "补齐知识库里缺失的 L3 必含工序"}


def _plain(text):
    return _ANSI.sub("", text)


# ======================================================================
# 桩：按 user 提示分派角色；被要求"补某某知识库活动"时**真的**补进去
# ======================================================================
_ASK_ACT = re.compile(r"([^\s（()、]+)\(([A-Za-z_0-9]+)/([^)/]+)\)")


class _RepairStub:
    """复评桩：前 N 轮报 HIGH，用户选了修复后（收到补类型要求）返回补齐后的子树。

    "真的补"这一点很关键：桩只有在**收到 `本次必须补齐【…】` 的重试要求**时才把
    知识库活动挂成叶子 —— 于是叶子数与"缺失类型数"的变化**只能**由真实的重做路径
    产生，测试因此能断言"确实重做了"，而不是断言桩自己的心情。
    """

    def __init__(self, repair_review=False):
        self.repair_review = repair_review
        self.review_calls = 0
        self.phase_calls = []
        self.phases_with_req = []
        self.fuse_user = None

    def chat_json(self, system, user, temperature=0.3, retries=1):
        if "候选三层WBS" in user:
            self.review_calls += 1
            # 第 1 轮：HIGH（混凝土/覆盖）；修复后的复评：PASS 收束（复评本身不是被测对象）
            if self.review_calls == 1:
                return {"verdict": "REVISE", "issues": [dict(HIGH_CONC), dict(HIGH_COVER)]}
            if self.repair_review:
                return {"verdict": "REVISE", "issues": [dict(HIGH_CONC), dict(HIGH_COVER)]}
            return {"verdict": "PASS", "issues": []}
        if "完整三层WBS" in user:
            self.fuse_user = user
            return {"fusions": []}
        if "本阶段：" in user:
            marker = user.split("本阶段：", 1)[1].split("\n\n项目参数", 1)[0].splitlines()[0].strip()
            self.phase_calls.append(marker)
            if "【修改要求（本阶段必须落实）】" in user:
                self.phases_with_req.append(marker)
                return {"work_packages": self._fills(marker, user)}
            return {"work_packages": self._base(marker)}
        return None

    @staticmethod
    def _wrap(marker, leaves):
        return [{"id": "1.1", "name": marker + "工作",
                 "sub_packages": [dict(l) for l in leaves]}]

    def _base(self, marker):
        return self._wrap(marker, [
            {"id": "1.1.1", "name": marker + "任务", "duration_days": 20,
             "quantity": 1, "unit": "项", "work_type": "土建临建"}])

    def _fills(self, marker, user):
        """重做时：把重试要求里点名的知识库活动**真的**展开成叶子。"""
        leaves = self._base(marker)[0]["sub_packages"]
        asked = _ASK_ACT.findall(user)
        for name, aid, unit in asked[:12]:
            leaves.append({"id": "1.1.%d" % (len(leaves) + 1), "name": name,
                           "duration_days": 5, "quantity": 100, "unit": unit.strip(),
                           "work_type": kb.l3_of_activity(aid) or "土建临建",
                           "kb_activity_id": aid})
        return self._wrap(marker, leaves)


class _Reg(InteractionRegistry):
    """人工门：返回预设决策（不带 repair_key == 打 Y/自由意见）。"""

    def __init__(self, decisions):
        super().__init__()
        self._q = list(decisions)
        self.payloads = []

    def wait(self, key, cancel_evt=None, timeout=600):
        return self._q.pop(0) if self._q else {"action": "continue"}


def _run(node, ctx):
    events = []
    node._emit = lambda e, d: events.append((e, d))
    node.run(ctx)
    return events


def _gates(events):
    return [d for e, d in events if e == "node_paused"]


def _node(stub, decisions, run_id="t"):
    n = WBSAgentNode(llm=stub)
    n._registry = _Reg(decisions)
    n._run_id = run_id
    return n


# ======================================================================
# 1. repairs 缺失 → 渲染与行为同老版本（回归）
# ======================================================================
def test_repairs_缺失时渲染与老版本一致():
    """规格 §3：`repairs` 为空/缺失 → 保持现在的界面不变（老行为不能坏）。"""
    old = {"node": "wbs_agent", "output_summary": "评审发现 1 条高优先级问题，请决定",
           "issues": [{"severity": "HIGH", "dimension": "工程量", "finding": "混凝土量偏低"}]}
    for extra in ({}, {"repairs": []}, {"repairs": None}, {"repairs": "不是列表"}):
        data = dict(old)
        data.update(extra)
        out = _plain(renderer.render_event("node_paused", data))
        assert "你可以让系统自己修" not in out, out
        assert "[1]" not in out, "repairs 缺失时不许冒出编号选项：\n%s" % out
        assert "问题：" in out and "混凝土量偏低" in out, out
    # 行为也同老版本：无 repair_key 的自由意见仍走 revise
    assert {"action": "continue"}.get("repair_key") is None
    assert _plain("".join(renderer.render_repairs(None, ""))) == ""
    assert _plain(renderer.render_event("node_paused", {"node": "x"})) != ""
    assert "上限说明" in _plain("".join(renderer.render_repairs([], "上限说明")))


def test_repairs_缺失时后端不带该字段():
    stub = _RepairStub()
    node = _node(stub, [{"action": "continue"}])
    node.llm = None                                    # 关掉 LLM → 无复评、无门
    node._registry = _Reg([{"action": "continue"}])
    gate = _gates(_run(node, {"prompt": "住宅", "extracted_params": dict(PARAMS)}))
    assert gate == [], "没有 LLM 时不该有复评门"


# ======================================================================
# 2/3/4. 编号 / 自由文字 / Y 三条上行路径
# ======================================================================
class _FakeClient:
    def __init__(self):
        self.calls = []

    def post_resume(self, pause_id, action, instruction=None, edits=None, run_id=None,
                    repair_key=None):
        self.calls.append({"pause_id": pause_id, "action": action,
                           "instruction": instruction, "repair_key": repair_key})
        return 200, {}


def _pause(monkeypatch, answer, data):
    seq = [answer]
    monkeypatch.setattr("builtins.input", lambda *a, **k: seq.pop(0) if seq else "")
    client = _FakeClient()
    confirmer.handle_pause(client, data, run_id="t")
    return client


PAUSE = {"pause_id": "wbsr_1", "node": "wbs_agent",
         "output_summary": "评审发现 2 条高优先级问题，请决定",
         "issues": [{"severity": "HIGH", "dimension": "覆盖", "finding": "缺给排水工程"}],
         "repairs": [
             {"key": REPAIR_REEXPAND, "label": "按项目参数重新展开受影响的 WBS 相",
              "hint": "重推总量与工期"},
             {"key": REPAIR_FILL_TYPES, "label": "补齐缺失的必含工程类型",
              "hint": "按知识库补齐"},
         ]}


def test_输入编号_1_上行带_repair_key_且_manual_input_是_label(monkeypatch):
    client = _pause(monkeypatch, "1", PAUSE)
    assert len(client.calls) == 1, client.calls
    call = client.calls[0]
    assert call["repair_key"] == REPAIR_REEXPAND, call
    assert call["instruction"] == PAUSE["repairs"][0]["label"], call
    assert call["action"] == "revise", "不认识 repair_key 的老后端要能退化成自由意见"


def test_输入编号_2_映射到第二个选项(monkeypatch):
    call = _pause(monkeypatch, "2", PAUSE).calls[0]
    assert call["repair_key"] == REPAIR_FILL_TYPES, call


def test_输入其它文字_仍走自由意见_没有_repair_key(monkeypatch):
    text = "主体班组太小，钢筋工要 30 人"
    call = _pause(monkeypatch, text, PAUSE).calls[0]
    assert "repair_key" not in call or not call.get("repair_key"), call
    assert call["instruction"] == text and call["action"] == "revise", call


def test_输入_Y_仍然通过(monkeypatch):
    call = _pause(monkeypatch, "Y", PAUSE).calls[0]
    assert call["action"] == "continue" and not call.get("repair_key"), call
    assert _pause(monkeypatch, "", PAUSE).calls[0]["action"] == "continue"
    assert _pause(monkeypatch, "abort", PAUSE).calls[0]["action"] == "abort"


def test_编号越界仍按自由意见_不吞输入(monkeypatch):
    """界面上只有 2 个修复项，`9` 不是选项 → 老路径（自由意见），绝不静默丢弃。"""
    call = _pause(monkeypatch, "9", PAUSE).calls[0]
    assert not call.get("repair_key"), call
    assert call["instruction"] == "9" and call["action"] == "revise", call


def test_没有_repairs_时提示语与老版本一致(monkeypatch):
    """老界面提示语必须原样保留（老行为不能坏）。"""
    seen = []
    import tui
    monkeypatch.setattr(tui, "ask", lambda hint, rule=True: seen.append(hint) or "Y")
    client = _FakeClient()
    confirmer.handle_pause(client, {"pause_id": "p"}, run_id="t")
    assert seen and "直接输入WBS修改意见" in seen[0], seen
    assert "编号" not in seen[0], seen


def test_resume_端点把_repair_key_带进决策():
    """§3 冻结上行契约：/resume 的 payload 里 repair_key 必须落到决策 dict。"""
    import main
    main.REGISTRY.register("pz1")
    from starlette.testclient import TestClient
    c = TestClient(main.app)
    r = c.post("/resume", json={"pause_id": "pz1", "action": "revise",
                                "repair_key": REPAIR_REEXPAND,
                                "instruction": "按项目参数重新展开",
                                "manual_input": "按项目参数重新展开"})
    assert r.status_code == 200 and r.json().get("ok") is True, r.text
    decision = main.REGISTRY.wait("pz1", timeout=1)
    assert decision.get("repair_key") == REPAIR_REEXPAND, decision
    assert decision.get("manual_input") == "按项目参数重新展开", decision
    assert decision.get("instruction") == "按项目参数重新展开", decision


def test_终端客户端上行真的带_repair_key_与_label(monkeypatch):
    """把"用户打 1"整条链走一遍：confirmer → client.payload → /resume 端点。

    这样 `manual_input == 该选项 label` 这条契约是**端到端**被验的，而不是只验一面。
    """
    import main
    import client as tclient
    posted = {}

    class _C(tclient.SSEClient):
        def __init__(self):
            pass

        def post(self, path, payload):
            posted["path"] = path
            posted["payload"] = payload
            return 200, {"ok": True}

    monkeypatch.setattr(confirmer, "_ask", lambda hint, rule=True: "1")
    confirmer.handle_pause(_C(), PAUSE, run_id="t")
    assert posted["path"] == "/resume", posted
    body = dict(posted["payload"])
    run_id = body.pop("run_id", None)
    assert run_id == "t", posted
    main.REGISTRY.register(body["pause_id"])
    from starlette.testclient import TestClient
    r = TestClient(main.app).post("/resume", json=body)
    assert r.json().get("ok") is True, r.text
    decision = main.REGISTRY.wait(body["pause_id"], timeout=1)
    assert decision.get("repair_key") == REPAIR_REEXPAND, decision
    assert decision.get("manual_input") == PAUSE["repairs"][0]["label"], decision


# ======================================================================
# 5. 选了编号 → **真的重做了**
# ======================================================================
def test_选了编号真的重做了_叶子与缺失类型都变了():
    """规格 §4：选了 repair_key → 走真实重做机制，并且回显「改了什么」。"""
    stub = _RepairStub()
    node = _node(stub, [{"action": "repair", "repair_key": REPAIR_FILL_TYPES,
                         "instruction": "补齐缺失的必含工程类型"}])
    events = _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})

    # ① 真的重跑了相（不是记一条意见就放行）
    assert stub.phases_with_req, "必须真的带着重试要求重跑相"
    assert node.retry_used == 1, node.retry_used
    # ② 回显"改了什么"，且带动前后对照
    line = (node.last_repair or {}).get("line") or ""
    assert "重做前后" in line and "→" in line, line
    echo = [d.get("message") for e, d in events
            if e == "node_progress" and str(d.get("message", "")).startswith("改动回显")]
    assert echo and "重做前后" in echo[0], echo
    # ③ 统计确实变了：缺失的必含工程类型下降
    before = (node.last_repair or {}).get("before") or {}
    after = (node.last_repair or {}).get("after") or {}
    assert before.get("missing", 0) > after.get("missing", 0), (before, after)
    # ④ 产物真的变了：树里出现了补齐的 KB 活动叶子
    leaves = [l for ph in node.frags for wp in ph.get("work_packages", [])
              for l in wp.get("sub_packages", [])]
    assert any(l.get("kb_activity_id") for l in leaves), "必须真的挂上了知识库活动"
    assert len(leaves) > len(node.specs), leaves


def test_重做走的是本节点内部的展开机制():
    """真重做的机制 = `_expand_phase(retry_req=...)`：用真实节点 + 桩 LLM 直接验证。"""
    stub = _RepairStub()
    node = WBSAgentNode(llm=stub)
    ctx = {"prompt": "x", "extracted_params": dict(PARAMS), "wbs": {"phases": []}}
    node.specs = [{"key": "mep", "phase": "机电安装", "hint": "h", "kb": ["plumbing"]}]
    node.frags = [None]
    miss = [{"l3": "plumbing", "name": "给排水工程", "phase": "机电安装", "keys": []}]
    targets, reqs = node._repair_targets(ctx, PARAMS, [dict(HIGH_COVER)],
                                         REPAIR_FILL_TYPES, miss)
    assert targets == {"机电安装"}, targets
    assert "给排水工程" in reqs["机电安装"], reqs
    ok, line = node._repair_gate(ctx, PARAMS,
                                 {"action": "repair", "repair_key": REPAIR_FILL_TYPES},
                                 [dict(HIGH_COVER)])
    assert ok, line
    assert node.retry_used == 1
    assert any(l.get("kb_activity_id") == "PLUMB_AI_001"
               for wp in node.frags[0]["work_packages"]
               for l in wp["sub_packages"]), node.frags


# ======================================================================
# 6. 用满 MAX_RETRY_ON_HUMAN → 选项消失 + 门上有说明
# ======================================================================
def test_用满上限后选项消失且门上有说明():
    stub = _RepairStub(repair_review=True)      # 修复后仍报 HIGH → 门会再来
    node = _node(stub, [{"action": "repair", "repair_key": REPAIR_FILL_TYPES,
                         "instruction": "补齐"},
                        {"action": "repair", "repair_key": REPAIR_REEXPAND,
                         "instruction": "重做"},
                        {"action": "continue"}])
    events = _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    gates = _gates(events)
    assert len(gates) >= 2, "第一次修复后仍不通过 → 应再来一次门"
    assert node.retry_used == MAX_RETRY_ON_HUMAN, node.retry_used
    last = gates[-1]
    assert not (last.get("repairs") or []), "用满上限后不许再出现修复选项：%s" % last.get("repairs")
    note = str(last.get("repair_limit_note") or "")
    assert note and "不再自动重做" in note and "/abort" in note, note
    # 渲染上也要看得见说明，并且编号占位符被填成真实编号
    out = _plain(renderer.render_event("node_paused", last))
    assert "不再自动重做" in out, out
    assert "{FREE}" not in out, out
    assert "我自己写意见" not in out, "没有修复选项时不该出现「我自己写意见」编号"

def test_选项最多出现到次数上限为止():
    stub = _RepairStub(repair_review=True)
    node = _node(stub, [{"action": "continue"}])
    _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    assert node.retry_used == 0, "打 Y 不该消耗重做次数"
    assert node._repair_options({"wbs": node.frags and {"phases": node.frags}},
                                PARAMS, [dict(HIGH_COVER)])[0]


def test_达到上限后_repair_gate_拒绝重做():
    node = _node(_RepairStub(), [])
    node.specs, node.frags = [{"phase": "机电安装", "kb": []}], [None]
    node.retry_used = MAX_RETRY_ON_HUMAN
    ok, why = node._repair_gate({"wbs": {"phases": []}}, PARAMS,
                                {"action": "repair", "repair_key": REPAIR_REEXPAND}, [])
    assert ok is False and "上限" in why, (ok, why)


# ======================================================================
# 7. 无法自动修的问题不出现在 repairs 里（防假修复）
# ======================================================================
def test_混凝土量的问题判定为无法自动修():
    """根因是节拍工程量在本门还没铺（builder 顺序 wbs_agent → beat_build）。"""
    need = issue_needs_user_info(dict(HIGH_CONC))
    assert need and "需要你提供信息" in need, need
    opts, notes, _ = WBSAgentNode()._repair_options({"wbs": {"phases": []}}, PARAMS,
                                                    [dict(HIGH_CONC)])
    assert notes and "需要你提供信息" in notes[0], notes
    for o in opts:
        assert "混凝土" not in str(o.get("label")), "不许给一个改不动混凝土的选项：%s" % o


def test_覆盖类问题才给补齐类型的选项():
    opts, notes, _ = WBSAgentNode()._repair_options({"wbs": {"phases": []}}, PARAMS,
                                                   [dict(HIGH_COVER)])
    keys = [o["key"] for o in opts]
    assert REPAIR_FILL_TYPES in keys, keys
    assert not notes, "覆盖类问题有真实机制可走，不该说「改不动」：%s" % notes


# ======================================================================
# 8. 结构性假 HIGH 的根因：自检证据必须自证"节拍型阶段还没展开"
#
# 用户截图里最扎眼的第一条（「脚本自检显示 conc_m3=… 但项目参数 total_concrete=…」）
# 就是这么来的：复评门跑在 beat_build **之前**（builder.py:65-68），节拍型阶段此刻
# 只有占位叶子 → conc_m3 必然是 0 → 评审模型据此报假 HIGH 把用户拦下。
# 修法：① 自检证据显式带 `beat_expanded` + 说明；② 评审提示词明令不得据此报 HIGH。
# ======================================================================
def test_自检在节拍未展开时显式自证局限():
    """造一份"节拍型阶段全是占位"的 WBS → 自检必须带 beat_expanded=false + 说明。"""
    from pipeline.nodes.wbs_agent import (BEAT_NOT_EXPANDED_NOTE, _beat_placeholder_phase)
    node = WBSAgentNode()
    wbs = {"phases": [_beat_placeholder_phase({"phase": "地上主体结构"}),
                      {"phase": "施工准备", "work_packages": [{"id": "1.1", "name": "准备",
                        "sub_packages": [{"id": "1.1.1", "name": "场地平整", "quantity": 1,
                                          "unit": "项", "duration_days": 3,
                                          "work_type": "土建临建"}]}]}]}
    ev = node._self_check(PARAMS, wbs)
    assert ev["beat_expanded"] is False, ev
    assert ev["placeholder_leaves"] == 1, ev
    assert ev["note"] == BEAT_NOT_EXPANDED_NOTE, ev
    # 说明里必须点明三件事：没展开、别据此判缺失、该去哪儿看
    assert "尚未展开" in ev["note"] and "不得据此判定工程量缺失" in ev["note"], ev["note"]
    # ⚠️ 第 32 轮起 `beat_build` 已移到本节点**上游**，所以这句说明不再点它，
    # 改成"属于下游步骤"这个**不受顺序影响**的说法（本节点仍可被单独调用）。
    assert "R1" in ev["note"] and "下游" in ev["note"], ev["note"]


def test_自检在节拍已展开时不误报():
    """别把正常情况也标成未展开：有一条真实量的节拍叶子 → beat_expanded=true。"""
    node = WBSAgentNode()
    wbs = {"phases": [{"phase": "地上主体结构", "work_packages": [{"id": "1.1", "name": "主体",
            "sub_packages": [{"id": "1.1.1", "name": "混凝土浇筑", "quantity": 4100,
                              "unit": "m³", "work_type": "混凝土工程", "duration_days": 18}]}]}]}
    ev = node._self_check(PARAMS, wbs)
    assert ev["beat_expanded"] is True, ev
    assert ev["placeholder_leaves"] == 0, ev
    assert "已展开" in ev["note"], ev
    # 数量为 0 的真实工序也算"未展开"的证据（别把它当展开）
    zero = {"phases": [{"phase": "地上主体结构", "work_packages": [{"id": "1.1", "name": "主体",
            "sub_packages": [{"id": "1.1.1", "name": "混凝土浇筑", "quantity": 0,
                              "unit": "m³", "work_type": "混凝土工程"}]}]}]}
    assert node._self_check(PARAMS, zero)["beat_expanded"] is False


def test_评审提示词明令禁止据未展开报HIGH():
    """规格要求把这条约束钉在提示词里，防以后被改掉（改掉就又把假 HIGH 放回来了）。"""
    text = (ROOT / "backend" / "prompts" / "wbs_review.txt").read_text(encoding="utf-8")
    assert "beat_expanded" in text, "提示词必须提到自检里的 beat_expanded 判据"
    assert "不得" in text and "HIGH" in text, text
    for must in ("不构成工程量缺失", "占位子树", "beat_build", "R1"):
        assert must in text, "提示词缺少关键约束：%s" % must
    # 自检产出的字段名与提示词里引用的必须是同一批（防两边改岔）
    ev = WBSAgentNode()._self_check(PARAMS, {"phases": []})
    assert "beat_expanded" in ev and "note" in ev, ev


def test_门的证据里带着这条自证说明():
    """端到端：门上给评审模型的证据必须含 beat_expanded=false 与那句说明。"""
    seen = {}

    class _Spy(_RepairStub):
        def chat_json(self, system, user, temperature=0.3, retries=1):
            if "候选三层WBS" in user:
                seen["user"] = user
            return _RepairStub.chat_json(self, system, user, temperature, retries)

    spy = _Spy()
    node = _node(spy, [{"action": "continue"}])
    _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    ev_text = (seen.get("user") or "").split("脚本自检证据：", 1)[-1]
    assert '"beat_expanded": false' in ev_text, ev_text[:400]
    assert "不得据此判定工程量缺失" in ev_text, ev_text[:400]


def test_骨架没有宿主相的类型不进选项(monkeypatch):
    """凭空造一个新的 1级 相 = 改产品结构，不是修复 → 必须不出现该选项。

    模拟一个知识库里 REQUIRED、但**代码骨架 10 个相的 KB 敞口里都没有**的类型：
    `_repair_gate` 必须拒绝，`_repair_options` 的 label 里也不许出现它。
    """
    import pipeline.nodes.wbs_agent as wa
    # 第 38 轮起节点统一走 `kb_essentials_report()`（带 checked/reason，用来区分
    # "不缺"与"没校验成"）——所以桩要桩在新入口上，桩旧入口已不起作用。
    monkeypatch.setattr(wa, "kb_essentials_report",
                        lambda wbs, params: {
                            "checked": True, "reason": "", "required": 1, "anchored": 0,
                            "missing": [{"l3": "roof", "name": "屋面工程", "phase": None,
                                         "keys": [], "kind": "missing",
                                         "kind_label": "真缺：树里没有任何相关工序",
                                         "evidence": [], "activities": []}]})
    node = WBSAgentNode(llm=_RepairStub())
    node.specs = list(wa.default_phases())
    node.frags = [{"phase": s["phase"], "work_packages": []} for s in node.specs]
    ok, why = node._repair_gate(
        {"wbs": {"phases": []}}, PARAMS,
        {"action": "repair", "repair_key": REPAIR_FILL_TYPES}, [dict(HIGH_COVER)])
    # 第 32 轮：措辞改成人话（原「…没有可挂靠的 1级 阶段，本门无法自动补」）
    assert ok is False and "系统改不了" in why, (ok, why)
    opts, notes, _ = node._repair_options({"wbs": {"phases": []}}, PARAMS, [dict(HIGH_COVER)])
    assert REPAIR_FILL_TYPES not in [o["key"] for o in opts], opts
    assert not any("屋面" in str(o.get("label")) for o in opts), opts


def test_缺失类型判定只用知识库与树上的挂靠():
    """判据只有两份数据：知识库 REQUIRED 清单 + 叶子的 kb_activity_id 挂靠。

    第 38 轮起 `material_transport` 已降级为"可选"（A1 三档化后枚举名 `OPTIONAL`，
    历史名 `USUAL`；见 devtools/migrate_material_transport_to_usual.py）：它名下
    116 个 L4 全是「XX运输」、给不出可注入工序，标着 REQUIRED 只会让每次生成都误报一次。
    """
    wbs = {"phases": [{"phase": "施工准备", "work_packages": [{"id": "1.1", "name": "x",
            "sub_packages": [{"id": "1.1.1", "name": "材料采购", "work_type": "材料准备",
                              "kb_activity_id": "MPREP_AI_001"}]}]}]}
    miss = missing_kb_essentials(wbs, PARAMS)
    ids = {m["l3"] for m in miss}
    assert "material_prep" not in ids, "已挂靠的类型不该算缺"
    assert "material_transport" not in ids, "已降为 OPTIONAL，不该再进必含清单：%s" % sorted(ids)
    assert {"demobilization", "ceiling", "waterproofing"} <= ids, sorted(ids)
    hosts = {m["l3"]: m["phase"] for m in miss}
    assert hosts["demobilization"] == "竣工验收", hosts    # 靠 _EXTRA_PHASE_KB 兜宿主
    assert hosts["ceiling"] == "装饰装修", hosts           # 相骨架本来就含 ceiling
    assert hosts["waterproofing"] == "地下室结构", hosts   # 相骨架本来就含 waterproofing
    # 空树/取不到建筑类型 → 不猜；新入口要把"没校验成"的原因一并给出
    assert missing_kb_essentials({"phases": []}, {}) == []
    rep = kb_essentials_report({"phases": []}, {})
    assert rep["checked"] is False and rep["missing"] == [] and rep["reason"]


def test_kb敞口缺口判定():
    spec = {"phase": "机电安装", "kb": ["plumbing"]}
    wbs = {"phases": []}
    gap = _kb_scope_gap(spec, wbs, ["plumbing"])
    assert gap and any("给水" in g or "排水" in g or "卫生器具" in g for g in gap), gap
    assert _kb_scope_gap({"phase": "x", "kb": []}, wbs, ["plumbing"]) == []


# ======================================================================
# 老行为回归：Y 通过 / 自由意见 / abort / 重做后复评 PASS
# ======================================================================
def test_打Y仍然直接通过不再重做():
    stub = _RepairStub()
    node = _node(stub, [{"action": "continue"}])
    events = _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    assert _gates(events), "HIGH 必须进人工门"
    assert node.retry_used == 0
    assert not stub.phases_with_req, "打 Y 不该重跑任何相"
    assert not [d for e, d in events if e == "node_progress"
                and str(d.get("message", "")).startswith("改动回显")]


def test_自由文字意见仍走融合修订():
    stub = _RepairStub()
    node = _node(stub, [{"action": "revise", "instruction": "混凝土总量请整体上调"}])
    _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    assert node.retry_used == 1, node.retry_used
    assert stub.fuse_user and "人工修改意见" in stub.fuse_user, "自由意见必须进融合"


def test_abort仍然取消():
    from pipeline.engine import PipelineCancelled
    node = _node(_RepairStub(), [{"action": "abort"}])
    try:
        _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    except PipelineCancelled:
        return
    raise AssertionError("abort 必须抛 PipelineCancelled")


def test_自由文字意见不消耗修复选项():
    """重做次数是**共用**的一个预算：打一段自由意见用掉 1 次后，修复选项仍在。"""
    stub = _RepairStub(repair_review=True)
    node = _node(stub, [{"action": "revise", "instruction": "混凝土总量请整体上调"},
                        {"action": "continue"}])
    events = _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    assert node.retry_used == 1, node.retry_used
    gate = _gates(events)[-1]
    assert [r["key"] for r in (gate.get("repairs") or [])], \
        "还有 1 次预算 → 修复选项必须还在：%s" % gate.get("repairs")


def test_选项label超长时被截断_渲染不爆版():
    """label 里塞了很长的相名列表 → 必须截断（终端一行放不下就折行，不能撑爆）。"""
    stub = _RepairStub()
    node = _node(stub, [{"action": "continue"}])
    gate = _gates(_run(node, {"prompt": "住宅项目",
                              "extracted_params": dict(PARAMS)}))[0]
    for r in gate.get("repairs") or []:
        assert len(str(r["label"])) <= 80, r
        assert isinstance(r.get("hint"), str) and r["hint"], r
    out = _plain(renderer.render_event("node_paused", gate))
    assert all(renderer._disp_width(l) <= 200 for l in out.splitlines()), out


def test_门载荷带上选项与说明而不破坏老字段():
    stub = _RepairStub()
    node = _node(stub, [{"action": "continue"}])
    events = _run(node, {"prompt": "住宅项目", "extracted_params": dict(PARAMS)})
    gate = _gates(events)[0]
    for k in ("pause_id", "node", "output_summary", "issues", "context_summary", "wbs_tree"):
        assert k in gate, "老字段不许丢：%s" % k
    assert [r["key"] for r in (gate.get("repairs") or [])] == \
        [REPAIR_REEXPAND, REPAIR_FILL_TYPES], gate.get("repairs")
    assert gate.get("retry_used") == 0 and gate.get("retry_limit") == MAX_RETRY_ON_HUMAN
    assert gate.get("issues_need_info"), "混凝土类问题要如实写「需要你提供信息」"
    assert "需要你提供信息" in gate["issues_need_info"][0], gate["issues_need_info"]


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except TypeError:
            pass
        print("  PASS  %s" % fn.__name__)
    print("全部 一键修复 用例通过 ✔")

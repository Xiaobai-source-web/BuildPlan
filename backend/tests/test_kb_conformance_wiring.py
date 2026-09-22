"""kb_conformance 的**接线**测试 — 校验结果真的到了该到的地方。

`test_kb_conformance.py` 只测纯函数本身；本文件测"接线有没有断"：
  ① `wbs_agent` 复评门：范围违规进 ctx、进评审证据、进门的 HIGH issues；
  ② 同一个问题不许在重做循环里反复拦人（`_scope_gated` 去重）；
  ③ `audit_wbs`（R1）门：**在门上现场重算**（因为 beat_build 在 wbs_agent 下游，
     节拍展开的违规只在这之后才存在于树里），摘要与结构化字段都要带上；
  ④ `plan_assembler.build_meta`：结果留档进 plan_json.meta（交付物可核对）；
  ⑤ 全链路都不许因为校验而抛异常（降级路径）。

运行：cd backend && python -m pytest tests/test_kb_conformance_wiring.py -q
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from pipeline.nodes.audit_gate import WBSAuditNode  # noqa: E402
from pipeline.nodes.kb_conformance import (check_scope_conformance,  # noqa: E402
                                           KIND_BANNED)
from pipeline.nodes.kb_scope import KBScopeNode  # noqa: E402
from pipeline.nodes.plan_assembler import build_meta  # noqa: E402
from pipeline.nodes.wbs_agent import WBSAgentNode  # noqa: E402

PARAMS = {"building_type": "住宅", "structure_type": "剪力墙结构",
          "area": 8000, "floors": 38}


# ==================== 夹具 ====================
def _scope(building="住宅", structure="剪力墙结构"):
    node = KBScopeNode()
    node._emit = lambda *a, **k: None
    return node.run({"extracted_params": {"building_type": building,
                                          "structure_type": structure}})["kb_scope"]


def _wbs(*leaves):
    """`(phase, wp, name, activity_id)` → 最小 WBS 树。"""
    phases = {}
    for phase, wp, name, aid in leaves:
        leaf = {"name": name, "quantity": 10, "unit": "m³", "duration_days": 2}
        if aid is not None:
            leaf["kb_activity_id"] = aid
        phases.setdefault(phase, {}).setdefault(wp, []).append(leaf)
    return {"phases": [
        {"phase": ph, "work_packages": [
            {"id": "", "name": wpn, "sub_packages": ls} for wpn, ls in wps.items()]}
        for ph, wps in phases.items()]}


class _Reg(object):
    """最小登记处替身（与 test_gate_d_payloads 同一套约定）。"""

    def __init__(self, decision=None):
        self.decision = decision if decision is not None else {"passed": True}
        self.registered = []

    def register(self, key):
        self.registered.append(key)

    def wait(self, key, cancel_evt=None, timeout=None):
        return dict(self.decision)


def _gate_events(ctx, decision=None, node=None):
    node = node or WBSAuditNode()
    events = []
    node._registry = _Reg(decision)
    node._run_id = "t"
    node._cancel_evt = None
    node._emit = lambda e, d: events.append((e, d))
    node.run(ctx)
    return events


def _first_payload(events):
    evs = [d for e, d in events if e in ("param_review", "node_paused")]
    assert evs, "门必须发出交互事件"
    return evs[0]


# ==================== ① wbs_agent：结果进 ctx / 证据 / 门 ====================
def test_scope_check_writes_ctx_and_evidence():
    """`_run_scope_check` 要把结构化结果与逐条原文都写进 ctx。"""
    node = WBSAgentNode()
    node._emit = lambda *a, **k: None
    ctx = {"wbs": _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN")),
           "kb_scope": _scope()}
    res = node._run_scope_check(ctx)

    assert res["violations"] == 1
    assert ctx["kb_scope_conformance"] is res
    assert ctx["scope_violations"], "逐条原文要单独放一个键，供引擎随 node_done 上行"
    assert "CONC_NEW_COLUMN" in ctx["scope_violations"][0]

    # 证据里必须有这份硬事实：评审模型靠它区分"漏项"与"被禁用的工序"
    ev = node._self_check(PARAMS, ctx["wbs"], scope_check=res)
    assert ev["kb_scope_conformance"]["violations"] == 1
    assert ev["kb_scope_conformance"]["issues"][0]["activity_id"] == "CONC_NEW_COLUMN"
    assert ev["kb_scope_conformance"]["checked"] is True


def test_self_check_omits_conformance_when_not_checked():
    """没拿到范围时证据里**不带**该键（缺失即优雅退化，不要给个空壳让人误读）。"""
    node = WBSAgentNode()
    node._emit = lambda *a, **k: None
    res = node._run_scope_check({"wbs": _wbs(("A", "包", "墙浇筑", "CONC_NEW_WALL")),
                                 "kb_scope": None})
    assert res["checked"] is False
    ev = node._self_check(PARAMS, {"phases": []}, scope_check=res)
    assert "kb_scope_conformance" not in ev


def test_scope_check_never_raises_on_broken_input():
    """烂数据（wbs 不是字典 / 树里有鸭子类型）不许把 WBS 生成搞挂。"""
    node = WBSAgentNode()
    node._emit = lambda *a, **k: None
    for bad_wbs in (None, "字符串", 123, {"phases": "不是列表"},
                    {"phases": [None, {"phase": "A", "work_packages": [None, "x"]}]}):
        res = node._run_scope_check({"wbs": bad_wbs, "kb_scope": _scope()})
        assert isinstance(res, dict) and "violations" in res


# ==================== ② 去重：不反复拦同一个问题 ====================
def test_scope_issues_deduplicated_across_rounds():
    """同一（阶段,编号）只拦一次；下一轮即使还在，也不当成新问题。"""
    node = WBSAgentNode()
    node._emit = lambda *a, **k: None
    node._run_scope_check({"wbs": _wbs(("二次结构与砌体", "砌体", "构造柱浇筑",
                                        "CONC_NEW_COLUMN")),
                           "kb_scope": _scope()})
    first = node._new_scope_issues(node._run_scope_check(
        {"wbs": _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN")),
         "kb_scope": _scope()}))
    assert len(first) == 1 and first[0]["severity"] == "HIGH"
    second = node._new_scope_issues(node._run_scope_check(
        {"wbs": _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN")),
         "kb_scope": _scope()}))
    assert second == [], "同一个问题不该在下一轮再次拦人"


def test_scope_issues_include_all_violations_then_dedupe():
    """不同阶段 / 不同编号各自成一条，后续轮次全部去重为空。"""
    node = WBSAgentNode()
    node._emit = lambda *a, **k: None
    ctx = {"wbs": _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN"),
                       ("主体", "结构", "钢梁", "STEEL0009")),
           "kb_scope": _scope()}
    first = node._new_scope_issues(node._run_scope_check(ctx))
    assert len(first) == 2
    assert node._new_scope_issues(node._run_scope_check(ctx)) == []


# ==================== ③ R1 审计门：现场重算 ====================
def test_r1_gate_recomputes_and_reports():
    """R1 门要在自己这一刻重算（beat_build 之后的树），并把结果带进正文与结构化字段。"""
    ctx = {"wbs": _wbs(("二次结构与砌体", "二次结构", "构造柱浇筑", "CONC_NEW_COLUMN"),
                       ("主体", "结构", "墙浇筑", "CONC_NEW_WALL")),
           "kb_scope": _scope(),
           "extracted_params": PARAMS}
    payload = _first_payload(_gate_events(ctx))

    assert "知识库范围一致性" in payload["summary"]
    assert "CONC_NEW_COLUMN" in payload["summary"]
    conf = payload["kb_scope_conformance"]
    assert conf["violations"] == 1
    assert conf["by_kind"][KIND_BANNED] == 1
    # 现场重算的结果必须回写 ctx —— plan_json.meta 要的是最终树的结论
    assert ctx["kb_scope_conformance"]["violations"] == 1
    assert "CONC_NEW_COLUMN" in ctx["scope_violations"][0]


def test_r1_gate_clean_tree_says_passed():
    """树完全在范围内 → 正文说"通过"，且不出现吓人的告警段。"""
    ctx = {"wbs": _wbs(("主体", "结构", "墙浇筑", "CONC_NEW_WALL")),
           "kb_scope": _scope(), "extracted_params": PARAMS}
    payload = _first_payload(_gate_events(ctx))
    assert "核对通过" in payload["summary"]
    assert payload["kb_scope_conformance"]["violations"] == 0
    assert "⚠" not in payload["summary"]


def test_r1_gate_without_scope_keeps_old_behaviour():
    """没有 kb_scope → 字段一个不加、摘要不提这件事（只增不改）。"""
    ctx = {"wbs": _wbs(("主体", "结构", "墙浇筑", "CONC_NEW_WALL")),
           "extracted_params": PARAMS}
    payload = _first_payload(_gate_events(ctx))
    assert "kb_scope_conformance" not in payload
    assert "知识库范围一致性" not in payload["summary"]
    assert payload["wbs_tree"], "既有字段一个都不能少"


def test_r1_gate_never_crashes_on_broken_scope():
    """kb_scope 是烂数据 → 门照常问，不许崩、不许给 null 字段。"""
    for bad in ("字符串", 123, {"l4_candidates": "不是字典"}, {"l4_candidates": {}}):
        ctx = {"wbs": _wbs(("主体", "结构", "墙浇筑", "CONC_NEW_WALL")),
               "kb_scope": bad, "extracted_params": PARAMS}
        payload = _first_payload(_gate_events(ctx))
        assert payload["summary"]
        assert all(v is not None for v in payload.values())


# ==================== ④ 留档进 plan_json.meta ====================
def test_meta_carries_conformance():
    """交付物通道：plan_json.meta.kb_scope_conformance 要能直接落盘。"""
    import json
    res = check_scope_conformance(
        _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN")), _scope())
    meta = build_meta({"kb_scope_conformance": res, "extracted_params": PARAMS})
    got = meta["kb_scope_conformance"]
    assert got["violations"] == 1
    assert json.loads(json.dumps(meta, ensure_ascii=False)) == meta


def test_meta_without_conformance_gives_empty_dict():
    """没跑过核对时给空字典（契约里字段恒存在，值可为空）。"""
    assert build_meta({})["kb_scope_conformance"] == {}
    assert build_meta({"extracted_params": PARAMS})["kb_scope_conformance"] == {}


# ==================== ⑤ 违规要真的上到终端（引擎的 opt-in 通道） ====================
def test_scope_violations_reach_node_done_payload():
    """护栏：违规得随 node_done 发出去，否则"抓到了但用户看不见"。

    `wbs_agent` 声明了 `warning_ctx_key = "scope_violations"`，引擎才会把它附到
    node_done 的 `warnings` 上（终端渲染"前 3 条 + 其余同类"）。这里验到载荷层。
    """
    from pipeline.engine import Pipeline

    node = WBSAgentNode()
    node._emit = lambda *a, **k: None
    ctx = {"wbs": _wbs(("二次结构与砌体", "砌体", "构造柱浇筑", "CONC_NEW_COLUMN")),
           "kb_scope": _scope()}
    node._run_scope_check(ctx)

    class _Stub(WBSAgentNode):
        def run(self, c):
            self.done_summary = "干完了"
            return {"scope_violations": list(c.get("scope_violations") or [])}

    stub = _Stub()
    stub._run_scope_check(ctx)
    events = []
    pipe = Pipeline(run_id="t")
    pipe.add_node(stub)
    pipe.run(ctx, emit=lambda ev, d: events.append((ev, d)))
    payload = [d for ev, d in events if ev == "node_done"][0]

    assert payload.get("warnings"), "范围违规没上行：用户永远看不到它"
    assert any("CONC_NEW_COLUMN" in w for w in payload["warnings"])


def test_no_violation_emits_no_warnings_key():
    """没有违规时不带 warnings 键（缺失即优雅退化，终端一字不变）。"""
    from pipeline.engine import Pipeline

    class _Stub(WBSAgentNode):
        def run(self, c):
            self.done_summary = "干完了"
            return {"scope_violations": []}

    stub = _Stub()
    events = []
    pipe = Pipeline(run_id="t")
    pipe.add_node(stub)
    pipe.run({"wbs": _wbs(("主体", "结构", "墙浇筑", "CONC_NEW_WALL")),
              "kb_scope": _scope()}, emit=lambda ev, d: events.append((ev, d)))
    payload = [d for ev, d in events if ev == "node_done"][0]
    assert "warnings" not in payload

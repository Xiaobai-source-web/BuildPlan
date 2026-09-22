# -*- coding: utf-8 -*-
"""终端斜杠命令测试 —— 「改得动」在终端里必须可用、好用、出错也不崩

覆盖新增的 5 条命令：/revise /versions /undo /goto /cost
以及它们与后端的配合：改完本地缓存要同步，否则接着 /show 看到的还是旧计划。

刻意**不连真后端**：用一个假的 client 记录收到的请求并回放预设响应，
这样断言的是"命令层的行为"，与网络无关。

运行：python -m pytest backend/tests/test_terminal_commands.py -q
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
# 必须先让 backend 可导入：conftest 的 autouse fixture 会 `from pipeline import config`，
# 而 conftest 在测试模块之前执行 —— 只跑本文件时若没把 backend 加进 sys.path，
# conftest 就会 ModuleNotFoundError（其他测试文件恰好各自加过，所以平时看不出来）。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import commands  # noqa: E402
import renderer  # noqa: E402


class _Ctx(object):
    def __init__(self, client=None, plan=None, plan_id=None):
        self.client = client
        self.current_plan = plan
        self.current_plan_id = plan_id
        self.history = []
        self.backend = "local"
        self.running = False
        self.run_id = "t"
        self.show_html = None


class _FakeClient(object):
    """记录请求、回放响应；可以设成抛异常来验证降级。"""

    def __init__(self, responses=None, boom=False):
        self.responses = responses or {}
        self.boom = boom
        self.calls = []

    def _get(self, key):
        if self.boom:
            raise RuntimeError("网络断了")
        return self.responses.get(key, (200, {}))

    def post_revise(self, plan_id, instruction):
        self.calls.append(("revise", plan_id, instruction))
        return self._get("revise")

    def get_versions(self, plan_id):
        self.calls.append(("versions", plan_id))
        return self._get("versions")

    def post_undo(self, plan_id):
        self.calls.append(("undo", plan_id))
        return self._get("undo")

    def post_goto(self, plan_id, version):
        self.calls.append(("goto", plan_id, version))
        return self._get("goto")


_ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _plain(text):
    """去掉 ANSI 颜色码，只比内容（renderer 在无 TTY 时也会加码）。"""
    return _ANSI.sub("", str(text))


# ==================== /help 必须列全新增命令 ====================
def test_help_lists_the_new_commands():
    out = _plain(commands.dispatch(_Ctx(), "/help"))
    for cmd in ("/revise", "/versions", "/undo", "/goto", "/cost"):
        assert cmd in out, cmd


# ==================== 没有计划时的提示要明确 ====================
def test_commands_without_a_plan_say_so():
    for cmd in ("/revise 把 5.1.1.1 的工期改成 20", "/versions", "/undo", "/goto 0"):
        out = _plain(commands.dispatch(_Ctx(), cmd))
        assert "还没有已生成的计划" in out, (cmd, out)


# ==================== /revise 正常路径 ====================
def test_revise_reports_summary_and_syncs_local_plan():
    new_plan = {"plan_id": "p1", "overview": {"total_duration_days": 55}}
    client = _FakeClient({"revise": (200, {
        "ok": True, "summary": "改完：生效 1 项修改；总工期 40 → 55 天",
        "applied": [{"target": "5.1.1.1", "field": "quantity", "value": 300}],
        "rejected": [], "warnings": ["提示一条"],
        "total_duration_days": 55, "plan": new_plan})})
    ctx = _Ctx(client=client, plan_id="p1")

    out = _plain(commands.dispatch(ctx, "/revise 把 5.1.1.1 的工程量改成 300"))

    assert client.calls == [("revise", "p1", "把 5.1.1.1 的工程量改成 300")]
    assert "总工期 40 → 55 天" in out
    assert "5.1.1.1" in out and "quantity" in out
    assert "提示一条" in out
    assert ctx.current_plan is new_plan, "改完必须同步本地缓存，否则 /show 还是旧计划"


def test_revise_without_arguments_prints_usage():
    client = _FakeClient()
    ctx = _Ctx(client=client, plan_id="p1")
    out = _plain(commands.dispatch(ctx, "/revise"))
    assert "用法" in out and client.calls == [], "没给原话时不该发请求"


def test_revise_surfaces_rejected_patches():
    client = _FakeClient({"revise": (200, {
        "ok": False, "summary": "改完：没有修改生效",
        "applied": [], "total_duration_days": 40,
        "rejected": [{"patch": {"target": "9.9.9"}, "reason": "清单里没有这个任务"}]})})
    out = _plain(commands.dispatch(_Ctx(client=client, plan_id="p1"), "/revise 改 9.9.9"))
    assert "9.9.9" in out and "清单里没有这个任务" in out


def test_revise_handles_backend_error_and_network_failure():
    bad = _FakeClient({"revise": (400, {"error": "缺少 instruction"})})
    assert "缺少 instruction" in _plain(
        commands.dispatch(_Ctx(client=bad, plan_id="p1"), "/revise 随便"))
    boom = _FakeClient(boom=True)
    assert "修改请求失败" in _plain(
        commands.dispatch(_Ctx(client=boom, plan_id="p1"), "/revise 随便"))


# ==================== /versions /undo /goto ====================
def test_versions_lists_the_chain():
    client = _FakeClient({"versions": (200, {
        "versions": [{"版本": "v0", "序号": 0, "说明": "基线"},
                     {"版本": "v1", "序号": 1, "说明": "5.1.1.1 → quantity=300"}],
        "history": [{"时间": "", "用户原话": "（基线）", "摘要": "初始计划"},
                    {"时间": "2026-01-02", "用户原话": "把量改成300", "摘要": "改完"}]})})
    out = _plain(commands.dispatch(_Ctx(client=client, plan_id="p1"), "/versions"))
    assert "v0" in out and "v1" in out and "quantity=300" in out
    assert "把量改成300" in out
    assert "/undo" in out, "要提示怎么回退"


def test_versions_on_a_plan_without_revisions():
    client = _FakeClient({"versions": (200, {"versions": [{"版本": "v0", "序号": 0,
                                                          "说明": "基线"}]})})
    out = _plain(commands.dispatch(_Ctx(client=client, plan_id="p1"), "/versions"))
    assert "还没有任何修改" in out


def test_undo_syncs_local_plan():
    plan = {"plan_id": "p1", "overview": {"total_duration_days": 40}}
    client = _FakeClient({"undo": (200, {"ok": True, "total_duration_days": 40,
                                         "plan": plan})})
    ctx = _Ctx(client=client, plan_id="p1")
    out = _plain(commands.dispatch(ctx, "/undo"))
    assert "已回退一轮" in out and "40" in out
    assert ctx.current_plan is plan


def test_goto_requires_a_numeric_version():
    client = _FakeClient()
    ctx = _Ctx(client=client, plan_id="p1")
    assert "用法" in _plain(commands.dispatch(ctx, "/goto abc"))
    assert client.calls == []


def test_goto_sends_the_version_number():
    client = _FakeClient({"goto": (200, {"ok": True, "total_duration_days": 40,
                                         "plan": {"plan_id": "p1"}})})
    out = _plain(commands.dispatch(_Ctx(client=client, plan_id="p1"), "/goto 0"))
    assert client.calls == [("goto", "p1", 0)]
    assert "v0" in out


# ==================== /cost ====================
def test_cost_prints_usage_and_money():
    plan = {"meta": {"usage": {"calls": 12, "prompt_tokens": 3400, "completion_tokens": 900,
                               "total_tokens": 4300, "cost_cny": 0.0172,
                               "model": "qwen-plus", "note": "元/千token",
                               "by_node": {"wbs_agent": 3000, "reporter": 1300}}}}
    out = _plain(commands.dispatch(_Ctx(plan=plan, plan_id="p1"), "/cost"))
    assert "4,300" in out or "4300" in out
    assert "0.0172" in out
    assert "wbs_agent" in out and "3000" in out
    assert "qwen-plus" in out


def test_cost_reads_the_field_names_usage_actually_produces():
    """用例名就是契约：/cost 读的键必须与 pipeline/usage.snapshot() 一致。

    snapshot() 产出的是 `cost_cny`（不是 cost_yuan）、`by_node` 是 {环节: 整数}。
    读错键的后果是"费用永远显示不出来"，而且不会报错 —— 静默失效。
    """
    from pipeline.usage import meter
    snap = meter().snapshot()
    for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens",
                "cost_cny", "by_node", "model", "note"):
        assert key in snap, "usage.snapshot() 必须给出 %s" % key
    assert isinstance(snap["by_node"], dict)


def test_cost_without_usage_is_honest():
    out = _plain(commands.dispatch(_Ctx(plan={"meta": {}}, plan_id="p1"), "/cost"))
    assert "没有记录到 token" in out


# ==================== 三轮回审门的终端交互 ====================
class _AuditClient(_FakeClient):
    def post_params(self, review_id, passed, manual_input=None, run_id=None):
        self.calls.append(("params", review_id, bool(passed), manual_input))
        return 200, {}


def _ask_audit(monkeypatch, answers, data=None):
    """跑一次审计门交互；answers 依次喂给 input()。"""
    import confirmer

    client = _AuditClient()
    payload = {"review_id": "au1", "purpose": "audit", "round": 3,
               "summary": "【第 3 轮 · Word 草案审计（不含图表）】\n  草案文件：d.docx",
               "next_hint": "如计划已成熟，请输入 Y，我将整理出最终计划并绘制可视化看板。"}
    payload.update(data or {})
    seq = list(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: seq.pop(0) if seq else "")
    confirmer._ask_audit(client, payload, "au1", "t")
    return client


def test_audit_gate_accepts_explicit_yes(monkeypatch):
    client = _ask_audit(monkeypatch, ["Y"])
    assert client.calls == [("params", "au1", True, None)]


def test_audit_gate_blank_enter_is_not_approval(monkeypatch):
    """回车**不等于**认可：审计是签字，必须明确打 Y（与其它门的语义刻意不同）。"""
    client = _ask_audit(monkeypatch, ["", "y"])
    assert client.calls == [("params", "au1", True, None)], \
        "第一次回车不该提交任何决策，第二次打 y 才算通过"


def test_audit_gate_comment_is_a_rejection(monkeypatch):
    client = _ask_audit(monkeypatch, ["主体班组太小，钢筋工要 30 人"])
    assert client.calls == [("params", "au1", False, "主体班组太小，钢筋工要 30 人")]


def test_audit_gate_eof_rejects_instead_of_hanging(monkeypatch):
    """读不到输入（EOF）时按"退回"处理，绝不默认通过、也不死锁。"""
    import confirmer

    client = _AuditClient()
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: (_ for _ in ()).throw(EOFError()))
    confirmer._ask_audit(client, {"review_id": "au1", "purpose": "audit",
                                 "summary": "x", "next_hint": "y"}, "au1", "t")
    assert client.calls == [("params", "au1", False, None)]


# ==================== 未知命令 ====================
def test_unknown_command_lists_help_hint():
    out = _plain(commands.dispatch(_Ctx(), "/nope"))
    assert "未知命令" in out and "/help" in out

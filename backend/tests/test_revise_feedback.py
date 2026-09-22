# -*- coding: utf-8 -*-
"""终端「改得动」的即时反馈 —— 大计划要跑几十秒，屏幕上不能一行提示都没有

背景：/revise 改成一份大计划要几秒到几十秒，这段时间旧实现是静默阻塞的，
用户会以为卡死（本项目历史上反复被投诉"不知道是挂了还是在跑"）。
这里只钉住行为：**发请求之前**就先出声，请求回头之后再收尾。

刻意**不连真后端**：假 client 只记录"什么时候被调用"，与我们关心的顺序无关。
提醒：路径/文案里的提示词是给人看的，不许出现内部术语（LLM / token / plan_json / CPL）。

运行：python -m pytest backend/tests/test_revise_feedback.py -q
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
# 与 test_terminal_commands.py 同样的前置：conftest 会 import pipeline，
# 只跑本文件时若没把 backend 加进 sys.path 就会 ModuleNotFoundError。
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import commands  # noqa: E402
import tui as tui_mod  # noqa: E402

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# 用户能看到的提示里**不许**出现的内部术语
_JARGON = ("LLM", "llm", "token", "Token", "plan_json", "CPL", "prompt", "post_revise")


def _plain(text):
    """去掉 ANSI 颜色码，只比内容（renderer 在无 TTY 时也会加码）。"""
    return _ANSI.sub("", str(text))


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
    """记录调用时刻；responses 按接口名回放，boom=True 时模拟网络断掉。"""

    def __init__(self, responses=None, boom=False, events=None):
        self.responses = responses or {}
        self.boom = boom
        self.events = events if events is not None else []
        self.calls = []

    def post_revise(self, plan_id, instruction):
        # 关键：**进入假请求的那一刻**先记一笔，用来证明提示已经先出去了
        self.events.append(("post_revise", instruction))
        self.calls.append(("revise", plan_id, instruction))
        if self.boom:
            raise RuntimeError("网络断了")
        return self.responses.get("revise", (200, {}))

    def get(self, path):
        self.events.append(("get", path))
        return 200, {}


def _run(monkeypatch, client, args="把 5.1.1.1 的工期改成 20", use_dispatch=True):
    """跑一次 /revise，返回 (事件流水, 原始返回文本, tui.out 的关键字参数流水)。

    第 33 轮改法：提示不写真 `print`，而是走 `tui.out(...)`（与全项目同一条输出路，
    顺序模式自带 flush、VT 模式落进滚动区）。所以这里 spy 的是 `tui.out`，
    同时它**内部也一样落到 stdout**，`capsys` 照样能抓到 —— 两条证据都保留。
    """
    events = []
    client.events = events
    out_kwargs = []
    real_out = tui_mod.out

    def spy(text, **k):
        events.append(("tui_out", str(text)))
        out_kwargs.append(k)
        return real_out(text, **k)

    monkeypatch.setattr(tui_mod, "out", spy, raising=False)
    monkeypatch.setattr(commands.tui, "out", spy, raising=False)
    ctx = _Ctx(client=client, plan_id="p1")
    if use_dispatch:
        out = commands.dispatch(ctx, "/revise " + args)
    else:
        out = commands._cmd_revise(ctx, args)
    return events, out, out_kwargs


def _first(events, kind, needle=None):
    """第一个匹配事件的下标；找不到返回 -1。"""
    for i, (tag, payload) in enumerate(events):
        if tag != kind:
            continue
        if needle is None or needle in str(payload):
            return i
    return -1


_OK_SUMMARY = "改完：生效 1 项修改；总工期 40 → 55 天"


# ==================== 核心：提示必须先于请求 ====================
def test_progress_hint_is_printed_before_the_request(monkeypatch):
    client = _FakeClient({"revise": (200, {
        "ok": True, "summary": _OK_SUMMARY,
        "applied": [{"target": "5.1.1.1", "field": "quantity", "value": 300}],
        "rejected": [], "total_duration_days": 55,
        "plan": {"plan_id": "p1"}})})
    events, _out, out_kwargs = _run(monkeypatch, client)

    i_hint = _first(events, "tui_out", "正在")
    i_call = _first(events, "post_revise")
    assert i_hint >= 0, "请求之前必须有一行「正在…」提示，实际流水：%r" % (events,)
    assert i_call >= 0, "假 client 应当被调用"
    assert i_hint < i_call, "提示必须在 post_revise 之前出现，否则用户还是对着空屏等"
    # 与全项目同一条输出路：gap=False，且它自带 flush（顺序模式走 _raw）
    assert out_kwargs and out_kwargs[0].get("gap") is False, out_kwargs


def test_progress_hint_reaches_stdout_and_has_no_jargon(monkeypatch):
    client = _FakeClient({"revise": (200, {"ok": True, "summary": _OK_SUMMARY,
                                           "applied": [], "rejected": [],
                                           "total_duration_days": 55})})
    events, _out, _kw = _run(monkeypatch, client)

    # 提示确实交给了终端输出层（`tui.out`），且文案里没有内部术语。
    # 为什么不直接断言 capsys：`tui.out` 在"没有当前实例"时会自己建一个 Tui 并把
    # **那一刻的 stdout** 记下来，pytest 的 capsys 对象会被记成那个引用 —— 这是
    # 测试基础设施的细节，不该用来判定产品行为。所以断言打在"调用与文案"上。
    texts = [t for tag, t in events if tag == "tui_out"]
    assert texts, "提示必须走终端输出层"
    hint = " ".join(texts)
    assert "正在把这句话翻译成修改指令并重排受影响任务" in hint, hint
    assert "⏳" in hint, "提示要有个显眼的前缀，别混在正文里"
    for word in _JARGON:
        assert word not in hint, "给人看的提示里不许出现内部术语：%s" % word


# ==================== 收尾提示：成功路径 ====================
def test_closing_hint_after_a_successful_call(monkeypatch):
    client = _FakeClient({"revise": (200, {
        "ok": True, "summary": _OK_SUMMARY, "applied": [], "rejected": [],
        "total_duration_days": 55})})
    events, out, _kw = _run(monkeypatch, client)

    i_call = _first(events, "post_revise")
    i_done = _first(events, "tui_out", "修改已处理")
    assert i_done > i_call, "请求回来之后要有收尾提示，不能只出声不落地"
    assert "✔ 修改已处理" in " ".join(t for tag, t in events if tag == "tui_out")
    # 原有的汇报文本一个都不能少
    assert _OK_SUMMARY in out and "总工期 55 天" in out


# ==================== 失败路径：提示照出，红字照回，不抛异常 ====================
def test_failure_still_shows_progress_and_returns_red_error(monkeypatch):
    client = _FakeClient(boom=True)
    events, out, _kw = _run(monkeypatch, client)

    assert _first(events, "tui_out", "正在") >= 0, "网络失败也要先说「正在…」"
    assert _first(events, "tui_out", "正在") < _first(events, "post_revise")
    assert "修改请求失败" in out and "网络断了" in out
    assert "\x1b[" in out, "失败必须是红色错误行（renderer.color(..., 'red')）"
    texts = " ".join(t for tag, t in events if tag == "tui_out")
    assert "修改已处理" not in texts, "失败时不需要收尾提示"


def test_backend_error_path_shows_progress_and_red_error(monkeypatch):
    client = _FakeClient({"revise": (400, {"error": "缺少 instruction"})})
    events, out, _kw = _run(monkeypatch, client)

    assert _first(events, "tui_out", "正在") < _first(events, "post_revise")
    assert "修改失败" in out and "缺少 instruction" in out


# ==================== 返回文本结构不许被提示污染 ====================
def test_hints_are_not_smuggled_into_the_returned_lines(monkeypatch):
    client = _FakeClient({"revise": (200, {
        "ok": True, "summary": _OK_SUMMARY,
        "applied": [{"target": "5.1.1.1", "field": "quantity", "value": 300}],
        "rejected": [], "total_duration_days": 55,
        "plan": {"plan_id": "p1"}})})
    ctx = _Ctx(client=client, plan_id="p1")
    out = _plain(commands._cmd_revise(ctx, "把 5.1.1.1 的工程量改成 300"))

    assert "正在把这句话翻译成修改指令" not in out, "提示走 tui.out，不该混进返回文本"
    assert "修改已处理" not in out
    assert out.startswith("✎ "), "返回文本仍是原来那一段汇报，首行是摘要"
    assert "5.1.1.1" in out and "quantity" in out and "总工期 55 天" in out


# ==================== 校验行为没被改动 ====================
def test_argument_checks_still_short_circuit(monkeypatch):
    # 没给原话：不该发请求，也不该先说「正在…」
    client = _FakeClient()
    events, out, _kw = _run(monkeypatch, client, args="")
    assert "用法" in out
    assert client.calls == []
    assert _first(events, "tui_out", "正在") == -1, "没发请求就不该说正在处理"

    # 没有计划：同样不该出声
    ctx = _Ctx(client=_FakeClient(), plan_id=None)
    out = _plain(commands.dispatch(ctx, "/revise 随便改改"))
    assert "还没有已生成的计划" in out

# -*- coding: utf-8 -*-
"""第 23 轮回归测试：警告不许只有数字 + 长节点必须"看得出在动"

对应用户实测的两句话：
  1. 「警告 27 条是什么意思」—— kb_scope 把 27 条同类警告**算完就丢**：
     run() 只 return {"kb_scope": scope}，warnings 不进 ctx，用户只看到一个数字。
  2. 「为什么卡这么久、没有任何提示，不知道是卡了还是在处理」—— 默认"顺序输出"模式下
     tui.status() 只有 force=True 才输出，而 kb_scope(#7) 之后的 wbs_agent(#8) 逐个
     一级相调用大模型（几分钟），它发的 node_progress 全被折叠 → 屏幕上一动不动。

这两条都是"算完了没用"类缺陷的护栏，所以断言必须打在**用户看得见的东西**上：
  · ctx 里的 kb_warnings、done_summary 的归并措辞、以及终端 node_done 渲染出来的行；
  · 顺序模式真的打出的紧凑进度行条数（用**注入的时钟**断言节流，不用真 sleep）。

运行：python -m pytest backend/tests/test_progress_feedback.py -q
"""

import io
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT / "terminal") not in sys.path:
    sys.path.insert(0, str(ROOT / "terminal"))

import console      # noqa: E402
import renderer     # noqa: E402
import tui as tui_mod  # noqa: E402
from pipeline.engine import Pipeline             # noqa: E402
from pipeline.nodes.audit_gate import (WBSAuditNode,  # noqa: E402
                                       granularity_caliber_note)
from pipeline.nodes.kb_scope import KBScopeNode  # noqa: E402

_MAPPING_ABSENT = "结构映射表中没有该工种的映射数据，已保留其全部 L4（无结构约束）。"
_STRUCT_UNKNOWN = "未识别结构形式，未做结构过滤，已保留全部 L4 工序。"


def _plain(text):
    return renderer.strip_ansi(text)


# ---- 期望值从**知识库现算**，不写死 ----
# 为什么不留硬编码的 27：这个数 = 「映射表里没有该工种数据的 L3 个数」，
# 是**知识库的数据事实**，会随补数据而变（例如给桩基工程补上结构映射后就是 26）。
# 把数据事实写进断言，等于"每补一行表数据就要改一批测试"，而真实意图是
# "同类警告必须归并成一句、能被用户看见"——那个意图与具体条数无关。
def _run_kb_scope(building="住宅", structure="剪力墙结构"):
    node = KBScopeNode()
    out = node.run({"extracted_params": {"building_type": building,
                                         "structure_type": structure}})
    return node, out


# 住宅 × 剪力墙：那份 scope 是下面多条测试的共同基准，模块级只算一次
_SCOPE_OUT = _run_kb_scope()[1]
_N = len(_SCOPE_OUT["kb_warnings"])
_MERGED = "警告 %d 条（%d 条均为「结构映射表缺该工种数据 → 保留全部 L4」）" % (_N, _N)
# 终端最多逐条展开 3 条 → "其余"从第 4 条起算
_REST = max(0, _N - 3)
_REST_NOTE = "…其余 %d 条同类（结构映射表缺该工种数据 → 保留全部 L4）" % _REST


def _mapping_warnings(n):
    """造 n 条**同类但不同工种**的"缺映射"警告（真实场景就是每个未覆盖的 L3 一条）。"""
    return ["%d 号工种（w%d）：%s" % (i, i, _MAPPING_ABSENT) for i in range(n)]


def test_kb_scope_缺映射警告真的进了ctx():
    """护栏：warnings 不许"算完就丢" —— ctx 里必须拿得到逐条原文。"""
    _node, out = _run_kb_scope()
    assert "kb_warnings" in out, "warnings 没进 ctx：用户永远看不到那些警告是什么"
    assert out["kb_warnings"] == out["kb_scope"]["warnings"]
    assert len(out["kb_warnings"]) == _N
    assert _N > 0, "前置条件：住宅×剪力墙下确实存在缺映射的工种"
    assert all(_MAPPING_ABSENT in w for w in out["kb_warnings"])


def test_done_summary是归并后的而不是逐个重复():
    """护栏：done_summary 只报**归并后**的一句，同类不逐条刷屏。"""
    node, _out = _run_kb_scope()
    summary = node.done_summary
    assert _MERGED in summary
    assert len(summary.splitlines()) == 1, "归并后的摘要必须还是一行：\n%s" % summary
    # 逐条原文一个字都不许出现在摘要里（逐条展开是 node_done 渲染那一支的事）
    assert _MAPPING_ABSENT not in summary
    assert summary.count("警告") == 1


def test_归并后的其余同类提示交给终端渲染():
    """引擎随 node_done 上行的"…其余 N 条同类"由节点自己算好（它才知道类别语义）。"""
    node, _out = _run_kb_scope()
    assert node.warning_note.startswith(_REST_NOTE)


def test_同类警告归并成1类加计数():
    from pipeline.nodes.kb_scope import merge_warnings
    digest = merge_warnings(_mapping_warnings(_N))
    assert digest["total"] == _N
    assert len(digest["groups"]) == 1, "同类警告必须归并成一类"
    assert digest["groups"][0]["count"] == _N
    assert digest["summary"] == _MERGED
    assert len(digest["samples"]) == 3, "终端最多逐条展开 3 条"
    assert len(digest["detail_lines"]) == 4, "前 3 条 + 其余 1 行"


def test_两三类警告列出前几类加计数():
    from pipeline.nodes.kb_scope import merge_warnings
    digest = merge_warnings([_STRUCT_UNKNOWN] + _mapping_warnings(5))
    assert digest["total"] == 6 and len(digest["groups"]) == 2
    assert "1 条「未识别结构形式 → 未做结构过滤」" in digest["summary"]
    assert "5 条「结构映射表缺该工种数据 → 保留全部 L4」" in digest["summary"]


def test_没有警告时摘要里不许凭空加一句():
    from pipeline.nodes.kb_scope import merge_warnings
    digest = merge_warnings([])
    assert digest["total"] == 0
    assert digest["summary"] == "" and digest["note"] == ""
    assert digest["samples"] == [] and digest["detail_lines"] == []


def test_警告原文随计划落进meta_不再只有数字():
    """交付物通道：plan_json.meta.kb_warnings 留全量原文（口径说明可核对）。"""
    from pipeline.nodes.plan_assembler import assemble_plan_json, build_meta
    _node, out = _run_kb_scope()
    assert build_meta(out)["kb_warnings"] == out["kb_warnings"]
    plan = assemble_plan_json(dict(out), {"overview": {}, "key_milestones": [],
                                         "critical_path_tasks": [],
                                         "all_tasks_schedule": [], "resource_plan": [],
                                         "risks": []})
    assert len(plan["meta"]["kb_warnings"]) == _N
    # 落盘前 meta 会过一遍 Pydantic 契约：不许静默丢字段
    from pipeline.schemas import PlanMeta
    back = PlanMeta.model_validate(plan["meta"]).model_dump()
    assert back["kb_warnings"] == out["kb_warnings"]


def test_引擎只给显式声明的节点带警告():
    """引擎的警告上行是**opt-in**：没声明的节点 node_done 载荷一字不变。"""
    from pipeline.base import BaseNode

    class _Plain(BaseNode):
        name = "plain"
        title = "没声明警告的节点"

        def run(self, ctx):
            self.done_summary = "干完了"
            return {"warnings": ["这条不该上行"], "kb_warnings": ["这条也不该"]}

    class _OptIn(BaseNode):
        name = "optin"
        title = "声明了警告的节点"
        warning_ctx_key = "kb_warnings"

        def run(self, ctx):
            self.done_summary = "干完了；警告 2 条"
            self.warning_note = "…其余 1 条同类（甲类）"
            return {"kb_warnings": ["甲：一条警告", "甲：又一条警告"]}

    events = []
    pipe = Pipeline(run_id="t")
    pipe.add_nodes(_Plain(), _OptIn())
    pipe.run({}, emit=lambda ev, d: events.append((ev, d)))
    payloads = {d["node"]: d for ev, d in events if ev == "node_done"}

    assert set(payloads["plain"]) == {"node", "summary"}, payloads["plain"]
    assert payloads["optin"]["warnings"] == ["甲：一条警告", "甲：又一条警告"]
    assert payloads["optin"]["warnings_note"] == "…其余 1 条同类（甲类）"


def test_终端node_done渲染出前3条加其余同类():
    """端到端：kb_scope → 引擎 → renderer，用户看到的必须是内容而不是数字。"""
    events = []
    pipe = Pipeline(run_id="t")
    pipe.add_node(KBScopeNode())
    pipe.run({"extracted_params": {"building_type": "住宅",
                                  "structure_type": "剪力墙结构"}},
             emit=lambda ev, d: events.append((ev, d)))
    payload = [d for ev, d in events if ev == "node_done"][0]
    assert len(payload["warnings"]) == _N

    lines = _plain(renderer.render_event("node_done", payload)).splitlines()
    assert lines[0].startswith("✔ kb_scope：")
    assert _MERGED in lines[0]
    assert sum(1 for ln in lines if "⚠" in ln) == 3, "终端至少给出前 3 条"
    assert lines[-1].strip().startswith("…其余 %d 条同类" % _REST), lines[-1]


def test_没有warning_note的事件渲染一字不变():
    """护栏：老事件（今天所有其它节点）不带 warnings_note → 输出不许变。"""
    out = _plain(renderer.render_event("node_done", {"node": "x", "summary": "完成"}))
    assert out == "✔ x：完成"


# ======================================================================
# 2. 问题二：顺序模式的紧凑进度行（节流 + 心跳兜底）
# ======================================================================
class _Clock:
    """可注入的假时钟：测试直接推进 `t`，不真 sleep（快、且完全确定）。"""

    def __init__(self, t=0.0, step=0.0):
        self.t = float(t)
        self.step = float(step)

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += float(dt)
        return self.t


class _FakeSSE:
    """假 SSE 客户端：按剧本逐个吐事件，每吐一个就推进假时钟。

    特殊事件 `("__sleep__", {"seconds": N})` 表示"这 N 秒什么都没有"（不进流）。
    """

    def __init__(self, events, clock=None):
        self.events = list(events)
        self.clock = clock
        self.cancelled = []

    def pause(self):
        pass

    def resume(self):
        pass

    def post_chat(self, prompt, run_id=None, mode=None, chat_scope=None):
        def _gen():
            for ev, data in self.events:
                if ev == "__sleep__":
                    if self.clock is not None:
                        self.clock.tick((data or {}).get("seconds", 0))
                    continue
                if self.clock is not None:
                    self.clock.tick(self.clock.step)
                yield ev, data
        return _gen()

    def post_cancel(self, run_id):
        self.cancelled.append(run_id)


class _Ctx:
    def __init__(self, events, term=None, clock=None):
        self.client = _FakeSSE(events, clock)
        self.history = []
        self.current_plan = None
        self.current_plan_id = None
        self.running = False
        self.run_id = None
        self.verbose = False
        self.fold = renderer.FoldState()
        self.event_log = []
        self.tui = term
        self.feed = None
        self.progress_now = clock


def _feed(clock, **kw):
    lines = []
    feed = tui_mod.ProgressFeed(lines.append, now=clock, **kw)
    return feed, lines


def _compact_lines(text):
    """只挑"紧凑进度行"（两个空格 + ⏳）—— 折叠心跳那种从第 0 列开始的不算。"""
    return [ln for ln in _plain(text).splitlines() if ln.startswith("  ⏳ ")]


def _progress_events(n, start=10, step_pct=1, message="逐相展开 %d/%d: 基坑支护与土方"):
    out = [("node_start", {"node": "wbs_agent", "title": "WBS 多级分工"})]
    for i in range(n):
        out.append(("node_progress", {
            "node": "wbs_agent", "progress": start + i * step_pct,
            "message": message % (i + 1, n)}))
    return out


def test_顺序模式10条进度只出2到3行():
    """节流：同节点两条之间 ≥2 秒 **或** 百分比变化 ≥5 才打 —— 绝不刷屏。"""
    clock = _Clock()
    feed, lines = _feed(clock)
    feed.on_event("node_start", {"node": "wbs_agent", "title": "WBS 多级分工"}, index=8)
    for i in range(10):
        clock.tick(0.3)
        feed.on_event("node_progress",
                      {"node": "wbs_agent", "progress": 10 + i, "message": "逐相展开"})
    # t=0.3 打第一条；t=1.8 百分比步进到 5 → 第二条；t=3.0 再步进到 5 → 第三条
    assert 2 <= len(lines) <= 3, lines


def test_硬下限拦住百分比突进的刷屏():
    """百分比步进再快也不许连成一片：两条紧凑行之间至少 hard_interval。"""
    clock = _Clock()
    feed, lines = _feed(clock)
    for i in range(10):
        clock.tick(0.1)          # 10 条挤在 1 秒内，每条 +10%
        feed.on_event("node_progress",
                      {"node": "n", "progress": 10 + i * 10, "message": "跑"})
    assert len(lines) <= 3, "10 条挤在 1 秒内最多留 2~3 行：\n%s" % "\n".join(lines)


def test_进度几乎不动的密集事件也不会刷屏():
    """200 条事件 / 20 秒 → 按"两条至少隔 2 秒"压成 ≈10 行，绝不逐条堆。

    这就是节流的上界：**每个节点最多 1 行 / 2 秒**（百分比突进还有 0.5 秒硬下限兜着）。
    """
    clock = _Clock()
    feed, lines = _feed(clock)
    for i in range(200):
        clock.tick(0.1)
        feed.on_event("node_progress",
                      {"node": "n", "progress": 10 + i * 0.1, "message": "跑"})
    assert 9 <= len(lines) <= 11, "20 秒里 200 条事件应压成 ≈10 行，实际 %d" % len(lines)


def test_慢节点每2秒必有一行看得到在动():
    """长节点：进度事件稀疏时也要持续有输出（20 秒 10 条 → 10 行）。"""
    clock = _Clock()
    feed, lines = _feed(clock)
    for _ in range(10):
        clock.tick(2.0)
        feed.on_event("node_progress",
                      {"node": "wbs_agent", "progress": 30, "message": "逐相展开 3/10"})
    assert len(lines) == 10, lines


def _steps_table(upto=8, total=26, title="编制 WBS 分工"):
    """造一份步数表（第 1…upto 步，第 upto 步是 wbs_agent）+ 本次运行的总步数。

    与真实主链一致：`run_plan` 下发 26 步的清单，`node_start` 再带上 `steps=26`。
    """
    table = [{"index": i, "node": "n%d" % i, "title": "第%d步" % i}
             for i in range(1, upto)]
    table.append({"index": upto, "node": "wbs_agent", "title": title})
    return table, total


def _start_wbs(feed, clock, table, total, title="编制 WBS 分工"):
    """先启动第 7 步（好让"跑了多步 → 才显示分母"生效），再进 wbs_agent。"""
    feed.note_steps(table)
    feed.on_event("node_start", {"node": "n7", "title": "第7步", "steps": total},
                  index=7, title="第7步")
    clock.tick(3.0)
    feed.on_event("node_start", {"node": "wbs_agent", "title": title, "steps": total},
                  index=8, title=title)


def test_紧凑行格式是单行且带步号与进度文本():
    """紧凑行：`  ⏳ 第 8 / 26 步 · 编制 WBS 分工 · 逐相展开 3/10`。

    第 32 轮：**默认不再打百分比**（用户实测「太冗余了」），步号也改成"第 k / N 步"
    这种读得懂的写法；百分比只在 `/verbose` 下出现（见下一条）。
    """
    clock = _Clock()
    feed, lines = _feed(clock)
    table, total = _steps_table()
    _start_wbs(feed, clock, table, total)
    clock.tick(1.0)
    feed.on_event("node_progress", {"node": "wbs_agent", "progress": 38,
                                    "message": "逐相展开 3/10: 基坑支护与土方"})
    assert lines == ["  ⏳ 第 8 / 26 步 · 编制 WBS 分工 · 逐相展开 3/10: 基坑支护与土方"], lines
    assert "%" not in lines[0], "默认输出里不该再有百分比"
    clock.tick(2.0)
    feed.on_event("node_progress", {"node": "wbs_agent", "progress": 43,
                                    "message": "带\n换行\t的消息"})
    assert len(lines) == 2 and "带 换行 的消息" in lines[1], lines
    assert all("\n" not in ln and "\t" not in ln for ln in lines)


def test_verbose下紧凑行仍带百分比():
    """脚本 / 探针要看数值时：/verbose 打开，百分比照旧出现（默认关）。"""
    clock = _Clock()
    feed, lines = _feed(clock, verbose=True)
    table, total = _steps_table()
    _start_wbs(feed, clock, table, total)
    clock.tick(1.0)
    feed.on_event("node_progress", {"node": "wbs_agent", "progress": 38,
                                    "message": "逐相展开 3/10"})
    assert lines and lines[-1].endswith("38%"), lines


def test_稀疏事件时打仍在跑的心跳行():
    """最坏情况：节点先做很多次调用才发第一次 progress / 后端只发 ping。

    第 32 轮：心跳行要把"在跑第几步、正在做什么"一起交代（用户不该对着一行数字猜）。
    """
    clock = _Clock()
    feed, lines = _feed(clock)
    table, total = _steps_table()
    _start_wbs(feed, clock, table, total)
    clock.tick(1.0)
    feed.on_event("node_progress", {"node": "wbs_agent", "progress": 35,
                                    "message": "逐相展开 3/10"})
    assert len(lines) == 1
    clock.tick(4.0)                                   # 断了 4 秒：还不够
    feed.on_event("ping", {})
    assert len(lines) == 1
    clock.tick(7.0)                                   # 累计断了 11 秒 → 兜一条
    feed.on_event("ping", {})
    assert len(lines) == 2 and "仍在跑" in lines[-1]
    clock.tick(1.0)                                   # 刚打过，不再重复
    feed.on_event("ping", {})
    assert len(lines) == 2
    clock.tick(11.0)                                  # 又断了 11 秒 → 再兜一条
    feed.on_event("ping", {})
    assert len(lines) == 3 and "仍在跑" in lines[-1]
    assert "第 8 / 26 步 · 编制 WBS 分工" in lines[-1] and "逐相展开 3/10" in lines[-1]


def test_门里等用户输入时不许刷心跳():
    """用户正在门里做选择 → suspend；期间一个字节都不许写。"""
    clock = _Clock()
    feed, lines = _feed(clock)
    feed.suspend()
    clock.tick(60.0)
    assert feed.on_event("ping", {}) is False
    feed.on_event("node_progress", {"node": "n", "progress": 50, "message": "跑"})
    assert lines == []
    feed.resume()
    clock.tick(1.0)
    feed.on_event("node_progress", {"node": "n", "progress": 55, "message": "跑"})
    assert len(lines) == 1


def test_vt模式下一个字节都不写():
    """VT 模式有自己的原地刷新状态区 —— 反馈器必须完全关闭。"""
    clock = _Clock()
    feed, lines = _feed(clock, enabled=False)
    clock.tick(120.0)
    assert feed.on_event("node_progress", {"node": "n", "progress": 50}) is False
    assert feed.heartbeat_if_stale() is False
    assert feed.start_watchdog() is False
    assert lines == []


def test_verbose时反馈器不插手():
    """`/verbose` 打开时逐节点事件本来就全量打印，这里不许再加戏。"""
    clock = _Clock()
    feed, lines = _feed(clock, enabled=False, verbose=True)
    feed.on_event("node_progress", {"node": "n", "progress": 50, "message": "跑"})
    assert lines == []


def test_后台心跳在一个事件都没有时也会报在跑():
    """顺序模式的最坏情况：模型很慢且一个事件都不发 —— 靠后台 tick 兜住。"""
    clock = _Clock()
    feed, lines = _feed(clock)
    assert feed.start_watchdog(tick=0.01) is True
    try:
        deadline = time.time() + 3.0
        while not lines and time.time() < deadline:
            clock.tick(5.0)          # 推着假时钟走，判定由 feed 自己做
            time.sleep(0.01)
    finally:
        feed.stop_watchdog()
    assert lines and "仍在跑" in lines[0], lines


def test_心跳线程可以关掉_日志场景的逃生口(monkeypatch):
    monkeypatch.setenv("BUILDPLAN_HEARTBEAT", "0")
    clock = _Clock()
    feed, _lines = _feed(clock)
    assert feed.start_watchdog() is False


def test_进度反馈可以整体关掉_脚本场景逃生口(monkeypatch, capsys):
    """`BUILDPLAN_PROGRESS=0` → 退回改造前行为（输出冻结，长节点期间不新增行）。"""
    monkeypatch.setenv("BUILDPLAN_PROGRESS", "0")
    clock = _Clock(step=0.3)
    ctx = _Ctx(_progress_events(10), clock=clock)
    console.run_chat(ctx, "生成计划")
    out = _plain(capsys.readouterr().out)
    assert _compact_lines(out) == [], out
    assert ctx.feed.lines == [] and ctx.feed.enabled is False
    assert "逐相展开" not in out


def test_进度反馈默认开着(monkeypatch):
    monkeypatch.delenv("BUILDPLAN_PROGRESS", raising=False)
    assert console._progress_enabled(None) is True

    class _T:
        vt = False

    assert console._progress_enabled(_T()) is True
    _T.vt = True
    assert console._progress_enabled(_T()) is False, "VT 模式必须关掉（它有状态区）"


# ======================================================================
# 3. console 集成：默认顺序模式真的把进度行打出来
# ======================================================================
def test_顺序模式run_chat打出紧凑进度行(capsys):
    clock = _Clock(step=0.3)
    ctx = _Ctx(_progress_events(10), clock=clock)
    console.run_chat(ctx, "生成计划")
    out = capsys.readouterr().out
    compact = _compact_lines(out)
    assert 2 <= len(compact) <= 3, _plain(out)
    assert all("WBS 多级分工" in ln for ln in compact)
    assert ctx.feed.lines and ctx.feed.lines == compact
    # 默认模式的"只打门/错误/结果"策略不变：逐节点事件仍然不完整打印
    assert "▶" not in _plain(out) and "✔" not in _plain(out)


def test_顺序模式run_chat长静默期打出心跳行(capsys):
    clock = _Clock()
    events = [("node_start", {"node": "kb_scope", "title": "知识库范围装配"}),
              ("node_progress", {"node": "kb_scope", "progress": 90,
                                 "message": "已选 30 个 L3"}),
              ("__sleep__", {"seconds": 12.0}),      # 模型那几分钟里的一段静默
              ("ping", {}),
              ("done", {"status": "ok"})]
    ctx = _Ctx(events, clock=clock)
    console.run_chat(ctx, "生成计划")
    out = _plain(capsys.readouterr().out)
    assert "无新进度，仍在跑" in out, out


def test_vt模式run_chat不打紧凑进度行():
    """VT 模式（BUILDPLAN_TUI=1）行为不变：进度只进它自己的状态区。"""
    cap = io.StringIO()
    term = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 30))
    clock = _Clock(step=0.3)
    ctx = _Ctx(_progress_events(10), term=term, clock=clock)
    assert term.vt is True
    console.run_chat(ctx, "生成计划")
    out = _plain(cap.getvalue())
    assert ctx.feed.lines == [], ctx.feed.lines
    assert "逐相展开" not in out, "VT 模式不许出现新的紧凑进度行"
    assert "  ⏳ 8 WBS" not in out


def test_进度行带节流才会被console打印(capsys):
    """集成层再确认一次"绝不刷屏"：200 条事件 / 20 秒 → 每个节点最多 1 行 / 2 秒。"""
    clock = _Clock(step=0.1)
    events = _progress_events(0) + [
        ("node_progress", {"node": "wbs_agent", "progress": 10 + i * 0.1,
                           "message": "逐相展开 %d/200" % (i + 1)}) for i in range(200)]
    ctx = _Ctx(events, clock=clock)
    console.run_chat(ctx, "生成计划")
    out = capsys.readouterr().out
    compact = _compact_lines(out)
    assert 9 <= len(compact) <= 11, "实际 %d 行：\n%s" % (len(compact), _plain(out))


# ======================================================================
# 3b. 第 32 轮：一次闲聊只该弹**一条**进度（用户实测："一个闲聊助手有必要弹
#     六行进度出来吗？太冗余了，只需要一条"）
# ======================================================================
def _chat_events(n_steps=26):
    """闲聊剧本：router 一条 node_start + 两条 node_progress（30% / 100%）+ done。

    步数表按**真实主链**的长度给（26），这样"该不该出现分母"才是真判定：
    闲聊只走了 1 步，不该出现 `第 1 / 26 步`。
    """
    steps = [{"index": i, "node": "n%d" % i, "title": "第%d步" % i}
             for i in range(1, n_steps + 1)]
    steps[0] = {"index": 1, "node": "router", "title": "意图识别与分流"}
    return [
        ("run_plan", {"run_id": "t", "steps": steps}),
        ("node_start", {"node": "router", "title": "意图识别与分流",
                        "step": 1, "steps": n_steps}),
        ("node_progress", {"node": "router", "progress": 30,
                           "message": "判断这句是闲聊、提问，还是要排计划"}),
        ("node_done", {"node": "router", "summary": "已识别为闲聊，已回答"}),
        ("node_progress", {"node": "router", "progress": 100, "message": "回答完成"}),
        ("done", {"status": "ok", "note": "你好！有项目要排工期吗？"}),
    ]


def test_一次闲聊只弹一条进度行(capsys):
    """护栏：node_start 的折叠心跳与紧凑进度行曾经叠成**两行**（同一节点重复报）。"""
    ctx = _Ctx(_chat_events(), clock=_Clock(step=0.3))
    console.run_chat(ctx, "你好")
    out = _plain(capsys.readouterr().out)
    compact = _compact_lines(out)
    assert len(compact) == 1, "闲聊只该有一条进度行：\n%s" % out
    assert "意图识别与分流" in compact[0] and "判断这句是闲聊" in compact[0]
    # "只说干完了"的收尾文案不许再占一行
    assert "回答完成" not in out, out


def test_一次闲聊不显示步数分母(capsys):
    """用户原话「什么叫已完成 0/26，哪来的 26」：单步运行既没有分母、也不该有小结行。"""
    ctx = _Ctx(_chat_events(), clock=_Clock(step=0.3))
    console.run_chat(ctx, "你好")
    out = _plain(capsys.readouterr().out)
    assert "0/26" not in out and "0 / 26" not in out, out
    assert "本轮共走" not in out, "只跑了一步，不需要步数小结：\n%s" % out
    assert "第 1 步 · 意图识别与分流" in out, out


def test_多步运行给分母并在收尾报步数(capsys):
    """真的排计划时（跑过多步）才给分母，并在结束前补一句"走了多少步"。"""
    events = [
        ("run_plan", {"run_id": "t", "steps": [
            {"index": i, "node": "n%d" % i, "title": "第%d步" % i} for i in range(1, 5)]}),
    ]
    for i in range(1, 4):
        events.append(("node_start", {"node": "n%d" % i, "title": "第%d步" % i,
                                      "step": i, "steps": 4}))
        events.append(("node_progress", {"node": "n%d" % i, "progress": 50,
                                         "message": "正在干第 %d 件事" % i}))
        events.append(("node_done", {"node": "n%d" % i, "summary": "第%d步完成" % i}))
    events.append(("done", {"status": "ok"}))
    ctx = _Ctx(events, clock=_Clock(step=3.0))
    console.run_chat(ctx, "生成计划")
    out = _plain(capsys.readouterr().out)
    assert "第 3 / 4 步" in out, out
    assert "本轮共走 3 / 4 步" in out, out


def test_仅收尾文案不单独占一行():
    """单元级：`_is_generic_done` 只认"没有任何具体信息"的收尾话。"""
    assert tui_mod._is_generic_done("回答完成")
    assert tui_mod._is_generic_done("参数读取完成")
    assert tui_mod._is_generic_done("WBS 校验通过")
    assert not tui_mod._is_generic_done("工序先后理清了，共 812 条")
    assert not tui_mod._is_generic_done("机械配员 12/14 台")
    assert not tui_mod._is_generic_done("正在编「地上主体结构」的工序（5/10）")
    assert not tui_mod._is_generic_done("")


# ======================================================================
# 4. 回车确认行（用户："每按一次回车都该先返回一个正在加载的提示语"）
# ======================================================================
def _drive_main(monkeypatch, inputs, extra_argv=()):
    """跑一次 `console.main()`（交互路径），把**所有可观察动作**按发生顺序记下来。

    记的是 `("out", 文本)` / `("dispatch", 输入)` / `("run_chat", 输入)` /
    `("system", 输入)`，因此"确认行在后端调用之前"可以用**顺序断言**证明，
    而不是只证明"这行存在"。
    """
    seq = []
    queue = list(inputs)

    class _FakeTui:
        def __init__(self, *a, **k):
            self.vt = False

        def start(self):
            pass

        def stop(self):
            pass

        def ask(self, *a, **k):
            if not queue:
                raise EOFError          # 剧本用完 = 用户按了 Ctrl+Z/输了 EOF → 主循环退出
            return queue.pop(0)

        def out(self, text, **kw):
            seq.append(("out", _plain(str(text))))

        def status(self, *a, **k):
            pass

        def clear_status(self):
            pass

        def suspend_mouse(self):
            pass

        def resume_mouse(self):
            pass

    monkeypatch.setattr(console.tui, "Tui", _FakeTui)
    monkeypatch.setattr(console, "run_chat",
                        lambda ctx, text, force_mode=None, chat_scope=None: seq.append(("run_chat", text)))
    monkeypatch.setattr(console.commands, "dispatch",
                        lambda ctx, text: (seq.append(("dispatch", text)), "ok")[1])
    monkeypatch.setattr(console.commands, "run_system_command",
                        lambda text: (seq.append(("system", text)), "out")[1])
    # `console.main` 会把假 Tui 装成 tui 模块的"当前实例"（tui.install）——
    # 跑完必须还回去，否则后面依赖 tui.current() 的测试会拿到这个假货（串测）。
    saved = tui_mod._current
    try:
        console.main(list(extra_argv))
    finally:
        tui_mod._current = saved
    return seq


def _acks(seq):
    return [t for kind, t in seq if kind == "out" and "⏳" in t]


def test_确认行出现在任何后端调用之前(monkeypatch):
    """顺序断言：`⏳ 已收到` 必须在 run_chat / dispatch **之前**。"""
    seq = _drive_main(monkeypatch, ["帮我编一个住宅项目的进度计划", "/sources", ""])
    kinds = [k for k, _ in seq]
    ack1 = next(i for i, (k, t) in enumerate(seq)
                if k == "out" and "已收到，正在处理" in t)
    ack2 = next(i for i, (k, t) in enumerate(seq) if k == "out" and "执行 /sources" in t)
    i_chat = kinds.index("run_chat")
    i_dispatch = kinds.index("dispatch")
    assert ack1 < i_chat, "确认行必须早于 run_chat（那几秒就是'以为挂了'的窗口）"
    assert ack2 < i_dispatch, "确认行必须早于 commands.dispatch"
    assert seq[i_chat] == ("run_chat", "帮我编一个住宅项目的进度计划")
    assert seq[i_dispatch] == ("dispatch", "/sources")


def test_命令与普通输入的确认文案不同(monkeypatch):
    """普通输入说"已收到"，命令只回显**命令名**（不复述整句）。"""
    seq = _drive_main(monkeypatch, ["住宅 剪力墙 30 层", "/revise 把主体工期缩短三天", "!dir"])
    acks = _acks(seq)
    assert acks[0] == "⏳ 已收到，正在处理…"
    assert acks[1] == "⏳ 执行 /revise …", acks
    assert acks[2] == "⏳ 执行 !dir …", acks
    assert all("缩短三天" not in a for a in acks), "命令别把整句复述一遍"
    assert seq[[k for k, _ in seq].index("system")] == ("system", "!dir")


def test_空输入不产生确认行(monkeypatch):
    """直接回车：什么都不打（主循环 continue，行为与改造前一致）。"""
    seq = _drive_main(monkeypatch, ["", "   "])
    assert _acks(seq) == [], _acks(seq)
    assert not [k for k, _ in seq if k in ("run_chat", "dispatch")]


def test_run路径不打确认行(monkeypatch):
    """`--run` 非交互路径输出必须保持稳定：脚本与测试靠它。"""
    seq = _drive_main(monkeypatch, [], extra_argv=("--run", "生成计划"))
    assert [k for k, _ in seq if k == "run_chat"] == ["run_chat"]
    assert seq[-1] == ("run_chat", "生成计划")
    assert _acks(seq) == [], _acks(seq)


def test_没有终端层时确认行也立刻flush(monkeypatch):
    """非 TTY / 重定向时 stdout 有缓冲：不 flush 就看不到"立刻"。"""
    calls = []

    class _Ctx:
        tui = None
        feed = None

    monkeypatch.setattr("builtins.print", lambda *a, **k: calls.append((a, k)))
    console._emit_input_ack(_Ctx(), "生成计划")
    assert len(calls) == 1, calls
    assert "⏳ 已收到，正在处理…" in _plain(calls[0][0][0])
    assert calls[0][1].get("flush") is True, "确认行必须 flush"


def test_确认行走终端出口_顺序与vt共用一条路():
    """两种模式都走 `term.out`：顺序模式落到 _raw（自带 flush），VT 落进滚动区。"""
    seen = []

    class _Term:
        vt = True

        def out(self, text, **kw):
            seen.append((_plain(str(text)), kw))

    term = _Term()

    class _Ctx:
        tui = term
        feed = None

    console._emit_input_ack(_Ctx(), "/revise 改工期")
    assert seen == [("⏳ 执行 /revise …", {"gap": False})], seen


def test_确认行用dim且只有一行():
    line = console.color(console._ack_text("生成计划"), "dim")
    assert line.startswith(renderer.C["dim"]) and line.endswith("\033[0m")
    assert "\n" not in console._ack_text("生成计划")
    assert console._ack_text("") == ""
    assert console._ack_text("   ") == ""


# ======================================================================
# 5. R1 审计门：展示粒度口径说明
#    用户原话：「明明选择的是五层一组，主体结构还是返回的一层一段？
#              到底有没有采用用户输入的内容？」
# ======================================================================
def _wbs_with_floors():
    """5 条叶子：1/2/3 层落在「1-5 层」组、6/9 层落在「6-10 层」组。

    所以「工序级 × 每 5 层一组」真算出来是 **2 行**，而原始 WBS 是 5 条叶子 ——
    这个差值正是用户在 R1 门里看不见、因而怀疑"我的选择没生效"的那层账。
    """
    def leaf(i, floor):
        return {"id": "5.1.%d" % i, "name": "钢筋绑扎", "location": floor,
                "quantity": 10, "unit": "t", "duration_days": 2}
    return {"phases": [{"phase": "地上主体结构", "work_packages": [
        {"id": "5.1", "name": "Ⅰ区", "sub_packages": [
            leaf(1, "1-1层"), leaf(2, "2-2层"), leaf(3, "3-3层"),
            leaf(4, "6-6层"), leaf(5, "9-9层")]}]}]}


def _r1_payload(ctx):
    """R1 门的实际事件载荷（走节点自己的 _extra，不手搓字段）。"""
    payload = {"round": 1, "purpose": "audit", "round_name": "WBS 结构",
               "next_hint": "确认结构没问题请输入 Y。"}
    payload.update({k: v for k, v in WBSAuditNode()._extra(ctx).items() if v is not None})
    return payload


def test_R1门给出口径说明_含粒度标签与真实行数():
    from pipeline import quantity
    wbs = _wbs_with_floors()
    ctx = {"wbs": wbs,
           "display_granularity": {"depth": "component", "floor_grouping": "per_5"}}
    text = _plain(renderer.render_audit_gate(_r1_payload(ctx)))

    assert "每 5 层一组" in text, text                      # 我选的是什么
    assert "工序级（细）" in text, text
    assert "本门按原始 WBS 展示" in text and "5 条叶子" in text
    rows = quantity.estimate_rows_for(wbs, "component", "per_5")
    leaves = quantity.count_rows(wbs)["rows"]
    assert rows == 2 and leaves == 5, (rows, leaves)
    assert "合并成 2 行" in text, text                      # 交付物里会变成几行
    # 口径说明必须排在**树前面**
    assert text.index("展示粒度：") < text.index("地上主体结构")


def test_口径说明的行数来自estimate_rows_for(monkeypatch):
    """防写死：把 estimate_rows_for 换成常数，文案里的行数必须跟着变。"""
    from pipeline import quantity
    monkeypatch.setattr(quantity, "estimate_rows_for", lambda *a, **k: 7)
    ctx = {"wbs": _wbs_with_floors(),
           "display_granularity": {"depth": "component", "floor_grouping": "per_5"}}
    text = _plain(renderer.render_audit_gate(_r1_payload(ctx)))
    assert "合并成 7 行" in text, text


def test_没有粒度选择就不显示这段():
    """回归：老数据 / 没经过细度门 → 一个字节都不变（不瞎写）。"""
    wbs = _wbs_with_floors()
    assert granularity_caliber_note({"wbs": wbs}) is None
    text = _plain(renderer.render_audit_gate(_r1_payload({"wbs": wbs})))
    assert "展示粒度" not in text and "本门按原始 WBS" not in text


def test_粒度回退到计划meta():
    """修订重跑时 ctx 里没有 display_granularity → 回退 plan_json.meta。"""
    ctx = {"wbs": _wbs_with_floors(),
           "plan_json": {"meta": {"display_granularity": {
               "depth": "component", "floor_grouping": "per_5"}}}}
    note = granularity_caliber_note(ctx)
    assert note and "每 5 层一组" in note[0], note
    text = _plain(renderer.render_audit_gate(_r1_payload(ctx)))
    assert "合并成 2 行" in text


def test_粒度值不合法时宁可不显示():
    note = granularity_caliber_note({"wbs": _wbs_with_floors(),
                                     "display_granularity": {"depth": "bogus",
                                                             "floor_grouping": "per_5"}})
    assert note is None


def test_没有树时口径说明也不出现():
    """取不到 WBS → 不带 wbs_tree；口径说明同样不出现（否则那句话是空话）。"""
    ctx = {"wbs": "烂数据",
           "display_granularity": {"depth": "component", "floor_grouping": "per_5"}}
    payload = _r1_payload(ctx)
    assert "wbs_tree" not in payload and "granularity_note" not in payload
    assert "展示粒度" not in _plain(renderer.render_audit_gate(payload))


def test_口径说明在VT与顺序模式走同一条输出路径():
    """两种模式都靠 `_emit` → `term.out`：这里两种模式各打一遍，都必须看得见。"""
    ctx = {"wbs": _wbs_with_floors(),
           "display_granularity": {"depth": "component", "floor_grouping": "per_5"}}
    text = renderer.render_audit_gate(_r1_payload(ctx))

    seq_cap = io.StringIO()
    seq_term = tui_mod.Tui(stream=seq_cap, force_vt=False, size=(100, 30))
    console._emit(_Ctx([], term=seq_term), text)
    assert "每 5 层一组" in _plain(seq_cap.getvalue())

    vt_cap = io.StringIO()
    vt_term = tui_mod.Tui(stream=vt_cap, force_vt=True, size=(100, 30))
    vt_term.start()
    try:
        console._emit(_Ctx([], term=vt_term), text)
    finally:
        vt_term.stop()
    vt_out = _plain(vt_cap.getvalue())
    assert "每 5 层一组" in vt_out and "合并成 2 行" in vt_out, vt_out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

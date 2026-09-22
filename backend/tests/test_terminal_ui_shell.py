# -*- coding: utf-8 -*-
"""终端界面外壳的新保证（第 16 轮改造）—— 折叠 / 间距 / 配色 / 开场白 / 门实物 / TUI 安全

对应用户原话（《资料/终端界面改造规格.md》开头那段）：
  1. 「门与门之间…都没有间隔」              → 块间空行 + 满宽细线
  2. 「做成 claude 这样的输入框放到下面」    → tui 底部 3 行输入框 + 滚动区 + 退化模式
  3. 「颜色分类过多」                        → C 只映射到 4 类语义色 + dim
  4. 「WBS 门应该返回详细的 WBS 树」         → render_audit_gate 渲染 §D 冻结契约
  5. 「没有设置开场白，方框…挤在最左边」     → banner_text 按显示宽度居中
  6. 「一定需要把所有的进度都展开吗」        → FoldState / event_action 默认折叠
  7. 终端坏掉是最贵的 bug                    → start/stop 必须成对，异常也要还原

运行：python -m pytest backend/tests/test_terminal_ui_shell.py -q
"""

import re
import sys
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

_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def _plain(text):
    return _ANSI.sub("", str(text))


# ======================================================================
# 1. 配色：只保留 4 类语义色 + dim（规格 §B3）
# ======================================================================
def test_只保留四类语义色加dim():
    semantic = {renderer.C[k] for k in ("accent", "ok", "warn", "err", "dim")}
    assert renderer.C["accent"] == "\033[36m"
    assert renderer.C["ok"] == "\033[32m"
    assert renderer.C["warn"] == "\033[33m"
    assert renderer.C["err"] == "\033[31m"
    assert "90m" not in renderer.C["dim"], "亮黑(90m)太暗，浅色主题下看不见"
    # 旧键名仍在（不改爆调用方），但取值只能落在语义色 + 粗体样式 + reset 里
    allowed = semantic | {"\033[1m", "\033[0m"}
    for key, val in renderer.C.items():
        assert val in allowed, "颜色键 %s 映射到了非语义色 %r" % (key, val)
    # 花哨色一律不许出现
    for banned in ("\033[95m", "\033[96m", "\033[92m", "\033[93m", "\033[91m"):
        assert banned not in renderer.C.values(), banned


def test_灰字要看得清但仍比正文弱():
    """用户反馈：「这个灰色字体有点看不清」（浅色主题下亮黑几乎不可见）。

    层次感不能靠"更深的灰"，要靠 `\\033[2m` 半亮属性 —— 所以两条都要钉住：
      ① 不许再用 90m（亮黑）；
      ② 半亮属性必须还在，否则灰字会跟正文一样抢眼，层次就没了。
    """
    dim = renderer.C["dim"]
    assert "90m" not in dim
    assert "\033[2m" in dim, "dim 必须保留半亮属性，层次感靠它"
    assert dim.endswith("37m"), "改用浅灰 37m：黑底看得清、浅底也看得见"
    # 别名 old-key 也得跟着走，否则 /help 之类走 gray 的地方又变暗
    assert renderer.color("x", "gray") == renderer.color("x", "dim")
    assert renderer.color("x", "grey") == renderer.color("x", "dim")


def test_人工门用warn不用第五种颜色():
    assert renderer.color("x", "magenta") == renderer.color("x", "warn")
    assert renderer.color("x", "yellow") == renderer.color("x", "warn")
    assert renderer.color("x", "blue") == renderer.color("x", "accent")


# ======================================================================
# 2. 开场白：**简洁优先**（用户拍板）
# ======================================================================
def test_开场白只留一行品牌():
    """用户原话：「把学校，小组，口号，只留"海之子·建策BuildPlan"，简洁优先。」

    所以：品牌块**只有一行**；学校 / 小组 / 口号 / 英文标语**一律不许出现**。
    它们仍然保留在交付物（Word / 看板）的品牌标识里 —— 这里只管终端开场页。
    """
    text = _plain(renderer.banner_text(width=80))
    lines = [l for l in text.splitlines() if l.strip()]
    brand = lines[0].strip()
    assert brand == "海之子 · 建策 BuildPlan", brand
    # 第二行起就不该再有品牌类文字（下一段是使用提示）
    for gone in ("华南理工大学", "建智领航", "智建领航", "算得清", "改得动", "审得了",
                 "定额为据", "Compute. Revise. Audit.", "施工进度计划生成系统"):
        assert gone not in text, "开场页不该再有「%s」：\n%s" % (gone, text)


def test_开场白按显示宽度居中():
    width = 80
    text = _plain(renderer.banner_text(width=width))
    lines = [l for l in text.splitlines() if l.strip()]
    brand = lines[0]
    left = len(brand) - len(brand.lstrip(" "))
    expect = max(0, (width - renderer.visible_width(brand.strip())) // 2)
    assert left > 0, "品牌行不许挤在最左边：%r" % brand
    assert left == expect, "没有按显示宽度居中：%r（左 %d 应为 %d）" % (brand, left, expect)


def test_开场白的使用提示不能超过三行():
    """简洁优先：除品牌行与后端行外，提示不超过 3 行。"""
    text = _plain(renderer.banner_text(width=100))
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) <= 5, "开场白太长（品牌 1 + 提示 ≤3 + 后端 1）：\n%s" % text
    assert "/help" in text and "/verbose" in text and "/quit" in text
    assert "例：" in text


def test_窄终端开场白左对齐不报错():
    text = _plain(renderer.banner_text(width=56))
    assert "终端宽度 56 列" in text
    assert "\n海之子 · 建策 BuildPlan" in text, "窄屏应左对齐"


def test_后端状态在最后一行且不抢戏():
    text = renderer.banner_text(width=80, backend="云端主后端")
    tail = [l for l in _plain(text).splitlines() if l.strip()][-1]
    assert tail.startswith("后端：云端主后端")
    assert renderer.C["dim"] in text.splitlines()[-1], "后端行要用 dim"


# ======================================================================
# 3. 间距：满宽细线（§B2）
# ======================================================================
def test_细线是满宽且不含ANSI宽度误差():
    line = renderer.rule(width=40)
    assert renderer.visible_width(line) == 39
    assert _plain(line) == "─" * 39


def test_块间空行由输出层统一保证(capsys):
    cap = _Capture()
    t = tui_mod.Tui(stream=cap, force_vt=False, size=(60, 20))
    t.out("块一")
    t.out("块二")
    assert _plain(cap.value()) == "块一\n\n块二\n", repr(_plain(cap.value()))


class _Capture:
    def __init__(self):
        self.buf = []

    def write(self, s):
        self.buf.append(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def value(self):
        return "".join(self.buf)


# ======================================================================
# 4. 折叠进度（§B5）
# ======================================================================
def test_状态区最多留三个节点():
    fold = renderer.FoldState()
    fold.set_steps([{"index": i + 1, "node": "n%d" % i, "title": "节点%d" % i}
                    for i in range(6)])           # 第 32 轮：步号由后端下发
    for i in range(6):
        fold.track("node_start", {"node": "n%d" % i, "title": "节点%d" % i})
        fold.track("node_done", {"node": "n%d" % i, "summary": "完成"})
    line1, line2 = fold.lines()
    assert line1.count("第") == 3, "只该留最近 3 个节点：%s" % line1
    assert "节点5" in line1 and "节点0" not in line1
    assert "已完成 6 / 6 步" in line2, line2


def test_单步运行不显示分母():
    """用户原话：「什么叫已完成 0/26，哪来的 26」。一次闲聊只跑 1 步，就不该有分母。"""
    fold = renderer.FoldState()
    fold.set_steps([{"index": i + 1, "node": "n%d" % i, "title": "节点%d" % i}
                    for i in range(26)])
    fold.track("node_start", {"node": "n0", "title": "意图识别与分流"})
    line1, line2 = fold.lines()
    assert line1 == "⏳ 第 1 步 · 节点0", line1
    assert "26" not in line1 and "26" not in line2, (line1, line2)
    assert line2.startswith("已完成 0 步"), line2
    # 真的跑起第二步 → 分母才出现（用户这时确实需要"还有多少"）
    fold.track("node_start", {"node": "n1", "title": "确认生成计划"})
    line1, line2 = fold.lines()
    assert "第 2 / 26 步" in line1 and "已完成 0 / 26 步" in line2, (line1, line2)


def test_默认折叠逐节点_verbose才全量():
    for node_event in ("node_start", "node_progress"):
        assert renderer.event_action(node_event, {"node": "x"}, verbose=False) == "fold"
        assert renderer.event_action(node_event, {"node": "x"}, verbose=True) == "print"
    assert renderer.event_action("node_done", {"node": "x", "summary": "完成"},
                                 verbose=False) == "fold"


def test_口径类警告在折叠时也必须打印():
    data = {"node": "norm_bind", "summary": "定额锚定完成；警告 3 条"}
    assert renderer.event_action("node_done", data, verbose=False) == "print"
    assert renderer.has_warning(data)
    assert renderer.has_warning({"schedule_warnings": ["★ 未锚定 12 条"]})
    assert not renderer.has_warning({"node": "x", "summary": "完成"})


def test_门与错误与结果永远完整打印():
    for event in ("node_paused", "confirm_required", "param_review", "error",
                  "plan_final", "done"):
        assert renderer.event_action(event, {"node": "x"}, verbose=False) == "print"


def test_详细模式在状态区标注():
    fold = renderer.FoldState()
    fold.track("node_start", {"node": "a", "title": "甲"})
    assert "/verbose 关闭" in fold.lines(verbose=True)[1]
    assert "/verbose" not in fold.lines(verbose=False)[1]


# ======================================================================
# 5. 门的实物内容（§C / §D）
# ======================================================================
R1_PAYLOAD = {
    "purpose": "audit", "round": 1, "round_name": "WBS 结构",
    "wbs_tree": {
        "counts": {"phases": 10, "work_packages": 38, "leaves": 415},
        "phases": [
            {"phase": "地上主体结构", "work_packages": [
                {"id": "5.1", "name": "Ⅰ区", "leaves": [
                    {"id": "5.1.1.1", "name": "钢筋绑扎", "qty": 12.4, "unit": "t",
                     "duration_days": 3},
                    {"id": "5.1.1.2", "name": "模板安装", "qty": 486, "unit": "㎡",
                     "duration_days": 4}]}]},
            {"phase": "地下结构", "work_packages": [
                {"id": "2.1", "name": "底板", "leaves": [
                    {"id": "2.1.1.1", "name": "土方开挖", "qty": 3800, "unit": "m³",
                     "duration_days": 6}]}]}],
        "shown_leaves": 3, "truncated_leaves": 412,
    },
    "issues": [{"severity": "HIGH", "dimension": "工程量", "finding": "量级不对，请核对"}],
    "next_hint": "确认结构没问题请输入 Y。",
}


def test_R1门要展示WBS树而不是摘要():
    out = _plain(renderer.render_audit_gate(R1_PAYLOAD))
    assert "地上主体结构" in out and "地下结构" in out
    assert "5.1.1.1" in out and "钢筋绑扎" in out
    assert "12.4" in out and "t" in out and "3 天" in out
    assert "├─" in out or "└─" in out, "要是树，不是一坨"
    assert "…另有 412 条叶子未展开" in out, out
    assert "[HIGH]" in out and "1." in out


def test_R2门要展示两版对比表与最长任务():
    payload = {"purpose": "audit", "round": 2, "round_name": "两版工期",
               "schedule_compare": {
                   "theory_min": {"total_duration_days": 616, "leaves": 415,
                                  "critical": 54, "peak_labor": 70},
                   "resource_ok": {"total_duration_days": 631, "leaves": 415,
                                   "critical": 54, "peak_labor": 82},
                   "limit_note": "用户未给资源限额，两版一致",
                   "top_tasks": [{"task_id": "5.1.1.1", "task_name": "钢筋绑扎", "days": 3,
                                  "es": 14, "ef": 17, "crew": {"钢筋工": 45},
                                  "capped": False}],
                   "labor_note": "定额工日需求合计 12,969 人日"}}
    out = _plain(renderer.render_audit_gate(payload))
    assert "指标" in out and "理论最短" in out and "资源不超额" in out
    assert "616" in out and "631" in out
    assert "钢筋绑扎" in out and "14→17" in out and "钢筋工×45" in out
    assert "用户未给资源限额" in out
    assert "12,969" in out


def test_R3门要展示目录表格图与出定稿条件():
    payload = {"purpose": "audit", "round": 3, "round_name": "Word 草案（不含图表）",
               "draft_outline": {
                   "sections": [{"title": "一、总体概述", "lines": 12}],
                   "tables": [{"title": "工序明细表", "rows": 26}],
                   "figures": ["甘特图", "人员配置曲线"],
                   "coverage": "定额口径覆盖率 67.0%（278/415）",
                   "note": "草案未审计、不含图表；通过后才出定稿与看板"}}
    out = _plain(renderer.render_audit_gate(payload))
    assert "一、总体概述" in out and "12 行" in out
    assert "工序明细表" in out and "26 行" in out
    assert "甘特图" in out and "人员配置曲线" in out
    assert "67.0%" in out
    assert "通过才出定稿与看板" in out


def test_终稿确认门展示计划概览():
    out = _plain(renderer.render_event("confirm_required", {
        "message": "确认生成最终方案？",
        "plan_overview": {"project_name": "潭村办公楼", "total_duration_days": 616,
                          "planned_start_date": "2026-09-18",
                          "planned_end_date": "2028-05-25", "leaves": 415,
                          "critical": 54, "peak_labor": 70,
                          "deliver_dir": "输出结果\\计划_x"}}))
    for kw in ("潭村办公楼", "616 天", "2026-09-18", "415 条", "54 条", "70 人", "输出结果"):
        assert kw in out, kw


def test_最终结果要打全概览与交付物路径():
    """规格 §B5.2：最终结果（plan_final / 交付物路径 / 用量费用）必须完整打印。

    后端目前**没有**在 plan_final 里带 D4 的 plan_overview，所以渲染器要从
    plan_json 里已有的字段拼出概览 —— 拼不出来就会出现"最后只看到一行 plan_id"。
    """
    plan = {
        "plan_id": "plan_x",
        "overview": {"project_name": "潭村办公楼", "total_duration_days": 616,
                     "planned_start_date": "2026-09-18",
                     "planned_end_date": "2028-05-25", "critical_path_length": 54},
        "all_tasks_schedule": [{"task_id": "1"}, {"task_id": "2"}],
        "critical_path_tasks": [{"task_id": "1"}],
        "resource_plan": {"peak_manpower": 70},
    }
    out = _plain(renderer.render_event("plan_final", {
        "plan_id": "plan_x", "plan": plan, "saved_path": r"D:\plans\plan_x.json"}))
    for kw in ("潭村办公楼", "616 天", "2026-09-18 → 2028-05-25", "2 条", "1 条",
               "70 人", r"D:\plans\plan_x.json"):
        assert kw in out, (kw, out)


def test_用量费用照旧打印():
    out = _plain(renderer.render_event("done", {"status": "ok", "usage": {
        "calls": 3, "prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "cost_cny": 0.01, "by_node": {"wbs_agent": 120}}}))
    assert "150 tok" in out and "0.01" in out and "wbs_agent" in out


@pytest.mark.parametrize("payload", [
    {"purpose": "audit", "round": 1, "summary": "【第 1 轮 · WBS 结构审计】\n  阶段 10 个",
     "highlights": {"phases": 10, "leaves": 415, "per_phase": [["结构", 12, 2]],
                    "by_unit": {"m³": 100.0}, "empty_phases": ["室外"]}},
    {"purpose": "audit", "round": 2, "summary": "第 2 轮",
     "highlights": {"theory_min_days": 616, "resource_ok_days": 631,
                    "norm_coverage": {"bound_pct": 67.0, "bound": 278, "total": 415}}},
    {"purpose": "audit", "round": 3, "summary": "【第 3 轮 · 草案】\n  草案文件：d.docx"},
    {"purpose": "audit", "round": 1,
     "wbs_tree": {"counts": {}, "phases": [{"phase": None, "work_packages": [
         {"id": None, "name": None, "leaves": [
             {"id": None, "name": None, "qty": None, "unit": None, "duration_days": None}]}]}]}},
    {"purpose": "audit", "round": 2},
    {"purpose": "audit", "round": 3, "wbs_tree": "不是字典"},
])
def test_字段缺失一律优雅退化不打印None(payload):
    out = _plain(renderer.render_audit_gate(payload))
    assert out.strip(), "退化后不能什么都不显示"
    assert "None" not in out, out


# ======================================================================
# 6. TUI：底部输入框 / 退化模式 / 屏幕还原（§B1）
# ======================================================================
def test_版式把输入框钉在底部三行():
    t = tui_mod.Tui(stream=_Capture(), force_vt=True, size=(80, 24))
    assert t.vt is True
    assert t.box_top == 22 and t.box_top + 2 == 24, "输入框必须是底部 3 行"
    assert t.status_top == 20, "状态区在输入框上方"
    assert t.region_bottom == 19, "滚动区结束在状态区之上，输出不会盖住它们"


def test_进入备用屏并设置滚动区():
    cap = _Capture()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24))
    t.start()
    out = cap.value()
    assert "\033[?1049h" in out, "必须进备用屏"
    assert "\033[?25l" in out, "必须隐藏光标"
    assert "\033[1;19r" in out, "必须按实际高度设滚动区"
    t.stop()


def test_退出时必须还原屏幕且可重入():
    cap = _Capture()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24))
    t.start()
    t.stop()
    first = cap.value()
    assert "\033[?25h" in first and "\033[?1049l" in first, "必须显示光标并退出备用屏"
    assert "\033[r" in first, "必须复位滚动区，否则退出后区域残留"
    t.stop()                                   # 再来一次不许重复写
    assert cap.value() == first, "stop() 必须可重入"


def test_退化模式不发任何定位码():
    cap = _Capture()
    t = tui_mod.Tui(stream=cap, force_vt=False, size=(80, 24))
    t.start()
    t.out("你好")
    t.stop()
    out = cap.value()
    assert "\033[?1049" not in out and "\033[?25" not in out
    assert "你好" in out


def test_非TTY自动退化():
    t = tui_mod.Tui(stream=_Capture(), size=(80, 24))     # isatty()=False
    assert t.vt is False


def test_交互读取走内建input所以测试与脚本都能喂(monkeypatch):
    cap = _Capture()
    t = tui_mod.Tui(stream=cap, force_vt=False, size=(60, 20))
    monkeypatch.setattr("builtins.input", lambda prompt="": "你好")
    assert t.ask("你 ▸ ") == "你好"
    assert _plain(cap.value()).strip().startswith("─"), "退化模式要有满宽细线"


def test_底部输入框在VT模式下同一次交互里落地():
    cap = _Capture()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24),
                    input_fn=lambda prompt="": "Y")
    t.start()
    t.ask("  [Y/n] ")
    out = cap.value()
    assert "╭" in out and "╰" in out, "输入框要有边框"
    assert "\033[22;1H" in out, "输入框顶边要写在 rows-2 行（绝对定位）"
    assert "你 ▸ Y" in _plain(out), "读到的内容要回显到滚动区"
    t.stop()


def test_ANSI不占显示宽度():
    colored = renderer.color("中文abc", "accent")
    assert renderer.visible_width(colored) == 7
    fixed = renderer._fit(colored, 10)
    assert renderer.visible_width(fixed) == 10
    wrapped = tui_mod._hard_wrap(renderer.rule(width=20), 19)
    assert len(wrapped) == 1, "带色细线不许因为 ANSI 被折成两行"


# ======================================================================
# 7. console：折叠落位 + 异常也要还原屏幕
# ======================================================================
class _FakeSSE:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def pause(self):
        pass

    def resume(self):
        pass

    def post_chat(self, prompt, run_id=None, mode=None, chat_scope=None):
        return iter(self.events)

    def post_cancel(self, run_id):
        self.calls.append(("cancel", run_id))


class _Ctx:
    def __init__(self, events, term=None):
        self.client = _FakeSSE(events)
        self.history = []
        self.current_plan = None
        self.current_plan_id = None
        self.running = False
        self.run_id = None
        self.verbose = False
        self.fold = renderer.FoldState()
        self.tui = term


def test_默认折叠时逐节点事件不打印_门照打印(capsys):
    events = [("node_start", {"node": "a", "title": "甲"}),
              ("node_progress", {"node": "a", "progress": 50, "message": "跑"}),
              ("node_done", {"node": "a", "summary": "完成"}),
              ("confirm_required", {"confirm_id": "c1", "message": "继续？"}),
              ("done", {"status": "ok"})]
    ctx = _Ctx(events)
    import confirmer
    orig = confirmer.ask_confirm
    confirmer.ask_confirm = lambda *a, **k: True
    try:
        console.run_chat(ctx, "测试")
    finally:
        confirmer.ask_confirm = orig
    out = _plain(capsys.readouterr().out)
    assert "节点启动" not in out, "默认不该堆出逐节点进度：\n%s" % out
    assert "▶" not in out and "✔" not in out
    assert "继续？" in out, "门必须完整打印"
    assert "流程结束" in out


def test_verbose打开后逐节点全量打印(capsys):
    events = [("node_start", {"node": "a", "title": "甲"}),
              ("node_done", {"node": "a", "summary": "完成"}),
              ("done", {"status": "ok"})]
    ctx = _Ctx(events)
    ctx.verbose = True
    console.run_chat(ctx, "测试")
    out = _plain(capsys.readouterr().out)
    assert "▶ 节点启动：甲" in out and "✔ a：完成" in out


def test_verbose开关由外壳处理(capsys):
    ctx = _Ctx([])
    assert console._handle_local(ctx, "/verbose") is True
    assert ctx.verbose is True
    console._handle_local(ctx, "/verbose off")
    assert ctx.verbose is False
    assert console._handle_local(ctx, "/help") is False, "其它命令仍交给 commands.py"
    assert "详细模式" in _plain(capsys.readouterr().out)


def test_折叠不丢信息_verbose能回看刚才折叠的事件(capsys):
    ctx = _Ctx([("node_start", {"node": "a", "title": "甲"}),
                ("node_progress", {"node": "a", "progress": 40, "message": "跑"}),
                ("node_done", {"node": "a", "summary": "完成"}),
                ("done", {"status": "ok"})])
    console.run_chat(ctx, "测试")
    capsys.readouterr()
    assert len(ctx.event_log) == 3, ctx.event_log
    console._handle_local(ctx, "/verbose")
    out = _plain(capsys.readouterr().out)
    assert "被折叠掉的事件" in out, out
    assert "节点启动 a（甲）" in out, out


def test_main异常退出也要还原屏幕(monkeypatch):
    """最贵的 bug：异常路径没退出备用屏 → 用户终端坏掉。"""
    stopped = []

    class _FakeTui:
        def __init__(self, *a, **k):
            self.vt = False

        def start(self):
            pass

        def stop(self):
            stopped.append(True)

        def out(self, *a, **k):
            pass

        def status(self, *a, **k):
            pass

        def clear_status(self):
            pass

        def ask(self, *a, **k):
            return "/quit"

    class _Boom:
        def __init__(self, *a, **k):
            self.base_url = "http://boom"

        def post_chat(self, prompt, run_id=None, mode=None, chat_scope=None):
            raise RuntimeError("后端炸了")

    monkeypatch.setattr(console.tui, "Tui", _FakeTui)
    monkeypatch.setattr(console, "SSEClient", _Boom)

    with pytest.raises(RuntimeError):
        console.main(["--run", "会炸"])
    assert stopped == [True], "异常路径必须调用 stop() 还原屏幕"


# ======================================================================
# 8. 开场白必须**真的被打出来**（用户实测"整屏空白"的回归）
# ======================================================================
def test_main必须真的把开场白打出来(monkeypatch, capsys):
    """用户实测：进入界面后**满屏空白、什么都看不到**。

    根因（真实缺陷）：`console.main` 里
        term.start()      # 进入备用屏（退化模式为空操作）        _emit(ctx, renderer.banner_text(...))
    两条语句被挤在同一行，`_emit(...)` 落在行尾注释之后 → **整条被注释吞掉**，
    开场白从不打印；VT 模式下备用屏刚被清空，于是满屏空白。

    教训：`renderer.banner_text()` 单测通过**不等于**它被调用过。
    这条测试从 `main()` 入口断言"开场白真的出现在输出里"。
    """
    monkeypatch.setattr(console, "run_chat", lambda ctx, text, **kw: None)   # 不碰网络
    monkeypatch.setattr(sys, "argv", ["console.py", "--run", "你好"])
    console.main(["--run", "你好"])
    out = capsys.readouterr().out
    assert "海之子 · 建策 BuildPlan" in out, "开场白没被打出来：\n%r" % out[:400]
    assert "/help" in out, "开场白里缺使用提示：\n%r" % out[:400]
    assert "后端" in out, out[:400]


def test_main必须调用banner_text(monkeypatch):
    """旧 bug 的直接断言：`_emit(...)` 被行尾注释吞掉时，`banner_text` 根本不会被调用。"""
    calls = []

    def _spy(**kw):
        calls.append(kw)
        return "BANNER"

    monkeypatch.setattr(console, "run_chat", lambda ctx, text, **kw: None)
    monkeypatch.setattr(renderer, "banner_text", _spy)
    console.main(["--run", "你好"])
    assert calls, "main() 没有调用 renderer.banner_text —— 开场白又被吞了"


def test_VT模式必须真的把内容写进滚动区():
    """VT 路径同样要证明"内容真的写出去了"，而不是只测排版常量。"""
    import io as _io

    stream = _io.StringIO()
    t = tui_mod.Tui(stream=stream, force_vt=True, size=(100, 30))
    assert t.vt is True, t.degrade_reason
    t.start()
    t.out("门的内容：WBS 结构审计")
    t.stop()
    buf = stream.getvalue()
    assert "门的内容：WBS 结构审计" in buf, "内容没写进输出流：\n%r" % buf[:300]
    assert "\033[1;1H" in buf, "没定位到滚动区第一行"
    assert "\033[?1049h" in buf and "\033[?1049l" in buf, "没进出备用屏"
    assert "\033[?25h" in buf, "退出时没还原光标"


def test_输入框画完光标必须回到中间那行():
    """用户实测"什么都输入不了"的第二个根因。

    `_draw_box` 最后写的是**下边框**，写完后光标就停在那一行；若不放回中间行，
    用户打字会打在边框线上、看起来像"输入不了"。这里断言最后一条定位指令指向
    `box_top + 1`（中间那行）而不是 `box_top + 2`。
    """
    import io as _io

    stream = _io.StringIO()
    t = tui_mod.Tui(stream=stream, force_vt=True, size=(100, 30))
    t._draw_box("你 ▸ ")
    buf = stream.getvalue()
    tail = _ANSI.findall(buf)[-1] if _ANSI.findall(buf) else ""
    row = int(re.findall(r"\033\[(\d+);", buf)[-1])
    assert row == t.box_top + 1, (
        "画完输入框后光标停在第 %d 行，应为中间那行 %d（否则打字打在边框上）"
        % (row, t.box_top + 1))
    assert tail, buf[-80:]


def test_终端改大小后要重算布局与滚动区():
    """拖一下窗口就错位，也是"界面坏了"的常见来源 —— 必须重算布局与滚动区。"""
    import io as _io

    stream = _io.StringIO()
    t = tui_mod.Tui(stream=stream, force_vt=True, size=(100, 30))
    t.start()
    stream.truncate(0)
    stream.seek(0)
    t._size = (80, 20)                  # 模拟用户把窗口拖小
    t.out("内容")
    buf = stream.getvalue()
    assert (t.cols, t.rows) == (80, 20), (t.cols, t.rows)
    assert t.box_top == 18, t.box_top
    assert "\033[1;15r" in buf, "没按新行数重设滚动区（20-5=15）：\n%r" % buf[:200]

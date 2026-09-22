# -*- coding: utf-8 -*-
"""终端"应用内滚动"的新保证（第 22 轮）—— 滚轮/键盘上翻历史

对应用户原话：《资料/终端上翻历史规格.md》开头那句「滚轮上滑无法查看历史聊天记录」。

根因（规格里已确认，这里只是复述）：VT 模式为了把输入框钉在底部用了
**备用屏 `?1049h` + 滚动区 `ESC[1;{n}r`**，终端**自带的回滚缓冲里什么都没有**，
所以鼠标滚轮翻的是空白 —— 只能照 Claude Code 的做法在应用内自己实现滚动。

这组测试刻意**不碰真键盘、不碰真终端**：
  · 按键 → 纯函数 `_parse_key()`，直接喂字节序列（Windows 双字节扫描码 / VT 序列 /
    SGR 与 X10 两种鼠标编码 / UTF-8 中文）；
  · 视图 → `_history` + `_offset` → `_viewport()`，直接断言切片；
  · 重画 → 假终端 `StringIO`，断言写出的控制码与内容；
  · 兜底 → monkeypatch 掉 msvcrt / 喂注入的按键源，断言仍然能 `ask()`；
  · 还原 → start()/stop() 必须成对写出鼠标上报的 h/l、显光标、退备用屏、复位滚动区。

运行：python -m pytest backend/tests/test_tui_scrollback.py -q
"""

import io
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

import tui as tui_mod  # noqa: E402

_ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def _plain(text):
    return _ANSI.sub("", str(text))


def _vt(lines=0, size=(80, 24), **kw):
    """造一个 VT 模式的 Tui，可选先灌 `lines` 行历史（`_write_region` 是唯一入口）。"""
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=size, **kw)
    t.start()
    for i in range(lines):
        t._write_region("L%03d" % i)
    return t, cap


def _keys(*items):
    """按键源：把若干"按键"按顺序喂给 `_read_line_raw`/`pump`，喂完就报没有按键。"""
    queue = list(items)

    def _fn():
        return queue.pop(0) if queue else b""

    def _peek():
        return bool(queue)

    return _fn, _peek


@pytest.fixture(autouse=True)
def 假控制台(monkeypatch):
    """pytest 里 stdin 不是控制台，`SetConsoleMode` 必然失败。

    这里把"打开/还原 VT 输入模式"换掉：（1）让鼠标上报这条路真的走通，
    （2）记录"还原输入模式"有没有被调用 —— 退出必须还原这一条要有测试守住。
    """
    restored = []
    monkeypatch.setattr(tui_mod, "_vt_input_mode", lambda: 0x001F)      # 假装原模式
    monkeypatch.setattr(tui_mod, "_restore_input_mode", lambda old: restored.append(old))
    return restored


# ======================================================================
# 1. 按键解析（纯函数：直接喂字节序列，不需要真键盘）
# ======================================================================
@pytest.mark.parametrize("seq,expect", [
    # Windows 双字节扫描码（\x00 / \xe0 前缀）
    (b"\xe0H", ("up", None)), (b"\x00H", ("up", None)),
    (b"\xe0P", ("down", None)), (b"\x00P", ("down", None)),
    (b"\xe0I", ("pgup", None)), (b"\x00I", ("pgup", None)),
    (b"\xe0Q", ("pgdn", None)), (b"\x00Q", ("pgdn", None)),
    (b"\xe0G", ("home", None)), (b"\x00G", ("home", None)),
    (b"\xe0O", ("end", None)), (b"\x00O", ("end", None)),
    # VT 序列（开了 ENABLE_VIRTUAL_TERMINAL_INPUT 之后走这一套）
    (b"\x1b[A", ("up", None)), (b"\x1b[B", ("down", None)),
    (b"\x1b[5~", ("pgup", None)), (b"\x1b[6~", ("pgdn", None)),
    (b"\x1b[H", ("home", None)), (b"\x1b[1~", ("home", None)), (b"\x1b[7~", ("home", None)),
    (b"\x1b[F", ("end", None)), (b"\x1b[4~", ("end", None)), (b"\x1b[8~", ("end", None)),
    (b"\x1bOA", ("up", None)), (b"\x1bOB", ("down", None)),
    (b"\x1bOH", ("home", None)), (b"\x1bOF", ("end", None)),
    # 带修饰键的参数也要认（Ctrl+Up）
    (b"\x1b[1;5A", ("up", None)),
])
def test_按键解析_上翻下翻的每一种编码(seq, expect):
    assert tui_mod._parse_key(seq) == expect


@pytest.mark.parametrize("seq,expect", [
    (b"\x1b[<64;10;5M", ("wheel", -1)),          # SGR 扩展：上滑
    (b"\x1b[<65;10;5M", ("wheel", 1)),           # SGR 扩展：下滑
    (b"\x1b[<64;1;1m", ("wheel", -1)),           # 有的终端发小写 m
    (b"\x1b[<64;120;40M", ("wheel", -1)),
    (b"\x1b[M`\x21\x21", ("wheel", -1)),         # X10 兜底：0x60 上滑
    (b"\x1b[Ma\x21\x21", ("wheel", 1)),          # X10 兜底：0x61 下滑
    ("\x1b[<64;10;5M", ("wheel", -1)),           # 宽字符源（getwch）给的是 str
])
def test_按键解析_滚轮上下滑(seq, expect):
    assert tui_mod._parse_key(seq) == expect


@pytest.mark.parametrize("seq", [
    b"\x1b[<0;10;5M",      # 左键按下
    b"\x1b[<32;10;5M",     # 左键拖动（1002h 才会有；我们没开，但别解析错）
    b"\x1b[M \x21\x21",    # X10 左键按下
])
def test_按键解析_鼠标点击不算滚动(seq):
    kind, _val = tui_mod._parse_key(seq)
    assert kind == "unknown", (_plain(seq), kind)


def test_按键解析_字符与行编辑键():
    assert tui_mod._parse_key("a") == ("char", "a")
    assert tui_mod._parse_key("你") == ("char", "你")
    assert tui_mod._parse_key("你".encode("utf-8")) == ("char", "你")
    assert tui_mod._parse_key("😀".encode("utf-8")) == ("char", "😀")
    assert tui_mod._parse_key(b"\x08") == ("backspace", None)
    assert tui_mod._parse_key(b"\x7f") == ("backspace", None)
    assert tui_mod._parse_key(b"\x0d") == ("enter", None)
    assert tui_mod._parse_key(b"\x0a") == ("enter", None)
    assert tui_mod._parse_key(b"\x03") == ("ctrl_c", None)
    assert tui_mod._parse_key(b"\x15") == ("ctrl_u", None)


def test_按键解析_半截序列不算按键():
    """`final=False`（还要等后续字节）时必须回 `(None, None)`，否则会把半个序列当按键。"""
    for half in (b"\x1b", b"\x1b[", b"\x1b[<64;10", b"\x1b[<64;10;5", b"\xe0",
                 b"\x00", b"\xe4", b"\xe4\xbd", b"\x1b[M", b"\x1b[M`"):
        assert tui_mod._parse_one(half, final=False)[0] is None, _plain(half)
    # 但"确定没有后续字节了"时，孤零零的 ESC 就是一个 ESC 键（不是死等）
    assert tui_mod._parse_key(b"\x1b") == ("esc", None)
    assert tui_mod._parse_key(b"\x1b[") == ("unknown", None)


def test_拼包函数带超时不死等():
    """§3.4 最后一条：读不到后续字节就返回，绝不能把行输入卡死。"""
    calls = []

    def _getch():
        calls.append(True)
        return b"\x1b"

    key = tui_mod._collect_key(_getch, lambda: False, lambda _s: None, timeout=0.0)
    assert key == "\x1b", repr(key)
    assert len(calls) == 1, "超时后不许再读"


def test_拼包函数会把双字节扫描码拼起来():
    got = [b"\xe0", b"H"]

    def _getch():
        return got.pop(0)

    def _kbhit():
        return bool(got)

    key = tui_mod._collect_key(_getch, _kbhit, lambda _s: None, timeout=0.05)
    assert tui_mod._parse_key(key) == ("up", None), repr(key)

    got2 = [b"\x1b", b"[", b"<", b"6", b"5", b";", b"3", b";", b"9", b"M"]

    def _getch2():
        return got2.pop(0)

    key2 = tui_mod._collect_key(_getch2, lambda: bool(got2), lambda _s: None, timeout=0.05)
    assert tui_mod._parse_key(key2) == ("wheel", 1), repr(key2)


# ======================================================================
# 2. 视图窗口（`_history` + `_offset` → `_viewport()`）
# ======================================================================
def test_视图窗口按offset切片():
    t, _cap = _vt(lines=100)
    n = t._region_height()
    assert n == 19, n                       # 24 行 - 3 输入框 - 2 状态区
    assert t._viewport() == ["L%03d" % i for i in range(100 - n, 100)], "offset=0 = 跟随最新"
    t._offset = 3
    assert t._viewport() == ["L%03d" % i for i in range(100 - n - 3, 100 - 3)]
    assert len(t._viewport()) == n, "窗口永远是满的"


def test_offset越界要钳位():
    t, _cap = _vt(lines=100)
    n = t._region_height()
    assert t._clamp_offset(-5) == 0, "不能负"
    assert t._clamp_offset(9999) == 100 - n, "不能越界（最多翻到最旧一行顶到窗口最上面）"
    t._offset = -5
    assert t._eff_offset() == 0
    assert t._viewport()[-1] == "L099"
    t.scroll("home")
    assert t._offset == 100 - n
    assert t._viewport()[0] == "L000", "Home 要跳到最旧"
    t.scroll("up")                          # 已经在最旧：保持不动
    assert t._offset == 100 - n
    t.scroll("end")
    t.scroll("down")                        # 已经在最新：不能翻成负数
    assert t._offset == 0


def test_历史条数不足一屏时不许上翻():
    t, _cap = _vt(lines=5)
    assert t._max_offset() == 0
    t.scroll("wheel", -1)
    assert t._offset == 0
    assert t._viewport() == ["L000", "L001", "L002", "L003", "L004"]


def test_历史缓冲有上限但不丢总行数():
    t, _cap = _vt(lines=0)
    for i in range(tui_mod.HIST_LIMIT + 100):
        t._write_region("H%04d" % i)
    assert len(t._history) == tui_mod.HIST_LIMIT, "deque 必须封顶"
    assert t._hist_total == tui_mod.HIST_LIMIT + 100
    t.scroll("home")
    assert t._offset == tui_mod.HIST_LIMIT - t._region_height()
    assert t._viewport()[0] == "H0100", "翻到最旧也只能翻到还留着的最旧一行"


def test_折行后的每一行都进历史():
    t, _cap = _vt(size=(40, 24))
    t._write_region("x" * 200)
    assert len(t._history) > 1, "太长的行必须按显示宽度折开再记"
    assert all(len(ln) <= 39 for ln in t._history), [len(x) for x in t._history]


# ======================================================================
# 3. 跟随语义（offset=0 跟随；offset>0 钉死；End 恢复跟随）
# ======================================================================
def test_跟随最新时新输出会更新视图():
    t, _cap = _vt(lines=100)
    t._write_region("最新一行")
    assert t._viewport()[-1] == "最新一行"


def test_正在看历史时新输出不改变视图():
    t, _cap = _vt(lines=100)
    t.scroll("wheel", -1)
    before = t._viewport()
    assert t._offset == 3
    t._write_region("新输出不该顶走视图")
    assert t._offset == 4, "offset 要跟着总行数一起涨，视图绝对位置才钉得住"
    assert t._viewport() == before, "正在看历史时被新输出顶走 = 用户刚翻到的地方没了"
    # 连开三行也不许动
    for i in range(3):
        t._write_region("又一行 %d" % i)
    assert t._viewport() == before


def test_End之后恢复跟随():
    t, _cap = _vt(lines=100)
    t.scroll("pgup")
    assert t._offset > 0
    t.scroll("end")
    assert t._offset == 0
    t._write_region("回到最新之后的新行")
    assert t._viewport()[-1] == "回到最新之后的新行", "End 之后必须重新跟随"


def test_滚动步长_滚轮3行_方向键1行_PgUp一屏():
    t, _cap = _vt(lines=200)
    n = t._region_height()
    t.scroll("wheel", -1)
    assert t._offset == tui_mod.WHEEL_STEP == 3
    t.scroll("wheel", 1)
    assert t._offset == 0
    t.scroll("up")
    assert t._offset == 1
    t.scroll("down")
    assert t._offset == 0
    t.scroll("pgup")
    assert t._offset == n - 1, "PgUp 一次一屏（滚动区高度 - 1）"
    t.scroll("pgdn")
    assert t._offset == 0
    t.scroll("pgup")
    t.scroll("wheel", -1)                   # 上滑继续往外翻
    assert t._offset == (n - 1) + 3


# ======================================================================
# 4. 重画（StringIO 断言写出的内容）
# ======================================================================
def test_重画内容就是当前视图窗口():
    t, cap = _vt(lines=100)
    t.scroll("wheel", -1)                   # offset=3
    cap.truncate(0)
    cap.seek(0)
    t._repaint_region()
    out = cap.getvalue()
    want = ["L%03d" % i for i in range(100 - 19 - 3, 100 - 3)]
    for i, line in enumerate(want):
        row = t.region_top + i
        assert ("\033[%d;1H" % row) + "\033[K" + line in out, "第 %d 行没按视图画" % row
        assert line in out
    assert "L099" not in out and "L000" not in out, "重画的是窗口，不是全部历史"
    assert re.findall(r"\033\[(\d+);1H", out) == [str(i) for i in range(1, 20)], out[:200]


def test_重画把窗口以外的行清空():
    """历史不足一屏时，剩下的行必须被 `ESC[K` 擦掉，否则会留着上一屏的残影。"""
    t, cap = _vt(size=(80, 24))
    t._write_region("只有一行")
    cap.truncate(0)
    cap.seek(0)
    t._repaint_region()
    out = cap.getvalue()
    assert out.count("\033[K") == 19, out
    assert _plain(out).strip() == "只有一行", repr(_plain(out))


def test_上翻后状态区出现可见提示_End后消失():
    t, cap = _vt(lines=100)
    t.status("⏳ 12 定额锚定 · 13 机械配员与工作面", "已完成 13/26")
    row2 = t.status_top + 1
    assert "\033[%d;1H" % row2 + "\033[K" + "已完成 13/26" in cap.getvalue()
    cap.truncate(0)
    cap.seek(0)
    t.scroll("wheel", -1)
    out = cap.getvalue()
    assert "已上翻 3 行（共 100 行）· End 回到最新" in _plain(out), _plain(out)
    assert "⏳ 12 定额锚定" in _plain(out), "第 1 行仍要显示进度"
    cap.truncate(0)
    cap.seek(0)
    t.scroll("end")
    out = cap.getvalue()
    assert "已上翻" not in _plain(out), _plain(out)
    assert "已完成 13/26" in _plain(out), "回到最新后要还原成原来的第 2 行"


# ======================================================================
# 5. 原始行编辑（Enter / Backspace / Ctrl+U / Ctrl+C / 中文回显）
# ======================================================================
def test_原始按键能编辑并回车提交():
    fn, peek = _keys("你", "好", b"\x08", "啊", b"\r")
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24), key_fn=fn, key_peek_fn=peek)
    assert t.raw_key is True
    ans = t.ask("你 ▸ ")
    assert ans == "你啊", ans                    # 你好 → 退格 → 你啊
    text = _plain(cap.getvalue())
    assert "你 ▸ 你啊" in text, "读到的内容要回显到滚动区"
    assert "你啊" in text


def test_Ctrl_U清行_ESC不污染输入():
    fn, peek = _keys("a", b"b", "\x1b", b"\x15", "c", b"\r")
    t, _cap = _vt(key_fn=fn, key_peek_fn=peek)
    assert t.ask("你 ▸ ") == "c"


def test_输入框要跟着每一个按键重画():
    fn, peek = _keys("a", "b", b"\r")
    t, cap = _vt(key_fn=fn, key_peek_fn=peek)
    t.ask("你 ▸ ")
    out = _plain(cap.getvalue())
    row = t.box_top + 1
    assert ("│ 你 ▸ a" in out) and ("│ 你 ▸ ab" in out), out


def test_Ctrl_C当KeyboardInterrupt交给上层():
    fn, peek = _keys("a", b"\x03")
    t, cap = _vt(key_fn=fn, key_peek_fn=peek)
    with pytest.raises(KeyboardInterrupt):
        t.ask("你 ▸ ")
    assert "\033[%d;1H" % t.box_top in cap.getvalue(), "中断也要把输入框收回去"
    t.stop()


# ======================================================================
# 6. 输出期间顺手响应滚轮（打字不丢）
# ======================================================================
def test_输出期间滚轮照样能上翻_且打字不丢():
    fn, peek = _keys(b"\x1b[<64;10;5M", "x", "y", b"\r")
    t, _cap = _vt(lines=100, key_fn=fn, key_peek_fn=peek)
    base = list(t._history)
    t.out("运行中又来了一行输出")
    # offset：滚轮 3 行 + 新输出 1 行（offset 跟着总行数涨，视图绝对位置才钉得住）
    assert t._offset == 3 + 1, "输出间隙的滚轮事件必须被处理"
    assert t._viewport() == base[100 - 19 - 3:100 - 3], "视图要正好停在「往上翻 3 行」处"
    assert "运行中又来了一行输出" not in t._viewport(), "新输出不许顶进正在回看的窗口"
    assert t.ask("你 ▸ ") == "xy", "输出期间打的字要原样交回行输入，不能丢"


def test_pump是纯非阻塞的_没有按键就直接返回():
    t, _cap = _vt(lines=10)                 # 没有按键源
    assert t.pump() == 0
    fn, peek = _keys()
    t2, _cap2 = _vt(lines=10, key_fn=fn, key_peek_fn=peek)
    assert t2.pump() == 0, "没按键立刻返回，绝不死等"


def test_按键源坏掉时静默关闭而不是卡住():
    def _boom():
        raise RuntimeError("kbhit/getch 坏了")

    t, _cap = _vt(lines=10, key_fn=_boom, key_peek_fn=lambda: True)
    assert t.pump() == 0
    assert t.raw_key is False, "坏掉之后必须彻底关掉原始按键"


# ======================================================================
# 7. 还原：start()→stop() 必须成对（上一轮"整屏空白"事故的回归）
# ======================================================================
def test_进入与退出必须成对开关鼠标上报():
    fn, peek = _keys()
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24), key_fn=fn, key_peek_fn=peek)
    t.start()
    on = cap.getvalue()
    assert "\033[?1000h" in on and "\033[?1006h" in on, "进备用屏就要开鼠标上报"
    assert "\033[?1049h" in on and "\033[?25l" in on and "\033[1;19r" in on
    cap.truncate(0)
    cap.seek(0)
    t.stop()
    off = cap.getvalue()
    assert "\033[?1006l" in off and "\033[?1000l" in off, "退出必须关掉鼠标上报"
    assert "\033[?1000h" not in off, "stop 里不许再开鼠标上报"
    assert "\033[r" in off, "必须复位滚动区"
    assert "\033[?25h" in off, "必须显光标"
    assert "\033[?1049l" in off, "必须退出备用屏"


def test_异常路径也必须还原完整():
    """Ctrl+C 打断读输入 → 仍然要把鼠标上报/光标/备用屏/滚动区全部还原。"""
    fn, peek = _keys(b"\x03")
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24), key_fn=fn, key_peek_fn=peek)
    t.start()
    with pytest.raises(KeyboardInterrupt):
        t.ask("你 ▸ ")
    cap.truncate(0)
    cap.seek(0)
    t.stop()
    out = cap.getvalue()
    for code in ("\033[?1006l", "\033[?1000l", "\033[r", "\033[?25h", "\033[?1049l"):
        assert code in out, (code, out.encode("unicode_escape"))
    assert out.count("\033[?1000h") == 0, "还原时不许再开鼠标上报"
    # 再 stop 一次不许重复写（可重入）
    cap.truncate(0)
    cap.seek(0)
    t.stop()
    assert cap.getvalue() == "", "stop() 必须可重入"


def test_退出必须还原控制台输入模式(假控制台):
    """开过 ENABLE_VIRTUAL_TERMINAL_INPUT 就必须还原原值，否则用户的终端输入行为变了。"""
    fn, peek = _keys()
    t, _cap = _vt(key_fn=fn, key_peek_fn=peek)
    assert 假控制台 == [], "还没退出，先别还原"
    t.stop()
    assert 假控制台 == [0x001F], "退出必须把控制台输入模式还原成原值"
    t.stop()
    assert 假控制台 == [0x001F], "stop() 可重入，不许重复还原"


def test_原始按键坏掉之后不再写鼠标开关():
    """降级（退回 input()）时鼠标上报必须跟着关掉，否则序列会灌进 input() 的行里。"""
    def _boom():
        raise RuntimeError("坏了")

    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24), key_fn=_boom,
                    key_peek_fn=lambda: True)
    t.start()
    assert "\033[?1006h" in cap.getvalue()
    cap.truncate(0)
    cap.seek(0)
    t.pump()                                  # 触发降级
    assert "\033[?1006l" in cap.getvalue(), "降级必须立刻关鼠标上报"
    assert t.raw_key is False


def test_临时挂起与恢复鼠标上报():
    """`commands.py` 里还有内建 input()：那段时间必须把鼠标上报挂起。"""
    fn, peek = _keys()
    t, cap = _vt(key_fn=fn, key_peek_fn=peek)
    assert t.suspend_mouse() is True
    assert "\033[?1006l" in cap.getvalue()
    cap.truncate(0)
    cap.seek(0)
    t.resume_mouse()
    assert "\033[?1006h" in cap.getvalue()
    assert t.resume_mouse() is None or True   # 幂等，不抛
    t.suspend_mouse()
    cap.truncate(0)
    cap.seek(0)
    t.stop()
    assert "\033[?1006l" not in cap.getvalue(), "已经关过就不许重复写"


# ======================================================================
# 8. 兜底：任何一条不满足 → 完全走原来的 input()
# ======================================================================
def test_注入input_fn时不开原始按键():
    t, _cap = _vt(input_fn=lambda prompt="": "Y")
    assert t.raw_key is False, "注入了 input_fn 说明调用方自己喂输入，别去抢键盘"


def test_非TTY时不开原始按键(monkeypatch):
    monkeypatch.setattr(tui_mod, "_stdin_isatty", lambda: False)
    monkeypatch.setattr(tui_mod, "_kbd_available", lambda: True)
    t = tui_mod.Tui(stream=io.StringIO(), force_vt=True, size=(80, 24))
    assert t.raw_key is False


def test_msvcrt不可用时仍走input(monkeypatch):
    monkeypatch.setattr(tui_mod, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(tui_mod, "_kbd_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "兜底输入")
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24))
    assert t.raw_key is False
    t.start()
    assert "\033[?1000h" not in cap.getvalue(), "没有原始按键就别开鼠标上报"
    assert t.ask("你 ▸ ") == "兜底输入"
    t.stop()
    assert "\033[?1006l" not in cap.getvalue()


def test_msvcrt真的不可用时也走input(monkeypatch):
    """把 `msvcrt` 从 `sys.modules` 里挖掉（`import msvcrt` → ImportError）后照样能用。"""
    monkeypatch.setitem(sys.modules, "msvcrt", None)     # None = 这个模块"不存在"
    monkeypatch.setattr(tui_mod, "_stdin_isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "没有 msvcrt 也能用")
    assert tui_mod._kbd_available() is False
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=True, size=(80, 24))
    assert t.raw_key is False
    assert t.ask("你 ▸ ") == "没有 msvcrt 也能用"
    assert "\033[?1000" not in cap.getvalue()


def test_环境变量可以关掉新功能(monkeypatch):
    monkeypatch.setenv("BUILDPLAN_KBD", "0")
    assert tui_mod._kbd_available() is False
    monkeypatch.delenv("BUILDPLAN_KBD")
    monkeypatch.setenv("BUILDPLAN_MOUSE", "0")
    assert tui_mod._mouse_enabled() is False
    fn, peek = _keys()
    t, cap = _vt(key_fn=fn, key_peek_fn=peek)
    t.suspend_mouse()
    cap.truncate(0)
    cap.seek(0)
    t.resume_mouse()
    assert "\033[?1006h" not in cap.getvalue(), "BUILDPLAN_MOUSE=0 时不许恢复鼠标上报"


def test_读按键抛异常立刻退回input(monkeypatch):
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise RuntimeError("按键源炸了")

    monkeypatch.setattr("builtins.input", lambda prompt="": "退回成功")
    t, _cap = _vt(key_fn=_boom, key_peek_fn=lambda: True)
    assert t.ask("你 ▸ ") == "退回成功", "新功能炸了也必须能继续用"
    assert called["n"] == 1
    assert t.raw_key is False, "降级之后不再尝试原始按键"
    assert t.ask("你 ▸ ") == "退回成功"


def test_退化模式完全不受影响():
    cap = io.StringIO()
    t = tui_mod.Tui(stream=cap, force_vt=False, size=(60, 20))
    assert t.vt is False and t.raw_key is False
    t.start()
    t.out("退化模式的输出")
    assert t.scroll("wheel", -1) is False, "没有滚动区就没有应用内滚动"
    assert t._offset == 0
    t.stop()
    out = cap.getvalue()
    assert "\033[?1049" not in out and "\033[?25" not in out and "\033[?1000" not in out
    assert "退化模式的输出" in out


def test_run模式与no_tui一律退化(monkeypatch):
    """`--run` / `--no-tui` 走 console.main 时 force_vt=False → 新功能整体关闭。"""
    import console

    seen = {}

    class _Spy(tui_mod.Tui):
        def __init__(self, *a, **kw):
            seen["force_vt"] = kw.get("force_vt")
            super().__init__(*a, **kw)

        def ask(self, *a, **kw):            # 交互模式下一句话就退出，别真的读 stdin
            return "/quit"

    monkeypatch.setattr(console.tui, "Tui", _Spy)
    monkeypatch.setattr(console, "run_chat", lambda ctx, text, **kw: None)
    console.main(["--run", "你好"])
    assert seen["force_vt"] is False, "--run 必须强制退化"
    console.main(["--no-tui"])
    assert seen["force_vt"] is False, "--no-tui 必须强制退化"


# ======================================================================
# 9. 窗口尺寸变化后视图仍然正确
# ======================================================================
def test_终端改大小后正在看的历史要按新高度重画():
    t, cap = _vt(lines=100, size=(80, 24))
    t.scroll("home")
    assert t._viewport()[0] == "L000"
    t._size = (80, 20)                      # 窗口被拖小：滚动区 19 → 15 行
    cap.truncate(0)
    cap.seek(0)
    t.out("尺寸变了")
    out = cap.getvalue()
    assert "\033[1;15r" in out, "没按新行数重设滚动区"
    assert t._region_height() == 15
    assert t._offset <= len(t._history) - 15, "偏移量必须按新高度重新钳位"
    assert _plain(out).count("L0") > 0, "正在看历史时要按新高度重画，而不是留一屏残影"


# ======================================================================
# 10. 端到端：走真正的 console.main 交互循环（VT + 原始按键）
# ======================================================================
def test_整条链路_滚轮上翻加打字提交加干净退出(monkeypatch, capsys):
    """用户操作：滚轮上滑 2 格 → 打 "/quit" → 回车。

    这条是"终端坏了"这类事故的守门员：它走的是真正的 `console.main` 循环
    （开场白 → ask → 滚动 → 分发 → break → stop），断言：
      ① 上翻提示真的出现过；② 命令照常提交、循环干净退出；
      ③ 退出的还原码齐全，且鼠标上报 h/l 成对（上一轮"整屏空白"的教训）。
    """
    import console

    cap = io.StringIO()
    keys = [b"\x1b[<64;10;5M", b"\x1b[<64;10;5M"] + [c.encode() for c in "/quit"]
    keys += [b"\r"]
    state = {"i": 0, "armed": False}

    def _key():
        if state["i"] < len(keys):
            state["i"] += 1
            return keys[state["i"] - 1]
        return b"\r"                        # 兜底：万一副本多问一次，别把测试挂死

    saved = {}
    _BaseVT = tui_mod.Tui

    class _VT(_BaseVT):
        def __init__(self, *a, **kw):
            kw.update(stream=cap, force_vt=True, size=(100, 20),
                      key_fn=_key, key_peek_fn=lambda: state["armed"])
            super().__init__(*a, **kw)
            saved["t"] = self                 # 取历史总行数用（不写死 30：开场白可能加行）

        def ask(self, *a, **kw):
            # 开场白那一次 out() 也会 pump()：按键要等真正读输入时才算"到了"，
            # 否则滚轮事件会在这时候被吃掉（那时历史还只有 0 行，本来也没什么可翻）。
            state["armed"] = True
            return super().ask(*a, **kw)

    monkeypatch.setattr(console.tui, "Tui", _VT)
    # 开场白换成 30 行：比一屏（20 行 - 3 输入框 - 2 状态区 = 15 行）长，才有历史可翻
    monkeypatch.setattr(console.renderer, "banner_text",
                        lambda **kw: "\n".join("B%02d" % i for i in range(30)))
    console.main([])                        # 交互模式（不带 --run）

    out = cap.getvalue()
    # ⚠️ 第 34 轮：开场白里多了"四种模式"介绍（几行），所以
    #    ① "共 N 行"不再是 30；② 一屏能装下的行数变了，滚轮那条提示可能被
    #    输入框重绘覆盖（VT 布局是固定 3 行输入框 + 2 行状态区）。
    #    这里改成断言"上翻提示这套机制真的生效"，数字不写死 —— 写死会因为
    #    "开场白多了一行"而失败（那正是这条用例刚才的失败原因），
    #    但它要守的东西（滚轮生效、提示可见、退出还原干净）一个都不少。
    plain = _plain(out)
    total = getattr(saved.get("t"), "_hist_total", 0)
    assert ("已上翻" in plain and "End 回到最新" in plain), plain[:600]
    assert total > 0
    assert "\033[?1000h" in out and "\033[?1000l" in out, "鼠标上报必须成对"
    assert out.index("\033[?1000h") < out.index("\033[?1000l"), "先开后关"
    assert "\033[r" in out and "\033[?25h" in out and "\033[?1049l" in out, "退出还原不全"
    assert "再见！" in capsys.readouterr().out, "循环没有干净退出"


# ======================================================================
# 默认必须是"顺序输出"（用户实测：无法选中、无法复制、滚轮依旧无效）
# ======================================================================
def test_默认必须不开备用屏与鼠标上报(monkeypatch):
    """用户原话：「现在的终端无法选中文字，无法复制，滚轮依旧无效」。

    根因：备用屏一开，终端**自带的回滚缓冲变空** → 滚轮失效；
    `?1000h` 一开就**接管鼠标** → 拖选与复制失效。
    而应用内自实现的滚动在真机上又收不到滚轮事件（Windows 下要 ReadFile 读 VT 输入，
    msvcrt.getwch() 那条路拿不到）→ 付出两项能力、换来零收益。
    所以**默认必须是顺序输出**：一条 ?1049h / ?1000h / ?25l 都不许写。
    """
    monkeypatch.delenv("BUILDPLAN_TUI", raising=False)
    monkeypatch.delenv("BUILDPLAN_MOUSE", raising=False)
    monkeypatch.delenv("BUILDPLAN_KBD", raising=False)

    stream = io.StringIO()
    t = tui_mod.Tui(stream=stream, force_vt=None)
    assert t.vt is False, "默认必须是顺序输出（滚轮 / 选中复制都用终端原生的）"
    assert "BUILDPLAN_TUI=1" in t.degrade_reason, t.degrade_reason

    t.start()
    t.out("输出一行内容")
    t.stop()
    buf = stream.getvalue()
    for code in ("\033[?1049h", "\033[?1049l", "\033[?1000h", "\033[?1006h",
                 "\033[?1000l", "\033[?25l", "\033[?25h"):
        assert code not in buf, "默认模式不许写 %r（会抢走终端原生能力）:\n%r" % (code, buf[:200])
    assert "输出一行内容" in buf, "默认模式必须照常输出"


def test_显式开启时才允许备用屏与鼠标上报(monkeypatch):
    """`BUILDPLAN_TUI=1` 才进 VT。

    并且这里守住一条更重要的保证：**读键源不可用时绝不打开鼠标上报** ——
    否则就成了"鼠标被应用接管、但应用又收不到滚轮事件"，正是用户踩到的那个坑。
    """
    monkeypatch.setenv("BUILDPLAN_TUI", "1")
    stream = io.StringIO()
    t = tui_mod.Tui(stream=stream, size=(100, 30))
    assert t.vt is True, t.degrade_reason
    assert t._kbd is None, "本测试没有真控制台，读键源应当不可用"
    t.start()
    t.out("内容")
    t.stop()
    buf = stream.getvalue()
    assert "\033[?1049h" in buf and "\033[?1049l" in buf, "该进也得出备用屏"
    assert "\033[?1000h" not in buf, (
        "读键源不可用时**不许**打开鼠标上报（否则白吃掉用户的拖选/复制，"
        "却又收不到滚轮事件）：\n%r" % buf)
    assert "\033[?1000l" not in buf, "没开就不该有关"
    assert "\033[?25h" in buf, "退出要还原光标"

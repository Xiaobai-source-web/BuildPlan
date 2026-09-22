"""终端外壳：输出排版（块间距 / 配色 / 开场白 / 折叠进度）+ **可选**的底部固定输入框。

━━ 默认模式：顺序输出（**别改这个默认**）━━
默认**不开**备用屏。一切照常写进终端普通缓冲，因此用户的两项原生能力都完好：
  · 鼠标滚轮翻历史 —— 终端自带回滚缓冲里就有全部内容；
  · 拖选 / 复制文本 —— 没有任何鼠标上报在抢。
历史教训（两个都真实发生过）：
  1. 备用屏 + 鼠标上报会把上面两项吃掉。`?1000h` 一旦打开，用户拖选就被应用接管；
     备用屏一开，原生回滚缓冲变空，滚轮直接失效。
  2. 我们在应用内自实现滚动的那条路，在真机上**收不到滚轮事件**：
     Windows 下要拿鼠标事件得用 `ReadFile` 读 VT 输入，而 `msvcrt.getwch()` 那条路拿不到。
     结果是"付出两项能力、换来零收益"。→ 因此默认关闭，只作为实验开关保留。
想要底部输入框：`set BUILDPLAN_TUI=1`（并接受上述两项代价）。

━━ 可选模式：VT 底部输入框（BUILDPLAN_TUI=1）━━
零依赖实现只能靠 ANSI：
  · 进入**备用屏** `ESC[?1049h`（不动用户原来的屏幕内容），
  · 屏幕底部 3 行留给输入框，第 rows-4/rows-3 两行留给**折叠状态区**（原地刷新），
  · 中间 `ESC[1;{rows-5}r` 设成滚动区，输出只在滚动区里滚，
  · 每帧写输出前用 `ESC[{row};1H` 显式定位并 `ESC[K` 清行，不依赖光标漂移。

安全底线（**最要紧的一条**）：进入备用屏后任何一条没走完的退出路径
（崩溃 / Ctrl+C / EOF / main 里 raise）都会把用户终端留在备用屏里 —— 那是"终端坏了"。
所以这里 atexit + try/finally 双保险，`stop()` 可重入，且**只在 start() 过之后**才写还原码。

第 22 轮补上「应用内滚动」（用户原话：「滚轮上滑无法查看历史聊天记录」）：
备用屏是另一块画布，终端**自带的回滚缓冲里什么都没有**，滚轮翻的是空白 ——
所以照 Claude Code 的做法在应用内自己实现：
  · `_write_region` 把写进滚动区的**已折行**文本同时记进 `_history`（带上限的行缓冲）；
  · `_offset` = 离最新一行的距离（0 = 跟随最新）；`_viewport()` 取窗口、`_repaint_region()` 重画；
  · 滚轮上滑 3 行 / PgUp·PgDn 一屏 / ↑↓ 一行 / Home 最旧 / End 回最新；
    离开"跟随最新"时状态区第 2 行给出「已上翻 N 行（共 M 行）· End 回到最新」。
  · 原始按键（Windows `msvcrt`，零第三方依赖）只在 **Windows + TTY + VT** 这条路上开，
    任何异常/`BUILDPLAN_KBD=0`/`msvcrt` 不可用 → **整体静默关闭，完全退回 `input()`**。
  · 鼠标上报 `?1000h`+`?1006h` 与 start/stop **成对**开关，退出时连同滚动区、光标、
    备用屏、控制台输入模式一起还原（上一轮"整屏空白"事故的教训）。
按键解析是**纯函数** `_parse_key()`：测试直接喂字节序列即可，不需要真键盘、真终端。

退化模式（非 TTY / 非 VT / `--run` / 终端太小）：不做任何定位，退回
「空行 → 满宽细线 → `你 ▸ ` 同一行读输入」，行为与改造前一致，测试与脚本照旧可用。
"""

import atexit
import collections
import os
import re
import shutil
import sys
import threading
import time

import renderer

ESC = "\033["
ALT_ON = ESC + "?1049h"          # 备用屏：不破坏用户原来的屏幕
ALT_OFF = ESC + "?1049l"
CUR_HIDE = ESC + "?25l"
CUR_SHOW = ESC + "?25h"
CLEAR_ALL = ESC + "2J" + ESC + "1;1H"
RESET_REGION = ESC + "r"

# 第 22 轮：应用内滚动。鼠标上报必须与 start()/stop() **成对**开关，
# 否则用户退出后终端会把滚轮/点击当成转义序列往输入行里灌。
MOUSE_ON = ESC + "?1000h" + ESC + "?1006h"     # 基本鼠标上报 + SGR 扩展坐标
MOUSE_OFF = ESC + "?1006l" + ESC + "?1000l"    # 顺序与 ON 相反，先关扩展再关基本
HIST_LIMIT = 3000                # 行缓冲上限（规格 §3.1）
WHEEL_STEP = 3                   # 一格滚轮 = 3 行（规格 §2）
KEY_TIMEOUT = 0.05               # ESC 序列拼包超时：读不到就放手，绝不死等（规格 §3.4）
KEY_PUMP_BUDGET = 64             # 输出间隙一次最多处理多少个按键（别把输出饿死）
QUEUE_LIMIT = 512                # 输出期间"打字前瞻"的按键上限
SCROLL_KEYS = ("wheel", "up", "down", "pgup", "pgdn", "home", "end")

# 布局常量：底部 3 行输入框 + 其上 2 行状态区（规格 §B1.2 / §B5.1）
BOX_LINES = 3
STATUS_LINES = 2
MIN_COLS = 40                    # 比这更窄就没法画框，直接退化
MIN_ROWS = 12                    # 比这更矮就没有滚动区，直接退化

# ======================================================================
# 第 23 轮：顺序输出模式下的**紧凑进度行**（长节点"看得出在动"）
# ======================================================================
# 背景（用户实测原话："为什么卡这么久、没有任何提示，不知道是卡了还是在处理"）：
#   · 默认是顺序输出（BUILDPLAN_TUI 未开），本文件 `status()` 只有 force=True 才输出；
#   · kb_scope(#7) 之后的 wbs_agent(#8) 逐个一级相调用大模型展开 2/3 级
#     （约 10 个相 × 每次几十秒 = 几分钟），它发的 node_progress 全被折叠进状态区
#     —— 而退化模式**没有**状态区，于是屏幕上什么都不动，用户无法区分"在跑"和"卡死"。
# 三条规则（缺任何一条都会退回老毛病）：
#   ① 同一节点两条紧凑行至少间隔 min_interval 秒，**或**百分比变化 ≥ min_step；
#   ② 任何两条紧凑行之间还有 hard_interval 秒硬下限 —— 百分比步进快也不许刷屏；
#   ③ 可见输出断了 stall 秒 → 打一条"无新进度，仍在跑（已 Ns）"心跳行兜底
#      （事件驱动 + 后台 tick 两条路都走这一处判定）。
PROGRESS_MIN_INTERVAL = 2.0      # 同一节点两条紧凑进度行的最小间隔（秒）
PROGRESS_MIN_STEP = 5.0          # 或者百分比变化 ≥ 5 个百分点
PROGRESS_HARD_INTERVAL = 0.5     # 任何两条紧凑行之间的硬下限（防刷屏的最后一道闸）
PROGRESS_STALL = 10.0            # 超过这么久没有任何可见输出 → 心跳行兜底
HEARTBEAT_TICK = 5.0             # 后台心跳线程的检查间隔（只查时间，不刷屏）
PROGRESS_MSG_MAX = 56            # 进度文本里 message 的显示宽度上限（保证单行）


def _enable_vt(stream):
    """Windows 下打开 ENABLE_VIRTUAL_TERMINAL_PROCESSING(0x0004)；失败 → 退化。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11；GetStdHandle 在重定向时会失败 → 直接退化
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _env_force():
    """BUILDPLAN_TUI=1/0 手工强制（调试与探针用）；未设置 → 自动判定。"""
    v = os.environ.get("BUILDPLAN_TUI", "").strip().lower()
    if v in ("0", "off", "no", "false"):
        return False
    if v in ("1", "on", "yes", "true"):
        return True
    return None


def _hard_wrap(line, width):
    """按显示宽度硬折行（**保留行首缩进**，不按标点断；ANSI 转义码不占列）。

    为什么需要：定位输出里每一行都必须由我们自己换行 —— 一旦让终端自动折行，
    行数就算错了，滚动区/状态区会错位。renderer.wrap_cjk 会 strip 掉缩进，
    所以这里单独一个"只保证宽度"的版本。
    """
    if width <= 1:
        return [line]
    out, cur, w, i = [], "", 0, 0
    while i < len(line):
        m = renderer._ANSI_RE.match(line, i)
        if m:                       # 转义码跟着当前行，不参与宽度
            cur += m.group(0)
            i = m.end()
            continue
        ch = line[i]
        i += 1
        cw = renderer._disp_width(ch)
        if w + cw > width and cur:
            out.append(cur)
            cur, w = "", 0
        cur += ch
        w += cw
    out.append(cur)
    return out


# ======================================================================
# 第 22 轮：原始按键解析（**纯函数**，测试直接喂字节序列 → 不需要真键盘/真终端）
# ======================================================================
_INCOMPLETE = (None, None, 0)

# Windows 双字节扫描码（\x00 / \xe0 前缀 + 第二字节）。即使开了 VT 输入，
# 老 conhost / 某些终端仍会走这一套，两套都得认。
_SCAN_KEYS = {
    "H": ("up", None), "P": ("down", None), "I": ("pgup", None), "Q": ("pgdn", None),
    "G": ("home", None), "O": ("end", None), "K": ("left", None), "M": ("right", None),
    "S": ("delete", None), "R": ("insert", None),
}
_CTRL_KEYS = {
    "\x03": ("ctrl_c", None), "\x15": ("ctrl_u", None),
    "\x08": ("backspace", None), "\x7f": ("backspace", None),
    "\x0d": ("enter", None), "\x0a": ("enter", None),
    "\x04": ("ctrl_d", None), "\x1a": ("ctrl_z", None),
}
_CSI_KEYS = {"A": ("up", None), "B": ("down", None), "H": ("home", None),
             "F": ("end", None), "C": ("right", None), "D": ("left", None)}
_SS3_KEYS = dict(_CSI_KEYS)
_TILDE_KEYS = {"1": ("home", None), "2": ("insert", None), "3": ("delete", None),
               "4": ("end", None), "5": ("pgup", None), "6": ("pgdn", None),
               "7": ("home", None), "8": ("end", None)}


def _to_text(buf):
    """bytes → 一字节一字符的 str（ASCII 控制码/扫描码/鼠标序列全在这一层）。"""
    if isinstance(buf, (bytes, bytearray, memoryview)):
        return bytes(buf).decode("latin-1")
    return str(buf)


def _utf8_need(byte):
    """UTF-8 首字节需要几个字节；不是首字节 → 0。"""
    if 0xC2 <= byte <= 0xDF:
        return 2
    if 0xE0 <= byte <= 0xEF:
        return 3
    if 0xF0 <= byte <= 0xF4:
        return 4
    return 0


def _parse_escape(s, raw, final):
    """`ESC` 开头。s 是 latin-1/字符视图，raw 是原始 bytes（可为 None）。"""
    if len(s) == 1:
        return ("esc", None, 1) if final else _INCOMPLETE
    nxt = s[1]
    if nxt == "[":
        return _parse_csi(s, final)
    if nxt == "O":                                   # SS3（部分终端的 F1~F4 / Home/End）
        if len(s) < 3:
            return ("unknown", None, len(s)) if final else _INCOMPLETE
        key = _SS3_KEYS.get(s[2])
        return (key[0], key[1], 3) if key else ("unknown", None, 3)
    # Alt+键 之类：整对吃掉，别让 ESC 污染输入行
    return ("unknown", None, 2)


def _parse_csi(s, final):
    """CSI：`ESC [ 参数 终止字节`，含 SGR / X10 鼠标。"""
    i = 2
    while i < len(s) and "\x20" <= s[i] <= "\x3f":    # 参数字节
        i += 1
    if i >= len(s):
        return ("unknown", None, len(s)) if final else _INCOMPLETE
    params, fin = s[2:i], s[i]
    consumed = i + 1
    if fin in ("M", "m") and params.startswith("<"):
        # SGR 扩展鼠标：ESC[<64;col;rowM（64=上滑 / 65=下滑）
        try:
            code = int((params[1:].split(";") or ["0"])[0] or 0)
        except ValueError:
            return ("unknown", None, consumed)
        if code in (64, 65):
            return ("wheel", -1 if code == 64 else 1, consumed)
        return ("unknown", None, consumed)
    if fin == "M" and not params:
        # X10 鼠标：ESC[M + 3 字节；滚轮 = 0x60(上) / 0x61(下)
        if len(s) < consumed + 3:
            return ("unknown", None, len(s)) if final else _INCOMPLETE
        btn = ord(s[consumed]) - 32
        consumed += 3
        if btn & 0x40:
            return ("wheel", -1 if (btn & 0x01) == 0 else 1, consumed)
        return ("unknown", None, consumed)
    if fin == "~":
        key = _TILDE_KEYS.get((params.split(";") or [""])[0].strip())
        return (key[0], key[1], consumed) if key else ("unknown", None, consumed)
    key = _CSI_KEYS.get(fin)
    return (key[0], key[1], consumed) if key else ("unknown", None, consumed)


def _parse_one(buf, final=False):
    """解析缓冲区里的**第一个**按键 → `(kind, value, consumed)`。

    还不足以判断（且 `final=False`，意思是"后面可能还有字节"）→ `(None, None, 0)`。
    kind ∈ char / enter / backspace / ctrl_c / ctrl_u / ctrl_d / ctrl_z / esc /
            up / down / left / right / home / end / pgup / pgdn / wheel /
            delete / insert / unknown；
    `wheel` 的 value：**-1 = 滚轮上滑（往历史翻）、+1 = 下滑**。
    """
    raw = bytes(buf) if isinstance(buf, (bytes, bytearray, memoryview)) else None
    s = _to_text(buf)
    if not s:
        return _INCOMPLETE
    c = s[0]
    if c == "\x1b":
        return _parse_escape(s, raw, final)
    if c in _CTRL_KEYS:
        return (_CTRL_KEYS[c][0], _CTRL_KEYS[c][1], 1)
    if c in ("\x00", "\xe0"):                        # 双字节扫描码
        if len(s) < 2:
            return ("unknown", None, 1) if final else _INCOMPLETE
        key = _SCAN_KEYS.get(s[1], ("unknown", None))
        return (key[0], key[1], 2)
    if c < "\x20":                                   # 其它控制码：忽略
        return ("unknown", None, 1)
    if raw is not None and raw[0] >= 0x80:           # 可能是 UTF-8 多字节字符
        n = _utf8_need(raw[0])
        if n:
            if len(raw) >= n:
                try:
                    return ("char", raw[:n].decode("utf-8"), n)
                except UnicodeDecodeError:
                    pass
            elif not final:
                return _INCOMPLETE
    return ("char", c, 1)


def _parse_key(buf):
    """**纯函数**：一段原始按键（bytes 或 str）→ `(kind, value)`。

    测试直接喂字节序列即可，例如：
        _parse_key(b"\\xe0H")            == ("up", None)
        _parse_key(b"\\x1b[<64;10;5M")   == ("wheel", -1)
        _parse_key("a")                  == ("char", "a")
    """
    kind, value, _n = _parse_one(buf, final=True)
    if kind is None:
        return ("unknown", None)
    return (kind, value)


def _collect_key(getch, kbhit, sleep, timeout=KEY_TIMEOUT):
    """读**一个**按键（含 ESC / 双字节扫描码的拼包）。

    `getch`/`kbhit`/`sleep` 全部注入 → 可以完全离线测（不需要 msvcrt、不需要真键盘）。
    拼包**带短超时**：读不到后续字节就放手，绝不死等（规格 §3.4 的最后一条）。
    """
    data = _to_text(getch())
    deadline = time.monotonic() + timeout
    while _parse_one(data, final=False)[0] is None:
        if not kbhit():
            if time.monotonic() >= deadline:
                break
            sleep(0.002)
            continue
        data += _to_text(getch())
        deadline = time.monotonic() + timeout
    return data


def _stdin_isatty():
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:
        return False


def _mouse_enabled():
    """`BUILDPLAN_MOUSE=0` 可关掉鼠标上报（个别终端下鼠标会捣乱的逃生口）。"""
    v = os.environ.get("BUILDPLAN_MOUSE", "").strip().lower()
    return v not in ("0", "off", "no", "false")


def _kbd_available():
    """Windows + 标准库 msvcrt 可用，且没被 `BUILDPLAN_KBD=0` 关掉（规格 §3.6）。"""
    if os.name != "nt":
        return False
    v = os.environ.get("BUILDPLAN_KBD", "").strip().lower()
    if v in ("0", "off", "no", "false"):
        return False
    try:
        import msvcrt  # noqa: F401
    except Exception:
        return False
    return True


def _vt_input_mode():
    """打开 STD_INPUT 的 ENABLE_VIRTUAL_TERMINAL_INPUT(0x0200) → 返回原 mode；失败 None。

    为什么必须开：不开的话控制台**不会**把鼠标滚轮翻译成 VT 序列，
    `?1000h` 写了也收不到任何东西（这是"滚轮没反应"的第二个根因）。
    注意 `old | 0x0200` **保留 ENABLE_PROCESSED_INPUT**，Ctrl+C 的信号语义照旧。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)                      # STD_INPUT_HANDLE
        m = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(m)):
            return None
        old = int(m.value)
        if not k.SetConsoleMode(h, old | 0x0200):
            return None
        return old
    except Exception:
        return None


def _restore_input_mode(old):
    if old is None or os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-10), ctypes.c_uint32(int(old)))
    except Exception:
        pass


class _MsvcrtKeys:
    """标准库 msvcrt 按键源（零第三方依赖）。"""

    def __init__(self):
        import msvcrt
        self._m = msvcrt

    def _getch(self):
        g = getattr(self._m, "getwch", None)         # 宽字符版：中文/IME 才拿得对
        return g() if callable(g) else self._m.getch()

    def kbhit(self):
        return bool(self._m.kbhit())

    def read(self):
        return _collect_key(self._getch, self.kbhit, time.sleep, KEY_TIMEOUT)


class _KeysFromFn:
    """测试/探针用：`key_fn()` 就是"读一个按键"，`peek_fn()` 是 kbhit。"""

    def __init__(self, key_fn, peek_fn=None):
        self._key_fn = key_fn
        self._peek_fn = peek_fn

    def read(self):
        return self._key_fn()

    def kbhit(self):
        return bool(self._peek_fn()) if self._peek_fn is not None else False


# ======================================================================
# 第 23 轮：顺序模式下的紧凑进度行（长节点"看得出在动"）
# ======================================================================
def _heartbeat_enabled():
    """`BUILDPLAN_HEARTBEAT=0` 可关掉后台心跳线程（重定向到日志/管道时的逃生口）。"""
    v = os.environ.get("BUILDPLAN_HEARTBEAT", "").strip().lower()
    return v not in ("0", "off", "no", "false")


def _clip_width(text, width):
    """按**显示宽度**压成一行并截断（中文 2 列；换行一律变空格）。"""
    s = " ".join(str(text or "").split())
    if width <= 1 or renderer.visible_width(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = renderer._disp_width(ch)
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


def _pct_text(percent):
    """百分比文本：38 → '38'、37.5 → '37.5'（拿不到数值 → ''）。"""
    try:
        f = float(percent)
    except (TypeError, ValueError):
        return ""
    return "%g" % f


# 「只说干完了、没说自己干了什么」的进度文案（节点收尾时几乎都发一条）。
# 这类消息在**已经为该节点打过实质进度行**之后是纯冗余：一次"闲聊"就会因为
# 「30% 判断意图」+「100% 回答完成」弹出两行（用户实测：「太冗余了，只需要一条」）。
# 判据两条，且都要求"没有具体信息"：① 以完成类词收尾；② 不含数字 / 顿号 / 冒号 / 括号。
_GENERIC_DONE = re.compile(
    r"(完成|完毕|通过|结束|就绪|已生成|已保存|已落盘|已算出|已理清|好了)\s*[。．.!！]?$")
_HAS_DETAIL = re.compile(r"[0-9０-９]|、|：|「|（|\(|→|/")


def _is_generic_done(message):
    """「完成 / 通过 / 已生成」这类没有具体信息的收尾文案 → True。

    要求同时满足"没有数字、没有顿号列表、没有冒号/括号"——所以
    「工序先后理清了，共 812 条」「机械配员 12/14 台」都**不会**被当成空话丢掉。
    """
    text = " ".join(str(message or "").split())
    if not text or _HAS_DETAIL.search(text):
        return False
    return bool(_GENERIC_DONE.search(text))


class ProgressFeed:
    """顺序（退化）模式下的进度反馈：**一条紧凑单行** + 节流 + 心跳兜底。

    只在"用户看得见的地方"打：VT 模式有自己的原地刷新状态区，这里 `enabled=False`
    → **一个字节都不写**（既有行为一字不变）；`/verbose` 打开时同样不动它
    （那时逐节点事件本来就全量打印）。

    三条规则见文件顶部常量处的长注释。时钟 `now` 可注入 → 测试不需要真 sleep；
    `sink` 是"把这一行写出去"的回调（console 注入 `term.out`）；`lines` 留下打过的
    **纯文本**行，测试与探针直接断言，不用去抓终端流。
    """

    def __init__(self, sink=None, now=None, enabled=True, verbose=False,
                 min_interval=PROGRESS_MIN_INTERVAL, min_step=PROGRESS_MIN_STEP,
                 hard_interval=PROGRESS_HARD_INTERVAL, stall=PROGRESS_STALL):
        self._sink = sink
        self._now = now if callable(now) else time.monotonic
        self.enabled = bool(enabled)
        self.verbose = bool(verbose)
        self.min_interval = float(min_interval)
        self.min_step = float(min_step)
        self.hard_interval = float(hard_interval)
        self.stall = float(stall)
        self.lines = []                       # 打出去的行（纯文本）
        self._last_visible = self._now()      # 上一次"屏幕上有字"的时间
        self._last_line_at = None             # 上一次紧凑行的时间（硬下限用）
        self._printed = {}                    # node -> (time, percent)：只记**打过**的点
        self._titles = {}
        self._indexes = {}
        self._steps_total = 0                 # run_plan 下发的总步数（0 = 老后端没给）
        self._started = []                    # 本次运行启动过的节点（按顺序，去重）
        self._current = ""
        self._current_percent = None
        self._current_message = ""            # 当前节点最后一条进度文本（心跳行里带上它）
        # 兜底心跳线程是否允许启动（第 33 轮）：console 按"有没有真终端"设置它。
        # 默认 True → 既有的直接构造（测试 / 探针）行为一字不变。
        self.allow_watchdog = True
        self._suspended = 0
        self._stop = threading.Event()
        self._thread = None

    # ---------------- 外部输入 ----------------
    def note_visible(self):
        """别处打了可见输出（块 / 门 / 结果）→ 心跳计时归零。"""
        self._last_visible = self._now()

    def suspend(self):
        """暂停反馈（用户正在门里做选择 / 输入）：这期间绝不刷心跳行。"""
        self._suspended += 1

    def resume(self):
        self._suspended = max(0, self._suspended - 1)
        self.note_visible()

    @property
    def suspended(self):
        return self._suspended > 0

    def note_node_start(self, node, index=None, title=""):
        self._current = str(node or "")
        self._current_percent = None
        self._current_message = ""            # 换节点：别把上一个节点的进度文本带到心跳行
        if node and node not in self._started:
            self._started.append(node)        # 用"走了几步"决定要不要显示分母
        if index:
            self._indexes[self._current] = index
        if title:
            self._titles[self._current] = title

    def note_steps(self, steps):
        """收下引擎的步数表（`run_plan`）→ 步号与界面名的**唯一**来源。

        终端自己数节点会数错（跳节点、门里重入），所以步号一律以后端下发的为准；
        没有下发时退回 `index`（老后端兼容）。
        """
        n = 0
        for item in (steps or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("node") or "")
            if not name:
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            self._indexes[name] = idx
            self._titles[name] = str(item.get("title") or name)
            n += 1
        if n:
            self._steps_total = max(self._steps_total, n)
        return n

    def note_steps_total(self, total):
        """`node_start.steps`（本次运行的总步数）→ 只抬不降，避免局部调用把分母改小。"""
        try:
            total = int(total or 0)
        except (TypeError, ValueError):
            return self._steps_total
        if total > 0:
            self._steps_total = max(self._steps_total, total)
        return self._steps_total

    # ---------------- 事件入口 ----------------
    def on_event(self, event, data=None, index=None, title=""):
        """消费一个 SSE 事件 → 返回是否打了行。

        **任何**事件都要从这里走一遍（包括 ping 与 node_done）：进度行只在
        node_progress 上打；其余事件只用来判断"是不是该兜一条心跳"。
        """
        if not self.enabled or self.suspended:
            return False
        data = data or {}
        node = str(data.get("node") or "")
        if event == renderer.EV_RUN_PLAN:
            self.note_steps(data.get("steps"))
            return False
        if event == renderer.EV_NODE_START:
            self.note_steps_total(data.get("steps"))     # 引擎随 node_start 带的总步数
            self.note_node_start(node, index=index, title=title or node)
            return False
        if event == renderer.EV_NODE_PROGRESS:
            return self._on_progress(node, data.get("progress"), data.get("message"),
                                     index=index, title=title)
        return self.heartbeat_if_stale()

    def heartbeat_if_stale(self):
        """可见输出断了 ≥ stall 秒 → 打一条"无新进度，仍在跑（已 Ns）"。"""
        if not self.enabled or self.suspended:
            return False
        now = self._now()
        idle = now - (self._last_visible if self._last_visible is not None else now)
        if idle < self.stall:
            return False
        return self._print(self._heartbeat_text(idle), now)

    # ---------------- 内部：节流 + 渲染 ----------------
    def _on_progress(self, node, percent, message, index=None, title=""):
        now = self._now()
        if index:
            self._indexes[node] = index
        if title:
            self._titles[node] = title
        if node:
            self._current = node
        if message:
            self._current_message = str(message)      # 心跳行要把"正在做什么"带上
        pct = None
        try:
            pct = float(percent) if percent is not None else None
        except (TypeError, ValueError):
            pct = None
        if pct is not None:
            self._current_percent = pct
        if not node:
            return self.heartbeat_if_stale()
        # ⓪ 这条只是"我干完了"、没有任何具体信息，而这个节点**已经**报过一次实质进度
        #    → 纯冗余（正是"闲聊弹两行"的来源）。记账（让它成为"报过"的基线）但不打。
        if self._printed.get(node) is not None and _is_generic_done(message):
            self._printed[node] = (now, pct)
            return False
        # ② 硬下限：百分比步进再快，两条紧凑行也不许挨得比 hard_interval 更近
        if self._last_line_at is not None and now - self._last_line_at < self.hard_interval:
            return False
        prev = self._printed.get(node)
        if prev is None:
            # 该节点的第一条：换节点之后也要隔 min_interval ——
            # 否则 26 个节点各来一条就是用户抱怨过的"全都堆出来"
            if self._last_line_at is not None and now - self._last_line_at < self.min_interval:
                return False
        else:
            # ① 同节点：间隔够长 **或** 百分比变化 ≥ 5
            dt = now - prev[0]
            step = (abs(pct - prev[1])
                    if (pct is not None and prev[1] is not None) else None)
            if not (dt >= self.min_interval or (step is not None and step >= self.min_step)):
                return False
        self._printed[node] = (now, pct)
        return self._print(self._progress_text(node, pct, message), now)

    def _name_of(self, node):
        """紧凑行里的节点名：`第 8 步 · 编制 WBS 分工`。

        分母只在**本次运行真的走了多步**时加（`第 8 / 26 步`）——
        一次闲聊只跑 1 步，"第 1 / 26 步"会让用户以为触发了整条流水线
        （用户实测原话：「什么叫已完成 0/26，哪来的 26」）。
        """
        title = self._titles.get(node) or node or "?"
        idx = self._indexes.get(node)
        if not idx:
            return title
        if self._steps_total > 1 and len(self._started) > 1:
            return "第 %s / %d 步 · %s" % (idx, self._steps_total, title)
        return "第 %s 步 · %s" % (idx, title)

    def _progress_text(self, node, percent, message):
        """一条紧凑进度行：`  ⏳ 第 8 / 26 步 · 编制 WBS 分工 · ② 逐相展开 3/10`。

        百分比**默认不显示**（用户实测：「太冗余了」；有进度文本时百分比不添信息）。
        `/verbose` 打开时照旧带上，脚本 / 探针要看数值时用它。
        """
        text = "  ⏳ " + self._name_of(node)
        msg = _clip_width(message, PROGRESS_MSG_MAX)
        if msg:
            text += " · " + msg
        if self.verbose:
            pct = _pct_text(percent)
            if pct:
                text += "  %s%%" % pct
        return text

    def _heartbeat_text(self, idle):
        """兜底心跳行：把"在跑第几步、正在做什么"一起交代清楚（用户不会看着一行数字猜）。"""
        text = "  ⏳ " + (self._name_of(self._current) if self._current else "流水线")
        msg = _clip_width(self._current_message or "", PROGRESS_MSG_MAX)
        if msg:
            text += " · " + msg
        text += " · 无新进度，仍在跑（已 %ds）" % int(idle)
        if self.verbose:
            pct = _pct_text(self._current_percent)
            if pct:
                text += "  %s%%" % pct
        return text

    def _print(self, text, now):
        self.lines.append(text)
        self._last_visible = now
        self._last_line_at = now
        if self._sink is not None:
            try:
                self._sink(text)
            except Exception:  # noqa: BLE001 — 打进度绝不能把正在跑的流水线打断
                pass
        return True

    # ---------------- 后台心跳（"一个事件都没有"的最坏情况） ----------------
    def start_watchdog(self, tick=HEARTBEAT_TICK):
        """起一条**只查时间、不刷屏**的守护线程：模型很慢 / 后端不 ping 也看得出在跑。

        这是规则③里"事件驱动"那条路的补强 —— 只要一个事件都收不到，
        事件驱动就永远不会触发，屏幕上就会重新变成死水。

        `allow_watchdog=False`（重定向 / 管道 / 测试）时**不起线程**：没有人盯着屏幕，
        心跳只会污染日志与测试输出（用户实测截图里那两行"仍在跑（已 14s）"就是它）。
        """
        if not self.enabled or self._thread is not None or not _heartbeat_enabled():
            return False
        if not getattr(self, "allow_watchdog", True):
            return False
        self._stop.clear()
        thread = threading.Thread(target=self._watch, args=(max(0.5, float(tick)),),
                                  name="buildplan-heartbeat", daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _watch(self, tick):
        while not self._stop.wait(tick):
            try:
                self.heartbeat_if_stale()
            except Exception:  # noqa: BLE001 — 心跳线程自己出错也不能影响主流程
                return

    def stop_watchdog(self):
        """停掉后台心跳（可重入；没起过也安全）。"""
        self._stop.set()
        self._thread = None


class Tui:
    """VT 定位输出层。`force_vt`/`size`/`input_fn`/`key_fn` 只为可测性而存在。"""

    def __init__(self, stream=None, *, force_vt=None, size=None, input_fn=None,
                 key_fn=None, key_peek_fn=None):
        self.stream = stream if stream is not None else sys.stdout
        self._input_fn = input_fn
        self._size = size                      # (cols, rows) 覆盖，探针用
        self._started = False
        self._wrote_any = False
        self._status_cache = None
        self._row = 1
        self.degrade_reason = ""
        # ---- 应用内滚动（第 22 轮）----
        self._history = collections.deque(maxlen=HIST_LIMIT)   # 已折行的历史行
        self._hist_total = 0                                   # 历史总行数（含被丢掉的）
        self._offset = 0                                       # 0 = 跟随最新
        self._queue = []                                       # 输出期间收到的按键（打字前瞻）
        self._box_state = None                                 # 正在行编辑时的 (hint, buf)
        self._console_mode_orig = None                         # 控制台输入模式原值
        self._input_mode_on = False
        self._mouse_on = False
        self._suspended = False
        # 后台心跳线程与主线程都会写同一个流：加一把锁，两行文字绝不许交叉
        # （第 23 轮：ProgressFeed 的守护线程）。
        self._wlock = threading.Lock()

        self.cols, self.rows = self._measure()

        forced = force_vt if force_vt is not None else _env_force()
        if forced is None:
            # ⚠️ **默认不开**备用屏 TUI。这是被实测打回来后的决定：
            #   备用屏 + 鼠标上报会把终端的两项原生能力吃掉 ——
            #     ① 原生回滚缓冲变空 → 鼠标滚轮翻不到历史；
            #     ② `?1000h` 接管鼠标 → 拖选、复制文本失效。
            #   而我们自己在应用内实现滚动的路子在真机上又收不到滚轮事件
            #   （Windows 下要拿鼠标事件得用 ReadFile 读 VT 输入，msvcrt.getwch() 那条路
            #    拿不到），于是"付出两项能力、换来零收益"。
            #   → 默认走**顺序输出**：一切进终端普通缓冲，滚轮与选中复制都是原生的。
            #   想要底部输入框：`set BUILDPLAN_TUI=1`（并接受上述两项代价）。
            self.vt = False
            self.degrade_reason = ("默认顺序输出（滚轮 / 选中复制都用终端原生的）；"
                                   "要底部输入框请设 BUILDPLAN_TUI=1")
        else:
            self.vt = bool(forced)
            if self.vt and (self.cols < MIN_COLS or self.rows < MIN_ROWS + 1):
                self.vt = False
                self.degrade_reason = "终端 %dx%d 太小（需要 ≥ %d 列 × %d 行）" % (
                    self.cols, self.rows, MIN_COLS, MIN_ROWS + 1)
            if not self.vt and not self.degrade_reason:
                self.degrade_reason = ("非 TTY / 无 VT 支持 / --run 非交互"
                                       if forced is None else "调用方要求退化模式")

        # 原始按键（应用内滚动的输入源）：只在 VT 模式下、且不会抢走注入的 input_fn。
        # 任何一条不满足 → self._kbd is None → ask() 完全走原来的 input()（兜底）。
        self._kbd = None
        if self.vt:
            if key_fn is not None:
                self._kbd = _KeysFromFn(key_fn, key_peek_fn)
            elif input_fn is None and _kbd_available() and _stdin_isatty():
                try:
                    self._kbd = _MsvcrtKeys()
                except Exception:
                    self._kbd = None

        # 版式（列从 1 起）：
        #   1 .. region_bottom     滚动区（输出）
        #   rows-4, rows-3         折叠状态区（原地刷新）
        #   rows-2, rows-1, rows   输入框（3 行）
        self.region_top = 1
        self.region_bottom = max(1, self.rows - (BOX_LINES + STATUS_LINES))
        self.status_top = self.rows - BOX_LINES - STATUS_LINES + 1
        self.box_top = self.rows - BOX_LINES + 1

    @property
    def raw_key(self):
        """原始按键是否可用（滚轮/键盘上翻只在它为 True 时开）。"""
        return self._kbd is not None

    # ---------------- 尺寸 / 判定 ----------------
    def _measure(self):
        if self._size:
            cols, rows = self._size
        else:
            try:
                cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            except Exception:
                cols, rows = 80, 24
        return max(20, int(cols)), max(6, int(rows))

    def _auto_vt(self):
        if os.environ.get("TERM", "").strip().lower() == "dumb":
            return False
        try:
            if not self.stream.isatty():
                return False
        except Exception:
            return False
        try:
            stdin = sys.stdin
            if stdin is not None and hasattr(stdin, "isatty") and not stdin.isatty():
                return False
        except Exception:
            return False
        return _enable_vt(self.stream)

    # ---------------- 底层写 ----------------
    def _raw(self, text):
        try:
            with self._wlock:          # 心跳线程可能并发：一次写完，不交叉
                self.stream.write(text)
                self.stream.flush()
        except Exception:
            pass          # 退出阶段流可能已关闭：绝不因为写失败而抛异常

    def start(self):
        """进入备用屏 + 设滚动区 + 开鼠标上报。可重复调用（幂等）。"""
        if self._started:
            return
        self._started = True
        atexit.register(self.stop)          # 崩溃/异常退出也要还原屏幕
        if not self.vt:
            return
        if self._kbd is not None and _mouse_enabled() and self._apply_input_mode():
            # 只有 VT 输入模式真的就绪才开鼠标上报：否则老 conhost 会白白吃掉
            # 用户的鼠标选择能力，而滚轮事件一个也收不到。
            self._mouse_on = True
            mouse = MOUSE_ON
        else:
            mouse = ""
        self._raw("".join([
            ALT_ON, CUR_HIDE, CLEAR_ALL, mouse,
            ESC + "1;%dr" % self.region_bottom,
            ESC + "1;1H",
        ]))
        self._row = 1

    def stop(self):
        """还原屏幕（鼠标上报 + 输入模式 + 滚动区 + 光标 + 备用屏）。可重入，未 start 过也安全。"""
        if not self._started:
            return
        self._started = False
        try:
            atexit.unregister(self.stop)
        except Exception:
            pass
        if not self.vt:
            self._disable_kbd()
            return
        # 顺序要紧：先关鼠标上报（否则用户后面在别的界面滚轮会灌进转义序列），
        # 再复位滚动区（不然退出备用屏后区域可能残留），再清掉底部状态/输入区，
        # 再显光标，最后离开备用屏。控制台输入模式也要还原。
        self._disable_kbd()
        self._mouse_off()
        self._raw("".join([
            RESET_REGION,
            ESC + "%d;1H" % self.status_top, ESC + "0J",
            CUR_SHOW,
            ALT_OFF,
        ]))

    # ---------------- 鼠标上报 / 控制台输入模式（必须成对还原） ----------------
    def _apply_input_mode(self):
        """打开 VT 输入模式 → 返回"是否就绪"（非 Windows 视为就绪）。"""
        if self._input_mode_on:
            return True
        if os.name != "nt":
            return True
        old = _vt_input_mode()
        if old is None:
            return False
        if self._console_mode_orig is None:
            self._console_mode_orig = old
        self._input_mode_on = True
        return True

    def _revert_input_mode(self):
        if not self._input_mode_on:
            return
        self._input_mode_on = False
        _restore_input_mode(self._console_mode_orig)

    def _mouse_off(self):
        """关掉鼠标上报（幂等：写过几次 ON 就只会配一次 OFF）。"""
        if not self._mouse_on:
            return
        self._mouse_on = False
        self._raw(MOUSE_OFF)

    def suspend_mouse(self):
        """临时关鼠标上报 + 还原控制台输入模式（`commands.py` 里还有内建 input()）。

        不这么做的后果：用户在那个提示上滚一下滚轮，SGR 鼠标序列会被 input()
        当成用户打的字灌进输入行。
        """
        if not (self._mouse_on or self._input_mode_on):
            return False
        self._mouse_off()
        self._revert_input_mode()
        self._suspended = True
        return True

    def resume_mouse(self):
        """suspend_mouse() 的配对操作；原本就没开或原始按键已关 → 什么都不做。"""
        if not getattr(self, "_suspended", False):
            return
        self._suspended = False
        if self._kbd is None or not self.vt or not _mouse_enabled():
            return
        if not self._apply_input_mode():
            return
        self._mouse_on = True
        self._raw(MOUSE_ON)

    def _disable_kbd(self):
        """关掉原始按键（异常/降级时调用）：还原输入模式 + 关鼠标，可重入。"""
        had = self._kbd is not None
        self._kbd = None
        if had:
            self._mouse_off()
        self._revert_input_mode()

    # ---------------- 输出（滚动区） ----------------
    def _refresh_layout(self):
        """终端尺寸变了 → 重算布局并重置滚动区。

        不做这件事的后果：用户拖一下窗口，滚动区还停在旧行数、输入框画到屏幕外或
        压在输出上 —— 表现出来就是"界面坏了"。这里在每次输出/读输入前顺手核对一次。
        """
        if not self.vt:
            return
        cols, rows = self._measure()
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        self.region_bottom = max(1, rows - (BOX_LINES + STATUS_LINES))
        self.status_top = rows - BOX_LINES - STATUS_LINES + 1
        self.box_top = rows - BOX_LINES + 1
        self._status_cache = None
        self._raw(RESET_REGION + CLEAR_ALL
                  + ESC + "1;%dr" % self.region_bottom + ESC + "1;1H")
        self._row = 1
        # 滚动区高度变了 → 偏移量要重新钳位；正在看历史就按新高度重画
        self._offset = self._clamp_offset(self._offset)
        if self._offset > 0:
            self._repaint_region()
            self._render_status()

    def _write_region(self, text):
        """滚动区唯一写入口：先记历史（已折行的行），再决定要不要真的画。

        规格 §3.1：`_history` 是带上限的行缓冲，视图只是它的一个窗口。
        规格 §3.2：`offset>0`（正在看历史）时新输出**只进缓冲、不改视图** ——
        否则用户正在回看时会被新输出顶走。做法是让 offset 跟着总行数一起涨，
        视图窗口的绝对位置就钉死了。
        """
        lines = []
        for ln in str(text).split("\n"):
            lines.extend(_hard_wrap(ln, self.cols - 1))
        for ln in lines:
            self._history.append(ln)
        self._hist_total += len(lines)
        if self._offset > 0:
            self._offset = self._clamp_offset(self._offset + len(lines))
            return
        if not self.vt:
            for ln in lines:
                self._raw(ln + "\n")
            return
        for ln in lines:
            if self._row > self.region_bottom:
                # 到滚动区底部：在底行换一次行 = 整个区域上滚一行（不碰状态区/输入框）
                self._raw(ESC + "%d;1H" % self.region_bottom + "\n")
                self._row = self.region_bottom
            self._raw(ESC + "%d;1H" % self._row + ESC + "K" + ln + "\n")
            self._row = min(self._row + 1, self.region_bottom)

    # ---------------- 应用内滚动（第 22 轮） ----------------
    def _region_height(self):
        return max(1, self.region_bottom - self.region_top + 1)

    def _max_offset(self):
        """最多能上翻多少行：让视图顶端停在**还留着的**最旧一行。"""
        return max(0, len(self._history) - self._region_height())

    def _clamp_offset(self, off):
        """钳位：不能负、不能越界（规格 §4）。"""
        try:
            off = int(off)
        except Exception:
            off = 0
        return max(0, min(off, self._max_offset()))

    def _eff_offset(self):
        return self._clamp_offset(self._offset)

    def _viewport(self):
        """当前视图窗口 = `_history` 的一个切片（长度 ≤ 滚动区高度）。"""
        n = self._region_height()
        total = self._hist_total
        base = total - len(self._history)          # 还留着的最旧一行的绝对序号
        off = self._eff_offset()
        end = max(0, total - off)
        start = max(base, end - n)
        if end <= base:
            return []
        return list(self._history)[start - base:end - base]

    def _repaint_region(self):
        """按当前视图重画滚动区：逐行 `ESC[{row};1H` + `ESC[K`，与 `_write_region` 一致。

        整屏一次写出（一次 flush）：逐行 flush 会在真终端上闪。
        """
        if not self.vt:
            return
        self._offset = self._clamp_offset(self._offset)
        rows = self._viewport()
        parts = []
        for i in range(self._region_height()):
            txt = rows[i] if i < len(rows) else ""
            parts.append(ESC + "%d;1H" % (self.region_top + i) + ESC + "K" + txt)
        self._raw("".join(parts))
        self._row = min(self.region_top + len(rows), self.region_bottom)

    def scroll(self, kind, value=None):
        """滚动视图。kind ∈ SCROLL_KEYS；wheel 的 value：-1 上滑 / +1 下滑。

        滚轮 3 行 / ↑↓ 1 行 / PgUp·PgDn 一屏（滚动区高度 - 1）/ Home 最旧 / End 回最新。
        返回是否真的处理了这个按键。
        """
        if not self.vt or kind not in SCROLL_KEYS:
            return False
        before = self._eff_offset()
        page = max(1, self._region_height() - 1)
        if kind == "wheel":
            delta = WHEEL_STEP if (value if value is not None else -1) < 0 else -WHEEL_STEP
        elif kind == "up":
            delta = 1
        elif kind == "down":
            delta = -1
        elif kind == "pgup":
            delta = page
        elif kind == "pgdn":
            delta = -page
        elif kind == "home":
            self._offset = self._max_offset()
            delta = 0
        else:                                       # end
            self._offset = 0
            delta = 0
        if delta:
            self._offset = self._clamp_offset(self._offset + delta)
        else:
            self._offset = self._clamp_offset(self._offset)
        if self._eff_offset() != before:
            self._repaint_region()
        self._render_status()
        if self._box_state is not None:             # 正在行编辑：把光标送回输入框
            self._draw_box(self._box_state[0], self._box_state[1])
        return True

    def pump(self, budget=KEY_PUMP_BUDGET):
        """输出间隙顺手处理滚轮/翻页（非阻塞，最多 budget 次）。

        为什么要它：一次运行里输出会持续很久，用户在滚轮上滑时也该能看历史。
        读到的**非滚动键**存进 `_queue`，下一次行输入照原样吃回去（不丢用户打的字）。
        """
        if not self.vt or self._kbd is None:
            return 0
        handled = 0
        for _ in range(max(0, int(budget))):
            try:
                if not self._kbd.kbhit():
                    break
                seq = self._kbd.read()
            except KeyboardInterrupt:
                raise
            except Exception:
                self._disable_kbd()                 # 按键源坏了：静默关闭，退回 input()
                break
            kind, value = _parse_key(seq)
            if kind in SCROLL_KEYS:
                self.scroll(kind, value)
                handled += 1
            elif kind == "ctrl_c":
                # VT 输入模式下 Ctrl+C 可能是原始字节而不是信号：这里补上中断语义
                raise KeyboardInterrupt
            elif len(self._queue) < QUEUE_LIMIT:
                self._queue.append((kind, value))
        return handled

    def out(self, text, gap=True, rule_before=False):
        """打印一个**块**。gap=True 时块前空一行（规格 §B2：块与块之间必须空一行）。"""
        if text is None:
            return
        text = str(text)
        if not text.strip():
            return
        self._refresh_layout()
        self.pump()                 # 输出间隙顺手响应滚轮（正在看历史时下面的写入不画）
        block = text
        if rule_before:
            block = renderer.rule(self.cols) + "\n" + text
        if self.vt:
            if gap and self._wrote_any:
                self._write_region("")
            self._write_region(block)
        else:
            if gap and self._wrote_any:
                self._raw("\n")
            self._write_region(block)
        self._wrote_any = True

    def status(self, line1, line2="", force=False):
        """折叠状态区：VT 原地刷新；退化模式无定位能力 → 只在 force 时打两行心跳。"""
        line1 = str(line1 or "")
        line2 = str(line2 or "")
        key = (line1, line2)
        if key == self._status_cache:
            return
        if self.vt:
            self._status_cache = key
            self.pump()             # 心跳也是输出间隙，同样顺手响应滚轮
            self._render_status()
            return
        if force:
            self._status_cache = key
            body = "\n".join(x for x in (line1, line2) if x)
            if body:
                self._write_region(body)
                self._wrote_any = True

    def _render_status(self):
        """画状态区。**离开"跟随最新"时第 2 行换成滚动提示**（规格 §2：必须有可见反馈）。"""
        if not self.vt:
            return
        line1, line2 = self._status_cache or ("", "")
        off = self._eff_offset()
        if off > 0:
            line2 = "已上翻 %d 行（共 %d 行）· End 回到最新" % (off, self._hist_total)
        for i, txt in enumerate((line1, line2)):
            row = self.status_top + i
            self._raw(ESC + "%d;1H" % row + ESC + "K" + renderer._fit(txt, self.cols - 1))


    def clear_status(self):
        """运行结束：把状态区擦干净（否则会留着上一次的节点名）。"""
        if not self.vt:
            self._status_cache = None
            return
        self._status_cache = None
        for i in range(STATUS_LINES):
            self._raw(ESC + "%d;1H" % (self.status_top + i) + ESC + "K")
        if self._eff_offset() > 0:
            self._render_status()       # 正上翻看历史时，提示不能跟着进度一起消失

    # ---------------- 输入（底部输入框） ----------------
    def _draw_box(self, hint, text=""):
        w = max(4, self.cols - 2)
        self._raw(ESC + "%d;1H" % self.box_top + ESC + "K"
                  + renderer.color("╭" + "─" * w + "╮", "accent"))
        inner = renderer.color("│ ", "accent") + hint + str(text or "")
        if renderer.visible_width(inner) > self.cols - 2:
            inner = renderer._fit(inner, self.cols - 2)     # 太长会顶破边框、整屏错位
        self._raw(ESC + "%d;1H" % (self.box_top + 1) + ESC + "K" + inner)
        self._raw(ESC + "%d;1H" % (self.box_top + 2) + ESC + "K"
                  + renderer.color("╰" + "─" * w + "╯", "accent"))
        # ⚠️ 必须把光标放回**中间那一行**的提示符之后。
        # 否则光标停在下边框行（最后写的那一笔），用户打字会打在边框线上 ——
        # 表现就是"什么都输入不了"（实测踩过）。
        col = (1 + renderer._disp_width("│ ") + renderer._disp_width(hint)
               + renderer._disp_width(str(text or "")))
        self._raw(ESC + "%d;%dH" % (self.box_top + 1, min(col, max(1, self.cols - 1))))

    def _clear_box(self):
        self._raw(ESC + "%d;1H" % self.box_top + ESC + "0J")

    def _read(self, prompt):
        fn = self._input_fn
        return fn(prompt) if fn is not None else input(prompt)

    def _next_key(self):
        """取下一个按键 → `(kind, value)`。先消化 pump() 期间收下的按键（打字前瞻）。"""
        if self._queue:
            return self._queue.pop(0)
        if self._kbd is None:
            return (None, None)
        seq = self._kbd.read()
        if not seq:
            return (None, None)
        return _parse_key(seq)

    def _read_line_raw(self, hint):
        """VT 模式自带的逐键行编辑（规格 §3.5）。

        Backspace / Enter / Ctrl+C / Ctrl+U 自己处理；滚轮 / PgUp·PgDn / ↑↓ / Home·End
        交给 `scroll()`（看得见历史）；中文等宽字符"收到就回显"，不做左右移动编辑。
        """
        buf = ""
        empty = 0
        self._box_state = (hint, buf)
        try:
            while True:
                kind, value = self._next_key()
                if kind is None:                 # 按键源没给出东西：有限次后放弃，别卡死终端
                    empty += 1
                    if empty > 1000:
                        raise RuntimeError("按键源无响应")
                    time.sleep(0.005)
                    continue
                empty = 0
                if kind in SCROLL_KEYS:
                    self.scroll(kind, value)     # 内部会把光标送回输入框
                    continue
                if kind == "char":
                    buf += value
                elif kind == "backspace":
                    buf = buf[:-1]
                elif kind == "ctrl_u":
                    buf = ""
                elif kind == "enter":
                    return buf
                elif kind == "ctrl_c":
                    raise KeyboardInterrupt
                elif kind in ("esc", "unknown", "left", "right", "insert", "delete",
                              "ctrl_d", "ctrl_z"):
                    continue
                self._box_state = (hint, buf)
                self._draw_box(hint, buf)
        finally:
            self._box_state = None

    def ask(self, hint="你 ▸ ", rule=True, echo=True):
        """读一行输入。

        VT + 原始按键可用：应用内行编辑（顺带支持滚轮上翻历史）；
        其余一切情况（退化 / `--run` / 非 TTY / msvcrt 不可用 / 读按键报错）：
        完全走原来的内建 `input()`，新功能静默关闭 —— 绝不让终端不可用。
        """
        if not self.vt:
            if rule:
                self.out(renderer.rule(self.cols), gap=True)
            return self._read(renderer.color(hint, "accent", bold=True))
        self._refresh_layout()
        colored = renderer.color(hint, "accent", bold=True)
        self._draw_box(colored)
        if self._kbd is None:
            try:
                ans = self._read("")
            finally:
                self._clear_box()
        else:
            try:
                ans = self._read_line_raw(colored)
            except (KeyboardInterrupt, EOFError):
                self._clear_box()
                raise
            except Exception:
                # 兜底：任何异常都立刻退回 input()（规格 §3.6），静默、不报错、不退出
                self._disable_kbd()
                self._clear_box()
                self._draw_box(colored)
                try:
                    ans = self._read("")
                finally:
                    self._clear_box()
            else:
                self._clear_box()
        if echo:
            self.out(renderer.rule(self.cols) + "\n"
                     + renderer.color("你 ▸ ", "accent", bold=True) + str(ans or ""),
                     gap=True)
        return ans

    # ---------------- 便捷属性 ----------------
    @property
    def mode(self):
        if not self.vt:
            return "退化（无定位）"
        return "VT（底部输入框%s）" % ("+应用内滚动" if self._kbd is not None else "")


# ----------------------------------------------------------------------
# 当前实例（confirmer / 命令层都要往同一个底部框里读写）
# ----------------------------------------------------------------------
_current = None


def install(t):
    """安装当前 Tui（console.main 调用）。"""
    global _current
    _current = t
    return t


def current():
    """取当前 Tui；没安装过（测试 / 被当库调用）→ 退化模式实例，不发定位码。"""
    global _current
    if _current is None:
        _current = Tui(force_vt=False)
    return _current


def out(text, **kw):
    current().out(text, **kw)


def ask(hint="你 ▸ ", **kw):
    return current().ask(hint, **kw)


def status(line1, line2="", **kw):
    current().status(line1, line2, **kw)


def suspend_mouse():
    """临时关鼠标上报（`commands.py` 里还有内建 input()；见 Tui.suspend_mouse）。"""
    return current().suspend_mouse()


def resume_mouse():
    """与 suspend_mouse() 配对。"""
    return current().resume_mouse()


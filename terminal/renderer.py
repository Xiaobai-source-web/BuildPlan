"""事件渲染 + 门的"实物内容" + 折叠进度 + Plan 落盘 + /show HTML — T-04（零第三方依赖）

这个模块承担用户吐槽最多的三件事：

1. **配色精简**（「颜色分类过多，显得界面十分杂乱」）
   只保留 4 类语义色 + dim：accent(36) / ok(32) / warn(33) / err(31) / dim(90)。
   `C` 字典仍留旧键名（yellow/blue/green/cyan/red/magenta/gray…）做**别名**，
   但一律映射到这 4+1 类 —— 调用方不用一次改爆，界面也不会再花。

2. **间距**（「门与门之间，流程与流程之间，对话与对话之间都没有间隔」）
   `rule()` 满宽细线 + `console`/`tui` 的块间空行；本模块只提供纯文本，落位由 tui 决定。

3. **门的实物内容**（「WBS 门应该返回详细的 WBS 树…你打算让用户依据什么来决定是否继续」）
   R1 渲染 WBS 树、R2 渲染两版工期对比表 + 最长任务、R3 渲染草案目录/表格/图，
   字段按规格 §D 的冻结契约读取；**缺字段一律优雅退化**（回落 highlights → summary），
   不崩、不打印 None。

4. **折叠进度**（「一定需要把所有的进度都展开吗」）
   `FoldState` 只留最近 3 个"正在跑/最近跑"的节点 + 总进度；
   `event_action()` 是 console 与探针**共用**的折叠策略（保证演示的和真跑的一致）。
"""

import datetime
import html
import json
import os
import re
import shutil

# ======================================================================
# 配色：只有 4 类语义色 + dim（规格 §B3）
# ======================================================================
_RESET = "\033[0m"

_SEMANTIC = {
    "accent": "\033[36m",           # 品牌 / 标题 / 框线 / 当前状态
    "ok": "\033[32m",               # 成功、通过、已保存
    "warn": "\033[33m",             # 警告、缺证据、待确认（★人工门就用它）
    "err": "\033[31m",              # 失败、错误、不可用
    # 次要说明、路径、提示。
    # ⚠️ 这里刻意**不**用 `\033[90m`（亮黑）：实测在浅色主题 / 非 Windows Terminal 下
    # 它几乎看不见（用户反馈原话：「这个灰色字体有点看不清」）。改用 `\033[37m`（浅灰）
    # 并保留 `\033[2m` 半亮属性 —— 层次感靠**半亮**维持（37+2m 比 90+2m 亮，仍明显
    # 弱于默认前景色），而不是靠"更深的灰"。
    "dim": "\033[2m\033[37m",
}

# 旧键名 → 语义色（别名表；只映射，不再引入第五种颜色）
_ALIASES = {
    "cyan": "accent", "blue": "accent", "lightblue": "accent", "light_cyan": "accent",
    "light_blue": "accent", "title": "accent", "brand": "accent", "info": "accent",
    "green": "ok", "light_green": "ok", "success": "ok",
    "yellow": "warn", "light_yellow": "warn", "magenta": "warn", "light_magenta": "warn",
    "notice": "warn",
    "red": "err", "light_red": "err", "error": "err", "danger": "err",
    "gray": "dim", "grey": "dim", "light_gray": "dim", "black": "dim",
}

C = dict(_SEMANTIC)
for _legacy, _sem in _ALIASES.items():
    C[_legacy] = _SEMANTIC[_sem]
C["bold"] = "\033[1m"               # 粗体是**样式**不是颜色，保留给旧调用方
C["reset"] = _RESET

# 事件类型（与 client.py / backend events.py 一致）
EV_NODE_START = "node_start"
EV_NODE_PROGRESS = "node_progress"
EV_NODE_DONE = "node_done"
EV_NODE_PAUSED = "node_paused"
EV_CONFIRM_REQUIRED = "confirm_required"
EV_PARAM_REVIEW = "param_review"
EV_PLAN_FINAL = "plan_final"
EV_ERROR = "error"
EV_DONE = "done"
EV_RUN_PLAN = "run_plan"          # 本次运行的步数表（引擎开跑时下发，不打印）

# 主链节点总数：**只做**"后端没下发步数表时"的兜底分母（backend/pipeline/builder.py
# 的 interface 名叫法见那里的 PIPELINE_TITLES）。真值由 `run_plan` 事件随每次运行下发，
# 所以这张表变了也不会说谎 —— 它只是老后端 / 裸事件的退化路径。
#
# 【第 3 批 · 域 5】26 → **27**：`builder._main_nodes` 在 `WBSAuditNode`(R1) 与
# `NormBindNode` 之间插入了第 27 个节点 `QuantityAgentNode`（name="quantity_fill"，
# 补全各工序工程量）。⚠️ 这是**功能常量不是注释** —— `:333`/`:334` 拿它当分母，
# 不改就会出现用户可见的「第 8 / 26 步」「已完成 0/26」与实际 27 不符。
TOTAL_NODES = 27


def color(text, fg=None, bold=False):
    pre = ("\033[1m" if bold else "") + (C.get(fg, "") if fg else "")
    if not pre:
        return str(text)
    return pre + str(text) + _RESET


def clear_screen():
    print("\033[2J\033[H", end="", flush=True)


# ======================================================================
# 显示宽度 / 折行 / 对齐
# ======================================================================
def _disp_width(s):
    """显示宽度：CJK、全角、emoji 按 2 列计（终端对齐用）。"""
    w = 0
    for ch in str(s):
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F
                or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
                or o in (0x2014, 0x2015, 0x3000)):
            w += 2
        elif (0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF
                or 0x2B00 <= o <= 0x2BFF or 0x231A <= o <= 0x231B
                or 0x23F0 <= o <= 0x23FA or o == 0xFE0F):
            w += 2          # emoji / 符号：现代终端普遍占 2 列
        else:
            w += 1
    return w


def _dw(txt):
    """print_banner 时代的老名字，保留以免调用方炸（同一套宽度）。"""
    return _disp_width(txt)


def term_size():
    """(列, 行)。取系统实际终端尺寸，取不到用 80x24 —— 任何框线宽度都按它算。"""
    try:
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    except Exception:
        cols, rows = 80, 24
    return max(20, int(cols)), max(6, int(rows))


# ANSI 控制序列不占列：算宽度/对齐/折行时必须先忽略它们，否则彩色文字会被算宽
_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def strip_ansi(text):
    return _ANSI_RE.sub("", str(text))


def visible_width(text):
    """可见显示宽度（先剥 ANSI）。对齐、截断、折行都用它。"""
    return _disp_width(strip_ansi(text))


def rule(width=None, char="─"):
    """满宽细线（用户每句话 / 每道门前的视觉断点）。"""
    w = int(width) if width else term_size()[0]
    return color(char * max(10, w - 1), "accent")


def _fit(text, width, align="left"):
    """按显示宽度截断/补齐到恰好 width 列（中文算 2 列；ANSI 不占列；None 显示空）。"""
    s = "" if text is None else str(text).replace("\n", " ")
    if width <= 0:
        return ""
    if visible_width(s) > width:
        out, w, i = "", 0, 0
        while i < len(s):
            m = _ANSI_RE.match(s, i)
            if m:                                   # 转义码原样带走，不占列
                out += m.group(0)
                i = m.end()
                continue
            ch = s[i]
            i += 1
            cw = _disp_width(ch)
            if w + cw > width - 1:
                break
            out += ch
            w += cw
        s = out + "…" if width > 1 else "…"
        while visible_width(s) > width:
            s = s[:-1]
    pad = " " * max(0, width - visible_width(s))
    if align == "right":
        return pad + s
    if align == "center":
        left = " " * (len(pad) // 2)
        right = " " * (len(pad) - len(pad) // 2)
        return left + s + right
    return s + pad


def _num(value, dash="—"):
    """数值展示：None/'' → —；整数不带 .0；其它保留 1 位小数。"""
    if value is None or value == "":
        return dash
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(f - round(f)) < 1e-9:
        return "{:,}".format(int(round(f)))
    return "{:,.1f}".format(f)


def _cell(value, dash="—"):
    """单元格：None / 空串 → —（绝不打印 None）。"""
    if value is None:
        return dash
    s = str(value).strip()
    return s if s else dash


def wrap_cjk(text, width=58):
    """按**显示宽度**折行（中文 2 列），并在常见标点后优先断行。"""
    text = str(text or "").strip()
    if not text:
        return []
    lines, cur, curw = [], "", 0
    for ch in text:
        cw = _disp_width(ch)
        if curw + cw > width and cur:
            # 优先在标点后断（读起来更自然）
            cut = max(cur.rfind(p) for p in "，。；、：）】」")
            if cut >= max(4, len(cur) // 3):
                lines.append(cur[:cut + 1])
                cur = cur[cut + 1:]
                curw = _disp_width(cur)
            else:
                lines.append(cur)
                cur, curw = "", 0
        cur += ch
        curw += cw
    if cur:
        lines.append(cur)
    return lines


# ---------------- 竖版化（把"一坨"摘要拆成一条一行） ----------------
_SEV_SPLIT = re.compile(r"(?=\[(?:HIGH|MID|MED|LOW|高|中|低)\])")


def verticalize(text):
    """把节点摘要拆成条目列表。

    为什么需要：`wbs_agent` 等节点过去把多条问题用 ` · ` 拼成**一整行**
    （还各自截断到 40 字）——实测在终端里糊成一片，完全没法读。
    这里统一改成一条一行；能识别严重度标记时按标记切，否则按 ` · ` 切。
    """
    s = str(text or "").strip()
    if not s:
        return []
    parts = [p.strip(" ·") for p in _SEV_SPLIT.split(s) if p.strip(" ·")]
    if len(parts) <= 1:
        parts = [p.strip() for p in s.split(" · ") if p.strip()]
    if len(parts) <= 1:
        return [s]
    return parts


def _body_width():
    """正文可用宽度（留出左右各 2 列）。"""
    cols, _rows = term_size()
    return max(24, cols - 4)


# ======================================================================
# 开场白（**简洁优先**：品牌只有一行，使用提示两行）
# 用户原话：「把学校，小组，口号，只留"海之子·建策BuildPlan"，简洁优先。」
# ======================================================================
HOWTO_LINES = [
    "直接说一句项目和条件就行 —— 每道门都会给你看依据。",
    "例：1 栋 3 层框架结构办公楼，总建筑面积 1500 ㎡，钢筋约 90 吨，开工 2026-03-01",
    "/help 看命令 · /verbose 看完整过程 · /quit 退出",
]


def banner_text(width=None, backend=None):
    """开场白纯文本（不含 ANSI 之外的定位码）。

    **简洁优先**（用户拍板）：品牌只有一行「海之子 · 建策 BuildPlan」，
    **不要学校 / 小组 / 口号 / 英文标语**；下面只留两行使用提示 + 后端状态行。

    · 品牌行按**显示宽度**居中（中文 2 列）；终端 < 60 列退化为左对齐；
    · 使用提示按实际宽度折行；
    · 后端状态放最后一行，用 dim，不抢戏。
    """
    cols = int(width) if width else term_size()[0]
    try:
        import branding as _b
        lines = _b.welcome_lines()
    except Exception:
        lines = ["海之子 · 建策 BuildPlan"]

    out = []
    centered = cols >= 60
    if not centered:
        out.append(color("（终端宽度 %d 列 < 60，开场白左对齐）" % cols, "dim"))
    for i, ln in enumerate(lines):
        pad = " " * max(0, (cols - _disp_width(ln)) // 2) if centered else ""
        strong = i == 0
        out.append(pad + color(ln, "accent", bold=strong))

    out.append("")
    strong_howto = {0: "accent", len(HOWTO_LINES) - 1: "accent"}
    for i, ln in enumerate(HOWTO_LINES):
        chunk = wrap_cjk(ln, max(24, cols - 6)) or [ln]
        for j, seg in enumerate(chunk):
            out.append(color(("    " + seg) if j else seg, strong_howto.get(i, "dim")))
    if backend:
        out.append("")
        out.append(color("后端：%s" % backend, "dim"))
    return "\n".join(out)


def print_banner(width=None):
    """老接口：直接打印开场白（console 现在走 tui.out(banner_text())）。"""
    print(banner_text(width))


def progress_bar(percent, width=24):
    percent = max(0, min(100, int(percent or 0)))
    filled = percent * width // 100
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {percent}%"


# ======================================================================
# 折叠进度（规格 §B5：默认不展开 27 个节点）
# ======================================================================
class FoldState:
    """折叠状态区：只保留最近 3 个"正在跑/最近跑"的节点 + 步数。

    为什么不全部展开：用户原话「一定需要把所有的进度都展开吗，不能像 claude 一样
    做一个展开项，界面只展示检验门的返回内容，以及两个或者三个目前正在跑的节点，
    全都默认堆出来是不是有些不必要」。完整事件流仍在（`/verbose` 打开即全量打印）。

    第 32 轮的两处收口（用户实测：「什么叫已完成 0/26，哪来的 26」）：
      · 步数与界面名**都由后端随 `run_plan` 下发**，这里不再自己数节点、也不再
        拿"见过的顺序"当步号（跳节点 / 重入时会数错）；
      · 分母只在"真的跑过多步"时才显示：一次闲聊只跑 1 步，就不该出现 1/26。
    """

    def __init__(self, total=TOTAL_NODES, keep=3):
        self.total = max(1, int(total or TOTAL_NODES))
        self.keep = keep
        self.recent = []          # [(index, node, label, progress)]
        self.completed = 0
        self.current = None
        self.current_index = None
        self.current_label = ""
        self.current_progress = None
        self.steps = {}           # node -> (后端步号, 界面名)：run_plan 下发
        self.seen = []            # 见过的节点名（按启动顺序；老路径的步号兜底）
        self.done_names = []
        self.ran_nodes = 0        # 本次运行真正启动过的节点数（单步运行 = 1）
        self.reported_nodes = []  # 发过 node_progress 的节点（收尾小结用来算"哪几步被合并了"）

    def reset_run(self):
        """每次用户发一句话就是一次新运行 —— 状态区跟着归零（步数表保留）。"""
        self.recent = []
        self.completed = 0
        self.current = None
        self.current_index = None
        self.current_label = ""
        self.current_progress = None
        self.done_names = []
        self.seen = []
        self.ran_nodes = 0
        self.reported_nodes = []

    # ---- 步数表 / 步号 ----
    def set_steps(self, steps):
        """`run_plan.steps` → {node: (步号, 界面名)}。形参不合法时**保持原样**（不瞎猜）。"""
        table = {}
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
            table[name] = (idx, str(item.get("title") or name))
        if table:
            self.steps = table
            self.total = max(self.total, max(i for i, _t in table.values()))
        return bool(table)

    def step_of(self, node):
        """(步号, 界面名)。没有步数表 → 退化成"见过第几个"，拿不到 → (None, node)。"""
        name = str(node or "")
        if name in self.steps:
            return self.steps[name]
        if name in self.seen:
            return self.seen.index(name) + 1, (self.current_label or name)
        return None, name

    def step_count(self):
        """本次运行的总步数（有下发就以后端为准）。"""
        return max(len(self.steps), 1) if self.steps else self.total

    def show_total(self):
        """要不要显示分母。只跑过一步（闲聊/问答）就不显示 —— 那句 0/26 是用户的原话。"""
        return self.ran_nodes >= 2 and self.step_count() >= 2

    # ---- 事件驱动 ----
    def track(self, event, data):
        data = data or {}
        node = str(data.get("node") or "")
        if event == EV_RUN_PLAN:
            self.set_steps(data.get("steps"))
            return
        if event == EV_NODE_START:
            idx, label = self.step_of(node)
            if node not in self.seen:
                self.seen.append(node)
                self.ran_nodes += 1
            self.current = node
            # 没有步数表时用事件里带的 title（老后端 / 裸事件的退化路径）
            self.current_index = idx
            self.current_label = label if (label and label != node) \
                else str(data.get("title") or node or "?")
            self.current_progress = None
            self._push(idx or len(self.seen), node, self.current_label, None)
        elif event == EV_NODE_PROGRESS:
            prog = data.get("progress")
            if node and node not in self.reported_nodes:
                self.reported_nodes.append(node)
            if node and node == self.current:
                self.current_progress = prog
                self._push(self.current_index or len(self.seen), node,
                           self.current_label or node, prog)
        elif event == EV_NODE_DONE:
            self.completed += 1
            if node:
                self.done_names.append(node)
            if node == self.current:
                self.current = None
                self.current_progress = None
        if self.seen and len(self.seen) > self.total:
            self.total = len(self.seen)      # 实际比常量多 → 以实际为准，不说谎

    def _push(self, idx, node, label, progress):
        self.recent = [r for r in self.recent if r[1] != node]
        self.recent.append((idx, node, label, progress))
        self.recent = self.recent[-self.keep:]

    def recent_heads(self):
        """最近几个节点的完整写法（`第 8 / 26 步 · 编制 WBS 分工`），给别的层复用。"""
        return [self._step_head(idx, label or node or "?")
                for idx, node, label, _p in self.recent]

    def _step_head(self, idx, label):
        """一行里的节点写法：有步号与分母时 `第 3 / 26 步 · 编制 WBS 分工`。"""
        if idx and self.show_total():
            return "第 %d / %d 步 · %s" % (idx, self.step_count(), label)
        if idx:
            return "第 %d 步 · %s" % (idx, label)
        return label

    # ---- 渲染 ----
    def lines(self, verbose=False):
        heads = self.recent_heads()
        line1 = "⏳ " + " · ".join(heads) if heads else "⏳ 等待节点启动"
        # 分母只在跑过多步时出现：一次闲聊只有 1 步，不该写成 0/26
        line2 = ("已完成 %d / %d 步" % (self.completed, self.step_count())
                 if self.show_total() else "已完成 %d 步" % self.completed)
        if self.current_label and self.current_progress is not None:
            line2 += " · %s %s%%" % (self.current_label, self.current_progress)
        elif self.current_label:
            line2 += " · 正在跑 %s" % self.current_label
        if verbose:
            line2 += "　（详细模式，/verbose 关闭）"
        return line1, line2


def is_progress_event(event, data=None):
    """只进状态区、默认不打印的事件。

    `node_done` 例外：摘要里带口径类警告时**必须**打出来（规格 §B5.2）。
    `run_plan`（本次运行的步数表）同样只喂状态区 —— 用户不需要看到一张节点清单。
    """
    if event in (EV_NODE_START, EV_NODE_PROGRESS, EV_RUN_PLAN):
        return True
    if event == EV_NODE_DONE:
        return not has_warning(data or {})
    return False


def is_gate_event(event):
    """需要"空一行 + 细线"隔开的块：门 / 结果 / 错误。"""
    return event in (EV_NODE_PAUSED, EV_CONFIRM_REQUIRED, EV_PARAM_REVIEW,
                     EV_ERROR, EV_PLAN_FINAL)


def event_action(event, data=None, verbose=False):
    """折叠策略（console 与 _probe_tmp/show_new_ui.py **共用同一份判定**）。

    返回 'fold'（只进状态区）| 'print'（完整打印）| 'skip'（彻底忽略）。
    """
    data = data or {}
    if event in ("ping", "", None):
        return "skip"
    if not verbose and is_progress_event(event, data):
        return "fold"
    return "print"


# 口径类警告：摘要里出现这些才算"必须让用户看见"的
_WARN_PAT = re.compile(
    r"⚠|警告\s*[1-9]\d*\s*条|未锚定|未纳入|超出|超额|不通过|未通过|缺证据|数据缺|冲突|降级")


def has_warning(data):
    """事件载荷里是否有口径类警告（用于折叠时"不丢信息"）。"""
    if not isinstance(data, dict):
        return False
    for key in ("warnings", "schedule_warnings"):
        val = data.get(key)
        if isinstance(val, (list, tuple)) and val:
            return True
    text = " ".join(str(data.get(k) or "") for k in ("summary", "output_summary", "message"))
    if _WARN_PAT.search(text):
        return True
    for it in (data.get("issues") or []):
        if isinstance(it, dict) and str(it.get("severity") or "").upper() in ("HIGH", "高"):
            return True
    return False


def warning_lines(data):
    """从任意事件里捞出可打印的警告行（去重、截断保护）。"""
    out = []
    for key in ("warnings", "schedule_warnings"):
        for w in (data.get(key) or []):
            t = str(w).strip()
            if t and t not in out:
                out.append(t)
    return out


# ======================================================================
# 参数 / 文件 / 细度 三张老门（保持"人话 + 编号选项"的既有语义）
# ======================================================================
_PARAM_LABELS = {
    "project_name": "项目名称",
    # 【G5 / W4-U】单位规范形 `m²`：这是**打给用户看的产物文案**（输出侧），
    # `㎡`(U+33A1) 只许留在输入侧的识别表里。
    # ⚠️ 与 `backend/pipeline/nodes/boundary.py::PARAM_LABELS` 是**两张表**
    # （终端进程不 import 后端），改一处必须同步改另一处。
    "total_area": "总建筑面积(m²)",
    "total_concrete": "混凝土总量(m³)",
    "total_rebar": "钢筋总量(吨)",
    "total_earthwork": "土方总量(m³)",
    # 【第 2 批 · 域 2 / 2.1】基础类型 —— 与后端 `boundary.PARAM_LABELS` 同步登记。
    "foundation_type": "基础类型",
    # 【W4-U 追加-3】模板 / 砌体
    "total_formwork": "模板总量(m²)",
    "total_masonry": "砌体总量(m³)",
    # 【第 2 批 · 域 2 / 2.2】填充墙（m³）+ 桩。
    # ⚠️ `total_pile` 的标签**刻意不带单位**（这个键不预设单位，用户给什么单位就收什么）——
    # 与后端 `boundary.PARAM_LABELS` 逐字一致，改一处必须同步改另一处。
    "total_infill_wall": "填充墙(m³)",
    "total_pile": "桩",
    "building_type": "建筑类型",
    "structure_type": "结构形式",
    "planned_start_date": "开工日期",
    "quality_target": "质量目标",
    "safety_target": "安全目标",
    # 第 32 轮补：这三个原来在门上露的是英文键名（用户实测："不要刻意使用
    # 一些英文和专业术语"）
    "building_count": "栋数",
    "floors": "层数",
    # 用户显式分段规则（用户裁定 2026-09-21 第八项）
    "segment_rule": "施工段划分规则",
    "segment_rule_pending": "施工段划分（待确认）",
    # 【第 2 批收口】A6 的两条通道（明确排除项 / 分层面积）原来**既没有中文名、
    # 也没进 `_OPTIONAL_PARAMS`**，于是终端参数表直接甩出
    #   `exclusions：[]`
    #   `floor_areas：{'source': 'average_assumption', 'unit': ..., 一大坨嵌套字典}`
    # —— 与用户投诉的「不要刻意使用一些英文和专业术语」是同一条毛病（键名裸奔 +
    # 把 Python 数据结构原样打给人看）。补中文名并归入"不填也能编"那一档。
    "exclusions": "明确排除项",
    "floor_areas": "分层面积",
}

# 参数值的枚举码 → 中文（抽取器给的是 KB 里的 id）
_PARAM_VALUE_LABELS = {
    "residential": "住宅", "office": "办公", "commercial": "商业", "hospital": "医院",
    "school": "学校", "industrial": "工业", "warehouse": "仓储", "hotel": "酒店",
    "shear_wall": "剪力墙结构", "frame": "框架结构", "frame_shear_wall": "框架-剪力墙结构",
    "masonry": "砌体结构", "steel": "钢结构", "frame_core": "框架-核心筒结构",
}

# 用户不填也能编的参数：为空时**不占版面**（只报一句"这些会用推算/默认值"）。
# 【第 2 批 · 域 2】删 `total_precast` / `total_wall`；增 `total_infill_wall` / `total_pile`
# （两者都在后端 `boundary.FALLBACK_KEYS` 里 —— 缺了不拦编制，所以在这里也属"不填也能编"）。
# ⚠️ `foundation_type` **不在**这里：它是硬必要键（缺了要中断），不是可选参数。
_OPTIONAL_PARAMS = ("project_name", "quality_target", "safety_target",
                    "total_infill_wall", "total_pile", "total_concrete", "total_rebar",
                    "total_earthwork",
                    # 【第 2 批收口】A6 两条通道：缺省值就是空的 `[]` / 一个"均摊假设"
                    # 字典，不是用户必须填的项目事实 —— 让它们空着时别占版面。
                    "exclusions", "floor_areas")


def _floor_areas_text(v):
    """`floor_areas` 的内部结构 → 一句人话（**绝不 repr 嵌套字典**）。

    【第 2 批收口】实测原文（终端参数表）：
        `floor_areas：{'source': 'average_assumption', 'unit': 'm²', 'buildings':
         {'default': {'floors': {}, 'floor_count': 38, 'sum_area': 128000.0, ...
         'needs_review': False, 'notes': ['均摊假设：总建筑面积 128000 ÷ 38 层 = ...']}}`
    这是把**内部数据结构**原样打给用户看。它永远不为空（即使用户什么都没给，
    也会带一个"均摊假设"骨架），所以进 `_OPTIONAL_PARAMS` 也躲不掉 —— 必须专门渲染。
    """
    if not isinstance(v, dict):
        return v
    notes = [str(x) for x in (v.get("notes") or []) if str(x).strip()]
    if notes:
        return "；".join(notes)
    n = sum(len(b.get("floors") or {}) for b in (v.get("buildings") or {}).values()
            if isinstance(b, dict))
    src = {"average_assumption": "未给逐层面积，按总面积均摊",
           "user": "用户给出", "doc": "资料给出"}.get(str(v.get("source") or ""), "")
    out = "，".join(x for x in (src, ("共 %d 个层段" % n) if n else "") if x)
    return out or "已生成"


def _param_value(key, value):
    """参数值 → 给人看的写法（枚举码转中文；查不到原样返回）。"""
    # 【第 2 批收口】两条 A6 通道是**内部结构**（不是给人看的项目事实）：
    # 直接 `repr` 就是裸键名 + Python 数据结构，与用户投诉的
    # 「不要刻意使用一些英文和专业术语」同一条毛病。各给一句人话。
    if key == "floor_areas":
        return _floor_areas_text(value)
    if key == "exclusions" and isinstance(value, (list, tuple)):
        return "、".join(str(x) for x in value) or "无"
    if isinstance(value, str):
        return _PARAM_VALUE_LABELS.get(value.strip().lower(), value)
    return value


def format_params(params, hide_empty=None):
    """把参数字典转成可读的多行文本。

    · 键名一律走 `_PARAM_LABELS`（内部键名 `total_concrete` 不再露给用户）；
    · `hide_empty`（默认 `_OPTIONAL_PARAMS`）里的键**为空时不占行** ——
      实测反馈过一张 14 行的表里 9 行是「—（缺，请补）」，看着像报错；
      它们仍会由门上那句「以下参数缺失…」统一交代；
    · 值为枚举码时转中文（`residential` → 住宅）。
    """
    if not isinstance(params, dict) or not params:
        return color("  （未提取到参数）", "dim")
    hide = _OPTIONAL_PARAMS if hide_empty is None else tuple(hide_empty or ())
    lines = []
    for k, v in params.items():
        # 【第 2 批收口】空容器也算"空"：`exclusions` 的缺省值是 `[]`、`floor_areas`
        # 可能是 `{}` —— 只判 `None` / `""` 时它们会被当成"有值"照常渲染，
        # 于是终端打出 `exclusions：[]` 这种话。
        empty = (v is None or v == ""
                 or (isinstance(v, (list, dict, tuple)) and not v))
        if empty and k in hide:
            continue
        label = _PARAM_LABELS.get(k, k)
        if empty:
            lines.append(color("    %s：—  （缺，请补）" % label, "warn"))
        else:
            lines.append("    %s：%s" % (label, _param_value(k, v)))
    return "\n".join(lines) or color("  （未提取到参数）", "dim")


def params_note(data):
    """取 params.note（老字段可能没有）。"""
    params = data.get("params") or {}
    return params.get("note") or ""


def render_repairs(repairs, repair_limit_note=""):
    """「一键修复」编号选择题（node_paused 的可选字段 repairs）。

    规格 §3：接在 `issues` 之后、竖版编号。`repairs` 为空/缺失 → 返回 []，
    调用方什么都不打（**老界面一字不变**）。
    超过次数上限时 repairs 为空但带上 repair_limit_note → 只打那句说明。
    """
    out = []
    items = [x for x in (repairs or []) if isinstance(x, dict) and x.get("key")]
    free_no = _free_option_no(items)
    if items:
        out.append(color("  你可以让系统自己修（不需要你懂技术）：", "accent"))
        for i, it in enumerate(items, 1):
            label = str(it.get("label") or it.get("key") or "").strip()
            out.append(color("    [%d] %s" % (i, label), "dim"))
            hint = str(it.get("hint") or "").strip()
            if hint:
                for w in wrap_cjk(hint, max(24, _body_width() - 10)):
                    out.append(color("        " + w, "dim"))
        out.append(color("    [%d] 我自己写意见（选这个就直接输入你的修改要求）" % free_no, "dim"))
    note = str(repair_limit_note or "").strip()
    if note:
        out.append(color("    " + _fill_free(note, free_no, show_no=bool(items)), "warn"))
    return out


def _free_option_no(repairs):
    """「我自己写意见」的编号 = 修复选项数 + 1（与 confirmer._pick_repair 同一口径）。"""
    return len([x for x in (repairs or []) if isinstance(x, dict) and x.get("key")]) + 1


def _fill_free(text, free_no, show_no=True):
    """后端写的 `{FREE}` 占位符 → 真实的"我自己写意见"编号。

    后端**不知道**界面上最终会出现几个修复选项（选项数由当时的问题决定），
    所以它只写占位符；编号由认识界面的渲染层填 —— 避免出现"选 [4]"而界面只有 3 项。
    `show_no=False`（修复选项已用满上限、界面上不再有编号列表）时，
    `（{FREE}）` 整块去掉，改成"直接输入你的意见"，不留下一个点了也没用的编号。
    """
    text = str(text or "")
    if not show_no:
        return text.replace("（{FREE}）", "").replace("{FREE}", "")
    return text.replace("{FREE}", "[%d]" % free_no)


def render_issue_info_requests(notes, repairs=None):
    """无法自动修的问题 → 「这条需要你提供信息：<问什么>」（规格 §4 的最后一条）。"""
    free_no = _free_option_no(repairs)
    out = []
    for n in (notes or []):
        text = _fill_free(str(n or "").strip(), free_no)
        if not text:
            continue
        first = True
        for w in wrap_cjk(text, max(24, _body_width() - 6)):
            out.append(color(("  " if first else "    ") + w, "warn"))
            first = False
    return out


def _ctx_summary(data):
    ctx = data.get("context")
    if isinstance(ctx, dict) and ctx.get("summary"):
        return ctx["summary"]
    if isinstance(ctx, str) and ctx:
        return ctx
    return ""


def _issue_lines(issues, indent="   ", width=None):
    """结构化问题清单 → 一条一行（编号 + 严重度 + 维度 + 完整描述，不截断）。"""
    width = width or _body_width()
    lines = []
    for i, it in enumerate(issues, 1):
        if not isinstance(it, dict):
            continue
        sev = str(it.get("severity") or "").strip()
        dim = str(it.get("dimension") or "").strip()
        finding = str(it.get("finding") or it.get("note") or "").strip()
        tag = f"[{sev}]" if sev else ""
        head = ("%s %s" % (tag, dim)).strip()
        if head:
            lines.append("%s%2d. %s" % (indent, i, head))
            pad = indent + "    "
        else:
            pad = "%s%2d. " % (indent, i)
        for w in wrap_cjk(finding, max(20, width - len(indent) - 6)):
            lines.append(pad + w)
    return lines


def _summary_lines(text, indent="   ", width=None):
    """把后端给的一段摘要渲染成竖版（先按行、再按条目拆，最后折行），不截断。"""
    width = width or _body_width()
    out = []
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        items = verticalize(raw)
        if len(items) == 1:
            for w in wrap_cjk(items[0], max(20, width - len(indent) - 4)):
                out.append(indent + w)
        else:
            for i, it in enumerate(items, 1):
                for j, w in enumerate(wrap_cjk(it, max(20, width - len(indent) - 5))):
                    out.append(("%s%2d. %s" % (indent, i, w)) if j == 0
                               else (indent + "    " + w))
    return out


def _summary_verbatim(text, indent="   ", width=None):
    """摘要**逐行**打出（不编号）：给审计门的退化路径用。

    为什么和 `_summary_lines` 分开：审计摘要里有「【第 3 轮 · Word 草案审计（不含图表）】」
    这种标题行，被 `verticalize` 按 ` · ` 拆开会变成"1. 【第 3 轮 / 2. Word 草案审计】"，
    反而更难读。标题行按原样一行一行打最稳。
    """
    width = width or _body_width()
    out = []
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        for w in wrap_cjk(raw, max(20, width - len(indent))):
            out.append(indent + w)
    return out


def render_param_review(data):
    """param_review 事件 → 全量参数清单（purpose=param/doc/plan_level 三套提示）。"""
    data = data or {}
    msg = data.get("message") or "请人工复核提取的参数："
    params = data.get("params") or {}
    purpose = data.get("purpose") or "param"

    if purpose == "plan_level":
        return _render_plan_level(msg, data)
    if purpose == "doc":
        return _render_doc_load(msg, data)
    if purpose == "audit":
        # 审计门正文由 confirmer 打印（正文与它的输入提示是同一次交互，见 render_event）
        return render_audit_gate(data)

    lines = [color(f"👁 参数人工复核门｜{msg}", "warn", bold=True)]
    lines.append(format_params(params))
    note = params_note(data)
    if note:
        lines.append(color("  提示：" + note, "dim"))
    lines.append(color("    输 Y 通过；或直接输入要修正/补充的项目参数后回车", "dim"))
    return "\n".join(lines)


def _render_doc_load(msg, data):
    """**项目文件加载门**（purpose=doc）。

    为什么单独写：老版本把 `files: []` / `unreadable: []` 这类**原始字段名**直接甩给
    用户，配上"你刚才输入的是否为全部项目数据？"这种反问，实测用户完全看不懂在问什么
    （"这一块看不懂啊有点"）。这里改成：**先说清为什么问、再给编号的选项**。
    """
    params = data.get("params") or {}
    files = params.get("files") or []
    unreadable = [f for f in (params.get("unreadable") or []) if f]
    reason = params.get("reason") or ("unreadable" if unreadable else "no_path")

    lines = [color("📁 项目文件加载门", "warn", bold=True)]
    if reason == "unreadable":
        lines.append(color("  你在输入里给了文件路径，但我读不到内容：", "dim"))
        for f in unreadable[:5]:
            lines.append(color(f"     · {f}", "dim"))
        lines.append(color("  常见原因：路径写错、文件被别的程序占用、或是不支持的格式"
                           "（.xlsx / .pdf 读不了，.docx / .txt / .md / .csv / .json 可以）。",
                           "dim"))
        lines.append(color("  怎么回：", "accent"))
        lines.append("     ① 重新发一次路径（可以加引号）")
        lines.append("     ② 打 Y → 不管这个文件了，只用你输入里的参数继续编制")
        lines.append("     ③ 输入 /abort → 中止本次运行")
    else:
        lines.append(color("  你这句话里没有出现本地文件路径，所以我手上只有你打的这行字。",
                           "dim"))
        lines.append(color("  想先确认一下：这行字是不是已经包含了编计划要用的项目信息？",
                           "dim"))
        lines.append(color("  怎么回：", "accent"))
        lines.append("     ① 打 Y → 是，就用你输入里的参数编制")
        lines.append("     ② 输入文件路径 → 参数在这个文件里（支持 .docx / .txt / .md / .csv / .json）")
        lines.append("     ③ 输入 /abort → 中止本次运行")
    if files:
        lines.append(color("  已在输入里识别到的路径：", "dim"))
        for f in files[:5]:
            lines.append(color("     · %s" % f, "dim"))
    lines.append(color("  提示：路径可以直接粘在中文句子里，加不加引号都行。", "dim"))
    return "\n".join(lines)


def _render_plan_level(msg, data):
    """计划**展示粒度**门：编号选项 + 真实行数（用户只要敲一个数字）。

    第 38 轮（用户原话）：「不要做成"X+X"两轴选项，**直接给用户六个选项**」——
    所以编号 1-6 每一个都是**完整的一档**：

        1. 按层 · 工序级（细）      2. 按层 · 工种级（粗）
        3. 每 5 层一组 · 工序级（细） 4. 每 5 层一组 · 工种级（粗）
        5. 整栋 · 工序级（细）      6. 整栋 · 工种级（粗）

    早期（第 32 轮）的跨轴编号（1-3 楼层、4-5 深度）仍能渲染，见下面的兼容分支；
    后端 `_parse_picker` 对两份编号都认。
    """
    lines = [color("👁 计划展示粒度门", "warn", bold=True)]
    lines.append(color("  " + (msg or "请选择计划展示粒度"), "dim"))
    opts = data.get("options") or {}
    matrix = data.get("matrix") or {}
    picker = data.get("picker") or {}

    groups = opts.get("floor_grouping") or []
    depths = opts.get("depth") or []
    g_label = dict((o.get("key"), o.get("label")) for o in groups)
    d_label = dict((o.get("key"), o.get("label")) for o in depths)
    rec = opts.get("recommend") or {}
    rec_nos = set(int(x) for x in (picker.get("recommend") or []))

    items = picker.get("options") or []
    combos = [o for o in items if o.get("axis") == "combo"]
    if combos:
        # 第 38 轮（用户原话）：「不要做成"X+X"两轴选项，**直接给用户六个选项**」——
        # 一个编号 = 一种楼层分段 + 一种工序细度，敲一个数字就定完，不用先选轴再选档。
        lines.append(color("  六个选项（三种楼层分段 × 两种工序细度，各带真实行数）：",
                           "accent"))
        width = max(16, min(30, max(visible_width(str(o.get("label") or ""))
                                    for o in combos)))
        for o in combos:
            no = o.get("no")
            star = " ★推荐" if int(no or 0) in rec_nos else ""
            lines.append("     %s. %s→ %s 行  %s" % (
                no, _fit(o.get("label"), width),
                _fit(o.get("rows"), 5, "right"), str(o.get("note") or "")))
            if star:
                lines[-1] = lines[-1][:200] + color(star, "ok")
    elif items:
        # 第 32 轮的老格式（两个轴各占几个号）：留着兼容旧后端/旧回放，别删。
        # 编号列 + 名字 + 真实行数 + 一句人话；推荐项标 ★
        axis_titles = {"floor_grouping": "① 楼层怎么分段？（行数主要由它决定）",
                       "depth": "② 工序拆到多细？"}
        shown_axis = None
        width = max(9, min(18, max(visible_width(str(o.get("label") or ""))
                                    for o in items)))
        for o in items:
            axis = o.get("axis")
            if axis != shown_axis:
                shown_axis = axis
                lines.append(color("  " + axis_titles.get(axis, axis or ""), "accent"))
            no = o.get("no")
            star = " ★推荐" if int(no or 0) in rec_nos else ""
            note = str(o.get("note") or "")
            lines.append("     %s. %s→ %s 行%s" % (
                no, _fit(o.get("label"), width),
                _fit(o.get("rows"), 5, "right"), ("  " + note) if note else ""))
            if star:
                lines[-1] = lines[-1][:200] + color(star, "ok")
    else:
        # 退化路径（老后端没有 picker）：保持改造前的两段清单
        if groups:
            lines.append(color("  ① 楼层分组（行数主杠杆）", "accent"))
            for o in groups:
                lines.append("     %s %s 行" % (_fit(o.get("label"), 14),
                                                _fit(o.get("rows"), 5, "right")))
        if depths:
            lines.append(color("  ② 工序拆解深度", "accent"))
            for o in depths:
                lines.append("     %s %s 行" % (_fit(o.get("label"), 14),
                                                _fit(o.get("rows"), 5, "right")))

    if matrix and not combos:
        # 六个组合选项本身就**是**这张矩阵（每格都列了行数），不重复印一遍。
        lines.append(color("  行数矩阵（深度 × 楼层分组）", "dim"))
        for d in ("component", "coarse"):
            row = matrix.get(d) or {}
            if not row:
                continue
            lines.append("     %s 按层 %4s ｜ 每5层 %4s ｜ 整栋 %4s"
                         % (_fit(d_label.get(d, d), 12), row.get("per_floor"),
                            row.get("per_5"), row.get("whole")))

    if rec:
        lines.append(color("  推荐：%s + %s → %s 行"
                           % (d_label.get(rec.get("depth"), rec.get("depth")),
                              g_label.get(rec.get("floor_grouping"),
                                          rec.get("floor_grouping")),
                              rec.get("rows")), "ok"))
    if opts.get("recommend_reason"):
        lines.append(color("  " + str(opts["recommend_reason"]), "dim"))
    if opts.get("blocked"):
        lines.append(color("  ⚠ " + str(opts["blocked"]), "warn"))

    note = (params_note(data) or "")
    if note:
        lines.append(color("  提示：" + note, "dim"))
    if combos:
        lines.append(color("    输入一个代号即可（如 3）；改主意就打最后一个（如「2 4」= 用 4 号）；"
                           "直接回车=用推荐值", "dim"))
    elif items:
        lines.append(color("    输入一个代号即可（如 2）；两个都定就输入两个（如 2 4）；"
                           "直接回车=用推荐值", "dim"))
    else:
        lines.append(color("    输 Y=用推荐值；或直接输入，如「整栋」「每5层」"
                           "「工种级 整栋」「按层」后回车", "dim"))
    return "\n".join(lines)


# ======================================================================
# 【R1】WBS 树 —— 规格 §D1 契约
# ======================================================================
def _leaf_line(leaf, cols):
    name_w = max(12, min(28, cols - 44))
    qty = _num(leaf.get("qty") if leaf.get("qty") is not None else leaf.get("quantity"))
    unit = _cell(leaf.get("unit"), "")
    dur = leaf.get("duration_days")
    dur_txt = "—" if dur is None or dur == "" else "%s 天" % _num(dur)
    return "%s %s %s %s" % (
        _fit(_cell(leaf.get("id")), 10),
        _fit(_cell(leaf.get("name")), name_w),
        _fit(("%s %s" % (qty, unit)).strip(), 14, "right"),
        _fit(dur_txt, 7, "right"),
    )


def render_wbs_tree(tree, cols=None):
    """D1 `wbs_tree` → 缩进树（阶段 → 工作包 → 叶子）。

    字段缺失一律跳过该级；叶子名称/编号按列宽截断（超出写省略号），
    阶段/叶子被后端截断时用 `truncated_leaves` 报数（规格：超长要写「…另有 N 条」）。
    """
    cols = cols or term_size()[0]
    if not isinstance(tree, dict) or not tree:
        return []
    counts = tree.get("counts") if isinstance(tree.get("counts"), dict) else {}
    phases = [p for p in (tree.get("phases") or []) if isinstance(p, dict)]
    lines = []
    shown = 0
    total_leaves = counts.get("leaves")

    scale = "阶段 %s 个 · 工作包 %s 个 · 叶子 %s 条" % (
        _num(counts.get("phases")), _num(counts.get("work_packages")), _num(total_leaves))
    lines.append(color("  规模：" + scale, "accent"))

    if not phases:
        lines.append(color("  （后端没有提供 WBS 树明细）", "dim"))
    for pi, ph in enumerate(phases):
        wps = [w for w in (ph.get("work_packages") or []) if isinstance(w, dict)]
        n_leaf = sum(len([l for l in (w.get("leaves") or []) if isinstance(l, dict)])
                     for w in wps)
        last = (pi == len(phases) - 1)
        lines.append("  %s %s  （%d 个工作包 / %d 条叶子）"
                     % ("└─" if last else "├─", _cell(ph.get("phase")), len(wps), n_leaf))
        pad = "  " + ("   " if last else "│  ")
        for wi, wp in enumerate(wps):
            leaves = [l for l in (wp.get("leaves") or []) if isinstance(l, dict)]
            wlast = (wi == len(wps) - 1)
            lines.append("%s%s %s %s"
                         % (pad, "└─" if wlast else "├─",
                            _fit(_cell(wp.get("id")), 8), _cell(wp.get("name"))))
            spad = pad + ("   " if wlast else "│  ")
            for leaf in leaves:
                shown += 1
                lines.append(spad + "· " + color(_leaf_line(leaf, cols), "dim"))
        if pi >= 5 and pi < len(phases) - 1:
            lines.append("  │  …另有 %d 个阶段未展开" % (len(phases) - pi - 1))
            break

    truncated = tree.get("truncated_leaves")
    try:
        truncated = int(truncated) if truncated else 0
    except (TypeError, ValueError):
        truncated = 0
    if truncated > 0:
        lines.append(color("  …另有 %s 条叶子未展开（后端只带前 %d 条；完整清单在交付物里）"
                           % (_num(truncated), shown), "warn"))
    if phases and counts.get("phases") and not truncated:
        try:
            missing_ph = int(counts.get("phases")) - len(phases)
        except (TypeError, ValueError):
            missing_ph = 0
        if missing_ph > 0:
            lines.append(color("  …另有 %d 个阶段未展开" % missing_ph, "warn"))
    return lines


def _r1_from_highlights(hl):
    """没有 wbs_tree 时，用后端既有的 highlights 退化渲染（老版本也能看）。"""
    lines = []
    if not isinstance(hl, dict) or not hl:
        return lines
    lines.append(color("  规模：阶段 %s 个 · 叶子 %s 条 · 细度 %s"
                       % (_num(hl.get("phases")), _num(hl.get("leaves")),
                          _cell(hl.get("plan_level"))), "accent"))
    per_phase = hl.get("per_phase") or []
    if per_phase:
        lines.append(color("  各阶段行数（行数降序）：", "dim"))
        for item in per_phase[:10]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                lines.append("    %s %s 条" % (_fit(item[0], 20), _fit(item[1], 5, "right")))
    by_unit = hl.get("by_unit") or {}
    if isinstance(by_unit, dict) and by_unit:
        pairs = sorted(by_unit.items(), key=lambda kv: -(kv[1] or 0))[:6]
        lines.append(color("  工程量合计：" + "；".join("%s %s" % (_num(v), k)
                                                       for k, v in pairs), "dim"))
    empty = hl.get("empty_phases") or []
    if empty:
        lines.append(color("  ⚠ 以下阶段没有叶子任务：%s" % "、".join(str(x) for x in empty[:6]),
                           "warn"))
    return lines


# ======================================================================
# 【R2】两版工期对比 —— 规格 §D2 契约
# ======================================================================
def _compare_table(th, ok):
    rows = [
        ("总工期（天）", th.get("total_duration_days"), ok.get("total_duration_days")),
        ("叶子任务数", th.get("leaves"), ok.get("leaves")),
        ("关键任务数", th.get("critical"), ok.get("critical")),
        ("人工峰值（人）", th.get("peak_labor"), ok.get("peak_labor")),
    ]
    out = ["  %s%s%s" % (_fit("指标", 18), _fit("理论最短", 14, "right"),
                         _fit("资源不超额", 14, "right")),
           "  " + "─" * 46]
    for label, a, b in rows:
        out.append("  %s%s%s" % (_fit(label, 18), _fit(_num(a), 14, "right"),
                                 _fit(_num(b), 14, "right")))
    return out


def _top_tasks_table(tasks, cols=None):
    cols = cols or term_size()[0]
    crew_w = max(10, cols - 56)
    out = ["  %s%s%s%s%s" % (_fit("编号", 10), _fit("任务", 22), _fit("天数", 6, "right"),
                             _fit("起止(天)", 12, "right"), "  " + _fit("班组", crew_w))]
    out.append("  " + "─" * max(30, min(cols - 4, 10 + 22 + 6 + 12 + crew_w + 2)))
    for t in tasks:
        if not isinstance(t, dict):
            continue
        es, ef = t.get("es"), t.get("ef")
        span = "—" if es is None or ef is None else "%s→%s" % (_num(es), _num(ef))
        crew = t.get("crew")
        if isinstance(crew, dict) and crew:
            crew_txt = "、".join("%s×%s" % (k, v) for k, v in list(crew.items())[:3])
        else:
            crew_txt = _cell(t.get("crew_note") or t.get("assigned") or "")
        if t.get("capped"):
            crew_txt = (crew_txt + "（封顶）") if crew_txt not in ("", "—") else "已封顶"
        out.append("  %s%s%s%s  %s" % (
            _fit(_cell(t.get("task_id")), 10),
            _fit(_cell(t.get("task_name")), 22),
            _fit(_num(t.get("days")), 6, "right"),
            _fit(span, 12, "right"),
            _fit(crew_txt, crew_w)))
    return out


def _r2_from_highlights(hl):
    """没有 schedule_compare 时用 highlights 退化（老后端也能看到关键数字）。"""
    if not isinstance(hl, dict) or not hl:
        return []
    lines = [color("  两版工期（后端未提供明细表，仅关键数字）：", "dim")]
    lines.append("    %s%s" % (_fit("理论最短（天）", 18),
                               _fit(_num(hl.get("theory_min_days")), 12, "right")))
    lines.append("    %s%s" % (_fit("资源不超额（天）", 18),
                               _fit(_num(hl.get("resource_ok_days")), 12, "right")))
    if hl.get("delta_days") is not None:
        lines.append("    %s%s" % (_fit("相差（天）", 18), _fit(_num(hl.get("delta_days")), 12, "right")))
    if hl.get("peak_labor") is not None:
        lines.append("    %s%s" % (_fit("人工峰值（人）", 18), _fit(_num(hl.get("peak_labor")), 12, "right")))
    cov = hl.get("norm_coverage") or {}
    if isinstance(cov, dict) and cov:
        lines.append(color("  定额口径覆盖率 %.1f%%（%s/%s 条有据可查）"
                           % (cov.get("bound_pct") or 0.0, _num(cov.get("bound")),
                              _num(cov.get("total"))), "dim"))
    if hl.get("target_verdict"):
        if hl.get("user_target") is not None:
            lines.append(color("  用户目标 %s 天 → 判定「%s」"
                               % (_num(hl.get("user_target")), hl.get("target_verdict")), "dim"))
        else:
            lines.append(color("  目标判定：「%s」（用户未给总工期）" % hl.get("target_verdict"),
                               "dim"))
    return lines


def render_schedule_compare(cmp_data, highlights=None, cols=None):
    """D2 `schedule_compare` → 两版对比表 + 最长任务 + 口径说明。"""
    lines = []
    if isinstance(cmp_data, dict) and cmp_data:
        th = cmp_data.get("theory_min") if isinstance(cmp_data.get("theory_min"), dict) else {}
        ok = cmp_data.get("resource_ok") if isinstance(cmp_data.get("resource_ok"), dict) else {}
        if th or ok:
            lines += _compare_table(th, ok)
        top = [t for t in (cmp_data.get("top_tasks") or []) if isinstance(t, dict)]
        if top:
            lines.append(color("  最长的 %d 条任务：" % len(top), "accent"))
            lines += _top_tasks_table(top[:8], cols)
        if cmp_data.get("limit_note"):
            lines.append(color("  资源限额口径：%s" % cmp_data["limit_note"], "dim"))
        if cmp_data.get("labor_note"):
            lines.append(color("  定额工日：%s" % cmp_data["labor_note"], "dim"))
    if not lines:
        lines = _r2_from_highlights(highlights or {})
    return lines


# ======================================================================
# 【R3】草案目录 —— 规格 §D3 契约
# ======================================================================
def render_draft_outline(outline, highlights=None):
    """D3 `draft_outline` → 草案目录 + 表格清单 + 图清单 + 覆盖率 + 出定稿条件。"""
    if not isinstance(outline, dict) or not outline:
        lines = []
        hl = highlights or {}
        if isinstance(hl, dict) and hl:
            if hl.get("docx"):
                lines.append(color("  草案文件：%s" % hl["docx"], "dim"))
            if hl.get("total_duration_days") is not None:
                lines.append(color("  总工期 %s 天 · 任务 %s 条"
                                   % (_num(hl.get("total_duration_days")),
                                      _num(hl.get("leaves"))), "accent"))
        return lines
    lines = []
    sections = [s for s in (outline.get("sections") or []) if isinstance(s, dict)]
    if sections:
        lines.append(color("  草案目录（%d 节）" % len(sections), "accent"))
        for i, s in enumerate(sections):
            branch = "└" if i == len(sections) - 1 else "├"
            lines.append("   %s %s %s" % (branch, _fit(_cell(s.get("title")), 34),
                                          _fit("%s 行" % _num(s.get("lines")), 8, "right")))
    tables = [t for t in (outline.get("tables") or []) if isinstance(t, dict)]
    if tables:
        lines.append(color("  表格（%d 张）" % len(tables), "accent"))
        for i, t in enumerate(tables):
            branch = "└" if i == len(tables) - 1 else "├"
            lines.append("   %s %s %s" % (branch, _fit(_cell(t.get("title")), 34),
                                          _fit("%s 行" % _num(t.get("rows")), 8, "right")))
    figures = [f for f in (outline.get("figures") or []) if f]
    if figures:
        lines.append(color("  图（%d 张）：%s" % (len(figures), "、".join(str(f) for f in figures)),
                           "accent"))
    if outline.get("coverage"):
        lines.append(color("  定额口径：%s" % outline["coverage"], "dim"))
    note = outline.get("note") or "草案未审计、不含图表；通过后才出定稿与看板"
    lines.append(color("  通过才出定稿与看板：%s" % note, "warn"))
    return lines


# ======================================================================
# 计划概览（终稿确认门）—— 规格 §D4 契约
# ======================================================================
def overview_from_plan(plan):
    """从 plan_json 推出 D4 计划概览（事件里还没带 `plan_overview` 时的兜底）。

    `plan_json.overview` 只有 项目名 / 总工期 / 起止 / **关键路径任务数（条数，不是天数——
    键名历史上叫 `critical_path_length`，别被名字骗了）**；叶子数、关键任务数、
    人工峰值分别在 `all_tasks_schedule` / `critical_path_tasks` / `resource_plan` 里 ——
    全都**已经在 SSE 帧里**，这里只是换个地方读，不新算任何数字（算错了就是骗用户）。
    """
    if not isinstance(plan, dict):
        return {}
    ov = plan.get("overview") if isinstance(plan.get("overview"), dict) else {}
    out = dict(ov)
    sched = plan.get("all_tasks_schedule") or []
    crit = plan.get("critical_path_tasks") or []
    res = plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    if sched and out.get("leaves") is None:
        out["leaves"] = len(sched)
    if crit and out.get("critical") is None:
        out["critical"] = len(crit)
    if res.get("peak_manpower") is not None and out.get("peak_labor") is None:
        out["peak_labor"] = res.get("peak_manpower")
    return out


def render_plan_overview(ov):
    """D4 `plan_overview` → 计划概览（缺字段的行直接不显示）。"""
    if not isinstance(ov, dict) or not ov:
        return []
    rows = [
        ("项目", ov.get("project_name")),
        ("总工期", None if ov.get("total_duration_days") is None
         else "%s 天" % _num(ov.get("total_duration_days"))),
        ("计划起止", None if not (ov.get("planned_start_date") or ov.get("planned_end_date"))
         else "%s → %s" % (_cell(ov.get("planned_start_date")), _cell(ov.get("planned_end_date")))),
        ("叶子任务", None if ov.get("leaves") is None else "%s 条" % _num(ov.get("leaves"))),
        ("关键任务", None if ov.get("critical") is None else "%s 条" % _num(ov.get("critical"))),
        ("人工峰值", None if ov.get("peak_labor") is None else "%s 人" % _num(ov.get("peak_labor"))),
        ("交付目录", ov.get("deliver_dir")),
    ]
    lines = []
    for label, value in rows:
        if value is None or value == "":
            continue
        lines.append("   %s %s" % (_fit(label, 10), _cell(value)))
    return lines


# ======================================================================
# 审计门总装（R1 / R2 / R3；字段缺失 → 优雅退化到 highlights / summary）
# ======================================================================
def _granularity_note_lines(note):
    """R1 门 WBS 树**前面**的展示粒度口径说明（后端算好、这里只排一行行）。

    第 23 轮：用户问过"我选了 5 层一组，主体结构还是一层一段，到底有没有采用我的输入"。
    说明文字与行数都由后端给（`audit_gate.granularity_caliber_note`，行数是真算的），
    终端不重算、不猜；没有这个字段 → 返回 []，输出与改造前一模一样。
    """
    if not note:
        return []
    items = note if isinstance(note, (list, tuple)) else [note]
    out = []
    for i, ln in enumerate(items[:3]):
        text = str(ln or "").strip()
        if text:
            out.append(color("  " + text, "accent" if i == 0 else "dim"))
    return out


def render_audit_gate(data):
    """param_review(purpose=audit) → R1/R2/R3 的**实物内容**。

    用户原话：「我最不满意的是你每道门的返回内容……WBS 门应该返回详细的 WBS 树的，
    为什么只返回摘要？你打算让用户依据什么来决定是否继续计划？」
    """
    data = data or {}
    try:
        rnd = int(data.get("round") or 0)
    except (TypeError, ValueError):
        rnd = 0
    name = _cell(data.get("round_name") or data.get("title"), "计划审计")
    head = ("【R%d 审计门】%s" % (rnd, name)) if rnd else ("【审计门】%s" % name)
    lines = [color(head, "warn", bold=True)]

    body = []
    if rnd == 1:
        body = render_wbs_tree(data.get("wbs_tree"))
        if body:
            # 口径说明紧贴在树前面（只在真有树时给 —— 否则"本门按原始 WBS 展示"是空话）
            lines += _granularity_note_lines(data.get("granularity_note"))
        else:
            body = _r1_from_highlights(data.get("highlights"))
    elif rnd == 2:
        body = render_schedule_compare(data.get("schedule_compare"), data.get("highlights"))
    elif rnd == 3:
        body = render_draft_outline(data.get("draft_outline"), data.get("highlights"))
    if not body:
        # 优雅退化：老后端 / 别的门没有结构化字段时，把 summary 竖版打出来（不丢信息）
        summary = str(data.get("summary") or "").strip()
        if summary:
            body = [color("  （本轮没有结构化明细，下面是后端摘要）", "dim")]
            body += [color(x, "dim") for x in _summary_verbatim(summary)]
        else:
            body = [color("  （本轮没有任何可展示的内容）", "dim")]
    lines += body

    # 附加：结构性问题清单 / 口径警告 / 概览（有就打，没有就跳过）
    issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
    if issues:
        lines.append(color("  需要你判断的问题（%d 条）：" % len(issues), "warn"))
        lines += [color(x, "dim") for x in _issue_lines(issues)]
    warns = warning_lines(data)
    for w in warns[:3]:
        lines.append(color("  ⚠ %s" % w[:110], "warn"))
    if rnd == 3 or data.get("plan_overview"):
        ov_lines = render_plan_overview(data.get("plan_overview"))
        if ov_lines:
            lines.append(color("  计划概览：", "accent"))
            lines += ov_lines

    hint = data.get("next_hint")
    if hint:
        lines.append(color("  " + str(hint), "dim"))
    return "\n".join(lines)


# ======================================================================
# 事件渲染
# ======================================================================
def render_event(event, data):
    """返回要打印的一段文本；返回 None 表示"这个事件不由这里打印"（如 ping）。"""
    data = data or {}
    node = data.get("node", "")

    if event == EV_RUN_PLAN:
        # 步数表只给状态区用（FoldState.track），不打给用户看。
        # 老终端不认识它 → 这里必须返回 None，否则会打出一行"[未知事件 run_plan] {…}"。
        return None

    if event == EV_NODE_START:
        title = data.get("title") or node
        note = data.get("note") or ""
        return color(f"▶ 节点启动：{title}" + (f"  {note}" if note else ""), "accent", bold=True)

    if event == EV_NODE_PROGRESS:
        msg = data.get("message") or ""
        bar = progress_bar(data.get("progress", 0))
        return color(f"  {bar} {msg}", "accent")

    if event == EV_NODE_DONE:
        summary = data.get("summary") or "完成"
        # 完成行也要用**界面名**（`✔ 第 1 轮审计：WBS 结构：…`），不能打 code name
        # （`✔ audit_wbs：…`）—— 用户刚在进度行里看到的是中文名，两行对不上就是"这谁"。
        # `title` 由 console 从 run_plan 的步数表带过来；没有该字段（老后端 / 探针）
        # 保持改造前的 `✔ <code name>：<summary>`。
        name = str(data.get("title") or node)
        step = data.get("step")
        try:
            step = int(step) if step is not None else None
        except (TypeError, ValueError):
            step = None
        if step and data.get("steps"):
            head = "✔ 第 %d / %d 步 · %s：" % (step, int(data["steps"]), name)
        else:
            head = "✔ %s：" % name
        lines = [color(head + summary, "ok" if not has_warning(data) else "warn")]
        for w in warning_lines(data)[:3]:
            lines.append(color("   ⚠ %s" % w[:110], "warn"))
        # "…其余 N 条同类（类别）" —— 由**节点自己**算好（它才知道类别的语义），
        # 引擎随 node_done 带上来（第 23 轮：警告不许只有数字没有内容）。
        # 没有这个字段的事件（今天所有其它节点）一个字节都不变。
        note = str(data.get("warnings_note") or "").strip()
        if note:
            lines.append(color("   %s" % note[:160], "dim"))
        return "\n".join(lines)

    if event == EV_NODE_PAUSED:
        out = (data.get("output_summary") or "").strip()
        ctx = (data.get("context_summary") or "").strip()
        issues = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
        lines = [color(f"⏸ 已暂停于节点 {node}", "warn", bold=True)]
        if out:
            lines.append(color(f"  结果：{out}", "warn"))
        if issues:
            # 结构化问题清单 → 一条一行（编号 + 严重度 + 维度 + 完整描述，不再截断到 40 字）
            lines.append(color("  问题：", "accent"))
            lines += [color(x, "dim") for x in _issue_lines(issues)]
        elif ctx:
            items = verticalize(ctx)
            if len(items) == 1:
                for w in wrap_cjk(items[0], max(24, _body_width() - 6)):
                    lines.append(color(f"  摘要：{w}", "dim"))
            else:
                lines.append(color("  摘要：", "accent"))
                lines += [color(x, "dim") for x in _summary_lines(ctx)]
        # 一键修复（规格 §3）：编号选择题，接在问题清单之后。
        # `repairs` 缺失/为空 → render_repairs 返回 [] → 老界面一个字都不变。
        lines += render_repairs(data.get("repairs"), data.get("repair_limit_note"))
        # 做不到自动修的问题 → 如实说明"需要你提供什么信息"（规格 §4）
        info_lines = render_issue_info_requests(data.get("issues_need_info"),
                                                data.get("repairs"))
        if info_lines:
            # 措辞（第 32 轮，用户实测："这些是什么意思，作为一个第一次使用的用户，
            # 根本看不懂"）：先给**默认动作**（不回答也能继续），再说怎么提意见 ——
            # 不再让人以为必须先回答一个技术选择题。
            lines.append(color("  下面几条系统改不动，你可以先不管（直接回车=继续）；"
                               "要改就写一句，或选 [%d] 写意见："
                               % _free_option_no(data.get("repairs")), "warn"))
            lines += info_lines
        done = str(data.get("repair_done") or "").strip()
        if done:
            lines.append(color("  上一轮系统已经改的：", "ok"))
            for w in wrap_cjk(done, max(24, _body_width() - 6)):
                lines.append(color("    " + w, "ok"))
        # WBS 复评门也会带 wbs_tree（规格 §D1：R1 与 WBS 复评门都要带）
        tree_lines = render_wbs_tree(data.get("wbs_tree"))
        if tree_lines:
            # 第一次出现 WBS 的地方补一句中文（用户实测："不要刻意使用一些英文和专业术语"）
            lines.append(color("  WBS 结构（任务分解：阶段 → 工作包 → 工序）：", "accent"))
            lines += tree_lines
        for w in warning_lines(data)[:3]:
            lines.append(color("  ⚠ %s" % w[:110], "warn"))
        lines.append(color("  可用：/continue · /retry [指令] · /edit 键=值 · /abort", "dim"))
        return "\n".join(lines)

    if event == EV_CONFIRM_REQUIRED:
        msg = data.get("message") or "是否继续？"
        ctx = _ctx_summary(data)
        lines = [color(f"❓ {msg}", "warn", bold=True)]
        ov_lines = render_plan_overview(data.get("plan_overview"))
        if ov_lines:
            lines.append(color("  计划概览：", "accent"))
            lines += ov_lines
        if ctx:
            items = verticalize(ctx)
            if len(items) == 1:
                for w in wrap_cjk(items[0], max(24, _body_width() - 6)):
                    lines.append(color(f"  摘要：{w}", "dim"))
            else:
                lines.append(color("  摘要：", "accent"))
                lines += [color(x, "dim") for x in _summary_lines(ctx)]
        for w in warning_lines(data)[:3]:
            lines.append(color("  ⚠ %s" % w[:110], "warn"))
        return "\n".join(lines)

    if event == EV_PARAM_REVIEW:
        if (data.get("purpose") or "") == "audit":
            # 审计门正文与它的输入提示是**同一次交互**（confirmer 打印），
            # 放这里打印会被别的输出插队、也会重复。
            return None
        return render_param_review(data)

    if event == EV_PLAN_FINAL:
        pid = data.get("plan_id", "")
        # ⚠️ 这里**不能**写「最终方案已生成」：本事件在**方案组装落盘**时就发，
        # 那时三轮回审门还没走完，计划状态是「未审计」，而且定稿 Word 与看板
        # 都还没产出（被审计门拦着）。写成"最终/已交付"会让人以为已经验收通过
        # （用户实测反馈：「明明我还没审批，为什么叫最终方案？」）。
        lines = [color("📄 计划数据已生成并落盘（尚未审计）"
                       + (f"（{pid}）" if pid else ""), "accent", bold=True)]
        lines.append(color("  这是一份可复核的计划数据；定稿 Word 与看板要等你"
                           "通过三轮回审门之后才产出。", "dim"))
        ov = data.get("plan_overview")
        if not isinstance(ov, dict) or not ov:
            # 事件里没有 D4 的 plan_overview → 从 plan_json 里已有的字段拼一份（不新算数字）
            ov = overview_from_plan(data.get("plan"))
        ov_lines = render_plan_overview(ov)
        if ov_lines:
            lines.append(color("  计划概览：", "accent"))
            lines += ov_lines
        # 交付物路径：后端落盘位置（saved_path）与交付目录（deliver_dir）有哪个打哪个
        saved = data.get("saved_path")
        if saved:
            lines.append(color("  计划文件：%s" % saved, "dim"))
        dir_path = data.get("deliver_dir")
        if not dir_path and isinstance(data.get("plan"), dict):
            dir_path = data["plan"].get("deliver_dir")
        if dir_path and not (isinstance(ov, dict) and ov.get("deliver_dir")):
            lines.append(color("  交付目录：%s" % dir_path, "dim"))
        return "\n".join(lines)

    if event == EV_ERROR:
        msg = data.get("message") or "未知错误"
        where = f"于节点 {node}" if node else ""
        return color(f"✖ 节点失败{where}：{msg}", "err", bold=True)

    if event == EV_DONE:
        status = data.get("status", "ok")
        tail = format_usage(data)          # token 用量与费用（末尾一行）
        if status == "ok":
            note = data.get("note")
            if note:
                # router 闲聊回答等经 done.note 上行
                head = color(f"💬 {note}", "accent") + "\n" + color("✅ 流程结束", "ok", bold=True)
            else:
                head = color("✅ 流程结束", "ok", bold=True)
            return head + ("\n" + tail if tail else "")
        if status == "cancelled":
            return color("⏹ 流程已取消", "warn", bold=True) + ("\n" + tail if tail else "")
        return color("✖ 流程异常结束", "err", bold=True) + ("\n" + tail if tail else "")

    if event == "ping":
        return None

    # 未知事件：打印原文，不崩溃
    return color(f"[未知事件 {event}] {json.dumps(data, ensure_ascii=False)}", "dim")


def format_usage(data):
    """把 done 事件里的 usage 渲染成一行中文（没有就不显示）。

    数据来自 backend/pipeline/usage.py 的记账器，经 done 事件上行。
    """
    u = (data or {}).get("usage") or {}
    if not isinstance(u, dict) or not u:
        return ""
    calls = u.get("calls") or 0
    if not calls:
        return color("  💰 本次未调用大模型（0 token / ¥0）", "dim")
    line = ("  💰 本次运行：{} 次调用 · 输入 {:,} tok · 输出 {:,} tok · "
            "合计 {:,} tok · 约 ¥{:.4f}").format(
        calls, u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0,
        u.get("total_tokens") or 0, u.get("cost_cny") or 0.0)
    top = u.get("by_node") or {}
    if top:
        name, tok = max(top.items(), key=lambda kv: kv[1])
        line += "\n  💰 最费 token 的环节：{}（{:,} tok）".format(name, tok)
    return color(line, "dim")


# ======================================================================
# Plan 落盘
# ======================================================================
def plans_dir():
    """终端落盘目录：默认 `<terminal>/plans`，但允许用环境变量覆盖。

    ⚠️ 为什么必须能覆盖（第 42 轮，测试隔离缺口）：
      本函数原来是**硬编码**的 `dirname(__file__)/plans`，因此
      `tests/conftest.py` 的 `_isolate_outputs` 把 `config.PLANS_DIR` 指到临时目录
      对它**完全无效**。任何走到"终端保存计划"这条路的用例（`renderer.save_plan`
      由 `console.run_chat` 的 `plan_final` 事件触发；`build_show_html` 由 `/show`
      触发）都会写进**真实** `terminal/plans/`，污染真实运行产物，
      也让 pytest-xdist 并行时多个 worker 抢同一个目录。
      现在统一走 `BUILDPLAN_PLANS_DIR`（conftest 在 autouse 夹具里设），
      与 `BUILDPLAN_MODE_FILE` / `BUILDPLAN_LLM_PROFILES` 同一套约定。
    """
    d = os.environ.get("BUILDPLAN_PLANS_DIR") or \
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "plans")
    os.makedirs(d, exist_ok=True)
    return d


def save_plan(data):
    """plan_final 事件载荷 → 落盘 ./plans/plan_<id>.json，返回文件路径。"""
    plan = data.get("plan") if isinstance(data, dict) else None
    if not plan:
        plan = data
    plan_id = plan.get("plan_id") if isinstance(plan, dict) else None
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if plan_id and plan_id.startswith("plan_"):
        name = f"{plan_id}.json"      # 后端已带 plan_ 前缀，直接用
    else:
        name = f"plan_{plan_id or ts}.json"
    path = os.path.join(plans_dir(), name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path


# ======================================================================
# /show：单文件 HTML 看板（标准库字符串拼接）
# ======================================================================
def _safe_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build_show_html(plan):
    """生成单文件 HTML（内嵌 JSON 数据，浏览器可读），返回文件路径。"""
    if not isinstance(plan, dict):
        raise ValueError("plan 必须是 dict")
    overview = plan.get("overview") or {}
    tasks = plan.get("all_tasks_schedule") or []
    crit = plan.get("critical_path_tasks") or []
    res = plan.get("resource_plan") or {}

    def kv(name, val):
        return f"<tr><td class='k'>{html.escape(str(name))}</td><td>{html.escape(str(val))}</td></tr>"

    overview_rows = "".join(kv(k, v) for k, v in overview.items())

    def task_rows(items):
        rows = []
        for t in items:
            ar = t.get("assigned_resources") or {}
            ars = ", ".join(f"{k}×{v}" for k, v in ar.items()) or "—"
            rows.append(
                f"<tr><td>{html.escape(t.get('task_id',''))}</td>"
                f"<td>{html.escape(t.get('task_name',''))}</td>"
                f"<td>{html.escape(str(t.get('start_date',''))) }</td>"
                f"<td>{html.escape(str(t.get('finish_date','')))}</td>"
                f"<td>{t.get('duration_days','')}</td><td>{html.escape(ars)}</td></tr>")
        return "".join(rows)

    task_thead = ("<tr><th>ID</th><th>任务</th><th>开始</th><th>完成</th>"
                  "<th>工期(天)</th><th>资源</th></tr>")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(str(overview.get("project_name", "施工进度计划")))} · 方案看板</title>
<style>
  body{{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:24px;color:#222;background:#fafafa}}
  h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;border-bottom:2px solid #4a90d9;padding-bottom:4px}}
  table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}}
  th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left}}
  th{{background:#4a90d9;color:#fff}}
  tr:nth-child(even){{background:#f5f8fc}}
  .k{{width:200px;color:#666;font-weight:600}}
  pre{{background:#1e1e2e;color:#cdd6f4;padding:14px;border-radius:6px;font-size:12px;overflow:auto}}
</style></head>
<body>
  <h1>📋 {html.escape(str(overview.get("project_name", "施工进度计划")))}</h1>
  <table><tr><td class="k">总工期</td><td>{overview.get("total_duration_days","—")} 天</td></tr>
  <tr><td class="k">计划周期</td><td>{overview.get("planned_start_date","—")} → {overview.get("planned_end_date","—")}</td></tr>
  <tr><td class="k">关键路径任务数</td><td>{overview.get("critical_path_length","—")} 个（条数，不是天数）</td></tr>
  <tr><td class="k">工序总数</td><td>{len(tasks)}</td></tr>
  <tr><td class="k">峰值人数</td><td>{res.get("peak_manpower","—")}</td></tr></table>

  <h2>关键里程碑</h2>
  <ul>{''.join(f"<li><b>{html.escape(str(m.get('name',''))) }</b>（{m.get('date','')}）— {html.escape(str(m.get('description',''))) }</li>" for m in (plan.get('key_milestones') or [])) or "<li>—</li>"}</ul>

  <h2>关键路径明细</h2>
  <table>{task_thead}{task_rows(crit)}</table>

  <h2>全部工序排程</h2>
  <table>{task_thead}{task_rows(tasks)}</table>

  <h2>监督报告</h2>
  <pre>{html.escape(str(plan.get('report','')))}</pre>

  <h2>原始数据（plan_json）</h2>
  <pre>{html.escape(_safe_json(plan))}</pre>
</body></html>"""

    pid = plan.get("plan_id") or "latest"
    path = os.path.join(plans_dir(), f"show_{pid}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return path

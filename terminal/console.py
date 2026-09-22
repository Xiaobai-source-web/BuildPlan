"""终端主循环 — T-01（零第三方依赖）

启动：
  python console.py --real      连真实云端后端（需 uvicorn backend.main 已起）
  python console.py --url <url> 自定义后端地址
  python console.py --run "生成计划"  跑一句后自动退出（测试用；**不做任何定位**，退化模式）
  python console.py --no-tui    强制退化模式（排查老终端 / 记录日志时用）

版式（第 16 轮改造，规格《终端界面改造规格》§B）：
  · 输入框固定在屏幕底部（VT 模式：备用屏 + 底部 3 行 + 滚动区）；
    非 TTY / 非 VT / --run / 终端太小 → 退化成「输出 → 满宽细线 → 你 ▸ 」。
  · 每个块（开场白 / 用户输入 / 助手输出 / 门 / 结果）之间空一行；
    用户输入与每道门之前有满宽细线。
  · 逐节点进度**默认折叠**成状态区里的 2~3 个节点 + 总进度；`/verbose` 打开全量。
  · 第 23 轮：**顺序输出**模式下，被折叠的长节点（如 wbs_agent 逐相展开）会额外打
    一条**有节流的紧凑进度行**，长时间无输出时兜一条"仍在跑"心跳行 ——
    用户能区分"在跑"和"卡死"。VT 模式行为不变（它有原地刷新的状态区）。
  · 第 23 轮：交互式主循环里每按一次回车（非空输入）先打一行 dim 确认
    （`⏳ 已收到，正在处理…` / `⏳ 执行 /revise …`），再调后端 ——
    "按下去不知道挂了还是在跑"的那个窗口由它填上。`--run` 路径不打。

交互：
  普通输入 → 触发流水线（SSE 流式）
  /命令   → commands.py 分发（/verbose 由本文件自己处理，见 _handle_local）
  !命令   → 透传系统命令
"""

import argparse
import os
import re
import sys
import time

# 允许从任意 cwd 启动时仍能找到本目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import commands
import confirmer
import renderer
import switch
import tui
from client import (EV_CONFIRM_REQUIRED, EV_DONE, EV_NODE_DONE, EV_NODE_PAUSED,
                    EV_PARAM_REVIEW, EV_PLAN_FINAL, SSEClient)
# color / clear_screen / print_banner 是改造前就从这里导出的老接口，保留以免炸调用方
from renderer import color, clear_screen, print_banner  # noqa: F401


class Ctx:
    """终端运行时上下文，供命令处理器读写。"""

    def __init__(self, backend="cloud", base_url=None):
        self.backend = backend
        self.client = SSEClient(base_url=base_url or switch.url_of(backend))
        self.history = []           # [(role, text), ...]
        self.current_plan = None
        self.current_plan_id = None
        self.show_html = None
        self.running = False
        self.run_id = None
        self.verbose = False                        # /verbose 开关（默认关）
        self.fold = renderer.FoldState()            # 折叠进度状态区
        self.event_log = []                         # 被折叠掉的逐节点事件（有上限）
        self.tui = None                             # 由 main 安装
        self.feed = None                            # 本次运行的紧凑进度反馈器（见 run_chat）
        self.progress_now = None                    # 测试/探针注入的时钟（None → 真时钟）
        # 第 33 轮：模式（意图对话隔离）—— 跨会话落盘，见 _load_mode/_save_mode
        self.mode = "normal"                        # normal | plan | revise | import
        self.mode_plan_id = ""                      # 修改模式的基准计划编号


# ----------------------------------------------------------------------
# 输出：所有"块"都从这里出去（间距与细线只在这一个地方实现，不会漏）
# ----------------------------------------------------------------------
def _emit(ctx, text, gap=True, rule_before=False):
    if not text:
        return
    term = getattr(ctx, "tui", None)
    if term is not None:
        term.out(text, gap=gap, rule_before=rule_before)
    else:
        print(text)
    # 屏幕上刚有字 → 心跳计时归零（第 23 轮：顺序模式下"看得出在动"的那套判定）
    feed = getattr(ctx, "feed", None)
    if feed is not None:
        feed.note_visible()


def _refresh_status(ctx, fold, verbose):
    """VT 模式：状态区原地刷新（节点切换 / 进度变化时）。退化模式：什么都不做。"""
    term = getattr(ctx, "tui", None)
    if term is None or not term.vt:
        return
    line1, line2 = fold.lines(verbose=verbose)
    term.status(line1, line2)


def _heartbeat(ctx, fold, verbose):
    """折叠期的"心跳"：VT 刷新状态区；退化模式打两行，让用户知道还在跑。

    ⚠️ 退化（顺序输出）模式下，进度行由 `tui.ProgressFeed` **独家**负责 ——
    这里再 force 打一次就成了同一个节点两行（用户实测截图里那一对
    `⏳ 1 闲聊 LLM 入口` / `⏳ 1 闲聊 LLM 入口 · 调用 LLM…30%` 就是这么来的）。
    所以本函数在退化模式下只把"屏幕刚有字"告诉反馈器（让它的节流与心跳计时对齐），
    一个字节都不再写。
    """
    line1, line2 = fold.lines(verbose=verbose)
    term = getattr(ctx, "tui", None)
    feed = getattr(ctx, "feed", None)
    if term is None:
        print(line1 + ("\n" + line2 if line2 else ""))
    elif term.vt:
        term.status(line1, line2)
    elif feed is None:
        # 没有反馈器（BUILDPLAN_PROGRESS=0 / 老调用方）→ 保持改造前的两行心跳
        term.status(line1, line2, force=True)
    if feed is not None:
        feed.note_visible()


def _node_index(fold, node):
    """节点在本次运行里的序号（1 起，按首次启动顺序）；拿不到返回 None。"""
    try:
        return fold.seen.index(str(node or "")) + 1
    except (ValueError, AttributeError):
        return None


def _progress_enabled(term):
    """紧凑进度反馈是否启用。

    默认：**顺序输出**模式启用（VT 模式有自己的状态区，一个字节都不写）；
    `BUILDPLAN_PROGRESS=0` 可整体关掉 —— 给"输出必须逐字稳定"的脚本 / 管道场景留的
    逃生口（关掉后退回改造前的行为：长节点期间屏幕上什么都不动）。
    """
    v = os.environ.get("BUILDPLAN_PROGRESS", "").strip().lower()
    if v in ("0", "off", "no", "false"):
        return False
    return (term is None) or (not term.vt)


def _make_progress_feed(ctx, verbose):
    """建本次运行的紧凑进度反馈器（第 23 轮）。

    只在**顺序输出**模式（非 VT）下生效：VT 模式有自己的原地刷新状态区，
    这里必须一个字节都不写；`/verbose` 打开时逐节点事件本来就全量打印，
    同样不给它加戏（既有语义一字不改）。
    """
    term = getattr(ctx, "tui", None)
    enabled = _progress_enabled(term)

    def _sink(text):
        if term is not None:
            term.out(renderer.color(text, "dim"), gap=False)   # 紧凑单行：不占块间距
        else:
            print(text)

    feed = tui.ProgressFeed(_sink, now=getattr(ctx, "progress_now", None),
                            enabled=enabled, verbose=verbose)
    # ⚠️ 心跳线程**只在真实交互终端**里起（第 33 轮）：它是给"用户盯着屏幕等"准备的兜底。
    # 重定向 / 管道（`isatty()` 为假）里没有人在盯屏幕，心跳只会往日志和测试输出里灌
    # 噪声 —— 用户实测截图里那两行"无新进度，仍在跑（已 14s）"就是它。
    # 逐节点进度行不受影响（它是事件驱动的，节流照旧）。
    feed.allow_watchdog = _is_interactive(term)
    ctx.feed = feed
    return feed


def _is_interactive(term):
    """当前是否"真有一个终端窗口在显示"（心跳线程只在这种情况下有意义）。"""
    try:
        stream = getattr(term, "stream", None)
        if stream is not None:
            return bool(stream.isatty())
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _clear_status(ctx):
    term = getattr(ctx, "tui", None)
    if term is not None:
        term.clear_status()


EVENT_LOG_LIMIT = 300

# 当前会话的运行上下文（供 confirmer 在门里改模式用；`main` 会设置它）。
# 为什么要这一份全局：`confirmer` 只拿到 client 与事件载荷，拿不到 Ctx；
# 而"工作模式确认门"是"用户确实要生成计划"最可靠的信号（第 33 轮）。
_CURRENT_CTX = None


def current_ctx():
    return _CURRENT_CTX

# ======================================================================
# 第 33 轮：四种「模式」（意图对话隔离）
# ======================================================================
# 用户原话：「意图对话隔离是必须要做的，必须要在每次对话时，自己处于什么模式，用户能
# 清清楚楚」「当用户进入修改模式之后，一定要在聊天框固定显示"修改模式，plan id：xxxx"」
#           「只需要将这些意图空间隔离开就好了，更有利于用户沉浸式使用」
#
# 设计（对齐现有代码，不新建机制）：
#   · 模式**只**决定"裸输入"怎么解释：normal 走 router 三路分类；plan/revise/import 直通；
#   · 斜杠命令**在任何模式都可用**（用户进了改计划模式还想 /versions 是最自然的）；
#   · 模式与 plan_id **落盘**（`POST /mode`）→ 关掉终端再打开还在改同一份计划；
#   · 退出指令 `/退出`（别名 /exit /mode normal）回到普通模式，**不退出程序**。
MODES = ("normal", "plan", "revise", "import")
MODE_LABELS = {
    "normal": "普通",
    "plan": "生成计划",
    "revise": "改计划",
    "import": "导入计划",
}
# 第 34 轮：命令一律英文（用户要求"命令都用英文，不要用中文把它们分开列出来"）。
# 入口只有两条：`/mode normal|plan|revise|import` 与 `/exit`。
MODE_ALIASES = {
    "normal": "normal", "chat": "normal", "普通": "normal",
    "plan": "plan", "生成计划": "plan", "生成": "plan",
    "revise": "revise", "edit": "revise", "改计划": "revise", "修改": "revise",
    "import": "import", "导入计划": "import", "导入": "import",
}
EXIT_WORDS = ("/exit", "/quit-mode", "退出模式")


def _mode_label(ctx):
    """输入框里那截标识：`[改计划 · plan_xxx]`（用户要求固定显示在聊天框上）。"""
    mode = str(getattr(ctx, "mode", "normal") or "normal")
    if mode not in MODES:
        mode = "normal"
    label = MODE_LABELS[mode]
    pid = str(getattr(ctx, "mode_plan_id", "") or "")
    if mode == "revise" and pid:
        return "[%s · %s]" % (label, pid)
    return "[%s]" % label


def _ask_hint(ctx, base="你 ▸ "):
    """带模式标识的提示符。"""
    return "%s %s" % (_mode_label(ctx), base)


def _mode_intro_lines():
    """开场白里的"四种模式"介绍（用户要求：把模式写进开场白，讲清怎么进、有什么用）。"""
    return [
        "四种模式（输入框左侧一直显示你在哪种模式）：",
        "  normal  纯聊天。不排计划、不改计划，问什么答什么（默认）。",
        "  plan    生成计划。进来后描述项目（类型/层数/面积/开工日期），直接开始编制。",
        "  revise  改计划。选定一份计划后，问它问题，或者说一句要改什么（改前会给你看要改哪几项）。",
        "  import  导入计划。给一个计划 JSON 的完整路径，导入后即可修改。",
        "怎么切换（命令都是英文）：/mode normal | plan | revise | import   ·   回普通模式：/exit",
        "斜杠命令在任何模式都能用，/help 看全部。",
    ]


def _mode_banner(ctx):
    """进入/切换模式时打一段人话（说清"现在能干什么、怎么出去"）。"""
    mode = str(getattr(ctx, "mode", "normal") or "normal")
    pid = str(getattr(ctx, "mode_plan_id", "") or "")
    head = {
        "normal": "已回到 normal（普通模式）：这里是纯聊天，想排计划请 /mode plan。",
        "plan": "已进入 plan（生成计划）模式：直接描述项目，我开始编制。",
        "revise": ("已进入 revise（修改计划）模式"
                   + ("，当前基准计划：%s" % pid if pid else "")
                   + "：可以直接问这份计划，或说一句要改什么（改前会给你看要改哪几项）。"),
        "import": "已进入 import（导入计划）模式：给我一个计划 JSON 的完整路径"
                  "（或直接 /import <路径>）。",
    }[mode]
    return (head + "\n  切模式：/mode normal|plan|revise|import ｜ 回普通：/exit ｜ "
                   "斜杠命令任何模式都能用。")


def _save_mode(ctx):
    """把模式落到磁盘（失败静默：模式是体验优化，不该影响主流程）。

    ⚠️ **必须非阻塞**：这个函数会在门里被调用（工作模式确认门 → 切到生成模式），
    那一刻主线程正握着 SSE 连接；一个同步 POST 要是卡住，整条流就停在那儿
    （门里的输入永远送不出去）。所以丢到后台线程去发，主流程不等它。
    """
    mode = str(getattr(ctx, "mode", "normal") or "normal")
    pid = str(getattr(ctx, "mode_plan_id", "") or "")
    try:
        import threading
        client = getattr(ctx, "client", None)
        if client is None:
            return

        def _post():
            try:
                client.post_mode(mode, pid)
            except Exception:
                pass

        threading.Thread(target=_post, name="buildplan-save-mode", daemon=True).start()
    except Exception:
        pass


def _load_mode(ctx):
    """启动时恢复上次的模式与计划编号。"""
    try:
        _status, data = ctx.client.get_mode()
    except Exception:
        return
    if not isinstance(data, dict):
        return
    mode = str(data.get("mode") or "normal")
    if mode in MODES:
        ctx.mode = mode
        ctx.mode_plan_id = str(data.get("plan_id") or "")


def _set_mode(ctx, mode, plan_id=None):
    """切换模式并落盘。

    ⚠️ 这里踩过一个坑（用户实测："模式也没有变化，我问它要基准计划，它却重新分向了闲聊"）：
    早先写的是"非 revise 模式就把 `mode_plan_id` 清空"，于是 `[1] 改一份已有的计划`
    走到 `_set_mode("revise")` 时 **plan_id 还没选出来**，`mode_plan_id` 被置空、
    模式又被判回 normal —— 用户的下一个输入（选项号 `1`）掉进普通输入路径，
    被意图识别当成闲聊回答。现在：
      · 只有**显式传 `plan_id`**（哪怕传空串）才改这个字段；
      · 模式本身保持不变，`revise` 允许"有模式但还没选基准计划"这个中间态。
    """
    ctx.mode = mode if mode in MODES else "normal"
    if plan_id is not None:
        ctx.mode_plan_id = str(plan_id or "")
    _save_mode(ctx)


_SWITCH_WORDS = ("改计划", "修改计划", "调整计划", "改一下计划", "改这份计划", "修改这份计划")
_QUESTION_WORDS = ("？", "?", "吗", "多少", "为什么", "怎么", "是不是", "能不能", "什么时候")


# 句首的"能/可以/支持/什么/怎么"等：这类开头是在**提问**，不是在下指令
_ASK_LEAD = re.compile(
    r"^\s*(能|可以|可不可以|能不能|是否|是不是|支持|有没有|什么|啥|怎么|如何|为什么|"
    r"哪|哪些|介绍|解释|讲讲|说说|请问|告诉我)")


def _wants_revise_mode(text):
    """这句是不是"我要改计划"（确定性，不调模型）。

    ⚠️ 第 35 轮补一道提问闸门：旧实现只看关键词，于是「**能**改计划**吗**？」
    「改计划怎么用」也被判成"要进修改模式"，直接打出三选一菜单、**一次模型都不调** ——
    用户问的是"能不能改"这个**产品能力**，该如实回答"能，这样改"，而不是弹菜单。
    （与后端 `router.looks_like_plan_request` 同一套判据，两边行为才不会打架。）
    """
    t = str(text or "").strip()
    if not any(w in t for w in _SWITCH_WORDS):
        return False
    if _looks_like_question(t) or _ASK_LEAD.match(t):
        return False
    return True


def _looks_like_question(text):
    return any(w in str(text or "") for w in _QUESTION_WORDS)


def _mode_menu(ctx, title="进入【修改计划】模式", extra=""):
    """选基准计划的三选一菜单（用户提案：改已有 / 导入 / 先新建）。

    打菜单的同时把 `_menu_pending` 立起来：这样 **不管当前模式是什么**，
    用户的 `1/2/3/0` 都会被菜单接住 —— 用户实测踩过的坑是"打了 /模式 改计划、
    再打 1，却被当成闲聊回答了"（模式状态与菜单状态脱节）。
    """
    ctx._menu_pending = True
    lines = ["🔧 %s" % title]
    if extra:
        lines.append("   " + extra)
    lines.append("   要改哪一份计划？")
    lines.append("     [1] 改一份已有的计划（本机已有）")
    lines.append("     [2] 从文件导入一份计划（/import <路径>）")
    lines.append("     [3] 先生成一份新计划，再改它")
    lines.append("     [0] 返回普通模式")
    return "\n".join(lines)


def _route_mode_menu(ctx, text):
    """处理"菜单还开着"时的用户输入（1 / 2 / 3 / 0）。返回 True=已处理。

    与模式**解耦**：菜单开着就归它管，模式是什么都不影响 —— 这就杜绝了
    "菜单打出来了、用户按提示选了、系统却去走别的路"。
    """
    ctx._menu_pending = False
    t = str(text or "").strip().strip("[]（）()")
    if t == "1":
        return _revise_pick_existing(ctx)
    if t == "2":
        _set_mode(ctx, "import", "")
        _emit(ctx, color("已切到【导入计划】模式：把计划 JSON 的完整路径发给我"
                         "（或直接 /import <路径>）。", "accent"))
        return True
    if t == "3":
        _set_mode(ctx, "plan", "")
        _emit(ctx, color("已切到【生成计划】模式：直接描述项目即可开始。", "accent"))
        return True
    if t in ("0", ""):
        _set_mode(ctx, "normal", "")
        _emit(ctx, color("已回到普通模式。", "accent"))
        return True
    return False


def _enter_revise_mode(ctx, plan_id=""):
    ctx.mode = "revise"
    ctx.mode_plan_id = str(plan_id or "")
    _save_mode(ctx)


def _handle_mode_input(ctx, text):
    """模式内的**裸输入**（非斜杠命令）处理。返回 True=已处理，False=按普通输入走。"""
    mode = str(getattr(ctx, "mode", "normal") or "normal")
    t = str(text or "").strip()
    if mode not in MODES or mode == "normal":
        return False
    low = t.lower()
    if low in ("/退出", "/exit", "退出", "退出模式", "end", "back"):
        _set_mode(ctx, "normal", "")
        _emit(ctx, color("已退出到普通模式。", "accent"))
        return True
    if mode == "plan":
        return False                      # 生成计划：照旧送后端（走完整流水线）
    if mode == "revise":
        if not ctx.mode_plan_id:
            # 还没选基准计划：菜单已由 `_mode_menu` 打出，`1/2/3/0` 由主循环的
            # `_route_mode_menu` 接（与模式解耦）。这里只负责兜底提示，避免用户
            # 打了一句没用的话就静默掉。
            _emit(ctx, _mode_menu(ctx, "修改模式：还没有选基准计划",
                                  extra="请输入 1 / 2 / 3 / 0"))
            return True
        if _looks_like_question(t):
            # 修改模式里提问 → 基于当前计划回答（不是拒绝，也不是改计划）
            _chat_about_plan(ctx, t, str(ctx.mode_plan_id or ""))
            return True
        _revise_with_confirm(ctx, t)
        return True
    if mode == "import":
        if os.path.isfile(t.strip('"').strip("'")):
            _run_local_command(ctx, "/import " + t)
        else:
            _emit(ctx, color("导入模式：请给一个计划 JSON 的完整路径（在资源管理器里右键"
                             "文件 → 复制文件地址）。", "warn"))
        return True
    return False


def _revise_pick_existing(ctx):
    """选 [1]：列出已有计划让用户挑一个作为基准。"""
    try:
        _status, data = ctx.client.list_plans()
    except Exception as e:
        _emit(ctx, color("读取计划列表失败：%s" % e, "red"))
        return True
    rows = (data or {}).get("plans") or []
    if not rows:
        _emit(ctx, color("本机还没有已生成的计划 → 可以选 [3] 先生成一份，"
                         "或选 [2] 导入一份。", "warn"))
        return True
    lines = ["本机已有 %d 份计划，输入序号选择基准计划（0 = 返回）：" % len(rows)]
    for i, r in enumerate(rows, 1):
        days = r.get("总工期")
        lines.append("  %2d. %-24s %-16s %s 天  %s"
                     % (i, str(r.get("plan_id") or ""), str(r.get("项目") or "—")[:16],
                        days if days is not None else "—", str(r.get("修改时间") or "")))
    ctx._plan_picker = [str(r.get("plan_id") or "") for r in rows]
    _emit(ctx, "\n".join(lines))
    return True


def _handle_plan_pick(ctx, text):
    """计划挑选态（上一条菜单刚列过计划）的数字选择。返回 True=已处理。"""
    picker = list(getattr(ctx, "_plan_picker", None) or [])
    if not picker:
        return False
    t = str(text or "").strip().strip("[]")
    if t in ("0", ""):
        ctx._plan_picker = []
        _emit(ctx, color("已取消选择。", "gray"))
        return True
    if not t.isdigit():
        ctx._plan_picker = []
        return False
    idx = int(t)
    if not (1 <= idx <= len(picker)):
        _emit(ctx, color("序号超出范围（1-%d），请重来。" % len(picker), "warn"))
        return True
    pid = picker[idx - 1]
    ctx._plan_picker = []
    _run_local_command(ctx, "/open " + pid)
    return True


def _cmd_revise_text(ctx, text):
    """把一句话交给 /revise 那条路（与斜杠命令**同一个实现**，不分叉）。

    显式打 `/revise <一句话>` 是**明确要求修改**，所以不再多问一次（用户自己下的命令）。
    """
    _run_local_command(ctx, "/revise " + text)


# ==================== 第 34 轮：修改模式的"先看后改" ====================
# 用户原话：「修改模式下，不要将任何输入都识别为修改，应该还是默认闲聊，能够基于某个计划来
# 回答问题，如果识别到修改意图时，再向用户确认修改项。」
#
# 所以修改模式里一条裸输入走三步：
#   ① 没看出修改意图 → 当**聊天**（可基于当前计划回答），一次模型调用；
#   ② 看出修改意图  → 调 `/revise` 的 **dry_run** 拿"会改什么"，打给用户看；
#   ③ 用户确认（y）→ 才真的改；否则不落盘、不进修订链（计划纹丝不动）。
# 判据分两类（保守优先 —— 宁可少判成修改，也不要误改用户的计划）：
#   ① "指向性"动词（改成/调到/删除/添加/更换/调整）：出现即算要改；
#   ② "数量"副词（加/减/缩短/延长/提前/推迟…）：**必须**同时出现被改的对象
#      （见 REVISE_FIELDS），否则"主体结构整体加 3 天"这种会被误判成修改
#      （用户实测的诉求就是"不要什么都当修改"）。
REVISE_VERBS = ("改成", "改到", "改为", "变更", "调整", "调成", "删掉", "删除", "去掉",
                "加上", "添加", "换成", "替换",
                # 第 36 轮补：用户实测原话「我想**修改**这个项目名为NUS大楼」被旧词表
                # 挡在门外（词表里只有"改成/改为"，没有"修改"），于是被当闲聊丢给模型，
                # 而闲聊提示词又告诉模型"终端会在本地拦下来" → 模型回了一句
                # 「修改项目名称属于计划级别的变更，目前我无法直接执行」这种**假答案**。
                "修改", "改", "命名", "叫",
                # "设置成/设为/命名为"这类命名动词也算修改意图
                "设置成", "设置为", "设为", "命名为", "名为", "叫做", "叫作",
                # 第 36 轮 Phase 3 补：「新增」是明确的"造一条新工序"，属于指向性动词。
                # ⚠️ 刻意**不**把"增加/添加"挪进来：它们在 `REVISE_ADVERBS` 里，
                # 那条规则要求**同时**出现修改对象（REVISE_FIELDS），这是用户明确要过的
                # 行为（"不要什么都当修改"，如"主体结构整体加 3 天"）。
                "新增")
REVISE_ADVERBS = ("缩短", "延长", "加长", "减少", "增加", "提前", "推迟", "压缩", "拉长",
                  "降到", "提到", "改一下")
REVISE_FIELDS = ("工期", "天数", "工程量", "数量", "班组", "人数", "资源", "定额", "开工",
                 "日期", "任务", "工序", "阶段", "总工期", "单价", "机械",
                 # 计划级（第 35 轮）：改名这类也算"要改"，否则会被当闲聊丢给模型
                 "项目名称", "工程名称", "计划名称", "名称", "改名",
                 # 第 36 轮补：口语里更常见的说法（"项目名"、"名字"、"标题"），
                 # 以及"这个项目叫X"里的"项目"。漏一个词就是一次假答案。
                 "项目名", "项目名字", "名字", "名号", "标题", "计划名", "工程名",
                 "楼名", "项目", "工程", "计划",
                 # 目前还没实现、但确实是"改计划"意图的对象：让它们进到解析器，
                 # 由解析器如实报"没解析出要改哪一项"，而不是由模型编一句做不到。
                 "层数", "栋数", "建筑面积", "竣工")
# 明显的"提问/闲聊"信号：即使句子里有动词，也先当聊天（避免"这个工期怎么改？"被当修改执行）
# 第 36 轮：原来只有 7 个词，于是很多"没有问号的中文问句"（"这个计划的工期是多少"）
# 会被下面 `_maybe_revision` 当成修改意图去花一次预览调用。把 _QUESTION_WORDS 并进来。
_PURE_QUESTION = ("怎么改", "如何改", "能不能改", "可以改吗", "怎么调", "能不能调", "为什么",
                  "多少", "吗", "怎么", "怎样", "是不是", "能不能", "什么时候", "是什么",
                  "叫什么", "啥", "哪些", "哪一个", "介绍一下", "讲讲",
                  # "讲/说/看/查/解释一下"这类是**让我解释**，不是要改：
                  # 它们句子里往往带着"计划/工期"这类修改对象，会被弱闸门误当成修改意图，
                  # 白花一次预览调用（实测"帮我把这份计划讲一下"）。
                  "讲一下", "说一下", "解释", "说明一下", "帮我看看", "看一下", "查一下",
                  "读一下", "概况", "简述", "总结一下")
# 第 36 轮 Phase 3：「改天/改日」这类寒暄里也含"改"，而句子往往还带着"计划"两个字，
# 动词表与对象表一撞就被判成"要改计划"（实测「改天再生成一份计划吧」）。
# 它们在中文里是"另找一天"，跟计划内容无关，直接当闲聊。
_CHITCHAT = ("改天", "改日", "回头再说", "以后再说", "下次再说", "稍后再说", "再说吧")


def _maybe_revision(text):
    """闸门**拿不准**时的"再想一下"：句子里出现了修改对象、又不是提问语气 → 值得试一次。

    为什么必须有这一层：`_looks_like_revision` 是关键词白名单，漏一个词就把用户的
    真实请求丢给闲聊，而闲聊提示词（`prompts/plan_qa.txt`）又写着"终端会在本地拦下来
    走确认流程" —— 模型据此认定"这事不归我管"，于是回一句
    「修改项目名称属于计划级别的变更，目前我无法直接执行此操作」，**是假答案**，
    因为第 35 轮已经把改名做成可执行的了（实测截图就是这个流程）。

    代价分析：这一层会多花一次 `/revise` 预览调用，只发生在"句子里有修改对象且不像
    提问"的时候；提问语气已被 `_PURE_QUESTION` 挡掉。宁可多花一次预览，也不能答错。
    """
    t = str(text or "")
    if not t:
        return False
    if any(c in t for c in _CHITCHAT):
        return False
    if any(q in t for q in _PURE_QUESTION) or t.rstrip().endswith(("？", "?")):
        return False
    return any(f in t for f in REVISE_FIELDS)


def _looks_like_revision(text):
    """这句话是不是"要改计划"（确定性关键词，不调模型）。

    保守优先：**必须**同时出现"要动什么"（对象或数值）与修改动词，否则当聊天。
    这样"告诉我目前这个计划的概况""总工期多少""为什么这么长"都不会被当成修改。
    """
    t = str(text or "")
    if not t:
        return False
    if any(c in t for c in _CHITCHAT):
        return False
    if any(q in t for q in _PURE_QUESTION) or t.rstrip().endswith(("？", "?")):
        return False
    has_field = any(f in t for f in REVISE_FIELDS)
    has_number = bool(re.search(r"\d", t))
    if any(v in t for v in REVISE_VERBS):
        return has_field or has_number
    if any(v in t for v in REVISE_ADVERBS):
        return has_field          # 数量副词必须带对象，例如"把工期缩短三天"
    return False


# 字段 → 中文标签（第 36 轮 Phase 3）。预览原来是直接打英文字段名，
# 用户看到的是「4.1.1.1 的 name → 地下室防水」这种半中半英的东西。
_FIELD_LABEL = {
    "quantity": "工程量", "duration": "工期", "norm": "定额", "crew": "班组",
    "name": "工序名", "level": "计划细度", "cost": "成本口径", "segment": "施工段",
    "plan_title": "项目名称", "start_date": "开工日期",
    "target_duration": "总工期目标", "add_task": "新增工序", "remove_task": "删除工序",
}


def _patch_line(p):
    """把一条 patch 渲染成一句人话（含 Phase 3 新增的增删工序/日期/总工期）。"""
    p = p or {}
    field = str(p.get("field") or "")
    target = p.get("target")
    value = p.get("value")
    label = _FIELD_LABEL.get(field, field or "字段")
    if field == "plan_title":
        return "项目名称 → %s" % value
    if field == "start_date":
        return "开工日期 → %s（整份日程同步平移）" % value
    if field == "target_duration":
        return "总工期目标 → %s 天（只记目标，不会替你压缩工期）" % value
    if field == "add_task":
        name = value.get("name") if isinstance(value, dict) else value
        return "新增工序「%s」（自动编号 %s）" % (name, target)
    if field == "remove_task":
        return "删除工序 %s" % target
    if isinstance(value, dict):
        value = "／".join("%s %s" % (k, v) for k, v in value.items())
    return "%s 的%s → %s" % (target, label, value)


def _preview_lines(data):
    """把 dry_run 结果转成给用户看的"会改什么"。"""
    lines = []
    applied = (data or {}).get("applied") or []
    rejected = (data or {}).get("rejected") or []
    if applied:
        lines.append("我打算改这些：")
        for p in applied[:8]:
            lines.append("   · %s" % _patch_line(p))
    if rejected:
        lines.append("这几条会被拦下：")
        for p in rejected[:5]:
            patch = p.get("patch") or {}
            lines.append("   · %s：%s" % (patch.get("target"), p.get("reason")))
    # 第 35 轮：**只有提示、没有可执行项**时要说清"接下来怎么办"，不能让用户对着
    # "确认要改吗"发呆（实测："我将总工期改为306天" → 预览说没看出改哪一项，
    # 却又问 y/n，用户只能瞎试）。
    if rejected and not applied:
        lines.append("   ↑ 没有可执行的修改，回车取消即可；也可以直接把上面提示里的编号补上重说。")
    if not applied and not rejected:
        lines.append("这句话我没看出要改哪一项。")
        # 第 36 轮 Phase 3：用户的原话是"基本什么都改不了"，而旧实现遇到解析不出
        # 只会说"没看出要改哪一项"就结束 —— **从不说明自己支持什么**。后端算好了
        # `hint`（做不到什么 + 能做的是…），这里必须打出来。
        hint = str((data or {}).get("hint") or "").strip()
        if hint:
            lines.append("   " + hint)
    return lines


def _revise_with_confirm(ctx, text):
    """修改模式里的裸输入：先判断意图 → 是修改就先预览 + 确认，否则**当聊天**。"""
    pid = str(getattr(ctx, "mode_plan_id", "") or "")
    if not _looks_like_revision(text) and not _maybe_revision(text):
        _chat_about_plan(ctx, text, pid)
        return
    try:
        status, data = ctx.client.post_revise_preview(pid, text)
    except Exception as e:
        _emit(ctx, color("算修改方案失败：%s" % e, "red"))
        return
    if status >= 400 or (data or {}).get("error"):
        _emit(ctx, color("算修改方案失败：%s" % ((data or {}).get("error") or status), "red"))
        return
    # 第 35 轮：预览**什么都没识别出来**时，不要再问"确认要改吗"（用户实测原话：
    # 「这句话我没看出要改哪一项」+「确认要改吗？输入 y」同时出现，自相矛盾）。
    # 这时说明关键词判成"要改"、但模型/规则都没解析出可执行项 —— 最可能是用户在
    # **问一个产品能力问题**（"这个能改吗"）。退回聊天去答，并且**告诉 he 怎么改**。
    if not (data or {}).get("applied") and not (data or {}).get("rejected"):
        _hint = str((data or {}).get("hint") or "").strip()
        _emit(ctx, color("这句我按「想改」来理解，但没解析出具体要改哪一项。", "gray"))
        if _hint:
            _emit(ctx, color(_hint, "gray"))
        _chat_about_plan(ctx, text, pid)
        return
    lines = ["🔧 识别到**修改意图**（这是预览，还没改）："]
    lines += _preview_lines(data)
    lines.append("")
    lines.append("确认要改吗？输入 y 执行；直接回车 / 输入别的内容 = 不改（计划保持原样）。")
    _emit(ctx, color("\n".join(lines), "accent"))
    _revise_confirm_pending(ctx, text)


def _revise_confirm_pending(ctx, text):
    ctx._revise_pending = str(text or "")
    ctx._menu_pending = False


def _route_revise_confirm(ctx, text):
    """处理"预览已给出、等用户确认"的那一次输入。返回 True=已处理。"""
    pending = str(getattr(ctx, "_revise_pending", "") or "")
    if not pending:
        return False
    ctx._revise_pending = ""
    if str(text or "").strip().lower() in ("y", "yes", "是", "改", "确认"):
        _cmd_revise_text(ctx, pending)
    else:
        _emit(ctx, color("已取消，计划保持原样（没有落盘、没有新增版本）。", "gray"))
    return True


def _chat_about_plan(ctx, text, plan_id):
    """修改模式下的**闲聊/问答**：把当前计划摘要带进上下文，让模型基于计划回答。

    为什么不让后端再判一次意图：用户明确要求取消意图识别。这里用**确定性规则**分流
    （见 `_looks_like_revision`），拿不准就当聊天 —— 聊天不会破坏任何数据。
    """
    summary = ""
    plan = getattr(ctx, "current_plan", None) or {}
    try:
        ov = (plan or {}).get("overview") or {}
        bits = []
        if ov.get("project_name"):
            bits.append("项目：%s" % ov["project_name"])
        if ov.get("total_duration_days"):
            bits.append("总工期：%s 天" % ov["total_duration_days"])
        if ov.get("planned_start_date"):
            bits.append("开工：%s" % ov["planned_start_date"])
        n = len((plan or {}).get("all_tasks_schedule") or [])
        if n:
            bits.append("工序：%d 条" % n)
        summary = "；".join(bits)
    except Exception:
        summary = ""
    prompt = text
    if summary:
        prompt = ("（当前正在修改的计划：%s，计划编号 %s）\n%s" % (summary, plan_id, text))
    # 第 35 轮：两件事一起送 —— ①`force_mode="normal"`（纯问答，别去排计划/别自动切模式）；
    # ②`chat_scope="plan"`（但口径是"基于当前计划问答"，用 plan_qa.txt 那份提示词）。
    # 只送 force_mode 会答错模式：实测用户在 revise 模式问"再详细一些"，却被回
    # 「这属于【修改计划】模式…请输入 /mode revise」——他自己就在那个模式里。
    run_chat(ctx, prompt, force_mode="normal", chat_scope="plan")


def _run_local_command(ctx, text):
    """在模式内执行一条斜杠命令（复用 commands.dispatch，保证只有一份实现）。"""
    tui.suspend_mouse()
    try:
        out = commands.dispatch(ctx, text)
    finally:
        tui.resume_mouse()
    if out:
        _emit(ctx, str(out))


def _handle_mode_command(ctx, text):
    """模式相关的**本地**命令：`/exit`、`/mode <名字>`、空参 `/revise`。返回 True=已处理。"""
    head = text.strip().split(" ", 1)
    cmd = head[0].lower()
    arg = head[1].strip() if len(head) > 1 else ""
    if cmd == "/exit" or (cmd == "/mode" and not arg):
        _set_mode(ctx, "normal", "")
        _emit(ctx, color("已回到 normal（普通模式）：这里是纯聊天，想排计划请 /mode plan。",
                         "accent"))
        return True
    if cmd == "/mode":
        want = MODE_ALIASES.get(arg.strip().lower(), "")
        if not want:
            _emit(ctx, color("用法：/mode normal | plan | revise | import", "gray"))
            return True
        # 进修改模式但还没基准计划 → 先把模式定住，再让用户挑一份
        _set_mode(ctx, want, ctx.mode_plan_id if want == "revise" else "")
        if want == "revise" and not ctx.mode_plan_id:
            _emit(ctx, _mode_menu(ctx, "进入 revise（修改计划）模式",
                                  extra="请输入 1 / 2 / 3 / 0"))
        else:
            _emit(ctx, color(_mode_banner(ctx), "accent"))
        return True
    if cmd == "/revise" and not arg:
        # 空参 /revise → 进修改模式（而不是只打一句用法）
        _set_mode(ctx, "revise", ctx.mode_plan_id)
        _emit(ctx, _mode_menu(ctx, "进入 revise（修改计划）模式",
                              extra="（也可以直接用 /revise <一句话> 改当前计划）"))
        return True
    return False


def _log_folded(ctx, event, data):
    """把被折叠掉的逐节点事件记进内存（有上限），供 /verbose 打开时回看。"""
    log = getattr(ctx, "event_log", None)
    if log is None:
        log = []
        ctx.event_log = log
    node = data.get("node") or "?"
    if event == "node_start":
        item = "节点启动 %s（%s）" % (node, data.get("title") or "")
    elif event == "node_progress":
        item = "进度 %s %s%% %s" % (node, data.get("progress"), data.get("message") or "")
    else:
        item = "%s：%s" % (node, data.get("summary") or "完成")
    log.append(item)
    if len(log) > EVENT_LOG_LIMIT:
        del log[:len(log) - EVENT_LOG_LIMIT]


def run_chat(ctx, text, force_mode=None, chat_scope=None):
    """把一句 prompt 送给后端，消费 SSE 事件流（含确认/暂停交互）。

    折叠策略：`renderer.event_action()` 说了算 —— 逐节点事件只进状态区，
    门 / 错误 / 口径类警告 / 最终结果永远完整打印（`/verbose` 打开后全量打印）。

    第 23 轮补上「长节点看得出在动」：顺序输出模式下，被折叠的 node_progress
    会经 `tui.ProgressFeed` 打**一条紧凑单行**（有节流，绝不再刷屏），
    并在长时间没有任何可见输出时兜一条"仍在跑"的心跳行。
    VT 模式（BUILDPLAN_TUI=1）走自己的状态区，行为不变。
    """
    ctx.running = True
    ctx.run_id = f"run_{int(time.time())}"
    reply = ""            # 助手的回复（由 done 事件的 note 带回）
    fold = getattr(ctx, "fold", None)
    if fold is None:
        fold = renderer.FoldState()
        ctx.fold = fold
    fold.reset_run()
    ctx.event_log = []                  # 每次运行重置"折叠事件流"缓冲
    verbose = bool(getattr(ctx, "verbose", False))
    feed = _make_progress_feed(ctx, verbose)
    feed.start_watchdog()               # 一个事件都收不到时的兜底（VT 模式不会起）
    # 第 34 轮：把**模式**随请求送给后端 —— 后端据此分流，不再做意图识别。
    # `force_mode` 用于"修改模式下基于计划聊天"：本地仍在 revise 模式，
    # 但这次请求按 normal 处理（纯问答，不引导切模式）。
    send_mode = force_mode or str(getattr(ctx, "mode", "normal") or "normal")
    # 第 35 轮：`chat_scope="plan"` = "虽然按普通模式跑，但口径是当前计划的问答"
    send_scope = chat_scope or None
    try:
        gen = ctx.client.post_chat(text, run_id=ctx.run_id, mode=send_mode,
                                  chat_scope=send_scope)
        for event, data in gen:
            data = data or {}
            fold.track(event, data)
            action = renderer.event_action(event, data, verbose)

            if action == "skip":
                # ping 之类不打印的事件也要喂给反馈器：它是"长时间无输出"的观察点
                feed.on_event(event, data)
                continue
            if action == "fold":
                # 折叠不等于丢信息：逐节点事件仍进内存事件流，/verbose 打开时可回看最近若干条
                _log_folded(ctx, event, data)
                _refresh_status(ctx, fold, verbose)
                node = data.get("node")
                feed.on_event(event, data, index=_node_index(fold, node),
                              title=fold.current_label)
                # ⚠️ 这里以前还有"本运行第一个 node_start 强制打一次状态行"和"每 4 个
                #    node_done 打一次"两处 force 心跳 —— 它们与 ProgressFeed 的紧凑行
                #    叠在同一个节点上，就是用户截图里那**重复的两行**。现在：
                #      · 逐节点反馈归 ProgressFeed（它自己会节流，断了 10 秒兜心跳）；
                #      · 状态区归 VT 模式（上面那句 _refresh_status）。
                #    退化模式不再有第二套心跳 —— 一条进度就是一条。
                continue

            if event == EV_NODE_DONE:
                # 完成行的**界面名 / 步号**由步数表补进来：用户刚在进度行里看到
                # 「第 11 步 · 第 1 轮审计：WBS 结构」，完成行不该变成 `audit_wbs`。
                _decorate_done(data, fold)
            line = renderer.render_event(event, data)
            if line:
                _emit(ctx, line, rule_before=renderer.is_gate_event(event))
            if event == EV_PLAN_FINAL:
                # 第 33 轮：计划数据落盘后**自动进入修改模式**并把新编号挂上 ——
                # 这是产品最顺的闭环（用户提案：「生成成功后自动回到改计划模式」）。
                try:
                    _set_mode(ctx, "revise", str(data.get("plan_id") or ""))
                except Exception:
                    pass
                try:
                    path = renderer.save_plan(data)
                except Exception as e:
                    _emit(ctx, color(f"  保存计划失败：{e}", "err"))
                else:
                    ctx.current_plan = data.get("plan", data)
                    ctx.current_plan_id = data.get("plan_id")
                    _emit(ctx, color(f"  已保存：{path}", "dim"), gap=False)

            elif event == EV_CONFIRM_REQUIRED:
                ctx.client.pause()
                feed.suspend()          # 用户正在做选择：绝不在他打字时刷心跳
                try:
                    confirmer.ask_confirm(ctx.client, data, run_id=ctx.run_id)
                finally:
                    feed.resume()
                    ctx.client.resume()

            elif event == EV_NODE_PAUSED:
                ctx.client.pause()
                feed.suspend()
                try:
                    confirmer.handle_pause(ctx.client, data, run_id=ctx.run_id)
                finally:
                    feed.resume()
                    ctx.client.resume()

            elif event == EV_PARAM_REVIEW:
                ctx.client.pause()
                feed.suspend()
                try:
                    confirmer.ask_param_review(ctx.client, data, run_id=ctx.run_id)
                finally:
                    feed.resume()
                    ctx.client.resume()

            elif event == EV_DONE:
                # 收尾补一行**步数小结**：顺序模式下逐节点进度是节流打的，密集的短节点
                # （读文件、读参数、补边界条件…）会被节流吞掉，用户最后看不到"到底跑了多少步"。
                # 这一行不占额外噪声（一次运行就一行），但把"这次干了多少活"交代清楚。
                step_line = _run_steps_line(fold)
                if step_line:
                    _emit(ctx, color(step_line, "dim"), gap=False)
                # 助手说了什么由 done 事件的 note 带回来（router 把闲聊/问答回复写进
                # ctx["chat_reply"]，经 _stop 的 note 上行）。这是**唯一**能拿到助手
                # 真实回复的地方，必须在这里接住。
                reply = str((data or {}).get("note") or "").strip()
                break
    except KeyboardInterrupt:
        _emit(ctx, color("⏹ 用户中断，正在取消后端任务…", "warn"), rule_before=True)
        try:
            ctx.client.post_cancel(ctx.run_id)
        except Exception:
            pass
    except ConnectionError as e:
        _emit(ctx, color(f"✖ {e}", "err"), rule_before=True)
    finally:
        ctx.running = False
        feed.stop_watchdog()
        _clear_status(ctx)
    # 只记**助手说了什么**。用户的输入已在主循环里以 ("user", text) 记过；
    # 这里若再用同一段 text 记成 "assistant"，/history 就会显示"助手说了用户的话"，
    # 而助手的真实回复反而丢了（真实缺陷）。没有回复就不记，不硬塞。
    if reply:
        ctx.history.append(("assistant", reply))


def _decorate_done(data, fold):
    """给 node_done 载荷补上**界面名**与步号（就地、只加字段）。

    · `title`：`run_plan` 下发的界面名（`第 1 轮审计：WBS 结构`），终端用它替代 code name；
    · `step` / `steps`：本次运行的第几步 / 共几步。
    拿不到（老后端、单测、探针）时**一个字段都不加**，渲染出来与改造前逐字一致。
    """
    if not isinstance(data, dict) or fold is None:
        return data
    node = data.get("node")
    idx, title = (None, "")
    if hasattr(fold, "step_of"):
        idx, title = fold.step_of(node)
    has_table = bool(getattr(fold, "steps", None))
    # 界面名与步号都只在**真拿到步数表**（run_plan）时才补：否则会把兜底常量
    # （`renderer.TOTAL_NODES`，现 27）当成分母、把老后端的 `✔ a：…` 改成
    # `✔ 甲：…` —— 退化路径必须一字不变。
    if has_table and title and title != node:
        data.setdefault("title", title)
    if idx and has_table:
        total = fold.step_count()
        if total:
            data.setdefault("step", idx)
            data.setdefault("steps", total)
    return data


def _run_steps_line(fold):
    """本次运行的"走了多少步"小结（不值得显示 → 返回 ""）。

    · 只跑了 1 步（闲聊 / 问答 / 被门拦下）：**不显示**——
      用户实测的反问就是「什么叫已完成 0/26，哪来的 26」；
    · 跑了多步：`↳ 本轮共走 9 / 26 步`（分母来自后端的 `run_plan`，不是终端自己数的）。
    另外：逐节点进度行是节流的，短节点会被吞掉几行 —— 这里补一句
    「另有 N 步没单独报进度」，用户才不会以为"系统漏跑了"。
    """
    if fold is None:
        return ""
    ran = int(getattr(fold, "ran_nodes", 0) or 0)
    if ran < 2:
        return ""
    total = fold.step_count() if hasattr(fold, "step_count") else None
    line = ("↳ 本轮共走 %d / %d 步" % (ran, total) if total and total > ran
            else "↳ 本轮共走 %d 步" % ran)
    # 逐节点进度行是节流的：短节点可能一条都没打。用户看到"共走 9 步"却只见过 3 条，
    # 会以为漏跑了 —— 这里用**节点自己**记账的 `printed`（谁真报过进度）如实说明。
    shown = len(getattr(fold, "reported_nodes", ()) or ()) or ran
    quiet = max(0, ran - shown)
    if quiet:
        line += "（另有 %d 步的进度被合并显示）" % quiet
    return line


def _handle_local(ctx, text):
    """外壳自己的命令（commands.py 不在本次改造的文件范围内，所以在这里拦）。

    目前只有 `/verbose`：默认关（只打门 / 错误 / 口径警告 / 结果），打开后恢复
    逐节点 node_start / node_progress / node_done 全量打印，并在状态区标注详细模式。
    """
    head = text.strip().split(" ", 1)
    if head[0].lower() not in ("/verbose", "/v"):
        return False
    arg = head[1].strip().lower() if len(head) > 1 else ""
    if arg in ("on", "1", "开", "是", "true"):
        ctx.verbose = True
    elif arg in ("off", "0", "关", "否", "false"):
        ctx.verbose = False
    else:
        ctx.verbose = not bool(getattr(ctx, "verbose", False))
    if ctx.verbose:
        tail = "\n  已打开：每个节点的启动 / 进度 / 完成都会打印；再打 /verbose 关掉。"
        log = [x for x in (getattr(ctx, "event_log", None) or []) if x]
        if log:
            tail += ("\n  刚才被折叠掉的事件（最近 %d 条）：" % min(12, len(log)))
            tail += "".join("\n    · %s" % x for x in log[-12:])
    else:
        tail = ("\n  已折叠逐节点进度：只完整打印门、错误、口径类警告与最终结果"
                "（完整事件流仍可用 /verbose 打开）。")
    _emit(ctx, color("🔎 详细模式：%s" % ("开" if ctx.verbose else "关"), "accent") + tail)
    return True


def _ack_text(text):
    """按输入类型给"回车已收到"的一句话（命令只回显**命令名**，不复述整句）。"""
    head = str(text or "").strip().split(" ", 1)[0]
    if not head:
        return ""
    if head.startswith("/") or head.startswith("!"):
        return "⏳ 执行 %s …" % head
    return "⏳ 已收到，正在处理…"


def _emit_input_ack(ctx, text):
    """主循环里"回车收到了"的**即时确认行**（一行、dim、立刻 flush）。

    为什么必须有（用户原话）："每按一次回车，都应该先返回一个'正在加载'之类的提示语，
    否则一按下去，不知道是挂了还是在跑"。`run_chat` 里第一个 SSE 事件可能等好几秒
    （建连 + router 节点 + 模型），那正是"以为挂了"的窗口 —— 这一行先把它填上。

    边界（都是有意的）：
      · 空输入不打：主循环 `continue`，根本不调用本函数；
      · `--run` 非交互路径**不调用**：脚本 / 测试依赖它的输出稳定；
      · 门（人工确认 / 审计 / 参数复核）前的回车不走这里：门有自己的提示与阻塞语义，
        再叠一层"正在处理"反而误导；
      · 走 `term.out(...)`：VT 模式落进滚动区，顺序模式与重定向落到 `_raw`（写+flush），
        两条路都**自带 flush** —— 非 TTY 下 stdout 有缓冲，不 flush 就看不到"立刻"。
    """
    line = _ack_text(text)
    if not line:
        return
    line = color(line, "dim")
    term = getattr(ctx, "tui", None)
    if term is not None:
        term.out(line, gap=False)
    else:
        print(line, flush=True)      # 没有终端层时也必须立刻可见（flush 是硬要求）
    feed = getattr(ctx, "feed", None)
    if feed is not None:
        feed.note_visible()


def _backend_desc(ctx):
    """后端标签：**用了 `--url` 就显示真实地址**。

    老实现只按 `switch.describe(ctx.backend)` 打固定文案，于是 `--url http://…:8015`
    启动时屏幕上仍写"云端主后端 (localhost:8000)" —— 用户在排查"到底连的哪个后端"时
    会被这句误导（第 33 轮实测发现）。
    """
    try:
        url = getattr(getattr(ctx, "client", None), "base_url", "") or ""
    except Exception:
        url = ""
    default_cloud = switch.BACKENDS.get("cloud", {}).get("url", "")
    if url and url != default_cloud:
        return "%s（%s）" % (switch.describe(ctx.backend), url)
    return switch.describe(ctx.backend)


def main(argv=None):
    parser = argparse.ArgumentParser(description="施工进度计划生成终端")
    parser.add_argument("--real", action="store_true", help="连真实云端后端 (localhost:8000)")
    parser.add_argument("--url", help="自定义后端地址，如 http://192.168.1.10:8000")
    parser.add_argument("--run", help="直接运行一句 prompt 后退出（测试用）")
    parser.add_argument("--no-tui", action="store_true",
                        help="强制退化模式（不用备用屏与光标定位）")
    args = parser.parse_args(argv)

    if args.url:
        backend, base_url = "cloud", args.url
    else:
        backend, base_url = "cloud", None

    ctx = Ctx(backend=backend, base_url=base_url)
    global _CURRENT_CTX
    _CURRENT_CTX = ctx

    # --run / --no-tui 一律退化：脚本、管道、日志里绝不做定位（规格 §B1.3）
    force_off = bool(args.run or args.no_tui)
    try:
        term = tui.Tui(force_vt=(False if force_off else None))
    except Exception:                       # 极端环境：外壳不能因为定位层而挂掉
        term = tui.Tui(force_vt=False)
    tui.install(term)
    ctx.tui = term

    exit_code = 0
    try:
        term.start()                        # 进入备用屏（退化模式为空操作）
        # ⚠️ 下面这行曾被并进上一行的行尾注释里，整条被注释吞掉 → 开场白从不打印，
        #    VT 模式下就是**满屏空白**（用户实测："什么都看不到"）。别再并行了。
        _emit(ctx, renderer.banner_text(backend=_backend_desc(ctx)), gap=False)
        # 第 34 轮：开场白里讲清"有哪几种模式、各自有什么用、怎么进"
        _emit(ctx, color("\n".join(_mode_intro_lines()), "dim"), gap=False)

        if args.run:
            run_chat(ctx, args.run)
            return 0

        # 第 33 轮：恢复上次的模式（关掉终端再打开，仍然在改同一份计划）
        _load_mode(ctx)
        if ctx.mode != "normal":
            _emit(ctx, color(_mode_banner(ctx), "accent"))

        while True:
            try:
                text = term.ask(_ask_hint(ctx)).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            ctx.history.append(("user", text))
            # 回车确认行：在任何后端调用（dispatch / run_chat）**之前**打，
            # 用户立刻知道"输入收到了、开始干活了"。详见 _emit_input_ack。
            _emit_input_ack(ctx, text)

            if text.startswith("/"):
                if _handle_mode_command(ctx, text):
                    continue
                if _handle_local(ctx, text):
                    continue
                # commands.py 里还有内建 input()（见 _cmd_quit 的运行中确认）：
                # 那段时间必须把鼠标上报挂起，否则滚轮/点击的 SGR 序列会被
                # input() 当成用户打的字灌进输入行（第 22 轮）。
                tui.suspend_mouse()
                try:
                    out = commands.dispatch(ctx, text)
                finally:
                    tui.resume_mouse()
                if out == "quit":
                    break
                if out:
                    out = str(out)
                    if text.split(" ")[0].lower() == "/help":
                        # commands.py 不归本轮改造，/verbose 由外壳补进帮助里
                        out += ("\n  /verbose         切换详细模式"
                                "（默认关：只打门/错误/结果，打开看全量过程）")
                    _emit(ctx, out)
            elif text.startswith("!"):
                # 外部命令自己会读键盘：同样先挂起鼠标上报
                tui.suspend_mouse()
                try:
                    out = commands.run_system_command(text)
                finally:
                    tui.resume_mouse()
                _emit(ctx, out)
            else:
                # ⓪ 修改模式：预览已给出、等用户确认（y = 真改）→ 最优先
                if _route_revise_confirm(ctx, text):
                    continue
                # ① 菜单还开着（刚列出"改已有 / 导入 / 先生成"）→ 菜单优先接住，
                #    与当前模式无关。用户实测踩过：打完菜单按提示选了，却被当闲聊。
                if getattr(ctx, "_menu_pending", False) and _route_mode_menu(ctx, text):
                    continue
                # ① 计划挑选态（刚列过计划）→ 数字选基准计划
                if _handle_plan_pick(ctx, text):
                    continue
                # ② 模式内的裸输入（改计划 / 导入）先处理，不走 router
                if _handle_mode_input(ctx, text):
                    continue
                # ③ 普通模式里说要改计划 → 直接进修改模式，省一次意图识别
                if _wants_revise_mode(text):
                    if ctx.mode_plan_id:
                        _enter_revise_mode(ctx, ctx.mode_plan_id)
                        _emit(ctx, color(_mode_banner(ctx), "accent"))
                    else:
                        _emit(ctx, _mode_menu(ctx))
                    continue
                run_chat(ctx, text)
    finally:
        term.stop()                         # 异常退出也必须还原屏幕（备用屏 + 光标）
    print(color("再见！", "dim"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
